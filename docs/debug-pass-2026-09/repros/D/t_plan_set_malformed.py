"""D: plan-set --from-refiner with malformed / hostile JSON; carryover gate."""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    proj = sb / "projA"
    db, pid, ids = _h.seed_project(proj, [{"content": "Seed decision: the scheduler runs every five minutes"}])

    def ps(payload, label=None):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        rc, out, err = _h.mem(["plan-set", "--from-refiner"], proj, cwd=proj, stdin=stdin)
        flag = "TRACEBACK" if _h.is_traceback(err) else ""
        first = (out.strip().splitlines() or [""])[0][:110]
        print(f"{flag:9} rc={rc} {(label or stdin)[:50]:<50} | {first}"
              + (f"\n           {err.strip().splitlines()[-1][:160]}" if flag else ""))
        return rc, out, err

    good = {"goal": "Ship the billing migration", "success_criteria": ["All invoices reconcile", "No 5xx in canary"],
            "steps": [{"id": 1, "title": "Write the migration script", "status": "pending"},
                      {"id": 2, "title": "Run canary for one day", "status": "pending"}],
            "context": "c", "refined_by": "plan-refiner"}
    for p in ("null", "42", '"s"', "[]", "{}", "", "   ", "{bad", "﻿" + json.dumps(good),
              {"goal": 1}, {"goal": "g"}, {"goal": "g", "steps": {}}, {"goal": "g", "steps": ["a"]},
              {"goal": "g", "steps": [{"title": None}]}, {"goal": "g", "steps": [{"title": "a", "status": "weird"}]},
              {"goal": "g", "steps": [{"title": "a", "id": 1e999}]}, {"goal": "g", "steps": [{"title": "a", "id": [1]}]},
              {"goal": "g", "steps": [{"title": "dup"}, {"title": "dup"}]},
              {"goal": "g", "steps": [{"title": "a"}], "success_criteria": "not a list"},
              {"goal": "g", "steps": [{"title": "a"}], "dispositions": "x"},
              {"goal": "g", "steps": [{"title": "a"}], "dispositions": [{"old_title": "unknown", "action": "done", "reason": "r"}]},
              ):
        ps(p, label=p if isinstance(p, str) else json.dumps(p)[:50])
        _h.mem(["plan-clear", "--reason", "test reset"], proj)

    # deeply nested
    ps("[" * 100000 + "]" * 100000, label="100k-deep nesting")
    _h.mem(["plan-clear", "--reason", "test reset"], proj)

    # huge steps list
    big = {"goal": "g", "steps": [{"title": f"Step number {i} does thing {i}"} for i in range(20000)]}
    t0 = time.time(); rc, out, err = ps(big, label="20000 steps"); dt = time.time() - t0
    print(f"           took {dt:.1f}s; PLAN.md size:", (proj / ".ccm" / "PLAN.md").stat().st_size if (proj / ".ccm" / "PLAN.md").exists() else "absent")
    rc, out, err = _h.mem(["plan-status"], proj); print("           plan-status:", out.strip().splitlines()[1][:80] if out.strip() else "")
    t0 = time.time(); rc, out, err = _h.mem(["plan-clear", "--reason", "test reset"], proj); print(f"           plan-clear of 20000 steps: rc={rc} {time.time()-t0:.1f}s")

    # carryover gate: store good, then replace dropping a step
    ps(good, label="store GOOD plan")
    repl = {"goal": "Different goal now", "steps": [{"title": "Completely unrelated work"}], "success_criteria": ["Something else"]}
    ps(repl, label="replace w/o dispositions (gate must refuse)")
    repl2 = dict(repl, dispositions=[{"old_title": "Write the migration script", "action": "done", "reason": "shipped in 1a2b"},
                                     {"old_title": "Run canary for one day", "action": "dropped", "reason": ""}])
    ps(repl2, label="dispositions, one with EMPTY reason")
    repl3 = dict(repl, dispositions=[{"old_title": "Write the migration script", "action": "done", "reason": "shipped"},
                                     {"old_title": "Run canary for one day", "action": "dropped", "reason": "no canary env"}])
    # success_criteria wrong type on a replacement of a plan that HAS criteria
    repl4 = dict(repl3, success_criteria=7)
    ps(repl4, label="valid dispositions + success_criteria: 7 (int)")
    rc, out, err = _h.mem(["plan-status"], proj); print("           plan-status after:", (out.strip().splitlines() or [''])[0][:80])
    ps(good, label="store GOOD again (needs dispositions? new plan replaced)")
    _h.mem(["plan-clear", "--reason", "reset"], proj)
    ps(good, label="store GOOD plan")
    repl5 = dict(repl3, success_criteria=True)
    ps(repl5, label="valid dispositions + success_criteria: true")
    _h.mem(["plan-clear", "--reason", "reset"], proj)
    ps(good, label="store GOOD plan")
    repl6 = dict(repl3, success_criteria={"a": 1})
    ps(repl6, label="valid dispositions + success_criteria: {dict}")
    _h.mem(["plan-clear", "--reason", "reset"], proj)
    ps(good, label="store GOOD plan")
    repl7 = dict(repl3, goal=["list goal"], context=5)
    ps(repl7, label="goal as list")
    # plan-clear gate
    ps(good, label="store GOOD plan")
    rc, out, err = _h.mem(["plan-clear"], proj); print(f"plan-clear w/o reason rc={rc} | {out.strip().splitlines()[0][:80]}")
    rc, out, err = _h.mem(["plan-clear", "--reason", "  "], proj); print(f"plan-clear --reason '  ' rc={rc} | {out.strip().splitlines()[0][:80]}")
finally:
    _h.destroy_sandbox()
