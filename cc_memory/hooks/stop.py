#!/usr/bin/env python3
"""
Stop hook — fires after each Claude response.

Three jobs:
  1. OBSERVER: extract memories from this turn's tool observations via Haiku.
     Saves through llm.memory_writer.upsert_smart (anti-patch).
  2. IDLE REORG: every 5 turns, run lightweight no-LLM cleanup +
     MEMORY.md regen + PROGRESS.md patch.
  3. PROGRESS.md PATCH: every turn, update files_touched and open_todos
     based on observations.

NOTE: The previous "save-memories reminder" text spam has been REMOVED.
The forced <system-reminder> in SessionStart and the auto-saves above do
the work; spamming Claude with "remember to call /save-memories" was noise.
"""
import json
import sys
import tempfile
import urllib.error
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

from core.db import MemoryDB
from core.logger import get_logger
from core.idle import maybe_run_idle
from core.progress import write_progress_md
from llm.memory_writer import upsert_batch

_log = get_logger("stop")

_MIN_OBS_FOR_EVAL = 3
_TURN_FILE_PREFIX = "cc_mem_turns_"
_PROMPT_FILE_PREFIX = "cc_mem_prompt_"
_LAST_EVAL_PREFIX = "cc_mem_eval_"

_API_TIMEOUT = 8  # within Stop hook budget

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


def _safe_id(session_id):
    return session_id[:16].replace("/", "_").replace("\\", "_")


def _read_turn_count(session_id):
    safe = _safe_id(session_id)
    f = Path(tempfile.gettempdir()) / f"{_TURN_FILE_PREFIX}{safe}"
    if not f.exists():
        return 0
    try:
        return int(f.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        # why: corrupted turn counter file — treat as 0 (best-effort; the
        # next UserPromptSubmit will overwrite it correctly)
        return 0


def _observer_evaluate(cwd, session_id, memory_dir):
    from core.auth import get_api_key
    from core.privacy import clean_for_storage

    db_path = memory_dir / "memory.db"
    if not db_path.exists():
        return 0

    api_key, _ = get_api_key()
    if not api_key:
        return 0

    db = MemoryDB(db_path)
    project_id = db.upsert_project(cwd)

    safe = _safe_id(session_id)
    eval_file = Path(tempfile.gettempdir()) / f"{_LAST_EVAL_PREFIX}{safe}"
    last_eval_ts = ""
    if eval_file.exists():
        try:
            last_eval_ts = eval_file.read_text(encoding="utf-8").strip()
        except OSError:
            # why: eval marker unreadable — fall back to "scan recent"
            # rather than skip evaluation entirely
            last_eval_ts = ""

    if last_eval_ts:
        observations = db.get_observations_since(project_id, last_eval_ts)
    else:
        observations = db.get_recent_observations(project_id, limit=20)

    if len(observations) < _MIN_OBS_FOR_EVAL:
        return 0

    prompt_file = Path(tempfile.gettempdir()) / f"{_PROMPT_FILE_PREFIX}{safe}"
    user_prompt = ""
    if prompt_file.exists():
        try:
            user_prompt = prompt_file.read_text(encoding="utf-8").strip()
        except OSError:
            # why: prompt context is enrichment, not required for extraction
            user_prompt = ""

    obs_lines = []
    for o in observations[-20:]:
        tool = o["tool_name"]
        inp = (o.get("tool_input", "") or "")[:200]
        out = (o.get("tool_output", "") or "")[:100]
        obs_lines.append(f"[{tool}] {inp}" + (f" -> {out}" if out else ""))

    obs_text = "\n".join(obs_lines)
    user_context = f"User request: {user_prompt}\n\n" if user_prompt else ""
    user_msg = f"{user_context}Tool observations:\n{obs_text}"

    try:
        from llm.ccl_backend import call_llm
        text = call_llm(_OBSERVER_PROMPT, user_msg, api_key,
                        max_tokens=1000, timeout=_API_TIMEOUT)
        text = text.strip()
        if text.startswith("```"):
            text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
        memories = json.loads(text)
        if not isinstance(memories, list):
            return 0

        # Sanitize content and route through memory_writer
        cleaned = []
        for m in memories:
            if not isinstance(m, dict):
                continue
            content = clean_for_storage((m.get("content") or "").strip())
            if not content or len(content) < 10:
                continue
            cleaned.append({
                "category": m.get("category", "note"),
                "content": content,
                "importance": max(1, min(int(m.get("importance", 3)), 5)),
                "topic": m.get("topic", "") if isinstance(m.get("topic", ""), str) else "",
                "tags": ["observer", "realtime"],
            })

        counts = upsert_batch(db, project_id, None, cleaned, memory_dir=memory_dir)
        n_total = sum(counts.get(k, 0) for k in ("inserted", "merged", "superseded"))

        try:
            eval_file.write_text(
                datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
            )
        except OSError:
            # why: marker write is best-effort; next eval will scan from
            # last_session boundary instead of last_eval — degraded but works
            pass

        if n_total:
            _log.info(
                f"observer: {counts.get('inserted',0)} new, "
                f"{counts.get('merged',0)} merged, "
                f"{counts.get('superseded',0)} superseded "
                f"from {len(observations)} obs"
            )
        return n_total

    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, OSError, KeyError, ValueError) as e:
        _log.error(f"observer evaluation failed: {e}")
        return 0


