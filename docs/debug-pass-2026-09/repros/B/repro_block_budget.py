"""stop.py _block_attempt: the escape budget counts refusals that were RESOLVED in between,
so the 4th legitimate plan-drift refusal of a session is silently downgraded to an advisory."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core.db import MemoryDB
    from core import plan as plan_mod
    proj = sb.proj; (proj / ".ccm").mkdir()
    db = MemoryDB(proj / ".ccm" / "memory.db"); pid = db.upsert_project(str(proj))
    plan_mod.apply_refined_plan(db, pid, {"goal": "ship it", "steps": [{"title": "step one", "status": "pending"}]},
                                memory_dir=proj / ".ccm")
    pl = {"session_id": "long-session", "cwd": str(proj)}
    results = []
    for cycle in range(5):
        # drift threshold: 8 turns since the last guardian check -> Stop must refuse
        db.upsert_plan_active(pid, turns_since_last_guardian=7)   # bump in the hook makes it 8
        r = sb.run_hook("stop", pl)
        blocked = r["out"].strip().startswith("{")
        # the model complies: guardian check + /cc-mem plan-check resets the counters
        db.reset_plan_guardian_counters(pid)
        r2 = sb.run_hook("stop", pl)         # the compliant Stop passes (no reasons)
        passed = not r2["out"].strip().startswith("{")
        results.append((cycle + 1, "BLOCK" if blocked else "ADVISORY-ONLY", "resolved" if passed else "?"))
        if not blocked:
            print("   advisory text:", r["out"].strip()[:200])
    for c in results: print(c)
finally:
    sb.cleanup()
