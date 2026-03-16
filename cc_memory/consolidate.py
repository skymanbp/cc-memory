#!/usr/bin/env python3
"""
cc-memory/consolidate.py -- Topic-based memory consolidation

Hierarchy:
  Level 0: Global overview  (derived, injected every session)
  Level 1: Topic summaries  (topics table, injected every session)
  Level 2: Active memories  (memories table, injected by relevance)
  Level 3: Archived         (is_active=0, queryable only)

Consolidation pipeline:
  1. cleanup_garbage()         -- delete known junk patterns
  2. merge_near_duplicates()   -- fuzzy dedup within active memories
  3. assign_topics_auto()      -- keyword-based topic tagging
  4. consolidate_topics_llm()  -- LLM summarize each topic -> topics table
  5. decay_importance()        -- reduce importance of old memories
  6. archive_consolidated()    -- archive memories captured in summaries

Can run:
  - Manually:  python mem.py --project <path> consolidate
  - Auto:      triggered by pre_compact.py every N sessions
"""

import json
import re
import sys
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))
from db import MemoryDB


# ---------------------------------------------------------------------------
# 1. Garbage cleanup
# ---------------------------------------------------------------------------
# Patterns that should NEVER be memories (Claude meta-text, XML tags, etc.)
_GARBAGE_PATTERNS = [
    r"^<ide_opened_file>",
    r"^</?(ide_opened_file|system-reminder|antml)",
    r"^This may or may not be related to the current task",
    r"^Now I have all the information I need",
    r"^Let me compile my findings",
    r"^Here are my findings",
    r"^I'll now\b",
    r"^I will now\b",
    r"^Let me (now |start |begin )",
    r"^I've (gathered|collected|compiled) ",
    r"^Based on my (analysis|review|examination)",
    r"^(OK|Okay),? (let me|I'll|now)",
    r"^The (TodoWrite|Agent|Read|Bash|Grep|Glob) tool",
]
_GARBAGE_RE = [re.compile(p, re.IGNORECASE) for p in _GARBAGE_PATTERNS]

# Minimum content length for a useful memory
_MIN_CONTENT_LEN = 20


def cleanup_garbage(db: MemoryDB, project_id: int) -> int:
    """Delete memories matching garbage patterns. Returns count deleted."""
    memories = db.get_all_active_memories(project_id)
    to_delete = []
    for m in memories:
        content = m["content"].strip()
        # Too short
        if len(content) < _MIN_CONTENT_LEN:
            to_delete.append(m["id"])
            continue
        # Matches garbage pattern
        if any(pat.search(content) for pat in _GARBAGE_RE):
            to_delete.append(m["id"])
            continue
    if to_delete:
        db.delete_memories(to_delete)
    return len(to_delete)


