"""
Unified memory write entry — the anti-patch contract.

EVERY save path (PreCompact, Stop observer, /save-memories skill, MCP server,
CLI mem.py add) MUST go through `upsert_smart`. Direct calls to
`MemoryDB.insert_memory` are reserved for migration / bulk-load.

Decision tree for a new memory M about topic T:

  1. Hash exact match (db.find_by_hash) → SKIP (M is a perfect duplicate).
  2. Same topic + trigram-Jaccard ≥ HIGH_SIM (>= 0.80) on existing memory E:
        → MERGE_IN_PLACE: db.update_memory(E.id, content=M.content, ...)
        Treats M as a refined wording of the SAME fact. No new row.
  3. Same topic + Jaccard ≥ MID_SIM (0.50-0.80):
        → SUPERSEDE: db.supersede_memory(E.id, M.content, ...)
        Archives E, inserts M with supersedes_id=E.id. Preserves history.
  4. Otherwise:
        → INSERT NEW (independent fact).

This is the OPPOSITE of "always append + dedup later". It prevents the
patch-style stacking the user flagged (cf. docs/MEMORY_RULES.md).

After every successful upsert, `regenerate_memory_index(project_id, memory_dir)`
is called so memory/MEMORY.md is always fresh (anti the 50-day-stale failure
mode observed in v2.0).
"""
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make sibling subpackages importable when this file is loaded via either
# `import llm.memory_writer` or as a script under the package root.
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from core.db import MemoryDB
from core.privacy import clean_for_storage
from core.logger import get_logger

_log = get_logger("memory_writer")

# Similarity thresholds (tuned: 0.8 demands "essentially same sentence")
HIGH_SIM = 0.80
MID_SIM = 0.50
MIN_CONTENT_LEN = 10
MAX_CANDIDATES_TO_SCAN = 50


