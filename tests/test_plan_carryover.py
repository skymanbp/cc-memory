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

Run:  python tests/test_plan_carryover.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "cc_memory"))

from core.db import MemoryDB          # noqa: E402  -- why: imports must follow the sys.path bootstrap above; repo tests run as plain scripts
from core import plan as plan_mod     # noqa: E402  -- why: same bootstrap ordering
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
    memory_dir = root / "memory"
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

    print(f"\n{'=' * 60}\nRESULT: {PASS} passed, {FAIL} failed\n{'=' * 60}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