# ---------------------------------------------------------------------------
# 2. Near-duplicate merging
# ---------------------------------------------------------------------------
def _trigram_set(text: str) -> set:
    """Generate character trigrams for fuzzy matching."""
    t = text.lower().strip()
    if len(t) < 3:
        return {t}
    return {t[i:i+3] for i in range(len(t) - 2)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def merge_near_duplicates(
    db: MemoryDB, project_id: int, threshold: float = 0.65
) -> int:
    """
    Find near-duplicate active memories and keep only the best version.
    Uses trigram Jaccard similarity. Returns count archived.
    """
    memories = db.get_all_active_memories(project_id)
    if len(memories) < 2:
        return 0

    # Precompute trigrams
    trigrams = [(m, _trigram_set(m["content"])) for m in memories]
    to_archive = set()

    for i in range(len(trigrams)):
        if trigrams[i][0]["id"] in to_archive:
            continue
        for j in range(i + 1, len(trigrams)):
            if trigrams[j][0]["id"] in to_archive:
                continue
            mi, ti = trigrams[i]
            mj, tj = trigrams[j]
            # Only compare same category (different categories can have similar text)
            if mi["category"] != mj["category"]:
                continue
            sim = _jaccard(ti, tj)
            if sim >= threshold:
                # Keep the one with higher importance, or newer if tied
                if mi["importance"] > mj["importance"]:
                    to_archive.add(mj["id"])
                elif mj["importance"] > mi["importance"]:
                    to_archive.add(mi["id"])
                else:
                    # Same importance: keep newer
                    if mi["created_at"] >= mj["created_at"]:
                        to_archive.add(mj["id"])
                    else:
                        to_archive.add(mi["id"])

    if to_archive:
        db.bulk_archive(list(to_archive))
    return len(to_archive)


# ---------------------------------------------------------------------------
# 3. Topic assignment (keyword-based, no LLM needed)
# ---------------------------------------------------------------------------
def _build_topic_keywords(db: MemoryDB, project_id: int) -> Dict[str, List[str]]:
    """
    Build topic -> keywords mapping from the keyword frequency table.
    Group by common prefixes and co-occurrence patterns.
    """
    top_kw = db.get_top_keywords(project_id, 60)
    if not top_kw:
        return {}

    # Frequency-weighted keywords: use the top ones as topic seeds
    # Strategy: each high-frequency keyword becomes a potential topic,
    # then cluster related keywords under it
    topic_map: Dict[str, List[str]] = {}

    # Common groupings (generic, works for any project)
    # Order matters: more specific groups first to avoid greedy matching
    _GROUPS = {
        # ML model types (specific first)
        "swin": ["swin", "transformer", "heatmap_loss", "windowed"],
        "gnn": ["gnn", "graph", "node", "edge", "peakgnn"],
        "cnn": ["cnn", "conv", "equivariant", "escnn", "physics_attention"],
        # Analysis
        "fusion": ["crf", "fusion", "channel", "noisy-or", "bayesian", "perpeak"],
        "physics": ["nfw", "sbi", "tda", "mass", "shear", "lensing", "diffusion"],
        "evaluation": ["accuracy", "f1", "precision", "recall", "auc", "loco"],
        # Pipeline stages
        "training": ["train", "pretrain", "finetune", "epoch", "loss", "weight"],
        "data": ["hdf5", "cache", "pkl", "csv", "dataset", "catalog"],
        "config": ["config", "grand_config", "hyperparameter", "parameter"],
    }

    # Check which groups have keywords in this project
    kw_lower_set = {kw.lower() for kw in top_kw}
    for group_name, group_kws in _GROUPS.items():
        matching = [kw for kw in group_kws if kw.lower() in kw_lower_set]
        if matching:
            topic_map[group_name] = [kw.lower() for kw in group_kws]

    # Add any high-frequency keywords not yet covered as their own topic
    covered = set()
    for kws in topic_map.values():
        covered.update(kws)

    for kw in top_kw[:15]:  # Top 15 most frequent
        if kw.lower() not in covered and len(kw) >= 3:
            topic_map[kw.lower()] = [kw.lower()]

    return topic_map


def _match_topic(content: str, topic_map: Dict[str, List[str]]) -> Optional[str]:
    """Find best matching topic for a memory's content.
    Uses keyword hit count, with tie-breaking by topic specificity (fewer keywords = more specific)."""
    content_lower = content.lower()
    scores: List[Tuple[str, int, int]] = []  # (topic, hits, -len(keywords))

    for topic, keywords in topic_map.items():
        hits = sum(1 for kw in keywords if kw in content_lower)
        if hits > 0:
            # Prefer topics with more hits; on tie, prefer more specific (fewer total keywords)
            scores.append((topic, hits, -len(keywords)))

    if not scores:
        return None
    scores.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return scores[0][0]


def assign_topics_auto(db: MemoryDB, project_id: int) -> int:
    """Assign topics to untagged memories using keyword matching. Returns count assigned."""
    topic_map = _build_topic_keywords(db, project_id)
    if not topic_map:
        return 0

    unassigned = db.get_unassigned_memories(project_id)
    assigned = 0

    # Group by topic for bulk update
    topic_groups: Dict[str, List[int]] = defaultdict(list)
    for m in unassigned:
        topic = _match_topic(m["content"], topic_map)
        if topic:
            topic_groups[topic].append(m["id"])
        else:
            # Fallback: use category as topic
            topic_groups[m["category"]].append(m["id"])

    for topic, ids in topic_groups.items():
        db.bulk_set_topic(ids, topic)
        assigned += len(ids)

    return assigned


# ---------------------------------------------------------------------------
# 4. Topic consolidation via LLM
# ---------------------------------------------------------------------------
_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_TIMEOUT = 30

_CONSOLIDATION_PROMPT = """\
You are consolidating project memories into a topic summary.

Given a list of individual memories for topic "{topic_name}", create ONE concise summary paragraph.

Rules:
- Capture ALL specific values (numbers, file paths, parameter names)
- Current state first, then key decisions/history
- Self-contained: readable without other context
- Max 200 words
- Output ONLY the summary text, no JSON, no markdown headers, no quotes"""


def _summarize_topic_llm(
    topic_name: str, memories: List[Dict]
) -> Optional[str]:
    """Call Haiku to consolidate memories into a topic summary."""
    from auth import get_api_key
    api_key, source = get_api_key()
    if not api_key:
        return None

    # Build memory list text
    mem_text = "\n".join(
        f"- [{m['category']}, imp={m['importance']}] {m['content']}"
        for m in memories
    )

    body = json.dumps({
        "model": _HAIKU_MODEL,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": f"Memories for topic \"{topic_name}\":\n\n{mem_text}"}],
        "system": _CONSOLIDATION_PROMPT.format(topic_name=topic_name),
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
        text = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        return text.strip() if text.strip() else None
    except Exception as e:
        print(f"[cc-memory] consolidation LLM error for {topic_name}: {e}",
              file=sys.stderr)
        return None


def _summarize_topic_fallback(
    topic_name: str, memories: List[Dict]
) -> str:
    """Fallback: concatenate top memories as bullet points."""
    # Sort by importance desc, take top entries
    sorted_mems = sorted(memories, key=lambda m: -m["importance"])
    lines = []
    for m in sorted_mems[:8]:
        lines.append(f"- {m['content']}")
    return "\n".join(lines)


def consolidate_topics(
    db: MemoryDB, project_id: int, use_llm: bool = True,
    min_memories_per_topic: int = 3,
) -> int:
    """
    For each topic with enough memories, create/update a consolidated summary.
    Returns number of topics consolidated.
    """
    all_memories = db.get_all_active_memories(project_id)

    # Group by topic
    by_topic: Dict[str, List[Dict]] = defaultdict(list)
    for m in all_memories:
        topic = m.get("topic") or "_unassigned"
        by_topic[topic].append(m)

    n_consolidated = 0
    for topic, memories in by_topic.items():
        if topic == "_unassigned":
            continue
        if len(memories) < min_memories_per_topic:
            continue

        # Try LLM, fallback to concatenation
        summary = None
        if use_llm:
            summary = _summarize_topic_llm(topic, memories)
        if not summary:
            summary = _summarize_topic_fallback(topic, memories)

        # Save to topics table
        db.upsert_topic(project_id, topic, summary)
        n_consolidated += 1

    return n_consolidated


# ---------------------------------------------------------------------------
# 5. Importance decay
# ---------------------------------------------------------------------------
def decay_importance(
    db: MemoryDB, project_id: int, age_days: int = 30
) -> int:
    """
    Reduce importance of old memories that haven't been updated.
    - imp 5 -> 4 after age_days
    - imp 4 -> 3 after age_days * 2
    This prevents importance inflation over time.
    Returns count decayed.
    """
    cutoff_5 = (datetime.now() - timedelta(days=age_days)).isoformat()
    cutoff_4 = (datetime.now() - timedelta(days=age_days * 2)).isoformat()

    n_decayed = 0
    with db._connect() as conn:
        # imp 5 -> 4 if older than age_days
        cur = conn.execute(
            """UPDATE memories SET importance = 4, updated_at = ?
               WHERE project_id = ? AND is_active = 1
                 AND importance = 5 AND updated_at < ?""",
            (db._now(), project_id, cutoff_5)
        )
        n_decayed += cur.rowcount

        # imp 4 -> 3 if older than age_days * 2
        cur = conn.execute(
            """UPDATE memories SET importance = 3, updated_at = ?
               WHERE project_id = ? AND is_active = 1
                 AND importance = 4 AND updated_at < ?""",
            (db._now(), project_id, cutoff_4)
        )
        n_decayed += cur.rowcount

    return n_decayed


# ---------------------------------------------------------------------------
# 6. Archive consolidated memories
# ---------------------------------------------------------------------------
def archive_consolidated(
    db: MemoryDB, project_id: int, keep_per_topic: int = 5
) -> int:
    """
    For topics that have been consolidated (have a summary in topics table),
    archive all but the top-N highest-importance memories.
    Returns count archived.
    """
    topics = db.get_topics(project_id)
    topic_names = {t["name"] for t in topics}

    all_memories = db.get_all_active_memories(project_id)
    by_topic: Dict[str, List[Dict]] = defaultdict(list)
    for m in all_memories:
        t = m.get("topic") or ""
        if t in topic_names:
            by_topic[t].append(m)

    to_archive = []
    for topic, memories in by_topic.items():
        if len(memories) <= keep_per_topic:
            continue
        # Sort by importance desc, then recency
        sorted_mems = sorted(
            memories,
            key=lambda m: (-m["importance"], m["created_at"]),
            reverse=False
        )
        # Sort properly: highest importance first, newest first within same importance
        sorted_mems = sorted(
            memories,
            key=lambda m: (-m["importance"], -len(m["created_at"]), m["created_at"]),
        )
        # Keep top N, archive the rest
        for m in sorted_mems[keep_per_topic:]:
            to_archive.append(m["id"])

    if to_archive:
        db.bulk_archive(to_archive)
    return len(to_archive)


# ---------------------------------------------------------------------------
# Master orchestration
# ---------------------------------------------------------------------------
def run_consolidation(
    cwd: str, use_llm: bool = True, verbose: bool = True
) -> Dict[str, int]:
    """
    Run the full consolidation pipeline.
    Returns dict of step -> count affected.
    """
    memory_dir = Path(cwd) / "memory"
    db_path = memory_dir / "memory.db"
    if not db_path.exists():
        if verbose:
            print(f"[cc-memory] no DB at {db_path}", file=sys.stderr)
        return {}

    db = MemoryDB(db_path)
    project_id = db.upsert_project(cwd)

    results = {}

    # Step 1: Garbage cleanup
    n = cleanup_garbage(db, project_id)
    results["garbage_deleted"] = n
    if verbose and n:
        print(f"[cc-memory] cleanup: {n} garbage memories deleted", file=sys.stderr)

    # Step 2: Dedup
    n = merge_near_duplicates(db, project_id)
    results["duplicates_archived"] = n
    if verbose and n:
        print(f"[cc-memory] dedup: {n} near-duplicates archived", file=sys.stderr)

    # Step 3: Topic assignment
    n = assign_topics_auto(db, project_id)
    results["topics_assigned"] = n
    if verbose and n:
        print(f"[cc-memory] topics: {n} memories assigned to topics", file=sys.stderr)

    # Step 4: Topic consolidation
    n = consolidate_topics(db, project_id, use_llm=use_llm)
    results["topics_consolidated"] = n
    if verbose and n:
        print(f"[cc-memory] consolidated: {n} topic summaries created/updated", file=sys.stderr)

    # Step 5: Importance decay
    n = decay_importance(db, project_id)
    results["importance_decayed"] = n
    if verbose and n:
        print(f"[cc-memory] decay: {n} memories had importance reduced", file=sys.stderr)

    # Step 6: Archive (only after consolidation has summaries)
    n = archive_consolidated(db, project_id)
    results["archived_after_consolidation"] = n
    if verbose and n:
        print(f"[cc-memory] archive: {n} memories archived (captured in topic summaries)", file=sys.stderr)

    # Final stats
    stats = db.get_stats(project_id)
    results["final_active"] = stats["n_memories"]
    results["final_topics"] = stats["n_topics"]
    if verbose:
        print(
            f"[cc-memory] consolidation done: "
            f"{stats['n_memories']} active memories, "
            f"{stats['n_topics']} topics",
            file=sys.stderr
        )

    return results
