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

Pure stdlib at module level — safe to import from any entry point. The two
extraction helpers at the bottom (v2.16.0, D2) import the package lazily,
inside their bodies, so `extract_json` keeps that property.
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


# ── ONE extraction prompt, ONE normaliser (v2.16.0, D2) ─────────────────────
# Four surfaces asked a model for memories — the Stop observer, PreCompact,
# the retroactive save and the dashboard's Save Session — and each spelled
# its own prompt and its own validation loop. Measured on the v2.15.2 tree:
# four prompts drifting in wording (the dashboard's never asked for `topic`,
# so its rows had none), four copies of the 10-character floor beside the
# writer's `MIN_CONTENT_LEN`, and `MODES[*]["extraction_prompt_suffix"]` — a
# per-mode focus line every mode has declared — read by NOTHING. A rule four
# sites must each remember is a rule the fifth breaks; these two functions
# are the sites now.

_EXTRACTION_FIELDS = (
    '- "category": one of {categories}\n'
    '- "content": one concise, self-contained sentence with specific values '
    '(numbers, file names, parameters)\n'
    '- "importance": 1-5 (5=critical/never-forget, 4=important, 3=useful, '
    '2=minor, 1=trivial)\n'
    '- "topic": a short lowercase keyword for grouping (e.g. "auth", '
    '"pipeline", "config", "ui")'
)

# kind -> (what the model is, its rules, the prefix of the user message)
_EXTRACTION_KINDS = {
    "transcript": (
        "You are a memory extraction system. Given a Claude Code conversation "
        "transcript, extract the most important information worth remembering "
        "across sessions.",
        "- Only save CONCLUSIONS, not discussion process or debugging steps\n"
        "- Each memory must be understandable WITHOUT context\n"
        "- Include specific values: \"lr=3e-4 chosen over 1e-3 because "
        "val_loss flatlined\" not \"tuned lr\"\n"
        "- Skip: conversation logistics, tool errors, meta-discussion, trivial Q&A\n"
        "- Output 5-15 memories maximum. Quality over quantity.\n"
        "- Do NOT include memories about the memory plugin itself unless it's "
        "a critical bug fix",
        "Extract memories from this conversation:\n\n",
    ),
    "observations": (
        "You are a memory observer. Given a user's request and a batch of tool "
        "observations from a Claude Code session, extract ONLY the observations "
        "worth remembering long-term.",
        "- Only save CONCLUSIONS and OUTCOMES, not intermediate steps\n"
        "- Skip: file reads without insight, routine git commands, navigation\n"
        "- Each memory must be understandable WITHOUT conversation context\n"
        "- Include specific values: file names, numbers, error messages\n"
        "- 0-5 memories max per batch. Return [] if nothing worth saving.",
        "",
    ),
}

# The store, shown to the extractor (v2.18.0, E1). Until this version the
# model that wrote memories never saw what was stored, so a changed fact —
# a version bumped, a plan dropped, a file renamed — could only become a
# NEW row beside the old one: the writer's merge/supersede branches fire
# on trigram similarity within one topic, and measured on a live
# 1,334-row database the last 211 rows written reached that bar 0 times
# in their topic and once anywhere (`CHANGELOG.md` § [2.18.0]). These
# rules name the two operations the model may now return, and the ONE
# shape each takes, so every caller's normaliser reads them the same way.
EXISTING_HEAD = ("Existing memories (already stored; use them ONLY to decide "
                 "what to update or archive, never restate one unchanged):")
_EXISTING_RULES = (
    '- The user message opens with "Existing memories", one per line as '
    '"#<id> [<category>] <content>". When a new fact CHANGES, CORRECTS or '
    'REPLACES one of them, add "supersedes": <id> to the new object; the old '
    'memory is archived and linked to the new one.\n'
    '- When an existing memory is now false, finished or no longer applies '
    'and nothing replaces it, output {"archive": <id>, "reason": "<why>"} in '
    'place of a memory.\n'
    '- Name only ids from that list. A memory that is still true and '
    'unchanged is not output again.'
)


def render_existing(rows):
    """The "Existing memories" block: one `#<id> [<category>] <content>` line
    per row, in the order given. Stored content is never interpolated raw
    into anything a model reads (`core.privacy.neutralize_inline`)."""
    from core.privacy import neutralize_inline
    lines = []
    for row in rows or []:
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        content = neutralize_inline(str(row.get("content") or "")).strip()
        if not content:
            continue
        lines.append(f"#{rid} [{row.get('category') or 'note'}] {content}")
    return "\n".join(lines)


