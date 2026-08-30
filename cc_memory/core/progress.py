"""
PROGRESS.md generator — single source of truth for session handoff.

Replaces v2.0's SESSION_HANDOFF.md (which got polluted by patch-style writes).

Contract:
  - PROGRESS.md is ALWAYS regenerated from the `progress` SQL table.
  - NEVER append, NEVER patch the file in place.
  - Updates happen in two places:
      * PreCompact hook: full rewrite from all signals (todos, summary, files).
      * Stop hook (per-turn): patch_progress() for files_touched / open_todos.

Schema (see core.db, table `progress`):
  current_request   the user's primary task (first prompt of session)
  status_done       what's completed
  status_in_flight  what's currently being worked
  status_blocked    what's blocked, and on what
  open_todos        JSON list of {content, priority, status}
  plan              sequenced next steps as free text
  critical_context  JSON list of memory IDs (top-importance, must-read)
  files_touched     JSON list of {path, action: "read|edit|write"}
  transcript_ptr    absolute path to JSONL of the session being compacted
  trigger_type      what caused the last write (precompact, stop, manual)

The forced-handoff system-reminder injected at SessionStart points to this
file. See docs/CONTRACTS.md#handoff-contract for the full handoff spec.
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Package root on sys.path. Hoisted here from below the constants in v2.13.0:
# MEMORY_GITIGNORE_LINES now opens with a name imported from `core.layout`, and
# a module-level constant is built before the import block that used to sit
# further down. One insert, one place — the guard below it was already
# idempotent, so the later `from core.X import ...` lines are unaffected.
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# The state directory's identifying marker line, imported rather than retyped
# — see MEMORY_GITIGNORE_LINES below. `core/layout.py` is pure stdlib plus
# markers.py at import time, so this costs a name binding, not a dependency.
from core.layout import CCM_GITIGNORE_MARKER


# ── .ccm/.gitignore — canonical ignore set ─────────────────────────────────
# Runtime artifacts the plugin writes into a project's state directory. They
# are machine state, not content: several embed verbatim conversation or plan
# prose, so leaking them into a user's repo is a privacy problem, not just
# noise. Keep in sync with the two standalone copies that cannot import this
# module: cc_memory/ui/installer.py (stdlib-only bootstrap) and
# skills/ccm-load/SKILL.md (inline script).
#
# Every line names an entry INSIDE the directory and none of them names the
# directory itself, which is why the v2.13.0 rename of `memory/` to `.ccm/`
# needed no change here and why an already-migrated install keeps its file.
# The first line is `core/layout.CCM_GITIGNORE_MARKER`, imported rather than
# retyped: `core/layout.is_ccm_dir` identifies a legacy directory BY that
# line, so a drift between the writer and the reader would make the migration
# stop recognising the directories this very list created.
MEMORY_GITIGNORE_LINES = [
    CCM_GITIGNORE_MARKER,
    "memory.db",
    "memory.db-wal",
    "memory.db-shm",
    "sessions/",
    ".last_save.json",
    ".last_inject.json",
    ".last_consolidation.json",
    ".consolidation.lock",
    ".consolidation.kick",
    ".pre_compact_attempt.json",
    ".plan_raw.md",
    ".plan_history/",
    "*.tmp",
]


def ensure_memory_gitignore(memory_dir: Path) -> None:
    """Create memory/.gitignore, or ADD any lines a older install is missing.

    Every previous generator was guarded by ``if not gi.exists()``, so each time
    the plugin started writing a new artifact, every existing install kept the
    stale ignore list forever and silently began leaking it. Appending only the
    missing lines migrates those installs while preserving anything the user
    added themselves.
    """
    gi = memory_dir / ".gitignore"
    try:
        # errors="replace", not strict: a user who appended a line from a GBK
        # editor makes strict UTF-8 raise UnicodeDecodeError — a ValueError,
        # NOT an OSError, so the handler below never caught it. One caller
        # (`hooks/pre_compact.py`) invokes this ABOVE the archive, the session
        # row, the memories and PROGRESS.md, so a mis-encoded byte in a
        # courtesy file silently cost the entire compaction. Same rationale as
        # `core/atomic.py`: replace the undecodable byte, keep the content.
        existing = gi.read_text(encoding="utf-8", errors="replace") \
            if gi.exists() else ""
        # rstrip, NOT strip: leading whitespace is SIGNIFICANT to git — a
        # line reading ` memory.db` ignores nothing, yet `.strip()` made it
        # count as the `memory.db` rule being present, so the migration
        # declined to add the real one and the database stayed trackable
        # (register D3). Trailing whitespace is insignificant to git unless
        # escaped, so rstrip keeps matching the shapes that DO work.
        have = {ln.rstrip() for ln in existing.splitlines()}
        missing = [ln for ln in MEMORY_GITIGNORE_LINES if ln not in have]
        if not missing:
            return
        prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
        gi.write_text(prefix + "\n".join(missing) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        # why: .gitignore is a courtesy to the user's VCS; a read-only or
        # missing memory dir must never break the hook that called us.
        # ValueError covers the decode family that `errors="replace"` above
        # does not reach (an undecodable filename, a surrogate in the join).
        pass


def ensure_memory_dir(memory_dir: Path) -> Path:
    """Create memory/ + its subdirs + the ignore file INSIDE AN EXISTING project.

    Raises FileNotFoundError if the project directory itself is gone.

    ``mkdir(parents=True)`` materialises the whole chain, so a project the user
    deleted or renamed mid-session was silently RECREATED as an empty shell —
    memory.db, .gitignore, sessions/ and topics/ included — by whichever
    surface touched it next. `ui/dashboard.py` already refused to do that, but
    it refused in a private method, so the other six creators (both hooks,
    cli/mem.py, cli/plan.py, and core/plan.py twice) each kept their own
    parents=True copy and kept resurrecting. One function, seven callers, and
    `core/db.py` dropped parents=True as a backstop for anything that opens a
    database without coming through here.
    """
    memory_dir = Path(memory_dir)
    if not memory_dir.parent.is_dir():
        raise FileNotFoundError(
            f"project directory not found: {memory_dir.parent}")
    if _markers_is_link(memory_dir):
        # Fail closed (register Y1, user-ratified): a linked memory/
        # redirects every artifact — memory.db, PROGRESS.md, session archives
        # — to wherever the link points, outside the project and outside
        # every reporting path. core/roots._has_db refuses the same shape as
        # project IDENTITY; this is the WRITE-side choke for a directory that
        # already exists. The probe is core.markers._is_link, NOT bare
        # is_symlink(): S_ISLNK is False for a Windows junction (`mklink /J`,
        # no admin needed), and the is_symlink()-only guard here ACCEPTED a
        # junctioned memory/ and created .gitignore/sessions/topics inside
        # the junction target (measured) — inert on the primary platform.
        # OSError so every caller's existing handler applies.
        raise OSError(
            f"memory/ at {memory_dir} is a symlink or junction; cc-memory "
            f"refuses to write through links (privacy fail-closed). Replace "
            f"it with a real directory, or pin an exotic layout with "
            f".ccm-root.")
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "sessions").mkdir(exist_ok=True)
    (memory_dir / "topics").mkdir(exist_ok=True)
    ensure_memory_gitignore(memory_dir)
    return memory_dir


# `_PKG_ROOT` is set and inserted at the top of this module (v2.13.0) — the
# constants above now import from `core.layout`, so the path setup had to move
# ahead of them. The duplicate that used to stand here was removed rather than
# left as a second, idempotent copy: two inserts is how they drift.
from core.atomic import write_atomic, _DERIVED_BUDGET_S
from core.db import MemoryDB
from core.logger import get_logger
# The junction-aware link probe. Aliased because this module already reads
# naturally with bare helper names; ensure_memory_dir above resolves it at
# call time, after this module-level import has run.
from core.markers import _is_link as _markers_is_link
from core.privacy import (neutralize_block, neutralize_document,
                          neutralize_inline, neutralize_markers)

_log = get_logger("progress")

# ── PROGRESS.md render budgets (register D4) ───────────────────────────────
# The progress ROW is the state; PROGRESS.md is a rendered VIEW of it, and a
# view read at every session start must be readable, not exhaustive. Nothing
# bounded §3: a 50k-entry open_todos rendered a 3.6 MiB document (measured).
# Both caps announce themselves in the output — no silent truncation.
_MAX_TODOS_RENDERED = 50
_MAX_PROGRESS_BYTES = 256 * 1024


def _coerce_entries(value, str_key: str) -> List[Dict]:
    """Normalize a list column that is *supposed* to hold dicts.

    `progress.open_todos` / `critical_context` / `files_touched` are JSON
    columns written by several paths (PreCompact, Stop, UserPromptSubmit, the
    MCP `progress_regenerate` tool, and hand edits). A bare list of strings is
    a realistic input, and it used to raise

        AttributeError: 'str' object has no attribute 'get'

    from write_progress_md — killing the entire handoff-document rewrite over
    one badly shaped entry. Strings become ``{str_key: s}``; anything that is
    neither a str nor a dict is skipped rather than crashing the rewrite.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [{str_key: value}] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    out: List[Dict] = []
    for item in value:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            if item.strip():
                out.append({str_key: item})
    return out


