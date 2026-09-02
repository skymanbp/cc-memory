#!/usr/bin/env python3
"""
UserPromptSubmit hook — fires on every user message.

Three jobs:
  1. Auto-initialize memory/ + DB on first contact (zero-config UX).
  2. Track turn count per session (temp file used by Stop hook).
  3. Save user prompt text so the Stop observer has "what the user wants" context.

On the session's FIRST NON-SCAFFOLDING user message it also seeds
`progress.current_request`, so PROGRESS.md captures the goal right away
(don't wait for PreCompact). Scaffolding -- a slash command such as
`/ccm-load` or `/compact` -- is skipped by `strip_scaffolding` below, the
predicate `pre_compact._first_user_request` shares: seeding it wrote a
PROGRESS.md whose "Current Request" was the harness's own words.
"""
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio (defensive — UserPromptSubmit's stdout is empty by
# contract, but error tracebacks could contain user prompt content).
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

# Shared entry ladder (v2.10.0): stdin parsing and the opt-out→anchor gate
# live in hooks/_entry.py, ONCE. Six hand-rolled copies of this ladder is how
# guard drift between hooks kept becoming shipped defects (v2.7.0's whole
# release theme; the v2.9.0 junk-cwd database plant). This hook is the one
# that mkdir's .ccm/, so it is the one a wrong `cwd` turns into a second
# database — see hooks/_entry.py for the ordering contract.
from hooks._entry import parse_payload, resolve_project
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
# The state directory, resolved rather than spelled (v2.13.0). `memory_dir`
# migrates a pre-v2.13.0 `memory/` to `.ccm/` on the way; `find_db_path` is
# the read-only twin, for the probes that must not write. `db_path` is
# deliberately NOT imported: this module has a local of that name.
from core.layout import DB_FILENAME, find_db_path, memory_dir

_TURN_FILE_PREFIX = "cc_mem_turns_"
_PROMPT_FILE_PREFIX = "cc_mem_prompt_"
# Written once, when the seed below lands. It answers "has this SESSION
# already seeded progress.current_request?" — a question the prompt marker
# cannot answer, because that marker is deliberately overwritten with "" on a
# scaffolding or entirely-private turn (the observer must never be handed the
# PREVIOUS turn's request), so "empty" there means "the last turn stored
# nothing", not "nothing has been stored this session". Reading it that way
# re-seeded §1 after every such turn: measured on this branch, real request
# → `/cc-mem status` → "ok continue" left current_request="ok continue", and
# real → `<private>…</private>` → "继续" left ("继续", "resume_request") — a
# RESUME PROTOCOL signal mid-session, which SessionStart reads as the
# session's opening message. Registered in ui/installer.py's uninstall sweep;
# an unregistered prefix leaks forever.
_SEEDED_FILE_PREFIX = "cc_mem_seeded_"

# A slash command: "/" + a command token, then whitespace or end of line.
# The token deliberately excludes "/" so a request that OPENS with a path
# ("/usr/bin/env is missing") is a request, not scaffolding — the blanket
# `startswith("/")` this replaces mangled that one into "usr/bin/env is
# missing" before storing it. Plugin commands are `plugin:command`, hence the
# colon.
_SLASH_COMMAND_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_:.-]*(?:\s|$)")


def strip_scaffolding(text: str) -> str:
    """The user's own words in `text`, or "" when it is ALL harness scaffolding.

    ONE predicate for BOTH `progress.current_request` ingresses — this hook
    (live, every turn) and `pre_compact._first_user_request` (reconstructed
    from the transcript). They disagreed: pre_compact skipped Claude Code's
    slash-command scaffolding on purpose, while this hook merely stripped the
    leading "/" and stored the rest, so the documented activation `/ccm-load`
    as the first message wrote PROGRESS.md §1 = "ccm-load" — and
    `session_start._refresh_progress_row` is fill-only-empty by contract, so a
    wrong non-empty value stood until the first compaction. The same text also
    reached the Stop observer's Anthropic request as "User request:".
    Two ingresses to one field with two policies is the disease
    `strip_harness_blocks` was unified to end; this is the second half of it.

    `strip_harness_blocks` covers the WRAPPED form a transcript records
    (`<command-name>…`); the regex covers the BARE form the harness hands
    UserPromptSubmit (`/ccm-load`, `/cc-mem status`, `/compact`). Returning
    the text rather than a bool is what lets both callers share it: one stores
    the result, the other keeps scanning when it is empty.
    """
    body = strip_harness_blocks(text or "")
    if _SLASH_COMMAND_RE.match(body.lstrip()):
        return ""
    return body


