"""(1) kick fail-closed when the marker cannot be written (a directory squats the name; root ignores chmod).
(2) a STALE .consolidation.lock (dead worker / reboot) vetoes the Stop backpressure probe FOREVER --
only a consolidate_async worker reclaims a stale lock, and the probe refuses to spawn one while it exists."""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core.db import MemoryDB
    from llm.memory_writer import upsert_batch
    proj = sb.proj; ccm = proj / ".ccm"; ccm.mkdir()
    db = MemoryDB(ccm / "memory.db"); pid = db.upsert_project(str(proj))
    upsert_batch(db, pid, None, [{"category": "note", "content": f"memory number {i} about topic {i%7} with details", "importance": 3} for i in range(60)])
    pl = {"session_id": "S", "cwd": str(proj)}
    # (1) unwritable kick marker: a stale DIRECTORY squats the name
    (ccm / ".consolidation.kick").mkdir(); old = time.time() - 3600; os.utime(ccm / ".consolidation.kick", (old, old))
    r = sb.run_hook("stop", pl); time.sleep(1.5)
    print("(1) kick unwritable: rc", r["rc"], "stderr", repr(r["err"][:40]), "| worker ran:", (ccm/".last_consolidation.json").exists(), "lock:", (ccm/".consolidation.lock").exists())
    (ccm / ".consolidation.kick").rmdir()
    # (2) stale lock from a dead worker, 2 hours old (> _STALE_LOCK_S=360s)
    (ccm / ".consolidation.lock").write_text("99999 2026-01-01T00:00:00"); os.utime(ccm / ".consolidation.lock", (old - 3600, old - 3600))
    for i in range(3):
        r = sb.run_hook("stop", pl); time.sleep(1.0)
    print("(2) stale lock present: kicks over 3 Stops:", (ccm/".consolidation.kick").exists(), "| worker ran:", (ccm/".last_consolidation.json").exists(),
          "| lock age h:", round((time.time() - (ccm/".consolidation.lock").stat().st_mtime)/3600, 1))
    # the async PreCompact leg WOULD reclaim it -- but that only fires on compaction, the case backpressure exists for
    (ccm / ".consolidation.lock").unlink()
    r = sb.run_hook("stop", pl)
    for _ in range(40):
        if (ccm/".last_consolidation.json").exists(): break
        time.sleep(0.5)
    print("    after removing the stale lock by hand: worker ran:", (ccm/".last_consolidation.json").exists())
finally:
    sb.cleanup()
