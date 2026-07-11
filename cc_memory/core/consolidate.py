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
import time
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


# ── Shared substrate (v2.3): decodability, aging, budget gate ───────────────

def is_decodable(content: str) -> bool:
    """True if content is clean enough to feed an LLM judge / similarity match.

    Rejects rows dominated by the U+FFFD replacement char (real corruption).
    Valid CJK / unicode is fine — we only guard against genuine mojibake.
    Verified on the live DB: 0 U+FFFD in memories/topics; this is a forward
    guard so future corruption can't poison dedup/staleness similarity.
    """
    if not content:
        return False
    n = len(content)
    fffd = content.count("�")
    return (fffd / n) < 0.10 if n else False


def effective_age_days(row: dict, now: Optional[datetime] = None) -> float:
    """Age in days from the more-recent of last_referenced_at / created_at.

    Keys on created_at (immutable) NOT updated_at — maintenance ops bump
    updated_at and would corrupt the signal. A memory injected into a recent
    session (last_referenced_at set) is treated as young.
    """
    now = now or datetime.now()
    ref = (row.get("last_referenced_at") or row.get("created_at") or "")
    if not ref:
        return 0.0
    try:
        ts = datetime.fromisoformat(ref)
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, (now - ts).total_seconds() / 86400.0)


class BudgetGate:
    """Residual-time budget for time-boxed LLM calls.

    The caller passes `total_s` as a sub-budget that MUST sit below the host's
    hard timeout, since this gate can only refuse to START a call, not
    interrupt one in flight. Consolidation runs in the `async` PreCompact hook
    (consolidate_async.py, timeout 300s in hooks/hooks.json) — off the blocking
    compaction path since v2.3.2 — with total_s=240; the manual `/cc-mem
    consolidate` path builds an UNBOUNDED gate.

    Correctness guarantee: every budgeted stage passes `can_spend(cost)` the
    TRUE worst-case wall-clock of one call (_worst_call_cost = haiku_timeout +
    ollama_fallback_timeout, both capped via call_llm(fallback_timeout=...)).
    Because the gate only starts a call when `remaining() >= cost`, the last
    call it allows finishes no later than `total_s - safety_s`. So as long as
    total_s - safety_s < the hook's hard timeout, the worker can NEVER be killed
    mid-write. (The pre-v2.3.2 bug: costs were a flat 20s while a real call
    could run haiku_timeout + min(3*timeout,120) ≈ 120s, so a call the gate
    "allowed" overran the 120s ceiling → "Hook cancelled".)
    """

    def __init__(self, total_s: float = 45.0, safety_s: float = 8.0,
                 unbounded: bool = False, start: Optional[float] = None):
        self.unbounded = unbounded
        self.total_s = total_s
        self.safety_s = safety_s
        # `start` lets a hook pass its OWN entry time so elapsed() reflects
        # time already spent (e.g. PreCompact extraction before consolidation).
        self._start = start if start is not None else time.monotonic()

    @classmethod
    def unbounded_gate(cls):
        return cls(unbounded=True)

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def remaining(self) -> float:
        if self.unbounded:
            return float("inf")
        return self.total_s - self.elapsed() - self.safety_s

    def can_spend(self, cost_s: float) -> bool:
        return self.unbounded or self.remaining() >= cost_s


# Budgeted LLM call bounds. Each budgeted stage caps BOTH call_llm legs so the
# gate knows the exact worst-case wall-clock of one call up front (see
# BudgetGate docstring). Keep haiku+fallback per stage well under total_s so
# several calls fit in one run.
_JUDGE_HAIKU_S,   _JUDGE_FALLBACK_S   = 20, 20   # semantic_dedup / obsolete judges
_SUMMARY_HAIKU_S, _SUMMARY_FALLBACK_S = 25, 20   # consolidate_topics summaries


def _worst_call_cost(haiku_s: int, fallback_s: int) -> float:
    """Max wall-clock one budgeted call_llm can consume: Haiku hang to its
    timeout THEN the capped Ollama fallback. This is the cost a BudgetGate
    must reserve before starting the call for its deadline guarantee to hold."""
    return float(haiku_s + fallback_s)


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


