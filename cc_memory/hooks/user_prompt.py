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
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio (defensive — UserPromptSubmit's stdout is empty by
# contract, but error tracebacks could contain user prompt content).
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

# Project opt-out. This USED to be a private literal copy here and a second
# byte-identical one in hooks/pre_compact.py, on the grounds that those two are
# "the ONLY paths that create memory/". The other four hooks
# <!--ce:hooks:asof--> gated on memory/memory.db merely existing, so a project
# initialised BEFORE being listed stayed fully captured; the single
# implementation now lives in core.modes and
# every hook calls it. See core/modes.py:is_excluded.
from core.modes import is_excluded

# Project-root anchoring (v2.6.0). THIS hook is the one that mkdir's memory/,
# so it is the one a wrong `cwd` turns into a second database. The payload's
# cwd follows the agent's own `cd`, which is how a session launched at a repo
# root came to report a subdirectory. See core/roots.py for the ladder.
from core.roots import project_root
# safe_id replaces this hook's private `[:16]` truncating copy (three hooks
# <!--ce:hooks:asof--> each had one; truncation cross-wired any two sessions
# sharing a 16-char prefix). read_marker never raises and refuses to follow a
# planted symlink.
from core.markers import marker_path, read_marker, safe_id as _safe_id, write_marker

# Privacy opt-out. `<private>…</private>` was honoured on the observation path
# (hooks/post_tool_use.py) and on every memory-write path (llm/memory_writer.py,
# core/extractor.py) but NOT on the progress path this hook feeds — the same tag,
# in the same session, behaving in opposite ways. See the call site in main().
from core.privacy import clean_for_storage, strip_harness_blocks

_TURN_FILE_PREFIX = "cc_mem_turns_"
_PROMPT_FILE_PREFIX = "cc_mem_prompt_"


def _init_project_if_needed(cwd):
    """Create memory/ + DB on first contact. Returns True if created."""
    db_path = Path(cwd) / "memory" / "memory.db"
    if db_path.exists():
        return False
    try:
        from core.progress import ensure_memory_dir
        # Raises FileNotFoundError when `cwd` no longer exists, which the
        # handler below turns into "skip this turn" — the project stays gone
        # instead of being reborn as an empty shell on the next message.
        memory_dir = ensure_memory_dir(Path(cwd) / "memory")
        from core.db import MemoryDB
        db = MemoryDB(db_path)
        db.upsert_project(cwd)
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

    # FIELD types, not just the container type — the guard the other five
    # hooks already carried and this one did not. `Path(123)` and
    # `_safe_id(123)` both raise out here, outside any try: rc=1 plus a
    # traceback on stderr, the exact pair of contract violations the
    # isinstance check on `data` was added to close.
    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    if not isinstance(cwd, str) or not cwd:
        sys.exit(0)
    if not isinstance(session_id, str) or not session_id:
        sys.exit(0)

    # Project opt-out — MUST precede _init_project_if_needed, which is the call
    # that mkdir's memory/ in whatever cwd we were handed. Placed after the
    # empty-cwd guard so `Path("").resolve()` can never widen the match to the
    # interpreter's own working directory. Deliberately silent: this hook fires
    # on every user message, so logging here would write a line per turn.
    if is_excluded(cwd):
        sys.exit(0)

    # Anchor AFTER the opt-out, never before: `is_excluded` must keep seeing
    # the raw cwd. A user who excluded one sensitive SUBDIRECTORY would
    # otherwise have that exclusion widened away by resolving to the parent
    # project first. Rebinding `cwd` here (rather than fixing each use site)
    # is deliberate — memory_dir, db_path and upsert_project must agree on
    # ONE directory, and a per-site fix is how they would drift apart again.
    cwd = str(project_root(cwd))

    # Zero-config bootstrap. The return value is deliberately NOT consulted:
    # gating the turn-1 PROGRESS seeding on it is exactly what made that seeding
    # unreachable for a project's first session (see below).
    _init_project_if_needed(cwd)
    if not (Path(cwd) / "memory" / "memory.db").exists():
        sys.exit(0)

    safe = _safe_id(session_id)

    try:
        turn_file = marker_path(_TURN_FILE_PREFIX, safe)
        turn_count = 1
        raw_count = read_marker(turn_file, "").strip()
        if raw_count:
            try:
                turn_count = int(raw_count) + 1
            except ValueError:
                # why: corrupted turn file — reset to 1; observer will still
                # work, just doesn't know how many turns we've had
                turn_count = 1
        try:
            write_marker(turn_file, str(turn_count))
        except OSError:
            # why: can't persist turn count; observer falls back to recent-20
            pass

        prompt = data.get("prompt", "")
        if prompt and isinstance(prompt, str):
            if prompt.startswith("/"):
                prompt = prompt[1:]
            # PRIVACY GATE (v2.5.2). BOTH consumers of this variable ship the
            # text somewhere it cannot be taken back, and neither used to clean
            # it — while the observation and memory paths always did:
            #   * the temp prompt file below, which hooks/stop.py reads and
            #     splices VERBATIM into the Anthropic observer request as
            #     "User request: …" (stop.py `_observer_evaluate`);
            #   * progress.current_request, rendered into memory/PROGRESS.md —
            #     a file core.progress.MEMORY_GITIGNORE_LINES deliberately does
            #     NOT ignore, so it is committed to the user's repository.
            # Cleaned BEFORE the 500-char cut so a span straddling the cut is
            # still seen as a matched pair; clean_for_storage fails CLOSED on a
            # dangling open tag, so a cut landing mid-span drops the remainder
            # instead of emitting it.
            # strip_harness_blocks first, same primitive and same reason as
            # pre_compact._first_user_request: whatever ends up here is
            # stored as `progress.current_request` and spliced into the
            # Stop observer's Anthropic request, and neither should ever
            # be Claude Code's own slash-command scaffolding.
            prompt = clean_for_storage(strip_harness_blocks(prompt))[:500]
            prompt_file = marker_path(_PROMPT_FILE_PREFIX, safe)
            try:
                # Written even when cleaning emptied it: this marker is
                # per-SESSION and reused every turn, so skipping the write would
                # leave the PREVIOUS turn's prompt in place for stop.py to read.
                write_marker(prompt_file, prompt)
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
            #
            # `and prompt` is the privacy gate's second half: a prompt that was
            # ENTIRELY private redacts to "" and must not be stored — and must
            # not fall through to the resume-signal whitelist below, which
            # contains "" and would mislabel it a resume_request. Whitespace-only
            # prompts are unaffected (clean_for_storage returns text with no
            # open tag byte-identical, so "   " stays truthy and still resolves
            # to the "" resume signal exactly as before).
            if turn_count == 1 and prompt:
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
