"""
Idle reorg — lightweight, no-LLM consolidation called from Stop hook.

Runs every N turns (default 5) to prevent drift between full consolidations.
Operations are O(N memories) and never call LLM, so they're safe to run in
the Stop hook's tight budget (≤2 seconds added).

What runs:
  1. cleanup_garbage         — drop known junk patterns
  2. assign_topics_auto      — keyword-frequency topic assignment for new memories
  3. gc_stale_claims         — delete old, trace-free unreceipted session claims
  4. regenerate_memory_index — refresh MEMORY.md so it never goes stale

What does NOT run here (deferred to PreCompact / manual consolidate):
  - LLM topic summarization (slow)
  - merge_near_duplicates    (O(N²), only at PreCompact)
  - decay_and_archive        (intentionally infrequent)
  - archive_consolidated     (only meaningful after summarization)
"""
import sys
from datetime import datetime
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from core.db import MemoryDB
from core.consolidate import (STALE_LOCK_S, assign_topics_auto, cleanup_garbage,
                              consolidation_lock_age)
from core.layout import DB_FILENAME, memory_dir as resolve_memory_dir
from core.logger import get_logger
# safe_id replaces this module's private `[:16]` truncating copy (three
# modules each had one; truncation cross-wired any two sessions sharing a
# 16-char prefix). read_marker never raises and refuses a planted symlink.
from core.markers import marker_path, read_marker, safe_id as _safe_id, write_marker
from llm.memory_writer import regenerate_memory_index

_log = get_logger("idle")

IDLE_INTERVAL_TURNS = 5  # run light reorg every N user turns
_MARKER_PREFIX = "cc_mem_idle_"


def _last_idle_turn(session_id):
    """Read the last turn at which we ran idle reorg. Returns 0 if never."""
    marker = marker_path(_MARKER_PREFIX, _safe_id(session_id))
    try:
        return int(read_marker(marker, "0").strip() or 0)
    except ValueError:
        # why: marker file corrupted — treat as never-run; will be overwritten
        # in this call so the corruption doesn't recur
        return 0


def _record_idle_turn(session_id, turn):
    # write_marker never raises: a refused write returns False, and the worst
    # case is one more idle reorg next turn, which is idempotent.
    write_marker(marker_path(_MARKER_PREFIX, _safe_id(session_id)), str(turn))


def maybe_run_idle(cwd: str, session_id: str, turn_count: int,
                   force: bool = False, db=None) -> dict:
    """Run idle reorg if enough turns have passed.

    Returns a dict of {garbage, topics_assigned, memory_md_regen} on actual
    run, or {} if skipped.

    `db` (v2.16.0): the caller's open `MemoryDB`, when it holds one. The
    Stop hook already has a handle for its other jobs, and constructing a
    second one here re-ran the bootstrap probes for nothing — measured at
    v2.15.2, two `MemoryDB.__init__` per Stop on the no-key path. A caller
    without a handle (the CLI, a test) passes nothing and one is opened.
    """
    if not force:
        last = _last_idle_turn(session_id)
        if turn_count - last < IDLE_INTERVAL_TURNS:
            return {}

    memory_dir = resolve_memory_dir(cwd)
    db_path = memory_dir / DB_FILENAME
    if not db_path.exists():
        return {}

    # A LIVE consolidation worker owns the tables for the next few minutes
    # (v2.16.0, C3): this reorg archives garbage and relabels topics while
    # `semantic_dedup` is re-reading survivors across a network round-trip,
    # and the worker's lock is scoped to other WORKERS, not to the data. The
    # horizon is the worker's own: a lock older than STALE_LOCK_S belongs to
    # a dead process and is no reason to wait. Nothing is recorded, so the
    # reorg is due again at the next Stop.
    lock_age = consolidation_lock_age(memory_dir)
    if lock_age is not None and lock_age < STALE_LOCK_S:
        _log.info(f"idle reorg deferred: consolidation lock is "
                  f"{lock_age:.0f}s old")
        return {}

    db = db if db is not None else MemoryDB(db_path)
    project_id = db.upsert_project(cwd)

    results = {
        "garbage": cleanup_garbage(db, project_id),
        "topics_assigned": assign_topics_auto(db, project_id),
        # Unreceipted claims from killed compactions, old enough that their
        # writer is dead and provably trace-free (no memories / summary /
        # archive attached). Single guarded DELETE — hook-budget cheap.
        "stale_claims": db.gc_stale_claims(project_id),
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