# ── 2b. Semantic de-duplication (word-Jaccard nominate + LLM judge) ─────────
# The lexical trigram dedup above (and upsert_smart) only catch near-verbatim
# restatement. The SAME fact reworded across sessions scores <0.5 trigram and
# accumulates as separate rows — the "shit mountain". This stage nominates
# candidate PAIRS by WORD-overlap (coarser, catches rewording), groups them
# conservatively (NO transitive union-find — that produced a 21-node mega-blob
# on the live DB), and asks Haiku to confirm before archiving. Same-category
# only; decodable only; survivor keeps history via supersedes_id.

def _word_set(text):
    return set(re.findall(r"[a-z0-9_]{3,}", (text or "").lower()))


def _word_jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _nominate_groups(memories, floor=0.30, max_group=4, max_groups=12):
    """Form small same-category candidate groups from high word-Jaccard pairs.

    Greedy, bounded: start each group from the highest-scoring unused pair,
    extend ONLY with members that exceed `floor` against EVERY current member
    (no transitive chaining through hub tokens). Caps group size and count.
    Returns list of groups (each a list of memory dicts, len 2..max_group).
    """
    by_cat = defaultdict(list)
    for m in memories:
        if is_decodable(m["content"]):
            by_cat[m["category"]].append(m)

    pairs = []
    wsets = {}
    for cat, mems in by_cat.items():
        for m in mems:
            wsets[m["id"]] = _word_set(m["content"])
        for i in range(len(mems)):
            for j in range(i + 1, len(mems)):
                s = _word_jaccard(wsets[mems[i]["id"]], wsets[mems[j]["id"]])
                if s >= floor:
                    pairs.append((s, mems[i], mems[j], cat))
    pairs.sort(key=lambda p: -p[0])

    used = set()
    groups = []
    for s, a, b, cat in pairs:
        if len(groups) >= max_groups:
            break
        if a["id"] in used or b["id"] in used:
            continue
        group = [a, b]
        gids = {a["id"], b["id"]}
        # try to extend within same category, all-pairwise >= floor
        for m in by_cat[cat]:
            if len(group) >= max_group:
                break
            if m["id"] in used or m["id"] in gids:
                continue
            if all(_word_jaccard(wsets[m["id"]], wsets[g["id"]]) >= floor
                   for g in group):
                group.append(m)
                gids.add(m["id"])
        for gid in gids:
            used.add(gid)
        groups.append(group)
    return groups


_DEDUP_JUDGE_PROMPT = """\
You are de-duplicating a project's memory database. You are given a small group \
of memories that are all the SAME category and lexically similar. Decide whether \
they state the SAME underlying fact (just reworded / re-discovered across sessions).

Output ONLY a JSON object, no markdown:
{"duplicates": true|false, "canonical_content": "<the single best merged statement, \
self-contained, preserving every specific value/path/number from the duplicates>", \
"reason": "<one short sentence>"}

Rules:
- duplicates=true ONLY if they are genuinely the same fact. If any member adds a \
DISTINCT fact (different file, different decision, different number), output false.
- canonical_content must preserve ALL specific values from every duplicate member.
- Be conservative: when unsure, output false (keep them separate)."""


def _judge_group_llm(group, api_key):
    """Ask Haiku whether a group is one fact. Returns dict or None on failure."""
    import json as _json
    mem_text = "\n".join(
        f"[{i}] (id={m['id']}, imp={m['importance']}) {m['content']}"
        for i, m in enumerate(group)
    )
    try:
        from llm.ccl_backend import call_llm
        raw = call_llm(
            _DEDUP_JUDGE_PROMPT,
            f"Memories (same category '{group[0]['category']}'):\n\n{mem_text}",
            api_key, max_tokens=400,
            timeout=_JUDGE_HAIKU_S, fallback_timeout=_JUDGE_FALLBACK_S,
        )
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1] if "```" in raw[3:] else raw.strip("`")
            raw = raw.lstrip("json").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < 0:
            return None
        return _json.loads(raw[start:end + 1])
    except Exception as e:
        _log.error(f"dedup judge error: {e}")
        return None