def _init_project_if_needed(cwd):
    """Create .ccm/ + DB on first contact. Returns True if created.

    `state_dir` is asked for ONCE and reused: it is the migrating resolver
    (`core/layout.memory_dir`), so calling it again for the database path
    could answer differently if the rename landed in between — and every
    artifact of one turn must agree on one directory.
    """
    state_dir = memory_dir(cwd)
    db_path = state_dir / DB_FILENAME
    if db_path.exists():
        return False
    try:
        from core.progress import ensure_memory_dir
        # Raises FileNotFoundError when `cwd` no longer exists, which the
        # handler below turns into "skip this turn" — the project stays gone
        # instead of being reborn as an empty shell on the next message.
        # Called for its side effect (the directory, sessions/, topics/ and
        # the .gitignore); its return value was already bound to an unused
        # local before v2.13.0, and the name it used now belongs to the
        # imported resolver.
        ensure_memory_dir(state_dir)
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
    # Silent (no logger): this hook fires on every user message.
    data = parse_payload()
    if data is None:
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

    # Opt-out gate + root anchor via the ONE shared gate (hooks/_entry.py),
    # which owns the ordering contract. MUST precede _init_project_if_needed,
    # the call that mkdir's the state directory in whatever cwd we were handed; placed
    # after the empty-cwd guard so `Path("").resolve()` can never widen the
    # match to the interpreter's own working directory. Deliberately silent
    # (no log passed): this hook fires on every user message.
    cwd = resolve_project(cwd)
    if cwd is None:
        sys.exit(0)

    # Zero-config bootstrap. The return value is deliberately NOT consulted:
    # gating the turn-1 PROGRESS seeding on it is exactly what made that seeding
    # unreachable for a project's first session (see below).
    _init_project_if_needed(cwd)
    # find_db_path, not db_path: init just ran and either created the state
    # directory or could not. This is the "did it work" check, and a probe
    # that migrates would be answering its own question.
    if not find_db_path(cwd).exists():
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
        if not isinstance(prompt, str):
            prompt = ""
        # PRIVACY GATE (v2.5.2). BOTH consumers of this variable ship the
        # text somewhere it cannot be taken back, and neither used to clean
        # it — while the observation and memory paths always did:
        #   * the temp prompt file below, which hooks/stop.py reads and
        #     splices VERBATIM into the Anthropic observer request as
        #     "User request: …" (stop.py `_observer_evaluate`);
        #   * progress.current_request, rendered into .ccm/PROGRESS.md —
        #     a file core.progress.MEMORY_GITIGNORE_LINES deliberately does
        #     NOT ignore, so it is committed to the user's repository.
        # Cleaned BEFORE the 500-char cut so a span straddling the cut is
        # still seen as a matched pair; clean_for_storage fails CLOSED on a
        # dangling open tag, so a cut landing mid-span drops the remainder
        # instead of emitting it.
        # strip_scaffolding first, THE shared primitive and the same reason
        # as pre_compact._first_user_request (which now calls it too):
        # whatever ends up here is stored as `progress.current_request` and
        # spliced into the Stop observer's Anthropic request, and neither
        # should ever be Claude Code's own slash-command scaffolding.
        prompt = clean_for_storage(strip_scaffolding(prompt))[:500]
        prompt_file = marker_path(_PROMPT_FILE_PREFIX, safe)
        try:
            # Written even when cleaning emptied it — AND when the raw prompt
            # was already empty: this marker is per-SESSION and reused every
            # turn, so skipping the write leaves the PREVIOUS turn's prompt in
            # place for stop.py to splice into the observer's Anthropic
            # request. The old `if prompt` guard around this whole block did
            # exactly that for a raw "" prompt (measured: turn 2's empty
            # prompt left turn 1's text in the marker), so the write sits
            # ABOVE any truthiness test now.
            write_marker(prompt_file, prompt)
        except OSError:
            # why: prompt context for observer is enrichment, not required
            pass

        if prompt:
            # The session's FIRST NON-SCAFFOLDING prompt seeds PROGRESS.md
            # current_request. It used to be `turn_count == 1`, which is the
            # same thing only when turn 1 is a real request: a session opened
            # with `/ccm-load` (this plugin's own documented activation) spent
            # the one seeding turn on the slash command, and every later turn
            # failed `turn_count != 1`, so the real request was never seeded at
            # all. The condition is therefore "this session has not seeded
            # yet", recorded by its OWN marker when the seed lands — not
            # inferred from what the previous turn stored, which is "" after
            # every scaffolding or private turn and so re-seeded §1 in the
            # MIDDLE of a session (see _SEEDED_FILE_PREFIX for the
            # measurement). At most once per session, exactly as before.
            #
            # `and not created` used to guard this too and made the branch
            # UNREACHABLE for a project's very first session: on turn 1 of a new
            # project _init_project_if_needed had just created the DB so
            # `created` was True, and on turn 2+ `turn_count != 1`. A brand-new
            # project therefore got no progress row and no PROGRESS.md until its
            # first compaction. db.patch_progress bootstraps the row itself
            # (one INSERT OR IGNORE + UPDATE transaction, core/db.py), so
            # running on a just-created DB is safe.
            #
            # `if prompt` is the privacy gate's second half: a prompt that was
            # ENTIRELY private redacts to "" and must not be stored — and must
            # not fall through to the resume-signal whitelist below, which
            # contains "" and would mislabel it a resume_request. Whitespace-only
            # prompts are unaffected (clean_for_storage returns text with no
            # open tag byte-identical, so "   " stays truthy and still resolves
            # to the "" resume signal exactly as before).
            seeded_file = marker_path(_SEEDED_FILE_PREFIX, safe)
            if not read_marker(seeded_file, "").strip():
                try:
                    from core.db import MemoryDB
                    from core.progress import write_progress_md
                    state_dir = memory_dir(cwd)
                    db = MemoryDB(state_dir / DB_FILENAME)
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
                    write_progress_md(db, pid, state_dir)
                    # AFTER the write, so a seed that failed halfway is
                    # retried on the next real prompt rather than skipped.
                    # A marker that cannot be written degrades to the OLD
                    # failure direction (re-seed every real turn), which is
                    # exactly what an unwritable turn counter already did:
                    # write_marker never raises, it returns False.
                    write_marker(seeded_file, "1")
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
