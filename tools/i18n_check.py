#!/usr/bin/env python3
"""
i18n_check.py — documentation translation drift checker for cc-memory.

Enforces the "English skeleton + drift-tracked translation" convention documented
in docs/I18N.md:

  * ``NAME.md``    is the canonical English source (the skeleton).
  * ``NAME.zh.md`` is a Chinese sibling. Its FIRST line carries a machine-readable
    marker recording a hash of the English source it was translated from::

        <!-- i18n-source: README.md | sha256: <16hex> | version: 2.3.2 | translated: 2026-07-11 -->

Drift is decided SOLELY by the sha256 of the *normalized* English source (``version``
and ``translated`` are informational, so a future version bump never mass-flags
translations as stale). Both emit-time (``--emit-marker``) and check-time run the
SAME normalizer, so CRLF/LF, a UTF-8 BOM, or trailing-whitespace churn cannot move
the digest — the single cross-platform-critical property of this tool.

This file is a DEV/CI tool. It lives OUTSIDE the ``cc_memory`` package on purpose and
is deliberately NOT added to ``ui/installer.py`` SUBPACKAGE_FILES, ``build_exe.py``, or
``cli/mem.py`` _REQUIRED_PLUGIN_FILES — the packaged plugin is unchanged by it.

Pure stdlib (hashlib, pathlib, re, sys, argparse, datetime, collections).

Usage
-----
    python tools/i18n_check.py                    # check every tracked doc; exit nonzero on drift
    python tools/i18n_check.py --list             # show each English/翻译 pair + recorded vs current hash
    python tools/i18n_check.py --emit-marker README.md   # print a fresh marker line for a translation
    python tools/i18n_check.py --root /path/to/repo      # override the repo root (default: this file's repo)

Exit codes: 0 = no drift (IN-SYNC or MISSING-TRANSLATION only); 1 = drift
(STALE / ORPHAN / NO-MARKER present).
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import namedtuple
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------- #
# Marker format + parser
# --------------------------------------------------------------------------- #

MARKER_FMT = (
    "<!-- i18n-source: {source} | sha256: {digest} | "
    "version: {version} | translated: {date} -->"
)

MARKER_RE = re.compile(
    r"<!--\s*i18n-source:\s*(?P<source>\S+)\s*\|\s*"
    r"sha256:\s*(?P<digest>[0-9a-f]{16})\s*\|\s*"
    r"version:\s*(?P<version>\S+)\s*\|\s*"
    r"translated:\s*(?P<date>\d{4}-\d{2}-\d{2})\s*-->"
)

# One row per doc (or orphan translation). english_rel / zh_rel are repo-relative
# posix paths (or None when not applicable); detail is a human-readable note.
Result = namedtuple("Result", ["state", "english_rel", "zh_rel", "detail"])

# States that make the checker fail (nonzero exit). MISSING-TRANSLATION is a soft
# warning (a translation simply hasn't been produced yet), never a failure.
FAIL_STATES = {"STALE", "ORPHAN", "NO-MARKER"}

_LABEL = {
    "IN-SYNC": "[OK]  ",
    "STALE": "[STALE]",
    "MISSING-TRANSLATION": "[WARN]",
    "ORPHAN": "[FAIL]",
    "NO-MARKER": "[FAIL]",
}

_ZH_SUFFIX = ".zh.md"


# --------------------------------------------------------------------------- #
# Normalization + hashing (cross-platform-stable)
# --------------------------------------------------------------------------- #

def normalize_markdown(raw: bytes) -> str:
    """Canonicalize markdown bytes so the hash is stable across platforms.

    Strip a UTF-8 BOM, decode utf-8, fold CRLF/CR -> LF, rstrip every line, and
    end with exactly one trailing newline. Emit-time and check-time both call this,
    so line-ending or trailing-whitespace churn cannot change the digest.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.rstrip("\n") + "\n"