def semantic_dedup(db, project_id, budget=None, use_llm=True,
                   max_groups=12, dry_run=False):
    """LLM-judged semantic de-duplication. Conservative + recoverable.

    For each confirmed-duplicate group: survivor = max importance, then oldest
    (lowest id); its content is updated to the LLM's canonical_content; the
    others are archived (is_active=0) with supersedes_id -> survivor so the
    lineage is preserved and recoverable.

    Returns {"groups_judged": N, "memories_archived": N, "proposals": [...]}.
    Gated by `budget` (BudgetGate) and no-ops without an API key.
    """
    from core.auth import get_api_key
    budget = budget or BudgetGate.unbounded_gate()
    result = {"groups_judged": 0, "memories_archived": 0, "proposals": []}

    if not use_llm:
        return result
    api_key, _ = get_api_key()
    if not api_key:
        return result

    memories = db.get_all_active_memories(project_id)
    groups = _nominate_groups(memories, max_groups=max_groups)
    if not groups:
        return result

    PER_CALL_COST = _worst_call_cost(_JUDGE_HAIKU_S, _JUDGE_FALLBACK_S)
    for group in groups:
        if not budget.can_spend(PER_CALL_COST):
            _log.info("dedup: budget exhausted, deferring remaining groups")
            break
        verdict = _judge_group_llm(group, api_key)
        result["groups_judged"] += 1
        if not verdict or not verdict.get("duplicates"):
            continue
        canonical = (verdict.get("canonical_content") or "").strip()
        if not canonical or len(canonical) < 10:
            continue
        survivor = max(group, key=lambda m: (m["importance"], -m["id"]))
        losers = [m["id"] for m in group if m["id"] != survivor["id"]]
        proposal = {
            "survivor": survivor["id"],
            "archived": losers,
            "reason": verdict.get("reason", ""),
            "canonical": canonical[:200],
        }
        result["proposals"].append(proposal)
        _log.info(f"dedup group -> keep #{survivor['id']}, archive {losers}: "
                  f"{verdict.get('reason','')}")
        if dry_run:
            continue
        # Apply: refresh survivor content to the merged canonical, tag, then
        # archive losers pointing forward to the survivor.
        existing_tags = []
        try:
            import json as _json
            existing_tags = _json.loads(survivor.get("tags") or "[]")
        except (ValueError, TypeError):
            existing_tags = []
        db.update_memory(
            survivor["id"], content=canonical,
            tags=list(set(existing_tags + ["llm-dedup", "merged"])),
        )
        n = db.archive_obsolete(losers, canonical_id=survivor["id"])
        result["memories_archived"] += n

    return result


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


def _normalize_topic(t):
    import unicodedata
    t = unicodedata.normalize("NFKC", (t or "")).strip().lower()
    t = re.sub(r"[\s_]+", "-", t)
    t = re.sub(r"-+", "-", t).strip("-")
    return t


def _topic_tokens(norm_key):
    return {tok for tok in re.split(r"[-\s_]+", norm_key) if tok}


def canonicalize_topics(db, project_id):
    """Merge fragmented topic LABELS (e.g. 'cc-memory','cc-memory backend',
    'cc-memory-fixes' -> 'cc-memory') so topic-based views are coherent.

    CONSERVATIVE pairwise only: requires word-Jaccard>=0.6 on topic tokens AND
    refuses to merge anything whose normalized key is a single bare token
    (bare tokens like 'memory'/'git' are hubs that chain unrelated topics —
    the live DB's 26-node blow-up). Does NOT chain transitively through a hub.
    Re-points memories to the canonical label (the variant with most memories).
    DECOUPLED from archiving: only relabels, never removes a memory.
    Returns the number of variant topics merged away.
    """
    counts = db.get_topic_memory_counts(project_id)
    topics = [t for t in counts if t and t != "_unassigned"]
    if len(topics) < 2:
        return 0

    # normalized key -> original variants
    norm_to_orig = defaultdict(list)
    for t in topics:
        norm_to_orig[_normalize_topic(t)].append(t)
    keys = list(norm_to_orig.keys())

    parent = {k: k for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(keys)):
        ta = _topic_tokens(keys[i])
        if len(ta) <= 1:
            continue  # refuse bare-token hub merges
        for j in range(i + 1, len(keys)):
            tb = _topic_tokens(keys[j])
            if len(tb) <= 1:
                continue
            if _word_jaccard(ta, tb) >= 0.6:
                union(keys[i], keys[j])

    # cluster -> all original variants; pick canonical = most memories
    clusters = defaultdict(list)
    for k in keys:
        clusters[find(k)].extend(norm_to_orig[k])

    merged = 0
    for _, variants in clusters.items():
        if len(variants) < 2:
            continue
        canonical = max(variants, key=lambda t: (counts.get(t, 0), -len(t)))
        for v in variants:
            if v == canonical:
                continue
            ids = [m["id"] for m in db.get_memories_by_topic(project_id, v)]
            if ids:
                db.bulk_set_topic(ids, canonical)
                merged += 1
    return merged


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
            api_key, max_tokens=500,
            timeout=_SUMMARY_HAIKU_S, fallback_timeout=_SUMMARY_FALLBACK_S,
        )
        return text.strip() if text.strip() else None
    except Exception as e:
        _log.error(f"consolidation LLM error for {topic_name}: {e}")
        return None


