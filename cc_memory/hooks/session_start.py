#!/usr/bin/env python3
"""
SessionStart hook — forced-handoff injection point.

Fires on every new session (startup, resume, post-compaction). Three jobs:

  1. INJECT layered context (topics, critical memories, recent timeline,
     handoff summary, footer).

  2. EMIT A FORCED <system-reminder> directing Claude to Read PROGRESS.md
     and MEMORY.md BEFORE responding. This is the hook-level enforcement
     of the handoff contract (see docs/HANDOFF_PROTOCOL.md).

  3. Best-effort RETROACTIVE SAVE — if previous JSONL transcripts were
     never compacted, extract memories from them now via Haiku.

Stdout: injected context (Claude reads it as additional system input).
Stderr: suppressed (file log only).
"""
import json
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

from core.db import MemoryDB
from core.extractor import load_transcript
from core.logger import get_logger
from llm.memory_writer import upsert_batch

_log = get_logger("session_start")


# ── Layered context injection ──────────────────────────────────────────────
_DEFAULT_BUDGET = 16000  # ~4000 tokens at 4 chars/token

_LAYER_BUDGETS = {
    "topics":   0.30,
    "critical": 0.15,
    "timeline": 0.20,
    "progress": 0.25,  # PROGRESS.md preview gets a larger share now
    "footer":   0.10,
}


