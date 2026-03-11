#!/usr/bin/env python3
"""
cc-memory/pre_compact.py  —  PreCompact hook
=============================================
Triggered by Claude Code BEFORE context compaction.
Reads the full conversation transcript, extracts structured memory,
and saves it to the project's memory/ directory + SQLite database.

Extraction strategy (two-tier):
  1. PRIMARY: Call Haiku API for LLM-judged memory extraction (high quality)
  2. FALLBACK: Regex heuristics if API call fails (no key, timeout, network)

Stdin (JSON):
  session_id      str   — Claude's internal session UUID
  transcript_path str   — path to the JSONL conversation file
  cwd             str   — current working directory (= project root)
  trigger         str   — "auto" | "manual"

Output:
  stderr only (informational); stdout must stay empty for PreCompact hooks.
  Hook NEVER blocks compaction (always exits 0).
"""

import json
import os
import sys
import traceback
import urllib.request
import urllib.error
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
# LLM-based extraction via Haiku API (primary)
# ---------------------------------------------------------------------------
_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_TIMEOUT = 25  # seconds (hook timeout is 30)

_EXTRACTION_PROMPT = """\
You are a memory extraction system. Given a Claude Code conversation transcript, extract the most important information worth remembering across sessions.

For each memory, output a JSON array of objects with these fields:
- "category": one of "decision", "result", "config", "bug", "task", "arch", "note"
- "content": one concise, self-contained sentence with specific values (numbers, file names, parameters)
- "importance": 1-5 (5=critical/never-forget, 4=important, 3=useful, 2=minor, 1=trivial)

Rules:
- Only save CONCLUSIONS, not discussion process or debugging steps
- Each memory must be understandable WITHOUT context
- Include specific values: "GNN D1 F1=0.741" not "GNN performed well"
- Skip: conversation logistics, tool errors, meta-discussion, trivial Q&A
- Output 5-15 memories maximum. Quality over quantity.
- Do NOT include memories about the memory system itself unless it's a critical bug fix

Output ONLY a valid JSON array, no markdown, no explanation."""


def _build_transcript_summary(messages: list, max_chars: int = 12000) -> str:
    """Build a condensed transcript for the LLM prompt."""
    parts = []
    total = 0
    for msg in messages:
        message = msg.get("message", {})
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        content = message.get("content", "")

        # Extract text from content
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
                        # Skip tool results (too verbose)
                        pass
            text = "\n".join(text_parts)
        else:
            continue

        if not text.strip():
            continue

        # Truncate very long messages
        if len(text) > 800:
            text = text[:400] + "\n...[truncated]...\n" + text[-400:]

        line = f"[{role}] {text}\n"
        if total + len(line) > max_chars:
            # Add truncation notice and stop
            parts.append(f"\n[...truncated, {len(messages) - len(parts)} more messages...]")
            break
        parts.append(line)
        total += len(line)

    return "\n".join(parts)


