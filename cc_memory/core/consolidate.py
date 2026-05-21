"""
Topic-based memory consolidation pipeline.

Pipeline:
  1. cleanup_garbage()           delete known junk patterns
  2. merge_near_duplicates()     fuzzy dedup within active memories (trigram Jaccard)
  3. assign_topics_auto()        keyword-based topic tagging
  4. consolidate_topics()        LLM summarize each topic -> topics table
  5. decay_importance()          reduce importance of old memories
  6. archive_consolidated()      archive memories captured in summaries

Anti-patch design: consolidation is the cleanup *backstop*. The primary
anti-patch mechanism is llm.memory_writer.upsert_smart, which prevents
duplicate insertion at write time. Consolidation handles drift accumulated
from sources that bypass the writer (manual SQL, legacy paths).

v2.1: project-neutral. The previous astrophysics _GROUPS dict has been
removed; topic clusters are derived purely from keyword frequency.
"""
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Allow import as a script (sys.path injection) AND as a package
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from core.db import MemoryDB
from core.logger import get_logger

_log = get_logger("consolidate")


# ── 1. Garbage cleanup ─────────────────────────────────────────────────────
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
_MIN_CONTENT_LEN = 20


def cleanup_garbage(db, project_id):
    memories = db.get_all_active_memories(project_id)
    to_delete = []
    for m in memories:
        content = m["content"].strip()
        if len(content) < _MIN_CONTENT_LEN:
            to_delete.append(m["id"])
            continue
        if any(pat.search(content) for pat in _GARBAGE_RE):
            to_delete.append(m["id"])
            continue
    if to_delete:
        db.delete_memories(to_delete)
    return len(to_delete)


# ── 2. Near-duplicate merging (trigram Jaccard) ─────────────────────────────
def _trigram_set(text):
    t = text.lower().strip()
    if len(t) < 3:
        return {t}
    return {t[i:i+3] for i in range(len(t) - 2)}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def merge_near_duplicates(db, project_id, threshold=0.65):
    memories = db.get_all_active_memories(project_id)
    if len(memories) < 2:
        return 0

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
            if mi["category"] != mj["category"]:
                continue
            sim = _jaccard(ti, tj)
            if sim >= threshold:
                if mi["importance"] > mj["importance"]:
                    to_archive.add(mj["id"])
                elif mj["importance"] > mi["importance"]:
                    to_archive.add(mi["id"])
                else:
                    if mi["created_at"] >= mj["created_at"]:
                        to_archive.add(mj["id"])
                    else:
                        to_archive.add(mi["id"])

    if to_archive:
        db.bulk_archive(list(to_archive))
    return len(to_archive)


# ── 3. Topic assignment (frequency-driven, project-neutral) ─────────────────
def _build_topic_seeds(db, project_id):
    """Use the top-K project keywords as topic seeds. No domain dictionary."""
    top_kw = db.get_top_keywords(project_id, 30)
    return [kw.lower() for kw in top_kw if len(kw) >= 3]


def _match_topic(content, topic_seeds):
    """Return the topic seed with the longest overlap in content, or None.

    A pure keyword presence test; we pick the seed whose appearance is most
    specific (longer seed wins ties). Generic enough for any codebase.
    """
    content_lower = content.lower()
    best = None
    best_len = 0
    for seed in topic_seeds:
        if seed in content_lower and len(seed) > best_len:
            best = seed
            best_len = len(seed)
    return best


def assign_topics_auto(db, project_id):
    seeds = _build_topic_seeds(db, project_id)
    if not seeds:
        return 0

    unassigned = db.get_unassigned_memories(project_id)
    assigned = 0

    topic_groups: Dict[str, List[int]] = defaultdict(list)
    for m in unassigned:
        topic = _match_topic(m["content"], seeds)
        if topic:
            topic_groups[topic].append(m["id"])
        else:
            # Fallback: category-as-topic ensures every memory has SOME topic
            topic_groups[m["category"]].append(m["id"])

    for topic, ids in topic_groups.items():
        db.bulk_set_topic(ids, topic)
        assigned += len(ids)

    return assigned


