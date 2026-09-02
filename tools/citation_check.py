#!/usr/bin/env python3
"""Definition-site checker for `file.py:LINE` citations in the docs.

The problem it exists for, quoted from CLAUDE.md § Tests before this tool
existed: *"Nothing gates doc `file:line` citations. They are hand-maintained
and rot on every refactor."* Three consecutive releases shipped with stale
ones, and each fix wave created more — a citation is correct on the day it is
written and wrong the next time anything above it grows a line.

## What it checks

For every citation ``path/to/file.py:120`` or ``file.py:120-138`` in a tracked
document, it collects the SYMBOLS named in the same sentence, resolves each
one's definition site in that file with ``ast``, and asserts the cited range
covers it.

  ``core.plan.raw_pending_refinement (`plan.py:262`)``
    -> symbol ``raw_pending_refinement``, defined at plan.py:250-268
    -> 262 lies inside that definition  -> OK

A citation whose sentence names no resolvable symbol is reported as SKIP, not
as a failure: this tool is a gate against ROT, and inventing a verdict for a
citation it cannot anchor would make it one more thing to distrust.

## Verbatim regions

A document may quote captured EVIDENCE — a transcript, a hook's refusal, a
subagent's report — that itself contains ``file:line`` text. Those are not
citations into this tree, and they must never be "repaired" into something
the capture did not say: on its first run over README § Before and after,
``--fix`` rewrote ``cli.py:12`` to ``cli.py:33`` inside a quoted guardian
report, which is the one edit a verbatim quote cannot survive. Fence such a
block with

  <!-- verbatim: demo/captures/guardian/C.with-ccm.txt -->
  ...
  <!-- /verbatim -->

and two things happen: nothing inside is scanned for citations, and every
segment of it (split on ``[…]`` elisions; blockquote ``>`` prefixes and code
fences stripped; whitespace collapsed) must occur in the named file, or the
region is reported as QUOTE and the gate fails. "The quotes are verbatim" is
then a measurement, not a promise.

## Exit codes

  0  no STALE citations (OK / SKIP only) and every verbatim region verified
  1  at least one STALE citation, a citation whose file does not exist, or a
     verbatim region its source does not contain

## Modes

  (default)      check, print a report
  --list         every citation with its verdict, not just the failures
  --fix          rewrite STALE line numbers in place to the resolved
                 definition site. Only the digits after ``.py:`` change.
  --root PATH    repository root (defaults to this file's parent's parent)

Pure stdlib, and deliberately NOT packaged — like tools/i18n_check.py it is a
dev/CI tool that lives outside cc_memory/ and is absent from
ui/installer.py SUBPACKAGE_FILES, build_exe.py and cli/mem.py's
_REQUIRED_PLUGIN_FILES.
"""
import argparse
import ast
import re
import sys
from pathlib import Path

# EVERY markdown document in the repository, not a hand-picked subset.
# Through v2.5.4 this listed seven files and the other six — CHANGELOG.md, the
# two agent prompts, the slash command and the two skills — were checked by
# nothing at all. "Which docs are gated" is exactly the kind of question that
# rots silently, so the answer is now "all of them", and `smoke_test.py`
# asserts this list equals `git ls-files "*.md"`.
# EVERY tracked markdown file. `smoke_test.py` asserts this list equals
# `git ls-files "*.md"`, so adding a document without adding it here turns the
# suite red — which is how the three v2.11.1 additions below were caught, by
# CI rather than locally: the gates were run BEFORE `git add`, so those files
# were still untracked and `git ls-files` did not yet see them. v2.12.2 hit
# the same trap with four captured artifacts, so the assertion now also lists
# UNTRACKED, not-ignored markdown (`--others --exclude-standard`): the local
# run sees what CI will see, minus EVIDENCE_PREFIXES below.
TRACKED = ["README.md", "README.zh.md", "CLAUDE.md", "CHANGELOG.md",
           "CONTRIBUTING.md", "SECURITY.md",
           ".github/PULL_REQUEST_TEMPLATE.md",
           "docs/ARCHITECTURE.md", "docs/ARCHITECTURE.zh.md",
           "docs/CONTRACTS.md", "docs/CONTRACTS.zh.md",
           "commands/cc-mem.md",
           "agents/plan-refiner.md", "agents/plan-guardian.md",
           "skills/ccm-load/SKILL.md", "skills/save-memories/SKILL.md",
           # the before/after demo (README § Before and after): its provenance
           # doc and the fixture's own README. The captured transcripts under
           # demo/captures/ are .txt on purpose — quoted evidence, not docs.
           "demo/README.md", "demo/tally/README.md"]

