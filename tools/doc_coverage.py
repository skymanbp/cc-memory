# -*- coding: utf-8 -*-
"""Documentation COVERAGE — is every public surface written down at all?

WHY THIS EXISTS. v2.11.2 migrated the schema and attached a load-bearing rule
to it (`turns_total`, `turns_at_touch`), and all ten gates passed. They check
that a `file.py:LINE` citation still points at its symbol
(`tools/citation_check.py`), that a sentence which COUNTS something matches the
tree (`tools/doc_claims.py`), and that a translation is bound to a hash of its
source (`tools/i18n_check.py`). Every one of them checks the docs that EXIST.
None of them asks whether a new surface produced any documentation at all —
so the columns appeared 0 times in `docs/CONTRACTS.md`,
`docs/ARCHITECTURE.md`, `commands/cc-mem.md` and both Chinese siblings, and
nothing went red. A contract that lives only in a changelog entry is a contract
the next change will break, because the person about to break it is reading the
specification.

WHAT IT CHECKS. Four surfaces, each ENUMERATED FROM THE CODE and required to
appear in the document that owns it — and in that document's translated
sibling, because a Chinese reader following the same spec must not be reading a
shorter one:

    schema tables      core/db.py CREATE [VIRTUAL] TABLE -> docs/ARCHITECTURE.md (+zh)
    schema columns     core/db.py ALTER ... ADD          -> docs/ARCHITECTURE.md (+zh)
    MCP tools          mcp/server.py TOOLS registry      -> README.md (+zh)
    config keys        cc_memory/config.json             -> README.md (+zh)

"Appear" means NAMED, not merely contained (v2.14.0). Membership used to be a
bare substring test, and a bare substring is a necessary condition mistaken
for a sufficient one: a column called `source` was satisfied by the
`<!-- i18n-source: … -->` marker on line 1 of the Chinese sibling, and the
MCP enumerator matched a name PREFIX (`memory_` / `progress_`), so a tool
outside it was never enumerated at all — both measured, both green. A member
now counts as documented only where a document spells it the way this
project's prose spells identifiers: as a code span (`name`, or a dotted
`path.name`), or as a quoted `"name":` key inside a fenced JSON example. The
table enumerator also sees `CREATE VIRTUAL TABLE` (`memories_fts`), and the
tool enumerator reads every `"name"` inside the `TOOLS = [...]` registry.

WHAT IT DELIBERATELY DOES NOT CHECK, measured rather than assumed:

  * Migration KEYS against `CHANGELOG.md`. Measured: 27 of 29 keys are absent
    from it. Requiring them would be a 27-item red gate satisfiable only by
    rewriting history entries — and this project holds that a history edited to
    stay current is not a history. A gate whose only remedy is falsifying the
    record is worse than no gate.
  * Whether the prose is CORRECT or complete. This gate answers "does the
    specification mention this surface at all", nothing more. `doc_claims`
    covers counted assertions and `citation_check` covers pointers; the
    remaining question — is the described BEHAVIOUR right — is not mechanical,
    and pretending otherwise would make a green run mean more than it does.
  * CLI subcommands, already gated inside `tests/smoke_test.py` against
    `commands/cc-mem.md`. Duplicating it here would create two lists to keep in
    step, which is the defect this file exists to catch.

Usage:
    python tools/doc_coverage.py           # report, exit nonzero on any gap
    python tools/doc_coverage.py --list    # every surface member and its owner
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        # why: reconfigure() needs a real TextIOWrapper; under a redirected or
        # already-wrapped stream there is nothing to change, and a checker must
        # never fail because it could not adjust its own output.
        pass


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8-sig")


def _schema_tables() -> list[str]:
    """Every table the schema creates — `CREATE TABLE` and `CREATE VIRTUAL
    TABLE` alike (the FTS5 index `memories_fts` is schema a reader must know
    about) — minus any name the same file also DROPs: `_fts5_probe` exists
    for one statement, to learn whether this SQLite build has FTS5 at all."""
    src = _read("cc_memory/core/db.py")
    created = set(re.findall(r"CREATE (?:VIRTUAL )?TABLE IF NOT EXISTS (\w+)", src))
    dropped = set(re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", src))
    return sorted(created - dropped)


def _schema_columns() -> list[str]:
    """Columns added by an ALTER migration.

    Columns declared inside the original CREATE TABLE are covered by the table
    itself being documented; an ALTER is the shape that arrives LATER, which is
    exactly when documentation is forgotten.
    """
    return sorted({c for _t, c in re.findall(
        r"ALTER TABLE (\w+) ADD COLUMN (\w+)", _read("cc_memory/core/db.py"))})


def _mcp_tools() -> list[str]:
    """Every tool the server advertises: the `TOOLS = [...]` registry, whole.

    A name-prefix guess (`memory_` / `progress_`) is how a tool outside the
    prefix was enumerated 0 times and the gate stayed green (measured with a
    `directive_list` tool: 0 mentions in either README, "0 gap(s)"). The
    registry is the one list `tools/list` answers from, so it is the one list
    to enumerate; a registry that cannot be found is an error, not an empty
    surface.
    """
    src = _read("cc_memory/mcp/server.py")
    start = src.index("\nTOOLS = [")
    end = src.index("\n]", start)
    return sorted(set(re.findall(r'"name":\s*"(\w+)"', src[start:end])))


# How a member must be SPELLED to count as named: a code span holding exactly
# the name (or a dotted path ending in it), or a quoted JSON key. Prose that
# merely contains the letters — `i18n-source` for a column called `source`,
# an English word that happens to be a key — is not documentation of it.
def _names(needle: str, text: str) -> bool:
    return re.search(r"`(?:[\w.-]+\.)?" + re.escape(needle) + r"`"
                     r'|"' + re.escape(needle) + r'"\s*:', text) is not None


def _config_keys() -> list[str]:
    """Leaf keys a user can set. `notes` is in-file documentation, not a knob."""
    data = json.loads(_read("cc_memory/config.json"))
    out: list[str] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if not path and key == "notes":
                    continue
                walk(value, f"{path}.{key}" if path else key)
        else:
            out.append(path)

    walk(data)
    return sorted(out)


# surface name -> (enumerator, owning documents, how a member is spelled in prose)
SURFACES = {
    "schema tables": (_schema_tables,
                      ("docs/ARCHITECTURE.md", "docs/ARCHITECTURE.zh.md")),
    "schema columns (ALTER)": (_schema_columns,
                               ("docs/ARCHITECTURE.md", "docs/ARCHITECTURE.zh.md")),
    "MCP tools": (_mcp_tools, ("README.md", "README.zh.md")),
    "config keys": (_config_keys, ("README.md", "README.zh.md")),
}


def check(repo: Path = REPO):
    """Return (problems, n_members, n_checks). A problem is (surface, member, doc)."""
    problems, members, checks = [], 0, 0
    for surface, (enumerate_members, docs) in SURFACES.items():
        found = enumerate_members()
        members += len(found)
        for doc in docs:
            text = _read(doc)
            for member in found:
                checks += 1
                # the LEAF of a dotted config key: prose writes `ccl.enabled`
                # as often as it writes the bare `enabled` inside a table row
                needle = member.rsplit(".", 1)[-1]
                if not _names(needle, text):
                    problems.append((surface, member, doc))
    return problems, members, checks


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="print every surface member and the docs that must name it")
    args = ap.parse_args()

    if args.list:
        for surface, (enumerate_members, docs) in SURFACES.items():
            found = enumerate_members()
            print(f"\n{surface}  ({len(found)}) -> {', '.join(docs)}")
            for member in found:
                print(f"    {member}")
        return 0

    problems, members, checks = check()
    for surface, member, doc in problems:
        print(f"[GAP] {doc} never mentions {surface[:-1] if surface.endswith('s') else surface}"
              f" `{member}` — a surface the code exposes and the specification does not")
    print(f"\nSummary: {members} surface member(s) across {len(SURFACES)} surfaces, "
          f"{checks} document check(s), {len(problems)} gap(s)")
    if problems:
        print("Result: FAIL (the code exposes something no document mentions)")
        return 1
    print("Result: OK (every enumerated surface is named by the doc that owns it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
