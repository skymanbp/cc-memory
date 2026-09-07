"""
Layer 2 of the injection-usage measurement: was a delivered memory USED?

WHY TWO LAYERS
==============
`/cc-mem inject-usage` answers "did the context reach Claude" with signals that
cannot be faked and cost nothing — Read observations against PROGRESS.md /
MEMORY.md, the handoff ack matched through the constant that demands it, and
`memories.recall_count`. Every one of those proves DELIVERY. None of them can
tell whether a delivered memory changed a single word Claude wrote, because
that is a judgement about text.

So the measurement is two layers, and the split is the design (user decision,
2026-09-07 — both were asked for, and neither replaces the other):

    layer  default  method           cost         question
    ----------------------------------------------------------------------
      1      ON     deterministic    free         was it DELIVERED
      2      OFF    LLM judge        one API call was it USED
             (--judge)

A judge that ran by default would put an Anthropic request behind a read-only
status command and bill it to a user who asked a question about their own
database; a judge that does not exist leaves the project's central claim —
that these memories are worth injecting — resting on delivery counts forever.
Opt-in is what makes both true at once.

TRI-STATE, for the same reason `cli/mem.py:_ack_signal` is
=========================================================
`unknown` — no credential, a refused call, an unparsable answer, an id the
judge did not answer for — is NEVER rendered as `unused`. "Unused" is a claim
about Claude's behaviour; a failure of ours is not evidence for it, and the
whole reason this file exists is that v2.14.1's `inject-usage` docstring
promised a signal nothing computed. A judge that reports its own outages as
negative findings is that defect with an API bill attached.

WHAT IT READS, AND WHAT IT DELIBERATELY DOES NOT
================================================
Claude's own reply text (`type: "text"` blocks of assistant records) and the
memories that were delivered. NOT the user's turns: the question is what the
ASSISTANT did with the memory, user text would double the payload, and the
`<private>` spans a user marks are in their own messages. The caller strips
private spans anyway (`core.privacy.strip_private`) — defence in depth, since
an assistant reply can quote one back.

The logic core here is PURE (v2.10.1 rule 1): `build_judge_input` and
`parse_verdicts` take plain data and return plain data, and `judge_usage`
takes the LLM entry point as an ARGUMENT, so `tests/smoke_test.py` drives all
three with no network and `tools/falsify_fixes.py` can break them.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from llm.parse import extract_json

# Bounds live HERE, not at the caller: a bound the caller owns is a bound the
# next caller forgets. Both sides of the payload are unbounded in principle —
# a transcript is arbitrarily long and a memory is user text.
JUDGE_MAX_MEMORIES = 10
JUDGE_MAX_MEMORY_CHARS = 400
JUDGE_MAX_TURNS = 12
JUDGE_MAX_TURN_CHARS = 1200

# Wall-clock for the whole judge. This is a CLI, so no host timeout kills it —
# which is exactly why it needs a bound of its own: a hung API must not wedge a
# read-only status command. Passed to `call_llm(deadline=…)`, the absolute
# form (v2.5.0 item 7), never a per-socket timeout.
JUDGE_DEADLINE_S = 45.0

VERDICT_USED = "used"
VERDICT_UNUSED = "unused"
VERDICT_UNKNOWN = "unknown"
VERDICTS = (VERDICT_USED, VERDICT_UNUSED, VERDICT_UNKNOWN)

_SYSTEM = (
    "You judge whether an assistant used a memory that was injected into its "
    "context. You are given numbered MEMORIES and the assistant's own REPLIES "
    "from the same session.\n"
    "For each memory answer exactly one verdict:\n"
    '  "used"    — a reply states, relies on, or acts on this memory\'s '
    "content\n"
    '  "unused"  — no reply reflects it\n'
    '  "unknown" — you cannot tell from the replies given\n'
    "A reply that merely covers the same broad topic is NOT used; the memory's "
    "specific content has to show up. Prefer \"unknown\" over guessing.\n"
    'Answer with a JSON array only: [{"id": <int>, "verdict": "used", '
    '"evidence": "<= 15 words quoted from a reply"}]'
)


def _clip(text: str, limit: int) -> str:
    """`text` collapsed to one line and cut to `limit`, marked when cut."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def build_judge_input(memories: Sequence[Dict],
                      turns: Sequence[str]) -> Tuple[str, str]:
    """(system, user) for one judge call. PURE — no I/O, no clock.

    `memories` are row-like mappings carrying `id` and `content`; `turns` are
    assistant reply texts, oldest first. Both are clipped here so the payload
    is a known quantity before it reaches the network.
    """
    lines: List[str] = ["MEMORIES:"]
    for row in list(memories)[:JUDGE_MAX_MEMORIES]:
        lines.append(f"  [{int(row['id'])}] "
                     f"{_clip(row.get('content', ''), JUDGE_MAX_MEMORY_CHARS)}")
    lines.append("")
    lines.append("ASSISTANT REPLIES (oldest first):")
    for turn in list(turns)[-JUDGE_MAX_TURNS:]:
        lines.append(f"  - {_clip(turn, JUDGE_MAX_TURN_CHARS)}")
    return _SYSTEM, "\n".join(lines)


