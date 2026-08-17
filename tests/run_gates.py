# -*- coding: utf-8 -*-
"""Run every cc-memory release gate, in one command.

WHY THIS EXISTS: the ten gates were ten commands in a documented list, run by
hand. The v2.11.0 release commit shipped with `tests/smoke_test.py` RED — the
directive ledger added three CLI subcommands and `commands/cc-mem.md` was never
updated, so the doc-fact assertion failed. Nothing caught it, because "run all
ten" was a sentence in a markdown file rather than an executable.

It is deliberately NOT a gate itself: it spawns the gates as subprocesses and
reports. It imports nothing from `cc_memory`, so it cannot perturb the hermetic
sandbox each suite installs for itself before importing the package.

Usage:
    python tests/run_gates.py              # run all, print a table, exit nonzero on any red
    python tests/run_gates.py --list       # show what each gate checks
    python tests/run_gates.py --only smoke # run one gate by key
    python tests/run_gates.py --fast       # skip the two slowest suites (NOT a release run)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable

# This runner's OWN stdout must not depend on the console codepage. GitHub's
# Windows runner gives Python a cp1252 stdout, and the first thing that hit it
# was this file's own box-drawing separator: `UnicodeEncodeError: 'charmap'
# codec can't encode characters` — the runner crashed while reporting a
# failure, turning a diagnosable red gate into a traceback about printing. The
# gates it spawns emit non-ASCII too (test_directive_enforcement prints Chinese
# section headers, several suites print box drawing), and all of it flows back
# through here. Reconfigured rather than importing core.encoding_setup: this
# script is deliberately hermetic and imports nothing from cc_memory.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        # why: reconfigure() needs a real TextIOWrapper; under an already-
        # wrapped or redirected stream there is nothing to change, and a
        # runner must never fail because it could not adjust its own output.
        pass

# Every version site that must carry the same string. `core/version.py` is
# canonical; the rest are copies that have gone stale before (two of cli/mem.py's
# literals were, in v2.4.x). smoke_test.py asserts this too — it is duplicated
# here on purpose, because it costs 40 ms and smoke_test costs minutes, so a
# version typo should not have to wait for the slow gate to surface.
_VERSION_SITES = (
    ("cc_memory/core/version.py", r'__version__\s*=\s*["\']([^"\']+)["\']'),
    ("pyproject.toml", r'^version\s*=\s*["\']([^"\']+)["\']'),
    (".claude-plugin/plugin.json", r'"version"\s*:\s*"([^"]+)"'),
    (".claude-plugin/marketplace.json", r'"version"\s*:\s*"([^"]+)"'),
    ("cc_memory/config.json", r'"version"\s*:\s*"([^"]+)"'),
)


def _gate_versions() -> tuple[int, str]:
    """The version-agreement gate, implemented rather than delegated."""
    found = {}
    for rel, pattern in _VERSION_SITES:
        path = REPO / rel
        if not path.is_file():
            return 1, f"missing version site: {rel}"
        text = path.read_text(encoding="utf-8-sig")
        m = re.search(pattern, text, re.M)
        if not m:
            return 1, f"no version literal found in {rel}"
        found[rel] = m.group(1)
    distinct = sorted(set(found.values()))
    if len(distinct) != 1:
        detail = "; ".join(f"{k}={v}" for k, v in sorted(found.items()))
        return 1, f"version sites disagree ({', '.join(distinct)}): {detail}"
    return 0, f"all {len(found)} version sites agree on {distinct[0]}"


def _gate_pyproject() -> tuple[int, str]:
    """pyproject.toml must be parseable by a PEP 517 frontend. A UTF-8 BOM in
    it made `tomllib` fail for three releases, so nothing could build or
    install the package at all."""
    try:
        import tomllib
    except ModuleNotFoundError:  # py<3.11 has no tomllib
        return 0, "skipped: tomllib needs Python 3.11+ (parse unverified here)"
    try:
        data = tomllib.loads(
            (REPO / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception as exc:            # noqa: BLE001 — any parse failure is the finding
        return 1, f"pyproject.toml does not parse: {exc}"
    name = data.get("project", {}).get("name")
    return 0, f"pyproject.toml parses (project.name={name!r})"


# key, human title, argv (None => a python-callable gate), the callable
GATES = [
    ("compile",   "byte-compile the whole tree",
     [PY, "-m", "compileall", "-q", "cc_memory", "tests", "tools", "scripts"], None),
    ("pyproject", "pyproject.toml parses (PEP 517)", None, _gate_pyproject),
    ("versions",  "every version site agrees",       None, _gate_versions),
    ("smoke",     "end-to-end + the three doc gates",
     [PY, "tests/smoke_test.py"], None),
    ("carryover", "the plan carryover gate",
     [PY, "tests/test_plan_carryover.py"], None),
    ("surfaces",  "installer · MCP · viewer · opt-out · anchoring",
     [PY, "tests/test_surfaces.py"], None),
    ("directive", "directive ledger + Stop enforcement",
     [PY, "tests/test_directive_enforcement.py"], None),
    ("i18n",      "translation drift",
     [PY, "tools/i18n_check.py"], None),
    ("citations", "every file.py:LINE citation in the tracked docs",
     [PY, "tools/citation_check.py"], None),
    ("claims",    "prose counts vs the sets computed from the tree",
     [PY, "tools/doc_claims.py"], None),
    ("coverage",  "every public surface is named by the doc that owns it",
     [PY, "tools/doc_coverage.py"], None),
]

# The two that dominate wall-clock. --fast skips them; a release run must not.
_SLOW = {"surfaces", "smoke"}


# How many trailing lines of a FAILED gate's output to reproduce. One line was
# not enough: a red `test_surfaces` in CI reported only its LAST print, which
# happened to be a parenthetical detail from an unrelated passing check, so the
# actual AssertionError never reached the CI log and the failure could not be
# diagnosed without re-running by hand. A runner that hides the failure it
# detected is only half a runner.
_FAIL_TAIL_LINES = 30

# Substrings that mark the line actually naming a failure, preferred over the
# last line printed.
_FAIL_MARKERS = ("Error", "error:", "FAIL", "Traceback", "assert")


def _run_one(gate) -> tuple[str, int, float, str, str]:
    key, title, argv, fn = gate
    t0 = time.time()
    tail = ""
    if fn is not None:
        try:
            rc, detail = fn()
        except Exception as exc:        # noqa: BLE001 — a crashing gate is a red gate
            rc, detail = 1, f"gate raised: {exc!r}"
    else:
        proc = subprocess.run(argv, cwd=str(REPO), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        rc = proc.returncode
        stream = (proc.stdout or "") + (proc.stderr or "")
        lines = [ln for ln in stream.splitlines() if ln.strip()]
        detail = lines[-1][:160] if lines else "(no output)"
        if rc != 0:
            for ln in reversed(lines):
                if any(m in ln for m in _FAIL_MARKERS):
                    detail = ln.strip()[:160]
                    break
            tail = "\n".join(lines[-_FAIL_TAIL_LINES:])
    return key, rc, time.time() - t0, detail, tail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show the gates and exit")
    ap.add_argument("--only", metavar="KEY", help="run a single gate by key")
    ap.add_argument("--fast", action="store_true",
                    help="skip the two slowest suites (NOT a release run)")
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    args = ap.parse_args()

    if args.list:
        print(f"{len(GATES)} release gates:\n")
        for key, title, argv, _fn in GATES:
            how = " ".join(argv[1:]) if argv else "(in-process)"
            print(f"  {key:<10} {title}\n             $ {how}")
        return 0

    selected = GATES
    if args.only:
        selected = [g for g in GATES if g[0] == args.only]
        if not selected:
            print(f"[FAIL] no gate named {args.only!r}. "
                  f"Known: {', '.join(g[0] for g in GATES)}")
            return 2
    elif args.fast:
        selected = [g for g in GATES if g[0] not in _SLOW]

    results = []
    for gate in selected:
        print(f"[..] {gate[0]:<10} {gate[1]}", flush=True)
        results.append(_run_one(gate))

    if args.json:
        print(json.dumps([{"gate": k, "rc": rc, "seconds": round(s, 2),
                           "detail": d} for k, rc, s, d, _t in results],
                         indent=2))

    failed = [r for r in results if r[1] != 0]
    print("\n" + "=" * 78)
    print(f"{'GATE':<11}{'RESULT':<9}{'TIME':>8}   DETAIL")
    print("-" * 78)
    for key, rc, secs, detail, _tail in results:
        print(f"{key:<11}{'PASS' if rc == 0 else 'FAIL':<9}{secs:>7.1f}s   "
              f"{detail[:44]}")
    print("=" * 78)

    skipped = len(GATES) - len(selected)
    note = f"  ({skipped} skipped — NOT a release run)" if skipped else ""
    if failed:
        print(f"[FAIL] {len(failed)} of {len(selected)} gates RED: "
              f"{', '.join(r[0] for r in failed)}{note}")
        for key, _rc, _s, detail, tail in failed:
            print(f"\n──── {key} ──── {detail}")
            # the red gate's OWN trailing output, so a CI log carries the
            # failure rather than only the name of the gate that found it
            for ln in tail.splitlines():
                print(f"  | {ln}")
        return 1
    print(f"[OK] all {len(selected)} gates green{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
