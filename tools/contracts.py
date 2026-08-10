#!/usr/bin/env python3
"""
Executable invariant registry — the sets this plugin's docs keep asserting.

WHY THIS EXISTS. Five consecutive convergence rounds each found defects, and
in round 5 eleven of seventeen findings were the same shape: prose that had
been true when written. The docs enumerate invariants by hand — "the marker
defence runs on every render path (PROGRESS.md, the injection, PLAN.md,
MEMORY.md)", "six hooks", "nine surfaces" — and `tools/citation_check.py`
verifies that `file.py:123` still points at its symbol, not that the SENTENCE
is still true. So every fix that added a seventh caller silently falsified a
sentence, and the next round reported it as a fresh defect.

A hand-maintained list of things is a thing that drifts. Each contract below
COMPUTES its answer from the tree, so the answer cannot lag the code, and
`tools/doc_claims.py` checks the prose against it.

Every contract returns a sorted tuple of repo-relative POSIX paths. Counts are
`len()` of that, so "how many" and "which ones" can never disagree — the two
were separately hand-maintained before, and did.

    python tools/contracts.py            # print every contract's current value
    python tools/contracts.py --json     # same, machine-readable
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = "cc_memory"

# name -> (compute(repo) -> tuple[str, ...], one-line description)
CONTRACTS = {}


def contract(name, description):
    def register(fn):
        CONTRACTS[name] = (fn, description)
        return fn
    return register


def _py_files(repo):
    """Every shipped module, skipping caches. Sorted for a stable answer."""
    root = repo / PKG
    return sorted(p for p in root.rglob("*.py")
                  if "__pycache__" not in p.parts)


def _rel(repo, path):
    return path.relative_to(repo).as_posix()


def _called_names(tree):
    """Every name a module invokes or takes a reference to.

    AST, not a regex over the text: a regex matches the function's own
    definition, its name inside a docstring, and the word in a comment, so a
    module that only DOCUMENTS a guard would count as one that applies it —
    which is precisely the false green this registry exists to prevent.

    Name LOADS count alongside calls, because `f = neutralize_markers`,
    `partial(neutralize_markers)` and `@neutralize_markers` all use the guard
    without a direct Call node — `core/progress.py` aliases `neutralize_block`
    this way today. Attribute access still counts only in call position:
    counting every attribute LOAD would make an unrelated `obj.is_excluded`
    field read register as an opt-out surface.

    An `import ... as` ALIAS counts as a use of the ORIGINAL name. Without
    this, `from core.privacy import neutralize_inline as _ni` followed by
    `_ni(...)` is invisible from both ends — the import binds `_ni`, the call
    loads `_ni`, and the guard's real name appears nowhere in the AST. That
    is not hypothetical: `ui/dashboard.py:1095` does exactly this, and it is
    half of why the render-path registry under-counted the marker defence.

    Two shapes stay invisible by choice — `getattr(mod, "guard_name")`
    (scanning string constants would make every docstring mention a false
    positive) and a foreign object's same-name method call; neither exists in
    the tree (checked by exact AST probe, not assumption).
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.Call) and \
                isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.asname:
                    names.add(alias.name.split(".")[-1])
    return names


def _modules_calling(repo, wanted, exclude=()):
    """Modules that CALL any name in `wanted`. Unparseable files are an error.

    A file this project cannot parse is a file whose contracts are unknown;
    swallowing that would let a syntax error read as "no callers".

    `exclude` names the module that IMPLEMENTS the guard. It calls its own
    helpers, so it matches, but it is the defence rather than a surface
    protected by it — counting it would inflate every "N surfaces" claim by
    one and make the number disagree with the list a reader can check.
    """
    hits = []
    for path in _py_files(repo):
        rel = _rel(repo, path)
        if rel in exclude:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
        if _called_names(tree) & wanted:
            hits.append(rel)
    return tuple(sorted(hits))


@contract("hooks", "hook scripts Claude Code is told to run (hooks/hooks.json)")
def _hooks(repo):
    spec = json.loads((repo / "hooks" / "hooks.json").read_text(
        encoding="utf-8"))
    # Only `command` VALUES, and only scripts under this package's hooks/
    # directory. Scanning the raw file for any `.py` token counted a script
    # named in a `description` field — or any unrelated path a future entry
    # mentions — as a registered hook, inflating every "N hooks" claim the
    # doc gate checks against this number.
    found = set()
    for hook in _command_strings(spec):
        for match in re.finditer(
                rf"{PKG}/hooks/([A-Za-z_][A-Za-z0-9_]*\.py)",
                hook.replace("\\", "/")):
            found.add(f"{PKG}/hooks/{match.group(1)}")
    return tuple(sorted(found))