def build_extraction_prompt(kind, body, *, mode_suffix="", existing=None):
    """`(system, user)` for one extraction call.

    `kind` is "transcript" (PreCompact, the retroactive save, the dashboard;
    `body` is the summarised transcript plus any observation context) or
    "observations" (the Stop observer; `body` is its request + observation
    block, sent as is). `mode_suffix` is the project mode's
    `extraction_prompt_suffix` (`core.modes.get_extraction_suffix`), placed
    after the rules so a `research` project's extractor is told to favour
    numbers and a `writing` project's to favour structure — the line every
    mode declared and no extractor read until v2.16.0.

    `existing` (v2.18.0, E1) is the list of stored rows the model may update
    or archive — `llm.memory_writer.existing_for` chooses them. With rows,
    the system prompt gains the two operations' rules and the user message
    opens with the rendered block; without, both are exactly what v2.16.0
    sent, so a caller that passes nothing changes nothing.
    """
    from core.db import CATEGORIES
    head, rules, user_prefix = _EXTRACTION_KINDS[kind]
    suffix = (mode_suffix or "").strip()
    block = render_existing(existing) if existing else ""
    if block:
        rules = rules + "\n" + _EXISTING_RULES
    system = (head + "\n\nOutput a JSON array of objects with these fields:\n"
              + _EXTRACTION_FIELDS.format(
                  categories=", ".join(f'"{c}"' for c in CATEGORIES))
              + "\n\nRules:\n" + rules
              + (("\n" + suffix) if suffix else "")
              + "\n\nOutput ONLY a valid JSON array, no markdown, no explanation.")
    user = user_prefix + body
    if block:
        user = EXISTING_HEAD + "\n" + block + "\n\n" + user
    return system, user


def _positive_id(value):
    """`value` as a positive int, else None. Model output: `"12"`, `12.0`
    and `12` all name row 12; anything else names nothing."""
    if isinstance(value, bool):
        return None
    try:
        rid = int(value)
    except (TypeError, ValueError):
        return None
    return rid if rid > 0 else None


def normalize_memories(items, allowed_ids=None):
    """The rows worth writing from a parsed extraction array.

    Every element is untrusted model output: a non-dict is dropped, `content`
    is cleaned (`core.privacy.clean_for_storage`) and must reach the writer's
    `MIN_CONTENT_LEN`, an unknown `category` becomes "note", `importance` is
    clamped to 1-5 (an unparsable one is 3 — it used to raise out of the loop
    and cost the whole batch), and a non-string `topic` becomes "". Returns
    dicts with exactly those four keys; callers add their own `tags`.

    The two operations of v2.18.0 (E1) ride on the same list. A memory may
    carry `"supersedes": <id>` (a fifth key, present only when it names a
    row); an element `{"archive": <id>, "reason": ...}` with no content
    becomes `{"archive": <id>, "reason": <cleaned text>}`. `allowed_ids`
    are the ids the prompt showed the model: an operation naming any other
    id is dropped — the key removed from a memory, the archive element
    skipped — because an id the model was never shown is an id it invented.
    With `allowed_ids=None` (a caller that showed nothing) every operation
    is dropped the same way, so the old call shape cannot archive a row.
    """
    from core.db import CATEGORIES
    from core.privacy import clean_for_storage
    from llm.memory_writer import MIN_CONTENT_LEN
    allowed = {int(i) for i in (allowed_ids or [])}
    out = []
    for m in items or []:
        if not isinstance(m, dict):
            continue
        content = clean_for_storage(str(m.get("content") or "").strip())
        if "archive" in m and not content:
            rid = _positive_id(m.get("archive"))
            if rid is not None and rid in allowed:
                reason = clean_for_storage(str(m.get("reason") or "").strip())
                out.append({"archive": rid, "reason": reason[:200]})
            continue
        if len(content) < MIN_CONTENT_LEN:
            continue
        category = m.get("category", "note")
        if category not in CATEGORIES:
            category = "note"
        try:
            importance = int(m.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        topic = m.get("topic", "")
        row = {"category": category, "content": content,
               "importance": max(1, min(importance, 5)),
               "topic": topic if isinstance(topic, str) else ""}
        old_id = _positive_id(m.get("supersedes"))
        if old_id is not None and old_id in allowed:
            row["supersedes"] = old_id
        out.append(row)
    return out
