"""
Query-time recall — the second injection channel (v2.15.0).

WHY THIS MODULE EXISTS
======================
cc-memory has TWO moments at which it can put memories in front of Claude, and
until now it used one:

    moment             is there a query?          what it did
    ---------------------------------------------------------------------
    SessionStart       NO (the user has not       six layers ranked by
                       spoken yet)                importance + recency
    UserPromptSubmit   YES (the user just         wrote to the DB; stdout
                       spoke)                     left deliberately EMPTY

So "should this project adopt a vector database" was the wrong question. On the
only automatic path that HAS a query, the channel was empty. The measured
symptom that prompted this — 82.6% of stored memories had never once been
injected — is not a weak ranking function: the selector never knew what the
user was asking about. `UserPromptSubmit` stdout IS injected into Claude's
context (verified against the hook documentation and by driving the hook), and
this plugin was the thing declining to use it.

Retrieval here is FTS5 BM25 plus this project's existing CJK-aware similarity.
No embeddings, no index server, no pip dependency — which is not a compromise
but the point: `CLAUDE.md` § Development guidelines makes pure-stdlib the first
rule, a local SQL query costs milliseconds against an embedding round-trip per
turn, and the same input gives the same output forever, so
`tools/falsify_fixes.py` can drive this red.

THE HONEST BOUNDARY, stated here rather than discovered later
=============================================================
This is LEXICAL recall, not semantic recall. A memory that shares no words with
the prompt is not retrieved. `memories_fts` indexes `content`, `tags` AND
`topic`, so topic/tags give one layer of abstraction, and the `trigram`
tokenizer makes CJK a substring match, which is more forgiving than English
word matching — but a cross-language synonym ("超时" vs "timeout") will not be
found. That limit is documented, not papered over.

It also proves only that a memory was RETRIEVED AS RELEVANT, never that Claude
USED it. `/cc-mem inject-usage` reports the retrieval deterministically, and
`--judge` (layer 2, opt-in — `llm/usage_judge.py`) is what answers the second
question, by reading that session's own replies.

WHAT THE CONSERVATIVE BAR COSTS, measured rather than glossed
============================================================
The floor is set for precision (user decision, 2026-09-07: recall only when
the match is strong), and that is a real trade, not a free one. Three Chinese
prompt/memory pairs a person would call correct matches scored 0.364, 0.400
and 0.667 — so at `RECALL_MIN_RELEVANCE` = 0.45 only the third is emitted,
even though all three are now RETRIEVED. The bar cannot be lowered to catch
them without cost: on this repository's real database an OFF-topic Chinese
prompt also reached 0.400, so 0.364-0.400 is a band where genuine matches and
coincidences are not separable by this metric. Two of three borderline misses
is the price of zero false positives, and the alternative was not "catch them
free" — it was "also emit an unrelated memory on a question about films".
Raise the floor to speak less, lower it to speak more; there is no setting
that does both, and pretending otherwise is how a relevance threshold gets
tuned until its own test passes.

MEASURED FACTS THIS MODULE IS BUILT ON (2026-09-07, sqlite 3.49.1)
=================================================================
1. `textsim.jaccard` CANNOT be the relevance floor. Its denominator is the
   UNION, which grows with the memory's length, so a query fully contained in a
   longer memory still scores near zero: "the PreCompact hook timeout" against
   the row that is literally about it scored jaccard **0.087** and overlap
   **0.960**. A floor set on jaccard would either admit everything or nothing.
   `relevance()` below is therefore the overlap coefficient.
2. A multi-word FTS5 query is an implicit **AND** over its terms, under BOTH
   `trigram` and `unicode61`. Passing a whole user sentence to MATCH returns
   nothing as soon as one word is absent ("the PreCompact hook timeout" → 0
   rows, because the row contains no "the"). A prompt must therefore be
   REDUCED TO TERMS and OR-ed — `build_query` — or the channel silently never
   fires, which is the failure mode hardest to notice.
3. Short noise queries reach rows through the LIKE fallback: `search_fts(pid,
   "ok")` returned 5 rows whose relevance was **0.000** ("ok" is a substring of
   "hooks" and "block"). The signal gate and the relevance floor are both
   load-bearing, and neither is redundant with the other.

The logic core is PURE (v2.10.1 rule 1): `build_query`, `select_recalls` and
`render_recall_block` take plain data and return plain data, so the gate can
drive them headlessly. `hooks/user_prompt.py` is plumbing only.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

from core.privacy import neutralize_inline
from core.textsim import shingle_set, word_set

# ── Budget and bar ─────────────────────────────────────────────────────────
# Deliberately CONSERVATIVE (user decision, 2026-09-07): recall only when the
# relevance is high, at most a few rows, and — the property that matters most —
# EMIT ZERO BYTES when nothing clears the bar. This block is prepended to every
# turn's context, so a channel that speaks when it has nothing to say is a
# per-turn tax on every session forever, and it trains the reader to skip the
# block on the turns it IS right.
#
# These are module constants, not `config.json` keys, for the reason that file
# records in its own notes: a key with no reader is a lie, and v2.5.0 deleted
# 34 of them. Add a key when a user needs to tune it, not in advance.
RECALL_MAX_ROWS = 3
RECALL_BUDGET_CHARS = 1200

# The relevance floor, on the OVERLAP COEFFICIENT (see `relevance`).
#
# MEASURED, not chosen. Twenty-two prompts against this repository's own
# 734-memory database on 2026-09-07 — twelve on-topic for the project, ten
# ordinary questions about anything else, both sets bilingual — scored by
# `relevance()`, best candidate per prompt:
#
#   on-topic  0.000 0.000 0.000 0.458 0.467 0.512 0.543 0.571 0.571 0.594
#             0.655 0.714
#   off-topic 0.000 0.000 0.000 0.186 0.226 0.243 0.286 0.350 0.350 0.400
#
# Coincidence tops out at 0.400, so the bar goes above it:
#
#   floor   on-topic fires   off-topic fires
#   0.40        9/12              1/10      <- a haiku request retrieves a row
#   0.45        9/12              0/10      <- here
#   0.50        7/12              0/10      <- costs recall, buys no precision
#
# 0.45 is the knee: zero false positives across every off-topic prompt tried,
# and raising it only loses true matches. The three on-topic prompts scoring
# 0.000 retrieved NOTHING to score — the honest lexical boundary this module's
# docstring states, not a thresholding failure.
RECALL_MIN_RELEVANCE = 0.45

# The signal gate. A prompt shorter than this, or carrying fewer content words
# than this, is not a question anyone can retrieve against; `search_fts` would
# answer it from the LIKE fallback, which is substring matching on two or three
# characters. Measured: "ok" returned five rows at relevance 0.000.
RECALL_MIN_PROMPT_CHARS = 12
RECALL_MIN_CONTENT_WORDS = 2

# How many candidates BM25 ranks before the floor and the budget cut them down.
RECALL_CANDIDATES = 20

# Terms carried into the FTS query, most distinctive (longest) first. FTS5
# OR-clauses cost little and a prompt's tail words are usually its least
# discriminating, so a cap here is a precision control, not just a budget.
RECALL_MAX_TERMS = 8

# CJK windows are 3 characters, so one Chinese sentence yields far more of them
# than a Latin sentence yields words — they get their own cap rather than
# competing for the Latin budget, or a bilingual prompt would spend every slot
# on one script.
RECALL_MAX_CJK_TERMS = 16

# Words that carry no retrieval signal. Kept SHORT and bilingual on purpose —
# this is a stop list, not a stemmer, and every entry that is not obviously
# contentless is a memory someone cannot find. `word_set` already drops
# sub-3-character Latin words, so this only has to catch the frequent long
# ones. i18n Tier 3: the Chinese entries are intentional and must not be
# reduced to English-only (docs/ARCHITECTURE.md#9-documentation-language-convention-i18n §1).
_STOPWORDS = frozenset("""
    the and for that this with have has had was were are you your from but not
    all can will would should could about into then than them they there here
    what when where which who why how our its it's dont don't just like make
    made does did done get got let use used using please thanks thank
    很 一个 这个 那个 我们 你们 他们 什么 怎么 为什么 可以 应该 已经 还是
    这样 那样 一下 现在 然后 因为 所以 如果 但是 而且 就是 不是 没有 需要
