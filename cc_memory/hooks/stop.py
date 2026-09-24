#!/usr/bin/env python3
"""
Stop hook — fires after each Claude response.

Three jobs:
  1. OBSERVER: extract memories from this turn's tool observations via Haiku.
     Saves through llm.memory_writer.upsert_smart (anti-patch). Since
     v2.16.0 the LLM call runs in a DETACHED worker (`stop.py --observe
     <cwd> <session_id>`, spawned by _maybe_spawn_observer) so the harness
     never waits on it; the hook itself pays one cursor read and a Popen.
  2. IDLE REORG: every 5 turns, run lightweight no-LLM cleanup +
     MEMORY.md regen + PROGRESS.md patch.
  3. PROGRESS.md PATCH: every turn, update files_touched and open_todos
     based on observations.
A continuation Stop (`stop_hook_active`, v2.16.0) skips all three and only
re-judges plan enforcement.

NOTE: The previous "save-memories reminder" text spam has been REMOVED.
The forced <system-reminder> in SessionStart and the auto-saves above do
the work; spamming Claude with "remember to call /save-memories" was noise.
"""
import json
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path

# Captured as early as possible: the reference instant for this hook's
# wall-clock budget (see _LLM_DEADLINE_S below). Taken BEFORE the package
# imports so their cost is charged against the budget instead of hidden from
# it. Same idiom as hooks/session_start.py:31.
_HOOK_T0 = time.monotonic()

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio (the refusal document carries directive slugs and
# plan text, which can be non-ASCII); avoid gbk crashes on Windows.
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

from core.db import CATEGORIES, MemoryDB
from core.layout import DB_FILENAME, memory_dir as resolve_memory_dir
from core.logger import get_logger
# read_marker, not bare read_text: it refuses to follow a planted symlink —
# load-bearing for the PROMPT marker below, whose content is spliced into the
# Anthropic request. safe_id replaces this hook's private `[:16]` truncating
# copy (three hooks <!--ce:hooks:asof--> each had one, and truncation
# cross-wired any two sessions sharing a 16-char prefix).
from core.markers import marker_path, read_marker, safe_id as _safe_id, write_marker
# Shared entry ladder (v2.10.0): stdin parsing + the opt-out→anchor gate,
# once, in hooks/_entry.py — six hand-rolled copies is how guard drift
# between hooks kept becoming shipped defects.
from hooks._entry import parse_payload, resolve_project, spawn_detached
from core.idle import maybe_run_idle
from core.progress import write_progress_md
from core import plan as plan_mod
from llm.memory_writer import upsert_batch

_log = get_logger("stop")

_MIN_OBS_FOR_EVAL = 3
# Oldest-first slice fed to ONE Stop-observer call. Named, because the
# fetch bound and the fed bound must be the SAME number: when they were
# two literal 20s in two branches they silently disagreed about which end
# of the queue they meant (register r7-B2).
_OBS_FED_PER_STOP = 20
# The detached observer worker's lock (v2.16.0, A1), taken through
# `consolidate_async._acquire_lock` — the lock's ONE policy point — with its
# own horizon: one worker holds it for at most _LLM_DEADLINE_S plus the
# upsert, so a lock older than a minute belongs to a worker that died. Not
# named `_STALE_LOCK_S`: that name is the consolidation worker's, imported
# below, and the gate refuses a second spelling of it in this file.
OBSERVER_LOCK = ".observer.lock"
_OBSERVER_STALE_LOCK_S = 60.0
_TURN_FILE_PREFIX = "cc_mem_turns_"
_PROMPT_FILE_PREFIX = "cc_mem_prompt_"
# (the observer watermark used to live in a `cc_mem_eval_` marker here; it is
# `projects.obs_watermark` since v2.8.0 — see _observer_evaluate. The prefix
# stays in ui/installer.py's sweep list so an uninstall still removes the
# files an older install wrote.)
# (`cc_mem_refine_` was the plan-refiner nudge cooldown until v2.11.0. The
# nudge was ADVISORY and rate-limited to once per 5 turns, so an unrefined
# plan produced one quiet line every five turns and nothing else — measured in
# lore_disaster: a 51,237-char raw plan sat unrefined while PLAN.md,
# plan-status and the drift guardian all answered from the PREVIOUS plan.
# Plan state is now ENFORCED at Stop; see _block_attempt / _emit_block and
# core.plan.blocking_reasons. The prefix stays in ui/installer.py's sweep
# list so an uninstall still removes files an older install wrote.)
_BLOCK_STALE_DIRECTIVE_TURNS = 25

# ── LLM wall-clock envelope (v2.5.0) ───────────────────────────────────────
# hooks/hooks.json gives Stop 22s. TWO bounds, because per-leg timeouts alone
# are NOT one:
#
#   _API_TIMEOUT /     the per-leg ceilings for the COMMON case, when there is
#   _FALLBACK_TIMEOUT  plenty of budget left. call_llm bounds the Anthropic
#                      legs at 2 candidates, so ONE call's NOMINAL cost is
#                      2*7 + 3 = 17s (the Ollama leg runs only when config
#                      `ccl.enabled` is true).
#   _LLM_DEADLINE_S    an ABSOLUTE instant (from _HOOK_T0) by which the
#                      observer's LLM call must be FINISHED. call_llm clamps
#                      every leg's socket timeout to the time actually
#                      remaining and skips a leg with <1s left, so the bound
#                      holds no matter how many credential candidates exist.
#
# Why the nominal arithmetic is not enough: `urlopen(req, timeout=t)` sets a
# PER-SOCKET-OPERATION timeout — it covers neither DNS resolution nor the TLS
# handshake, so a leg overruns t. An adversarial verifier measured a
# SUCCESSFUL leg at 11.81s against a nominal timeout=8 (1.48x) in ~5% of legs.
# Reproduced here with both credential candidates live, ccl.enabled=true and
# every leg stalling at 1.48x: 3 legs = 10.36 + 10.36 + 4.44 = 25.16s, hook
# total 25.45s — OVER the 22s budget. A timeout kill is TerminateProcess: no
# `except`, no `finally`, i.e. the v2.3.2 / v2.4.2 "killed mid-write" class.
#
# Worst case WITH the deadline = _LLM_DEADLINE_S + the final (clamped) leg's
# overrun = 14 + 0.48*7 = 17.4s, leaving ~4.6s for the idle reorg, the
# PROGRESS.md patch and interpreter teardown (measured non-LLM cost: 0.24s on
# a small project). Same stall run, measured after: 16.05s total.
#
# Since v2.16.0 (A1) the call runs in the DETACHED worker, so this deadline
# bounds the worker's own wall-clock and no longer the hook's: the hook
# returns in the non-LLM cost alone. The numbers stay as calibrated — the
# worker still holds `.observer.lock` for exactly this long at most, which
# is what _OBSERVER_STALE_LOCK_S is sized against.
_API_TIMEOUT = 7
_FALLBACK_TIMEOUT = 3
_LLM_DEADLINE_S = 14.0

_OBSERVER_PROMPT = """\
You are a memory observer. Given a user's request and a batch of tool observations \
from a Claude Code session, extract ONLY the observations worth remembering long-term.

Output a JSON array of objects:
- "category": """ + "|".join(CATEGORIES) + """
- "content": one concise, self-contained sentence with specific values
- "importance": 1-5 (5=critical, 4=important, 3=useful, 2=minor)
- "topic": short keyword for grouping

Rules:
- Only save CONCLUSIONS and OUTCOMES, not intermediate steps
- Skip: file reads without insight, routine git commands, navigation
- Each memory must be understandable WITHOUT conversation context
- Include specific values: file names, numbers, error messages
- 0-5 memories max per batch. Return [] if nothing worth saving.
- Output ONLY valid JSON array."""


def _read_turn_count(session_id):
    f = marker_path(_TURN_FILE_PREFIX, _safe_id(session_id))
    try:
        return int(read_marker(f, "0").strip() or 0)
    except ValueError:
        # why: corrupted turn counter file — treat as 0 (best-effort; the
        # next UserPromptSubmit will overwrite it correctly)
        return 0


# (_claim_refine_nudge lived here until v2.11.0. It rate-limited the
# plan-refiner ADVISORY to once per 5 turns — and a rate-limited advisory is
# exactly how a plan sat unrefined indefinitely while every reader answered
# from the stale structured copy. Enforcement replaced it; the dead helper is
# deleted rather than left as a second, unreachable policy. Its temp-marker
# prefix stays registered in ui/installer.py so an uninstall still sweeps what
# older installs wrote.)


_BLOCK_MARKER_PREFIX = plan_mod.BLOCK_MARKER_PREFIX


def _block_attempt(session_id, keys):
    """How many times in a row we have refused for THIS exact condition set.

    Keyed by the sorted condition keys, not by a bare counter: when the
    conditions change the count restarts, so fixing one problem and hitting a
    different one does not consume the escape budget of the new one. Returns
    the 1-based attempt number, or None when the marker cannot be read/written
    (in which case the caller degrades to advisory rather than risk a Stop
    loop it cannot count).
    """
    import hashlib
    digest = hashlib.sha256("|".join(sorted(keys)).encode()).hexdigest()[:12]
    f = marker_path(_BLOCK_MARKER_PREFIX, _safe_id(session_id))
    prev_digest, prev_n = "", 0
    # FIRST line only: the second line, when present, is the advisory a
    # previous Stop parked for the next UserPromptSubmit (v2.16.0, D8), and
    # reading it into `n_text` would restart the count — re-arming the
    # block on the turn after the budget was spent.
    raw = read_marker(f, "").strip().split("\n", 1)[0].strip()
    if ":" in raw:
        try:
            prev_digest, n_text = raw.split(":", 1)
            prev_n = int(n_text)
        except ValueError:
            # why: corrupt marker — restart the count rather than inherit a
            # bogus one; a restarted budget is safe, an inflated one is not.
            prev_digest, prev_n = "", 0
    n = prev_n + 1 if prev_digest == digest else 1
    # write_marker NEVER RAISES — its docstring says so in its first line, and
    # all three of its failure paths `return False`. The `except OSError` that
    # used to sit here was therefore dead code and the return value was
    # discarded, which inverted this function's entire contract: on a marker
    # directory `core.markers` refuses (a mode-1777 temp root, a planted
    # reparse point, a read-only temp) nothing persisted, every later read came
    # back empty, `n` was 1 forever, and the escape budget NEVER released —
    # measured [1,1,1,1,1,1,1,1] over eight consecutive Stops, i.e. a session
    # that can no longer end. "An unbreakable block is worse than no block" is
    # the invariant this line exists to hold, so the failure must be OBSERVED,
    # not merely handled in a shape that cannot fire.
    if not write_marker(f, f"{digest}:{n}"):
        # why: cannot persist the attempt count. Blocking without a countable
        # escape budget could trap the session, so the caller treats None as
        # "advise, do not block" — the safe direction.
        return None
    return n


def _note_advisory(session_id, line):
    """Park `line` for the next UserPromptSubmit to print. Never raises.

    v2.16.0 (D8). A Stop hook's stdout is shown in the transcript view and
    reaches neither the model nor the user's terminal (verified against the
    hooks documentation, docs/plans/2026-09-24), so the "loud advisory" the
    spent escape budget degraded to since v2.11.0 was silent in fact. The
    one automatic stream that IS injected and has a turn to attach to is
    UserPromptSubmit's, so the line rides the session's block marker —
    second line, below the attempt count `_block_attempt` keeps on the
    first — and `hooks/user_prompt.py:_emit_block_advisory` prints it once
    and drops it. `write_marker` never raises; a marker that cannot be
    written costs the advisory, which is what a Stop print cost anyway.
    """
    f = marker_path(_BLOCK_MARKER_PREFIX, _safe_id(session_id))
    head = read_marker(f, "").strip().split("\n", 1)[0].strip()
    write_marker(f, head + "\n" + line.strip())


def _block_reset(session_id):
    """End the refusal streak the moment a turn is allowed to close. Never raises.

    `_block_attempt` counts CONSECUTIVE refusals of one condition set, and
    until this function existed nothing ever ended a streak: the marker
    survived the Stop on which the condition was resolved, so the next time
    the SAME set came back — and `plan-drift` comes back every 8 turns by
    design — the count resumed where it had stopped. After three resolved
    refusals in a session the fourth episode opened at attempt 4, past
    `_BLOCK_MAX_CONSECUTIVE`, and plan enforcement was an advisory for the
    rest of that session: the v2.11.0 measurement (a plan unrefined for
    416 user messages) waiting to recur one budget later. Measured with the
    real hook before the fix: refuse, refuse, resolve, re-trigger -> the
    second episode's first Stop was attempt 3, its second was already
    advisory.

    Clears only a marker that carries a count, so a session that was never
    refused costs one failed open per turn and no write. Called on every
    Stop that does not block, live plan or not: a cleared plan is a resolved
    condition too.
    """
    f = marker_path(_BLOCK_MARKER_PREFIX, _safe_id(session_id))
    if read_marker(f, "").strip():
        write_marker(f, "")


def _idle_directives(db, project_id, idle_turns=25, plan_row=None):
    """Active directives that have gone `idle_turns` turns untouched.

    Idleness is measured from the plan's own turn counter rather than from
    wall-clock time: a directive is not stale because a week passed, it is
    stale because many turns of work went by without it moving. Returns rows
    with `turns_idle` filled in, the shape plan.blocking_reasons expects.

    PER-DIRECTIVE, from the MONOTONIC clock (v9). Idleness is
    `plan_active.turns_total - directives.turns_at_touch`: two counters that
    only ever increase, so the answer is plain subtraction and no reset can
    distort it.

    Two earlier shapes were wrong and are worth naming, because both looked
    right. v2.11.0 stamped EVERY active directive with the project's
    `turns_since_last_guardian`, so one recorded ten seconds ago was announced
    as "no progress for 40 turns" and refused the user's turn. v2.11.1 added a
    "touched since the guardian window opened?" guard, which killed that false
    positive but inherited a worse one from the counter it still read: that
    counter is RESET by `/cc-mem plan-check` and by every plan replacement, so
    a directive genuinely untouched for 100 turns looked fresh again the moment
    anybody ran a guardian check — the ledger forgiving exactly the neglect it
    exists to surface. A resettable counter cannot measure elapsed neglect; the
    fix is a clock that never resets, not a cleverer comparison against one
    that does.
    """
    try:
        rows = db.list_directives(project_id, status="active")
    except Exception:
        # why: the v8/v9 columns may be absent on a DB an older ccm created and
        # a newer one has not opened yet. No ledger simply means no directive
        # conditions — never a crash in the Stop path.
        return []
    # The caller's row when it holds one (v2.16.0, A7): main() had already
    # read it, this function read it again, and main() re-read it after
    # the bump — one row, three SELECTs per turn.
    plan_row = plan_row if plan_row is not None else (
        db.get_plan_active(project_id) or {})
    turns_total = int(plan_row.get("turns_total") or 0)
    out = []
    for row in rows:
        row = dict(row)
        idle = turns_total - int(row.get("turns_at_touch") or 0)
        if idle < idle_turns:
            continue
        row["turns_idle"] = idle
        out.append(row)
    return out


def _emit_block(reason_text):
    """Stop-hook refusal. Top-level decision/reason, exit 0 — the shape the
    harness accepts (verified against the working stop_guard implementation
    on this machine, not inferred from documentation)."""
    payload = {"decision": "block", "reason": reason_text}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def _maybe_spawn_observer(cwd, session_id, memory_dir, db, project_id):
    """Spawn the DETACHED observer worker when there is something to observe.

    v2.16.0 (A1). The observer's LLM call used to run INSIDE this hook —
    up to _LLM_DEADLINE_S of the 22 s envelope, every turn, with Claude Code
    waiting on it — so a project with a credential paid the whole call at
    every Stop. The call now runs in `stop.py --observe <cwd> <session_id>`,
    spawned detached exactly like the backpressure worker, and this hook
    pays for one cursor read, one bounded SELECT and a Popen.

    No LLM and no marker read, in THIS order, so a test without a
    credential can never spawn: no key -> False; fewer than
    _MIN_OBS_FOR_EVAL rows above the cursor -> False (the worker would
    return at once); backed off after a failed call -> False; a
    `.observer.lock` younger than _OBSERVER_STALE_LOCK_S (a worker is still
    running) -> False. No kick-style cooldown: a failing worker writes the
    backoff, a crashed one leaves a lock that expires in a minute. Returns
    True when a worker was started.
    """
    from core.auth import get_api_key, llm_backoff
    api_key, _ = get_api_key()
    if not api_key:
        return False
    last_eval = db.observer_watermark(project_id, window=_OBS_FED_PER_STOP)
    pending = db.get_observations_since(project_id, last_eval or 0,
                                        limit=_MIN_OBS_FOR_EVAL)
    if len(pending) < _MIN_OBS_FOR_EVAL:
        return False
    if llm_backoff(memory_dir)[0]:
        return False
    try:
        lock_age = time.time() - (memory_dir / OBSERVER_LOCK).stat().st_mtime
    except OSError:
        # why: no lock (the common case) or an unstatable one; the worker's
        # own O_CREAT|O_EXCL acquire is the real guard and exits if it loses
        lock_age = None
    if lock_age is not None and lock_age < _OBSERVER_STALE_LOCK_S:
        return False
    return spawn_detached([sys.executable, str(_HERE / "stop.py"),
                           "--observe", str(cwd), str(session_id)])


def _observe_worker(cwd, session_id):
    """`stop.py --observe <cwd> <session_id>`: the detached observer (v2.16.0).

    Everything the inline observer did, in a process the harness does not
    wait for: the hook's own ladder (opt-out -> anchor -> state directory),
    `.observer.lock` through `consolidate_async._acquire_lock` with the
    60 s horizon, then `_observer_evaluate`, which re-checks the backoff
    under the lock, records a failure and advances the cursor only after
    its upsert. Never raises; the caller exits 0 either way.
    """
    try:
        if not isinstance(cwd, str) or not cwd \
                or not isinstance(session_id, str) or not session_id:
            return
        resolved = resolve_project(cwd)
        if resolved is None:
            return
        cwd = resolved
        memory_dir = resolve_memory_dir(cwd)
        db_path = memory_dir / DB_FILENAME
        if not db_path.exists():
            return
        from hooks.consolidate_async import _acquire_lock, _release_lock
        lock = memory_dir / OBSERVER_LOCK
        if not _acquire_lock(lock, stale_s=_OBSERVER_STALE_LOCK_S):
            return
        try:
            _observer_evaluate(cwd, session_id, memory_dir,
                               db=MemoryDB(db_path))
        finally:
            _release_lock(lock)
    except Exception:
        _log.error_tb("observer worker")


def _observer_evaluate(cwd, session_id, memory_dir, db=None):
    from core.auth import (clear_llm_backoff, get_api_key, llm_backoff,
                           note_llm_failure)
    from core.privacy import clean_for_storage

    db_path = memory_dir / DB_FILENAME
    if not db_path.exists():
        return 0

    api_key, _ = get_api_key()
    if not api_key:
        return 0
    # Backed off after a failed call (v2.16.0): the same prompt used to go
    # out again on the very next turn, every turn, for the length of an
    # outage. `core.auth.llm_backoff` is the one reader of the record.
    backed_off, backoff = llm_backoff(memory_dir)
    if backed_off:
        _log.info(f"observer: backed off after {backoff.get('failures')} "
                  f"failure(s) until {backoff.get('until')}: "
                  f"{backoff.get('reason', '')!r}")
        return 0

    # The caller's handle when it has one (v2.16.0, one handle per hook);
    # a direct caller without one still gets its own.
    db = db if db is not None else MemoryDB(db_path)
    project_id = db.upsert_project(cwd)

    safe = _safe_id(session_id)
    # The watermark is a PROJECT column, not a per-session temp marker. It held
    # the last-evaluated observation ROW ID (an ISO timestamp hid every row
    # written after a backwards clock step — see db.get_observations_since),
    # but it was keyed by `safe_id(session_id)` while observations are
    # per-project and are deleted only by PreCompact. Every new session
    # therefore began with no watermark and replayed the project's whole
    # unconsumed backlog at one Anthropic call per Stop, and a marker directory
    # that `core.markers` refuses (which that module explicitly designs for)
    # made the replay permanent. `observer_watermark` seeds a never-run project
    # at the live end of the queue rather than at row 0, so an upgrade does not
    # re-walk a 5 000-row history to reach what the user is doing now.
    last_eval = db.observer_watermark(project_id, window=_OBS_FED_PER_STOP)

    # ONE reader, oldest-first, for both cases. The `else` branch used to call
    # `get_recent_observations`, which is `ORDER BY id DESC` — so on the FIRST
    # Stop of every session (no marker yet, which is the branch every session
    # takes exactly once) the model was shown the NEWEST 20 and the watermark
    # below was then set to their maximum. Every older unevaluated row was
    # recorded as evaluated without ever entering a prompt: measured 30 rows
    # in, ids 1-10 skipped permanently. That is the same defect register r6-B3
    # fixed in `hooks/pre_compact.py`, left standing in one of this hook's two
    # branches — and `_as_row_id` already maps an absent/legacy watermark to
    # "from the start", so the two branches were never needed.
    observations = db.get_observations_since(project_id, last_eval or 0,
                                             limit=_OBS_FED_PER_STOP)

    if len(observations) < _MIN_OBS_FOR_EVAL:
        return 0

    prompt_file = marker_path(_PROMPT_FILE_PREFIX, safe)
    # clean_for_storage on the INPUT too (v2.5.2). The observer's OUTPUT
    # has always been cleaned (see the `cleaned` loop below), but this
    # marker was spliced RAW into the Anthropic request at `user_context`
    # — so `<private>…</private>` typed by the user left the machine.
    # hooks/user_prompt.py now writes the marker already cleaned; this
    # stays because the marker is a plain temp file with a predictable
    # per-session name that OUTLIVES a plugin upgrade, so one written by
    # a pre-2.5.2 UserPromptSubmit (or by anything else) must not leak.
    # read_marker (v2.8.0) additionally refuses to follow a symlink: this
    # is the one marker whose content reaches the Anthropic API, so a
    # planted link would otherwise exfiltrate any file the user can read.
    user_prompt = clean_for_storage(read_marker(prompt_file, "").strip())

    # OLDEST prefix, and the watermark below covers exactly this slice
    # (register r6-B3 — the same shape pre_compact fixed as B3): the prompt
    # used to take the newest 20 while the marker advanced over everything
    # fetched, so on a >20 backlog the oldest rows were marked evaluated
    # having never been shown to the model. Feeding oldest-first catches the
    # backlog up one Stop at a time; rows past the slice keep ids above the
    # watermark and are fed next turn.
    obs_fed = observations[:_OBS_FED_PER_STOP]
    obs_lines = []
    for o in obs_fed:
        tool = o["tool_name"]
        inp = (o.get("tool_input", "") or "")[:200]
        out = (o.get("tool_output", "") or "")[:100]
        obs_lines.append(f"[{tool}] {inp}" + (f" -> {out}" if out else ""))

    obs_text = "\n".join(obs_lines)
    user_context = f"User request: {user_prompt}\n\n" if user_prompt else ""
    user_msg = f"{user_context}Tool observations:\n{obs_text}"

    try:
        from llm.ccl_backend import call_llm
        from llm.parse import extract_json
        text = call_llm(_OBSERVER_PROMPT, user_msg, api_key,
                        max_tokens=1000, timeout=_API_TIMEOUT,
                        fallback_timeout=_FALLBACK_TIMEOUT,
                        deadline=_HOOK_T0 + _LLM_DEADLINE_S)
        memories = extract_json(text, kind="array")
        if memories is None:
            return 0

        # Sanitize content and route through memory_writer
        cleaned = []
        for m in memories:
            if not isinstance(m, dict):
                continue
            content = clean_for_storage((m.get("content") or "").strip())
            if not content or len(content) < 10:
                continue
            cleaned.append({
                "category": m.get("category", "note"),
                "content": content,
                "importance": max(1, min(int(m.get("importance", 3)), 5)),
                "topic": m.get("topic", "") if isinstance(m.get("topic", ""), str) else "",
                "tags": ["observer", "realtime"],
            })

        counts = upsert_batch(db, project_id, None, cleaned, memory_dir=memory_dir)
        n_total = sum(counts.get(k, 0) for k in ("inserted", "merged", "superseded"))

        # write_marker, not write_text: it never raises, and it refuses to
        # follow a symlink. Marker write is best-effort either way; the next
        # eval scans the recent window instead of resuming from the
        # watermark — degraded but works.
        #
        # The HIGHEST id actually FED to the model, not the highest fetched
        # (register r6-B3) and not the clock. A timestamp watermark went
        # backwards whenever the system clock did; a fetched-max watermark
        # marked rows evaluated that the prompt never contained. Rows beyond
        # the fed slice — and any PostToolUse landing during the LLM call —
        # keep higher ids and are picked up next turn.
        db.advance_observer_watermark(
            project_id, max((o["id"] for o in obs_fed), default=0))
        # A call that came back ends the outage, whatever the answer held.
        clear_llm_backoff(memory_dir)

        if n_total:
            _log.info(
                f"observer: {counts.get('inserted',0)} new, "
                f"{counts.get('merged',0)} merged, "
                f"{counts.get('superseded',0)} superseded "
                f"from {len(obs_fed)} of {len(observations)} obs"
            )
        return n_total

    except RuntimeError as e:
        # `call_llm` folds every failed leg into ONE RuntimeError: that is the
        # "the model could not be reached" signal, and until v2.16.0 the tuple
        # below did not name it, so it escaped this function, the cursor
        # stayed put, and the same prompt went out again next turn. Recorded
        # once here; the Stops that follow skip the call until the backoff
        # expires. A parsed-but-useless answer (`extract_json` -> None) is a
        # bad answer, not an outage, and writes no backoff.
        n = note_llm_failure(memory_dir, str(e))
        _log.error(f"observer evaluation failed: {e} (backoff #{n})")
        return 0
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, OSError, KeyError, ValueError) as e:
        _log.error(f"observer evaluation failed: {e}")
        return 0


