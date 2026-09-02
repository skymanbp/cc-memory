#!/usr/bin/env python3
"""
Documentation CLAIM gate — prose that counts things must count them correctly.

`tools/citation_check.py` proves `file.py:123` still points at its symbol. It
says nothing about whether the SENTENCE around it is true, so this repo's docs
kept asserting "four renderers are covered" while the tree had six, and every
convergence round rediscovered the same class of defect. Line numbers were
already machine-maintained; the claims were not.

HOW A CLAIM IS BOUND. Write the number in prose as usual, then bind it to a
contract from `tools/contracts.py` with an HTML comment (invisible when the
markdown renders):

    the six hooks <!--ce:hooks--> run on every session
    Four of the six hooks <!--ce:hooks:subset--> gate on memory.db existing

`<!--ce:NAME-->` asserts the preceding number EQUALS `len(contract NAME)`.
`<!--ce:NAME:subset-->` asserts it is strictly LESS — a subset that grew to
the whole set is itself drift, and saying "four of the six" once there are
seven is exactly the failure this gate exists to catch.
`<!--ce:NAME:asof-->` records a statement about the PAST ("v2.5.1 fixed the
six hooks and missed the seventh") and is never compared. It is an escape
hatch on purpose: an unbound number is an error, so declaring one historical
is a deliberate, greppable act rather than a silent omission.

WHY BINDING RATHER THAN PATTERN-MATCHING. A regex over "<number> hooks" fires
on "Four of the six hooks", "the other five hooks" and "three hooks with hard
host timeouts" — all true, all subsets. A gate that cries wolf is a gate that
gets ignored, so the author states which kind of claim it is, once.

COVERAGE. Nouns in TRIGGER_NOUNS must carry a binding wherever a number
precedes them: that is what stops a NEW unbound sentence from drifting in.
Bindings are honoured on any noun, so anything else can be bound voluntarily.

SURFACES. Three, all judged by the same grammar so they cannot drift apart:
the tracked markdown files, `cc_memory/config.json` (its `notes` block is
documentation that happens to be JSON), and the prose INSIDE the shipped
package — Python docstrings and comment runs. The third surface exists
because the disease does not respect file extensions: config.json called the
MCP server "the seventh caller" of `is_excluded` while twelve surfaces
consult it, and `db.py`'s `_connect` docstring counted 66 call sites in a
file that holds 80. `tools/` and `tests/` are deliberately NOT scanned:
their numbers are examples and expected-output strings (this very docstring
shows "the six hooks" as syntax), and a gate that fails on its own
documentation is a gate that gets ignored.

    python tools/doc_claims.py             # check
    python tools/doc_claims.py --report    # list unbound trigger sites
"""
import argparse
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import citation_check
import contracts

REPO = Path(__file__).resolve().parent.parent

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
         "twenty": 20,
         # the Chinese translations carry the same claims and drift with them
         "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
         "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
         "十三": 13}

_NUM = r"(?:\d+|" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")"
# ASCII number words need a word boundary; CJK numerals must NOT have one
# (`\b` is not a boundary between two Han characters), so the back-scan below
# builds its own alternation rather than reusing `_NUM` with `\b`.
_NUM_BOUNDED = (r"(?:\d+|\b(?:"
                + "|".join(w for w in sorted(WORDS, key=len, reverse=True)
                           if w.isascii())
                + r")\b|"
                + "|".join(w for w in sorted(WORDS, key=len, reverse=True)
                           if not w.isascii())
                + r")")

# Nouns whose numeric claims MUST be bound. Deliberately short: a noun only
# belongs here when every use of it means one countable set. "surfaces" does
# not qualify — the installer's five copied surfaces and the twelve modules
# that anchor are both "surfaces" in this repo's prose.
TRIGGER_NOUNS = (r"hooks?", r"renderers?", r"render paths?")

# "seven of the hooks", "all eight of the hooks", "four out of the six hooks":
# a count claim with a quantifier between the number and the noun. The
# number-adjacent grammar above sees none of them, so each was a way to state
# a countable claim with no binding and no complaint — measured on both
# spellings. Kept SEPARATE from TRIGGER_RE rather than folded into it as an
# optional group, because the filler must be bounded to these exact words: a
# permissive `.{0,20}` matched across sentence boundaries and paired numbers
# with nouns from different clauses.
TRIGGER_OF_RE = None  # assigned below, once TRIGGER_NOUNS is complete

