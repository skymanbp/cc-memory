#!/usr/bin/env python3
"""
cc-memory/pre_compact.py  —  PreCompact hook
=============================================
Triggered by Claude Code BEFORE context compaction.
Reads the full conversation transcript, extracts structured memory,
and saves it to the project's memory/ directory + SQLite database.

Stdin (JSON):
  session_id      str   — Claude's internal session UUID
  transcript_path str   — path to the JSONL conversation file
  cwd             str   — current working directory (= project root)
  trigger         str   — "auto" | "manual"

Output:
  stderr only (informational); stdout must stay empty for PreCompact hooks.
  Hook NEVER blocks compaction (always exits 0).

Memory layout written:
  <cwd>/memory/
    memory.db                        ← SQLite (primary)
    MEMORY.md                        ← auto-generated index
    SESSION_HANDOFF.md               ← current session state (overwritten)
    sessions/YYYY/MM/
      session_YYYYMMDD_HHMMSS.md     ← archived full summary
"""

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

# ── resolve plugin directory so we can import siblings ──────────────────────
_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))

from db import MemoryDB
from extractor import (
    build_extraction,
    group_sentences,
    CATEGORY_ORDER,
    CATEGORY_LABELS,
    load_transcript,
)


# ---------------------------------------------------------------------------
# Markdown formatters
# ---------------------------------------------------------------------------
def _fmt_archive(ext: dict, timestamp: str, trigger: str, project_name: str) -> str:
    """Full session archive — kept for historical reference."""
    lines = [
        f"# Session Archive — {project_name}",
        f"**Timestamp**: {timestamp}  |  **Trigger**: {trigger}  "
        f"|  **Messages**: {ext['msg_count']}",
        "",
    ]

    # ── Metrics ─────────────────────────────────────────────────────────────
    if ext["metrics"]:
        lines += ["## Metrics & Results", ""]
        for m in ext["metrics"][:20]:
            lines.append(f"- `{m}`")
        lines.append("")

    # ── Categorised sentences ────────────────────────────────────────────────
    grouped = group_sentences(ext["sentences"])
    for cat in CATEGORY_ORDER:
        if cat not in grouped:
            continue
        label = CATEGORY_LABELS[cat]
        lines += [f"## {label}", ""]
        for text, imp in grouped[cat][:12]:
            prefix = "⚠️ " if imp >= 4 else "- "
            lines.append(f"{prefix}{text}")
        lines.append("")

    # ── Todo items ───────────────────────────────────────────────────────────
    if ext["todos"]:
        lines += ["## Todos", ""]
        for t in ext["todos"]:
            box = "[x]" if t["status"] == "completed" else "[ ]"
            lines.append(f"- {box} `{t['priority']}` {t['content']}")
        lines.append("")

    # ── File changes ─────────────────────────────────────────────────────────
    if ext["file_changes"]:
        lines += ["## Files Changed", ""]
        for f in ext["file_changes"][:15]:
            lines.append(f"- `{f}`")
        lines.append("")

    # ── Top keywords ─────────────────────────────────────────────────────────
    if ext["keywords"]:
        top = sorted(ext["keywords"].items(), key=lambda x: -x[1])[:15]
        lines += ["## Top Keywords", ""]
        lines.append(", ".join(f"`{k}`" for k, _ in top))
        lines.append("")

    # ── Last assistant message ────────────────────────────────────────────────
    if ext["assistant_texts"]:
        last = ext["assistant_texts"][-1]
        if len(last) > 600:
            last = last[:597] + "…"
        lines += ["## Last Response (truncated)", "", last, ""]

    return "\n".join(lines)


