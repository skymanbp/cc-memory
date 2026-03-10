#!/usr/bin/env python3
"""
cc-memory/session_start.py -- SessionStart hook (matcher: "compact")
Triggered AFTER context compaction. Reads saved memory and prints to stdout
so Claude Code auto-injects it into the new context window.
"""
import json, sys, traceback
from datetime import datetime
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))
from db import MemoryDB


def build_context(memory_dir, db, project_id, project_name):
    lines = [
        "=== CC-MEMORY: Context Restored ===",
        f"Project: {project_name}  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    # 1. Critical memories (importance=5, all-time)
    critical = db.get_critical_memories(project_id, min_importance=5)
    if critical:
        lines += ["### Critical (Always Remember)", ""]
        for m in critical[:5]:
            lines.append(f"- [{m['category']}] {m['content']}")
        lines.append("")

    # 2. Recent memories grouped by category (last 3 sessions)
    recent = db.get_recent_memories(project_id, sessions_back=3, min_importance=2, limit=25)
    if recent:
        by_cat = {}
        for m in recent:
            by_cat.setdefault(m["category"], []).append(m)
        order = ["decision", "bug", "result", "task", "config", "arch"]
        labels = {"decision": "Recent Decisions", "bug": "Bugs Fixed",
                  "result": "Recent Results", "task": "Active Tasks",
                  "config": "Config Changes", "arch": "Architecture Notes"}
        for cat in order:
            if cat not in by_cat:
                continue
            lines += [f"### {labels.get(cat, cat.title())}", ""]
            for m in by_cat[cat][:4]:
                prefix = "! " if m["importance"] >= 4 else "- "
                lines.append(f"{prefix}{m['content']}")
            lines.append("")

    # 3. Last session handoff snippet
    handoff = memory_dir / "SESSION_HANDOFF.md"
    if handoff.exists():
        text = handoff.read_text(encoding="utf-8").strip()
        body_lines = text.splitlines()
        body = "\n".join(body_lines[2:]) if len(body_lines) > 2 else text
        if len(body) > 650:
            body = body[:647] + "..."
        if body.strip():
            lines += ["### Last Session State", "", body, ""]

    stats = db.get_stats(project_id)
    lines.append(f"[{stats['n_sessions']} sessions, {stats['n_memories']} memories]")
    lines += ["=== END CC-MEMORY ===", ""]
    return "\n".join(lines)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"[cc-memory] session_start stdin error: {e}", file=sys.stderr)
        sys.exit(0)

    cwd = data.get("cwd", "")
    if not cwd:
        sys.exit(0)

    try:
        memory_dir = Path(cwd) / "memory"
        db_path = memory_dir / "memory.db"
        if not db_path.exists():
            print(f"[cc-memory] no DB for {cwd}", file=sys.stderr)
            sys.exit(0)
        db = MemoryDB(db_path)
        project_id = db.upsert_project(cwd)
        print(build_context(memory_dir, db, project_id, Path(cwd).name))
        print(f"[cc-memory] injected context for {Path(cwd).name}", file=sys.stderr)
    except Exception:
        print(f"[cc-memory] session_start ERROR:\n{traceback.format_exc()}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
