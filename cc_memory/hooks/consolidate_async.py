#!/usr/bin/env python3
"""
PreCompact hook (ASYNC leg) — background memory consolidation.

Declared with ``"async": true`` in hooks/hooks.json, so Claude Code starts it
and immediately continues compaction WITHOUT waiting. This is the whole point:
consolidation makes several network LLM calls whose latency is variable and
occasionally large, and before v2.3.2 it ran inline in the (blocking) PreCompact
hook, where a slow run overran the hook timeout and Claude Code reported
"Hook cancelled". Moving it to an async sibling hook removes it from the
blocking compaction path permanently — a slow run can no longer surface as a
compaction failure, no matter how large the memory DB grows.

Runs the full consolidation pipeline (core.consolidate.run_consolidation) under
a BudgetGate whose deadline sits safely below this hook's own ``timeout`` (300s
in hooks/hooks.json), so even the async worker itself is never killed mid-write
(see BudgetGate docstring for the deadline proof).

Cadence + safety (this hook fires on EVERY compaction, same as the sync leg):
  * Interval marker (.ccm/.last_consolidation.json) records the session count
    at the last successful consolidation. The hook path runs when
    ``get_session_count() - last >= AUTO_INTERVAL`` OR when
    ``core.consolidate.consolidation_backlog`` says the write backlog is due
    (v2.12.0 — the sessions interval alone starved projects that never
    compact). This is race-immune against the sibling sync hook (which
    inserts the session row concurrently): a ±1 drift in the count cannot
    cause a double-run or a miss, and it never inserts its own session row.
  * Lock file (.ccm/.consolidation.lock) prevents two overlapping workers
    from churning the same DB when compactions fire close together; a stale lock
    (older than STALE_LOCK_S) is reclaimed.

Entry (two ways):
  hook       — stdin JSON: session_id, transcript_path, cwd, trigger
  standalone — ``consolidate_async.py --cwd <path>`` (v2.12.0): spawned
               DETACHED by the Stop hook's backpressure probe; gates on the
               backlog predicate only, re-checked under the lock.
Output:        stdout empty (async stdout is not shown inline). File log only.
               Always exits 0 — a background hook must never disrupt the session.
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent  # cc_memory/
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio; consolidation logging / any print must not crash the
# hook on Windows gbk (matches the other hooks).
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

from core.db import MemoryDB
from core.layout import DB_FILENAME, memory_dir as resolve_memory_dir
from core.logger import get_logger
from core.modes import read_config
# Shared entry ladder (v2.10.0): stdin parsing + the opt-out→anchor gate,
# once, in hooks/_entry.py — six hand-rolled copies is how guard drift
# between hooks kept becoming shipped defects.
from hooks._entry import parse_payload, resolve_project

_log = get_logger("consolidate_async")

# Total time-budget for one consolidation run. MUST sit below this hook's own
# `timeout` (300s in hooks/hooks.json) with margin: the BudgetGate guarantees
# the last LLM call it starts finishes by total_s - safety_s = 232s < 300s, so
# the worker is never killed mid-write. See core.consolidate.BudgetGate.
_BUDGET_TOTAL_S = 240.0
_BUDGET_SAFETY_S = 8.0

# A worker holds the lock for at most ~_BUDGET_TOTAL_S; reclaim anything older
# (a lock left by a hard-killed process) so consolidation can't wedge forever.
_STALE_LOCK_S = 360.0

_DEFAULT_INTERVAL = 5


def _auto_interval():
    """Sessions between consolidations, from config.json (fallback 5).

    Reads through `core.modes.read_config`, which is BOM-tolerant: this hook
    used to open config.json with plain ``encoding="utf-8"``, so a
    PowerShell-resaved file (``Out-File`` writes UTF-8 WITH a BOM on the
    primary platform) silently reverted the cadence to 5 — the same parser hole
    that switched the privacy opt-out off, and just as silent.

    Every degradation is logged: a consolidation cadence that is not the one
    the user configured should be explainable after the fact.
    """
    try:
        cfg, note = read_config()
        if cfg is None:
            _log.warn(f"config.json {note} -- using default consolidation "
                      f"interval {_DEFAULT_INTERVAL}")
            return _DEFAULT_INTERVAL
        section = cfg.get("consolidation")
        if section is None:
            return _DEFAULT_INTERVAL
        if not isinstance(section, dict):
            _log.warn(f"config.json consolidation is a "
                      f"{type(section).__name__}, not an object -- using "
                      f"default interval {_DEFAULT_INTERVAL}")
            return _DEFAULT_INTERVAL
        n = int(section.get("auto_interval_sessions", _DEFAULT_INTERVAL))
        if n > 0:
            return n
        _log.warn(f"config.json consolidation.auto_interval_sessions={n} is "
                  f"not positive -- using default {_DEFAULT_INTERVAL}")
        return _DEFAULT_INTERVAL
    except Exception as e:
        # why: hook contract — never raise. A non-numeric auto_interval_sessions
        # (TypeError/ValueError from int()) or any unforeseen shape falls back
        # to the shipped cadence rather than killing the consolidation leg.
        _log.warn(f"config.json consolidation.auto_interval_sessions unusable "
                  f"({type(e).__name__}: {e}) -- using default "
                  f"{_DEFAULT_INTERVAL}")
        return _DEFAULT_INTERVAL


def _acquire_lock(lock_path):
    """Atomic best-effort lock. Returns True if acquired. Reclaims a stale lock.

    Uses O_CREAT|O_EXCL so only one process wins the create race. If the lock
    already exists and is older than _STALE_LOCK_S, it's treated as abandoned
    (owner was killed) and reclaimed.
    """
    try:
        if lock_path.exists():
            age = time.time() - lock_path.stat().st_mtime
            if age < _STALE_LOCK_S:
                return False
            # Stale — a previous worker died holding it. Reclaim ATOMICALLY:
            # os.replace of the lock onto a tomb name succeeds for exactly ONE
            # contender (the loser raises FileNotFoundError), where the old
            # stat-then-unlink let two workers both pass the age check, both
            # unlink (the second a no-op), and BOTH acquire (register X5,
            # measured: results {'A': True, 'B': True}).
            tomb = lock_path.with_name(lock_path.name + f".stale-{os.getpid()}")
            try:
                os.replace(str(lock_path), str(tomb))
            except FileNotFoundError:
                return False  # another contender reclaimed it first
            try:
                tomb.unlink()
            except OSError:
                # why: the tomb is inert debris carrying this pid; the next
                # reclaim by this pid overwrites it via os.replace anyway
                pass
            _log.info(f"reclaimed stale consolidation lock (age {age:.0f}s)")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}")
        return True
    except FileExistsError:
        # Another worker won the create race between our check and open.
        return False
    except OSError as e:
        _log.error(f"lock acquire error: {e}")
        return False


def _release_lock(lock_path):
    try:
        lock_path.unlink()
    except OSError:
        # why: lock already gone (reclaimed) — nothing to clean up.
        pass


def main():
    # Standalone spawn mode (v2.12.0): the Stop hook's backpressure probe
    # launches this same script DETACHED with `--cwd <path>` and no stdin.
    # Everything downstream — the opt-out→anchor gate, the lock, the budget,
    # the marker — is identical to the hook path; only the cadence predicate
    # differs (backlog-only: a spawned worker was launched BECAUSE of the
    # backlog, so the sessions-interval gate would wrongly veto it).
    standalone = len(sys.argv) >= 3 and sys.argv[1] == "--cwd"
    if standalone:
        data = {"cwd": sys.argv[2]}
    else:
        # Logged: this hook is rare, and a skipped consolidation is worth
        # being able to explain afterwards.
        data = parse_payload(log=_log)
    if data is None:
        sys.exit(0)

    # FIELD types, not just the container type. The guard above only makes
    # `.get()` legal; `Path(["a"])` still raises TypeError out here, outside any
    # try — rc=1 plus a traceback on stderr. Verified before the fix:
    # `echo '{"cwd":["a"]}' | python consolidate_async.py` exited 1.
    cwd = data.get("cwd", "")
    if not isinstance(cwd, str) or not cwd:
        sys.exit(0)

    # Opt-out gate + root anchor via the ONE shared gate (hooks/_entry.py).
    # Consolidation is the heaviest LLM leg in the plugin (semantic de-dup
    # ships memory content to the Anthropic API); an excluded project must
    # not reach it just because its state directory predates the exclusion. Rare
    # hook, so it carries the reporting duty: the exclusion is logged and
    # `project_root` announces any redirection.
    resolved = resolve_project(cwd, log=_log)
    if resolved is None:
        _log.info(f"skipped: {cwd} is in config.json excluded_projects")
        sys.exit(0)
    cwd = resolved

    # A rare hook, so it passes the logger: if a pre-v2.13.0 `memory/` is
    # migrated here, or could not be, that is exactly the kind of one-off
    # event this hook's log exists to record.
    memory_dir = resolve_memory_dir(cwd, log=_log)
    db_path = memory_dir / DB_FILENAME
    if not db_path.exists():
        # No memory yet for this project — nothing to consolidate.
        sys.exit(0)

    lock_path = memory_dir / ".consolidation.lock"
    acquired = False
    try:
        db = MemoryDB(db_path)
        project_id = db.upsert_project(cwd)

        n_sessions = db.get_session_count(project_id)
        interval = _auto_interval()
        # The marker read is path-validated (core.consolidate.
        # read_consolidation_marker): the marker follows the DIRECTORY, but
        # session counts follow the project ROW, which is keyed by path — so
        # after a rename the same state directory carried a marker counted against
        # the OLD row while the new row's count restarted at 0, and
        # `n_sessions - last` went negative: consolidation silently stalled
        # for interval+last more sessions (register C4, measured: marker
        # last=6, new row sessions=0, next run at session 11). Any marker not
        # stamped for THIS path — a different path OR no path at all — reads
        # as never-run (register r6-B8): grandfathering pathless legacy
        # markers kept the rename residual open, and the price is ONE early
        # consolidation per project, async and budget-gated.
        from core.consolidate import (consolidation_backlog,
                                      read_consolidation_marker,
                                      write_consolidation_marker)
        marker = read_consolidation_marker(memory_dir, cwd)
        last = int(marker.get("last_session_count", 0) or 0)

        # Cadence gate (race-immune; see module docstring). Two ways to be
        # due since v2.12.0: the sessions interval (the v2.3.2 rule — only
        # meaningful when compactions happen), OR a write backlog
        # (core.consolidate.consolidation_backlog — the trigger that ends
        # the starvation of projects that never compact). A standalone
        # spawn checks the backlog ONLY: it was launched because of it,
        # and this re-check under the lock is what makes a racing spawn a
        # no-op instead of a double-run.
        due_sessions = (not standalone) and (n_sessions - last >= interval)
        backlog_reason = consolidation_backlog(db, project_id, marker)
        if not due_sessions and backlog_reason is None:
            sys.exit(0)

        if not _acquire_lock(lock_path):
            _log.info("consolidation already running (lock held), skipping")
            sys.exit(0)
        acquired = True

        _log.info(f"async consolidation start (session #{n_sessions}, "
                  f"last={last}, interval={interval}, "
                  f"backlog={backlog_reason or 'none'}, "
                  f"standalone={standalone})")

        from core.consolidate import run_consolidation, BudgetGate
        gate = BudgetGate(total_s=_BUDGET_TOTAL_S, safety_s=_BUDGET_SAFETY_S)
        results = run_consolidation(cwd, use_llm=True, verbose=True, budget=gate)

        # Consolidation archives/merges rows; refresh the generated MEMORY.md so
        # it reflects the post-consolidation state (the DB is authoritative for
        # SessionStart injection, but MEMORY.md is a user/Claude-facing artifact).
        try:
            from llm.memory_writer import regenerate_memory_index
            regenerate_memory_index(db, project_id, memory_dir)
        except Exception as e:
            _log.error(f"MEMORY.md regen after consolidation failed: {e}")

        # ONE marker writer (core.consolidate.write_consolidation_marker),
        # shared with the manual CLI path; it stamps `last_memory_id`, the
        # row-id watermark the backpressure predicate subtracts against.
        write_consolidation_marker(db, project_id, memory_dir, cwd, results)
        _log.info(f"async consolidation OK: {results.get('final_active')} active "
                  f"memories, {results.get('final_topics')} topics")

    except Exception:
        _log.error_tb("consolidate_async ERROR")
    finally:
        if acquired:
            _release_lock(lock_path)

    sys.exit(0)


if __name__ == "__main__":
    main()
