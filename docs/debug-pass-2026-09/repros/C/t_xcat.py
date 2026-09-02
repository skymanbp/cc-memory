import _harness
try:
    from pathlib import Path
    from core.db import MemoryDB
    from llm import memory_writer as mw
    root = Path(_harness.SB)/"p"; (root/".ccm").mkdir(parents=True)
    db = MemoryDB(root/".ccm"/"memory.db"); pid = db.upsert_project(str(root))
    # First store as category 'note', topic 'auth'
    r1 = mw.upsert_smart(db, pid, None, "note", "The auth module rejects tokens whose signature does not verify", importance=3, topic="auth")
    print("r1:", r1, "cat:", db.get_memory(r1["id"])["category"])
    # Now store a HIGH-similarity restatement but as category 'bug', same topic
    r2 = mw.upsert_smart(db, pid, None, "bug", "The auth module rejects tokens whose signature does not verifyy", importance=4, topic="auth")
    print("r2:", r2)
    m = db.get_memory(r1["id"])
    print("after: id", m["id"], "category:", m["category"], "(model said 'bug', importance 4)")
    print("CROSS-CATEGORY MERGE (category kept as 'note')?", m["category"]=="note" and r2["action"]=="merged")
finally:
    _harness.cleanup()
