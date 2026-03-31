#!/usr/bin/env python3
"""
cc-memory/pre_compact.py  --  PreCompact hook
=============================================
Triggered by Claude Code BEFORE context compaction.
Reads the full conversation transcript, extracts structured memory,
and saves it to the project's memory/ directory + SQLite database.

Extraction strategy (two-tier):
  1. PRIMARY: Call Haiku API for LLM-judged memory extraction (high quality)
     - Now includes topic assignment in extraction
  2. FALLBACK: Skip extraction if API unavailable (regex disabled)

Auto-consolidation:
  - Every CONSOLIDATION_INTERVAL sessions, run topic consolidation
  - Keeps topic summaries fresh and memory count manageable

Stdin (JSON):
  session_id      str
  transcript_path str
  cwd             str
  trigger         str   -- "auto" | "manual"

Output:
  stderr only; stdout must stay empty for PreCompact hooks.
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
from extractor import (
    build_extraction,
    group_sentences,
    CATEGORY_ORDER,
    CATEGORY_LABELS,
    load_transcript,
)
from logger import get_logger
from privacy import clean_for_storage

_log = get_logger("pre_compact")

# How often to auto-trigger consolidation (every N sessions)
CONSOLIDATION_INTERVAL = 5


# ---------------------------------------------------------------------------
# LLM-based extraction via Haiku API (primary)
# ---------------------------------------------------------------------------
_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_TIMEOUT = 25

_EXTRACTION_PROMPT = """\
You are a memory extraction system. Given a Claude Code conversation transcript, extract the most important information worth remembering across sessions.

For each memory, output a JSON array of objects with these fields:
- "category": one of "decision", "result", "config", "bug", "task", "arch", "note"
- "content": one concise, self-contained sentence with specific values (numbers, file names, parameters)
- "importance": 1-5 (5=critical/never-forget, 4=important, 3=useful, 2=minor, 1=trivial)
- "topic": a short lowercase keyword for grouping (e.g. "cnn", "gnn", "pipeline", "fusion", "config", "data")

Rules:
- Only save CONCLUSIONS, not discussion process or debugging steps
- Each memory must be understandable WITHOUT context
- Include specific values: "GNN D1 F1=0.741" not "GNN performed well"
- Skip: conversation logistics, tool errors, meta-discussion, trivial Q&A
- Output 5-15 memories maximum. Quality over quantity.
- Do NOT include memories about the memory system itself unless it's a critical bug fix