# History records what was true AT A VERSION. "v2.5.1 wired a control into the
# six hooks" must not be rewritten when a seventh arrives — a history edited to
# stay current is not a history. Explicit bindings are still honoured in these
# regions; only the must-be-bound requirement is lifted.
#
# Whole files, and — because README.md is two thirds release notes — any
# section whose H2 is a RELEASE-NOTE heading. The phrases are matched, not the
# version number: "any H2 containing vN.N" also exempted `## Live plan anchor
# (v2.2)`, a section describing the CURRENT plan lifecycle, and an injected
# false claim under it sailed through. Every genuine history heading in the
# tree spells one of these phrases; a new spelling shows up as claims
# suddenly requiring bindings, which is loud, not silent.
HISTORY_DOCS = ("CHANGELOG.md",)
HISTORY_HEADING_RE = re.compile(
    r"^##\s+(?:Previously\b|What(?:'s|\s+is)\s+new\b|What\s+changed\b"
    r"|此前|.*有什么新变化)", re.MULTILINE)
HEADING_RE = re.compile(r"^##\s", re.MULTILINE)

# `(?![-/])` because "the three hook/package line numbers" counts LINE
# NUMBERS and "two hook-contract violations" counts VIOLATIONS — neither is
# a claim about hooks. `(?<!\.)` because a version digit reads as a count
# once its dot puts a word boundary before it: the package scan parsed
# "a still-running v2.7 hook" as '7 hook' and "a pre-v2.8.0 hook" as
# '0 hook' (both measured). A gate whose first output is a false positive
# teaches its reader to skip the rest of them.
TRIGGER_RE = re.compile(
    rf"\b(?<!\.)(?P<n>{_NUM})\s+(?P<noun>{'|'.join(TRIGGER_NOUNS)})\b(?![-/])",
    re.IGNORECASE)
# The quantifier forms described above: "<n> of the <noun>", with an optional
# leading "all"/"only"/"just" and an optional "out". The `every`/`each`
# lookbehinds are ALL-quantifiers, not counts: "every one of the six hooks"
# parsed as a claim of ONE hook (measured on config.json), when the claim in
# that sentence is the inner "six hooks" — which TRIGGER_RE picks up once
# this pattern declines the match.
TRIGGER_OF_RE = re.compile(
    rf"(?<!every\s)(?<!each\s)"
    rf"\b(?<!\.)(?P<n>{_NUM})\s+(?:out\s+)?of\s+(?:the\s+|these\s+|those\s+)?"
    rf"(?:{_NUM}\s+)?(?P<noun>{'|'.join(TRIGGER_NOUNS)})\b(?![-/])",
    re.IGNORECASE)
# "6 command hooks", "three sibling renderers": ONE modifier word between the
# number and the noun — the commonest English shape, and seen by none of the
# patterns above, so two live claims about the hooks contract sat unbound and
# unchecked (`docs/CONTRACTS.md` "declares 5 events / 6 command hooks" and
# CLAUDE.md "PreCompact fires TWO command hooks").
#
# Two guards make this safe, and both were measured over every scanned surface
# before being kept (5 matches, 0 false positives):
#  * ONE OR TWO words, never a free-length gap — `.{0,20}` was tried and
#    paired numbers with nouns from different clauses. Exactly one word was
#    the rule through v2.13.2, and one extra modifier was enough to state an
#    unbound count: "All nine shipped plugin hooks" passed while "All nine
#    hooks" was refused (measured). Widening to two surfaced exactly one live
#    site in the tree (`ui/installer.py`'s "5 command hooks" — a real count,
#    now bound) and no false positive;
#  * the noun must be PLURAL. That is what separates a count of hooks from the
#    ADJECTIVAL use, where the trigger noun modifies the real head: "Two
#    independent hook registries" counts registries, "the 300 s hook timeout"
#    counts seconds, and both are in this tree today.
# `of` and number words are excluded because TRIGGER_OF_RE owns those forms.
TRIGGER_GAP_RE = re.compile(
    rf"(?<!every\s)(?<!each\s)\b(?<!\.)(?P<n>{_NUM})\s+"
    rf"(?:(?!of\b)(?!{_NUM}\b)[A-Za-z][A-Za-z-]*\s+){{1,2}}"
    rf"(?P<noun>hooks|renderers|render\s+paths)\b(?![-/])",
    re.IGNORECASE)