def hash_source(path: Path) -> str:
    """Return the 16-hex-char sha256 prefix of the normalized English source."""
    return hashlib.sha256(
        normalize_markdown(path.read_bytes()).encode("utf-8")
    ).hexdigest()[:16]


def parse_marker(path: Path):
    """Return the marker dict from a translation's first line, or None (fail-closed).

    BOM-tolerant. Any read/decode error, or a first line that doesn't match the
    marker grammar, yields None -> the caller reports NO-MARKER.
    """
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        text = raw.decode("utf-8", errors="replace")
        first_line = text.split("\n", 1)[0]
        m = MARKER_RE.search(first_line)
        return m.groupdict() if m else None
    except Exception:
        # why: fail-closed is the contract — an unreadable/undecodable translation
        # must surface as NO-MARKER (a FAIL state), never be silently treated as valid.
        return None


# --------------------------------------------------------------------------- #
# Path conventions
# --------------------------------------------------------------------------- #

def english_source_for(zh_path: Path) -> Path:
    """Map a translation path to its expected English source (NAME.zh.md -> NAME.md)."""
    name = zh_path.name
    if name.endswith(_ZH_SUFFIX):
        eng_name = name[: -len(_ZH_SUFFIX)] + ".md"
    else:  # defensive: treat any other suffix as already-English
        eng_name = name
    return zh_path.with_name(eng_name)


def zh_sibling_for(english_path: Path) -> Path:
    """Map an English source to its expected translation path (NAME.md -> NAME.zh.md)."""
    return english_path.with_name(english_path.name[: -len(".md")] + _ZH_SUFFIX)


def discover_english(root: Path):
    """Tracked English docs: README.md at root + docs/*.md (excluding *.zh.md)."""
    files = []
    readme = root / "README.md"
    if readme.exists():
        files.append(readme)
    docs = root / "docs"
    if docs.is_dir():
        for p in sorted(docs.glob("*.md")):
            if not p.name.endswith(_ZH_SUFFIX):
                files.append(p)
    return files


def discover_translations(root: Path):
    """Tracked translations: README.zh.md at root + docs/*.zh.md (non-recursive)."""
    files = []
    for base in (root, root / "docs"):
        if base.is_dir():
            files.extend(sorted(base.glob("*" + _ZH_SUFFIX)))
    return files


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except Exception:
        # why: paths outside root can't be made relative; fall back to an absolute
        # posix string for display only (this is cosmetic, never used for hashing).
        return path.as_posix()


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

def classify(root: Path):
    """Return a list of Result rows for every tracked English doc + orphan translation."""
    root = Path(root)
    results = []
    english = discover_english(root)

    for eng in english:
        zh = zh_sibling_for(eng)
        eng_rel = _rel(root, eng)
        if not zh.exists():
            results.append(Result(
                "MISSING-TRANSLATION", eng_rel, None, "no .zh.md sibling yet"))
            continue
        zh_rel = _rel(root, zh)
        marker = parse_marker(zh)
        if marker is None:
            results.append(Result(
                "NO-MARKER", eng_rel, zh_rel, "first line has no valid i18n marker"))
            continue
        current = hash_source(eng)
        if marker["digest"] == current:
            results.append(Result(
                "IN-SYNC", eng_rel, zh_rel, f"sha256={current}"))
        else:
            results.append(Result(
                "STALE", eng_rel, zh_rel,
                f"recorded={marker['digest']} current={current} — English source changed"))

    # Orphans: a translation whose English source is gone/renamed.
    for zh in discover_translations(root):
        eng = english_source_for(zh)
        if not eng.exists():
            results.append(Result(
                "ORPHAN", None, _rel(root, zh),
                f"missing English source {_rel(root, eng)}"))

    return results


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def print_report(results, verbose=False, root=None):
    """Print one line per Result plus a summary; return True if any FAIL state present."""
    if not results:
        print("i18n: no tracked docs found.")
        return False

    for r in results:
        doc = r.english_rel or r.zh_rel
        label = _LABEL.get(r.state, "[?]")
        print(f"{label} {r.state:<20} {doc}")
        if verbose or r.state in FAIL_STATES:
            detail = r.detail
            if r.zh_rel and r.english_rel and r.state != "MISSING-TRANSLATION":
                detail = f"{r.zh_rel} <- {r.english_rel} :: {detail}"
            print(f"        {detail}")

    counts = {}
    for r in results:
        counts[r.state] = counts.get(r.state, 0) + 1
    summary = ", ".join(f"{counts[s]} {s.lower()}" for s in sorted(counts))
    has_fail = any(r.state in FAIL_STATES for r in results)
    print()
    print(f"Summary: {summary}")
    print("Result: " + ("DRIFT DETECTED" if has_fail else "OK (no drift)"))
    return has_fail