def _fmt_handoff(ext: dict, timestamp: str, project_name: str) -> str:
    """
    SESSION_HANDOFF.md — always overwritten, optimised for fast reading.
    This is what the SessionStart hook injects back into context.
    """
    lines = [
        f"# Session Handoff — {project_name}",
        f"*{timestamp}*",
        "",
    ]
    grouped = group_sentences(ext["sentences"])

    # Priority order: tasks → decisions → results → config → bugs
    priority = ["task", "decision", "result", "config", "bug"]

    for cat in priority:
        if cat not in grouped:
            continue
        label = CATEGORY_LABELS[cat]
        lines += [f"## {label}", ""]
        for text, imp in grouped[cat][:6]:
            prefix = "⚠️ " if imp >= 4 else "- "
            lines.append(f"{prefix}{text}")
        lines.append("")

    # Todos (from TodoWrite — authoritative)
    pending = [t for t in ext["todos"] if t["status"] != "completed"]
    if pending:
        lines += ["## Active Todos", ""]
        for t in pending[:10]:
            lines.append(f"- [ ] `{t['priority']}` {t['content']}")
        lines.append("")

    # Key metrics
    if ext["metrics"]:
        lines += ["## Key Metrics", ""]
        for m in ext["metrics"][:8]:
            lines.append(f"- `{m}`")
        lines.append("")

    # Files changed
    if ext["file_changes"]:
        lines += ["## Files Changed This Session", ""]
        for f in ext["file_changes"][:8]:
            lines.append(f"- `{f}`")
        lines.append("")

    return "\n".join(lines)


