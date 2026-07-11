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
  * Interval marker (memory/.last_consolidation.json) records the session count
    at the last successful consolidation. We run only when
    ``get_session_count() - last >= AUTO_INTERVAL``. This is race-immune against
    the sibling sync hook (which inserts the session row concurrently): a ±1
    drift in the count cannot cause a double-run or a miss, and it never inserts
    its own session row.
  * Lock file (memory/.consolidation.lock) prevents two overlapping workers
    from churning the same DB when compactions fire close together; a stale lock
    (older than STALE_LOCK_S) is reclaimed.

Stdin (JSON):  session_id, transcript_path, cwd, trigger
Output:        stdout empty (async stdout is not shown inline). File log only.
               Always exits 0 — a background hook must never disrupt the session.
"""
import json
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
from core.logger import get_logger

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
    """Sessions between consolidations, from config.json (fallback 5)."""
    try:
        cfg_path = _PKG_ROOT / "config.json"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        n = int(cfg.get("consolidation", {}).get("auto_interval_sessions",
                                                 _DEFAULT_INTERVAL))
        return n if n > 0 else _DEFAULT_INTERVAL
    except Exception:
        # why: config absent/malformed must not break the hook; use the default.
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
            # Stale — a previous worker died holding it. Reclaim.
            _log.info(f"reclaiming stale consolidation lock (age {age:.0f}s)")
            try:
                lock_path.unlink()
            except OSError:
                return False
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


def _read_marker(marker_path):
    try:
        return json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_marker(marker_path, data):
    try:
        marker_path.write_text(json.dumps(data, ensure_ascii=False),
                               encoding="utf-8")
    except OSError as e:
        # why: marker is observability + cadence bookkeeping; a write failure
        # only means the next run may re-consolidate — not a correctness issue.
        _log.error(f".last_consolidation.json write failed: {e}")


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception as exc:
        _log.error(f"stdin parse error: {exc}")
        sys.exit(0)

    cwd = data.get("cwd", "")
    if not cwd:
        sys.exit(0)

    memory_dir = Path(cwd) / "memory"
    db_path = memory_dir / "memory.db"
    if not db_path.exists():
        # No memory yet for this project — nothing to consolidate.
        sys.exit(0)

    lock_path = memory_dir / ".consolidation.lock"
    marker_path = memory_dir / ".last_consolidation.json"
    acquired = False
    try:
        db = MemoryDB(db_path)
        project_id = db.upsert_project(cwd)

        n_sessions = db.get_session_count(project_id)
        interval = _auto_interval()
        marker = _read_marker(marker_path)
        last = int(marker.get("last_session_count", 0) or 0)

        # Interval-since-last gate (race-immune; see module docstring).
        if n_sessions - last < interval:
            sys.exit(0)

        if not _acquire_lock(lock_path):
            _log.info("consolidation already running (lock held), skipping")
            sys.exit(0)
        acquired = True

        _log.info(f"async consolidation start (session #{n_sessions}, "
                  f"last={last}, interval={interval})")

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

        _write_marker(marker_path, {
            "last_session_count": n_sessions,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "final_active": results.get("final_active"),
            "final_topics": results.get("final_topics"),
            "semantic_dedup_archived": results.get("semantic_dedup_archived"),
            "archived_obsolete": results.get("archived_obsolete"),
        })
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
