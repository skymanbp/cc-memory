import _harness
try:
    import os
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api-FAKE"
    from pathlib import Path
    import json
    from core.db import MemoryDB
    from core import consolidate as C
    import llm.ccl_backend as backend

    # Fake judge: always says duplicates true, canonical = first member content
    calls = {"n":0}
    def fake_call_llm(system, user, api_key="", **kw):
        calls["n"] += 1
        # parse the member ids/contents out of user to build canonical from first
        # canonical: pick a fixed canonical text
        return json.dumps({"duplicates": True,
                           "canonical_content": "Cache TTL is set to thirty seconds for the redis layer",
                           "reason":"same fact reworded"})
    backend.call_llm = fake_call_llm

    root = Path(_harness.SB)/"p"; (root/".ccm").mkdir(parents=True)
    db = MemoryDB(root/".ccm"/"memory.db"); pid = db.upsert_project(str(root))
    # 3 rewordings, same category 'config'
    ids = []
    for c in ["Cache TTL set to 30 seconds in the redis caching layer configuration",
              "The redis layer cache TTL is configured at thirty seconds total",
              "We set redis cache time-to-live to 30s for the caching layer here"]:
        ids.append(db.insert_memory(pid, None, "config", c, importance=3))
    print("inserted ids:", ids, "active:", db.get_stats(pid)["n_memories"])

    res = C.semantic_dedup(db, pid, use_llm=True)
    print("semantic_dedup:", {k:res[k] for k in ("groups_judged","memories_archived")})
    active = [m["id"] for m in db.get_all_active_memories(pid)]
    print("active after:", active)
    # check supersedes chain on an archived loser
    for i in ids:
        m = db.get_memory(i)
        print(f"  id {i}: active={m['is_active']} supersedes_id={m['supersedes_id']} content={m['content'][:40]!r} tags={m['tags']}")

    print("\n=== convergence: judge that ALWAYS errors -> deep_dedup must terminate ===")
    def erroring(system, user, api_key="", **kw):
        calls["n"] += 1
        raise RuntimeError("API down")
    backend.call_llm = erroring
    root2 = Path(_harness.SB)/"p2"; (root2/".ccm").mkdir(parents=True)
    db2 = MemoryDB(root2/".ccm"/"memory.db"); pid2 = db2.upsert_project(str(root2))
    for k in range(6):
        db2.insert_memory(pid2, None, "note", f"The widget factory pattern handles allocation strategy variant {k%2}", importance=3)
    calls["n"]=0
    tot = C.deep_dedup(db2, pid2, use_llm=True, max_rounds=50)
    print("deep_dedup totals:", tot, " judge calls:", calls["n"])
    print("terminated (rounds<50):", tot["rounds"] < 50)
finally:
    _harness.cleanup()
