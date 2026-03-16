#!/usr/bin/env python3
"""
cc-memory/session_start.py -- SessionStart hook
================================================
Triggered on EVERY new session (startup, resume, post-compaction).

Two jobs:
  1. INJECT: Read topic summaries + recent memories -> print to stdout
  2. RETROACTIVE SAVE: Check if previous session was saved; extract if not.

Context injection strategy (Topic Consolidation):
  Level 0: Topic summaries    (always injected, compact)
  Level 1: Unmerged critical   (imp>=5 not yet in any topic summary)
  Level 2: Recent decisions    (last 3 sessions, imp>=3)
  Level 3: Session handoff     (from last session)

Stdout: context injection (seen by Claude)
Stderr: logging only
"""
import json
import os
import sys
import traceback
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))
from db import MemoryDB
from extractor import build_extraction, load_transcript


# ---------------------------------------------------------------------------
# Context injection (fast, always runs)
# ---------------------------------------------------------------------------
def build_context(memory_dir, db, project_id, project_name):
    lines = [
        "=== CC-MEMORY: Context Restored ===",
        f"Project: {project_name}  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    # ── 1. Topic summaries (L0: always injected, most important) ─────────
    topics = db.get_topics(project_id)
    if topics:
        lines.append("### Knowledge Base (by topic)")
        lines.append("")
        for t in topics:
            # Compact: truncate to ~250 chars to keep total injection reasonable
            summary = t["content"]
            if len(summary) > 250:
                # Cut at last sentence boundary within limit
                cut = summary[:250].rfind(".")
                if cut > 100:
                    summary = summary[:cut+1]
                else:
                    summary = summary[:247] + "..."
            lines.append(f"**[{t['name']}]** {summary}")
            lines.append("")

    # ── 2. Unmerged critical memories (not yet captured in topics) ────────
    topic_names = {t["name"] for t in topics} if topics else set()
    critical = db.get_critical_memories(project_id, min_importance=5)
    # Filter out memories that belong to a consolidated topic
    unmerged = [
        m for m in critical
        if not m.get("topic") or m.get("topic") not in topic_names
    ]
    if unmerged:
        lines.append("### Critical (unmerged)")
        lines.append("")
        for m in unmerged[:8]:
            lines.append(f"- [{m['category']}] {m['content']}")
        lines.append("")

    # ── 3. Recent high-value memories (last 3 sessions, imp>=3) ──────────
    recent = db.get_recent_memories(
        project_id, sessions_back=3, min_importance=3, limit=15
    )
    if recent:
        # Filter out those already shown in critical or topics
        shown_ids = {m["id"] for m in unmerged} if unmerged else set()
        fresh = [m for m in recent if m["id"] not in shown_ids]

        if fresh:
            # Group by category, show compactly
            by_cat = {}
            for m in fresh:
                by_cat.setdefault(m["category"], []).append(m)

            labels = {
                "decision": "Recent Decisions",
                "bug": "Bugs Fixed",
                "result": "Results",
                "task": "Active Tasks",
                "config": "Config Changes",
                "arch": "Architecture",
            }
            priority_order = ["decision", "bug", "result", "task", "config", "arch"]
            has_section = False
            for cat in priority_order:
                if cat not in by_cat:
                    continue
                if not has_section:
                    lines.append("### Recent")
                    lines.append("")
                    has_section = True
                items = by_cat[cat][:3]
                for m in items:
                    prefix = "! " if m["importance"] >= 4 else "- "
                    lines.append(f"{prefix}[{cat}] {m['content']}")
            if has_section:
                lines.append("")

    # ── 4. Last session handoff ──────────────────────────────────────────
    handoff = memory_dir / "SESSION_HANDOFF.md"
    if handoff.exists():
        text = handoff.read_text(encoding="utf-8").strip()
        body_lines = text.splitlines()
        body = "\n".join(body_lines[2:]) if len(body_lines) > 2 else text
        if len(body) > 500:
            body = body[:497] + "..."
        if body.strip():
            lines += ["### Last Session State", "", body, ""]

    # ── 5. Footer ────────────────────────────────────────────────────────
    last_save = memory_dir / ".last_save.json"
    if last_save.exists():
        try:
            save_info = json.loads(last_save.read_text(encoding="utf-8"))
            ts = save_info.get("timestamp", "?")
            if save_info.get("success"):
                method = save_info.get("method", "?")
                n = save_info.get("n_saved", 0)
                lines.append(f"[Last auto-save: {ts} | {n} memories via {method}]")
            else:
                lines.append(f"[Last auto-save FAILED at {ts}]")
        except Exception:
            pass

    try:
        from auth import get_api_key
        _key, source = get_api_key()
        if source == "oauth_expired":
            lines.append("[WARNING: Claude OAuth token expired -- LLM extraction disabled until refreshed]")
        elif not _key:
            lines.append("[WARNING: No API key -- LLM memory extraction disabled]")
    except Exception:
        pass

    stats = db.get_stats(project_id)
    lines.append(
        f"[{stats['n_sessions']} sessions, {stats['n_memories']} memories, "
        f"{stats.get('n_topics', 0)} topics]"
    )
    lines += [
        "",
        "REMINDER: Before ending this conversation, call /save-memories to preserve important information.",
        "=== END CC-MEMORY ===",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Retroactive save from previous session
# ---------------------------------------------------------------------------
_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_TIMEOUT = 20

_EXTRACTION_PROMPT = """\
You are a memory extraction system. Given a Claude Code conversation transcript, extract the most important information worth remembering across sessions.

Output a JSON array of objects: {"category": str, "content": str, "importance": int, "topic": str}
- category: decision|result|config|bug|task|arch|note
- content: one concise, self-contained sentence with specific values
- importance: 1-5 (5=critical, 4=important, 3=useful)
- topic: a short keyword for the topic (e.g. "cnn", "gnn", "pipeline", "config")

Rules: Only conclusions, not process. Self-contained. Specific values. 5-15 items max.
Output ONLY valid JSON array."""


def _find_transcript_dir(project_path: str) -> "Path | None":
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None

    path_str = str(Path(project_path).resolve())
    hash_candidate = path_str.replace(":", "-").replace("\\", "-").replace("/", "-")

    candidate = claude_projects / hash_candidate
    if candidate.exists():
        return candidate

    hash_lower = hash_candidate.lower()
    for d in claude_projects.iterdir():
        if d.is_dir() and d.name.lower() == hash_lower:
            return d

    proj_name = Path(project_path).name.lower()
    best, best_mtime = None, 0
    for d in claude_projects.iterdir():
        if d.is_dir() and proj_name in d.name.lower():
            jsonls = list(d.glob("*.jsonl"))
            if jsonls:
                mtime = max(f.stat().st_mtime for f in jsonls)
                if mtime > best_mtime:
                    best, best_mtime = d, mtime
    return best


def _get_saved_session_ids(db, project_id: int) -> set:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT claude_session_id FROM sessions WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    return {r["claude_session_id"] for r in rows if r["claude_session_id"]}


def _build_transcript_summary(messages: list, max_chars: int = 12000) -> str:
    parts = []
    total = 0
    for msg in messages:
        message = msg.get("message", {})
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        content = message.get("content", "")

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        if name in ("Edit", "Write", "MultiEdit"):
                            text_parts.append(f"[Tool: {name} {inp.get('file_path', '')}]")
                        elif name == "Bash":
                            text_parts.append(f"[Bash: {inp.get('command', '')[:100]}]")
                        else:
                            text_parts.append(f"[Tool: {name}]")
            text = "\n".join(text_parts)
        else:
            continue

        if not text.strip():
            continue
        if len(text) > 800:
            text = text[:400] + "\n...\n" + text[-400:]

        line = f"[{role}] {text}\n"
        if total + len(line) > max_chars:
            parts.append(f"\n[...truncated, {len(messages) - len(parts)} more messages...]")
            break
        parts.append(line)
        total += len(line)

    return "\n".join(parts)


def _extract_via_llm(messages: list) -> "list[dict] | None":
    from auth import get_api_key
    api_key, source = get_api_key()
    if not api_key:
        return None

    transcript_text = _build_transcript_summary(messages)
    if len(transcript_text) < 100:
        return None

    body = json.dumps({
        "model": _HAIKU_MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": f"Extract memories:\n\n{transcript_text}"}],
        "system": _EXTRACTION_PROMPT,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        _API_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        text_content = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text_content += block.get("text", "")

        text_content = text_content.strip()
        if text_content.startswith("```"):
            lines = text_content.split("\n")
            text_content = "\n".join(l for l in lines if not l.strip().startswith("```"))

        memories = json.loads(text_content)
        if not isinstance(memories, list):
            return None

        valid = []
        for m in memories:
            if not isinstance(m, dict):
                continue
            cat = m.get("category", "note")
            content = m.get("content", "").strip()
            imp = m.get("importance", 3)
            topic = m.get("topic", "")
            if not content or len(content) < 10:
                continue
            if cat not in ("decision", "result", "config", "bug", "task", "arch", "note"):
                cat = "note"
            valid.append({
                "category": cat, "content": content,
                "importance": max(1, min(int(imp), 5)),
                "topic": topic if isinstance(topic, str) else "",
            })

        return valid if valid else None
    except Exception:
        return None


def retroactive_save(cwd: str, db, project_id: int, current_session_id: str = ""):
    transcript_dir = _find_transcript_dir(cwd)
    if not transcript_dir:
        return

    saved_ids = _get_saved_session_ids(db, project_id)

    jsonl_files = sorted(
        transcript_dir.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    n_retroactive = 0
    for jsonl in jsonl_files[:3]:
        session_uuid = jsonl.stem

        if session_uuid == current_session_id:
            continue
        if session_uuid in saved_ids:
            continue
        if jsonl.stat().st_size < 1024:
            continue

        print(f"[cc-memory] retroactive save for {session_uuid[:8]}...", file=sys.stderr)
        try:
            messages = load_transcript(str(jsonl))
            if not messages or len(messages) < 5:
                continue

            memories = _extract_via_llm(messages)
            method = "llm"
            if memories is None:
                memories = []
                method = "none"

            if not memories:
                continue

            existing = set()
            with db._connect() as conn:
                for row in conn.execute(
                    "SELECT content FROM memories WHERE project_id = ? AND is_active = 1",
                    (project_id,),
                ):
                    existing.add(row["content"].strip().lower())

            now = datetime.now()
            sid = db.insert_session(
                project_id=project_id,
                claude_session_id=session_uuid,
                trigger_type=f"retroactive_{method}",
                msg_count=len(messages),
                archive_path="",
                brief_summary=f"Retroactive save at {now.strftime('%Y-%m-%d %H:%M')}",
            )

            n_saved = 0
            for m in memories:
                if m["content"].strip().lower() in existing:
                    continue
                db.insert_memory(
                    project_id, sid, m["category"], m["content"],
                    importance=m["importance"],
                    tags=[method, "retroactive"],
                    topic=m.get("topic", ""),
                )
                existing.add(m["content"].strip().lower())
                n_saved += 1

            n_retroactive += 1
            print(
                f"[cc-memory] retroactive: {n_saved} memories from {session_uuid[:8]} via {method}",
                file=sys.stderr,
            )

        except Exception as e:
            print(f"[cc-memory] retroactive save error: {e}", file=sys.stderr)

    if n_retroactive:
        from pre_compact import _fmt_memory_index
        memory_dir = Path(cwd) / "memory"
        index_text = _fmt_memory_index(db, project_id, memory_dir)
        (memory_dir / "MEMORY.md").write_text(index_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"[cc-memory] session_start stdin error: {e}", file=sys.stderr)
        sys.exit(0)

    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
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

        # Job 1: Inject context (MUST happen first, fast)
        print(build_context(memory_dir, db, project_id, Path(cwd).name))
        print(f"[cc-memory] injected context for {Path(cwd).name}", file=sys.stderr)

        # Job 2: Retroactive save (best effort)
        try:
            retroactive_save(cwd, db, project_id, session_id)
        except Exception as e:
            print(f"[cc-memory] retroactive save failed: {e}", file=sys.stderr)

    except Exception:
        print(f"[cc-memory] session_start ERROR:\n{traceback.format_exc()}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