def collect_progress_state(db: MemoryDB, project_id: int,
                           memory_dir: Path,
                           current_request: str = "",
                           todos: Optional[List[Dict]] = None,
                           files_read: Optional[List[str]] = None,
                           files_modified: Optional[List[str]] = None,
                           transcript_ptr: str = "",
                           trigger_type: str = "precompact") -> Dict:
    """Build a complete progress state from DB + provided fresh data.

    Used by PreCompact to do a FULL rewrite.
    """
    # Aggregate from latest session summary
    summary = db.get_latest_summary(project_id) or {}

    # Critical memories (importance >= 4, top 10 newest)
    crit = db.get_critical_memories(project_id, min_importance=4)[:10]
    critical_ctx = [
        {"id": m["id"], "category": m["category"], "topic": m.get("topic", ""),
         "content": m["content"][:200]}
        for m in crit
    ]

    # Open todos: filter to non-completed if provided
    open_todos = []
    for t in _coerce_entries(todos, "content"):
        status = t.get("status", "pending")
        if status != "completed":
            open_todos.append({
                "content": str(t.get("content", ""))[:300],
                "priority": t.get("priority", "medium"),
                "status": status,
            })

    # Files touched
    files_touched = []
    if files_read:
        for f in dict.fromkeys(files_read):
            files_touched.append({"path": f, "action": "read"})
    if files_modified:
        for f in dict.fromkeys(files_modified):
            files_touched.append({"path": f, "action": "edit"})

    # Status string fields can be derived from summary
    status_done = summary.get("completed", "")
    status_in_flight = summary.get("learned", "")  # work in progress

    next_steps = summary.get("next_steps", "")

    return {
        "current_request":  current_request or summary.get("request", ""),
        "status_done":      status_done,
        "status_in_flight": status_in_flight,
        "status_blocked":   "",  # populated only via patch_progress when known
        "open_todos":       open_todos,
        "plan":             next_steps,
        "critical_context": critical_ctx,
        "files_touched":    files_touched,
        "transcript_ptr":   transcript_ptr,
        "trigger_type":     trigger_type,
    }


