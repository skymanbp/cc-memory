"""D: two project rows in ONE memory.db -- does any mem.py/plan.py command leak or touch B?"""
import sys, json, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

LEAK = "ZZLEAKB"
sb = _h.make_sandbox()
try:
    proj = sb / "projA"
    db, pidA, idsA = _h.seed_project(proj, [
        {"content": "Project A decision alpha: keep the auth module in Rust for speed"},
        {"content": "Project A result beta: p95 latency 42 ms after caching", "category": "result"},
    ])
    MemoryDB, upsert_smart, upsert_batch, _ = _h.pkg_imports()
    # second project ROW in the SAME file (a renamed dir / nested layout produces this)
    pidB = db.upsert_project(str(sb / "projB_ghost"))
    rB = upsert_smart(db, pidB, None, category="decision",
                      content=f"{LEAK} secret decision of project B about the billing schema",
                      importance=5, tags=["manual"], topic=f"{LEAK}topic")
    idB = rB["id"]
    # corrupted row in B (for encoding-check --apply)
    rB2 = upsert_smart(db, pidB, None, category="note",
                       content=f"{LEAK} corrupted �� text row of project B here",
                       importance=2, tags=["manual"], topic="")
    idB2 = rB2["id"]
    sidB = db.insert_session(pidB, "sessB", "manual", 5, f"/x/{LEAK}archive.jsonl", "B summary")
    db.mark_session_complete(sidB)
    db.upsert_topic(pidB, f"{LEAK}-topic", f"{LEAK} topic body of B")
    with db._connect() as c:
        c.execute("INSERT INTO keywords (project_id, keyword, frequency, last_seen) VALUES (?,?,?,?)",
                  (pidB, f"{LEAK}kw", 9, db._now()))
    db.insert_observation(pidB, "sessB", "Read", f"{LEAK}/path/file.py", "out", 0) \
        if hasattr(db, "insert_observation") else None
    db.insert_session_summary(sidB, pidB, {"request": f"{LEAK} request of B"})
    db.upsert_progress(pidB, trigger_type="manual", current_request=f"{LEAK} B progress")
    db.upsert_directive(pidB, f"{LEAK}-directive", demand=f"{LEAK} demand", quote="q")
    db.upsert_plan_active(pidB, raw=f"{LEAK} raw plan of B", needs_refine=1)
    planB = db.add_plan(pidB, f"{LEAK} plan-queue item of B")

    def snapshot():
        c = sqlite3.connect(str(proj / ".ccm" / "memory.db"))
        c.row_factory = sqlite3.Row
        snap = {}
        for t in ("memories", "sessions", "topics", "keywords", "observations",
                  "session_summaries", "progress", "directives", "plan_active", "plans"):
            try:
                snap[t] = [tuple(r) for r in c.execute(
                    f"SELECT * FROM {t} WHERE project_id = ? ORDER BY 1", (pidB,))]
            except sqlite3.Error as e:
                snap[t] = str(e)
        c.close()
        return snap
    before = snapshot()

    reads = [
        ["stats"], ["list"], ["list", "--sessions", "50"], ["search", LEAK],
        ["search", "decision"], ["sessions"], ["keywords"], ["topics"],
        ["observations"], ["summary"], ["supersedes", str(idB)],
        ["directive-list", "--status", "all"], ["directive-list", "--json"],
        ["plan-status"], ["plan-show"], ["inject-usage"], ["encoding-check"],
        ["paths"], ["progress"], ["status"],
    ]
    leaks = []
    for args in reads:
        rc, out, err = _h.mem(args, proj, cwd=proj)
        if LEAK in out or LEAK in err:
            leaks.append((args, rc, out[:400]))
        if _h.is_traceback(err):
            print("TRACEBACK in", args, err[-600:])
    print("READ leaks:", leaks or "none")

    writes = [
        ["archive", str(idB)], ["archive", str(idsA[0]), "--supersedes", str(idB)],
        ["encoding-check", "--apply"], ["cleanup"], ["consolidate", "--no-llm"],
        ["mode", "research"], ["directive-add", "a-dir", "--demand", "A demand"],
        ["directive-close", f"{LEAK}-directive", "--evidence", "x"],
        ["directive-edit", f"{LEAK}-directive", "--demand", "hijack"],
        ["plan-set", "--raw", "A raw plan"], ["plan-replan"], ["plan-clear", "--reason", "r"],
        ["add", "decision", "Project A adds a new decision about the cache layer"],
    ]
    for args in writes:
        rc, out, err = _h.mem(args, proj, cwd=proj)
        if _h.is_traceback(err):
            print("TRACEBACK in", args, err[-600:])
        if LEAK in out:
            print("WRITE cmd printed B content:", args, out[:300])
    # plan.py queue
    for args in (["list", "--all"], ["status"], ["approve", str(planB)], ["done", str(planB)],
                 ["evaluate", str(planB)], ["clear"], ["reorder", str(planB)]):
        rc, out, err = _h.plan(args, proj, cwd=proj)
        if LEAK in out or _h.is_traceback(err):
            print("plan.py", args, rc, out[:300], err[-300:])
    after = snapshot()
    changed = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
    if changed:
        for t, (b, a) in changed.items():
            print(f"B's {t} CHANGED:\n  before={b}\n  after ={a}")
    else:
        print("B's rows: untouched by every write command")
finally:
    _h.destroy_sandbox()
