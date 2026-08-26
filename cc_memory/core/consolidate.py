"""
Topic-based memory consolidation pipeline.

Pipeline:
  1. cleanup_garbage()           archive known junk patterns (recoverable)
  2. merge_near_duplicates()     fuzzy dedup within active memories (trigram Jaccard)
  3. assign_topics_auto()        keyword-based topic tagging
  4. consolidate_topics()        LLM summarize each topic -> topics table
  5. decay_and_archive()         reference-aware decay + zero-false-archive net
  6. archive_consolidated()      archive memories captured in summaries

Anti-patch design: consolidation is the cleanup *backstop*. The primary
anti-patch mechanism is llm.memory_writer.upsert_smart, which prevents
duplicate insertion at write time. Consolidation handles drift accumulated
from sources that bypass the writer (manual SQL, legacy paths).

v2.1: project-neutral. The previous astrophysics _GROUPS dict has been
removed; topic clusters are derived purely from keyword frequency.
"""
import json
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

    def deadline(self) -> Optional[float]:
        """Absolute time.monotonic() instant a budgeted call must FINISH by
        (total_s - safety_s past this gate's own start), or None when
        unbounded.

        Passed to call_llm(deadline=...) so the bound is enforced as true
        wall-clock INSIDE the leg. `can_spend` alone reserves worst-case cost
        before starting a call but cannot interrupt one in flight — the three
        LLM stages here had the reservation and no in-flight bound (register
        C3: call sites 3, `deadline=` 0), so a single dripping response could
        still carry a stage past the arithmetic the gate's guarantee rests on.
        """
        if self.unbounded:
            return None
        return self._start + self.total_s - self.safety_s


# Budgeted LLM call bounds. Each budgeted stage caps BOTH call_llm legs so the
# gate knows the exact worst-case wall-clock of one call up front (see
# BudgetGate docstring). Keep haiku+fallback per stage well under total_s so
# several calls fit in one run.
_JUDGE_HAIKU_S,   _JUDGE_FALLBACK_S   = 20, 20   # semantic_dedup / obsolete judges
_SUMMARY_HAIKU_S, _SUMMARY_FALLBACK_S = 25, 20   # consolidate_topics summaries


def _worst_call_cost(haiku_s: int, fallback_s: int) -> float:
    """Max wall-clock one budgeted call_llm can consume: up to TWO Anthropic
    candidates (v2.3.4 — env key + OAuth fall-through, each bounded by the
    haiku timeout) THEN the capped Ollama fallback (0 when ccl.enabled=false,
    but reserving it keeps the guarantee safe in both configurations). This is
    the cost a BudgetGate must reserve before starting the call for its
    deadline guarantee to hold."""
    return float(2 * haiku_s + fallback_s)


# ── 1. Garbage cleanup ─────────────────────────────────────────────────────
# NOT a security control, and the `system-reminder|antml` entry below must not
# be read as one. These patterns delete low-value transcript NOISE during
# consolidation; they are anchored at position 0, so one leading word evades
# every one of them. The marker defence is core.privacy.neutralize_markers,
# applied on the write path (clean_for_storage) and again on every render path.
# Do not "harden" this list in place of that: an unanchored regex here would
# silently DELETE memories that legitimately quote a tag, which is the opposite
# of what the escape-don't-delete design is for.
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


def _min_content_len():
    """The ONE length floor, taken from the writer that enforces it.

    This used to be a second literal, `_MIN_CONTENT_LEN = 20`, against the
    writer's 10 — so the janitor destroyed what four surfaces had just
    accepted. `/cc-mem add note "lr=3e-4 wins"` (12 chars) printed
    `[inserted] #1`, appeared in MEMORY.md, and was gone five turns later.
    Import it; a copied threshold is a threshold that drifts.
    """
    try:
        from llm.memory_writer import MIN_CONTENT_LEN
        return MIN_CONTENT_LEN
    except Exception:
        # why: consolidation must still run if the writer cannot be imported;
        # 10 is that module's value, restated only for this unreachable path
        return 10