# Backpressure spawn rate limit. The spawned worker re-checks the backlog
# under the consolidation lock, so a duplicate spawn is a no-op — but a worker
# that keeps FAILING before it writes the marker would otherwise be re-spawned
# on every single turn. Ten minutes matches the worker's own stale-lock
# horizon (consolidate_async._STALE_LOCK_S = 360s) with margin.
_CONSOLIDATE_KICK_COOLDOWN_S = 600.0


def _maybe_kick_consolidation(cwd, memory_dir, db, project_id):
    """Spawn a DETACHED background consolidation when the write backlog is due.

    v2.12.0. Consolidation's only automatic trigger used to be the async
    PreCompact leg gated on "≥ N sessions since last run" — both halves
    assume compactions happen, so a project worked in short sessions starved:
    this repository measured 349 memories written in one month against a
    17-day-old consolidation marker, with SessionStart injecting topic
    summaries three minor versions stale. The Stop hook fires every turn, so
    it is where backpressure can be SEEN; the decision lives in
    `core.consolidate.consolidation_backlog` and the work stays in
    consolidate_async.py (spawned `--cwd`, budget-gated, lock-guarded) so
    this hook's 22s envelope only ever pays for one COUNT query and a
    detached Popen.

    Returns True when a worker was spawned. Never raises past its caller's
    try (hook contract).
    """
    from core.consolidate import (consolidation_backlog,
                                  read_consolidation_marker)
    marker = read_consolidation_marker(memory_dir, str(cwd), project_id)
    reason = consolidation_backlog(db, project_id, marker)
    if reason is None:
        return False
    # A LIVE worker defers the spawn; an ABANDONED lock must not.
    # `consolidate_async._acquire_lock` is the lock's ONE policy point: it
    # refuses while a lock is younger than `_STALE_LOCK_S` and RECLAIMS one
    # older, because a worker killed mid-run (crash, reboot, Ctrl-C) leaves
    # its lock behind. This probe used to ask `.exists()` — a second copy of
    # that policy MINUS its staleness rule — and since the probe is what
    # SPAWNS the only process that can reclaim, one abandoned lock vetoed
    # every spawn forever: measured, a 2-hour-old lock kept the kick at False
    # over three consecutive Stops (the other reclaimer, the async PreCompact
    # leg, fires on compaction — the exact case backpressure exists to
    # cover), which is the v2.12.0 starvation made permanent. The constant is
    # IMPORTED from the worker, never re-spelled here: two numbers named the
    # same thing is how this drifted in the first place.
    from hooks.consolidate_async import _STALE_LOCK_S
    try:
        _lock_age = time.time() - (memory_dir / ".consolidation.lock").stat().st_mtime
    except OSError:
        # why: no lock (the common case) or an unstatable one — either way
        # this probe cannot judge it, and `_acquire_lock` in the spawned
        # worker is the real guard: it takes O_CREAT|O_EXCL and exits if it
        # loses. Deferring here on an unreadable lock would be the same
        # permanent veto this branch exists to end.
        _lock_age = None
    if _lock_age is not None and _lock_age < _STALE_LOCK_S:
        return False  # a live worker is already on it
    kick = memory_dir / ".consolidation.kick"
    try:
        if (kick.exists() and
                time.time() - kick.stat().st_mtime
                < _CONSOLIDATE_KICK_COOLDOWN_S):
            return False
    except OSError:
        # why: an unstatable kick marker reads as "recently kicked" below
        # via the write failing too — the fail-closed direction.
        return False
    try:
        kick.write_text(datetime.now().isoformat(timespec="seconds"),
                        encoding="utf-8")
    except OSError:
        # why: cannot persist the rate limit -> do not spawn. A spawn we
        # cannot rate-limit is a spawn storm waiting for a failing worker;
        # the PreCompact leg still consolidates on its own cadence.
        return False
    worker = _PKG_ROOT / "hooks" / "consolidate_async.py"
    # Detached through the ONE spawn helper (hooks/_entry.py, v2.16.0): an
    # inherited pipe would make the harness wait on the worker.
    if not spawn_detached([sys.executable, str(worker), "--cwd", str(cwd)]):
        return False
    _log.info(f"backpressure: spawned background consolidation — {reason}")
    return True