def print_list(root: Path):
    """--list: every English/translation pair with recorded vs current hash."""
    results = classify(root)
    for r in results:
        if r.state == "MISSING-TRANSLATION":
            print(f"{r.english_rel}: (no translation)")
            continue
        if r.state == "ORPHAN":
            print(f"{r.zh_rel}: ORPHAN — {r.detail}")
            continue
        zh = root / r.zh_rel
        eng = root / r.english_rel
        marker = parse_marker(zh)
        recorded = marker["digest"] if marker else "(none)"
        current = hash_source(eng)
        match = "==" if recorded == current else "!="
        print(f"{r.english_rel}  ->  {r.zh_rel}")
        print(f"    recorded {recorded} {match} current {current}   [{r.state}]")


# --------------------------------------------------------------------------- #
# Marker emission
# --------------------------------------------------------------------------- #

def _read_version(root: Path) -> str:
    """Best-effort read of cc_memory.__version__ for the marker's informational field."""
    init = root / "cc_memory" / "__init__.py"
    try:
        text = init.read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            return m.group(1)
    except Exception:
        # why: version is an informational marker field only (drift is decided by the
        # sha256, not this string). A missing/renamed __init__ must not crash the tool.
        return "0.0.0"
    return "0.0.0"


def emit_marker(english_path: Path, version: str, when: str) -> str:
    """Build the marker line for a translation of ``english_path``."""
    return MARKER_FMT.format(
        source=english_path.name,
        digest=hash_source(english_path),
        version=version,
        date=when,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _default_root() -> Path:
    # tools/i18n_check.py -> repo root is the parent of tools/. CWD-independent.
    return Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Documentation translation drift checker (English skeleton + *.zh.md).")
    parser.add_argument(
        "--root", type=Path, default=None,
        help="Repo root (default: the repo containing this script).")
    parser.add_argument(
        "--list", action="store_true", dest="do_list",
        help="List every English/translation pair with recorded vs current hash.")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show the detail line for every doc, not just failures.")
    parser.add_argument(
        "--emit-marker", metavar="ENGLISH_DOC",
        help="Print a fresh marker line for a translation of ENGLISH_DOC and exit.")
    parser.add_argument(
        "--version-label", default=None,
        help="Version string for --emit-marker (default: read cc_memory.__version__).")
    parser.add_argument(
        "--date", default=None,
        help="translated: date for --emit-marker (default: today, YYYY-MM-DD).")
    args = parser.parse_args(argv)

    root = (args.root or _default_root()).resolve()

    if args.emit_marker:
        eng = Path(args.emit_marker)
        if not eng.is_absolute():
            eng = root / eng
        if not eng.exists():
            print(f"error: English source not found: {eng}", file=sys.stderr)
            return 2
        version = args.version_label or _read_version(root)
        when = args.date or date.today().isoformat()
        print(emit_marker(eng, version, when))
        return 0

    if args.do_list:
        print_list(root)
        return 0

    results = classify(root)
    has_fail = print_report(results, verbose=args.verbose, root=root)
    return 1 if has_fail else 0


if __name__ == "__main__":
    sys.exit(main())