def cleanup_garbage(db, project_id):
    """Archive transcript noise. NEVER deletes — see `core/db.py`'s contract.

    `db.py:176-180` states that every delete path must archive, because a hard
    DELETE strands any `supersedes_id` pointing at the row and nothing catches
    it; `delete_memories()` is reserved there for USER-DRIVEN purges. This
    function is neither user-driven nor a purge — it runs unattended from the
    Stop hook every 5 turns (`core/idle.py`) and as stage 1 of every
    consolidation — and it was `delete_memories`'s only caller in the tree.
    Archived rows stay recoverable and keep the supersede chain walkable.
    """
    floor = _min_content_len()
    memories = db.get_all_active_memories(project_id)
    to_archive = []
    for m in memories:
        content = m["content"].strip()
        if len(content) < floor:
            to_archive.append((m["id"], m["content"]))
            continue
        if any(pat.search(content) for pat in _GARBAGE_RE):
            to_archive.append((m["id"], m["content"]))
            continue
    if to_archive:
        # Conditional on content_hash, not a blind bulk_archive: this runs
        # unattended from the Stop hook's idle reorg CONCURRENT with the
        # PreCompact writer, and the verdict above was computed from a
        # snapshot. A row whose garbage content was repaired (merged into)
        # between the read and this write used to be archived anyway —
        # measured: the freshly-written good content went inactive. The hash
        # guard makes the write apply only to the content the verdict saw.
        return db.archive_if_unchanged(to_archive)
    return 0


# ── 2. Near-duplicate merging (shingle Jaccard) ─────────────────────────────
# ONE substrate (core/textsim.py) shared with llm/memory_writer.py and
# core/plan.py. The private English-only trigram copy this replaced made every
# CJK near-duplicate invisible to this stage (see textsim's module docstring
# for the measured collapse); the aliases keep the call sites readable.
from core.textsim import jaccard as _jaccard, shingle_set as _trigram_set


# Row cap for the pairwise stages. `BudgetGate` bounds the three LLM stages in
# SECONDS and nothing bounds these in anything: N(N-1)/2 Jaccard comparisons
# over ~200-element trigram sets. At the 3,725-memory project this codebase has
# measured in the field that is ~6.9M comparisons before the first network
# call, and the async worker's 300 s hook timeout kills the process mid-stage —
# which runs no `finally`, so neither the cadence marker nor the lock is
# released and the identical doomed run is re-attempted at every compaction
# from then on. Bounding by ROWS is the analogue of bounding the LLM legs by
# seconds.
#
# The slice is not arbitrary: `get_all_active_memories` orders by
# `topic, importance DESC, created_at DESC`, so taking the head keeps whole
# topic runs together with each topic's most important rows first — and
# near-duplicates cluster inside a topic, which is exactly what this stage
# looks for. Only the topic straddling the boundary is split.
_MAX_PAIRWISE_ROWS = 1500


def merge_near_duplicates(db, project_id, threshold=0.65):
    memories = db.get_all_active_memories(project_id)
    if len(memories) < 2:
        return 0
    if len(memories) > _MAX_PAIRWISE_ROWS:
        # Newest-UPDATED slice, not the alphabetical head (register r6-A11):
        # a fixed (topic, importance) head re-examined the SAME 1500 rows on
        # every run, so a duplicate pair in a late-alphabet topic was never
        # compared, permanently. Recency-of-change is where fresh duplicates
        # appear, and archived rows leave the active set — so over runs the
        # slice ROTATES through the population instead of pinning to one end.
        # A heuristic (updated_at is wall time), not an identity; the id
        # tiebreak keeps it deterministic within one second.
        _log.info(f"merge_near_duplicates: {len(memories)} active rows, "
                  f"comparing the {_MAX_PAIRWISE_ROWS} most recently updated")
        memories = sorted(memories,
                          key=lambda m: (m.get("updated_at") or "", m["id"]),
                          reverse=True)[:_MAX_PAIRWISE_ROWS]

    trigrams = [(m, _trigram_set(m["content"])) for m in memories]
    to_archive = set()
    content_of = {m["id"]: m["content"] for m in memories}

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
                    # Survivor by id, NOT by the created_at STRING: `_now()`
                    # is naive local wall time, which repeats an hour at DST
                    # fall-back and steps back on NTP corrections — the
                    # "newer by clock" row was the OLDER fact, so the
                    # CORRECTION was archived and the superseded wording
                    # kept (register X8, measured at sim 0.9531 after a
                    # stepped-back clock). id is creation order by
                    # construction; keep the higher one.
                    to_archive.add(min(mi["id"], mj["id"]))
                if mi["id"] in to_archive:
                    # The ANCHOR just lost. Jaccard is not transitive — the
                    # outer loop's guard skips a doomed anchor at entry, but
                    # kept comparing one doomed MID-SCAN: a later row similar
                    # only to the doomed anchor (0.61 to the survivor, under
                    # threshold) was archived on its authority, leaving the
                    # active set with no near-duplicate of it. Same failure
                    # _nominate_groups documents avoiding ("NO transitive
                    # union-find"). Stop; the outer loop re-anchors.
                    break

    if to_archive:
        # Hash-guarded, same rationale as cleanup_garbage: the pairwise loop
        # above can run for seconds on a large set, and a row rewritten by a
        # concurrent PreCompact merge in that window is no longer the row this
        # verdict judged near-duplicate.
        return db.archive_if_unchanged(
            [(mid, content_of[mid]) for mid in to_archive])
    return 0