Output ONLY a valid JSON array, no markdown, no explanation."""


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
                            cmd = inp.get("command", "")[:100]
                            text_parts.append(f"[Bash: {cmd}]")
                        elif name == "TodoWrite":
                            text_parts.append(f"[TodoWrite: {json.dumps(inp.get('todos', [])[:5], ensure_ascii=False)[:200]}]")
                        else:
                            text_parts.append(f"[Tool: {name}]")
                    elif block.get("type") == "tool_result":
                        pass
            text = "\n".join(text_parts)
        else:
            continue

        if not text.strip():
            continue
        if len(text) > 800:
            text = text[:400] + "\n...[truncated]...\n" + text[-400:]

        line = f"[{role}] {text}\n"
        if total + len(line) > max_chars:
            parts.append(f"\n[...truncated, {len(messages) - len(parts)} more messages...]")
            break
        parts.append(line)
        total += len(line)

    return "\n".join(parts)


def _extract_via_llm(messages: list, observations: list = None) -> "list[dict] | None":
    from auth import get_api_key
    api_key, source = get_api_key()
    if not api_key:
        reason = "OAuth token expired" if source == "oauth_expired" else "no API key found"
        _log.info(f"{reason}, skipping LLM extraction")
        return None

    transcript_text = _build_transcript_summary(messages)
    if len(transcript_text) < 100:
        return None

    # Append observation context if available
    obs_context = ""
    if observations:
        obs_lines = []
        for o in observations[-50:]:  # Last 50 observations
            obs_lines.append(f"[{o['tool_name']}] {o['tool_input']}")
        if obs_lines:
            obs_context = "\n\nTool observations (for context):\n" + "\n".join(obs_lines)

    user_content = f"Extract memories from this conversation:\n\n{transcript_text}{obs_context}"

    body = json.dumps({
        "model": _HAIKU_MODEL,
        "max_tokens": 2500,
        "messages": [
            {
                "role": "user",
                "content": user_content,
            }
        ],
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
            text_content = "\n".join(
                l for l in lines if not l.strip().startswith("```")
            )

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
            imp = max(1, min(int(imp), 5))
            valid.append({
                "category": cat, "content": content,
                "importance": imp,
                "topic": topic if isinstance(topic, str) else "",
            })

        _log.info(f"LLM extracted {len(valid)} memories")
        return valid if valid else None

    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, OSError, KeyError, ValueError) as e:
        _log.error(f"LLM extraction failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Markdown formatters
# ---------------------------------------------------------------------------
def _fmt_archive(ext: dict, timestamp: str, trigger: str, project_name: str) -> str:
    lines = [
        f"# Session Archive -- {project_name}",
        f"**Timestamp**: {timestamp}  |  **Trigger**: {trigger}  "
        f"|  **Messages**: {ext['msg_count']}",
        "",
    ]

    if ext["metrics"]:
        lines += ["## Metrics & Results", ""]
        for m in ext["metrics"][:20]:
            lines.append(f"- `{m}`")
        lines.append("")

    grouped = group_sentences(ext["sentences"])
    for cat in CATEGORY_ORDER:
        if cat not in grouped:
            continue
        label = CATEGORY_LABELS[cat]
        lines += [f"## {label}", ""]
        for text, imp in grouped[cat][:12]:
            prefix = "! " if imp >= 4 else "- "
            lines.append(f"{prefix}{text}")
        lines.append("")

    if ext["todos"]:
        lines += ["## Todos", ""]
        for t in ext["todos"]:
            box = "[x]" if t["status"] == "completed" else "[ ]"
            lines.append(f"- {box} `{t['priority']}` {t['content']}")
        lines.append("")

    if ext["file_changes"]:
        lines += ["## Files Changed", ""]
        for f in ext["file_changes"][:15]:
            lines.append(f"- `{f}`")
        lines.append("")

    if ext["keywords"]:
        top = sorted(ext["keywords"].items(), key=lambda x: -x[1])[:15]
        lines += ["## Top Keywords", ""]
        lines.append(", ".join(f"`{k}`" for k, _ in top))
        lines.append("")

    if ext["assistant_texts"]:
        last = ext["assistant_texts"][-1]
        if len(last) > 600:
            last = last[:597] + "..."
        lines += ["## Last Response (truncated)", "", last, ""]

    return "\n".join(lines)


def _fmt_handoff(ext: dict, timestamp: str, project_name: str) -> str:
    lines = [
        f"# Session Handoff -- {project_name}",
        f"*{timestamp}*",
        "",
    ]
    grouped = group_sentences(ext["sentences"])
    priority = ["task", "decision", "result", "config", "bug"]

    for cat in priority:
        if cat not in grouped:
            continue
        label = CATEGORY_LABELS[cat]
        lines += [f"## {label}", ""]
        for text, imp in grouped[cat][:6]:
            prefix = "! " if imp >= 4 else "- "
            lines.append(f"{prefix}{text}")
        lines.append("")

    pending = [t for t in ext["todos"] if t["status"] != "completed"]
    if pending:
        lines += ["## Active Todos", ""]
        for t in pending[:10]:
            lines.append(f"- [ ] `{t['priority']}` {t['content']}")
        lines.append("")

    if ext["metrics"]:
        lines += ["## Key Metrics", ""]
        for m in ext["metrics"][:8]:
            lines.append(f"- `{m}`")
        lines.append("")

    if ext["file_changes"]:
        lines += ["## Files Changed This Session", ""]
        for f in ext["file_changes"][:8]:
            lines.append(f"- `{f}`")
        lines.append("")

    return "\n".join(lines)


def _fmt_memory_index(db: MemoryDB, project_id: int, memory_dir: Path) -> str:
    stats = db.get_stats(project_id)
    topics = db.get_topics(project_id)
    top_kw = db.get_top_keywords(project_id, 25)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Memory Index  *(auto-generated by cc-memory)*",
        f"*Updated: {now_str}*  "
        f"|  Sessions: {stats['n_sessions']}  "
        f"|  Memories: {stats['n_memories']}  "
        f"|  Topics: {stats.get('n_topics', 0)}",
        "",
    ]

    if topics:
        lines += ["## Topic Summaries (L1)", ""]
        for t in topics:
            preview = t["content"][:120] + "..." if len(t["content"]) > 120 else t["content"]
            lines.append(f"- **{t['name']}** (v{t['version']}): {preview}")
        lines.append("")

    # Topic memory counts
    topic_counts = db.get_topic_memory_counts(project_id)
    if topic_counts:
        lines += ["## Memory Distribution", ""]
        for topic, count in sorted(topic_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- `{topic}`: {count}")
        lines.append("")

    if stats["by_category"]:
        lines += ["## By Category", ""]
        for row in stats["by_category"]:
            avg = f"{row['avg_imp']:.1f}"
            lines.append(f"- `{row['category']}`: {row['n']} entries  (avg importance {avg})")
        lines.append("")

    if top_kw:
        lines += ["## Project Vocabulary", ""]
        lines.append(", ".join(f"`{kw}`" for kw in top_kw))
        lines.append("")

    sessions_dir = memory_dir / "sessions"
    if sessions_dir.exists():
        archive_files = sorted(
            sessions_dir.rglob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
        if archive_files:
            lines += ["## Recent Archives", ""]
            for af in archive_files:
                rel = af.relative_to(memory_dir)
                lines.append(f"- `memory/{rel}`")
            lines.append("")

    lines += [
        "---",
        "*Query: `python ~/.claude/hooks/cc-memory/mem.py --help`*",
        "*Consolidate: `python ~/.claude/hooks/cc-memory/mem.py --project <path> consolidate`*",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-consolidation trigger
# ---------------------------------------------------------------------------
def _maybe_consolidate(cwd: str, db: MemoryDB, project_id: int):
    """Run consolidation if enough sessions have passed since last consolidation."""
    n_sessions = db.get_session_count(project_id)
    if n_sessions % CONSOLIDATION_INTERVAL != 0:
        return

    _log.info(f"auto-consolidation triggered (session #{n_sessions})")
    try:
        from consolidate import run_consolidation
        results = run_consolidation(cwd, use_llm=True, verbose=True)
    except Exception as e:
        _log.error(f"auto-consolidation error: {e}")


# ---------------------------------------------------------------------------
# Main hook entry point
# ---------------------------------------------------------------------------
def main():
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        _log.error(f"stdin parse error: {exc}")
        sys.exit(0)

    cwd = data.get("cwd", "")
    transcript_path = data.get("transcript_path", "")
    trigger = data.get("trigger", "auto")
    claude_sid = data.get("session_id", "")

    if not cwd or not transcript_path:
        _log.warn("missing cwd or transcript_path")
        sys.exit(0)

    try:
        # ── Setup paths ──────────────────────────────────────────────────────
        memory_dir = Path(cwd) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "sessions").mkdir(exist_ok=True)
        (memory_dir / "topics").mkdir(exist_ok=True)

        db = MemoryDB(memory_dir / "memory.db")
        project_id = db.upsert_project(cwd)
        project_name = Path(cwd).name

        # ── Load transcript ──────────────────────────────────────────────────
        messages = load_transcript(transcript_path)
        if not messages:
            _log.info("empty transcript, skipping")
            sys.exit(0)

        # ── Extract structured info (always needed for archive/handoff) ─────
        project_kw = db.get_top_keywords(project_id, 40)
        ext = build_extraction(messages, project_kw)

        # ── Timestamps ───────────────────────────────────────────────────────
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        file_ts = now.strftime("%Y%m%d_%H%M%S")
        ym = now.strftime("%Y/%m")

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
        session_id = db.insert_session(
            project_id=project_id,
            claude_session_id=claude_sid,
            trigger_type=trigger,
            msg_count=ext["msg_count"],
            archive_path=str(archive_path.relative_to(memory_dir)),
            brief_summary=archive_text[:1000],
        )

        # ── Gather observations for enriched extraction ──────────────────
        last_session = db.get_recent_session_ids(project_id, 1)
        last_ts = ""
        if last_session:
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT compacted_at FROM sessions WHERE id = ?",
                    (last_session[0],)
                ).fetchone()
                if row:
                    last_ts = row["compacted_at"]
        observations = db.get_observations_since(project_id, last_ts) if last_ts else []

        # ── Extract memories via LLM (with observation context) ─────────
        extracted = _extract_via_llm(messages, observations)
        method = "llm" if extracted else "none"
        if not extracted:
            extracted = []
            _log.info("no API key or LLM failed; skipping memory extraction")

        # Save extracted memories with hash-based dedup
        n_saved = 0
        for m in extracted:
            content = clean_for_storage(m["content"])
            if not content or len(content) < 10:
                continue
            content_hash = MemoryDB.compute_content_hash(content)
            if db.is_duplicate_hash(project_id, content_hash):
                continue
            db.insert_memory(
                project_id, session_id, m["category"], content,
                importance=min(max(m["importance"], 1), 5),
                tags=[method, "auto"],
                topic=m.get("topic", ""),
            )
            n_saved += 1

        # Update keyword vocabulary
        if ext["keywords"]:
            db.upsert_keywords(project_id, ext["keywords"])

        # ── Structured session summary (derived, no extra LLM call) ─────
        obs_files_read = list(set(
            o["tool_input"] for o in observations
            if o["tool_name"] == "Read" and o["tool_input"]
        ))
        obs_files_modified = list(set(
            o["tool_input"] for o in observations
            if o["tool_name"] in ("Edit", "Write", "MultiEdit") and o["tool_input"]
        ))
        # Derive request from first user message
        first_user_msg = ""
        for msg in messages[:5]:
            message = msg.get("message", {})
            if isinstance(message, dict) and message.get("role") == "user":
                content_field = message.get("content", "")
                if isinstance(content_field, str):
                    first_user_msg = content_field[:300]
                elif isinstance(content_field, list):
                    for block in content_field:
                        if isinstance(block, dict) and block.get("type") == "text":
                            first_user_msg = block.get("text", "")[:300]
                            break
                if first_user_msg:
                    break
        # Derive next_steps from task memories
        task_mems = [m["content"] for m in extracted if m.get("category") == "task"]

        try:
            db.insert_session_summary(session_id, project_id, {
                "request": first_user_msg,
                "investigated": ", ".join(obs_files_read[:10]),
                "learned": "",
                "completed": ", ".join(obs_files_modified[:10]),
                "next_steps": "; ".join(task_mems[:5]),
                "notes": "",
                "files_read": obs_files_read[:20],
                "files_modified": obs_files_modified[:20],
            })
        except Exception as e:
            _log.error(f"session summary save error: {e}")

        # ── Cleanup processed observations ──────────────────────────────
        if observations:
            db.cleanup_observations(project_id, timestamp)

        # ── Regenerate MEMORY.md index ───────────────────────────────────────
        index_text = _fmt_memory_index(db, project_id, memory_dir)
        (memory_dir / "MEMORY.md").write_text(index_text, encoding="utf-8")

        # ── Write save status ────────────────────────────────────────────────
        status = {
            "timestamp": timestamp,
            "method": method,
            "n_saved": n_saved,
            "n_observations": len(observations),
            "msg_count": ext["msg_count"],
            "success": True,
        }
        try:
            (memory_dir / ".last_save.json").write_text(
                json.dumps(status, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

        # ── Auto-consolidation check ─────────────────────────────────────────
        _maybe_consolidate(cwd, db, project_id)

        # ── Done ─────────────────────────────────────────────────────────────
        _log.info(
            f"saved: {archive_path.name} "
            f"({ext['msg_count']} msgs, "
            f"{n_saved} new memories via {method}, "
            f"{len(observations)} observations, "
            f"{len(ext['keywords'])} keywords)"
        )

    except Exception:
        _log.error_tb("pre_compact ERROR")
        try:
            memory_dir = Path(cwd) / "memory"
            (memory_dir / ".last_save.json").write_text(
                json.dumps({"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "success": False, "error": traceback.format_exc()[-200:]}),
                encoding="utf-8",
            )
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
