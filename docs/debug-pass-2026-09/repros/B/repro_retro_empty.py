"""session_start._retroactive_extract returns None for an EMPTY model result (`valid if valid else None`),
so retroactive_save writes no sessions row -> the same transcript is re-sent to the LLM at EVERY SessionStart.
pre_compact._extract_via_llm was fixed for exactly this (register C1: an empty list is a RESULT)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-fake"
    add_pkg_path()
    from core.db import MemoryDB
    from core.extractor import mangle_project_path
    import llm.ccl_backend as be
    import hooks.session_start as ss
    calls = []
    be.call_llm = lambda system, user, *a, **k: (calls.append(len(user)), "[]")[1]   # model: nothing worth saving
    proj = sb.proj; (proj / ".ccm").mkdir()
    db = MemoryDB(proj / ".ccm" / "memory.db"); pid = db.upsert_project(str(proj))
    resolved = str(proj.resolve())
    tdir = sb.home / ".claude" / "projects" / mangle_project_path(resolved); tdir.mkdir(parents=True)
    rec = lambda **k: json.dumps(dict(cwd=resolved, **k))
    for name in ("s1", "s2", "s3"):
        recs = [rec(type="user", message={"role": "user", "content": f"{name} short question about x " * 20})] + \
               [rec(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": f"{name} answer " * 60}]}) for _ in range(6)]
        (tdir / f"{name}.jsonl").write_text("\n".join(recs) + "\n")
    for start in range(1, 4):
        n0 = len(calls)
        ss.retroactive_save(str(proj), db, pid, current_session_id="cur", deadline=None)
        print(f"SessionStart #{start}: LLM calls this start = {len(calls) - n0}, sessions rows recorded = {db.get_session_count(pid)}")
finally:
    sb.cleanup()