# Captured EVIDENCE is not documentation. demo/captures/ also holds the
# plugin's own artifacts as they stood when the README's sessions ran —
# PROGRESS.md, PLAN.md, MEMORY.md — and a `file:line` inside them is a
# statement about the FIXTURE at capture time, exactly like the text inside a
# verbatim region, never a claim about this tree. smoke_test.py leaves these
# prefixes out of its "every tracked markdown file is in TRACKED" assertion.
# Nothing else may be listed here: a document that is not evidence is gated.
EVIDENCE_PREFIXES = ("demo/captures/",)

# `cc_memory/core/db.py:1349`, `db.py:188-201`, `tests/smoke_test.py:266-278`,
# `hooks/hooks.json:9`, `skills/ccm-load/SKILL.md:137`, `plugin.json:4`.
#
# NOT `.py`-only. It was, and that silently exempted 25 citations in the
# tracked docs from the invariant CLAUDE.md states as "no citation may be
# UNCHECKED" — two of them already rotten in exactly the way the BOUNDS
# branch exists to catch (`cc_memory/config.json:63` in a 29-line file;
# `skills/ccm-load/SKILL.md:137`, a blank line). A non-Python file has no
# AST, so those citations get the bounds check and nothing more — a weaker
# verdict than the symbol anchor, and infinitely stronger than none.
_CITED_EXTS = ("py", "json", "md", "toml", "cfg", "ini", "txt", "yml", "yaml")
CITATION_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:" + "|".join(_CITED_EXTS) + r"))"
    r":(?P<start>\d+)(?:\s*[-–]\s*(?P<end>\d+))?")

# An identifier, optionally dotted: `core.plan.raw_pending_refinement`,
# `MemoryDB.supersede_memory`, `_reserve_archive_ts`.
SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

# Sentence-ish window around a citation: docs mix prose, tables and bullets, so
# split on the separators that actually end a thought in this repo's style.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;])\s+|\s+\|\s+|\s+—\s+|\s+--\s+")

