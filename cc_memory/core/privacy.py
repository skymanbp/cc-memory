"""
Privacy tag filtering.

Strip <private>...</private> and <cc-memory-context>...</cc-memory-context>
from any text before storage. The latter prevents recursive storage of
already-injected context.

Why this is a hand-rolled linear scan and not a regex
----------------------------------------------------
The pre-v2.5 implementation was `re.sub(r"<private>.*?</private>", "", text)`
behind a `text.count("<private>") > 100` ReDoS guard — and that guard RETURNED
THE TEXT UNCHANGED. The filter therefore failed OPEN exactly when the payload
looked adversarial:

    n_tags= 100  marker_survives_strip=False
    n_tags= 101  marker_survives_strip=True   ← LEAK

`clean_for_storage` guards both the LLM-facing path (core/extractor.py) and the
memory write path (llm/memory_writer.py), so above the cap `<private>` content
reached the Anthropic API call *and* the memories table.

The cap was also calibrated on the wrong signal. Well-formed tags are cheap for
the regex engine; an UNTERMINATED `<private>` is the quadratic case, because
every open-tag start position rescans the remainder for a `</private>` that is
not there (measured, CPython 3.13):

    WELL-FORMED  20000 tags (996.1 KiB)  re.sub =    6.0 ms
    UNTERMINATED 16000 tags (140.6 KiB)  re.sub = 9517.4 ms   ← the real blowup

A single left-to-right `str.find` scan is O(n) with no backtracking, so no cap
is needed at all — and an unterminated open tag now fails CLOSED: everything
from the dangling `<private>` to the end of the text is DROPPED rather than
emitted.
"""

_PRIVATE_OPEN, _PRIVATE_CLOSE = "<private>", "</private>"
_CONTEXT_OPEN, _CONTEXT_CLOSE = "<cc-memory-context>", "</cc-memory-context>"


def _strip_tagged_spans(text: str, open_tag: str, close_tag: str) -> str:
    """Remove every ``open_tag`` … ``close_tag`` span in one linear pass.

    Pairing matches the non-greedy regex this replaces: each open tag binds to
    the FIRST close tag that follows it. Like the regex path, the result is
    `.strip()`ed only when at least one open tag was present, and text with no
    open tag is returned byte-identical.

    Fails CLOSED: a dangling open tag with no matching close tag drops the whole
    remainder of the text, because there is no way to know where the private
    span was meant to end.
    """
    if not text or open_tag not in text:
        return text
    parts = []
    pos = 0
    while True:
        start = text.find(open_tag, pos)
        if start < 0:
            parts.append(text[pos:])
            break
        parts.append(text[pos:start])
        end = text.find(close_tag, start + len(open_tag))
        if end < 0:
            # why: fail CLOSED — an unterminated <private> gives no end
            # boundary, so the remainder is dropped instead of being stored or
            # sent to the API. Emitting it is the leak this module exists to
            # prevent; the previous cap-and-return-unchanged did exactly that.
            break
        pos = end + len(close_tag)
    return "".join(parts).strip()


def strip_private(text: str) -> str:
    """Remove all <private>…</private> spans. No tag count cap; fails closed."""
    return _strip_tagged_spans(text, _PRIVATE_OPEN, _PRIVATE_CLOSE)


def strip_context_tags(text: str) -> str:
    """Remove all <cc-memory-context>…</cc-memory-context> spans (anti-recursion)."""
    return _strip_tagged_spans(text, _CONTEXT_OPEN, _CONTEXT_CLOSE)


def has_private(text: str) -> bool:
    return bool(text and _PRIVATE_OPEN in text)


def clean_for_storage(text: str) -> str:
    """Strip both private and context tags. Use before any storage."""
    text = strip_private(text)
    text = strip_context_tags(text)
    return text
