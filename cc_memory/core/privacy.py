"""
Privacy tag filtering.

Strip <private>...</private> and <cc-memory-context>...</cc-memory-context>
from any text before storage. The latter prevents recursive storage of
already-injected context.
"""
import re

_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL)
_CONTEXT_RE = re.compile(r"<cc-memory-context>.*?</cc-memory-context>", re.DOTALL)
_MAX_TAGS = 100  # ReDoS guard


def strip_private(text: str) -> str:
    if not text or "<private>" not in text:
        return text
    if text.count("<private>") > _MAX_TAGS:
        return text
    return _PRIVATE_RE.sub("", text).strip()


def strip_context_tags(text: str) -> str:
    if not text or "<cc-memory-context>" not in text:
        return text
    if text.count("<cc-memory-context>") > _MAX_TAGS:
        return text
    return _CONTEXT_RE.sub("", text).strip()


def has_private(text: str) -> bool:
    return bool(text and "<private>" in text)


def clean_for_storage(text: str) -> str:
    """Strip both private and context tags. Use before any storage."""
    text = strip_private(text)
    text = strip_context_tags(text)
    return text