def _fmt_memory_index(db: MemoryDB, project_id: int, memory_dir: Path) -> str:
    """
    MEMORY.md — auto-generated index, replaces the old manual MEMORY.md.
    Lightweight: pointers to where things live, not the things themselves.
    """
    stats    = db.get_stats(project_id)
    topics   = db.get_topics(project_id)
    top_kw   = db.get_top_keywords(project_id, 25)
    critical = db.get_critical_memories(project_id, min_importance=5)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Memory Index  *(auto-generated by cc-memory)*",
        f"*Updated: {now_str}*  "
        f"|  Sessions: {stats['n_sessions']}  "
        f"|  Memories: {stats['n_memories']}",
        "",
    ]

    # ── Critical (importance=5) — always show ────────────────────────────────
    if critical:
        lines += ["## ⛔ Critical (Never Forget)", ""]
        for m in critical[:8]:
            lines.append(f"- **[{m['category']}]** {m['content']}")
        lines.append("")

    # ── Memory breakdown by category ─────────────────────────────────────────
    if stats["by_category"]:
        lines += ["## Memory by Category", ""]
        for row in stats["by_category"]:
            avg = f"{row['avg_imp']:.1f}"
            lines.append(f"- `{row['category']}`: {row['n']} entries  (avg importance {avg})")
        lines.append("")

    # ── Topics ───────────────────────────────────────────────────────────────
    if topics:
        lines += ["## Topic Files", ""]
        for t in topics:
            lines.append(
                f"- `memory/topics/{t['name']}.md`  "
                f"(v{t['version']}, updated {t['updated_at'][:10]})"
            )
        lines.append("")

    # ── Top keywords ─────────────────────────────────────────────────────────
    if top_kw:
        lines += ["## Project Vocabulary (top keywords)", ""]
        lines.append(", ".join(f"`{kw}`" for kw in top_kw))
        lines.append("")

    # ── Session archive index ────────────────────────────────────────────────
    sessions_dir = memory_dir / "sessions"
    if sessions_dir.exists():
        archive_files = sorted(
            sessions_dir.rglob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:10]
        if archive_files:
            lines += ["## Recent Session Archives", ""]
            for af in archive_files:
                rel = af.relative_to(memory_dir)
                lines.append(f"- `memory/{rel}`")
            lines.append("")

    lines += [
        "---",
        "*To query memories: `python ~/.claude/hooks/cc-memory/mem.py --help`*",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main hook entry point
# ---------------------------------------------------------------------------
def main():
    # ── Read hook input ──────────────────────────────────────────────────────
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        print(f"[cc-memory] pre_compact: stdin parse error: {exc}", file=sys.stderr)
        sys.exit(0)

    cwd             = data.get("cwd", "")
    transcript_path = data.get("transcript_path", "")
    trigger         = data.get("trigger", "auto")
    claude_sid      = data.get("session_id", "")

    if not cwd or not transcript_path:
        print("[cc-memory] pre_compact: missing cwd or transcript_path", file=sys.stderr)
        sys.exit(0)

    try:
        # ── Setup paths ──────────────────────────────────────────────────────
        memory_dir  = Path(cwd) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "sessions").mkdir(exist_ok=True)
        (memory_dir / "topics").mkdir(exist_ok=True)

        db          = MemoryDB(memory_dir / "memory.db")
        project_id  = db.upsert_project(cwd)
        project_name = Path(cwd).name

        # ── Load transcript ──────────────────────────────────────────────────
        messages = load_transcript(transcript_path)
        if not messages:
            print("[cc-memory] pre_compact: empty transcript, skipping", file=sys.stderr)
            sys.exit(0)

        # ── Extract structured info ──────────────────────────────────────────
        project_kw = db.get_top_keywords(project_id, 40)  # existing vocabulary
        ext        = build_extraction(messages, project_kw)

        # ── Timestamps ───────────────────────────────────────────────────────
        now       = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        file_ts   = now.strftime("%Y%m%d_%H%M%S")
        ym        = now.strftime("%Y/%m")

        # ── Write session archive ────────────────────────────────────────────
        archive_dir = memory_dir / "sessions" / ym
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"session_{file_ts}.md"

        archive_text = _fmt_archive(ext, timestamp, trigger, project_name)
        archive_path.write_text(archive_text, encoding="utf-8")

        # ── Write SESSION_HANDOFF.md ─────────────────────────────────────────
        handoff_text = _fmt_handoff(ext, timestamp, project_name)
        (memory_dir / "SESSION_HANDOFF.md").write_text(handoff_text, encoding="utf-8")

        # ── Persist to SQLite ────────────────────────────────────────────────
        # 1. Session record
        session_id = db.insert_session(
            project_id  = project_id,
            claude_session_id = claude_sid,
            trigger_type = trigger,
            msg_count   = ext["msg_count"],
            archive_path = str(archive_path.relative_to(memory_dir)),
            brief_summary = archive_text[:1000],
        )

        # 2. Individual memories by category
        # Base importance per category (heuristic)
        cat_base_imp = {
            "decision": 3, "result": 3, "arch": 3,
            "config": 2,   "bug": 4,    "task": 2, "note": 1,
        }
        grouped = group_sentences(ext["sentences"])
        for cat, items in grouped.items():
            base = cat_base_imp.get(cat, 2)
            for text, imp in items[:10]:   # top-10 per category
                db.insert_memory(
                    project_id, session_id, cat, text,
                    importance=max(imp, base),
                )

        # 3. Metrics as result memories
        for metric in ext["metrics"][:10]:
            db.insert_memory(
                project_id, session_id, "result", metric,
                importance=3, tags=["metric", "auto"],
            )

        # 4. Todos as task memories
        for t in ext["todos"]:
            imp = 3 if t["priority"] == "high" else 2
            db.insert_memory(
                project_id, session_id, "task",
                f"[{t['status']}] {t['content']}",
                importance=imp, tags=["todo", t["status"]],
            )

        # 5. Update keyword vocabulary
        if ext["keywords"]:
            db.upsert_keywords(project_id, ext["keywords"])

        # ── Regenerate MEMORY.md index ───────────────────────────────────────
        index_text = _fmt_memory_index(db, project_id, memory_dir)
        (memory_dir / "MEMORY.md").write_text(index_text, encoding="utf-8")

        # ── Done ─────────────────────────────────────────────────────────────
        print(
            f"[cc-memory] saved: {archive_path.name} "
            f"({ext['msg_count']} msgs, "
            f"{len(ext['sentences'])} sentences, "
            f"{len(ext['keywords'])} keywords)",
            file=sys.stderr,
        )

    except Exception:
        # NEVER let an exception block compaction
        print(f"[cc-memory] pre_compact ERROR:\n{traceback.format_exc()}",
              file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