def _build_topics_layer(db, project_id, budget):
    topics = db.get_topics(project_id)
    if not topics:
        return "", set()
    lines = ["### Knowledge Base (by topic)", ""]
    used = 0
    topic_names = set()
    for t in topics:
        summary = t["content"]
        max_len = min(250, (budget - used) // max(len(topics), 1))
        if len(summary) > max_len:
            cut = summary[:max_len].rfind(".")
            summary = summary[:cut+1] if cut > 50 else summary[:max_len-3] + "..."
        entry = f"**[{t['name']}]** {summary}\n"
        if used + len(entry) > budget:
            break
        lines.append(entry)
        used += len(entry)
        topic_names.add(t["name"])
    return "\n".join(lines), topic_names


def _build_critical_layer(db, project_id, budget, topic_names):
    critical = db.get_critical_memories(project_id, min_importance=5)
    unmerged = [
        m for m in critical
        if not m.get("topic") or m.get("topic") not in topic_names
    ]
    if not unmerged:
        return "", set()
    lines = ["### Critical (unmerged)", ""]
    used = 0
    shown = set()
    for m in unmerged[:8]:
        entry = f"- [{m['category']}] {m['content']}"
        if used + len(entry) > budget:
            break
        lines.append(entry)
        used += len(entry)
        shown.add(m["id"])
    lines.append("")
    return "\n".join(lines), shown


def _build_timeline_layer(db, project_id, budget, shown_ids, mode_name="code"):
    from core.modes import get_injection_priority
    priority = get_injection_priority(mode_name)
    recent = db.get_recent_memories(project_id, sessions_back=3, min_importance=3, limit=20)
    fresh = [m for m in recent if m["id"] not in shown_ids]
    if not fresh:
        return ""
    cat_rank = {cat: i for i, cat in enumerate(priority)}
    fresh.sort(key=lambda m: (cat_rank.get(m["category"], 99), -m["importance"]))
    lines = ["### Recent", ""]
    used = 0
    for i, m in enumerate(fresh):
        if i < 5:
            prefix = "! " if m["importance"] >= 4 else "- "
            entry = f"{prefix}[{m['category']}] {m['content']}"
        else:
            short = m["content"][:60] + "..." if len(m["content"]) > 60 else m["content"]
            entry = f"#{m['id']} {m['category']}: {short}"
        if used + len(entry) > budget:
            break
        lines.append(entry)
        used += len(entry)
    lines.append("")
    return "\n".join(lines)


def _build_progress_preview(memory_dir, budget):
    """Render a compact preview of PROGRESS.md.

    The FORCED reminder block below asks Claude to read the full file, but we
    also embed a preview here so the model has the highlights even if it
    skips the Read (defense in depth).
    """
    progress = memory_dir / "PROGRESS.md"
    if not progress.exists():
        return ""
    try:
        text = progress.read_text(encoding="utf-8")
    except OSError:
        # why: read failure shouldn't break SessionStart; fall through to empty
        return ""
    # Trim to budget
    if len(text) > budget:
        text = text[:budget].rsplit("\n", 1)[0] + "\n…[truncated, read memory/PROGRESS.md]"
    return "### Last Session PROGRESS (preview)\n\n" + text + "\n"


def _build_footer(db, project_id, memory_dir):
    lines = []
    last_save = memory_dir / ".last_save.json"
    if last_save.exists():
        try:
            info = json.loads(last_save.read_text(encoding="utf-8"))
            ts = info.get("timestamp", "?")
            if info.get("success"):
                method = info.get("method", "?")
                ni = info.get("n_inserted", 0)
                nm = info.get("n_merged", 0)
                ns = info.get("n_superseded", 0)
                lines.append(
                    f"[Last save: {ts} | +{ni}/~{nm}/↻{ns} via {method}]"
                )
            else:
                lines.append(f"[Last save FAILED at {ts}]")
        except (json.JSONDecodeError, OSError):
            # why: malformed status file shouldn't block injection;
            # the next PreCompact will overwrite it
            pass
    try:
        from core.auth import get_api_key
        _key, source = get_api_key()
        if source == "oauth_expired":
            lines.append("[WARNING: OAuth expired — LLM extraction disabled]")
        elif not _key:
            lines.append("[WARNING: No API key — LLM extraction disabled]")
    except Exception:
        # why: auth check is purely informational here; never block startup
        pass

    stats = db.get_stats(project_id)
    n_obs = db.get_observation_count(project_id)
    lines.append(
        f"[{stats['n_sessions']} sessions, {stats['n_memories']} memories, "
        f"{stats.get('n_topics', 0)} topics, {n_obs} observations]"
    )
    lines += ["", "=== END CC-MEMORY ===", ""]
    return "\n".join(lines)


def _build_forced_reminder(memory_dir):
    """Emit a <system-reminder> that FORCES the next response to Read PROGRESS.md.

    This is the core of the v2.1 forced-handoff mechanism. Soft reminders
    were unreliable (cf. v2.0 SESSION_HANDOFF.md drift). The system-reminder
    block is honored as authoritative context by Claude.
    """
    progress = memory_dir / "PROGRESS.md"
    memory_md = memory_dir / "MEMORY.md"
    has_progress = progress.exists()
    has_memory = memory_md.exists()
    if not (has_progress or has_memory):
        return ""

    lines = [
        "",
        "<system-reminder>",
        "CC-MEMORY HANDOFF — MANDATORY READ-FIRST PROTOCOL",
        "",
        "Before responding to any user request in this session, you MUST:",
    ]
    n = 1
    if has_progress:
        lines.append(f"  {n}. Use the Read tool on `memory/PROGRESS.md` "
                     f"(absolute: `{progress.as_posix()}`).")
        n += 1
    if has_memory:
        lines.append(f"  {n}. Use the Read tool on `memory/MEMORY.md` "
                     f"(absolute: `{memory_md.as_posix()}`).")
        n += 1
    lines += [
        "",
        "After reading, explicitly state in your first reply:",
        '  "Read PROGRESS.md — prior progress: <one-sentence summary>."',
        "",
        "Why: this is the project's handoff contract (single source of truth).",
        "Skipping it risks duplicating work or contradicting prior decisions.",
        "Spec: `docs/HANDOFF_PROTOCOL.md`.",
        "</system-reminder>",
        "",
    ]
    return "\n".join(lines)


def build_context(memory_dir, db, project_id, project_name):
    total_budget = _DEFAULT_BUDGET
    mode_name = db.get_project_mode(project_id)

    header = (
        f"=== CC-MEMORY: Context Restored ===\n"
        f"Project: {project_name}  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    )
    parts = [header]

    budget = int(total_budget * _LAYER_BUDGETS["topics"])
    topics_text, topic_names = _build_topics_layer(db, project_id, budget)
    if topics_text:
        parts.append(topics_text)

    budget = int(total_budget * _LAYER_BUDGETS["critical"])
    critical_text, shown_ids = _build_critical_layer(db, project_id, budget, topic_names)
    if critical_text:
        parts.append(critical_text)

    budget = int(total_budget * _LAYER_BUDGETS["timeline"])
    timeline_text = _build_timeline_layer(db, project_id, budget, shown_ids, mode_name)
    if timeline_text:
        parts.append(timeline_text)

    budget = int(total_budget * _LAYER_BUDGETS["progress"])
    progress_text = _build_progress_preview(memory_dir, budget)
    if progress_text:
        parts.append(progress_text)

    footer = _build_footer(db, project_id, memory_dir)
    parts.append(footer)

    # The forced reminder block goes LAST so it's the freshest context.
    parts.append(_build_forced_reminder(memory_dir))

    result = "\n".join(parts)
    _log.info(f"injected ~{len(result)//4} tokens ({len(result)} chars)")
    return result


# ── Retroactive save from prior session JSONL ──────────────────────────────
_API_TIMEOUT = 20

_RETROACTIVE_PROMPT = """\
You are a memory extraction system. Given a Claude Code conversation transcript, \
extract the most important information worth remembering across sessions.

Output a JSON array of objects: {"category": str, "content": str, "importance": int, "topic": str}
- category: decision|result|config|bug|task|arch|note
- content: one concise, self-contained sentence with specific values
- importance: 1-5 (5=critical, 4=important, 3=useful)
- topic: a short keyword for the topic

Rules: Only conclusions, not process. Self-contained. Specific values. 5-15 items max.
Output ONLY valid JSON array."""


def _find_transcript_dir(project_path):
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


def _get_saved_session_ids(db, project_id):
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT claude_session_id FROM sessions WHERE project_id = ?",
            (project_id,)
        ).fetchall()
    return {r["claude_session_id"] for r in rows if r["claude_session_id"]}


def _summarize_transcript(messages, max_chars=12000):
    parts, total = [], 0
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


def _retroactive_extract(messages):
    from core.auth import get_api_key
    api_key, _ = get_api_key()
    if not api_key:
        return None
    transcript_text = _summarize_transcript(messages)
    if len(transcript_text) < 100:
        return None
    try:
        from llm.ccl_backend import call_llm
        text = call_llm(_RETROACTIVE_PROMPT,
                        f"Extract memories:\n\n{transcript_text}",
                        api_key, max_tokens=2000, timeout=_API_TIMEOUT)
        text = text.strip()
        if text.startswith("```"):
            text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
        memories = json.loads(text)
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
        # why: retroactive save is best-effort; any LLM/JSON failure
        # should be silent — the rest of the hook still works
        return None


def retroactive_save(cwd, db, project_id, current_session_id=""):
    transcript_dir = _find_transcript_dir(cwd)
    if not transcript_dir:
        return
    saved_ids = _get_saved_session_ids(db, project_id)
    jsonls = sorted(transcript_dir.glob("*.jsonl"),
                    key=lambda f: f.stat().st_mtime, reverse=True)

    memory_dir = Path(cwd) / "memory"
    n_retroactive = 0
    for jsonl in jsonls[:3]:
        session_uuid = jsonl.stem
        if session_uuid == current_session_id:
            continue
        if session_uuid in saved_ids:
            continue
        if jsonl.stat().st_size < 1024:
            continue
        try:
            messages = load_transcript(str(jsonl))
            if not messages or len(messages) < 5:
                continue
            memories = _retroactive_extract(messages)
            if not memories:
                continue

            sid = db.insert_session(
                project_id=project_id,
                claude_session_id=session_uuid,
                trigger_type="retroactive_llm",
                msg_count=len(messages),
                archive_path="",
                brief_summary=f"Retroactive save at {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            )
            counts = upsert_batch(db, project_id, sid, memories, memory_dir=memory_dir)
            n_retroactive += 1
            _log.info(
                f"retroactive {session_uuid[:8]}: +{counts.get('inserted',0)} "
                f"~{counts.get('merged',0)} ↻{counts.get('superseded',0)}"
            )
        except Exception as e:
            _log.error(f"retroactive save error: {e}")


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception as e:
        _log.error(f"session_start stdin error: {e}")
        sys.exit(0)

    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    if not cwd:
        sys.exit(0)

    try:
        memory_dir = Path(cwd) / "memory"
        db_path = memory_dir / "memory.db"
        if not db_path.exists():
            _log.info(f"no DB for {cwd}")
            sys.exit(0)

        db = MemoryDB(db_path)
        project_id = db.upsert_project(cwd)

        print(f"\n[cc-memory] Session start — loading memory for '{Path(cwd).name}'...")
        print(build_context(memory_dir, db, project_id, Path(cwd).name))
        stats = db.get_stats(project_id)
        print(
            f"[cc-memory OK] Context loaded: "
            f"{stats['n_memories']} memories, {stats.get('n_topics', 0)} topics"
        )
        _log.info(f"injected context for {Path(cwd).name}")

        try:
            retroactive_save(cwd, db, project_id, session_id)
        except Exception as e:
            _log.error(f"retroactive save failed: {e}")

    except Exception:
        _log.error_tb("session_start ERROR")
    sys.exit(0)


if __name__ == "__main__":
    main()
