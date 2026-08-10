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
patch-style stacking the user flagged (cf. docs/CONTRACTS.md#anti-patch-contract).

After every successful upsert, `regenerate_memory_index(project_id, memory_dir)`
is called so memory/MEMORY.md is always fresh (anti the 50-day-stale failure
mode observed in v2.0).
"""
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make sibling subpackages importable when this file is loaded via either
# `import llm.memory_writer` or as a script under the package root.
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from core.db import CATEGORIES, MemoryDB
from core.atomic import write_atomic, _DERIVED_BUDGET_S
from core.privacy import clean_for_storage, neutralize_document, neutralize_inline
from core.logger import get_logger
# ONE similarity substrate (core/textsim.py). This module, core/consolidate.py
# and core/plan.py each carried a private English-only `_trigram_set` copy;
# character trigrams collapse on CJK (a one-character correction in a
# 10-character Chinese fact scored 0.4545 — and 0.23 on a live database —
# where the equivalent English edit scores 0.7317), so every CJK correction
# fell below MID_SIM and was INSERTED beside the fact it corrects. The old
# names are kept as aliases so the decision tree below reads unchanged.
from core.textsim import jaccard as _jaccard, shingle_set as _trigram_set

_log = get_logger("memory_writer")


# Was a private copy through v2.5.2, documented as a "deliberate literal twin"
# of core/plan.py's. Neither was a twin of core/progress.py's, which retried and
# re-raised; these two had no retry and fell back to the plain TRUNCATING write,
# reintroducing the torn-read defect for that call. `core` may be imported by
# `llm` — the split never had a dependency reason. See core/atomic.py.
_atomic_write_text = write_atomic


# Similarity thresholds (tuned: 0.8 demands "essentially same sentence")
HIGH_SIM = 0.80
MID_SIM = 0.50
MIN_CONTENT_LEN = 10
# 500, up from 50. The scan is a set-intersection per candidate (~µs each);
# what 50 actually bounded was CORRECTNESS, not cost: get_memories_by_topic
# orders by (importance DESC, created_at DESC), so on a 51-memory topic a
# 0.95-similar row ranked 51st was simply never compared and the "new" fact
# was inserted beside it — measured, reported similarity 0.036 against a true
# 0.952. Topics larger than 500 log the truncation instead of hiding it.
MAX_CANDIDATES_TO_SCAN = 500

# Tag-list ceiling. `memory_add` is a model-invokable MCP tool and nothing
# bounded `tags`: a 10,000-entry list was stored verbatim (measured), and
# every render of the row pays for it forever. 32 is far above any organic
# use (the largest emitter ships 3).
MAX_TAGS = 32

# How many topic summaries MEMORY.md lists. The Memory Distribution block one
# section below has capped at 30 since it was written; this one had no cap at
# all, in the file SessionStart's forced reminder orders the next Claude to
# read first.
_MEMORY_MD_TOPICS = 40


def _merged_tags(*groups):
    """Order-preserving union of tag lists, capped at MAX_TAGS.

    The MERGE branch used to write ``set(incoming + ["merged"])`` — the
    SURVIVING row's own tags were destroyed, so a memory born as
    ``["observer","realtime"]`` lost its provenance on the first merge
    (measured: tags collapsed to ``["merged"]``). Existing tags come first so
    the cap can only ever drop the newest excess, never provenance.
    """
    out = []
    for group in groups:
        if isinstance(group, str):
            # A bare string is ONE tag, not an iterable of its characters:
            # `_merged_tags(rows, "manual")` used to explode into
            # ['m','a','n','u','l'] and store that verbatim.
            group = [group]
        for tag in group or []:
            if isinstance(tag, str) and tag and tag not in out:
                out.append(tag)
    if len(out) > MAX_TAGS:
        _log.warn(f"tag list capped at {MAX_TAGS} (got {len(out)})")
        out = out[:MAX_TAGS]
    return out


def _row_tags(row):
    """A memory row's stored tags as a list, [] on any malformed shape."""
    try:
        tags = json.loads(row.get("tags") or "[]")
        return tags if isinstance(tags, list) else []
    except (ValueError, TypeError):
        return []


def _make_pick(content: str):
    """Build the pure best-candidate function `reconcile_upsert` calls INSIDE
    its transaction. Replaces the old `_find_similar`, whose reads ran on
    their own self-committing connections — the decision was made from a
    snapshot no write boundary protected (register X1). The scan itself is
    unchanged: trigram-Jaccard, first MAX_CANDIDATES_TO_SCAN candidates,
    truncation logged (no silent caps — a candidate beyond the slice is a
    candidate the decision never saw, and the cost is a duplicate row)."""
    target = _trigram_set(content)

    def pick(candidates: List[Dict]) -> Tuple[Optional[Dict], float]:
        if len(candidates) > MAX_CANDIDATES_TO_SCAN:
            _log.info(f"similarity scan truncated: {len(candidates)} "
                      f"candidates, scanning first {MAX_CANDIDATES_TO_SCAN}")
        best, best_sim = None, 0.0
        for c in candidates[:MAX_CANDIDATES_TO_SCAN]:
            s = _jaccard(target, _trigram_set(c["content"]))
            if s > best_sim:
                best_sim = s
                best = c
        return best, best_sim

    return pick


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

    if category not in CATEGORIES:
        category = "note"
    importance = max(1, min(5, int(importance)))
    tags = tags or []
    # Same gate as `content` above. `topic` is model-supplied through the
    # `memory_add` MCP tool (its schema declares a free-form string with no
    # pattern), and it is rendered into MEMORY.md, PROGRESS.md §5, the
    # SessionStart topics layer and every MCP result — so an armed value here
    # is a permanent forgery re-injected as authoritative context every
    # session. It was the one model-controlled column with no write-path gate.
    topic = clean_for_storage((topic or "").strip())

    # 1-4. The WHOLE decision tree — hash-skip, similarity scan, and the
    # branch write — runs inside one BEGIN IMMEDIATE transaction
    # (MemoryDB.reconcile_upsert). It used to be find_by_hash + _find_similar
    # + the write across 3+ self-committing connections, so two concurrent
    # savers of the same sentence both observed an empty table and both
    # INSERTed — the anti-patch contract's "never stacks" was false exactly
    # when two hooks <!--ce:hooks:asof--> ran at once (register X1; measured
    # actions=['inserted','inserted'], 2 active rows with one hash). This
    # module keeps the POLICY: thresholds, the similarity function, and the
    # tag-union rule below all go in as parameters.
    def _merge_fields(row: Dict) -> Dict:
        # Union with the SURVIVING row's tags, never replace: merge rewrites
        # the row in place and supersede carries the fact forward, so both
        # inherit its provenance (["observer","realtime"], ["mcp"], …).
        return {"importance": max(importance, row["importance"]),
                "topic": topic or row.get("topic"),
                "tags": _merged_tags(_row_tags(row), tags, ["merged"])}

    def _supersede_fields(row: Dict) -> Dict:
        return {"importance": max(importance, row["importance"]),
                "topic": topic or row.get("topic"),
                "tags": _merged_tags(_row_tags(row), tags, ["supersedes"])}

    result = db.reconcile_upsert(
        project_id, session_id, category, content,
        importance=importance, tags=_merged_tags(tags), topic=topic,
        high_sim=HIGH_SIM, mid_sim=MID_SIM,
        max_candidates=MAX_CANDIDATES_TO_SCAN,
        pick=_make_pick(content),
        merge_fields=_merge_fields,
        supersede_fields=_supersede_fields,
    )
    if result["action"] == "merged":
        _log.info(f"merged into #{result['id']} sim={result['similarity']:.2f}")
    elif result["action"] == "superseded":
        _log.info(f"superseded #{result['old_id']} -> #{result['id']} "
                  f"sim={result['similarity']:.2f}")
    return result


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


_RENDER_ATTEMPTS = 3


def regenerate_memory_index(db: MemoryDB, project_id: int, memory_dir: Path) -> None:
    """Rewrite memory/MEMORY.md from the current DB state.

    Called automatically after every batch upsert AND on consolidation /
    Stop-hook idle reorg, so MEMORY.md never goes stale.

    The render is a SNAPSHOT verdict — several separate queries, each on its
    own connection — and `write_atomic` makes the replacement atomic without
    making it ORDERED. Two surfaces (a hook, the MCP server, the dashboard,
    the viewer) routinely render concurrently, and the slower renderer used
    to replace the faster one's newer file: measured, the DB held 2 memories
    while MEMORY.md announced 1, and MEMORY.md is re-injected as
    authoritative context. So the render is retried while the DB moves under
    it, and a stale render is re-done rather than written.

    The moved-under-us probe is `PRAGMA data_version`, read twice on ONE
    HELD connection: the counter bumps iff some OTHER connection committed
    between the two reads, and every writer in this package commits on its
    own connection (`_connect` opens per call), so any concurrent commit is
    visible — same-process or not. The previous probe was a fingerprint of
    row counts / MAX(id) / MAX(updated_at); `_now()` stamps whole seconds,
    so an in-place UPDATE landing in the same second changed none of the
    three and the stale render was accepted as current (reproduced: the DB
    held "new summary" v2 while MEMORY.md kept "old summary").
    """
    for _attempt in range(_RENDER_ATTEMPTS):
        with db._connect() as conn:
            _dv_before = conn.execute("PRAGMA data_version").fetchone()[0]
            _rendered = _render_memory_index(db, project_id, memory_dir)
            _moved = (conn.execute("PRAGMA data_version").fetchone()[0]
                      != _dv_before)
        if not _moved:
            break
    # After _RENDER_ATTEMPTS of continuous churn the last render is written
    # anyway: it projects a state that really existed moments ago, every
    # writer regenerates again on its next save, and refusing to write at all
    # would strand MEMORY.md at a much older generation.
    _write_memory_index(memory_dir, _rendered)


def _render_memory_index(db: MemoryDB, project_id: int, memory_dir: Path) -> str:
    """The pure render half of regenerate_memory_index: DB + dir -> text."""
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

    # Every interpolated value below is neutralised. MEMORY.md renders no
    # memory CONTENT — but topic names, category names and keywords are all
    # derived by the LLM extractor from transcript text, so they are just as
    # model-reachable. Measured on one armed topic name (content untouched,
    # ordinary): 1 complete <system-reminder> block and 3 "## " headings in a
    # document that has 2. `neutralize_inline` because each of these owns
    # exactly one output line, and several sit inside `backticks` that a
    # newline would also escape from.
    if topics:
        lines += ["## Topic Summaries", ""]
        # Bounded, like the Memory Distribution block below it. This list was
        # the one unbounded section of a file SessionStart's forced reminder
        # tells the next Claude to read: measured 42 KB on this project today
        # and ~298 KB at 2 000 topics, which is context budget spent on a
        # listing rather than on the facts.
        if len(topics) > _MEMORY_MD_TOPICS:
            lines.append(f"_(newest {_MEMORY_MD_TOPICS} of {len(topics)}; "
                         f"`/cc-mem topics` lists them all)_")
            lines.append("")
        for t in topics[:_MEMORY_MD_TOPICS]:
            preview = t["content"][:120] + "..." if len(t["content"]) > 120 else t["content"]
            lines.append(f"- **{neutralize_inline(t['name'])}** "
                         f"(v{t['version']}): {neutralize_inline(preview)}")
        lines.append("")

    topic_counts = db.get_topic_memory_counts(project_id)
    if topic_counts:
        lines += ["## Memory Distribution", ""]
        for topic, count in sorted(topic_counts.items(), key=lambda x: -x[1])[:30]:
            lines.append(f"- `{neutralize_inline(str(topic))}`: {count}")
        lines.append("")

    if stats["by_category"]:
        lines += ["## By Category", ""]
        for row in stats["by_category"]:
            avg = f"{row['avg_imp']:.1f}"
            lines.append(f"- `{neutralize_inline(str(row['category']))}`: "
                         f"{row['n']} entries  (avg importance {avg})")
        lines.append("")

    if top_kw:
        lines += ["## Project Vocabulary", ""]
        lines.append(", ".join(f"`{neutralize_inline(str(kw))}`" for kw in top_kw))
        lines.append("")

    sessions_dir = memory_dir / "sessions"
    if sessions_dir.exists():
        # Newest-first WITHOUT rglob + stat over the whole archive. The old
        # shape walked every session file ever written and called `stat()` on
        # each one to pick five, on every MEMORY.md regeneration — work
        # proportional to a project's entire history for a five-line display
        # block. `core.progress.write_session_archive` lays archives out as
        # `sessions/YYYY/MM/<YYYYmmddTHHMMSS_mmm>…md`, so descending name order
        # IS descending time order and only the newest month has to be opened.
        archive_files = []
        try:
            for year in sorted((d for d in sessions_dir.iterdir() if d.is_dir()),
                               key=lambda d: d.name, reverse=True):
                for month in sorted((d for d in year.iterdir() if d.is_dir()),
                                    key=lambda d: d.name, reverse=True):
                    archive_files += sorted(month.glob("*.md"),
                                            key=lambda p: p.name, reverse=True)
                    if len(archive_files) >= 5:
                        break
                if len(archive_files) >= 5:
                    break
        except OSError:
            # why: the archive is a display convenience; an unreadable
            # directory must not cost the whole MEMORY.md rewrite.
            archive_files = []
        archive_files = archive_files[:5]
        if archive_files:
            lines += ["## Recent Archives", ""]
            for af in archive_files:
                # neutralize_inline like every other value under the comment
                # above: this one is a FILENAME off disk, and `<` is a legal
                # POSIX filename character, so the "every interpolated value
                # below is neutralised" claim was false for exactly one slot.
                rel = neutralize_inline(af.relative_to(memory_dir).as_posix())
                lines.append(f"- `memory/{rel}`")
            lines.append("")

    lines += [
        "---",
        "*Query:        `python -m cc_memory.cli.mem --project <path> stats`*",
        "*Consolidate:  `python -m cc_memory.cli.mem --project <path> consolidate`*",
        "*Anti-patch contract:  see `docs/CONTRACTS.md#anti-patch-contract`*",
    ]
    # Assembled sweep — measured forgery across two adjacent topic labels
    # in the "## Memory Distribution" list.
    return neutralize_document("\n".join(lines))


def _write_memory_index(memory_dir: Path, rendered: str) -> None:
    try:
        # A wall-clock budget, not the default try count: a failure here is
        # swallowed below, so it costs a STALE MEMORY.md rather than an error
        # the caller sees. See core/atomic.py:_DERIVED_BUDGET_S.
        _atomic_write_text(memory_dir / "MEMORY.md", rendered,
                           budget_s=_DERIVED_BUDGET_S)
    except OSError as e:
        # why: MEMORY.md is a projection of the DB, which is already committed
        # when this runs, and this function is called from the Stop-hook idle
        # reorg, PreCompact and every batch upsert. A raise here would abort a
        # save whose rows are already stored. v2.5.2 got the same effect by
        # falling back to a truncating write — i.e. it kept the artifact by
        # making it torn. The previous COMPLETE file stays instead, and the
        # next upsert regenerates it. See core/atomic.py.
        _log.error(f"MEMORY.md not regenerated ({e}); previous file intact")
