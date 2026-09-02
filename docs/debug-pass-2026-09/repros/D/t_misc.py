"""D: (a) disposition naming an unknown old_title; (b) what `status` says about the D1 stray memory/."""
import sys, json, re, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    proj = sb / "projA"
    db, pid, ids = _h.seed_project(proj, [{"content": "Seed decision: the scheduler runs every five minutes"}])
    good = {"goal": "Ship the billing migration", "success_criteria": ["All invoices reconcile"],
            "steps": [{"id": 1, "title": "Write the migration script", "status": "pending"},
                      {"id": 2, "title": "Run canary for one day", "status": "pending"}]}
    _h.mem(["plan-set", "--from-refiner"], proj, stdin=json.dumps(good))
    bad = {"goal": "New goal", "steps": [{"title": "Unrelated"}],
           "dispositions": [{"old_title": "Write the migration script", "action": "done", "reason": "shipped"},
                            {"old_title": "THIS STEP NEVER EXISTED", "action": "dropped", "reason": "bogus"}]}
    rc, out, err = _h.mem(["plan-set", "--from-refiner"], proj, stdin=json.dumps(bad))
    print("(a) unknown old_title disposition -> rc", rc, "|", out.strip().splitlines()[0][:120])

    # (b) D1 scenario, then status
    (proj / "memory").mkdir()
    MemoryDB, upsert_smart, _, _ = _h.pkg_imports()
    db2 = MemoryDB(proj / "memory" / "memory.db")
    pid2 = db2.upsert_project(str(proj))
    upsert_smart(db2, pid2, None, category="decision", content="Chose SQLite WAL mode for the memory store", importance=4, tags=["manual"], topic="")
    rc, out, err = _h.mem(["status"], proj, cwd=proj)
    print("(b) status lines mentioning the stray:")
    for l in out.splitlines():
        if "WARN" in l or "memory/" in l or "Separate" in l:
            print("   ", l.strip()[:160])
    rc, out, err = _h.mem(["paths"], proj, cwd=proj)
    print("    paths:", " | ".join(l.strip()[:60] for l in out.splitlines() if "memory_dir" in l or "database" in l))
finally:
    _h.destroy_sandbox()
