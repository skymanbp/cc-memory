#!/usr/bin/env python3
"""
PostToolUse hook — fires after every tool call.

Two jobs:
  1. LIVE PLAN (v2.2) — capture ExitPlanMode output, sync TodoWrite snapshots
     into the active plan's step statuses, and accrue the guardian drift
     counters. MODE-INDEPENDENT (see the comment in main()).
  2. OBSERVATION ROW — capture the tool event as a lightweight row for the
     Stop-hook observer to later sift through. No LLM call. This is the ONLY
     part the project's mode gates.

Output: stdout EMPTY, stderr suppressed, always exits 0.

Latency: NOT <50 ms. Measured on Windows 11 / CPython 3.13, median of 25
subprocess invocations against a warm 40 KiB DB with an 8-step active plan:
**~180-290 ms end-to-end** depending on machine load, of which ~75-120 ms is
bare CPython start-up — so this hook's own body (imports + sqlite) is
~100-180 ms. It fires after EVERY tool call, so keep it free of network I/O
and of any unbounded scan. The previous "<50 ms" claim was never measured and
understated reality by ~4x.
"""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio. PostToolUse has empty stdout by contract, but error
# paths still print tracebacks; gbk would crash the hook on emoji glyphs.
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

# Module level, unlike `should_observe` below (imported lazily inside main's
# try): the opt-out has to be evaluated BEFORE any project work, so it must not
# depend on reaching a lazy import site. Shared entry ladder (v2.10.0): stdin
# parsing + the opt-out→anchor gate live ONCE in hooks/_entry.py. This hook
# never creates a memory dir, but it does write observation rows and bump plan
# counters, so resolving to the same root as UserPromptSubmit is what keeps a
# subdirectory turn from being recorded against a different project row.
from hooks._entry import parse_payload, resolve_project
from core.logger import get_logger

# Construction is I/O-free (the file opens on first WRITE, core/logger.py
# Logger._get_file), so a module-level logger costs this per-tool-call hook
# nothing on the happy path.
_log = get_logger("post_tool_use")

_MAX_INPUT_CHARS = 2000
_MAX_OUTPUT_CHARS = 1000


def _truncate_input(tool_name, tool_input):
    if not isinstance(tool_input, dict):
        return str(tool_input)[:_MAX_INPUT_CHARS]
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return tool_input.get("file_path", tool_input.get("notebook_path", ""))
    if tool_name == "Read":
        return tool_input.get("file_path", "")
    if tool_name == "Bash":
        return tool_input.get("command", "")[:500]
    if tool_name in ("Grep", "Glob"):
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern} in {path}" if path else pattern
    if tool_name == "Agent":
        return tool_input.get("prompt", "")[:300]
    return json.dumps(tool_input, ensure_ascii=False)[:_MAX_INPUT_CHARS]


def _truncate_output(tool_name, output):
    """Shrink a tool result for STORAGE. Lossy by design — never classify on it.

    `Read` collapses to a fixed placeholder and every other body is cut at
    _MAX_OUTPUT_CHARS, so a `<private>` marker inside a Read body — or past the
    cut in any other body — is already gone by the time this returns. Privacy
    classification therefore runs on the RAW payload in main(), before this.
    """
    if output is None:
        return ""
    s = str(output) if not isinstance(output, str) else output
    if not s:
        return ""
    if tool_name == "Read":
        return "(file content)"
    return s[:_MAX_OUTPUT_CHARS]


def _apply_plan_integration(db, project_id, cwd, tool_name, tool_input):
    """v2.2 live plan anchor. Best-effort: the caller logs and swallows.

    Deliberately independent of `core.modes.should_observe` — see main().
    """
    from core import plan as plan_mod
    memory_dir = Path(cwd) / "memory"

    if tool_name == "ExitPlanMode":
        raw_plan = tool_input.get("plan", "") if isinstance(tool_input, dict) else ""
        if raw_plan:
            plan_mod.capture_exit_plan_mode(
                db, project_id, raw_plan, memory_dir=memory_dir
            )

    elif tool_name == "TodoWrite":
        todos = tool_input.get("todos", []) if isinstance(tool_input, dict) else []
        if todos and db.get_plan_active(project_id):
            plan_mod.apply_todowrite_sync(
                db, project_id, todos, memory_dir=memory_dir
            )

    elif tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        if db.get_plan_active(project_id):
            db.bump_plan_edit_counter(project_id, n=1)

    # Sensitive tool calls (git push, rm -rf, drop table, deploys, ...)
    # bump the counter by a lot — semantically "this single act carries
    # the same drift risk as ~20 ordinary edits", so the next Stop hook
    # will surface a guardian-recommendation line immediately.
    if plan_mod.is_sensitive_tool_call(tool_name, tool_input):
        if db.get_plan_active(project_id):
            db.bump_plan_edit_counter(project_id, n=20)