# ── 2b. Semantic de-duplication (word-Jaccard nominate + LLM judge) ─────────
# The lexical trigram dedup above (and upsert_smart) only catch near-verbatim
# restatement. The SAME fact reworded across sessions scores <0.5 trigram and
# accumulates as separate rows — the "shit mountain". This stage nominates
# candidate PAIRS by WORD-overlap (coarser, catches rewording), groups them
# conservatively (NO transitive union-find — that produced a 21-node mega-blob
# on the live DB), and asks Haiku to confirm before archiving. Same-category
# only; decodable only; survivor keeps history via supersedes_id.

# CJK-aware word sets (core/textsim.py). The old `[a-z0-9_]{3,}` grammar here
# produced an EMPTY set for a pure-CJK memory, so word-Jaccard returned 0.0
# and a Chinese duplicate could never even be NOMINATED to the LLM judge —
# the stage existed and simply never saw the rows it was for.
from core.textsim import word_set as _word_set
_word_jaccard = _jaccard


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


def _judge_group_llm(group, api_key, deadline=None):
    """Ask Haiku whether a group is one fact. Returns dict or None on failure.

    `deadline` is the caller's BudgetGate.deadline() — enforced as true
    wall-clock inside the leg (register C3)."""
    import json as _json
    mem_text = "\n".join(
        f"[{i}] (id={m['id']}, imp={m['importance']}) {m['content']}"
        for i, m in enumerate(group)
    )
    try:
        from llm.ccl_backend import call_llm
        from llm.parse import extract_json
        raw = call_llm(
            _DEDUP_JUDGE_PROMPT,
            f"Memories (same category '{group[0]['category']}'):\n\n{mem_text}",
            api_key, max_tokens=400,
            timeout=_JUDGE_HAIKU_S, fallback_timeout=_JUDGE_FALLBACK_S,
            deadline=deadline,
        )
        return extract_json(raw, kind="object")
    except Exception as e:
        _log.error(f"dedup judge error: {e}")
        return None


