import _harness
try:
    from pathlib import Path
    from core.db import MemoryDB
    from core import consolidate as C
    root = Path(_harness.SB)/"p"; (root/".ccm").mkdir(parents=True)
    db = MemoryDB(root/".ccm"/"memory.db"); pid = db.upsert_project(str(root))
    # 3 near-identical, same importance, same category -> ids 1,2,3
    base = "The authentication service validates JWT tokens using the shared secret key here"
    a = db.insert_memory(pid, None, "arch", base, importance=3)
    b = db.insert_memory(pid, None, "arch", base.replace("here","now"), importance=3)
    c = db.insert_memory(pid, None, "arch", base.replace("here","today"), importance=3)
    print("ids:", a, b, c)
    from core.textsim import jaccard, shingle_set
    print("sims a-b,a-c,b-c:", round(jaccard(shingle_set(base),shingle_set(base.replace('here','now'))),3),
          round(jaccard(shingle_set(base),shingle_set(base.replace('here','today'))),3),
          round(jaccard(shingle_set(base.replace('here','now')),shingle_set(base.replace('here','today'))),3))
    n = C.merge_near_duplicates(db, pid, threshold=0.65)
    active = sorted(m["id"] for m in db.get_all_active_memories(pid))
    print("archived:", n, "active remaining:", active)
    # Expectation: keep 1 survivor (highest id among the cluster), archive the rest.
finally:
    _harness.cleanup()