# Headings and tables also write the count AFTER the noun: `## Hooks (6)`.
# Invisible to the number-first grammar, that 6 was the one live count in
# CLAUDE.md nothing checked.
TRIGGER_PAREN_RE = re.compile(
    rf"\b(?P<noun>{'|'.join(TRIGGER_NOUNS)})\s*\(\s*(?P<n>\d+)\s*\)",
    re.IGNORECASE)
# The Chinese docs write "六个钩子" with no space and a measure word. The
# lookbehind rejects quantifier and ordinal prefixes where the numeral is not
# a count: 每一个/任一个 (every/any), 这一个/哪一个 (this/which), 任意一个
# (arbitrary), 第六个 (SIXTH — an index, not six of them). Measured: all four
# unguarded forms parsed as count claims.
# Measure words: 个 was the only one recognised, so `六条钩子` (the natural
# classifier for a line/strand, and the one the Chinese docs actually reach
# for) parsed as no claim at all. `道` covers 九道门-style phrasing. The noun
# alternation also accepts the LATIN spellings, because the Chinese docs write
# `6 个 hook` and `六个 render path` inline — measured: both invisible before.
# Because of those latin spellings this pattern scans ENGLISH text too, so it
# needs the same guards as TRIGGER_RE or it resurrects every false positive
# they kill (measured: it re-matched 'v2.7 hook' and 'hook-contract' after
# the English patterns learned to decline them). `(?<![A-Za-z])` stands in
# for the `\b` this pattern deliberately lacks — without it the `one` inside
# `done` is a number when a noun happens to follow.
TRIGGER_ZH_RE = re.compile(
    rf"(?<![每任这哪意第])(?<![A-Za-z.])(?P<n>{_NUM})\s*[个条道处项]?\s*"
    rf"(?P<noun>钩子|渲染路径|hooks?|render\s*paths?|renderers?)(?!\s*[-/])",
    re.IGNORECASE)
BIND_RE = re.compile(
    r"<!--\s*ce:(?P<name>[a-z_]+)(?::(?P<kind>subset|asof))?\s*-->")

# How far after a number the binding may sit: enough for the rest of a clause,
# short enough that it cannot be captured by an unrelated later binding.
BIND_WINDOW = 160


def _to_int(token):
    return int(token) if token.isdigit() else WORDS[token.lower()]


def _history_spans(text):
    """Byte ranges covered by a version-titled H2, up to the next H2."""
    heads = [m.start() for m in HEADING_RE.finditer(text)] + [len(text)]
    spans = []
    for hit in HISTORY_HEADING_RE.finditer(text):
        end = next(h for h in heads if h > hit.start())
        spans.append((hit.start(), end))
    return spans


def _in_history(spans, offset):
    return any(start <= offset < end for start, end in spans)


def _fenced(text):
    """Offsets of code fences, so claims inside code blocks can be skipped.

    An HTML comment is invisible in prose and LITERAL inside a fence, so a
    binding cannot be attached to an ASCII architecture diagram or a shell
    transcript without defacing it. Those are illustrations; the prose around
    them carries the claim this gate checks.

    CommonMark allows up to three leading spaces and tilde fences; both exist
    in this repo (`agents/plan-refiner.md` opens fences at three spaces).
    Recognising only column-zero backticks made the text inside those blocks
    look like prose.
    """
    return [m.start()
            for m in re.finditer(r"^ {0,3}(?:```|~~~)", text, re.MULTILINE)]