""".split())

# A resume signal is not a query. These are the tokens `hooks/user_prompt.py`
# and `hooks/session_start.py` already share as the RESUME PROTOCOL vocabulary;
# recalling against "继续" would retrieve on the word "continue", which is in
# half the database. i18n Tier 3: bilingual by design — keep in sync with
# `user_prompt.resume_signals` and session_start's RESUME PROTOCOL block.
_NO_QUERY_TOKENS = frozenset({
    "", "继续", "接着", "接着做", "接着干", "继续干",
    "resume", "continue", "go on", "keep going", "ok", "okay", "yes", "no",
    "好", "好的", "行", "对", "是", "嗯", "go", "next", "下一步",
})

# FTS5 treats these as operators / syntax. A term carrying one is quoted, and
# an all-punctuation term is dropped: both forms of `_match_fts`'s expression
# would otherwise raise out of the fts5 parser, and its double-failure branch
# concludes the INDEX is broken and rebuilds it — a read turning into an
# unbounded write on user-chosen input.
_FTS_UNSAFE = re.compile(r'[^\w一-鿿぀-ヿ가-힯]')


def is_query_like(prompt: str) -> bool:
    """Is `prompt` a question worth retrieving against at all?

    The gate is deliberately BEFORE the search rather than after it: a two-
    character prompt reaches real rows through `search_fts`'s LIKE fallback
    (measured — "ok" matched inside "hooks" and "block", five rows, relevance
    0.000), so filtering afterwards would spend a query and rely entirely on
    the floor. Two independent gates, because they fail differently.
    """
    text = (prompt or "").strip()
    if len(text) < RECALL_MIN_PROMPT_CHARS:
        return False
    if text.lower() in _NO_QUERY_TOKENS:
        return False
    return len(_content_words(text)) >= RECALL_MIN_CONTENT_WORDS


def _content_words(text: str) -> List[str]:
    """Distinctive words of `text`, longest first, stop words removed.

    `core.textsim.word_set` is the CJK-aware tokenizer this project already
    uses for nomination; reusing it means a Chinese prompt produces words at
    all (the retired `[a-z0-9_]{3,}` grammar returned an EMPTY set for Chinese
    text, which is why `semantic_dedup` could never nominate a Chinese memory).
    """
    words = {w for w in word_set(text or "") if w.lower() not in _STOPWORDS}
    return sorted(words, key=lambda w: (-len(w), w))


def _cjk_terms(prompt: str) -> List[str]:
    """Sliding 3-character windows of each CJK run in `prompt`.

    THREE characters, because that is the `trigram` tokenizer's floor and the
    index simply cannot match anything shorter — the same documented property
    `/cc-mem search` states for a 1- or 2-character CJK query.

    This is why the CJK branch cannot reuse `_content_words`: `textsim.
    word_set` shingles CJK as BIGRAMS (v2.8.0, deliberately — character
    trigrams collapse on CJK for SIMILARITY scoring), and a query built from
    them is a set of 2-character terms, every one of which is below the
    tokenizer's floor. Measured before this function existed: the 8-term OR
    query built from "连接超时了该怎么办，要不要重试" returned **0 rows**
    against a database containing "连接超时后重试三次…", while searching the
    single term "重试" — reached by the LIKE fallback, not the index —
    returned 1. The whole Chinese recall path retrieved nothing, silently,
    which is the failure mode that looks exactly like "no relevant memories".

    Windows overlap, so a shared 4-character phrase produces two matching
    terms and ranks above a coincidental single window. Precision is NOT this
    function's job — it is the relevance floor's; this one only has to make
    the right rows reachable.
    """
    from core.textsim import CJK_RUN
    windows: List[str] = []
    seen = set()
    for run in CJK_RUN.findall(prompt or ""):
        for i in range(len(run) - 2):
            w = run[i:i + 3]
            if w not in seen:
                seen.add(w)
                windows.append(w)
    return windows


def build_query(prompt: str) -> str:
    """An FTS5 MATCH expression for `prompt`, or "" when there is nothing to ask.

    OR, not AND. This is the whole reason the function exists: FTS5 ANDs the
    terms of a bare multi-word query under BOTH tokenizers, so handing it a
    user sentence returns nothing the moment one word is missing from a row —
    measured, "the PreCompact hook timeout" scored 0 rows against the row that
    is literally about the PreCompact hook timeout, because that row contains
    no "the". A channel that silently never fires is worse than no channel: it
    looks installed.

    Latin terms come from `_content_words` (stop-worded, longest first); CJK
    terms are 3-character windows, because the tokenizer cannot match less —
    see `_cjk_terms`. Both are capped, so a long bilingual prompt cannot build
    an unbounded expression.

    Every term is double-quoted, so a term containing fts5 syntax is a literal
    rather than an operator — and therefore cannot reach `_match_fts`'s
    double-parse-failure branch, which concludes the INDEX is broken and
    REBUILDS it: a read turning into an unbounded write on user-chosen input.
    """
    terms: List[str] = []
    seen = set()
    for word in _content_words(prompt):
        cleaned = _FTS_UNSAFE.sub("", word)
        # 3, not 2: a 2-character Latin term is noise, and a 2-character CJK
        # term cannot match under `trigram` at all. The CJK branch below
        # supplies windows that CAN.
        if len(cleaned) >= 3 and cleaned not in seen:
            seen.add(cleaned)
            terms.append(cleaned)
        if len(terms) >= RECALL_MAX_TERMS:
            break
    for window in _cjk_terms(prompt):
        if len(terms) >= RECALL_MAX_TERMS + RECALL_MAX_CJK_TERMS:
            break
        if window not in seen:
            seen.add(window)
            terms.append(window)
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def relevance(query: str, content: str) -> float:
    """Overlap coefficient of the two CONTENT-WORD shingle sets, in [0, 1].

    Three deliberate choices, each measured rather than assumed:

    1. NOT `textsim.jaccard`. Jaccard divides by the UNION, which grows with
       the memory's length, so a query fully present in a longer memory still
       scores near zero: "the PreCompact hook timeout" against the memory that
       is exactly about it scored jaccard **0.087** and overlap **0.960**. A
       floor on jaccard would admit everything or nothing.

    2. `min(len(a), len(b))`, so the coefficient is SYMMETRIC: it is high when
       the memory is largely about what the prompt says OR the prompt is
       largely about what the memory says. Both are "relevant", and neither
       should be penalised for the other text being longer.

    3. Scored on the CONTENT WORDS, not the raw text. Filler is not evidence
       of relevance in either direction, and it inflates the denominator —
       which hurts most in Chinese, where a natural question carries a lot of
       it. Measured: "连接超时了该怎么办，要不要重试" against the memory
       "连接超时后重试三次，然后回退到本地缓存" — a plainly correct match —
       scored **0.385** on raw text and would have been REFUSED, and scores
       0.571 on content words. Across the same 22-prompt calibration the
       content-word form strictly dominates: at its best the raw form fires on
       8 of 12 on-topic prompts with 0 of 10 false positives, while this one
       fires on 9 of 12 with 0 of 10.
    """
    a = shingle_set(" ".join(_content_words(query)))
    b = shingle_set(" ".join(_content_words(content)))
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def select_recalls(rows: Sequence[Dict], prompt: str,
                   exclude_ids: Optional[Sequence[int]] = None,
                   max_rows: int = RECALL_MAX_ROWS,
                   budget: int = RECALL_BUDGET_CHARS,
                   min_relevance: float = RECALL_MIN_RELEVANCE) -> List[Dict]:
    """The rows worth putting in front of Claude for this prompt. PURE.

    `rows` arrive BM25-ranked from `db.search_fts`. BM25 scores are not
    comparable across queries — the same score means different things for a
    two-word and a ten-word query — so the absolute bar is `relevance()`, and
    BM25 is used only for the ORDER in which candidates are considered.

    `exclude_ids` are the memories Claude has already been shown: what
    SessionStart injected this session (`.last_inject.json`) plus what earlier
    turns already recalled (`.last_recall.json`). Re-injecting a row already
    in the context window spends budget to tell the model something it can
    already see, and is what would make this channel feel like noise.

    An over-budget row is SKIPPED, never a stop — the same rule and the same
    reason as `session_start._LAYER_SKIP_NOTE`: rows are ranked, so a `break`
    lets one oversized row suppress every better row behind it.
    """
    excluded = {int(i) for i in (exclude_ids or [])}
    picked: List[Dict] = []
    used = 0
    for row in rows or []:
        if len(picked) >= max_rows:
            break
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if rid in excluded:
            continue
        content = str(row.get("content") or "")
        if relevance(prompt, content) < min_relevance:
            continue
        cost = len(content) + 24          # the rendered line's fixed overhead
        if used + cost > budget:
            continue                      # skip THIS row, never the rest
        picked.append(row)
        used += cost
    return picked


def render_recall_block(rows: Sequence[Dict], prompt: str = "") -> str:
    """The text handed to Claude, or "" for no rows. PURE.

    THIS IS A RENDER PATH (CLAUDE.md v2.5.2 rule 1). Stored memory content is
    model-writable — `memory_add` is an MCP tool — and this function's output
    goes STRAIGHT into the context window, so every interpolated value is
    `neutralize_inline`d: one line per row, so a newline in stored content
    cannot forge a second entry, and a stored `</system-reminder>` cannot close
    a block it did not open.

    The frame is emitted OUTSIDE that escaping, exactly as
    `session_start.build_context` does it: the frame is the plugin speaking and
    must stay literal, while everything between the frames is stored text and
    must not.

    The wording tells Claude what this block IS — retrieved by lexical match,
    possibly irrelevant — because a block presented as authoritative context
    that happens to be a bad match is worse than one the model can discount.
    """
    if not rows:
        return ""
    lines = [
        "<cc-memory-recall>",
        f"{len(rows)} stored memory(ies) matched this message "
        f"(lexical retrieval — may be irrelevant; ignore if so):",
    ]
    for row in rows:
        cat = neutralize_inline(str(row.get("category") or "note"))
        content = neutralize_inline(str(row.get("content") or ""))
        topic = str(row.get("topic") or "").strip()
        suffix = f"  [topic: {neutralize_inline(topic)}]" if topic else ""
        lines.append(f"  - [{cat}] {content}{suffix}")
    lines.append("</cc-memory-recall>")
    return "\n".join(lines) + "\n"
