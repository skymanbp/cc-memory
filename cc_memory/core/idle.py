"""
Idle reorg — lightweight, no-LLM consolidation called from Stop hook.

Runs every N turns (default 5) to prevent drift between full consolidations.
Operations are O(N memories) and never call LLM, so they're safe to run in
the Stop hook's tight budget (≤2 seconds added).

What runs:
  1. cleanup_garbage         — drop known junk patterns
  2. assign_topics_auto      — keyword-frequency topic assignment for new memories
  3. regenerate_memory_index — refresh MEMORY.md so it never goes stale

What does NOT run here (deferred to PreCompact / manual consolidate):
  - LLM topic summarization (slow)
  - merge_near_duplicates    (O(N²), only at PreCompact)
  - decay_importance         (intentionally infrequent)
  - archive_consolidated     (only meaningful after summarization)
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from core.db import MemoryDB
from core.consolidate import cleanup_garbage, assign_topics_auto
from core.logger import get_logger
from llm.memory_writer import regenerate_memory_index

_log = get_logger("idle")

IDLE_INTERVAL_TURNS = 5  # run light reorg every N user turns
_MARKER_PREFIX = "cc_mem_idle_"


def _safe_id(session_id):
    return session_id[:16].replace("/", "_").replace("\\", "_")


def _last_idle_turn(session_id):
    """Read the last turn at which we ran idle reorg. Returns 0 if never."""
    marker = Path(tempfile.gettempdir()) / f"{_MARKER_PREFIX}{_safe_id(session_id)}"
    if not marker.exists():
        return 0
    try:
        return int(marker.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        # why: marker file corrupted — treat as never-run; will be overwritten
        # in this call so the corruption doesn't recur
        return 0


def _record_idle_turn(session_id, turn):
    marker = Path(tempfile.gettempdir()) / f"{_MARKER_PREFIX}{_safe_id(session_id)}"
    try:
        marker.write_text(str(turn), encoding="utf-8")
    except OSError:
        # why: tempfile write failure (read-only fs / disk full) — skip the
        # marker update; worst case we re-run idle reorg next turn, which
        # is idempotent
        pass


def maybe_run_idle(cwd: str, session_id: str, turn_count: int,
                   force: bool = False) -> dict:
    """Run idle reorg if enough turns have passed.

    Returns a dict of {garbage, topics_assigned, memory_md_regen} on actual
    run, or {} if skipped.
    """
    if not force:
        last = _last_idle_turn(session_id)
        if turn_count - last < IDLE_INTERVAL_TURNS:
            return {}

    memory_dir = Path(cwd) / "memory"
    db_path = memory_dir / "memory.db"
    if not db_path.exists():
        return {}

    db = MemoryDB(db_path)
    project_id = db.upsert_project(cwd)

    results = {
        "garbage": cleanup_garbage(db, project_id),
        "topics_assigned": assign_topics_auto(db, project_id),
        "memory_md_regen": False,
    }

    try:
        regenerate_memory_index(db, project_id, memory_dir)
        results["memory_md_regen"] = True
    except Exception as e:
        _log.error(f"MEMORY.md regen failed: {e}")

    _record_idle_turn(session_id, turn_count)

    if any(v for v in results.values() if v):
        _log.info(
            f"idle reorg @ turn {turn_count}: "
            f"garbage={results['garbage']} topics={results['topics_assigned']}"
        )
    return results