def parse_verdicts(raw, ids: Sequence[int]) -> Dict[int, Tuple[str, str]]:
    """{id: (verdict, evidence)} for EVERY id in `ids`.

    Every id is present in the result, and one the judge did not answer for —
    or answered with a word outside `VERDICTS` — is `unknown`, never `unused`.
    A silent judge is a judge that said nothing, and the difference between
    "no reply used this" and "we did not find out" is the entire point of the
    measurement.

    Never raises: `llm.parse.extract_json` returns None for prose, a refusal
    or truncated output (ONE extractor for every JSON an LLM returns here —
    do not re-implement the fence stripping), and None means every id is
    unknown.
    """
    out: Dict[int, Tuple[str, str]] = {
        int(i): (VERDICT_UNKNOWN, "") for i in ids}
    payload = extract_json(raw, kind="array")
    if not isinstance(payload, list):
        return out
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            mid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if mid not in out:
            continue  # why: the judge answering about an id nobody asked
            # about is its own confusion, not a verdict on this project's rows
        verdict = str(item.get("verdict", "")).strip().lower()
        if verdict not in VERDICTS:
            continue
        out[mid] = (verdict, _clip(item.get("evidence", ""), 120))
    return out


def judge_usage(memories: Sequence[Dict], turns: Sequence[str], *, call,
                api_key: str = "",
                deadline_s: float = JUDGE_DEADLINE_S
                ) -> Tuple[Optional[Dict[int, Tuple[str, str]]], str]:
    """(verdicts, detail) — verdicts is None when the judge could not RUN.

    `call` is the LLM entry point (`llm.ccl_backend.call_llm`), taken as an
    argument rather than imported: it is what lets the gate drive this function
    with a stub instead of a network, and what lets a falsification case make
    the call fail on demand. The same shape `core/plan.py` uses for its own
    testable cores.

    None is the tri-state's third value and is the ONLY thing a failure may
    produce. The caught set is the CLASS this call can raise, not a bare
    `Exception` (v2.14.0 rule 18): `call_llm` catches per leg and raises
    `RuntimeError` when every candidate credential fails — that is its
    documented failure — while `OSError` and `ValueError` cover a socket or a
    decode escaping a leg. Nothing here may abort the layer-1 report that has
    already printed above it.
    """
    import time
    if not memories:
        return None, "nothing was delivered to judge"
    if not turns:
        return None, "no assistant replies in the transcript window"
    system, user = build_judge_input(memories, turns)
    ids = [int(r["id"]) for r in list(memories)[:JUDGE_MAX_MEMORIES]]
    try:
        raw = call(system, user, api_key=api_key, max_tokens=1000, timeout=20,
                   deadline=time.monotonic() + deadline_s)
    except (RuntimeError, OSError, ValueError) as exc:
        return None, f"the judge call failed ({type(exc).__name__}: {exc})"
    verdicts = parse_verdicts(raw, ids)
    if all(v == VERDICT_UNKNOWN for v, _ in verdicts.values()):
        return verdicts, "the judge returned no usable verdict"
    return verdicts, f"judged {len(ids)} delivered memory(ies)"