def _short_sid(sid: str, width: int = 8) -> str:
    """First N chars of a Claude session UUID, with a leading hash to make it
    visually distinct from plain numbers in the rendered table."""
    s = neutralize_inline(sid)
    return ("#" + s[:width]) if s else "(untagged)"


def _short_ts(ts: str) -> str:
    """Trim ISO timestamps to date+HH:MM for readability in the timeline."""
    s = neutralize_inline(ts)
    if not s:
        return "(unknown)"
    # accept '2026-06-02T12:56:18' or '2026-06-02T12:56:18.123' etc.
    return s.replace("T", " ")[:16]


# Moved to core.atomic in v2.5.3. This module's copy was the STRONGEST of the
# three that v2.5.2 shipped (it retried and re-raised); the other two fell back
# to a truncating write, which is the defect the function exists to remove.
# One implementation now, for exactly the reason `is_excluded` and the
# .gitignore line list each had to be unified after they drifted. Bound to the
# old private name so this module's call site is unchanged.
_atomic_write = write_atomic


# Moved to core.privacy in v2.5.2 — core/plan.py needs the identical escaping
# for PLAN.md's Goal and Context blocks, and a second copy of the marker
# defence is exactly how the six `is_excluded` call sites drifted apart before
# v2.5.0. Bound to the old private name so this module's six call sites below
# are unchanged; the implementation and its rationale now live in one place.
_neutralize_block = neutralize_block


