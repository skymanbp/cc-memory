import _harness
try:
    import os
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api-FAKE"
    from pathlib import Path
    import json
    from core.db import MemoryDB
    from core import consolidate as C
    import llm.ccl_backend as backend
    calls = {"n":0}
    def erroring(system, user, api_key="", **kw):
        calls["n"] += 1
        raise RuntimeError("API down")
    backend.call_llm = erroring
    root2 = Path(_harness.SB)/"p2"; (root2/".ccm").mkdir(parents=True)
    db2 = MemoryDB(root2/".ccm"/"memory.db"); pid2 = db2.upsert_project(str(root2))
    contents = [
        "The widget factory pattern handles allocation strategy alpha here",
        "The widget factory pattern handles allocation strategy beta today",
        "The widget factory pattern handles allocation strategy gamma now",
        "The widget factory pattern handles allocation strategy delta soon",
        "The widget factory pattern handles allocation strategy epsilon fast",
        "The widget factory pattern handles allocation strategy zeta later",
    ]
    for c in contents:
        db2.insert_memory(pid2, None, "note", c, importance=3)
    calls["n"]=0
    tot = C.deep_dedup(db2, pid2, use_llm=True, max_rounds=50)
    print("deep_dedup totals:", tot, " judge calls:", calls["n"])
    print("terminated (rounds<50):", tot["rounds"] < 50)
finally:
    _harness.cleanup()