def _scan_doc(text):
    """Yield (offset, number, noun, binding-or-None) for every claim site.

    Pairing rule: each binding, taken in document order, attaches to the
    NEAREST unclaimed site before it (within BIND_WINDOW). That is the
    documented semantics — "the preceding number" — and both other rules
    failed measurably: sites-forward let claim A steal the binding that sits
    immediately after claim B (an adjacent `Five hooks … six hooks <subset>
    <equal>` swapped kinds and PASSED), and it also let a newly inserted
    unbound sentence adopt the next paragraph's binding. A binding serves
    exactly one site, and it serves the closest one.
    """
    fences = _fenced(text)
    raw = sorted(
        (hit for pattern in (TRIGGER_OF_RE, TRIGGER_GAP_RE, TRIGGER_RE,
                             TRIGGER_ZH_RE, TRIGGER_PAREN_RE)
         for hit in pattern.finditer(text)
         if not sum(1 for f in fences if f < hit.start()) % 2),
        key=lambda m: (m.start(), -(m.end() - m.start())))
    # De-overlap: "four of the six hooks" matches TRIGGER_OF_RE (n=4, the real
    # claim) AND TRIGGER_RE ("six hooks", n=6) inside it. Two sites for one
    # sentence means one of them can never be bound and the gate reports a
    # permanent false problem. Longest-match-wins at each start, then drop any
    # later match that begins inside one already taken.
    sites, taken_to = [], -1
    for hit in raw:
        if hit.start() < taken_to:
            continue
        sites.append(hit)
        taken_to = hit.end()
    claimed = {}
    for bind in BIND_RE.finditer(text):
        best = max((s for s in sites if s not in claimed.values()
                    and s.end() <= bind.start() <= s.end() + BIND_WINDOW),
                   key=lambda s: s.start(), default=None)
        if best is not None:
            claimed[bind] = best
    owner = {site: bind for bind, site in claimed.items()}
    for hit in sites:
        yield (hit.start(), _to_int(hit.group("n")), hit.group("noun"),
               owner.get(hit))


def _bound_claims(text):
    """Every binding, paired with the number it belongs to.

    The pairing comes from `_scan_doc`, which already matched each claim site
    to the binding that follows it. Re-deriving it by scanning backwards for
    "the nearest number" was a SECOND implementation of the same fact and it
    disagreed with the first on every Chinese line: `\\b` is not a boundary
    between Han characters, so the scan skipped 六 and picked up the digits in
    `§4` and `v2.5.1` instead — reporting "says 4 hooks, code says 6" about a
    sentence that reads 六个钩子.

    A binding with no trigger site before it (a voluntary one, on a noun that
    is not in TRIGGER_NOUNS) still needs a number, so that case scans back —
    without word boundaries, which is what made it wrong.
    """
    paired = {bind.start(): number
              for _, number, _, bind in _scan_doc(text) if bind is not None}
    fences = _fenced(text)
    for bind in BIND_RE.finditer(text):
        if sum(1 for f in fences if f < bind.start()) % 2:
            # A binding inside a fence is an EXAMPLE of the syntax, not a
            # claim: this gate's own documentation shows all three forms, and
            # checking them failed the build on a sentence nobody asserted.
            continue
        if bind.start() in paired:
            yield bind, paired[bind.start()]
            continue
        head = text[max(0, bind.start() - BIND_WINDOW):bind.start()]
        # `_NUM_BOUNDED`, not `_NUM`: the bare alternation matches ASCII number
        # words INSIDE ordinary words — measured, "the work is done" bound as 1
        # (the `one` in "done") and "appears often" as 10 (the `ten` in
        # "often"). A voluntary binding then silently checked a number nobody
        # wrote. CJK numerals keep no boundary, because `\b` is not one between
        # Han characters — the mistake the docstring above records.
        nums = re.findall(rf"({_NUM_BOUNDED})", head, re.IGNORECASE)
        yield bind, (_to_int(nums[-1]) if nums else None)


# Prose surfaces beyond the tracked markdown. config.json is scanned as one
# document (JSON has no headings, so `_history_spans` finds nothing and every
# claim in it must be bound or written as history with `:asof`). The package
# scan takes docstrings and comment runs only — string literals are runtime
# OUTPUT (error messages, SQL, rendered templates) and quote hypotheticals.
EXTRA_DOCS = ("cc_memory/config.json",)
PY_PROSE_ROOT = "cc_memory"


