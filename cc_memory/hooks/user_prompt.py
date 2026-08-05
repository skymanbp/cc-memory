#!/usr/bin/env python3
"""
UserPromptSubmit hook — fires on every user message.

Three jobs:
  1. Auto-initialize memory/ + DB on first contact (zero-config UX).
  2. Track turn count per session (temp file used by Stop hook).
  3. Save user prompt text so the Stop observer has "what the user wants" context.

If this is the FIRST user message of a session AND PROGRESS.md exists,
also seed `progress.current_request` so PROGRESS.md captures the goal
right away (don't wait for PreCompact).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio (defensive — UserPromptSubmit's stdout is empty by
# contract, but error tracebacks could contain user prompt content).
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

_TURN_FILE_PREFIX = "cc_mem_turns_"
_PROMPT_FILE_PREFIX = "cc_mem_prompt_"


# ── Project opt-out (config.json `excluded_projects`) ──────────────────────
# DELIBERATE literal copy: hooks/pre_compact.py carries the same function, and
# those two hooks are the ONLY paths that create memory/ — so the check has to
# exist in both. They are kept import-independent of each other on purpose: a
# cross-hook import would make PreCompact die whenever UserPromptSubmit does,
# and hook safety outranks DRY here (see CLAUDE.md, "Hook safety > anything
# else"). Change one, change the other; there is no third copy.
def _is_excluded(cwd):
    """True when `cwd` is opted out of cc-memory via config.json.

    Matching is on the RESOLVED absolute path, so symlinks, ".." and relative
    entries all normalise to one form, and through ``os.path.normcase`` so
    Windows compares case- and separator-insensitively while POSIX stays
    case-sensitive.

    A listed directory also excludes everything BENEATH it. Claude Code's cwd
    is routinely a subdirectory of the project a user meant to exclude, and an
    exact-match-only test would happily create memory/ there — the control
    failing at the one job it has.
    """
    try:
        with open(_PKG_ROOT / "config.json", encoding="utf-8") as f:
            entries = json.load(f).get("excluded_projects") or []
        if not isinstance(entries, list):
            return False
        target = os.path.normcase(str(Path(cwd).expanduser().resolve()))
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                continue
            try:
                listed = os.path.normcase(str(Path(entry).expanduser().resolve()))
            except (OSError, ValueError):
                # why: one malformed entry must not disable the rest of the
                # opt-out list — skip it and keep checking the others
                continue
            if target == listed or target.startswith(listed + os.sep):
                return True
    except Exception:
        # why: an absent / malformed / unreadable config must never break the
        # hook. "not excluded" is the behaviour that shipped before this
        # control existed, so a broken config degrades to exactly that.
        return False
    return False


def _safe_id(session_id):
    return session_id[:16].replace("/", "_").replace("\\", "_")


def _init_project_if_needed(cwd):
    """Create memory/ + DB on first contact. Returns True if created."""
    db_path = Path(cwd) / "memory" / "memory.db"
    if db_path.exists():
        return False
    try:
        memory_dir = Path(cwd) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "sessions").mkdir(exist_ok=True)
        (memory_dir / "topics").mkdir(exist_ok=True)
        from core.db import MemoryDB
        db = MemoryDB(db_path)
        db.upsert_project(cwd)
        from core.progress import ensure_memory_gitignore
        ensure_memory_gitignore(memory_dir)
        from core.logger import get_logger
        get_logger("user_prompt").info(f"auto-initialized memory for {Path(cwd).name}")
        return True
    except Exception:
        # why: init failure shouldn't block the user's prompt from being
        # processed by Claude; we'll try again on the next message
        return False


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        sys.exit(0)

    # json.loads SUCCEEDS on well-formed non-object payloads (`null`, `42`,
    # `"s"`, `[1,2]`, `true`). The `.get()` calls below sit outside the try, so
    # without this guard those raise AttributeError, print a traceback to
    # stderr and exit 1 — two hook-contract violations at once.
    if not isinstance(data, dict):
        sys.exit(0)

    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    if not cwd or not session_id:
        sys.exit(0)

    # Project opt-out — MUST precede _init_project_if_needed, which is the call
    # that mkdir's memory/ in whatever cwd we were handed. Placed after the
    # empty-cwd guard so `Path("").resolve()` can never widen the match to the
    # interpreter's own working directory. Deliberately silent: this hook fires
    # on every user message, so logging here would write a line per turn.
    if _is_excluded(cwd):
        sys.exit(0)

    # Zero-config bootstrap. The return value is deliberately NOT consulted:
    # gating the turn-1 PROGRESS seeding on it is exactly what made that seeding
    # unreachable for a project's first session (see below).
    _init_project_if_needed(cwd)
    if not (Path(cwd) / "memory" / "memory.db").exists():
        sys.exit(0)

    safe = _safe_id(session_id)
    tmp = Path(tempfile.gettempdir())

    try:
        turn_file = tmp / f"{_TURN_FILE_PREFIX}{safe}"
        turn_count = 1
        if turn_file.exists():
            try:
                turn_count = int(turn_file.read_text(encoding="utf-8").strip()) + 1
            except (ValueError, OSError):
                # why: corrupted turn file — reset to 1; observer will still
                # work, just doesn't know how many turns we've had
                turn_count = 1
        try:
            turn_file.write_text(str(turn_count), encoding="utf-8")
        except OSError:
            # why: can't persist turn count; observer falls back to recent-20
            pass

        prompt = data.get("prompt", "")
        if prompt and isinstance(prompt, str):
            if prompt.startswith("/"):
                prompt = prompt[1:]
            prompt = prompt[:500]
            prompt_file = tmp / f"{_PROMPT_FILE_PREFIX}{safe}"
            try:
                prompt_file.write_text(prompt, encoding="utf-8")
            except OSError:
                # why: prompt context for observer is enrichment, not required
                pass

            # First turn of a session: also seed PROGRESS.md current_request.
            # `and not created` used to guard this and made the branch
            # UNREACHABLE for a project's very first session: on turn 1 of a new
            # project _init_project_if_needed had just created the DB so
            # `created` was True, and on turn 2+ `turn_count != 1`. A brand-new
            # project therefore got no progress row and no PROGRESS.md until its
            # first compaction. db.patch_progress bootstraps the row itself
            # (core/db.py: `if not self.get_progress(...): self.upsert_progress(...)`),
            # so running on a just-created DB is safe.
            if turn_count == 1:
                try:
                    from core.db import MemoryDB
                    from core.progress import write_progress_md
                    db = MemoryDB(Path(cwd) / "memory" / "memory.db")
                    pid = db.upsert_project(cwd)
                    # v5: tag this session BEFORE patching other fields so
                    # PROGRESS.md §0 reflects the new owner. Idempotent — if
                    # stop / pre_compact already tagged the same session this
                    # turn, no-op.
                    db.tag_progress_session(pid, session_id)
                    # Detect resume signal: exact-match whitelist (trim+lower).
                    # Contracted by the SessionStart forced reminder's RESUME
                    # PROTOCOL — when the user says one of these tokens, the
                    # next Claude is required to auto-execute open_todos[0].
                    # Tagging trigger_type here makes the intent auditable.
                    normalized = prompt.strip().lower()
                    # i18n Tier 3: bilingual resume tokens are intentional — do NOT
                    # reduce to English-only (see docs/ARCHITECTURE.md#9-documentation-language-convention-i18n §1). Keep in sync with
                    # session_start.py RESUME PROTOCOL.
                    resume_signals = {
                        "", "继续", "接着", "接着做", "接着干", "继续干",
                        "resume", "continue", "go on", "keep going",
                    }
                    trigger = "resume_request" if normalized in resume_signals else "user_prompt"
                    db.patch_progress(pid, current_request=prompt, trigger_type=trigger)
                    write_progress_md(db, pid, Path(cwd) / "memory")
                except Exception:
                    # why: PROGRESS seeding is best-effort; PreCompact will
                    # overwrite it with a full state anyway
                    pass

    except Exception:
        try:
            from core.logger import get_logger
            get_logger("user_prompt").error_tb("UserPromptSubmit hook error")
        except Exception:
            # why: logger failing in a hook — silent fallback per hook contract
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