def _summarize_topic_fallback(topic_name, memories):
    """No-LLM bullet summary."""
    sorted_mems = sorted(memories, key=lambda m: -m["importance"])
    return "\n".join(f"- {m['content']}" for m in sorted_mems[:8])


def consolidate_topics(db, project_id, use_llm=True, min_memories_per_topic=3,
                       budget=None):
    """Summarize each topic (>=min_memories) into the topics table.

    Budget-gated (v2.3.2): the LLM summary is only attempted while the gate can
    cover a full worst-case call; once exhausted, the topic falls back to the
    deterministic no-LLM summary so it is still refreshed (never skipped) and
    the worker never STARTS a call it can't finish before its deadline. This
    closes the pre-v2.3.2 hole where this stage was the one ungated LLM loop
    and overran the PreCompact hook timeout on large DBs → "Hook cancelled".
    """
    budget = budget or BudgetGate.unbounded_gate()
    PER_CALL_COST = _worst_call_cost(_SUMMARY_HAIKU_S, _SUMMARY_FALLBACK_S)
    all_memories = db.get_all_active_memories(project_id)
    by_topic: Dict[str, List[Dict]] = defaultdict(list)
    for m in all_memories:
        topic = m.get("topic") or "_unassigned"
        by_topic[topic].append(m)

    n_consolidated = 0
    n_deferred_llm = 0
    for topic, memories in by_topic.items():
        if topic == "_unassigned":
            continue
        if len(memories) < min_memories_per_topic:
            continue
        summary = None
        if use_llm and budget.can_spend(PER_CALL_COST):
            summary = _summarize_topic_llm(topic, memories)
        if not summary:
            if use_llm and not budget.can_spend(PER_CALL_COST):
                n_deferred_llm += 1
            summary = _summarize_topic_fallback(topic, memories)
        db.upsert_topic(project_id, topic, summary)
        n_consolidated += 1

    if n_deferred_llm:
        _log.info(f"consolidate_topics: budget exhausted, {n_deferred_llm} "
                  f"topic(s) used the no-LLM fallback summary")
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


# ── 5b. Staleness net + LLM obsolescence ────────────────────────────────────
# decay_importance only LOWERS importance; stale/contradicted facts live
# forever as is_active=1 (e.g. old 2-hook arch, "uninstalled cc-memory").
# Two layers:
#   (A) SQL safety net — zero false-archive: only very old + low-importance +
#       NEVER-injected rows. On a healthy DB this archives ~nothing; it's a
#       slow backstop, not the primary mechanism.
#   (B) LLM obsolescence — the precision instrument: per category, show the
#       OLDEST and NEWEST rows together so old-vs-new contradictions co-occur,
#       and let Haiku name {stale_id, current_id} pairs. Archives stale via
#       archive_obsolete (no new row — never duplicates the survivor).

def decay_and_archive(db, project_id, decay_age_days=30, archive_age_days=180):
    """Reference-aware importance decay + a zero-false-archive staleness net.

    Importance decay uses effective age (created_at / last_referenced_at), so
    a fact injected into a recent session is treated as young and not decayed.
    The archive net only touches rows that are simultaneously very old, low
    importance, AND never injected — the safest possible signal.
    Returns {"importance_decayed": N, "archived_stale": N}.
    """
    now = datetime.now()
    referenced = db.get_referenced_id_set(project_id)
    mems = db.get_all_active_memories(project_id)

    decayed = 0
    archived = []
    for m in mems:
        age = effective_age_days(m, now)
        # (A) archive net: very old AND low-importance AND never injected
        if age > archive_age_days and m["importance"] <= 2 and m["id"] not in referenced:
            archived.append(m["id"])
            continue
        # (B) reference-aware importance decay (lower, don't remove)
        imp = m["importance"]
        new_imp = imp
        if imp == 5 and age > decay_age_days * 2:
            new_imp = 4
        elif imp == 4 and age > decay_age_days * 4:
            new_imp = 3
        if new_imp != imp:
            db.update_importance(m["id"], new_imp)
            decayed += 1

    n_arch = db.archive_obsolete(archived) if archived else 0
    return {"importance_decayed": decayed, "archived_stale": n_arch}