def semantic_dedup(db, project_id, budget=None, use_llm=True,
                   max_groups=12, dry_run=False, skip_signatures=None):
    """LLM-judged semantic de-duplication. Conservative + recoverable.

    For each confirmed-duplicate group: survivor = max importance, then oldest
    (lowest id); its content is updated to the LLM's canonical_content; the
    others are archived (is_active=0) with supersedes_id -> survivor so the
    lineage is preserved and recoverable.

    `skip_signatures` (v2.12.0) is `deep_dedup`'s convergence state: a
    mutable set of frozenset(member-ids) this run has already put in front
    of the judge. Nomination is deterministic, so without it every extra
    round re-judged the same refused groups forever and "run until dry"
    could never terminate. When supplied, nomination over-fetches by the
    number of already-seen signatures so the cap cannot mask unseen groups,
    already-seen groups are filtered out, and every group actually sent to
    the judge is recorded — including ones whose verdict errors, so a dead
    API ends the loop instead of spinning it.

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
    if len(memories) > _MAX_PAIRWISE_ROWS:
        # `_nominate_groups` is the second pairwise stage and is bounded by the
        # same cap for the same reason: it runs BEFORE the first `can_spend`,
        # so the budget that guards the judge calls cannot reach it. Same
        # newest-updated rotation as merge_near_duplicates (register r6-A11).
        _log.info(f"semantic_dedup: {len(memories)} active rows, nominating "
                  f"from the {_MAX_PAIRWISE_ROWS} most recently updated")
        memories = sorted(memories,
                          key=lambda m: (m.get("updated_at") or "", m["id"]),
                          reverse=True)[:_MAX_PAIRWISE_ROWS]
    nominate_cap = max_groups + (len(skip_signatures)
                                 if skip_signatures else 0)
    groups = _nominate_groups(memories, max_groups=nominate_cap)
    if skip_signatures:
        groups = [g for g in groups
                  if frozenset(m["id"] for m in g) not in skip_signatures]
    groups = groups[:max_groups]
    if not groups:
        return result

    PER_CALL_COST = _worst_call_cost(_JUDGE_HAIKU_S, _JUDGE_FALLBACK_S)
    for group in groups:
        if not budget.can_spend(PER_CALL_COST):
            _log.info("dedup: budget exhausted, deferring remaining groups")
            break
        if skip_signatures is not None:
            skip_signatures.add(frozenset(m["id"] for m in group))
        verdict = _judge_group_llm(group, api_key, deadline=budget.deadline())
        result["groups_judged"] += 1
        if not verdict or not verdict.get("duplicates"):
            continue
        canonical = (verdict.get("canonical_content") or "").strip()
        if not canonical or len(canonical) < 10:
            continue
        # Survivor selection prefers a member whose content ALREADY IS the
        # canonical (register r6-A8): the judge routinely picks one member's
        # text verbatim, and rewriting a DIFFERENT member to it would collide
        # with the still-active hash-twin on the unique index — the
        # IntegrityError path below. When the canonical matches a member,
        # that member survives and the rewrite is a near-no-op.
        canon_hash = MemoryDB.compute_content_hash(canonical)
        hash_twins = [m for m in group
                      if MemoryDB.compute_content_hash(m["content"]) == canon_hash]
        pool = hash_twins or group
        survivor = max(pool, key=lambda m: (m["importance"], -m["id"]))
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
        # Re-read the survivor before writing. The candidate set was read
        # before `_judge_group_llm`, which blocks on the network for tens of
        # seconds, and the Stop hook's idle reorg (`core/idle.py`) mutates the
        # same tables without consulting this worker's lock — that lock is
        # scoped to other consolidation workers, not to the data. If the
        # survivor was archived OR REWRITTEN in that window, the verdict no
        # longer describes it. The is_active-only version of this check let a
        # concurrent correction be clobbered by the stale canonical (register
        # X2: survivor ended as the verdict text, the correction gone).
        fresh = db.get_memory(survivor["id"])
        if (not fresh or not fresh["is_active"]
                or fresh["content"] != survivor["content"]):
            _log.warn(f"dedup group skipped: survivor #{survivor['id']} was "
                      f"archived or rewritten during the judge call")
            continue
        existing_tags = []
        try:
            import json as _json
            existing_tags = _json.loads(fresh.get("tags") or "[]")
        except (ValueError, TypeError):
            existing_tags = []
        # The whole write phase — survivor re-verify, loser archive (losers
        # FIRST, for the active-hash unique index), survivor rewrite — is ONE
        # transaction in db.apply_dedup_verdict. Three separate transactions
        # used to let a survivor write fail AFTER the losers were archived,
        # leaving the group half-applied (the r6 triage's recorded limit).
        loser_contents = {m["id"]: m["content"] for m in group
                          if m["id"] != survivor["id"]}
        outcome = db.apply_dedup_verdict(
            survivor["id"], survivor["content"], canonical,
            list(set(existing_tags + ["llm-dedup", "merged"])),
            losers, loser_contents)
        result["memories_archived"] += outcome["archived"]
        if outcome["skipped"] == "survivor_changed":
            _log.warn(f"dedup group skipped: survivor #{survivor['id']} was "
                      f"rewritten during the judge call; nothing applied")
        elif outcome["skipped"] == "canonical_collision":
            _log.warn(f"dedup: canonical for #{survivor['id']} collides with "
                      f"another active row; survivor keeps its own wording")

    return result


def deep_dedup(db, project_id, use_llm=True, budget=None, max_rounds=50,
               echo=None):
    """Round `semantic_dedup` until it runs dry (v2.12.0 — `consolidate --deep`).

    One `semantic_dedup` pass judges at most `max_groups` (12) groups, which
    is sized for the budget-gated background run — against a real backlog
    (this repository measured 349 unreconciled rows accumulated in one month)
    a single pass is a trickle. This loop exists to pay the backlog down in
    one sitting: each round re-nominates from the post-archive state, the
    shared `skip_signatures` set stops refused groups from being re-judged,
    and the loop ends when a round sends the judge nothing new.

    Bounded three ways, none silent: the `max_rounds` cap and an exhausted
    `budget` are both announced through `echo` (the CLI's narration sink;
    hooks leave it None → file log), and a dead API converges via the
    recorded-even-on-error signatures. Returns
    {"rounds": N, "groups_judged": N, "memories_archived": N}.
    """
    say = echo or _log.info
    seen: set = set()
    totals = {"rounds": 0, "groups_judged": 0, "memories_archived": 0}
    per_call = _worst_call_cost(_JUDGE_HAIKU_S, _JUDGE_FALLBACK_S)
    for _ in range(max_rounds):
        r = semantic_dedup(db, project_id, budget=budget, use_llm=use_llm,
                           skip_signatures=seen)
        totals["rounds"] += 1
        totals["groups_judged"] += r["groups_judged"]
        totals["memories_archived"] += r["memories_archived"]
        say(f"deep dedup round {totals['rounds']}: "
            f"{r['groups_judged']} group(s) judged, "
            f"{r['memories_archived']} archived")
        if r["groups_judged"] == 0:
            break
        if budget is not None and not budget.can_spend(per_call):
            say("deep dedup: budget exhausted before convergence — "
                "re-run to continue")
            break
    else:
        say(f"deep dedup: stopped at the {max_rounds}-round cap "
            f"without convergence — re-run to continue")
    return totals


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

    STAR-SHAPED clustering around a SEED. The contract is "variants of ONE
    canonical label", so each member must clear the 0.6 bar against the
    cluster's most-representative key — most memories, then fewest tokens
    (most general), then shortest — which approximates the canonical.
    Members never recruit, so a sub-bar PAIR can share a cluster only
    through a strong seed ('cc-memory-fixes' + 'cc-memory backend' under
    'cc-memory' is the legitimate case), never through an accidental middle
    key: the previous union-find was transitive despite its own docstring —
    A~B 0.75 and B~C 0.75 chained A to C at 0.50 (register Y6, measured:
    three labels collapsed to one). Bare single-token keys are refused
    entirely (hubs like 'memory'/'git' chain unrelated topics — the live
    DB's 26-node blow-up).

    Re-points memories to the canonical label (the variant with most
    memories), then DELETES the topics-table summary rows of merged-away
    variants: they described a label no memory carries any more, yet stayed
    rendered into MEMORY.md and the SessionStart topics layer as if live
    (register Y6's second half — 3 stranded summaries measured). The
    canonical label's summary is regenerated by consolidate_topics, which
    runs AFTER this stage in run_consolidation.
    DECOUPLED from archiving: relabels and drops summaries, never removes a
    memory. Returns the number of variant topics merged away.
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

    token_of = {k: _topic_tokens(k) for k in keys}
    variant_count = {k: sum(counts.get(v, 0) for v in norm_to_orig[k])
                     for k in keys}
    order = sorted(keys, key=lambda k: (-variant_count[k],
                                        len(token_of[k]), len(k)))
    unplaced = [k for k in order if len(token_of[k]) > 1]  # refuse bare hubs
    clusters = []  # each a list of keys; members >= 0.6 vs their cluster SEED
    while unplaced:
        seed = unplaced.pop(0)
        cl, rest = [seed], []
        for k in unplaced:
            if _word_jaccard(token_of[seed], token_of[k]) >= 0.6:
                cl.append(k)
            else:
                rest.append(k)
        unplaced = rest
        clusters.append(cl)

    summaries = {t["name"] for t in db.get_topics(project_id)}
    merged = 0
    for cluster in clusters:
        variants = []
        for k in cluster:
            variants.extend(norm_to_orig[k])
        if len(variants) < 2:
            continue
        canonical = max(variants, key=lambda t: (counts.get(t, 0), -len(t)))
        for v in variants:
            if v == canonical:
                continue
            ids = [m["id"] for m in db.get_memories_by_topic(project_id, v)]
            if ids or v in summaries:
                # Relabel + summary drop are ONE transaction (the r6 triage
                # recorded the two-commit version as a limit: a kill between
                # them stranded a summary for a label no memory carries).
                db.merge_topic_variant(project_id, ids, canonical,
                                       v if v in summaries else None)
            if ids:
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


def _summarize_topic_llm(topic_name, memories, deadline=None):
    """LLM topic summary. `deadline` = the caller's BudgetGate.deadline(),
    enforced as true wall-clock inside the leg (register C3)."""
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
            deadline=deadline,
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
            summary = _summarize_topic_llm(topic, memories,
                                           deadline=budget.deadline())
        if not summary:
            if use_llm and not budget.can_spend(PER_CALL_COST):
                n_deferred_llm += 1
            summary = _summarize_topic_fallback(topic, memories)
        # `summary` is raw model output from `_summarize_topic_llm`, and
        # `topics.content` is rendered into MEMORY.md, the SessionStart topics
        # layer and the `memory_topics` MCP tool. It was the one post-v2.5.2
        # path writing a rendered column with no write-path gate at all: a
        # memory whose stored form is safely escaped gets re-emitted unescaped
        # by the summarising model, and lands here armed.
        # `summary` ONLY. `topic` is a JOIN KEY: `get_memories_by_topic`
        # matches `memories.topic = ?` on string equality, so escaping it here
        # while a pre-v2.8.0 row still holds the raw value silently orphans
        # that topic — measured, a legacy `build<system-reminder>x` topic went
        # from 1 matching memory to 0. New rows are already clean because
        # `upsert_smart` cleans `topic` on the write path, and every render
        # path escapes at render time, so cleaning the key here buys nothing
        # and breaks the lookup.
        from core.privacy import clean_for_storage
        db.upsert_topic(project_id, topic, clean_for_storage(summary))
        n_consolidated += 1

    if n_deferred_llm:
        _log.info(f"consolidate_topics: budget exhausted, {n_deferred_llm} "
                  f"topic(s) used the no-LLM fallback summary")
    return n_consolidated


# ── 5. Staleness net + LLM obsolescence ─────────────────────────────────────
# (The standalone `decay_importance` this section replaced — updated_at-keyed,
# superseded by decay_and_archive's reference-aware decay in v2.3 — sat here
# dead through v2.8.0 while the module docstring still listed it as pipeline
# stage 5. Deleted; register G10.)
# Importance decay only LOWERS importance; stale/contradicted facts live
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
    archived = {}  # id -> the content the verdict saw (register r6-A7)
    for m in mems:
        age = effective_age_days(m, now)
        # (A) archive net: very old AND low-importance AND never injected
        if age > archive_age_days and m["importance"] <= 2 and m["id"] not in referenced:
            archived[m["id"]] = m["content"]
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

    # ALL THREE snapshot predicates re-asserted in the write's WHERE clause:
    # `require_never_referenced` covers a SessionStart injecting the row
    # mid-verdict (register X3, measured), `expected_contents` covers a
    # concurrent merge writing valuable content into it (register r6-A7 — the
    # reference guard alone still archived a repaired row), and
    # `max_importance` covers an importance-only bump with content unchanged
    # (the r6 triage's recorded limit, closed: the verdict selected on
    # importance <= 2, so the write re-asserts exactly that).
    n_arch = (db.archive_obsolete(list(archived), require_never_referenced=True,
                                  expected_contents=archived,
                                  max_importance=2)
              if archived else 0)
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
    # `contents` feeds archive_obsolete's expected_contents guard below: the
    # verdict is computed from THIS snapshot across a network round-trip, so
    # the write re-asserts it (same contract as the dedup stage).
    contents = {m["id"]: m["content"] for m in mems}
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
            from llm.parse import extract_json
            raw = call_llm(
                _OBSOLETE_PROMPT,
                f"Category '{cat}':\n\n{mem_text}",
                api_key, max_tokens=500,
                timeout=_JUDGE_HAIKU_S, fallback_timeout=_JUDGE_FALLBACK_S,
                deadline=budget.deadline(),
            )
            pairs = extract_json(raw, kind="array")
            if pairs is None:
                continue
        except Exception as ex:
            _log.error(f"obsolete judge error ({cat}): {ex}")
            continue
        for p in pairs:
            sid, cid = p.get("stale_id"), p.get("current_id")
            if not (sid in valid_ids and cid in valid_ids and sid != cid):
                continue
            # Temporal guard (validated on the live DB): obsolescence flows
            # FORWARD — a fact can only be made obsolete by a NEWER one, else
            # the LLM treats a historical EVENT as current state and archives
            # older still-valid facts. Compared on id, not the created_at
            # STRING: naive local wall time repeats/steps back (DST, NTP), so
            # the string order inverted across a clock step and the guard
            # passed exactly the pair it exists to reject (register X8's
            # clock, this stage's guard). id is creation order.
            if cid <= sid:
                _log.info(f"obsolete REJECTED (not newer): #{sid} <- #{cid}")
                continue
            to_archive[sid] = cid
            result["proposals"].append(
                {"stale_id": sid, "current_id": cid, "reason": p.get("reason", "")})
            _log.info(f"obsolete: #{sid} superseded by #{cid}: {p.get('reason','')}")

    result["pairs_found"] = len(to_archive)
    if to_archive and not dry_run:
        # group by canonical for forward-linking; content-guarded because the
        # verdict snapshot predates the judge round-trips (see `contents`)
        by_canon = defaultdict(list)
        for sid, cid in to_archive.items():
            by_canon[cid].append(sid)
        for cid, sids in by_canon.items():
            result["archived"] += db.archive_obsolete(
                sids, canonical_id=cid,
                expected_contents={s: contents[s] for s in sids})
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
        # Tiebreak on id (creation order), not the created_at string — same
        # clock-step reasoning as merge_near_duplicates' survivor choice.
        sorted_mems = sorted(memories, key=lambda m: (-m["importance"], m["id"]))
        kept = sorted_mems[:keep_per_topic]
        kept_tri = [_trigram_set(k["content"]) for k in kept]
        for m in sorted_mems[keep_per_topic:]:
            mt = _trigram_set(m["content"])
            # only archive if it's near-duplicate of something we're keeping
            if any(_jaccard(mt, kt) >= dup_threshold for kt in kept_tri):
                to_archive.append((m["id"], m["content"]))

    if to_archive:
        # Hash-guarded like the other two snapshot-verdict stages above.
        return db.archive_if_unchanged(to_archive)
    return 0


# ── Consolidation cadence: marker + backpressure (v2.12.0) ─────────────────
# Until v2.11.4 consolidation had exactly ONE automatic trigger: the async
# PreCompact leg, gated on "≥ interval sessions since the last run". Both
# halves of that predicate assume compactions happen: a project worked in
# short sessions never compacts, so `sessions` never grows, so consolidation
# never runs — measured on this repository, 349 memories written in one month
# with the last consolidation 17 days old, while the SessionStart injection
# served topic summaries three minor versions stale. The write path
# reconciles per-row (anti-patch), but cross-topic rewordings and topic
# summaries are BATCH work, and batch work needs a trigger that watches the
# batch: rows-since-last-run, not compactions-since-last-run.

# Unconsolidated writes that make the backlog "due" on their own.
BACKLOG_ROWS = 50
# Staleness trigger: due after this many days IF anything new was written at
# all (the floor below) — an idle project must not burn LLM calls on a timer.
BACKLOG_DAYS = 7.0
_BACKLOG_MIN_ROWS_FOR_DAYS = 10


def read_consolidation_marker(memory_dir: Path, cwd: str) -> Dict:
    """memory/.last_consolidation.json, or {} when absent / corrupt / FOREIGN.

    The path check is the same rule consolidate_async enforces (register C4 /
    r6-B8): the marker follows the DIRECTORY but its counters describe a
    project ROW keyed by path, so after a rename the stale marker must read
    as never-run — the price is one early consolidation, async and budgeted.
    """
    try:
        marker = json.loads((memory_dir / ".last_consolidation.json")
                            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(marker, dict):
        return {}
    # normcase, not ==: the hook path writes the cwd Claude Code handed it
    # ("d:\\Projects\\x") while the CLI writes an anchored resolve()
    # ("D:\\Projects\\x"). Windows paths are case-insensitive; this check
    # exists to catch RENAMES, and a case-only mismatch reading as "foreign"
    # would make every manual `/cc-mem consolidate` invisible to the
    # backpressure probe — the exact redundant-run the shared marker writer
    # exists to prevent.
    import os as _os
    if (_os.path.normcase(str(marker.get("project_path") or ""))
            != _os.path.normcase(str(cwd))):
        return {}
    return marker


def write_consolidation_marker(db, project_id, memory_dir: Path, cwd: str,
                               results: Dict) -> Dict:
    """Stamp the cadence marker after a completed consolidation run.

    ONE writer for both the async hook and the manual CLI path (`/cc-mem
    consolidate`) — the CLI never wrote the marker at all before v2.12.0, so
    a hand-run deep clean left the backlog predicate still reading "due" and
    the next Stop hook kicked a redundant background run over a database
    that had just been consolidated. `last_memory_id` is the row-id
    watermark `consolidation_backlog` subtracts against.
    """
    marker = {
        "last_session_count": db.get_session_count(project_id),
        "project_path": str(cwd),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "last_memory_id": db.max_memory_id(project_id),
        "final_active": results.get("final_active"),
        "final_topics": results.get("final_topics"),
        "semantic_dedup_archived": results.get("semantic_dedup_archived"),
        "archived_obsolete": results.get("archived_obsolete"),
    }
    try:
        (memory_dir / ".last_consolidation.json").write_text(
            json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        # why: the marker is cadence bookkeeping; a failed write only means
        # the next trigger may re-consolidate — never a correctness issue.
        _log.error(f".last_consolidation.json write failed: {e}")
    return marker


def consolidation_backlog(db, project_id, marker: Dict,
                          now: Optional[datetime] = None) -> Optional[str]:
    """Why consolidation is due NOW, or None. Pure decision, no side effects.

    Two triggers, both measured against the marker's watermark:
      * ROWS — `BACKLOG_ROWS`+ memories written since the last run: the
        backlog is large enough to be worth a budgeted background pass.
      * DAYS — `BACKLOG_DAYS`+ days since the last run AND at least
        `_BACKLOG_MIN_ROWS_FOR_DAYS` new rows: topic summaries injected at
        SessionStart go stale by TIME, but an idle project has nothing to
        integrate and must not pay for a run on schedule alone.

    A marker with no usable watermark falls back the way
    `count_memories_since` documents (created_at, then the full count):
    a never-consolidated project is one big backlog, which is the reading
    that ends the starvation this predicate exists to end.
    """
    n_new = db.count_memories_since(
        project_id,
        row_id=int(marker.get("last_memory_id") or 0),
        since_ts=str(marker.get("ts") or ""))
    if n_new >= BACKLOG_ROWS:
        return (f"{n_new} unconsolidated memories "
                f"(threshold {BACKLOG_ROWS})")
    if n_new < _BACKLOG_MIN_ROWS_FOR_DAYS:
        return None
    ts = str(marker.get("ts") or "")
    if ts:
        try:
            age_days = ((now or datetime.now())
                        - datetime.fromisoformat(ts)).total_seconds() / 86400.0
        except ValueError:
            age_days = float("inf")
    else:
        age_days = float("inf")
    if age_days >= BACKLOG_DAYS:
        return (f"{n_new} unconsolidated memories and "
                f"{age_days:.0f} day(s) since the last run "
                f"(threshold {BACKLOG_DAYS:.0f})")
    return None


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
            (globals().get("_cli_echo") or _log.info)(f"no DB at {db_path}")  # cli.mem sets _cli_echo=print so `verbose` reaches the user; hooks leave it unset -> log only
        return {}

    db = MemoryDB(db_path)
    project_id = db.upsert_project(cwd)
    budget = budget or BudgetGate.unbounded_gate()

    results = {}
    # 1. cheap, deterministic, no-LLM cleanup first
    # "archived", not "deleted": cleanup_garbage stopped calling
    # delete_memories, and a caller reading `garbage_deleted` would report an
    # irreversible purge for rows that are still recoverable and still on the
    # supersede chain. The key IS the report on every consumer.
    results["garbage_archived"] = cleanup_garbage(db, project_id)
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
        (globals().get("_cli_echo") or _log.info)(  # same dual sink as above: CLI echo when set, log-only in hooks
            f"consolidation done: {stats['n_memories']} active memories, "
            f"{stats['n_topics']} topics"
        )
    return results