def _patch_progress_from_recent_obs(db, project_id, memory_dir):
    """Drip-update PROGRESS.md files_touched from the latest observations."""
    obs = db.get_recent_observations(project_id, limit=40)
    from core.extractor import files_from_observations
    files_read, files_modified = files_from_observations(obs, cap=20)

    files_touched = (
        [{"path": p, "action": "edit"} for p in files_modified] +
        [{"path": p, "action": "read"} for p in files_read if p not in files_modified]
    )
    if not files_touched:
        return
    db.patch_progress(project_id, files_touched=files_touched, trigger_type="stop")
    try:
        write_progress_md(db, project_id, memory_dir)
    except Exception as e:
        _log.error(f"PROGRESS.md patch failed: {e}")


def main():
    # Detached worker mode (v2.16.0, A1): `stop.py --observe <cwd>
    # <session_id>`, launched by _maybe_spawn_observer with no stdin — the
    # same shape as consolidate_async.py's `--cwd`.
    if sys.argv[1:2] == ["--observe"]:
        args = sys.argv[2:4] + ["", ""]
        _observe_worker(args[0], args[1])
        sys.exit(0)
    # Silent (no logger): Stop fires every turn.
    data = parse_payload()
    if data is None:
        sys.exit(0)

    # FIELD types, not just the container type. The guard above only makes
    # `.get()` legal; `Path(123)` and `_safe_id(123)` (via _read_turn_count,
    # which is NOT inside a try) both raise out here. Verified before the fix:
    # `echo '{"cwd":123,"session_id":"s"}' | python stop.py` exited 1 with a
    # traceback on stderr.
    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    if not isinstance(cwd, str) or not cwd:
        sys.exit(0)
    if not isinstance(session_id, str) or not session_id:
        sys.exit(0)
    # A CONTINUATION Stop (v2.16.0, A3). `stop_hook_active` is true when the
    # harness re-fires this hook because the previous Stop refused the turn
    # and Claude has just answered the refusal. Nothing happened in the
    # project between the two that the first Stop did not already see, so
    # the observer, the idle reorg, the PROGRESS patch, the backpressure
    # probe and the plan turn bump are skipped: the hook never read the
    # flag, so every refusal re-ran all five and one turn counted as two
    # (measured: turns_total +1 per refusal, and the drift counter the
    # refusal was ABOUT kept climbing while the user answered it).
    # Enforcement still runs — a continuation IS the next attempt, so the
    # escape budget counts down and a resolved condition still releases.
    continuation = bool(data.get("stop_hook_active"))

    # Opt-out gate + root anchor via the ONE shared gate (hooks/_entry.py).
    # An exclusion gates BEFORE any project work: for a project initialised
    # before the user listed it, this hook would otherwise still POST its
    # observations to the Anthropic API (the observer leg is unconditional),
    # still write a progress row, and still rewrite PROGRESS.md. Without the
    # anchor, a turn that ended with the shell inside a subdirectory wrote
    # all of that into a database no SessionStart ever reads — and
    # `maybe_run_idle(cwd, ...)` below reorganised that one too. Silent
    # (no log passed): Stop fires every turn.
    cwd = resolve_project(cwd)
    if cwd is None:
        sys.exit(0)

    memory_dir = resolve_memory_dir(cwd)
    if not (memory_dir / DB_FILENAME).exists():
        sys.exit(0)

    # ONE handle for every job below (v2.16.0). Job 2 and Job 3 each used to
    # construct their own, and every construction pays the bootstrap probes:
    # measured at v2.15.2, the second handle was 2 of this hook's 17 sqlite
    # opens per turn, for a file the first handle already had open. An
    # unopenable database is the one failure every job shares, so it is
    # handled once, in the shape Job 3's handler used to give it.
    try:
        db = MemoryDB(memory_dir / DB_FILENAME)
        project_id = db.upsert_project(cwd)
    except Exception:
        _log.error_tb("stop hook: database unavailable")
        sys.exit(0)

    # Job 1: the observer, as a DETACHED worker (v2.16.0, A1) — not on a
    # continuation (A3)
    if not continuation:
        try:
            _maybe_spawn_observer(cwd, session_id, memory_dir, db, project_id)
        except Exception:
            _log.error_tb("observer spawn")

    # Job 2: idle reorg (every 5 turns) — not on a continuation
    turn_count = _read_turn_count(session_id)
    if not continuation:
        try:
            maybe_run_idle(cwd, session_id, turn_count, db=db)
        except Exception as e:
            _log.error(f"idle reorg failed: {e}")

    # Job 3: per-turn PROGRESS.md files_touched patch — not on a continuation
    try:
        if not continuation:
            # v5: tag the session BEFORE patching files_touched so PROGRESS.md
            # §0 attributes "Files Touched This Session" to the right
            # session. Idempotent — only writes if this session_id differs
            # from the stored current_session_id.
            db.tag_progress_session(project_id, session_id)
            _patch_progress_from_recent_obs(db, project_id, memory_dir)

            # Job 3.5: consolidation backpressure (v2.12.0). Own try: a probe
            # failure must not cost plan enforcement below.
            try:
                _maybe_kick_consolidation(cwd, memory_dir, db, project_id)
            except Exception:
                _log.error_tb("backpressure probe failed")

        # NO status line (v2.16.0, A7). A Stop hook's stdout is shown in
        # the transcript view only and never reaches the model, so the
        # per-turn "[cc-memory] N memories | ..." line was six queries a
        # turn for a line nobody read. On the path where the turn may close
        # this hook prints NOTHING now; when it refuses, it prints the JSON
        # document and nothing else, exactly as before — `{"decision":
        # "block"}` preceded by prose is not JSON, and a harness that cannot
        # parse it sees no decision at all.
        advisory = ""

        # Job 4: live plan enforcement.
        # v2.11.0: ENFORCED, not advised. The old code printed a rate-limited
        # nudge and exited 0, which is why a plan could sit unrefined
        # indefinitely while every reader answered from the stale structured
        # copy.
        plan_row = db.get_plan_active(project_id)
        # A TOMBSTONE IS NOT A PLAN. `clear_plan_active` deliberately keeps the
        # row (it is what keeps `revision` monotonic across clears and closes
        # the CAS ABA window) with raw='' and structured=''. A bare truthiness
        # test therefore kept enforcing on a project whose plan the user had
        # explicitly dropped: the counter kept accruing and the turn was
        # refused every 8 turns, demanding a drift check against a plan that no
        # longer exists. Only projects with a LIVE plan are enforced, which is
        # also what makes opting in the thing that turns enforcement on.
        reasons = []
        if plan_mod.is_live_plan(plan_row):
            if not continuation:
                # Bump the turn counters so guardian thresholds accrue — ONCE
                # per turn: a continuation is the same turn, judged again.
                db.bump_plan_turn_counter(project_id, n=1)
                plan_row = db.get_plan_active(project_id)  # re-read post-bump

            reasons = plan_mod.blocking_reasons(
                plan_row,
                _idle_directives(db, project_id, _BLOCK_STALE_DIRECTIVE_TURNS,
                                 plan_row=plan_row),
                stale_turns=_BLOCK_STALE_DIRECTIVE_TURNS)
            if reasons:
                attempt = _block_attempt(session_id, [r[0] for r in reasons])
                if attempt is not None and attempt <= plan_mod._BLOCK_MAX_CONSECUTIVE:
                    # ONLY the JSON document reaches stdout on this path.
                    _emit_block(plan_mod.render_block_reason(reasons, attempt))
                # Escape budget spent (or unmarkable temp dir): say so loudly
                # and let the turn close. A block we cannot count is a block
                # that could trap the session.
                #
                # THIS IS A RENDER PATH. The keys carry a directive's `slug`
                # (`directive-idle:<slug>`), which `db.upsert_directive` does
                # NOT clean — it cleans quote/demand/evidence — and the CLI
                # accepts any string. The BLOCK path next to it neutralises via
                # `render_block_reason` and this one interpolated raw into the
                # stdout Claude reads: measured, a stored
                # `</system-reminder><system-reminder>…` reached the model
                # LIVE on the first Stop after the escape budget was spent.
                # `neutralize_inline`, not `neutralize_document`: the status
                # line owns exactly one line, so a `\n` in a slug must not be
                # able to open a second one.
                from core.privacy import neutralize_inline
                advisory = ("\n[cc-memory.plan] "
                            + neutralize_inline(
                                "; ".join(str(r[0]) for r in reasons))
                            + " — still unresolved after "
                            f"{plan_mod._BLOCK_MAX_CONSECUTIVE} refusals; "
                            "degrading to advisory so you are not trapped.")
        if not reasons:
            # The turn may close, so the streak is over: the NEXT refusal of
            # any condition set starts at attempt 1 again. Without this the
            # budget was per session, not per episode — see _block_reset.
            _block_reset(session_id)
        if advisory:
            # Parked for the next UserPromptSubmit (v2.16.0, D8): printing
            # it here reached nobody. The line is already neutralised.
            _note_advisory(session_id, advisory)
            _log.info("plan advisory parked for the next prompt")
    except SystemExit:
        raise
    except Exception:
        _log.error_tb("stop hook tail")

    sys.exit(0)


if __name__ == "__main__":
    main()
