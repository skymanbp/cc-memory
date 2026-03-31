#!/usr/bin/env python3
"""
cc-memory/stop.py -- Stop hook (fires after each Claude response)
=================================================================
Two jobs:
  1. OBSERVER: If enough unsaved observations accumulated this turn,
     call Haiku to extract memories in real-time (like claude-mem's
     observer agent, but batched per-turn instead of per-tool-call).
  2. REMINDER: After 8+ turns, remind to call /save-memories.

The observer mechanism provides "AI decides what to save" on every turn,
not just at compaction. Combined with PreCompact extraction, this gives
double coverage.

Stdout: reminder text (Claude sees it)
Stderr: SUPPRESSED
"""
import json
import sys
import tempfile
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))

_MIN_TURNS_FOR_REMINDER = 8
_MIN_OBS_FOR_EVAL = 3         # Minimum observations to trigger evaluation
_MARKER_PREFIX = "cc_memory_reminded_"
_TURN_FILE_PREFIX = "cc_mem_turns_"
_PROMPT_FILE_PREFIX = "cc_mem_prompt_"
_LAST_EVAL_PREFIX = "cc_mem_eval_"

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_TIMEOUT = 8  # seconds — must fit within hook timeout

_OBSERVER_PROMPT = """\
You are a memory observer. Given a user's request and a batch of tool observations \
from a Claude Code session, extract ONLY the observations worth remembering long-term.

Output a JSON array of objects:
- "category": decision|result|config|bug|task|arch|note
- "content": one concise, self-contained sentence with specific values
- "importance": 1-5 (5=critical, 4=important, 3=useful, 2=minor)
- "topic": short keyword for grouping

Rules:
- Only save CONCLUSIONS and OUTCOMES, not intermediate steps
- Skip: file reads without insight, routine git commands, navigation
- Each memory must be understandable WITHOUT conversation context
- Include specific values: file names, numbers, error messages
- 0-5 memories max per batch. Return [] if nothing worth saving.
- Output ONLY valid JSON array."""


def _safe_id(session_id: str) -> str:
    return session_id[:16].replace("/", "_").replace("\\", "_")


def _observer_evaluate(cwd, session_id):
    """Batch-evaluate recent observations via Haiku. Returns count saved."""
    from logger import get_logger
    log = get_logger("stop_observer")

    db_path = Path(cwd) / "memory" / "memory.db"
    if not db_path.exists():
        return 0

    from db import MemoryDB
    from auth import get_api_key
    from privacy import clean_for_storage

    api_key, source = get_api_key()
    if not api_key:
        return 0

    db = MemoryDB(db_path)
    project_id = db.upsert_project(cwd)

    # Get last evaluation timestamp
    safe = _safe_id(session_id)
    eval_file = Path(tempfile.gettempdir()) / f"{_LAST_EVAL_PREFIX}{safe}"
    last_eval_ts = ""
    if eval_file.exists():
        try:
            last_eval_ts = eval_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    # Get observations since last evaluation
    if last_eval_ts:
        observations = db.get_observations_since(project_id, last_eval_ts)
    else:
        observations = db.get_recent_observations(project_id, limit=20)

    if len(observations) < _MIN_OBS_FOR_EVAL:
        return 0

    # Get user prompt for context
    prompt_file = Path(tempfile.gettempdir()) / f"{_PROMPT_FILE_PREFIX}{safe}"
    user_prompt = ""
    if prompt_file.exists():
        try:
            user_prompt = prompt_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    # Build observation text
    obs_lines = []
    for o in observations[-20:]:  # Cap at 20
        tool = o["tool_name"]
        inp = (o.get("tool_input", "") or "")[:200]
        out = (o.get("tool_output", "") or "")[:100]
        obs_lines.append(f"[{tool}] {inp}" + (f" -> {out}" if out else ""))

    obs_text = "\n".join(obs_lines)
    user_context = f"User request: {user_prompt}\n\n" if user_prompt else ""

    body = json.dumps({
        "model": _HAIKU_MODEL,
        "max_tokens": 1000,
        "messages": [{
            "role": "user",
            "content": f"{user_context}Tool observations:\n{obs_text}",
        }],
        "system": _OBSERVER_PROMPT,
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
            return 0

        n_saved = 0
        for m in memories:
            if not isinstance(m, dict):
                continue
            content = clean_for_storage(m.get("content", "").strip())
            if not content or len(content) < 10:
                continue
            cat = m.get("category", "note")
            if cat not in ("decision", "result", "config", "bug", "task", "arch", "note"):
                cat = "note"
            imp = max(1, min(int(m.get("importance", 3)), 5))

            # Hash dedup
            content_hash = MemoryDB.compute_content_hash(content)
            if db.is_duplicate_hash(project_id, content_hash):
                continue

            db.insert_memory(
                project_id, None, cat, content,
                importance=imp,
                tags=["observer", "realtime"],
                topic=m.get("topic", ""),
            )
            n_saved += 1

        # Update evaluation timestamp
        try:
            eval_file.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        except OSError:
            pass

        if n_saved > 0:
            log.info(f"observer: {n_saved} memories from {len(observations)} observations")

        return n_saved

    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, OSError, KeyError, ValueError) as e:
        log.error(f"observer evaluation failed: {e}")
        return 0


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    if not cwd or not session_id:
        sys.exit(0)

    # Check if this project has cc-memory initialized
    memory_dir = Path(cwd) / "memory"
    if not (memory_dir / "memory.db").exists():
        sys.exit(0)

    # ── Job 1: Observer evaluation (AI decides what to save) ──────────
    try:
        _observer_evaluate(cwd, session_id)
    except Exception:
        try:
            from logger import get_logger
            get_logger("stop").error_tb("observer error")
        except Exception:
            pass

    # ── Job 2: Reminder (after 8+ turns, once per session) ────────────
    marker = Path(tempfile.gettempdir()) / f"{_MARKER_PREFIX}{session_id[:16]}"
    if not marker.exists():
        turn_count = 0
        safe = _safe_id(session_id)
        turn_file = Path(tempfile.gettempdir()) / f"{_TURN_FILE_PREFIX}{safe}"
        if turn_file.exists():
            try:
                turn_count = int(turn_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass

        if turn_count >= _MIN_TURNS_FOR_REMINDER:
            try:
                marker.write_text(session_id, encoding="utf-8")
            except Exception:
                pass
            print(
                "\n[cc-memory] This conversation has been substantial. "
                "Remember to call /save-memories before ending to preserve "
                "important decisions, results, and insights."
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