# ── 4. Topic consolidation via LLM ─────────────────────────────────────────
_CONSOLIDATION_PROMPT = """\
You are consolidating project memories into a topic summary.

Given a list of individual memories for topic "{topic_name}", create ONE concise summary paragraph.

Rules:
- Capture ALL specific values (numbers, file paths, parameter names)
- Current state first, then key decisions/history
- Self-contained: readable without other context
- Max 200 words
- Output ONLY the summary text, no JSON, no markdown headers, no quotes"""


def _summarize_topic_llm(topic_name, memories):
    from core.auth import get_api_key
    api_key, _ = get_api_key()
    if not api_key:
        return None

    mem_text = "\n".join(
        f"- [{m['category']}, imp={m['importance']}] {m['content']}"
        for m in memories
    )
    try:
        from llm.ccl_backend import call_llm
        text = call_llm(
            _CONSOLIDATION_PROMPT.format(topic_name=topic_name),
            f"Memories for topic \"{topic_name}\":\n\n{mem_text}",
            api_key, max_tokens=500, timeout=30,
        )
        return text.strip() if text.strip() else None
    except Exception as e:
        _log.error(f"consolidation LLM error for {topic_name}: {e}")
        return None


def _summarize_topic_fallback(topic_name, memories):
    """No-LLM bullet summary."""
    sorted_mems = sorted(memories, key=lambda m: -m["importance"])
    return "\n".join(f"- {m['content']}" for m in sorted_mems[:8])


def consolidate_topics(db, project_id, use_llm=True, min_memories_per_topic=3):
    all_memories = db.get_all_active_memories(project_id)
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
        summary = None
        if use_llm:
            summary = _summarize_topic_llm(topic, memories)
        if not summary:
            summary = _summarize_topic_fallback(topic, memories)
        db.upsert_topic(project_id, topic, summary)
        n_consolidated += 1

    return n_consolidated


# ── 5. Importance decay ─────────────────────────────────────────────────────
def decay_importance(db, project_id, age_days=30):
    cutoff_5 = (datetime.now() - timedelta(days=age_days)).isoformat()
    cutoff_4 = (datetime.now() - timedelta(days=age_days * 2)).isoformat()

    n_decayed = 0
    with db._connect() as conn:
        cur = conn.execute(
            """UPDATE memories SET importance = 4, updated_at = ?
               WHERE project_id = ? AND is_active = 1
                 AND importance = 5 AND updated_at < ?""",
            (db._now(), project_id, cutoff_5)
        )
        n_decayed += cur.rowcount
        cur = conn.execute(
            """UPDATE memories SET importance = 3, updated_at = ?
               WHERE project_id = ? AND is_active = 1
                 AND importance = 4 AND updated_at < ?""",
            (db._now(), project_id, cutoff_4)
        )
        n_decayed += cur.rowcount
    return n_decayed


# ── 6. Archive consolidated ─────────────────────────────────────────────────
def archive_consolidated(db, project_id, keep_per_topic=5):
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
        sorted_mems = sorted(memories, key=lambda m: (-m["importance"], m["created_at"]))
        for m in sorted_mems[keep_per_topic:]:
            to_archive.append(m["id"])

    if to_archive:
        db.bulk_archive(to_archive)
    return len(to_archive)


# ── Master orchestration ────────────────────────────────────────────────────
def run_consolidation(cwd, use_llm=True, verbose=True):
    memory_dir = Path(cwd) / "memory"
    db_path = memory_dir / "memory.db"
    if not db_path.exists():
        if verbose:
            _log.info(f"no DB at {db_path}")
        return {}

    db = MemoryDB(db_path)
    project_id = db.upsert_project(cwd)

    results = {}
    results["garbage_deleted"] = cleanup_garbage(db, project_id)
    results["duplicates_archived"] = merge_near_duplicates(db, project_id)
    results["topics_assigned"] = assign_topics_auto(db, project_id)
    results["topics_consolidated"] = consolidate_topics(db, project_id, use_llm=use_llm)
    results["importance_decayed"] = decay_importance(db, project_id)
    results["archived_after_consolidation"] = archive_consolidated(db, project_id)

    stats = db.get_stats(project_id)
    results["final_active"] = stats["n_memories"]
    results["final_topics"] = stats["n_topics"]
    if verbose:
        _log.info(
            f"consolidation done: {stats['n_memories']} active memories, "
            f"{stats['n_topics']} topics"
        )
    return results