def _render_session_section(db: MemoryDB, project_id: int, prog: Dict) -> List[str]:
    """Build the §0 Session block.

    Reads:
      - current session tag from `prog` (progress row)
      - prior session history from db.get_recent_sessions()

    The current session is marked with 🟢 + "YOU" so a new Claude reading
    the file knows immediately whether the row belongs to its own session.
    Prior sessions are listed newest-first with brief summaries.
    """
    # cur_sid stays RAW: it is compared against `sessions.claude_session_id`
    # below. Every RENDER of it goes through _short_sid, which neutralises.
    cur_sid = (prog.get("current_session_id") or "").strip()
    started = (prog.get("session_started_at") or "").strip()
    trigger = neutralize_inline(prog.get("trigger_type") or "")
    updated = (prog.get("updated_at") or "").strip()

    out: List[str] = ["## 0. Session", ""]

    # --- Current session line ------------------------------------------------
    if cur_sid:
        out.append(
            f"🟢 **Current session**: `{_short_sid(cur_sid)}`  ·  "
            f"started `{_short_ts(started)}`  ·  "
            f"last write `{_short_ts(updated)}`"
            + (f"  ·  trigger `{trigger}`" if trigger else "")
        )
        out.append("")
        out.append(
            "> If your Claude session ID does NOT start with "
            f"`{_short_sid(cur_sid)[1:]}`, this row was written by a "
            "different session — treat the §3 todos / §6 files as that "
            "session's work, not yours."
        )
    else:
        out.append("⚪ **Current session**: *(no session tagged — first run, or a write path bypassed `tag_progress_session`)*")
    out.append("")

    # --- Prior session timeline ---------------------------------------------
    recent = db.get_recent_sessions(project_id, n=5) or []
    # Filter out the current session from the timeline so it isn't listed twice
    prior = [r for r in recent
             if (r.get("claude_session_id") or "") != cur_sid]
    if not prior:
        out.append("*(no prior compacted sessions yet)*")
    else:
        out.append("**Prior sessions** (most recent first):")
        out.append("")
        for r in prior[:5]:
            sid = _short_sid(r.get("claude_session_id") or "")
            ended = _short_ts(r.get("compacted_at") or "")
            msgs = r.get("msg_count") or 0
            # Prefer the session_summary.completed line; fall back to brief_summary
            summary = (
                (r.get("summary_completed") or "").strip()
                or (r.get("brief_summary") or "").strip()
                or "(no summary)"
            )
            # Flatten embedded newlines + collapse runs of whitespace so a
            # multi-line brief_summary doesn't break the list-item alignment —
            # and neutralise markers, because a summary is LLM-written text
            # rendered into a one-line slot (see core.privacy).
            summary = neutralize_inline(summary)
            if len(summary) > 100:
                summary = summary[:97] + "..."
            out.append(f"- `{sid}`  ·  ended `{ended}`  ·  {msgs} msgs  ·  {summary}")
    out.append("")
    return out