def _command_strings(spec):
    """Every `command` value in the hook spec, at any nesting depth."""
    stack, out = [spec], []
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if isinstance(item.get("command"), str):
                out.append(item["command"])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


@contract("render_paths",
          "modules that escape authority markers before emitting content")
def _render_paths(repo):
    # `neutralize_document` belongs here as much as the per-slot helpers do:
    # it is the ASSEMBLED-document sweep, and for `ui/dashboard.py` it is the
    # escaping call made by its own name. Omitting it made this registry —
    # the one CLAUDE.md points readers at as authoritative — report 6 render
    # paths while the tree had 7. The same N+1 shape `_BACKSTOP_CREATORS`
    # below records recurring "inside its own cure".
    return _modules_calling(repo, {
        "neutralize_markers", "neutralize_inline", "neutralize_block",
        "neutralize_document", "_neutralize", "_defang"},
        exclude=(f"{PKG}/core/privacy.py",))


@contract("optout_surfaces",
          "modules that consult config.json excluded_projects")
def _optout_surfaces(repo):
    return _modules_calling(repo, {"is_excluded", "cli_opt_out_notice",
                                   "refusal_notice"},
                            exclude=(f"{PKG}/core/modes.py",))


# Creators that deliberately do NOT call `ensure_memory_dir`, and the exact
# mkdir call each uses instead. `core/db.py` is the documented BACKSTOP (its
# `__init__` mkdirs memory/ without parents, so anything opening a database
# off the main path still creates the directory); `ui/installer.py` is a
# stdlib-only bootstrap that cannot import the package. Counting only
# `ensure_memory_dir` callers certified SIX creators while the tree had
# EIGHT — the N+1 prose disease recurring inside its own cure. Each entry is
# VERIFIED against the module's source: if its mkdir call disappears (the
# module stops being a creator, or renames the receiver), the contract ERRORS
# instead of silently keeping a stale member — same fail-loud rule as an
# unparseable file in `_modules_calling`.
_BACKSTOP_CREATORS = {
    f"{PKG}/core/db.py": "self.db_path.parent.mkdir(",
    f"{PKG}/ui/installer.py": "memory_dir.mkdir(",
}


@contract("memory_dir_creators",
          "modules that bring a project's memory/ directory into existence")
def _memory_dir_creators(repo):
    members = set(_modules_calling(repo, {"ensure_memory_dir"}))
    for rel, call_text in _BACKSTOP_CREATORS.items():
        src = (repo / rel).read_text(encoding="utf-8", errors="replace")
        if call_text not in src:
            raise RuntimeError(
                f"{rel} is registered as a backstop memory/ creator but "
                f"`{call_text}` no longer appears in it — update "
                f"_BACKSTOP_CREATORS to match the tree")
        members.add(rel)
    return tuple(sorted(members))


@contract("anchoring_surfaces",
          "modules that resolve a raw cwd to the project root")
def _anchoring_surfaces(repo):
    return _modules_calling(repo, {"project_root", "anchor_project"},
                            exclude=(f"{PKG}/core/roots.py",))


@contract("insert_memory_callers",
          "modules that bypass the anti-patch writer and insert a memory row")
def _insert_memory_callers(repo):
    """The anti-patch contract, COMPUTED. It must be empty.

    "Never call `db.insert_memory` directly from a caller path" is the oldest
    rule in this project and it was enforced by prose alone — CLAUDE.md hand-
    lists ten save paths, which is the enumeration-in-prose disease this file
    exists to cure, with this set never migrated into it. The consequence is
    not just duplicate rows: `clean_for_storage` is the write-path half of the
    privacy defence and it is reached from ONE place, `memory_writer`. Nothing
    anywhere re-strips `<private>` on a render path — that family is stripped
    on write only — so a single new direct caller stores user-marked-private
    text verbatim, and SessionStart re-injects it every session forever.

    `core/db.py` is excluded because it DEFINES the method (and `_modules_
    calling` cannot tell a definition's own recursive helpers from a caller);
    `llm/memory_writer.py` is excluded because it IS the writer. Anything else
    appearing here is the regression.
    """
    return _modules_calling(repo, {"insert_memory"},
                            exclude=(f"{PKG}/core/db.py",
                                     f"{PKG}/llm/memory_writer.py"))


def values(repo=REPO):
    """Every contract's current value. The single entry point for consumers."""
    return {name: fn(repo) for name, (fn, _) in CONTRACTS.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output")
    ap.add_argument("--root", default=str(REPO), help="repo root")
    args = ap.parse_args()
    current = values(Path(args.root))
    if args.json:
        print(json.dumps({k: list(v) for k, v in current.items()}, indent=2))
        return 0
    for name, (_, description) in CONTRACTS.items():
        members = current[name]
        print(f"{name}  ({len(members)})  — {description}")
        for member in members:
            print(f"    {member}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
