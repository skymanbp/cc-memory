#!/usr/bin/env python3
"""Extract one version's section from CHANGELOG.md, for the release body.

Usage: python scripts/release_notes.py 2.12.0 --out notes.md

Prefer --out over shell redirection: a `>` hands the bytes to the capturing
shell, which decodes them with its own codec (PowerShell 5.1 uses the console
codepage), and the CHANGELOG deliberately contains characters — including a
literal U+FFFD in the v2.12.0 entry — that GBK/cp1252 cannot encode. Writing
the file ourselves keeps the bytes UTF-8 end to end; this is the same failure
class `/cc-mem --json` exists for.

Fails loud (exit 1) when the section is absent: a release whose notes cannot
be traced to a CHANGELOG entry is a release the CHANGELOG does not know about,
and generating notes from commit subjects instead would silently paper over
that. CHANGELOG.md is this project's single history — the release body quotes
it, never replaces it.

Pure stdlib, like everything else in scripts/ and tools/.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _take_option(args, name):
    """Pop `name VALUE` out of args; return (value_or_None, remaining)."""
    if name not in args:
        return None, args
    i = args.index(name)
    if i + 1 >= len(args):
        raise SystemExit(f"ERROR: {name} requires a path")
    return args[i + 1], args[:i] + args[i + 2:]


def _release_title(version, body):
    """'v2.12.0 — the field report release: …' from the section's first
    `### ` heading, first letter lowercased — the shape every release on the
    project's Releases page has carried since v2.11.x, so the CI-published
    ones sit in the same list without a visible seam."""
    for line in body.splitlines():
        if line.startswith("### "):
            heading = line[4:].strip()
            if heading:
                return f"v{version} — {heading[0].lower()}{heading[1:]}"
    raise SystemExit(f"ERROR: CHANGELOG section {version} has no '### ' "
                     f"headline to title the release with")


def main():
    args = sys.argv[1:]
    out_path, args = _take_option(args, "--out")
    title_path, args = _take_option(args, "--title-out")
    if len(args) != 1:
        print("usage: release_notes.py <version, e.g. 2.12.0> "
              "[--out FILE] [--title-out FILE]", file=sys.stderr)
        return 2
    version = args[0].lstrip("v")
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    # Heading shape used throughout: "## [2.12.0] — 2026-08-26". The dash
    # between ] and the date has varied historically, so only the bracketed
    # version anchors the match.
    pattern = re.compile(
        r"^## \[" + re.escape(version) + r"\][^\n]*\n(.*?)(?=^## \[|\Z)",
        re.M | re.S,
    )
    m = pattern.search(text)
    if not m:
        print(f"ERROR: CHANGELOG.md has no '## [{version}]' section",
              file=sys.stderr)
        return 1
    body = m.group(1).strip("\n") + "\n"
    if not body.strip():
        print(f"ERROR: CHANGELOG.md section for {version} is empty",
              file=sys.stderr)
        return 1
    if title_path:
        title = _release_title(version, body)
        Path(title_path).write_text(title + "\n", encoding="utf-8",
                                    newline="\n")
        print(f"wrote title to {title_path}: {title}", file=sys.stderr)
    if out_path:
        Path(out_path).write_text(body, encoding="utf-8", newline="\n")
        print(f"wrote {len(body)} chars to {out_path}", file=sys.stderr)
    elif not title_path:
        # Stdout kept for eyeballing; forced UTF-8 so a GBK console cannot
        # crash the print. Redirection still risks the shell's codec — use
        # --out for anything a machine will read.
        sys.stdout.reconfigure(encoding="utf-8")
        print(body, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