def write_progress_md(db: MemoryDB, project_id: int, memory_dir: Path) -> Path:
    """Render the `progress` row to memory/PROGRESS.md (FULL REWRITE).

    Returns the path to the written file.

    Every field interpolated below is CONTENT, not structure, and all of it is
    model-reachable: `critical_context` is memory text (and `memory_add` is a
    model-invokable MCP tool), `plan` / `status_*` come from the LLM session
    summary, `open_todos` / `files_touched` come from tool traffic. So each one
    goes through core.privacy — `neutralize_inline` for slots that own exactly
    one rendered line (a newline there opens a forged `## N.` section),
    `neutralize_markers` for the genuinely multi-line slots, whose newlines are
    real structure. Measured before this change, from ONE stored memory: 4
    complete `<system-reminder>` blocks and 3 copies of
    "## 7. Pre-compact Transcript Pointer" in a document that has 1.
    """
    prog = db.get_progress(project_id) or {}
    # A replacement query was added below without deleting the original, so
    # this ran on the per-turn Stop path: five discarded round-trips (each
    # opening a connection and running seven PRAGMAs) whose result the next
    # assignment overwrote unconditionally. It was also the only unguarded
    # subscript here — `get_project_by_path` returns None on a miss, and
    # `None["path"]` is a TypeError that would take the whole PROGRESS.md
    # rewrite down for a value nothing reads.
    with db._connect() as conn:
        row = conn.execute(
            "SELECT name, path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        project_name = neutralize_inline(row["name"] if row else "(unknown)")
        project_path = neutralize_inline(row["path"] if row else "")

    updated_at = neutralize_inline(
        prog.get("updated_at") or datetime.now().isoformat(timespec="seconds"))
    trigger = neutralize_inline(prog.get("trigger_type") or "")

    lines = [
        f"# PROGRESS — {project_name}",
        "",
        f"*Generated: {updated_at}*"
        + (f" · via {trigger}" if trigger else "")
        + (f" · {project_path}" if project_path else ""),
        "",
        "> SINGLE SOURCE OF TRUTH for session handoff. Always full-rewrite from SQLite",
        "> table `progress`. **Never append. Never patch by hand.**",
        "",
    ]

    # --- Session Annotation (v5) ---------------------------------------------
    # Top of the file so any reader can immediately tell:
    #   (a) is this MY session's progress or a stale write from a different one?
    #   (b) what did the prior sessions accomplish (project-wide context)?
    lines += _render_session_section(db, project_id, prog)

    # --- Current Request -----------------------------------------------------
    lines += ["## 1. Current Request", ""]
    cr = _neutralize_block((prog.get("current_request") or "").strip())
    lines.append(cr or "*(no request recorded yet)*")
    lines += [""]

    # --- Status --------------------------------------------------------------
    lines += ["## 2. Status", ""]
    done = _neutralize_block((prog.get("status_done") or "").strip())
    in_flight = _neutralize_block((prog.get("status_in_flight") or "").strip())
    blocked = _neutralize_block((prog.get("status_blocked") or "").strip())
    lines.append(f"**Done** —    {done or '*(none yet)*'}")
    lines.append("")
    lines.append(f"**In-flight** — {in_flight or '*(none active)*'}")
    lines.append("")
    lines.append(f"**Blocked** —  {blocked or '*(none)*'}")
    lines += [""]

    # --- Open Todos ----------------------------------------------------------
    # _coerce_entries: these three JSON columns are shared write surfaces (see
    # its docstring) — a bare string entry must degrade, never raise.
    lines += ["## 3. Open Todos", ""]
    todos = _coerce_entries(prog.get("open_todos"), "content")
    if not todos:
        lines.append("*(no open todos)*")
    else:
        for t in todos[:_MAX_TODOS_RENDERED]:
            prio = neutralize_inline(str(t.get("priority", "medium")))
            status = t.get("status", "pending")
            mark = "[ ]" if status == "pending" else "[~]"
            lines.append(f"- {mark} `{prio}` "
                         f"{neutralize_inline(str(t.get('content','')))}")
        if len(todos) > _MAX_TODOS_RENDERED:
            lines.append(f"- … {len(todos) - _MAX_TODOS_RENDERED} more "
                         f"(render capped at {_MAX_TODOS_RENDERED}; the "
                         f"`progress` row holds the full list)")
    lines += [""]

    # --- Plan ----------------------------------------------------------------
    lines += ["## 4. Plan (sequenced next steps)", ""]
    plan = _neutralize_block((prog.get("plan") or "").strip())
    lines.append(plan or "*(no plan recorded)*")
    lines += [""]

    # --- Critical Context ----------------------------------------------------
    lines += ["## 5. Critical Context (must-know memories)", ""]
    crit = _coerce_entries(prog.get("critical_context"), "content")
    if not crit:
        lines.append("*(no critical memories)*")
    else:
        for m in crit[:10]:
            mid = neutralize_inline(str(m.get("id", "?")))
            cat = neutralize_inline(str(m.get("category", "")))
            topic = neutralize_inline(str(m.get("topic", "")))
            topic_tag = f"[{topic}] " if topic else ""
            content = neutralize_inline(str(m.get("content", "") or ""))[:200]
            lines.append(f"- #{mid} `{cat}` {topic_tag}{content}")
    lines += [""]

    # --- Files Touched -------------------------------------------------------
    lines += ["## 6. Files Touched This Session", ""]
    # bare strings coerce to {"path": s} here, not {"content": s} — this
    # column's entries are {path, action}, so the path is the meaningful key
    files = _coerce_entries(prog.get("files_touched"), "path")
    if not files:
        lines.append("*(no files touched)*")
    else:
        # Group by action
        by_action: Dict[str, List[str]] = {}
        for f in files:
            by_action.setdefault(
                neutralize_inline(str(f.get("action", "?"))) or "?", []
            ).append(neutralize_inline(str(f.get("path", ""))))
        for action, paths in by_action.items():
            lines.append(f"**{action}**:")
            for p in list(dict.fromkeys(paths))[:30]:
                lines.append(f"  - `{p}`")
            lines.append("")

    # --- Transcript pointer --------------------------------------------------
    lines += ["## 7. Pre-compact Transcript Pointer", ""]
    tptr = neutralize_inline(prog.get("transcript_ptr") or "")
    if tptr:
        lines.append("If you need raw conversation history before compaction, read:")
        lines.append("")
        # Fence widened past the longest backtick run in the pointer — the same
        # defence core.plan.render_pending_plan_md already applies to raw plan
        # text. A path containing ``` would otherwise close the block early and
        # let the rest of the value render as document structure.
        longest_run, run = 0, 0
        for ch in tptr:
            run = run + 1 if ch == "`" else 0
            longest_run = max(longest_run, run)
        fence = "`" * max(3, longest_run + 1)
        lines.append(f"{fence}\n{tptr}\n{fence}")
        lines.append("")
        lines.append("This is a JSONL file: one message per line. Read with the Read tool.")
    else:
        lines.append("*(transcript pointer not yet recorded)*")
    lines += [""]

    # --- Footer --------------------------------------------------------------
    lines += [
        "---",
        "*This file is the handoff contract for the next session. Read it FIRST.*",
        "*Spec: `docs/CONTRACTS.md#handoff-contract` · Anti-patch contract: `docs/CONTRACTS.md#anti-patch-contract`*",
    ]

    out = memory_dir / "PROGRESS.md"
    # neutralize_document, not a bare join: every slot above is already escaped
    # individually, and that is exactly what a marker split across two slots
    # survives — see its docstring for the measured PROGRESS.md forgery.
    text = neutralize_document("\n".join(lines))
    if len(text.encode("utf-8")) > _MAX_PROGRESS_BYTES:
        # Line-boundary cut under the byte budget, with a LOUD notice. All
        # content above is already neutralised, so the worst a cut can do is
        # leave a fence open around the notice — cosmetic, never structural.
        # The notice's own bytes are RESERVED out of the budget (register
        # r6-C10): slicing at the full cap and then appending pushed the
        # rendered file past the very limit the slice enforced.
        notice = ("\n\n*(TRUNCATED: PROGRESS.md hit the "
                  f"{_MAX_PROGRESS_BYTES // 1024} KiB render budget; the "
                  "`progress` SQL row holds the full state.)*")
        keep = max(0, _MAX_PROGRESS_BYTES - len(notice.encode("utf-8")))
        cut = text.encode("utf-8")[:keep].decode("utf-8", errors="ignore")
        text = cut.rsplit("\n", 1)[0] + notice
    _atomic_write(out, text)
    return out


def write_session_archive(memory_dir: Path, project_name: str,
                          archive_text: str, file_ts: str) -> Path:
    """Write a session archive (one per compaction) under sessions/YYYY/MM/.

    `errors="replace"`: this call sits ABOVE insert_session, upsert_batch and
    write_progress_md in hooks/pre_compact.py, so one lone surrogate anywhere in
    the extracted text used to raise UnicodeEncodeError and lose the ENTIRE
    compaction — no archive, no session row, no memories, and no PROGRESS.md,
    which is the one thing the plugin exists to guarantee. A replacement
    character in an archive is strictly better than losing the handoff.
    """
    # `ym` comes from file_ts, NOT from a second datetime.now(). The caller
    # (hooks/pre_compact.py:_reserve_archive_ts) has ALREADY claimed the exact
    # path sessions/<ym>/session_<file_ts>.md with O_CREAT|O_EXCL, using its own
    # `now`; taking a fresh clock reading here means the claim and the write can
    # straddle a month boundary, which voids that atomic claim (collisions can
    # destroy an archive again) and orphans a 0-byte placeholder in the previous
    # month. file_ts begins with %Y%m%d by construction — plus a millisecond
    # field and, on collision, a -N suffix — so deriving from it makes the two
    # agree by definition. The clock fallback covers a caller that passes some
    # other stem shape.
    ym = (f"{file_ts[:4]}/{file_ts[4:6]}"
          if len(file_ts) >= 6 and file_ts[:6].isdigit()
          else datetime.now().strftime("%Y/%m"))
    archive_dir = memory_dir / "sessions" / ym
    # Component-by-component, NOT parents=True: parents=True materialises the
    # whole chain, so a project deleted after the caller's ensure_memory_dir
    # check was silently resurrected as an empty shell by this write. Without
    # parents, a vanished memory_dir raises FileNotFoundError to the caller —
    # the same refusal contract as ensure_memory_dir itself.
    for part in (memory_dir / "sessions", memory_dir / "sessions" / ym.split("/")[0],
                 archive_dir):
        part.mkdir(exist_ok=True)
    archive_path = archive_dir / f"session_{file_ts}.md"
    # write_atomic, not write_text: this was the LAST generated artifact in
    # this module still using the truncate-then-write call that core/atomic.py
    # exists to remove. Measured under three concurrent readers, 150 writes:
    # `write_text` produced 332 EMPTY reads in 2,264 samples (14.7 %), against
    # 0 empty in 3.4 M samples for `write_atomic`. And here a torn file is
    # PERMANENT: `_reserve_archive_ts` has already claimed this exact path
    # with O_CREAT|O_EXCL and `sessions.archive_path` already points at it, so
    # the claim cannot be repeated and nothing rewrites it.
    #
    # `_DERIVED_BUDGET_S`, and it RAISES on exhaustion. `os.replace` onto a
    # path another process holds open is a `PermissionError` on Windows, and
    # the truncating write it replaces simply never failed — it tore instead.
    # Reproduced here with three concurrent readers: the plain write raised 0
    # times and produced 332 empty reads; the atomic one retries and then
    # raises. The caller decides what a failure costs — see
    # `hooks/pre_compact.py`, which keeps the compaction and its PROGRESS.md
    # handoff rather than trading them for an archive.
    write_atomic(archive_path, archive_text, budget_s=_DERIVED_BUDGET_S)
    return archive_path


def migrate_legacy_handoff(memory_dir: Path):
    """One-shot: move any stale SESSION_HANDOFF.md aside (it's polluted).

    Don't delete — rename to .v2.bak so the user can inspect if they want.
    """
    old = memory_dir / "SESSION_HANDOFF.md"
    if old.exists():
        bak = memory_dir / "SESSION_HANDOFF.md.v2.bak"
        try:
            # os.replace overwrites an existing backup ATOMICALLY. The old
            # unlink-then-rename pair had a window where the previous backup
            # was already gone while the rename could still fail (a sharing
            # violation on the source) — measured operation order
            # [unlink .v2.bak, rename FAILED], both generations lost
            # (register Y7). One syscall has no such window.
            os.replace(str(old), str(bak))
            _log.info(f"renamed legacy SESSION_HANDOFF.md → {bak.name}")
        except OSError as e:
            _log.error(f"could not rename legacy handoff: {e}")