def _patch_progress_from_recent_obs(db, project_id, memory_dir):
    """Drip-update PROGRESS.md files_touched from the latest observations."""
    obs = db.get_recent_observations(project_id, limit=40)
    files_read = list(dict.fromkeys(
        o["tool_input"] for o in obs
        if o["tool_name"] == "Read" and o["tool_input"]
    ))[:20]
    files_modified = list(dict.fromkeys(
        o["tool_input"] for o in obs
        if o["tool_name"] in ("Edit", "Write", "MultiEdit") and o["tool_input"]
    ))[:20]

    files_touched = (
        [{"path": p, "action": "edit"} for p in files_modified] +
        [{"path": p, "action": "read"} for p in files_read if p not in files_modified]
    )
    if not files_touched:
        return
    db.patch_progress(project_id, files_touched=files_touched, trigger_type="stop")
    try:
        write_progress_md(db, project_id, memory_dir)
    except Exception as e:
        _log.error(f"PROGRESS.md patch failed: {e}")


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        sys.exit(0)

    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    if not cwd or not session_id:
        sys.exit(0)

    memory_dir = Path(cwd) / "memory"
    if not (memory_dir / "memory.db").exists():
        sys.exit(0)

    # Job 1: observer evaluation
    try:
        _observer_evaluate(cwd, session_id, memory_dir)
    except Exception:
        _log.error_tb("observer error")

    # Job 2: idle reorg (every 5 turns)
    turn_count = _read_turn_count(session_id)
    try:
        maybe_run_idle(cwd, session_id, turn_count)
    except Exception as e:
        _log.error(f"idle reorg failed: {e}")

    # Job 3: per-turn PROGRESS.md files_touched patch
    try:
        db = MemoryDB(memory_dir / "memory.db")
        project_id = db.upsert_project(cwd)
        _patch_progress_from_recent_obs(db, project_id, memory_dir)

        # Compact status line for Claude (one line, every turn)
        stats = db.get_stats(project_id)
        n_obs = db.get_observation_count(project_id)
        print(
            f"\n[cc-memory] {stats['n_memories']} memories"
            f" | {n_obs} obs"
            f" | {stats.get('n_topics', 0)} topics"
            f" | PROGRESS.md fresh"
        )
    except Exception:
        _log.error_tb("stop hook tail")
        print("\n[cc-memory] stop hook ran (degraded)")

    sys.exit(0)


if __name__ == "__main__":
    main()
