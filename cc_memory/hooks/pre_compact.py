#!/usr/bin/env python3
"""
PreCompact hook.

Triggered by Claude Code BEFORE context compaction. Three jobs:

  1. Extract memories from the full transcript via Haiku LLM,
     routing every save through llm.memory_writer.upsert_smart so
     reconciliation (merge / supersede / insert) happens at write time.

  2. FULL-REWRITE memory/PROGRESS.md from the `progress` SQLite table.
     This is the handoff contract for the next session.

  3. Optionally run consolidation (every CONSOLIDATION_INTERVAL sessions)
     for LLM-based topic summarization that's too expensive for Stop hook.

Stdin (JSON):
  session_id, transcript_path, cwd, trigger ("auto"|"manual")

Output:
  stderr suppressed.
  stdout: ONE compact status line (visible in next session's context).
  Always exits 0 — must never block compaction.
"""
import json
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent  # cc_memory/
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio BEFORE anything prints; Windows gbk crashes on ↻ etc.
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

from core.db import MemoryDB
from core.extractor import build_extraction, load_transcript, group_sentences, CATEGORY_ORDER, CATEGORY_LABELS
from core.logger import get_logger
from core.progress import write_progress_md, write_session_archive, collect_progress_state, migrate_legacy_handoff
from llm.memory_writer import upsert_batch, regenerate_memory_index

_log = get_logger("pre_compact")

CONSOLIDATION_INTERVAL = 5  # every N sessions, run full LLM consolidation
_API_TIMEOUT = 25

_EXTRACTION_PROMPT = """\
You are a memory extraction system. Given a Claude Code conversation transcript, \
extract the most important information worth remembering across sessions.

Output a JSON array of objects with these fields:
- "category": one of "decision", "result", "config", "bug", "task", "arch", "note"
- "content": one concise, self-contained sentence with specific values (numbers, file names, parameters)
- "importance": 1-5 (5=critical/never-forget, 4=important, 3=useful, 2=minor, 1=trivial)
- "topic": a short lowercase keyword for grouping (e.g. "auth", "pipeline", "config", "ui")

Rules:
- Only save CONCLUSIONS, not discussion process or debugging steps
- Each memory must be understandable WITHOUT context
- Include specific values: "lr=3e-4 chosen over 1e-3 because val_loss flatlined" not "tuned lr"
- Skip: conversation logistics, tool errors, meta-discussion, trivial Q&A
- Output 5-15 memories maximum. Quality over quantity.
- Do NOT include memories about the memory plugin itself unless it's a critical bug fix

Output ONLY a valid JSON array, no markdown, no explanation."""


def _build_transcript_summary(messages, max_chars=12000):
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
                        elif name == "TodoWrite":
                            text_parts.append(
                                f"[TodoWrite: {json.dumps(inp.get('todos', [])[:5], ensure_ascii=False)[:200]}]"
                            )
                        else:
                            text_parts.append(f"[Tool: {name}]")
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


def _extract_via_llm(messages, observations=None):
    from core.auth import get_api_key
    api_key, source = get_api_key()
    if not api_key:
        reason = "OAuth token expired" if source == "oauth_expired" else "no API key found"
        _log.info(f"{reason}, skipping LLM extraction")
        return None

    transcript_text = _build_transcript_summary(messages)
    if len(transcript_text) < 100:
        return None

    obs_context = ""
    if observations:
        obs_lines = [f"[{o['tool_name']}] {o['tool_input']}" for o in observations[-50:]]
        if obs_lines:
            obs_context = "\n\nTool observations (for context):\n" + "\n".join(obs_lines)

    user_content = f"Extract memories from this conversation:\n\n{transcript_text}{obs_context}"

    try:
        from llm.ccl_backend import call_llm
        text = call_llm(_EXTRACTION_PROMPT, user_content, api_key,
                        max_tokens=2500, timeout=_API_TIMEOUT)
        text = text.strip()
        if text.startswith("```"):
            text = "\n".join(
                l for l in text.split("\n") if not l.strip().startswith("```")
            )
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
            imp = max(1, min(int(imp), 5))
            valid.append({
                "category": cat, "content": content, "importance": imp,
                "topic": topic if isinstance(topic, str) else "",
            })
        _log.info(f"LLM extracted {len(valid)} memories")
        return valid if valid else None

    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, OSError, KeyError, ValueError) as e:
        _log.error(f"LLM extraction failed: {e}")
        return None


def _fmt_archive(ext, timestamp, trigger, project_name):
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


def _first_user_request(messages):
    for msg in messages[:5]:
        message = msg.get("message", {})
        if isinstance(message, dict) and message.get("role") == "user":
            content_field = message.get("content", "")
            if isinstance(content_field, str):
                return content_field[:500]
            if isinstance(content_field, list):
                for block in content_field:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block.get("text", "")[:500]
    return ""


