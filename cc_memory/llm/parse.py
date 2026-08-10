"""ONE extractor for every JSON value an LLM response carries. (register M1)

Every surface that calls an LLM and expects JSON re-implemented the same two
steps — strip markdown code fences, then json.loads — as seven hand-rolled
copies in five files, two of which had drifted into different behaviour
(`core/consolidate.py` sliced to the outermost braces/brackets and so
tolerated prose around the payload; the five hook/dashboard copies did not,
so the identical model response parsed in one surface and failed in
another). A rule seven sites must each remember is a rule the eighth breaks;
the shared implementation keeps the MOST tolerant behaviour, which every
caller wants.

Pure stdlib, no package imports — safe to import from any entry point.
"""
import json


def extract_json(raw, kind="array"):
    """The first JSON value of `kind` in an LLM response, or None.

    kind is "array" or "object". NEVER raises: a response with no parseable
    value of the requested kind — prose refusals, truncated output, the
    wrong container type — returns None, which every caller already treats
    as "extraction produced nothing".

    Two model habits are absorbed here: markdown code fences around the
    payload (any ``` line is dropped, language tag included), and prose
    before/after it (the outermost open/close pair of the requested kind is
    sliced first; the whole text is the fallback for payloads that ARE bare
    JSON containing the delimiter in a string).
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    open_ch, close_ch = ("[", "]") if kind == "array" else ("{", "}")
    expected = list if kind == "array" else dict
    # The ONE-LINE fenced payload (register r6-A12) — the whole value inside
    # one ``` pair on a single line — lives ON the fence line, so the
    # dropped-lines variant erases it. It is the RAW-TEXT slice below that
    # carries that shape: fences are backticks, never the payload's own
    # delimiters, so slicing the outermost open/close pair of `text` itself
    # isolates the payload. A third "fences replaced by spaces" variant
    # shipped alongside this comment until 2026-08-09, when falsification
    # proved it unreachable — 88 fence/payload shapes, 0 where it changed
    # the result — so it is gone; the smoke A12 assertion pins the
    # BEHAVIOUR and a falsify case breaks the slice to keep it honest.
    variants = [text]
    if text.startswith("```"):
        variants.insert(0, "\n".join(l for l in text.split("\n")
                                     if not l.strip().startswith("```")))
    candidates = []
    for v in variants:
        s, e = v.find(open_ch), v.rfind(close_ch)
        if s >= 0 and e > s:
            candidates.append(v[s:e + 1])
        candidates.append(v)
    for cand in candidates:
        try:
            val = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(val, expected):
            return val
    return None
