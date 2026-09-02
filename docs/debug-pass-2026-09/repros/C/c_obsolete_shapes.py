"""C candidate: detect_obsolete_llm trusts the SHAPE of the judge's array:
a non-dict element raises out of the stage (aborting run_consolidation), and
string ids are silently rejected (stage does nothing, no log)."""
import _sandbox
SB = _sandbox.enter()
import json, traceback
try:
    from core.db import MemoryDB
    from llm.memory_writer import upsert_smart
    import core.consolidate as C
    import llm.ccl_backend as B
    import core.auth as A

    proj = _sandbox.project()
    db = MemoryDB(proj / ".ccm" / "memory.db")
    pid = db.upsert_project(str(proj))
    ids = []
    for i, txt in enumerate(["The plugin ships two hooks in hooks.json today",
                             "Deploys go to the staging cluster first, always",
                             "The plugin ships five hooks in hooks.json today"]):
        ids.append(upsert_smart(db, pid, None, "arch", txt, topic=f"t{i}")["id"])
    print("ids:", ids)
    A.get_api_key = lambda: ("sk-ant-api-fake", "env")

    # (1) a model answering with a bare list of ids / strings
    for shape in ([1], ["none"], [[ids[0], ids[2]]], [None]):
        B.call_llm = (lambda s, u, k="", _shape=shape, **kw: json.dumps(_shape))
        try:
            r = C.detect_obsolete_llm(db, pid)
            print("shape", shape, "->", r)
        except Exception as e:
            print("shape", shape, "-> RAISED", type(e).__name__, e)

    # (2) ids quoted as strings: silently nothing
    B.call_llm = lambda s, u, k="", **kw: json.dumps(
        [{"stale_id": str(ids[0]), "current_id": str(ids[2]), "reason": "2 vs 5 hooks"}])
    r = C.detect_obsolete_llm(db, pid)
    print("string ids ->", r)
    print("log mentions rejection?:", "REJECTED" in _sandbox.logfile_text())

    # (3) same shape through run_consolidation (what the async hook / CLI run)
    B.call_llm = lambda s, u, k="", **kw: json.dumps([1])
    try:
        res = C.run_consolidation(str(proj), use_llm=True, verbose=False)
        print("run_consolidation ->", res)
    except Exception as e:
        print("run_consolidation RAISED", type(e).__name__, "->", e)

    # contrast: the other extract_json consumers type-check each element
    import re, pathlib
    src = pathlib.Path(_sandbox.PKG)
    for f in ["hooks/pre_compact.py", "hooks/stop.py", "hooks/session_start.py", "core/consolidate.py"]:
        t = (src / f).read_text(encoding="utf-8")
        print(f, "isinstance(m, dict) guards:", len(re.findall(r"isinstance\((m|p), dict\)", t)))
finally:
    _sandbox.leave()