_OBSOLETE_PROMPT = """\
You are auditing a project's memory database for OBSOLETE facts. You are given \
memories from ONE category. Some older memories may be DIRECTLY CONTRADICTED by \
a newer one that states the SAME attribute differently (e.g. "X has 2 hooks" \
then later "X has 5 hooks" → the "2 hooks" fact is obsolete).

Output ONLY a JSON array, no markdown:
[{"stale_id": <id of the outdated memory>, "current_id": <id of the NEWER memory \
that directly contradicts it>, "reason": "<one short sentence>"}]

STRICT rules:
- Report a pair ONLY when the newer memory states the SAME attribute with a \
DIFFERENT value, making the old value factually WRONG now. Reworded-but-still-\
true is NOT obsolete.
- An ACTION or EVENT (uninstalled / deleted / removed / fixed / created / \
reverted X) does NOT by itself make descriptive facts about X obsolete. Code, \
files, plans, and config facts remain valid even if a one-time action mentions \
them — actions and the things they act on coexist. Do NOT use an event memory \
as the current_id that obsoletes a descriptive fact.
- A historical event accurately recorded is NEVER obsolete on its own.
- When in doubt, OMIT the pair. Be conservative: empty array [] is a fine answer."""


def detect_obsolete_llm(db, project_id, budget=None, use_llm=True,
                        per_category=8, dry_run=False):
    """LLM contradiction/obsolescence detection. Returns
    {"pairs_found": N, "archived": N, "proposals": [...]}. No-op without key."""
    import json as _json
    from core.auth import get_api_key
    budget = budget or BudgetGate.unbounded_gate()
    result = {"pairs_found": 0, "archived": 0, "proposals": []}
    if not use_llm:
        return result
    api_key, _ = get_api_key()
    if not api_key:
        return result

    mems = [m for m in db.get_all_active_memories(project_id)
            if is_decodable(m["content"])]
    by_cat = defaultdict(list)
    for m in mems:
        by_cat[m["category"]].append(m)

    PER_CALL_COST = _worst_call_cost(_JUDGE_HAIKU_S, _JUDGE_FALLBACK_S)
    valid_ids = {m["id"] for m in mems}
    # Temporal guard (validated on the live DB): obsolescence flows FORWARD in
    # time — a fact can only be made obsolete by a NEWER one. Without this, the
    # LLM treats a historical EVENT (e.g. "uninstalled cc-memory") as current
    # state and wrongly archives older still-valid facts. Key on created_at
    # with id as tiebreaker.
    created = {m["id"]: (m.get("created_at") or "", m["id"]) for m in mems}
    to_archive = {}  # stale_id -> current_id
    for cat, group in by_cat.items():
        if len(group) < 3:
            continue
        if not budget.can_spend(PER_CALL_COST):
            _log.info("obsolete: budget exhausted, deferring remaining categories")
            break
        # oldest + newest co-present so old-vs-new contradictions are visible
        by_age = sorted(group, key=lambda m: m["created_at"])
        sample = by_age[:per_category] + by_age[-per_category:]
        seen = set()
        sample = [m for m in sample if not (m["id"] in seen or seen.add(m["id"]))]
        mem_text = "\n".join(f"(id={m['id']}) {m['content']}" for m in sample)
        try:
            from llm.ccl_backend import call_llm
            raw = call_llm(
                _OBSOLETE_PROMPT,
                f"Category '{cat}':\n\n{mem_text}",
                api_key, max_tokens=500,
                timeout=_JUDGE_HAIKU_S, fallback_timeout=_JUDGE_FALLBACK_S,
            ).strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip() if "```" in raw[3:] else raw.strip("`")
            s, e = raw.find("["), raw.rfind("]")
            if s < 0 or e < 0:
                continue
            pairs = _json.loads(raw[s:e + 1])
        except Exception as ex:
            _log.error(f"obsolete judge error ({cat}): {ex}")
            continue
        for p in pairs:
            sid, cid = p.get("stale_id"), p.get("current_id")
            if not (sid in valid_ids and cid in valid_ids and sid != cid):
                continue
            # temporal guard: the superseding memory MUST be newer than the
            # one it obsoletes. Rejects "historical event obsoletes older fact".
            if created.get(cid, ("", 0)) <= created.get(sid, ("", 0)):
                _log.info(f"obsolete REJECTED (not newer): #{sid} <- #{cid}")
                continue
            to_archive[sid] = cid
            result["proposals"].append(
                {"stale_id": sid, "current_id": cid, "reason": p.get("reason", "")})
            _log.info(f"obsolete: #{sid} superseded by #{cid}: {p.get('reason','')}")

    result["pairs_found"] = len(to_archive)
    if to_archive and not dry_run:
        # group by canonical for forward-linking
        by_canon = defaultdict(list)
        for sid, cid in to_archive.items():
            by_canon[cid].append(sid)
        for cid, sids in by_canon.items():
            result["archived"] += db.archive_obsolete(sids, canonical_id=cid)
    return result