def _trigram_set(text: str) -> set:
    t = text.lower().strip()
    if len(t) < 3:
        return {t}
    return {t[i:i+3] for i in range(len(t) - 2)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _find_similar(db: MemoryDB, project_id: int, content: str, topic: str,
                  category: str) -> Tuple[Optional[Dict], float]:
    """Find the most similar active memory. Search scope: same topic (if any)
    OR same category. Returns (memory, similarity) or (None, 0.0)."""
    candidates: List[Dict] = []
    if topic:
        candidates = db.get_memories_by_topic(project_id, topic)
    if not candidates:
        # Fall back to category-scoped scan
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1 AND category = ?
                   ORDER BY updated_at DESC LIMIT ?""",
                (project_id, category, MAX_CANDIDATES_TO_SCAN)
            ).fetchall()
            candidates = [dict(r) for r in rows]

    if not candidates:
        return None, 0.0

    target = _trigram_set(content)
    best = None
    best_sim = 0.0
    for c in candidates[:MAX_CANDIDATES_TO_SCAN]:
        s = _jaccard(target, _trigram_set(c["content"]))
        if s > best_sim:
            best_sim = s
            best = c
    return best, best_sim


def upsert_smart(db: MemoryDB,
                 project_id: int,
                 session_id: Optional[int],
                 category: str,
                 content: str,
                 importance: int = 3,
                 tags: Optional[List[str]] = None,
                 topic: str = "") -> Dict:
    """Anti-patch write entry.

    Returns a result dict:
      {"action": "skipped"|"merged"|"superseded"|"inserted",
       "id": <memory_id>, "similarity": <float>, "old_id": <int|None>}
    """
    content = clean_for_storage((content or "").strip())
    if not content or len(content) < MIN_CONTENT_LEN:
        return {"action": "skipped", "id": None, "reason": "too_short"}

    if category not in ("decision", "result", "config", "bug", "task", "arch", "note"):
        category = "note"
    importance = max(1, min(5, int(importance)))
    tags = tags or []
    topic = (topic or "").strip()

    # 1. Hash exact match → skip
    h = MemoryDB.compute_content_hash(content)
    existing = db.find_by_hash(project_id, h)
    if existing:
        return {"action": "skipped", "id": existing["id"],
                "similarity": 1.0, "reason": "hash_match"}

    # 2/3. Similarity-based reconcile
    similar, sim = _find_similar(db, project_id, content, topic, category)
    if similar:
        if sim >= HIGH_SIM:
            db.update_memory(
                similar["id"],
                content=content,
                importance=max(importance, similar["importance"]),
                topic=topic or similar.get("topic"),
                tags=list(set(tags + ["merged"])),
            )
            _log.info(f"merged into #{similar['id']} sim={sim:.2f}")
            return {"action": "merged", "id": similar["id"],
                    "similarity": sim, "old_id": similar["id"]}

        if sim >= MID_SIM:
            new_id = db.supersede_memory(
                similar["id"], content, project_id, session_id,
                category, importance=max(importance, similar["importance"]),
                tags=list(set(tags + ["supersedes"])),
                topic=topic or similar.get("topic"),
            )
            _log.info(f"superseded #{similar['id']} -> #{new_id} sim={sim:.2f}")
            return {"action": "superseded", "id": new_id,
                    "similarity": sim, "old_id": similar["id"]}

    # 4. New independent fact
    new_id = db.insert_memory(
        project_id, session_id, category, content,
        importance=importance, tags=tags, topic=topic,
    )
    return {"action": "inserted", "id": new_id, "similarity": sim if similar else 0.0,
            "old_id": None}


def upsert_batch(db: MemoryDB,
                 project_id: int,
                 session_id: Optional[int],
                 memories: List[Dict],
                 memory_dir: Optional[Path] = None) -> Dict:
    """Batch upsert with single MEMORY.md regen at the end.

    Each item in `memories` is a dict with keys:
        category, content, importance, topic (optional), tags (optional)

    Returns aggregate counts: {"inserted": N, "merged": N, "superseded": N,
                               "skipped": N, "results": [<per-item>]}
    """
    counts = {"inserted": 0, "merged": 0, "superseded": 0, "skipped": 0}
    results = []
    for m in memories:
        r = upsert_smart(
            db, project_id, session_id,
            category=m.get("category", "note"),
            content=m.get("content", ""),
            importance=m.get("importance", 3),
            tags=m.get("tags"),
            topic=m.get("topic", ""),
        )
        counts[r["action"]] = counts.get(r["action"], 0) + 1
        results.append(r)

    counts["results"] = results

    if memory_dir is not None:
        try:
            regenerate_memory_index(db, project_id, memory_dir)
        except Exception as e:
            _log.error(f"MEMORY.md regen after batch failed: {e}")

    return counts


def regenerate_memory_index(db: MemoryDB, project_id: int, memory_dir: Path) -> None:
    """Rewrite memory/MEMORY.md from the current DB state.

    Called automatically after every batch upsert AND on consolidation /
    Stop-hook idle reorg, so MEMORY.md never goes stale.
    """
    from datetime import datetime

    stats = db.get_stats(project_id)
    topics = db.get_topics(project_id)
    top_kw = db.get_top_keywords(project_id, 25)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        "# Memory Index",
        "",
        "<!--",
        "  AUTO-GENERATED by cc-memory. DO NOT EDIT THIS FILE BY HAND.",
        "  Direct edits are overwritten on the next:",
        "    - PreCompact hook (auto / manual /compact)",
        "    - Stop-hook idle reorg (every 5 turns)",
        "    - llm.memory_writer.upsert_batch (any save path)",
        "    - cli/mem.py consolidate / cleanup",
        "  To add a memory, use one of:",
        "    /cc-mem add <category> \"<content>\"",
        "    /save-memories (skill)",
        "    dashboard or web_viewer Add-Memory dialogs",
        "  Each routes through llm.memory_writer.upsert_smart so the new entry",
        "  gets merged / superseded / inserted by the anti-patch contract and",
        "  this index gets regenerated automatically.",
        "-->",
        "",
        f"*Updated: {now_str}*  "
        f"|  Sessions: {stats['n_sessions']}  "
        f"|  Memories: {stats['n_memories']}  "
        f"|  Topics: {stats.get('n_topics', 0)}",
        "",
    ]

    if topics:
        lines += ["## Topic Summaries", ""]
        for t in topics:
            preview = t["content"][:120] + "..." if len(t["content"]) > 120 else t["content"]
            lines.append(f"- **{t['name']}** (v{t['version']}): {preview}")
        lines.append("")

    topic_counts = db.get_topic_memory_counts(project_id)
    if topic_counts:
        lines += ["## Memory Distribution", ""]
        for topic, count in sorted(topic_counts.items(), key=lambda x: -x[1])[:30]:
            lines.append(f"- `{topic}`: {count}")
        lines.append("")

    if stats["by_category"]:
        lines += ["## By Category", ""]
        for row in stats["by_category"]:
            avg = f"{row['avg_imp']:.1f}"
            lines.append(f"- `{row['category']}`: {row['n']} entries  (avg importance {avg})")
        lines.append("")

    if top_kw:
        lines += ["## Project Vocabulary", ""]
        lines.append(", ".join(f"`{kw}`" for kw in top_kw))
        lines.append("")

    sessions_dir = memory_dir / "sessions"
    if sessions_dir.exists():
        archive_files = sorted(
            sessions_dir.rglob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
        if archive_files:
            lines += ["## Recent Archives", ""]
            for af in archive_files:
                rel = af.relative_to(memory_dir).as_posix()
                lines.append(f"- `memory/{rel}`")
            lines.append("")

    lines += [
        "---",
        "*Query:        `python -m cc_memory.cli.mem --project <path> stats`*",
        "*Consolidate:  `python -m cc_memory.cli.mem --project <path> consolidate`*",
        "*Anti-patch contract:  see `docs/MEMORY_RULES.md`*",
    ]
    (memory_dir / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")
