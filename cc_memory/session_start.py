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
from logger import get_logger

_log = get_logger("session_start")


# ---------------------------------------------------------------------------
# Progressive Disclosure Context Injection
# ---------------------------------------------------------------------------
# Token budget: ~4000 tokens = ~16000 chars (4 chars/token estimate)
_DEFAULT_BUDGET = 16000
_CHARS_PER_TOKEN = 4

# Layer budget fractions (unused budget flows to next layer)
_LAYER_BUDGETS = {
    "topics":   0.35,
    "critical": 0.15,
    "timeline": 0.25,
    "handoff":  0.15,
    "footer":   0.10,
}


def _build_topics_layer(db, project_id, budget):
    """Layer 1: Topic summaries (always injected, most important)."""
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
    """Layer 2: Unmerged critical memories (imp>=5, not in topics)."""
    critical = db.get_critical_memories(project_id, min_importance=5)
    unmerged = [
        m for m in critical
        if not m.get("topic") or m.get("topic") not in topic_names
    ]
    if not unmerged:
        return "", set()
    lines = ["### Critical (unmerged)", ""]
    used = 0
    shown_ids = set()
    for m in unmerged[:8]:
        entry = f"- [{m['category']}] {m['content']}"
        if used + len(entry) > budget:
            break
        lines.append(entry)
        used += len(entry)
        shown_ids.add(m["id"])
    lines.append("")
    return "\n".join(lines), shown_ids


def _build_timeline_layer(db, project_id, budget, shown_ids, mode_name="code"):
    """Layer 3: Unified chronological timeline (recent=full, older=compact)."""
    from modes import get_injection_priority
    priority = get_injection_priority(mode_name)

    recent = db.get_recent_memories(
        project_id, sessions_back=3, min_importance=3, limit=20
    )
    fresh = [m for m in recent if m["id"] not in shown_ids]
    if not fresh:
        return ""

    # Sort by priority order, then importance
    cat_rank = {cat: i for i, cat in enumerate(priority)}
    fresh.sort(key=lambda m: (cat_rank.get(m["category"], 99), -m["importance"]))

    lines = ["### Recent", ""]
    used = 0
    # First 5: full content. Rest: compact one-liners
    for i, m in enumerate(fresh):
        if i < 5:
            prefix = "! " if m["importance"] >= 4 else "- "
            entry = f"{prefix}[{m['category']}] {m['content']}"
        else:
            content_short = m["content"][:60] + "..." if len(m["content"]) > 60 else m["content"]
            entry = f"#{m['id']} {m['category']}: {content_short}"
        if used + len(entry) > budget:
            break
        lines.append(entry)
        used += len(entry)
    lines.append("")
    return "\n".join(lines)


def _build_handoff_layer(db, project_id, memory_dir, budget):
    """Layer 4: Structured session summary or handoff."""
    # Try structured summary first
    summary = db.get_latest_summary(project_id)
    if summary:
        lines = ["### Last Session"]
        if summary.get("request"):
            lines.append(f"**Request**: {summary['request'][:200]}")
        if summary.get("completed"):
            lines.append(f"**Completed**: {summary['completed'][:200]}")
        if summary.get("next_steps"):
            lines.append(f"**Next**: {summary['next_steps'][:200]}")
        if summary.get("learned"):
            lines.append(f"**Learned**: {summary['learned'][:200]}")
        lines.append("")
        text = "\n".join(lines)
        return text[:budget]

    # Fallback: SESSION_HANDOFF.md
    handoff = memory_dir / "SESSION_HANDOFF.md"
    if handoff.exists():
        text = handoff.read_text(encoding="utf-8").strip()
        body_lines = text.splitlines()
        body = "\n".join(body_lines[2:]) if len(body_lines) > 2 else text
        if body.strip():
            section = "### Last Session State\n\n" + body
            return section[:budget]
    return ""


def _build_footer(db, project_id, memory_dir):
    """Layer 5: Stats, warnings, token economics."""
    lines = []

    # Last save status
    last_save = memory_dir / ".last_save.json"
    if last_save.exists():
        try:
            save_info = json.loads(last_save.read_text(encoding="utf-8"))
            ts = save_info.get("timestamp", "?")
            if save_info.get("success"):
                method = save_info.get("method", "?")
                n = save_info.get("n_saved", 0)
                n_obs = save_info.get("n_observations", 0)
                lines.append(f"[Last save: {ts} | {n} memories, {n_obs} observations via {method}]")
            else:
                lines.append(f"[Last save FAILED at {ts}]")
        except Exception:
            pass

    # API key warnings
    try:
        from auth import get_api_key
        _key, source = get_api_key()
        if source == "oauth_expired":
            lines.append("[WARNING: OAuth expired -- LLM extraction disabled]")
        elif not _key:
            lines.append("[WARNING: No API key -- LLM extraction disabled]")
    except Exception:
        pass

    # Stats
    stats = db.get_stats(project_id)
    n_obs = db.get_observation_count(project_id)
    lines.append(
        f"[{stats['n_sessions']} sessions, {stats['n_memories']} memories, "
        f"{stats.get('n_topics', 0)} topics, {n_obs} observations]"
    )
    lines += [
        "",
        "REMINDER: Before ending this conversation, call /save-memories to preserve important information.",
        "=== END CC-MEMORY ===",
        "",
    ]
    return "\n".join(lines)