def _py_prose(text):
    """(start_line, prose) segments of a Python source: docstrings + comments.

    Consecutive comment lines join into one segment so a claim and its
    binding may sit on adjacent lines, exactly as they can in a paragraph.
    A binding still cannot travel across a blank/code line — each segment is
    judged alone, which is the same locality BIND_WINDOW gives markdown.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []  # compileall owns syntax errors; nothing to scan here
    segments = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                segments.append((node.body[0].lineno, doc))
    run, run_start, prev = [], None, None
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type != tokenize.COMMENT:
            continue
        line = tok.start[0]
        if prev is not None and line == prev + 1:
            run.append(tok.string)
        else:
            if run:
                segments.append((run_start, "\n".join(run)))
            run, run_start = [tok.string], line
        prev = line
    if run:
        segments.append((run_start, "\n".join(run)))
    return sorted(segments)


def _judge_block(rel, text, sizes, line_of, spans=()):
    """Judge one prose block's claim sites and bindings.

    Shared by all three surfaces so the grammar cannot drift between them —
    a sentence that triggers in a README must trigger identically in the
    docstring that restates it.
    """
    problems, checked, sites = [], 0, 0
    for offset, number, noun, bind in _scan_doc(text):
        if _in_history(spans, offset):
            continue
        sites += 1
        if bind is None:
            problems.append(
                f"{rel}:{line_of(offset)} '{number} {noun}' is not bound "
                f"to a contract — add <!--ce:NAME--> (whole set) or "
                f"<!--ce:NAME:subset--> (a part of it)")
    for bind, number in _bound_claims(text):
        name, kind = bind.group("name"), bind.group("kind")
        line = line_of(bind.start())
        if name not in sizes:
            problems.append(
                f"{rel}:{line} binds to unknown contract '{name}' "
                f"(known: {', '.join(sorted(sizes))})")
            continue
        if number is None:
            problems.append(
                f"{rel}:{line} ce:{name} has no number before it")
            continue
        checked += 1
        problems.extend(_verdict(rel, line, name, kind, number, sizes[name]))
    return problems, checked, sites


def check(repo=REPO):
    """Return (problems, n_checked, n_sites). A problem is a human sentence."""
    sizes = {name: len(members)
             for name, members in contracts.values(repo).items()}
    problems, checked, sites = [], 0, 0
    for rel in list(citation_check.TRACKED) + list(EXTRA_DOCS):
        path = repo / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        line_of = _line_indexer(text)
        fences = _fenced(text)
        if len(fences) % 2:
            # Fence state is tracked by parity, so one unclosed fence would
            # silently exempt everything after it — measured: a lone ``` made
            # a following unbound "five hooks" invisible. An odd count is a
            # broken document, not a scanning choice.
            problems.append(
                f"{rel}:{line_of(fences[-1])} unclosed code fence — every "
                f"claim after it would be exempt, so the file is rejected")
        # A HISTORY_DOCS file is one whole history span; bindings inside it
        # are still verified (an `:asof` exempts itself), only the
        # must-be-bound requirement is lifted — same as a history section.
        spans = (((0, len(text)),) if rel in HISTORY_DOCS
                 else _history_spans(text))
        p, c, s = _judge_block(rel, text, sizes, line_of, spans)
        problems.extend(p)
        checked += c
        sites += s
    for path in sorted((repo / PY_PROSE_ROOT).rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for base_line, seg in _py_prose(text):
            p, c, s = _judge_block(
                rel, seg, sizes,
                lambda off, _b=base_line, _s=seg: _b + _s.count("\n", 0, off))
            problems.extend(p)
            checked += c
            sites += s
    return problems, checked, sites


def _verdict(rel, line, name, kind, number, size):
    if kind == "asof":
        return []
    if kind == "subset":
        if number >= size:
            return [f"{rel}:{line} claims {number} of the {name}, but there "
                    f"are only {size} — a subset that caught up with the "
                    f"whole set is drift, not a subset"]
    elif number != size:
        return [f"{rel}:{line} says {number} {name}, code says {size} "
                f"(python tools/contracts.py to see which)"]
    return []


def _line_indexer(text):
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)

    def line_of(offset):
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1
    return line_of


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--root", default=str(REPO))
    ap.add_argument("--report", action="store_true",
                    help="list problems and exit 0 (for triage, not CI)")
    args = ap.parse_args()
    problems, checked, sites = check(Path(args.root))
    for problem in problems:
        print(f"[CLAIM] {problem}")
    print(f"\nSummary: {checked} bound claim(s) verified, {sites} trigger "
          f"site(s) seen, {len(problems)} problem(s)")
    if args.report:
        return 0
    print("Result: " + ("OK (every bound claim matches the code)"
                        if not problems else "FAIL"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
