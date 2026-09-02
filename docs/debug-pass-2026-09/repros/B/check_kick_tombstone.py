"""Backpressure kick: fail-closed on an unwritable state dir; detached spawn when due; tombstone not
enforced; turns_total monotonic across resets."""
import json, os, stat, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core.db import MemoryDB
    from core import plan as plan_mod
    from llm.memory_writer import upsert_batch
    proj = sb.proj; ccm = proj / ".ccm"; ccm.mkdir()
    db = MemoryDB(ccm / "memory.db"); pid = db.upsert_project(str(proj))
    upsert_batch(db, pid, None, [{"category": "note", "content": f"memory number {i} about topic {i%7} with details", "importance": 3} for i in range(60)])
    pl = {"session_id": "S", "cwd": str(proj)}
    # 1. fail closed: make .ccm unwritable -> kick marker cannot be written -> no spawn
    os.chmod(ccm, 0o555)
    r = sb.run_hook("stop", pl)
    os.chmod(ccm, 0o755)
    print("unwritable .ccm: rc", r["rc"], "kick exists:", (ccm/".consolidation.kick").exists(), "stdout:", r["out"].strip()[:70])
    # 2. writable: spawn expected; wait for worker to write the marker
    r = sb.run_hook("stop", pl)
    print("writable .ccm: kick exists:", (ccm/".consolidation.kick").exists())
    for _ in range(60):
        if (ccm/".last_consolidation.json").exists(): break
        time.sleep(0.5)
    print("worker wrote marker:", (ccm/".last_consolidation.json").exists(), "lock cleaned:", not (ccm/".consolidation.lock").exists())
    # 3. tombstone: cleared plan must not be enforced
    plan_mod.apply_refined_plan(db, pid, {"goal": "g", "steps": [{"title": "s", "status": "pending"}]}, memory_dir=ccm)
    db.upsert_plan_active(pid, turns_since_last_guardian=50)
    db.clear_plan_active(pid)
    r = sb.run_hook("stop", pl)
    print("tombstone enforced (should be False):", r["out"].strip().startswith("{"))
    # 4. turns_total monotonic across reset_plan_guardian_counters
    plan_mod.apply_refined_plan(db, pid, {"goal": "g2", "steps": [{"title": "s2", "status": "pending"}]}, memory_dir=ccm)
    for _ in range(3): db.bump_plan_turn_counter(pid)
    before = db.get_plan_active(pid)["turns_total"]
    db.reset_plan_guardian_counters(pid); db.bump_plan_turn_counter(pid)
    after = db.get_plan_active(pid)
    print("turns_total before/after reset+bump:", before, after["turns_total"], "turns_since_last_guardian:", after["turns_since_last_guardian"])
finally:
    try: os.chmod(sb.proj / ".ccm", 0o755)
    except Exception: pass
    sb.cleanup()