def main():
    # replace_errors: this hook's payloads carry tool output, which tolerates
    # U+FFFD — the read-to-EOF / no-prefix-cap rationale lives in _entry.
    # Logged: a dropped event loses not just the observation row but the
    # mode-independent live-plan block, which is worth one log line.
    data = parse_payload(log=_log, replace_errors=True)
    if data is None:
        sys.exit(0)

    # FIELD types, not just the container type. The guard above only makes
    # `.get()` legal; `Path(123)` still raises TypeError out here, outside any
    # try — rc=1 plus a traceback on stderr, the exact pair of contract
    # violations that guard was added to close. Verified before the fix:
    # `echo '{"cwd":123}' | python post_tool_use.py` exited 1.
    cwd = data.get("cwd", "")
    if not isinstance(cwd, str) or not cwd:
        sys.exit(0)

    # Opt-out gate + root anchor via the ONE shared gate (hooks/_entry.py),
    # ahead of the DB probe. Gating on memory/memory.db existing is not an
    # opt-out: a project initialised BEFORE the user listed it would otherwise
    # keep storing every tool input and output. No log passed — this hook
    # fires after every tool call, so a redirection line would be a line per
    # call; the rare hooks pass a logger and carry the reporting duty.
    cwd = resolve_project(cwd)
    if cwd is None:
        sys.exit(0)

    db_path = Path(cwd) / "memory" / "memory.db"
    if not db_path.exists():
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name:
        sys.exit(0)

    # session_id is bound into a parameterized INSERT below; a list/dict would
    # raise sqlite3.InterfaceError and lose the row. Normalise instead.
    session_id = data.get("session_id", "")
    if not isinstance(session_id, str):
        session_id = ""

    tool_input = data.get("tool_input", {})

    try:
        from core.modes import should_observe
        from core.privacy import clean_for_storage, has_private
        from core.db import MemoryDB

        db = MemoryDB(db_path)
        project_id = db.upsert_project(cwd)
        mode = db.get_project_mode(project_id)

        # ── v2.2 live plan integration — runs in EVERY mode ───────────────
        # This block used to sit BELOW an early `sys.exit(0)` on
        # `not should_observe(mode, tool_name)`, which made the entire v2.2
        # live-plan anchor dead through its own hook: `TodoWrite` is in every
        # mode's skip_tools and `ExitPlanMode` is in no mode's non-empty
        # observe_tools allow-list, so should_observe() was False for both
        # plan-control tools in all three modes — plan_active was never
        # written, memory/.plan_raw.md and memory/PLAN.md never appeared, and
        # the guardian drift counters silently varied by mode (research
        # counted no Edits; writing counted no sensitive Bash).
        # Plan control is NOT observation. Mode selects what is worth
        # *remembering*; it must never decide whether the plan anchor tracks
        # reality. Keep this block above the gate.
        try:
            _apply_plan_integration(db, project_id, cwd, tool_name, tool_input)
        except Exception:
            try:
                from core.logger import get_logger
                get_logger("post_tool_use").error_tb("plan integration error")
            except Exception:
                # why: logger itself failing means observability is already
                # lost; continue so the observation row below still gets written
                pass

        # ── observation row — the ONLY mode-gated part of this hook ───────
        if should_observe(mode, tool_name):
            # `tool_response` IS the key Claude Code sends on PostToolUse
            # stdin — verified, not assumed: 749 of 749 observation rows in
            # this repo's live memory.db carry a real body read from it.
            # `toolUseResult` is the spelling the SAME payload uses inside the
            # transcript .jsonl records, so it is accepted as a fallback for a
            # reader fed a transcript record. Neither alias may be dropped.
            tool_response = data.get("tool_response")
            if tool_response is None:
                tool_response = data.get("toolUseResult", "")

            # Classify BEFORE truncating. Both _truncate_* helpers are lossy —
            # a Read body becomes "(file content)", Bash input is cut at 500
            # chars, any other body at _MAX_OUTPUT_CHARS — so classifying on
            # their output made `<private>` unobservable exactly where it
            # mattered most: a Read of a file the user marked private stored as
            # is_private=0. is_private is what keeps a row out of
            # get_recent_observations / get_observations_since, i.e. out of the
            # Stop-hook observer and the PreCompact extraction prompt, so a
            # false 0 ships that file's path to the Anthropic API and into
            # progress.files_touched. `has_private` is a single substring
            # find, linear in the payload — microseconds even on the multi-MB
            # payloads the uncapped stdin read now admits.
            raw_in = tool_input if isinstance(tool_input, str) else str(tool_input)
            raw_out = (tool_response if isinstance(tool_response, str)
                       else str(tool_response))
            is_private = 1 if (has_private(raw_in) or has_private(raw_out)) else 0

            input_str = clean_for_storage(_truncate_input(tool_name, tool_input))
            output_str = clean_for_storage(_truncate_output(tool_name, tool_response))

            db.insert_observation(
                project_id=project_id,
                session_id=session_id,
                tool_name=tool_name,
                tool_input=input_str,
                tool_output=output_str,
                is_private=is_private,
            )

    except Exception:
        try:
            from core.logger import get_logger
            get_logger("post_tool_use").error_tb("PostToolUse hook error")
        except Exception:
            # why: logger itself failing in a hook means we already lost
            # observability; there's nothing left to do but exit cleanly
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