# A verbatim region: quoted evidence, verified AGAINST its source instead of
# scanned for citations. See the module docstring. The grammar is deliberately
# not `<!--ce:...-->` — that prefix belongs to tools/doc_claims.py.
VERBATIM_OPEN_RE = re.compile(r"<!--\s*verbatim:\s*(?P<src>\S+)\s*-->")
VERBATIM_CLOSE_RE = re.compile(r"<!--\s*/verbatim\s*-->")
_ELISION_RE = re.compile(r"\[(?:…|\.\.\.)\]")
_QUOTE_PREFIX_RE = re.compile(r"^\s*>\s?")
_FENCE_RE = re.compile(r"^\s*```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _verbatim_regions(doc_lines):
    """Split one document into (skip-set, regions).

    Returns the 1-based line numbers no citation scan may touch, and a list
    of ``(open_line, source, body_lines, closed)`` regions. Both marker lines
    are in the skip-set too: the opening marker names a path that could
    itself look like a citation.
    """
    skip, regions = set(), []
    current = None
    for n, line in enumerate(doc_lines, 1):
        # A marker quoted in inline code is a DESCRIPTION of the grammar, not
        # a region: CLAUDE.md and CHANGELOG.md both show
        # `<!-- verbatim: <capture> -->` in backticks, and the first run
        # opened a region at each of them (one swallowed 1,470 lines of
        # CLAUDE.md up to the next description; the other never closed).
        bare = _INLINE_CODE_RE.sub("", line)
        if current is None:
            m = VERBATIM_OPEN_RE.search(bare)
            if m:
                current = [n, m.group("src"), [], False]
                skip.add(n)
            continue
        skip.add(n)
        if VERBATIM_CLOSE_RE.search(bare):
            current[3] = True
            regions.append(tuple(current))
            current = None
        else:
            current[2].append(line)
    if current is not None:
        regions.append(tuple(current))
    return skip, regions


def _verbatim_results(root: Path, rel: str, regions):
    """One Result per region: VERBATIM when every segment is in the source,
    QUOTE (a failure) otherwise. A QUOTE names the first 70 characters of the
    segment that was not found, so the drift is locatable without a diff."""
    out = []
    for open_line, src, body, closed in regions:
        if not closed:
            out.append(Result(rel, open_line, src, 0, 0, "QUOTE",
                              detail="verbatim region never closed"))
            continue
        target = root / src
        if not target.is_file():
            out.append(Result(rel, open_line, src, 0, 0, "QUOTE",
                              detail="source file does not exist"))
            continue
        haystack = _collapse(target.read_text(encoding="utf-8",
                                              errors="replace"))
        text = " ".join(_QUOTE_PREFIX_RE.sub("", ln) for ln in body
                        if not _FENCE_RE.match(ln))
        segments = [s for s in (_collapse(x) for x in _ELISION_RE.split(text))
                    if s]
        if not segments:
            out.append(Result(rel, open_line, src, 0, 0, "QUOTE",
                              detail="empty verbatim region"))
            continue
        missing = [s for s in segments if s not in haystack]
        if missing:
            out.append(Result(rel, open_line, src, 0, 0, "QUOTE",
                              detail='not in source: "%s%s"'
                              % (missing[0][:70],
                                 "…" if len(missing[0]) > 70 else "")))
        else:
            out.append(Result(rel, open_line, src, 0, 0, "VERBATIM",
                              detail="%d segment(s) found in source"
                              % len(segments)))
    return out


def _is_constant_name(name: str) -> bool:
    """ALL_CAPS (or _ALL_CAPS) — the only assignments the docs actually cite."""
    core = name.lstrip("_")
    return bool(core) and core.upper() == core and any(c.isalpha() for c in core)


def _defs_in(path: Path):
    """{symbol: [(first_line, last_line)]} for one Python file.

    Records functions, classes and ALL_CAPS module constants, under both the
    bare name and the ``Owner.name`` form so a citation may name either.

    Ordinary assignments are DELIBERATELY excluded. Including them made the
    checker anchor a sentence's prose word onto some unrelated local: `db`,
    `pid`, `window`, `status`, `plan` and `events` all exist as assignment
    targets somewhere in this tree, so ~40 correct citations were reported
    STALE against a variable they never referred to. A checker with that
    false-positive rate is worse than no checker — it teaches you to ignore it,
    which is how the citations rotted in the first place.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
    except (OSError, SyntaxError):
        return None
    out = {}

    def add(name, node, owner=None):
        span = (node.lineno, getattr(node, "end_lineno", node.lineno))
        out.setdefault(name, []).append(span)
        if owner:
            out.setdefault(f"{owner}.{name}", []).append(span)

    def walk(node, owner=None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(child.name, child, owner)
                walk(child, owner)
            elif isinstance(child, ast.ClassDef):
                add(child.name, child, owner)
                walk(child, child.name)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = (child.targets if isinstance(child, ast.Assign)
                           else [child.target])
                for t in targets:
                    if isinstance(t, ast.Name) and _is_constant_name(t.id):
                        add(t.id, child, owner)
            else:
                walk(child, owner)

    walk(tree)
    return out


def _resolve_path(root: Path, cited: str):
    """Resolve a cited path. Returns ``(path, note)``.

    A citation may be repo-relative or bare (`db.py`). A bare name matching
    more than one file — this repo has both core/plan.py and cli/plan.py —
    resolves to nothing: guessing which one a sentence meant is exactly the
    kind of invention this tool exists to replace. That is reported as SKIP,
    not FAIL: the citation may be perfectly correct.
    """
    direct = root / cited
    if direct.is_file():
        return direct, None
    matches = [p for p in root.rglob(cited.split("/")[-1])
               if "__pycache__" not in p.parts and p.is_file()
               and p.as_posix().endswith(cited)]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        rels = sorted(p.relative_to(root).as_posix() for p in matches)
        return None, f"ambiguous bare filename, matches {rels}"
    return None, "no such file in the tree"


_GLOBAL_DEFS = None


def _global_defs(root: Path):
    """Every function / class / ALL_CAPS name defined anywhere in the tree.

    The cross-file anchor needs this. Without it the tool happily "anchors" on
    an ordinary English word that occurs in the cited file — the first run
    accused a citation of rot because the word *guardian* appears at five lines
    of core/plan.py and not at the cited ones. A candidate must be a name that
    is actually a symbol SOMEWHERE, or the whole verdict is noise.
    """
    global _GLOBAL_DEFS
    if _GLOBAL_DEFS is None:
        names = set()
        for py in root.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            d = _defs_in(py)
            if d:
                names |= {k for k in d if "." not in k}
        _GLOBAL_DEFS = names
    return _GLOBAL_DEFS


_SRC_CACHE = {}


def _lines_of(path: Path):
    """Source lines of a cited file, cached (each is read many times)."""
    key = str(path)
    if key not in _SRC_CACHE:
        _SRC_CACHE[key] = path.read_text(encoding="utf-8",
                                         errors="replace").splitlines()
    return _SRC_CACHE[key]


def _context(line: str, at: int) -> str:
    """The sentence containing the citation at offset `at`."""
    start = 0
    for m in SENTENCE_SPLIT_RE.finditer(line):
        if m.end() <= at:
            start = m.end()
        else:
            break
    end = len(line)
    for m in SENTENCE_SPLIT_RE.finditer(line):
        if m.start() > at:
            end = m.start()
            break
    return line[start:end]


class Result:
    __slots__ = ("doc", "docline", "cited", "start", "end", "verdict",
                 "symbol", "want", "detail")

    def __init__(self, doc, docline, cited, start, end, verdict,
                 symbol=None, want=None, detail=""):
        self.doc, self.docline, self.cited = doc, docline, cited
        self.start, self.end, self.verdict = start, end, verdict
        self.symbol, self.want, self.detail = symbol, want, detail


def _bounds_result(rel, n, cited, start, end, src):
    """The weakest verdict that is still a verdict — never "unchecked".

    A citation nothing can anchor on can still be WRONG in a mechanically
    decidable way: it can point past the end of the file, or at nothing but
    blank lines. 23 of the 253 previously-unanchored citations were exactly
    that. Reported separately (BOUNDS) rather than counted as a full
    verdict, so the strength of the check is never overstated — but the
    number of citations NOTHING checks stays zero. Used by BOTH the
    last-resort branch for Python files and every non-Python citation,
    which have no AST to anchor against at all.
    """
    n_lines = len(src)
    live = [i for i in range(start, min(end, n_lines) + 1)
            if i >= 1 and src[i - 1].strip()]
    if end > n_lines:
        return Result(rel, n, cited, start, end, "STALE", None, None,
                      f"cites line {end} of a {n_lines}-line file")
    if not live:
        return Result(rel, n, cited, start, end, "STALE", None, None,
                      f"lines {start}-{end} are blank")
    return Result(rel, n, cited, start, end, "BOUNDS", None, None,
                  "in range, no symbol to anchor on")


def classify(root: Path):
    results = []
    defs_cache = {}
    for rel in TRACKED:
        doc = root / rel
        if not doc.is_file():
            continue
        doc_lines = doc.read_text(encoding="utf-8").splitlines()
        skip, regions = _verbatim_regions(doc_lines)
        results.extend(_verbatim_results(root, rel, regions))
        for n, line in enumerate(doc_lines, 1):
            if n in skip:
                # Quoted evidence: verified against its source above, and
                # never scanned — a `file:line` inside a transcript is what
                # the capture said, not a claim about this tree.
                continue
            for m in CITATION_RE.finditer(line):
                cited = m.group("path")
                start = int(m.group("start"))
                end = int(m.group("end")) if m.group("end") else start
                target, note = _resolve_path(root, cited)
                if target is None and note and "ambiguous" in note:
                    # Disambiguate by SYMBOL rather than giving up. This repo
                    # has both cli/plan.py and core/plan.py, and 13 citations
                    # said only `plan.py`; the sentence around each names a
                    # symbol that exists in exactly one of them.
                    cands = [p for p in root.rglob(cited.split("/")[-1])
                             if "__pycache__" not in p.parts and p.is_file()
                             and p.as_posix().endswith(cited)]
                    ctx_all = "\n".join(doc_lines[max(0, n - 2):n + 1])
                    names = {s.split(".")[-1]
                             for s in SYMBOL_RE.findall(ctx_all)}
                    owning = [p for p in cands
                              if (_defs_in(p) or {}).keys() & names]
                    if len(owning) == 1:
                        target, note = owning[0], None
                if target is None:
                    # An ambiguous bare filename is NOT rot — the citation may
                    # be exactly right and this tool simply cannot tell which
                    # file it means. Only a genuinely absent file is a failure.
                    verdict = "SKIP" if "ambiguous" in (note or "") else "MISSING"
                    results.append(Result(rel, n, cited, start, end, verdict,
                                          detail=note))
                    continue
                if target.suffix != ".py":
                    # No AST, so no symbol anchor — but a line number is still
                    # mechanically checkable, and the two rotten citations
                    # this branch was added for were both found by it.
                    results.append(_bounds_result(rel, n, cited, start, end,
                                                  _lines_of(target)))
                    continue
                key = str(target)
                if key not in defs_cache:
                    defs_cache[key] = _defs_in(target)
                defs = defs_cache[key]
                if defs is None:
                    results.append(Result(rel, n, cited, start, end, "SKIP",
                                          detail="file could not be parsed"))
                    continue
                # Sentence first; if that names nothing resolvable, widen to the
                # neighbouring lines. Markdown prose wraps, so the symbol a
                # citation belongs to is regularly on the line ABOVE it — the
                # sentence-only window called those SKIP and let real rot
                # through (`core/db.py:450` for `db.insert_memory`, whose name
                # sat one line up).
                hit = None
                for ctx in (_context(line, m.start()),
                            "\n".join(doc_lines[max(0, n - 2):n + 1])):
                    # Longest match first: `MemoryDB.supersede_memory` should
                    # win over the bare `MemoryDB` that is a prefix of it.
                    # The tiebreak on the NAME is load-bearing, not tidiness:
                    # sorting a set by length alone leaves equal-length symbols
                    # in set-iteration order, which PYTHONHASHSEED randomises
                    # per process — so the same citation could anchor on a
                    # different symbol run to run, and the gate reported a
                    # different number of stale citations each time it ran.
                    for name in sorted({s for s in SYMBOL_RE.findall(ctx)},
                                       key=lambda s: (-len(s), s)):
                        tail = name.split(".")[-1]
                        for cand in (name, tail,
                                     ".".join(name.split(".")[-2:])):
                            spans = defs.get(cand)
                            if spans and len(spans) == 1:
                                hit = (cand, spans[0])
                                break
                        if hit:
                            break
                    if hit:
                        break
                if hit is None:
                    # CROSS-FILE citation: the symbol the sentence names lives
                    # in another module and this line is a CALL SITE. That is
                    # the single most common shape in these docs —
                    #   "`db.tag_progress_session(...)` (`user_prompt.py:117`)"
                    # — and treating it as unanchorable left 357 of 594
                    # citations, 60 %, entirely unchecked. Anchor it on the
                    # TEXT instead: the cited range must mention the symbol.
                    src = _lines_of(target)
                    cand = None
                    for ctx in (_context(line, m.start()),
                                "\n".join(doc_lines[max(0, n - 2):n + 1])):
                        for name in sorted(
                                {s for s in SYMBOL_RE.findall(ctx)},
                                key=lambda s: (-len(s), s)):
                            tail = name.split(".")[-1]
                            # Two filters, both load-bearing:
                            #  * >=6 chars — short identifiers ('db', 'path',
                            #    'main') match half the lines in any file, so a
                            #    hit proves nothing and a miss is a false
                            #    accusation;
                            #  * must be a real symbol SOMEWHERE in the tree —
                            #    without this the anchor lands on ordinary
                            #    prose. Measured: the word 'guardian' occurs at
                            #    5 lines of core/plan.py, so a correct citation
                            #    that did not include one of them was reported
                            #    as rot.
                            if len(tail) < 6 or tail not in _global_defs(root):
                                continue
                            occ = [i + 1 for i, ln in enumerate(src)
                                   if tail in ln]
                            if occ:
                                cand = (tail, occ)
                                break
                        if cand:
                            break
                    if cand is None:
                        results.append(_bounds_result(rel, n, cited, start,
                                                      end, src))
                        continue
                    tail, occ = cand
                    if any(start <= o <= end for o in occ):
                        results.append(Result(rel, n, cited, start, end, "OK",
                                              tail, None,
                                              "cross-file reference"))
                    else:
                        # Repair to the occurrence NEAREST the stale number.
                        # The assumption is stated so it can be argued with: a
                        # citation was correct when written and the file grew
                        # above it, so the drift is small and monotonic. Every
                        # candidate is a line that genuinely mentions the
                        # symbol — this chooses among true anchors, it does not
                        # invent one — and the choice is reported, not silent.
                        near = min(occ, key=lambda o: (abs(o - start), o))
                        results.append(Result(
                            rel, n, cited, start, end, "STALE", tail,
                            (near, near),
                            f"{tail} does not appear at {start}-{end}; "
                            f"nearest occurrence {near}"
                            + (f" of {len(occ)}" if len(occ) > 1 else "")))
                    continue
                sym, (d0, d1) = hit
                # A citation is correct if it points at the symbol's DEFINITION
                # *or* at a line that mentions it — the docs cite call sites at
                # least as often as definitions, and one line of CONTRACTS.md
                # legitimately does both:
                #   "PreCompact `_first_user_request(window.head)`
                #    (`pre_compact.py:456`) ... skips empty-content user rows
                #    (`pre_compact.py:254-278`)"
                # Requiring the definition would have called the first of those
                # rot and "fixed" it into pointing somewhere it never meant.
                if start <= d1 and end >= d0:
                    results.append(Result(rel, n, cited, start, end, "OK", sym,
                                          (d0, d1)))
                    continue
                src = _lines_of(target)
                tail = sym.split(".")[-1]
                window = src[max(0, start - 1):min(len(src), end)]
                if any(tail in ln for ln in window):
                    results.append(Result(rel, n, cited, start, end, "OK", sym,
                                          (d0, d1),
                                          "reference, not definition"))
                else:
                    results.append(Result(rel, n, cited, start, end, "STALE",
                                          sym, (d0, d1),
                                          f"{sym} is neither defined nor "
                                          f"mentioned there; defined at "
                                          f"{d0}-{d1}"))
    return results


def apply_fix(root: Path, results):
    """Rewrite STALE line numbers to the resolved definition site."""
    by_doc = {}
    for r in results:
        # `want is None` marks a STALE the tool refuses to guess at (a
        # cross-file symbol occurring at several lines). Reported, never
        # rewritten: a plausible-looking wrong number is worse than a red gate.
        if r.verdict == "STALE" and r.want:
            by_doc.setdefault(r.doc, []).append(r)
    changed = 0
    for rel, rows in by_doc.items():
        doc = root / rel
        lines = doc.read_text(encoding="utf-8").splitlines(keepends=True)
        # Group by document line, then splice by CHARACTER OFFSET, right to
        # left. Substring replacement is wrong here and produced a mangled
        # citation on the first run: `str.replace("…memory_writer.py:83",
        # "…memory_writer.py:55")` applied to the text `…memory_writer.py:83-83`
        # matches the PREFIX of the range and yields `…memory_writer.py:55-83`,
        # a range that never existed. Right-to-left keeps earlier offsets valid
        # when one documentation line carries several citations, which these
        # docs do constantly (tables list three or four).
        by_line = {}
        for r in rows:
            by_line.setdefault(r.docline, []).append(r)
        for docline, rs in by_line.items():
            i = docline - 1
            text = lines[i]
            spans = []
            for m in CITATION_RE.finditer(text):
                m_start = int(m.group("start"))
                m_end = int(m.group("end")) if m.group("end") else m_start
                for r in rs:
                    if (m.group("path") == r.cited and m_start == r.start
                            and m_end == r.end):
                        d0, d1 = r.want
                        # A one-line target gets a single number, never "83-83":
                        # a degenerate range reads as a typo and invites a
                        # "correction" that undoes this fix.
                        new = (f"{r.cited}:{d0}" if d0 == d1
                               else f"{r.cited}:{d0}-{d1}")
                        spans.append((m.start(), m.end(), new))
                        rs.remove(r)
                        break
            for a, b, new in sorted(spans, reverse=True):
                text = text[:a] + new + text[b:]
                changed += 1
            lines[i] = text
        doc.write_text("".join(lines), encoding="utf-8")
    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--list", action="store_true",
                    help="print every citation, not only the failures")
    ap.add_argument("--fix", action="store_true",
                    help="rewrite STALE line numbers in place")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    results = classify(root)
    if args.fix:
        n = apply_fix(root, results)
        print(f"rewrote {n} stale citation(s); re-checking\n")
        results = classify(root)

    counts = {}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    for r in results:
        if r.verdict in ("STALE", "MISSING", "QUOTE") or args.list:
            tag = {"OK": "[OK]   ", "SKIP": "[SKIP] ", "BOUNDS": "[BNDS] ",
                   "STALE": "[STALE]", "MISSING": "[FAIL] ",
                   "VERBATIM": "[VERB] ", "QUOTE": "[QUOTE]"}[r.verdict]
            if r.verdict in ("VERBATIM", "QUOTE"):
                # A region, not a citation: no line span to print.
                print(f"{tag} {r.doc}:{r.docline}  ->  {r.cited}"
                      + (f"   {r.detail}" if r.detail else ""))
                continue
            span = r.start if r.start == r.end else f"{r.start}-{r.end}"
            print(f"{tag} {r.doc}:{r.docline}  ->  {r.cited}:{span}"
                  + (f"   {r.detail}" if r.detail else ""))

    n_regions = counts.get("VERBATIM", 0) + counts.get("QUOTE", 0)
    total = len(results) - n_regions
    print(f"\nSummary: {total} citations — "
          + ", ".join(f"{counts.get(k, 0)} {k.lower()}"
                      for k in ("OK", "BOUNDS", "SKIP", "STALE", "MISSING"))
          + f"; {n_regions} verbatim region(s) — "
          f"{counts.get('VERBATIM', 0)} verified, {counts.get('QUOTE', 0)} quote")
    # Two strengths, stated as two numbers rather than one "checked" total:
    # a symbol-anchored citation was VERIFIED to cover or mention its symbol;
    # a bounds-only one was found in range and non-blank, which a citation
    # that has drifted onto the wrong line also is. Five tag-emitter citations
    # in CLAUDE.md sat 24 lines off their targets under a green "checked:
    # 624 of 624" (measured, v2.13.2) — the summary must not overstate.
    print(f"         verified against a symbol: {counts.get('OK', 0)}; "
          f"bounds-only (in range and non-blank, NOT verified against a "
          f"symbol): {counts.get('BOUNDS', 0)}; unchecked: {counts.get('SKIP', 0)}")
    bad = (counts.get("STALE", 0) + counts.get("MISSING", 0)
           + counts.get("QUOTE", 0))
    print("Result: " + ("OK (no rot detectable)" if not bad
                        else f"FAIL ({bad} citation(s) do not cover their "
                             f"symbol's definition or quote their source)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
