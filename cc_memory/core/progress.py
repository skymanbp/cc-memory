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
file. See docs/HANDOFF_PROTOCOL.md for the full handoff spec.
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from core.db import MemoryDB
from core.logger import get_logger

_log = get_logger("progress")


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
    if todos:
        for t in todos:
            status = t.get("status", "pending")
            if status != "completed":
                open_todos.append({
                    "content": t.get("content", "")[:300],
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
    s = (sid or "").strip()
    return ("#" + s[:width]) if s else "(untagged)"


def _short_ts(ts: str) -> str:
    """Trim ISO timestamps to date+HH:MM for readability in the timeline."""
    s = (ts or "").strip()
    if not s:
        return "(unknown)"
    # accept '2026-06-02T12:56:18' or '2026-06-02T12:56:18.123' etc.
    return s.replace("T", " ")[:16]


def _render_session_section(db: MemoryDB, project_id: int, prog: Dict) -> List[str]:
    """Build the §0 Session block.

    Reads:
      - current session tag from `prog` (progress row)
      - prior session history from db.get_recent_sessions()

    The current session is marked with 🟢 + "YOU" so a new Claude reading
    the file knows immediately whether the row belongs to its own session.
    Prior sessions are listed newest-first with brief summaries.
    """
    cur_sid = (prog.get("current_session_id") or "").strip()
    started = (prog.get("session_started_at") or "").strip()
    trigger = (prog.get("trigger_type") or "").strip()
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
            # multi-line brief_summary doesn't break the list-item alignment.
            summary = " ".join(summary.split())
            if len(summary) > 100:
                summary = summary[:97] + "..."
            out.append(f"- `{sid}`  ·  ended `{ended}`  ·  {msgs} msgs  ·  {summary}")
    out.append("")
    return out


def write_progress_md(db: MemoryDB, project_id: int, memory_dir: Path) -> Path:
    """Render the `progress` row to memory/PROGRESS.md (FULL REWRITE).

    Returns the path to the written file.
    """
    prog = db.get_progress(project_id) or {}
    project_name = Path(db.get_project_by_path(
        db.get_all_projects()[0]["path"] if db.get_all_projects() else "."
    )["path"]).name if db.get_progress(project_id) else "(unknown)"

    # Get project name properly
    with db._connect() as conn:
        row = conn.execute(
            "SELECT name, path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        project_name = row["name"] if row else "(unknown)"
        project_path = row["path"] if row else ""

    updated_at = prog.get("updated_at", datetime.now().isoformat(timespec="seconds"))
    trigger = prog.get("trigger_type", "")

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
    cr = (prog.get("current_request") or "").strip()
    lines.append(cr or "*(no request recorded yet)*")
    lines += [""]

    # --- Status --------------------------------------------------------------
    lines += ["## 2. Status", ""]
    done = (prog.get("status_done") or "").strip()
    in_flight = (prog.get("status_in_flight") or "").strip()
    blocked = (prog.get("status_blocked") or "").strip()
    lines.append(f"**Done** —    {done or '*(none yet)*'}")
    lines.append("")
    lines.append(f"**In-flight** — {in_flight or '*(none active)*'}")
    lines.append("")
    lines.append(f"**Blocked** —  {blocked or '*(none)*'}")
    lines += [""]

    # --- Open Todos ----------------------------------------------------------
    lines += ["## 3. Open Todos", ""]
    todos = prog.get("open_todos") or []
    if not todos:
        lines.append("*(no open todos)*")
    else:
        for t in todos:
            prio = t.get("priority", "medium")
            status = t.get("status", "pending")
            mark = "[ ]" if status == "pending" else "[~]"
            lines.append(f"- {mark} `{prio}` {t.get('content','')}")
    lines += [""]

    # --- Plan ----------------------------------------------------------------
    lines += ["## 4. Plan (sequenced next steps)", ""]
    plan = (prog.get("plan") or "").strip()
    lines.append(plan or "*(no plan recorded)*")
    lines += [""]

    # --- Critical Context ----------------------------------------------------
    lines += ["## 5. Critical Context (must-know memories)", ""]
    crit = prog.get("critical_context") or []
    if not crit:
        lines.append("*(no critical memories)*")
    else:
        for m in crit[:10]:
            mid = m.get("id", "?")
            cat = m.get("category", "")
            topic = m.get("topic", "")
            topic_tag = f"[{topic}] " if topic else ""
            content = (m.get("content", "") or "")[:200]
            lines.append(f"- #{mid} `{cat}` {topic_tag}{content}")
    lines += [""]

    # --- Files Touched -------------------------------------------------------
    lines += ["## 6. Files Touched This Session", ""]
    files = prog.get("files_touched") or []
    if not files:
        lines.append("*(no files touched)*")
    else:
        # Group by action
        by_action: Dict[str, List[str]] = {}
        for f in files:
            by_action.setdefault(f.get("action", "?"), []).append(f.get("path", ""))
        for action, paths in by_action.items():
            lines.append(f"**{action}**:")
            for p in list(dict.fromkeys(paths))[:30]:
                lines.append(f"  - `{p}`")
            lines.append("")

    # --- Transcript pointer --------------------------------------------------
    lines += ["## 7. Pre-compact Transcript Pointer", ""]
    tptr = (prog.get("transcript_ptr") or "").strip()
    if tptr:
        lines.append("If you need raw conversation history before compaction, read:")
        lines.append("")
        lines.append(f"```\n{tptr}\n```")
        lines.append("")
        lines.append("This is a JSONL file: one message per line. Read with the Read tool.")
    else:
        lines.append("*(transcript pointer not yet recorded)*")
    lines += [""]

    # --- Footer --------------------------------------------------------------
    lines += [
        "---",
        "*This file is the handoff contract for the next session. Read it FIRST.*",
        "*Spec: `docs/HANDOFF_PROTOCOL.md` · Anti-patch contract: `docs/MEMORY_RULES.md`*",
    ]

    out = memory_dir / "PROGRESS.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_session_archive(memory_dir: Path, project_name: str,
                          archive_text: str, file_ts: str) -> Path:
    """Write a session archive (one per compaction) under sessions/YYYY/MM/."""
    now = datetime.now()
    ym = now.strftime("%Y/%m")
    archive_dir = memory_dir / "sessions" / ym
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"session_{file_ts}.md"
    archive_path.write_text(archive_text, encoding="utf-8")
    return archive_path


def migrate_legacy_handoff(memory_dir: Path):
    """One-shot: move any stale SESSION_HANDOFF.md aside (it's polluted).

    Don't delete — rename to .v2.bak so the user can inspect if they want.
    """
    old = memory_dir / "SESSION_HANDOFF.md"
    if old.exists():
        bak = memory_dir / "SESSION_HANDOFF.md.v2.bak"
        try:
            if bak.exists():
                bak.unlink()
            old.rename(bak)
            _log.info(f"renamed legacy SESSION_HANDOFF.md → {bak.name}")
        except OSError as e:
            _log.error(f"could not rename legacy handoff: {e}")
