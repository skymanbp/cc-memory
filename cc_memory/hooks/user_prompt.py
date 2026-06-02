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
        gi = memory_dir / ".gitignore"
        if not gi.exists():
            gi.write_text(
                "memory.db\nmemory.db-wal\nmemory.db-shm\n"
                "sessions/\n.last_save.json\n",
                encoding="utf-8"
            )
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

    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    if not cwd or not session_id:
        sys.exit(0)

    created = _init_project_if_needed(cwd)
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

            # First turn of a session: also seed PROGRESS.md current_request
            if turn_count == 1 and not created:
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
