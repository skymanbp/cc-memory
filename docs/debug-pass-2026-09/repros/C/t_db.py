import _harness
try:
    from pathlib import Path
    import json
    from core.db import MemoryDB
    from llm import memory_writer as mw
    root = Path(_harness.SB) / "proj"
    (root / ".ccm").mkdir(parents=True)
    db = MemoryDB(root / ".ccm" / "memory.db")
    pid = db.upsert_project(str(root))

    print("=== (1) identical content, different case+tags+importance -> SKIP loses them ===")
    r1 = mw.upsert_smart(db, pid, None, "note", "Deploy key rotates monthly on the first", importance=2, tags=["alpha"], topic="sec")
    print("first:", r1)
    r2 = mw.upsert_smart(db, pid, None, "note", "deploy key rotates monthly on the first   ", importance=5, tags=["beta","gamma"], topic="sec")
    print("second (identical modulo case/space):", r2)
    row = db.get_memory(r1["id"])
    print("stored importance:", row["importance"], " tags:", row["tags"])
    # Compare: a NEAR-dup (>=0.80) bumps importance & unions tags
    r3 = mw.upsert_smart(db, pid, None, "note", "Deploy key rotates monthly on the 1st!!", importance=5, tags=["delta"], topic="sec")
    print("near-dup:", r3, "-> stored imp:", db.get_memory(r2['id'] if r2.get('id') else r1['id']))

    print("\n=== (2) tag cap drops the writer's own 'merged'/'supersedes' provenance ===")
    root2 = Path(_harness.SB) / "proj2"; (root2/".ccm").mkdir(parents=True)
    db2 = MemoryDB(root2/".ccm"/"memory.db"); pid2 = db2.upsert_project(str(root2))
    base_tags = [f"t{i}" for i in range(mw.MAX_TAGS)]  # exactly 32 tags
    b = mw.upsert_smart(db2, pid2, None, "note", "The alpha subsystem uses redis for caching layer", importance=3, tags=base_tags, topic="x")
    print("base tag count:", len(json.loads(db2.get_memory(b['id'])['tags'])))
    # now MERGE something >=0.80 similar; writer should append 'merged'
    m = mw.upsert_smart(db2, pid2, None, "note", "The alpha subsystem uses redis for caching layerr", importance=3, tags=[], topic="x")
    print("merge action:", m["action"])
    merged_tags = json.loads(db2.get_memory(b['id'])['tags'])
    print("tags after merge count:", len(merged_tags), " contains 'merged'?", "merged" in merged_tags)

    print("\n=== (3) consolidation_backlog with corrupt marker field types ===")
    from core import consolidate as C
    # last_memory_id as a list
    try:
        print("list last_memory_id:", C.consolidation_backlog(db, pid, {"last_memory_id":[1,2],"ts":"2020-01-01T00:00:00","project_path":str(root)}))
    except Exception as e:
        print("CRASH:", type(e).__name__, e)
    # ts as a number
    try:
        print("numeric ts:", C.consolidation_backlog(db, pid, {"last_memory_id":0,"ts":12345}))
    except Exception as e:
        print("CRASH:", type(e).__name__, e)
finally:
    _harness.cleanup()