def build_context(memory_dir, db, project_id, project_name):
    """Progressive disclosure context injection with token budget."""
    total_budget = _DEFAULT_BUDGET
    mode_name = db.get_project_mode(project_id)

    # Header (fixed cost)
    header = (
        f"=== CC-MEMORY: Context Restored ===\n"
        f"Project: {project_name}  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    )
    remaining = total_budget - len(header)
    parts = [header]

    # Layer 1: Topics
    budget = int(total_budget * _LAYER_BUDGETS["topics"])
    topics_text, topic_names = _build_topics_layer(db, project_id, budget)
    if topics_text:
        parts.append(topics_text)
        remaining -= len(topics_text)

    # Layer 2: Critical unmerged
    budget = int(total_budget * _LAYER_BUDGETS["critical"])
    critical_text, shown_ids = _build_critical_layer(db, project_id, budget, topic_names)
    if critical_text:
        parts.append(critical_text)
        remaining -= len(critical_text)

    # Layer 3: Timeline
    budget = int(total_budget * _LAYER_BUDGETS["timeline"])
    timeline_text = _build_timeline_layer(db, project_id, budget, shown_ids, mode_name)
    if timeline_text:
        parts.append(timeline_text)
        remaining -= len(timeline_text)

    # Layer 4: Handoff/summary
    budget = min(int(total_budget * _LAYER_BUDGETS["handoff"]), max(remaining - 500, 0))
    handoff_text = _build_handoff_layer(db, project_id, memory_dir, budget)
    if handoff_text:
        parts.append(handoff_text)
        remaining -= len(handoff_text)

    # Layer 5: Footer (always included)
    footer = _build_footer(db, project_id, memory_dir)
    parts.append(footer)

    result = "\n".join(parts)

    # Token economics log
    total_tokens = len(result) // _CHARS_PER_TOKEN
    _log.info(f"injected ~{total_tokens} tokens of context ({len(result)} chars)")

    return result


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

    try:
        from ccl_backend import call_llm
        text_content = call_llm(
            _EXTRACTION_PROMPT,
            f"Extract memories:\n\n{transcript_text}",
            api_key, max_tokens=2000, timeout=_API_TIMEOUT,
        )
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

        _log.info(f"retroactive save for {session_uuid[:8]}...")
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
            _log.info(f"retroactive: {n_saved} memories from {session_uuid[:8]} via {method}")

        except Exception as e:
            _log.error(f"retroactive save error: {e}")

    if n_retroactive:
        from pre_compact import _fmt_memory_index
        memory_dir = Path(cwd) / "memory"
        index_text = _fmt_memory_index(db, project_id, memory_dir)
        (memory_dir / "MEMORY.md").write_text(index_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# CCL upstream update check
# ---------------------------------------------------------------------------
def _ccl_update_check(cwd: str):
    """
    Check if tracked CCL source repos have new upstream commits.
    Only runs when cwd is under the CCL root, once per session.
    """
    import subprocess
    import tempfile

    ccl_root = Path("D:/Projects/Claude-Code-Local")
    try:
        if not Path(cwd).resolve().is_relative_to(ccl_root.resolve()):
            return
    except Exception:
        return

    # Once per session marker
    marker = Path(tempfile.gettempdir()) / "ccl_update_checked"
    if marker.exists():
        return

    sources_cfg = ccl_root / ".ccl" / "sources.json"
    if not sources_cfg.exists():
        return

    cfg = json.loads(sources_cfg.read_text(encoding="utf-8"))
    if not cfg.get("update_check", {}).get("enabled", True):
        return

    updates = []
    errors = []
    for src in cfg.get("sources", []):
        if not src.get("track", False):
            continue
        name = src.get("name", "?")
        local = Path(src.get("local", ""))
        url = src.get("url", "")
        branch = src.get("branch", "HEAD")
        if not local.exists() or not url:
            continue
        try:
            local_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(local), stderr=subprocess.DEVNULL, timeout=3,
            ).decode().strip()

            remote_out = subprocess.check_output(
                ["git", "ls-remote", url, f"refs/heads/{branch}"],
                stderr=subprocess.DEVNULL, timeout=10,
            ).decode().strip()

            if not remote_out:
                continue
            remote_head = remote_out.split()[0]

            if local_head != remote_head:
                updates.append(name)
        except Exception as e:
            errors.append(name)

    # Write marker regardless of result
    try:
        marker.touch()
    except Exception:
        pass

    if updates:
        print(
            f"\n[CCL] Update check: new commits available in: {', '.join(updates)}\n"
            f"[CCL] Run /ccl-update to fetch, or: git -C <path> fetch && git merge"
        )
    elif not errors:
        _log.info(f"ccl update check: all tracked repos up to date")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
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

        # Job 1: Inject context (MUST happen first, fast)
        print(f"\n[cc-memory] Session start — loading memory for '{Path(cwd).name}'...")
        print(build_context(memory_dir, db, project_id, Path(cwd).name))
        _stats = db.get_stats(project_id)
        print(
            f"[cc-memory OK] Context loaded: "
            f"{_stats['n_memories']} memories, "
            f"{_stats.get('n_topics', 0)} topics"
        )
        _log.info(f"injected context for {Path(cwd).name}")

        # Job 2: Retroactive save (best effort)
        try:
            retroactive_save(cwd, db, project_id, session_id)
        except Exception as e:
            _log.error(f"retroactive save failed: {e}")

        # Job 3: CCL upstream update check (only for CCL project, once per session)
        try:
            _ccl_update_check(cwd)
        except Exception as e:
            _log.error(f"ccl update check failed: {e}")

    except Exception:
        _log.error_tb("session_start ERROR")
    sys.exit(0)


if __name__ == "__main__":
    main()