# ── 6. Archive consolidated (content-near-dup guarded) ──────────────────────
def archive_consolidated(db, project_id, keep_per_topic=5, dup_threshold=0.65):
    """Archive over-the-cap topic members — but ONLY ones that are CONTENT
    near-duplicates (trigram>=dup_threshold) of a KEPT member of the same
    topic. This decouples archiving from topic LABELS: canonicalize_topics may
    merge 'cc-memory backend' into 'cc-memory', but a distinct fact that merely
    shares the label is NEVER archived here — only genuine content redundancy is.
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
        sorted_mems = sorted(memories, key=lambda m: (-m["importance"], m["created_at"]))
        kept = sorted_mems[:keep_per_topic]
        kept_tri = [_trigram_set(k["content"]) for k in kept]
        for m in sorted_mems[keep_per_topic:]:
            mt = _trigram_set(m["content"])
            # only archive if it's near-duplicate of something we're keeping
            if any(_jaccard(mt, kt) >= dup_threshold for kt in kept_tri):
                to_archive.append(m["id"])

    if to_archive:
        db.bulk_archive(to_archive)
    return len(to_archive)


# ── Master orchestration ────────────────────────────────────────────────────
def run_consolidation(cwd, use_llm=True, verbose=True, budget=None):
    """Full consolidation pipeline. Stage order is load-bearing (see comments).

    `budget` (BudgetGate) bounds EVERY LLM stage (semantic_dedup,
    consolidate_topics, detect_obsolete_llm); pass None on the manual CLI path
    for an unbounded gate. Since v2.3.2 this runs in the `async` PreCompact hook
    (consolidate_async.py, timeout 300s) — off the blocking compaction path —
    with a total_s=240 gate. Because each stage reserves the TRUE worst-case
    call cost before starting, the run finishes by total_s - safety_s < 300s,
    so it can never be killed mid-write.
    """
    memory_dir = Path(cwd) / "memory"
    db_path = memory_dir / "memory.db"
    if not db_path.exists():
        if verbose:
            _log.info(f"no DB at {db_path}")
        return {}

    db = MemoryDB(db_path)
    project_id = db.upsert_project(cwd)
    budget = budget or BudgetGate.unbounded_gate()

    results = {}
    # 1. cheap, deterministic, no-LLM cleanup first
    results["garbage_deleted"] = cleanup_garbage(db, project_id)
    # 2. lexical near-dup (verbatim restatement) — content, category-gated
    results["duplicates_archived"] = merge_near_duplicates(db, project_id)
    # 3. SEMANTIC dedup (reworded same-fact) — LLM-judged, budget-gated.
    #    Runs BEFORE topic work so there are fewer rows to relabel/summarize.
    sd = semantic_dedup(db, project_id, budget=budget, use_llm=use_llm)
    results["semantic_dedup_archived"] = sd["memories_archived"]
    # 4. topic assignment then CANONICALIZE labels (relabel only, no archive)
    results["topics_assigned"] = assign_topics_auto(db, project_id)
    results["topics_canonicalized"] = canonicalize_topics(db, project_id)
    # 5. summarize topics into the topics table (budget-gated: LLM while the
    #    gate allows, deterministic fallback once exhausted)
    results["topics_consolidated"] = consolidate_topics(
        db, project_id, use_llm=use_llm, budget=budget)
    # 6. staleness: reference-aware decay + zero-false-archive SQL net
    da = decay_and_archive(db, project_id)
    results["importance_decayed"] = da["importance_decayed"]
    results["archived_stale"] = da["archived_stale"]
    # 7. LLM obsolescence (old-vs-new contradiction) — budget-gated
    ob = detect_obsolete_llm(db, project_id, budget=budget, use_llm=use_llm)
    results["archived_obsolete"] = ob["archived"]
    # 8. archive_consolidated LAST, content-near-dup guarded (label-safe)
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
