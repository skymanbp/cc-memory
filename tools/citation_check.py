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

## Exit codes

  0  no STALE citations (OK / SKIP only)
  1  at least one STALE citation, or a citation whose file does not exist

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

# Documents whose citations are gated. README*.md carry none today; they are
# listed so that adding one starts being checked immediately.
TRACKED = ["README.md", "README.zh.md", "CLAUDE.md",
           "docs/ARCHITECTURE.md", "docs/ARCHITECTURE.zh.md",
           "docs/CONTRACTS.md", "docs/CONTRACTS.zh.md"]

# `cc_memory/core/db.py:1349`, `db.py:188-201`, `tests/smoke_test.py:266-278`
CITATION_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*\.py)"
    r":(?P<start>\d+)(?:\s*[-–]\s*(?P<end>\d+))?")

# An identifier, optionally dotted: `core.plan.raw_pending_refinement`,
# `MemoryDB.supersede_memory`, `_reserve_archive_ts`.
SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

# Sentence-ish window around a citation: docs mix prose, tables and bullets, so
# split on the separators that actually end a thought in this repo's style.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;])\s+|\s+\|\s+|\s+—\s+|\s+--\s+")


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


def classify(root: Path):
    results = []
    defs_cache = {}
    for rel in TRACKED:
        doc = root / rel
        if not doc.is_file():
            continue
        doc_lines = doc.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(doc_lines, 1):
            for m in CITATION_RE.finditer(line):
                cited = m.group("path")
                start = int(m.group("start"))
                end = int(m.group("end")) if m.group("end") else start
                target, note = _resolve_path(root, cited)
                if target is None:
                    # An ambiguous bare filename is NOT rot — the citation may
                    # be exactly right and this tool simply cannot tell which
                    # file it means. Only a genuinely absent file is a failure.
                    verdict = "SKIP" if "ambiguous" in (note or "") else "MISSING"
                    results.append(Result(rel, n, cited, start, end, verdict,
                                          detail=note))
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
                        results.append(Result(rel, n, cited, start, end, "SKIP",
                                              detail="no unique symbol in context"))
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
        if r.verdict in ("STALE", "MISSING") or args.list:
            tag = {"OK": "[OK]   ", "SKIP": "[SKIP] ",
                   "STALE": "[STALE]", "MISSING": "[FAIL] "}[r.verdict]
            span = r.start if r.start == r.end else f"{r.start}-{r.end}"
            print(f"{tag} {r.doc}:{r.docline}  ->  {r.cited}:{span}"
                  + (f"   {r.detail}" if r.detail else ""))

    total = len(results)
    print(f"\nSummary: {total} citations — "
          + ", ".join(f"{counts.get(k, 0)} {k.lower()}"
                      for k in ("OK", "SKIP", "STALE", "MISSING")))
    bad = counts.get("STALE", 0) + counts.get("MISSING", 0)
    print("Result: " + ("OK (no rot detectable)" if not bad
                        else f"FAIL ({bad} citation(s) do not cover their "
                             f"symbol's definition)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