def _extract_via_llm(messages: list) -> "list[dict] | None":
    """
    Call Haiku API to extract structured memories.
    Returns list of {category, content, importance} or None on failure.
    """
    # Try: env var > Claude OAuth token
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        creds_path = Path.home() / ".claude" / ".credentials.json"
        if creds_path.exists():
            try:
                creds = json.loads(creds_path.read_text(encoding="utf-8"))
                token = creds.get("claudeAiOauth", {}).get("accessToken", "")
                if token and token.startswith("sk-ant-"):
                    api_key = token
            except Exception:
                pass
    if not api_key:
        print("[cc-memory] no API key (env or OAuth), skipping LLM extraction", file=sys.stderr)
        return None

    transcript_text = _build_transcript_summary(messages)
    if len(transcript_text) < 100:
        return None

    body = json.dumps({
        "model": _HAIKU_MODEL,
        "max_tokens": 2000,
        "messages": [
            {
                "role": "user",
                "content": f"Extract memories from this conversation:\n\n{transcript_text}",
            }
        ],
        "system": _EXTRACTION_PROMPT,
    }).encode("utf-8")

    req = urllib.request.Request(
        _API_URL,
        data=body,
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

        # Parse response
        text_content = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text_content += block.get("text", "")

        # Extract JSON array from response
        text_content = text_content.strip()
        # Handle possible markdown wrapping
        if text_content.startswith("```"):
            lines = text_content.split("\n")
            text_content = "\n".join(
                l for l in lines if not l.strip().startswith("```")
            )

        memories = json.loads(text_content)
        if not isinstance(memories, list):
            return None

        # Validate and clean
        valid = []
        for m in memories:
            if not isinstance(m, dict):
                continue
            cat = m.get("category", "note")
            content = m.get("content", "").strip()
            imp = m.get("importance", 3)
            if not content or len(content) < 10:
                continue
            if cat not in ("decision", "result", "config", "bug", "task", "arch", "note"):
                cat = "note"
            imp = max(1, min(int(imp), 5))
            valid.append({"category": cat, "content": content, "importance": imp})

        print(f"[cc-memory] LLM extracted {len(valid)} memories", file=sys.stderr)
        return valid if valid else None

    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, OSError, KeyError, ValueError) as e:
        print(f"[cc-memory] LLM extraction failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Regex fallback extraction
# ---------------------------------------------------------------------------
def _extract_via_regex(ext: dict) -> "list[dict]":
    """Fallback: use regex heuristics from extractor.py."""
    cat_base_imp = {
        "decision": 3, "result": 3, "arch": 3,
        "config": 2, "bug": 4, "task": 2, "note": 1,
    }
    results = []

    grouped = group_sentences(ext["sentences"])
    for cat, items in grouped.items():
        base = cat_base_imp.get(cat, 2)
        for text, imp in items[:10]:
            results.append({
                "category": cat,
                "content": text,
                "importance": min(max(imp, base), 5),
            })

    # Metrics
    for metric in ext["metrics"][:10]:
        results.append({"category": "result", "content": metric, "importance": 3})

    # Todos
    for t in ext["todos"][:20]:
        content = f"[{t['status']}] {t['content']}"
        imp = 3 if t["priority"] == "high" else 2
        results.append({"category": "task", "content": content, "importance": imp})

    return results


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
    """SESSION_HANDOFF.md — overwritten each time, for SessionStart injection."""
    lines = [
        f"# Session Handoff — {project_name}",
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
    """MEMORY.md — auto-generated index."""
    stats = db.get_stats(project_id)
    topics = db.get_topics(project_id)
    top_kw = db.get_top_keywords(project_id, 25)
    critical = db.get_critical_memories(project_id, min_importance=5)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Memory Index  *(auto-generated by cc-memory)*",
        f"*Updated: {now_str}*  "
        f"|  Sessions: {stats['n_sessions']}  "
        f"|  Memories: {stats['n_memories']}",
        "",
    ]

    if critical:
        lines += ["## Critical (Never Forget)", ""]
        for m in critical[:8]:
            lines.append(f"- **[{m['category']}]** {m['content']}")
        lines.append("")

    if stats["by_category"]:
        lines += ["## Memory by Category", ""]
        for row in stats["by_category"]:
            avg = f"{row['avg_imp']:.1f}"
            lines.append(f"- `{row['category']}`: {row['n']} entries  (avg importance {avg})")
        lines.append("")

    if topics:
        lines += ["## Topic Files", ""]
        for t in topics:
            lines.append(
                f"- `memory/topics/{t['name']}.md`  "
                f"(v{t['version']}, updated {t['updated_at'][:10]})"
            )
        lines.append("")

    if top_kw:
        lines += ["## Project Vocabulary (top keywords)", ""]
        lines.append(", ".join(f"`{kw}`" for kw in top_kw))
        lines.append("")

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
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        print(f"[cc-memory] pre_compact: stdin parse error: {exc}", file=sys.stderr)
        sys.exit(0)

    cwd = data.get("cwd", "")
    transcript_path = data.get("transcript_path", "")
    trigger = data.get("trigger", "auto")
    claude_sid = data.get("session_id", "")

    if not cwd or not transcript_path:
        print("[cc-memory] pre_compact: missing cwd or transcript_path", file=sys.stderr)
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
            print("[cc-memory] pre_compact: empty transcript, skipping", file=sys.stderr)
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
        # 1. Session record
        session_id = db.insert_session(
            project_id=project_id,
            claude_session_id=claude_sid,
            trigger_type=trigger,
            msg_count=ext["msg_count"],
            archive_path=str(archive_path.relative_to(memory_dir)),
            brief_summary=archive_text[:1000],
        )

        # 2. Load existing memories for deduplication
        existing_content = set()
        with db._connect() as conn:
            for row in conn.execute(
                "SELECT content FROM memories WHERE project_id = ? AND is_active = 1",
                (project_id,),
            ):
                existing_content.add(row["content"].strip().lower())

        def _is_dup(text):
            return text.strip().lower() in existing_content

        # 3. Extract memories via LLM (regex disabled — produces too much garbage)
        extracted = _extract_via_llm(messages)
        method = "llm" if extracted else "none"
        if not extracted:
            extracted = []
            print("[cc-memory] no API key or LLM failed; skipping memory extraction (archive/handoff still saved)",
                  file=sys.stderr)

        # 4. Save extracted memories (with dedup)
        n_saved = 0
        for m in extracted:
            content = m["content"]
            if _is_dup(content):
                continue
            db.insert_memory(
                project_id, session_id, m["category"], content,
                importance=min(max(m["importance"], 1), 5),
                tags=[method, "auto"],
            )
            existing_content.add(content.strip().lower())
            n_saved += 1

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
            f"{n_saved} new memories via {method}, "
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
