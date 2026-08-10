"""One similarity substrate — shingle sets, word sets, Jaccard. CJK-aware.

WHY THIS MODULE EXISTS. Three modules carried byte-identical private copies of
`_trigram_set` / `_jaccard` (llm/memory_writer.py, core/consolidate.py,
core/plan.py) and a fourth primitive (`_word_set`) lived beside one of them.
All four tokenized for English only, and the whole anti-patch contract keys on
their scores:

* Character TRIGRAMS collapse on CJK. A 10-character Chinese fact carries 8
  trigrams and a one-character correction rewrites 3 of them: similarity
  0.4545, where the equivalent one-word English edit scores 0.7317 — measured,
  and reproduced on a live database where a near-verbatim Chinese correction
  scored 0.23 and was INSERTED as an independent fact, leaving two
  contradictory memories active at once. Everything below MID_SIM (0.50) is
  invisible to merge/supersede, so for CJK content the reconcile-on-write
  contract silently degraded to append-only.

* `_word_set` used ``[a-z0-9_]{3,}`` — a pure-ASCII token grammar — so a
  pure-CJK memory produced an EMPTY word set, word-Jaccard returned 0.0, and
  the LLM-judged semantic dedup could never even nominate it.

The fix is granularity per script, not threshold tuning: CJK runs shingle as
character BIGRAMS (a one-character edit in a 10-character run now rewrites 2
of 9 bigrams: similarity 0.636, safely over MID_SIM), while text with no CJK
at all returns byte-identical sets to the old per-module copies, so English
scores are untouched.

**A HIGHER CJK SCORE IS NOT UNIFORMLY SAFE, and this module must not be read
as if it were.** Two consumers use these scores in OPPOSITE safety
directions:

* `llm/memory_writer.py` compares against `MID_SIM` to decide whether to
  MERGE — a higher score merges more, and merging a genuine duplicate is the
  goal. Raising CJK similarity helps.
* `core/plan.py` compares against `CARRYOVER_MATCH_THRESHOLD` to decide
  whether an unfinished plan step was carried into the replacement plan — a
  higher score CARRIES more, and a false carry SILENTLY DROPS a step. That
  gate exists to refuse, so raising CJK similarity hurts.

The first version of this module used one constant for both and measurably
loosened the second: a sweep of 325 one-character CJK substitutions moved 98
from FLAGGED to auto-carried and 0 the other way — including
`把超时设为三十秒` vs `把超时设为六十秒` (thirty vs sixty seconds, opposite
facts) at 0.3333 → 0.5556. `core/plan.py` therefore keeps its OWN
bigram-calibrated bar; see `_carryover_bar` there for the arithmetic. A
future consumer of this module owes the same question: which direction is
the unsafe one for me?

Pure stdlib. Import as ``from core.textsim import shingle_set, jaccard``.
"""
import re

# Han (unified + ext-A + compatibility), kana, and Hangul syllables: the
# scripts where "one glyph ≈ one morpheme" makes trigrams too coarse. Latin,
# Cyrillic, Greek etc. keep trigrams — their edits move letter-by-letter
# inside longer words, which is what trigrams were tuned on.
CJK_RUN = re.compile(
    "["
    "぀-ヿ"   # Hiragana + Katakana
    "㐀-䶿"   # CJK Extension A
    "一-鿿"   # CJK Unified Ideographs
    "豈-﫿"   # CJK Compatibility Ideographs
    "가-힯"   # Hangul syllables
        # Supplementary planes (register D2): a fact written in Extension-B..H
    # ideographs sailed past every CJK mitigation in this module -- a
    # one-character correction to a ten-character Ext-B string scored
    # 0.4545 under the trigram fallback (below MID_SIM, so it was INSERTED
    # beside the fact it corrects) and `word_set` came back EMPTY, so the
    # LLM dedup judge could never even nominate it. Same bigram treatment
    # as the BMP runs; every tuned threshold is untouched.
    "𠀀-𮹝"   # Extensions B-F + I (r6-C5: I begins at U+2EBF0)
    "丽-𯨟"   # Compatibility Supplement
    "𰀀-𲎯"   # Extensions G-H
"]+")


def _ascii_shingles(out, run):
    run = run.strip()
    if not run:
        return
    if len(run) < 3:
        out.add(run)
    else:
        out.update(run[i:i + 3] for i in range(len(run) - 2))


def shingle_set(text):
    """Shingles for similarity: trigrams for non-CJK, bigrams for CJK runs.

    A string with no CJK at all takes the fast path and returns EXACTLY what
    the retired per-module `_trigram_set` copies returned, including the
    whole-short-string set for len < 3 — English similarity scores must not
    move, because HIGH_SIM/MID_SIM and the plan-carryover threshold were all
    tuned against them.
    """
    t = (text or "").lower().strip()
    if not CJK_RUN.search(t):
        if len(t) < 3:
            return {t}
        return {t[i:i + 3] for i in range(len(t) - 2)}
    out = set()
    pos = 0
    for m in CJK_RUN.finditer(t):
        _ascii_shingles(out, t[pos:m.start()])
        run = m.group(0)
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
        pos = m.end()
    _ascii_shingles(out, t[pos:])
    return out or {t}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def word_set(text):
    """Coarse token set for the rewording nominator: words + CJK bigrams.

    The ASCII half keeps the historical grammar (``[a-z0-9_]{3,}``) untouched.
    The CJK half exists because that grammar matches nothing in a Chinese
    sentence: the word set came back empty, `jaccard` returned 0.0 by its
    empty-set guard, and semantic dedup could never nominate the row — so for
    CJK-heavy projects the LLM judge never even saw the duplicates it exists
    to merge. Bigrams (not full runs) so a rewording that keeps the key
    two-character terms still overlaps.

    THE THIRD BRANCH IS THE SAME BUG, ONE SCRIPT FAMILY OVER. ``[a-z0-9_]``
    is not "ASCII plus CJK is everything"; it is "Latin". Measured token
    counts before this branch existed — Cyrillic **0**, Greek **0**, Arabic
    **0**, Hebrew **0**, Thai **0**, Devanagari **0** — against 41 shingles
    for the same Cyrillic sentence, so the row was perfectly comparable and
    simply un-nominatable. Note what this does NOT claim: unlike the CJK case,
    the trigram substrate was never broken for these scripts (two near-identical
    Russian facts score 0.6522, well over ``MID_SIM``), so MERGE and SUPERSEDE
    always worked. The single casualty is `core/consolidate.py`'s LLM-dedup
    nominator, which is `word_set`'s only consumer.

    Unicode letters, and only the non-ASCII ones are added, so a pure-Latin
    input returns EXACTLY the historical set — the property the ASCII
    byte-identity assertion in `smoke_test.py` pins. Scripts with no word
    separator (Thai, Lao, Khmer) yield one token per run, which nominates an
    identical restatement but not a rewording; that is a real remaining limit,
    recorded rather than papered over, and it is strictly better than the zero
    tokens they produced before.
    """
    t = (text or "").lower()
    words = set(re.findall(r"[a-z0-9_]{3,}", t))
    for m in CJK_RUN.finditer(t):
        run = m.group(0)
        if len(run) == 1:
            words.add(run)
        else:
            words.update(run[i:i + 2] for i in range(len(run) - 1))
    for seg in CJK_RUN.split(t):
        # `[^\W\d_]` is "Unicode letter"; the CJK runs are already consumed
        # above, so this only ever sees the alphabetic scripts.
        words.update(w for w in re.findall(r"[^\W\d_]{3,}", seg)
                     if not w.isascii())
    return words