def _maybe_consolidate(cwd, db, project_id):
    n_sessions = db.get_session_count(project_id)
    if n_sessions % CONSOLIDATION_INTERVAL != 0:
        return
    _log.info(f"auto-consolidation triggered (session #{n_sessions})")
    try:
        from core.consolidate import run_consolidation
        run_consolidation(cwd, use_llm=True, verbose=True)
    except Exception as e:
        _log.error(f"auto-consolidation error: {e}")


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
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
        memory_dir = Path(cwd) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "sessions").mkdir(exist_ok=True)
        (memory_dir / "topics").mkdir(exist_ok=True)

        # Migrate old SESSION_HANDOFF.md aside (one-shot)
        migrate_legacy_handoff(memory_dir)

        db = MemoryDB(memory_dir / "memory.db")
        project_id = db.upsert_project(cwd)
        project_name = Path(cwd).name

        messages = load_transcript(transcript_path)
        if not messages:
            _log.info("empty transcript, skipping")
            sys.exit(0)

        project_kw = db.get_top_keywords(project_id, 40)
        ext = build_extraction(messages, project_kw)

        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        file_ts = now.strftime("%Y%m%d_%H%M%S")

        # Session archive
        archive_text = _fmt_archive(ext, timestamp, trigger, project_name)
        archive_path = write_session_archive(memory_dir, project_name, archive_text, file_ts)

        # Session row
        session_id = db.insert_session(
            project_id=project_id,
            claude_session_id=claude_sid,
            trigger_type=trigger,
            msg_count=ext["msg_count"],
            archive_path=str(archive_path.relative_to(memory_dir)),
            brief_summary=archive_text[:1000],
        )

        # Observations from this session (for LLM context)
        last_session_ids = db.get_recent_session_ids(project_id, 2)
        last_ts = ""
        if len(last_session_ids) >= 2:
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT compacted_at FROM sessions WHERE id = ?",
                    (last_session_ids[1],),
                ).fetchone()
                if row:
                    last_ts = row["compacted_at"]
        observations = db.get_observations_since(project_id, last_ts) if last_ts else []

        # LLM extraction → upsert through memory_writer (anti-patch path)
        extracted = _extract_via_llm(messages, observations) or []
        method = "llm" if extracted else "none"

        counts = upsert_batch(db, project_id, session_id, extracted, memory_dir=memory_dir)
        n_inserted = counts.get("inserted", 0)
        n_merged   = counts.get("merged", 0)
        n_super    = counts.get("superseded", 0)
        n_skipped  = counts.get("skipped", 0)

        # Update keyword vocabulary
        if ext["keywords"]:
            db.upsert_keywords(project_id, ext["keywords"])

        # Session summary (structured)
        obs_files_read = list(dict.fromkeys(
            o["tool_input"] for o in observations
            if o["tool_name"] == "Read" and o["tool_input"]
        ))
        obs_files_modified = list(dict.fromkeys(
            o["tool_input"] for o in observations
            if o["tool_name"] in ("Edit", "Write", "MultiEdit") and o["tool_input"]
        ))
        first_user_msg = _first_user_request(messages)
        task_mems = [m["content"] for m in extracted if m.get("category") == "task"]
        # Pull the CURRENT todo snapshot from the transcript (last TodoWrite
        # tool_use). This is the live state — much more reliable than LLM-
        # extracted "task" memories or regex aggregation. Used as the primary
        # source for both PROGRESS.md.open_todos and session_summary.next_steps.
        latest_todos = ext.get("latest_todos") or []
        pending_todos = [t["content"] for t in latest_todos
                         if t.get("status") != "completed"]
        next_steps_str = (
            "; ".join(pending_todos[:5]) if pending_todos
            else "; ".join(task_mems[:5])
        )

        try:
            db.insert_session_summary(session_id, project_id, {
                "request": first_user_msg,
                "investigated": ", ".join(obs_files_read[:10]),
                "learned": "",
                "completed": ", ".join(obs_files_modified[:10]),
                "next_steps": next_steps_str,
                "notes": "",
                "files_read": obs_files_read[:20],
                "files_modified": obs_files_modified[:20],
            })
        except Exception as e:
            _log.error(f"session summary save error: {e}")

        # === PROGRESS.md: FULL REWRITE from authoritative state ===========
        progress_state = collect_progress_state(
            db, project_id, memory_dir,
            current_request=first_user_msg,
            todos=latest_todos or ext.get("todos", []),
            files_read=obs_files_read,
            files_modified=obs_files_modified,
            transcript_ptr=str(Path(transcript_path).resolve()),
            trigger_type=trigger,
        )
        db.upsert_progress(project_id, **progress_state)
        progress_path = write_progress_md(db, project_id, memory_dir)

        # Clean up observations consumed by this extraction
        if observations:
            db.cleanup_observations(project_id, timestamp)

        # MEMORY.md was already regenerated by upsert_batch. Touch again
        # to make sure it reflects any non-batch state changes.
        regenerate_memory_index(db, project_id, memory_dir)

        # Save status (consumed by SessionStart for the footer warning)
        status = {
            "timestamp": timestamp,
            "method": method,
            "n_inserted": n_inserted,
            "n_merged": n_merged,
            "n_superseded": n_super,
            "n_skipped": n_skipped,
            "n_observations": len(observations),
            "msg_count": ext["msg_count"],
            "transcript_ptr": str(Path(transcript_path).resolve()),
            "success": True,
        }
        try:
            (memory_dir / ".last_save.json").write_text(
                json.dumps(status, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            # why: status file is purely cosmetic for the next SessionStart;
            # disk failure here must not propagate into the compact path
            _log.error(f".last_save.json write failed: {e}")

        # Maybe run LLM consolidation (expensive — gated)
        _maybe_consolidate(cwd, db, project_id)

        _log.info(
            f"pre_compact OK: ins={n_inserted} mrg={n_merged} sup={n_super} skp={n_skipped} "
            f"obs={len(observations)} archive={archive_path.name}"
        )
        # One-line visible status for the next session
        print(
            f"[cc-memory] Pre-compact: "
            f"+{n_inserted} new, ~{n_merged} merged, ↻{n_super} superseded, "
            f"={n_skipped} skipped  ({ext['msg_count']} msgs, via {method}). "
            f"PROGRESS.md regenerated."
        )

    except Exception:
        _log.error_tb("pre_compact ERROR")
        try:
            memory_dir = Path(cwd) / "memory"
            (memory_dir / ".last_save.json").write_text(
                json.dumps({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "success": False,
                    "error": "see logs",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            # why: status write also failing means disk is unavailable; we
            # can't surface anything further without breaking the hook contract
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
