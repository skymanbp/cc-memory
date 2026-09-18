# -*- coding: utf-8 -*-
"""R610 carryover gate — 换计划不许丢步骤 (2026-07-29).

The plan slot is single-row; replacing or clearing it was the one moment
staged work could silently vanish (the SELF-ITER S1-S3 sink). This suite
pins the mandatory gate:

  §1 replace with NO old plan → allowed (bootstrap unchanged)
  §2 replace that drops unfinished steps, no dispositions → REFUSED
  §3 replace that carries them (title similarity) → allowed + archived
  §4 replace with explicit dispositions (action+reason) → allowed,
     dispositions stored for audit, old plan archived
  §5 disposition without a reason → REFUSED
  §6 CLI plan-clear with unfinished steps → exit 1 without --reason,
     OK with --reason, archive written
  §8 PROGRESS.md §4 renders the plan STORE — it read a free-text column the store
     replaced, so a project with a plan loaded still printed "(no plan recorded)"

Run:  python tests/test_plan_carryover.py
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ── sandbox: must be installed BEFORE importing anything from cc_memory ─────
# This is one of the nine release gates and it was the only one running against
# the REAL home and the REAL %TEMP%: `Path.home()` stayed the maintainer's, so
# every core.logger write landed in the live ~/.claude/hooks/cc-memory/logs/,
# and `_mk_project` leaked two project directories per run into %TEMP% with no
# teardown at all (measured: 270 `ccm_gate_*` directories, 42 MB, each holding
# a memory.db). Its two siblings have carried this block for releases, and both
# treat an uncleanable leak as a test FAILURE. Deliberate literal twin of
# smoke_test.py / test_surfaces.py: standalone scripts that cannot import each
# other, and a shared helper would have to live inside the package under test.
_SANDBOX = Path(tempfile.mkdtemp(prefix="ccm-gate-box-"))
_HOME = _SANDBOX / "home"
_TMP = _SANDBOX / "tmp"
_HOME.mkdir(parents=True, exist_ok=True)
_TMP.mkdir(parents=True, exist_ok=True)
_drive, _rest = os.path.splitdrive(str(_HOME))
os.environ.update({
    "USERPROFILE": str(_HOME),      # ntpath.expanduser checks this first
    "HOME": str(_HOME),             # posixpath.expanduser
    "HOMEDRIVE": _drive or "",
    "HOMEPATH": _rest or str(_HOME),
    "TEMP": str(_TMP),
    "TMP": str(_TMP),
    "TMPDIR": str(_TMP),
})
# env alone is not enough in-process: tempfile caches gettempdir() on first use,
# and mkdtemp() above already primed it with the REAL temp dir.
tempfile.tempdir = str(_TMP)
assert Path.home() == _HOME, (
    f"sandbox home not in effect (Path.home()={Path.home()}); refusing to run "
    f"against the real ~/.claude")


def _cleanup_sandbox():
    """Close every sqlite handle this process opened, then REMOVE the sandbox."""
    for _conn in [o for o in gc.get_objects()
                  if isinstance(o, sqlite3.Connection)]:
        try:
            _conn.close()
        except sqlite3.Error:
            # why: an already-closed or mid-statement handle; the only goal is
            # releasing the OS file handle before rmtree, and one we cannot
            # release is caught by the rmtree check below anyway
            pass
    gc.collect()
    try:
        from core import logger as _logger_mod
        for _lg in list(getattr(_logger_mod, "_loggers", {}).values()):
            _lg.close()
    except ImportError:
        # why: teardown must work even if the package never became importable
        pass
    tempfile.tempdir = None
    try:
        shutil.rmtree(_SANDBOX)
    except OSError as exc:
        left = sorted(str(p) for p in _SANDBOX.rglob("*") if p.is_file())
        raise AssertionError(
            f"sandbox {_SANDBOX} survived cleanup ({exc}); {len(left)} file(s) "
            f"leaked into the real %TEMP%: {left[:10]}")


sys.path.insert(0, str(REPO / "cc_memory"))

from core.db import MemoryDB          # noqa: E402  -- why: imports must follow the sys.path bootstrap above; repo tests run as plain scripts
from core.layout import MEMORY_DIRNAME as _MEM  # noqa: E402  -- why: same bootstrap ordering
from core import plan as plan_mod     # noqa: E402  -- why: same bootstrap ordering
from core import progress as prog_mod  # noqa: E402  -- why: same bootstrap ordering (§8)
from core.encoding_setup import enable_utf8_io  # noqa: E402  -- why: same bootstrap ordering

# Every section header below contains non-ASCII (§, →, 换计划不许丢步骤). Under
# the host's default console codec — gbk on this machine — Python encodes them
# to locale bytes, which any UTF-8 reader (terminal, log file, CI capture)
# renders as mojibake: "§1 ... → ..." arrived as "??1 ... ?? ...". This is the
# project's own fix for exactly that, and it must run BEFORE the first print().
# tests/smoke_test.py asserts that ordering so the garbling cannot come back.
enable_utf8_io()

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def _mk_project():
    root = Path(tempfile.mkdtemp(prefix="ccm_gate_"))
    memory_dir = root / _MEM
    memory_dir.mkdir()
    db = MemoryDB(memory_dir / "memory.db")
    pid = db.upsert_project(str(root))
    return root, memory_dir, db, pid


def _plan(goal, titles, status="pending", dispositions=None):
    p = {
        "version": 1,
        "goal": goal,
        "success_criteria": ["testable thing"],
        "steps": [{"id": i + 1, "title": t, "status": status, "notes": ""}
                  for i, t in enumerate(titles)],
        "context": "",
        "refined_by": "plan-refiner",
    }
    if dispositions is not None:
        p["dispositions"] = dispositions
    return p


def main() -> None:
    print("§1 bootstrap: no old plan → replacement allowed")
    root, memory_dir, db, pid = _mk_project()
    plan_mod.apply_refined_plan(
        db, pid, _plan("old goal", ["wire the token refresh flow",
                                    "ship the export panel"]),
        memory_dir=memory_dir)
    row = db.get_plan_active(pid)
    check("first plan stored", len(row["structured"]["steps"]) == 2)

    print("§2 dropping unfinished steps with no dispositions → REFUSED")
    refused = None
    try:
        plan_mod.apply_refined_plan(
            db, pid, _plan("new goal", ["a completely different thing"]),
            memory_dir=memory_dir)
    except ValueError as e:
        refused = str(e)
    check("replacement refused", refused is not None
          and "carryover gate REFUSED" in refused, str(refused))
    check("refusal names BOTH lost steps",
          refused is not None and "token refresh" in refused
          and "export panel" in refused, str(refused))
    check("old plan untouched after refusal",
          db.get_plan_active(pid)["structured"]["goal"] == "old goal")

    print("§3 carrying the steps by similar title → allowed + archived")
    plan_mod.apply_refined_plan(
        db, pid, _plan("new goal", [
            "wire the token refresh flow end to end",
            "ship the export panel with tests",
            "a completely different thing"]),
        memory_dir=memory_dir)
    check("auto-carry replacement stored",
          db.get_plan_active(pid)["structured"]["goal"] == "new goal")
    hist = list((memory_dir / ".plan_history").glob("plan_*_replace.json"))
    check("outgoing plan archived", len(hist) >= 1, str(hist))

    print("§4 explicit dispositions (action+reason) → allowed + audited")
    dispo = [
        {"old_title": "wire the token refresh flow end to end",
         "action": "done", "reason": "shipped in commit abc123"},
        {"old_title": "ship the export panel with tests",
         "action": "dropped", "reason": "user ruling 2026-07-29: not needed"},
        {"old_title": "a completely different thing",
         "action": "merged", "reason": "absorbed into the omnibus step"},
    ]
    plan_mod.apply_refined_plan(
        db, pid, _plan("third goal", ["one omnibus step"],
                       dispositions=dispo),
        memory_dir=memory_dir)
    stored = db.get_plan_active(pid)["structured"]
    check("dispositioned replacement stored", stored["goal"] == "third goal")
    check("dispositions retained for audit",
          len(stored.get("dispositions", [])) == 3, str(stored.keys()))

    print("§4b long notes must not dilute an identical-title auto-carry "
          "(v2.4.1 regression)")
    # v2.4.0 matched old titles against title+notes ONLY; a long notes
    # field diluted the trigram overlap below threshold, so replacing a
    # plan with ITSELF (status/notes updates) was refused. Found on the
    # gate's second real replacement (R610).
    long_notes = ("this is a very long progress note " * 8
                  + "with ids 298/301/302/386/387 and a ledger pointer")
    same_title_plan = _plan("third goal", ["one omnibus step"])
    same_title_plan["steps"][0]["notes"] = long_notes
    plan_mod.apply_refined_plan(
        db, pid, same_title_plan, memory_dir=memory_dir)
    check("identical title with long notes auto-carries",
          db.get_plan_active(pid)["structured"]["steps"][0]["notes"]
          == long_notes)

    print("§5 disposition without a reason → REFUSED")
    refused2 = None
    try:
        plan_mod.apply_refined_plan(
            db, pid, _plan("fourth goal", ["unrelated"],
                           dispositions=[{"old_title": "one omnibus step",
                                          "action": "dropped",
                                          "reason": ""}]),
            memory_dir=memory_dir)
    except ValueError as e:
        refused2 = str(e)
    check("reasonless disposition refused",
          refused2 is not None and "no reason" in refused2, str(refused2))

    print("§6 CLI plan-clear gate")
    mem_py = REPO / "cc_memory" / "cli" / "mem.py"
    # encoding pinned: the child prints UTF-8 (gate message contains an
    # em-dash); text=True would decode with the locale codec (GBK on this
    # host), crash the reader thread, and hand back stdout=None.
    r = subprocess.run(
        [sys.executable, str(mem_py), "--project", str(root), "plan-clear"],
        capture_output=True, encoding="utf-8", errors="replace")
    check("clear without --reason exits 1 and names the gate",
          r.returncode == 1 and "carryover gate" in r.stdout,
          f"rc={r.returncode} out={r.stdout[:200]}")
    check("plan survives the refused clear",
          db.get_plan_active(pid) is not None
          and (db.get_plan_active(pid).get("structured") or {}).get("goal")
          == "third goal")
    r2 = subprocess.run(
        [sys.executable, str(mem_py), "--project", str(root), "plan-clear",
         "--reason", "test teardown ruling"],
        capture_output=True, encoding="utf-8", errors="replace")
    check("clear WITH --reason succeeds",
          r2.returncode in (0, None) and "[OK]" in r2.stdout,
          f"rc={r2.returncode} out={r2.stdout[:200]}")
    clear_hist = list((memory_dir / ".plan_history").glob("plan_*_clear.json"))
    check("cleared plan archived with reason",
          len(clear_hist) >= 1 and "test teardown ruling"
          in clear_hist[-1].read_text(encoding="utf-8"), str(clear_hist))

    print("§7 success_criteria carryover advisory (v2.5.6)")
    # The steps gate is deliberately silent about criteria, and that silence
    # cost a real plan two criteria on 2026-08-05: the replacement passed
    # cleanly and nobody was told. §7 pins the advisory that closes it.
    root7, mem7, db7, pid7 = _mk_project()
    old7 = _plan("goal seven", ["build the ingest lane"], status="done")
    old7["success_criteria"] = [
        "the ingest lane replays a captured batch byte for byte",
        "no XXXXXX placeholder survives into a shipped string",
        "the release bundle is reproducible across two clean checkouts",
    ]
    plan_mod.apply_refined_plan(db7, pid7, old7, memory_dir=mem7)

    new7 = _plan("goal seven prime", ["build the ingest lane"], status="done")
    new7["success_criteria"] = [
        "the ingest lane replays a captured batch byte for byte",
        "the release bundle is reproducible across two clean checkouts",
    ]
    lost = plan_mod.unmatched_criteria(
        db7.get_plan_active(pid7)["structured"], new7)
    check("advisory names exactly the dropped criterion",
          len(lost) == 1 and "XXXXXX" in lost[0], str(lost))

    kept = plan_mod.unmatched_criteria(
        db7.get_plan_active(pid7)["structured"], old7)
    check("identical criteria lists report nothing lost", kept == [], str(kept))

    # A criterion folded into the replacement's context text still survives —
    # lossy, but not a silent disappearance, so it must not be flagged.
    folded = dict(new7)
    folded["context"] = ("no XXXXXX placeholder survives into a shipped "
                         "string — folded into context on purpose")
    check("criterion folded into context counts as carried",
          plan_mod.unmatched_criteria(
              db7.get_plan_active(pid7)["structured"], folded) == [],
          "context fold should suppress the advisory")

    # and the CLI must actually PRINT it — a core function nobody surfaces is
    # the same silence with extra steps.
    mem_py7 = REPO / "cc_memory" / "cli" / "mem.py"
    r7 = subprocess.run(
        [sys.executable, str(mem_py7), "--project", str(root7),
         "plan-set", "--from-refiner"],
        input=json.dumps(new7, ensure_ascii=False).encode("utf-8"),
        capture_output=True)
    out7 = r7.stdout.decode("utf-8", "replace")
    check("CLI stores the replacement", r7.returncode == 0 and "[OK]" in out7,
          f"rc={r7.returncode} out={out7[:200]}")
    check("CLI prints the carryover advisory",
          "carryover advisory" in out7 and "XXXXXX" in out7, out7[:400])
    check("advisory says context is not compared at all",
          "`context` is free text" in out7, out7[:400])

    print("§8 PROGRESS.md §4 reads the plan STORE, not the dead free-text column")
    # §4 rendered `progress.plan`, a column the structured store replaced and left with no
    # writer. On a live project holding a 31-step plan with an active step, §4 still printed
    # "(no plan recorded)" on every regeneration — measured at 0 characters in the same
    # second `plan-status` reported "6/31 steps done · active step #5". Two readers of one
    # concept, one pointed at a dead field, and the generated artifact says "nothing here"
    # where the truth is "here, and this far along". It lived because no gate had ever
    # rendered §4 for a project that HAS a plan.
    root8, mem8, db8, pid8 = _mk_project()
    titles8 = ["wire the token refresh flow", "ship the export panel",
               "retire the legacy importer", "document the gate"]
    plan8 = _plan("goal eight", titles8)
    plan8["steps"][0]["status"] = "done"
    plan_mod.apply_refined_plan(db8, pid8, plan8, memory_dir=mem8)
    db8.upsert_plan_active(pid8, active_step=2)

    def _section4(db, pid, memory_dir):
        text = prog_mod.write_progress_md(db, pid, memory_dir).read_text(encoding="utf-8")
        assert "## 4. Plan" in text, "PROGRESS.md has no §4 at all"
        return text.split("## 4. Plan")[1].split("## 5.")[0]

    sec8 = _section4(db8, pid8, mem8)
    check("§4 no longer claims there is no plan",
          "(no plan recorded)" not in sec8, sec8[:200])
    check("§4 carries the goal and the progress line",
          "goal eight" in sec8 and "1/4 steps done" in sec8, sec8[:200])
    check("§4 marks the active step",
          "active step #2" in sec8 and "← ACTIVE" in sec8, sec8[:300])
    check("the finished step is not listed as work still to do",
          titles8[0] not in sec8 and all(t in sec8 for t in titles8[1:]), sec8[:300])

    root8b, mem8b, db8b, pid8b = _mk_project()
    check("reverse control: a project with NO plan still says so",
          "(no plan recorded)" in _section4(db8b, pid8b, mem8b))
    db8b.upsert_progress(pid8b, plan="1. the old free-text plan line")
    check("a legacy free-text plan is still shown, not dropped",
          "the old free-text plan line" in _section4(db8b, pid8b, mem8b))

    many8 = [f"step number {i}" for i in range(1, 21)]
    root8c, mem8c, db8c, pid8c = _mk_project()
    plan_mod.apply_refined_plan(db8c, pid8c, _plan("a long plan", many8), memory_dir=mem8c)
    sec8c = _section4(db8c, pid8c, mem8c)
    check("the render cap announces itself instead of truncating in silence",
          sec8c.count("\n- [") <= prog_mod._MAX_PLAN_STEPS_RENDERED
          and "more (render capped at" in sec8c, sec8c[-200:])

    class _BrokenStore:
        def get_plan_active(self, _pid):
            raise sqlite3.OperationalError("no such table: plan_active")

    check("an unreadable plan store degrades to a note instead of taking §4 down",
          any("plan unavailable" in line
              for line in prog_mod._render_plan_section(_BrokenStore(), pid8, {})))

    print(f"\n{'=' * 60}\nRESULT: {PASS} passed, {FAIL} failed\n{'=' * 60}")


if __name__ == "__main__":
    try:
        main()
    finally:
        # Teardown runs on the failure path too — a suite that leaks only when
        # it fails leaks exactly when someone is least likely to notice. The
        # sys.exit that used to end main() skipped every finally in this file.
        _cleanup_sandbox()
    sys.exit(1 if FAIL else 0)
