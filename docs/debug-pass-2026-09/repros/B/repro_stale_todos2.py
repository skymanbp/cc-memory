"""Same root as repro_stale_todos.py, second path (tier 2B deferred): with NO transcripts on disk,
PreCompact wrote open_todos=[] (nothing pending) and next_steps = LLM 'task' memories; SessionStart
then re-fills §3 from next_steps split on ';' -- the '[]' PreCompact wrote is read as 'never filled'."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core.db import MemoryDB
    proj = sb.proj; (proj / ".ccm").mkdir()
    db = MemoryDB(proj / ".ccm" / "memory.db"); pid = db.upsert_project(str(proj))
    sid = db.insert_session(pid, "S1", "auto", 5, "", "")
    # what pre_compact writes when pending_todos is empty: next_steps = "; ".join(task memories)
    db.insert_session_summary(sid, pid, {"completed": "x", "next_steps": "consider caching later; maybe add metrics"})
    db.mark_session_complete(sid)
    db.tag_progress_session(pid, "S1")
    db.upsert_progress(pid, current_request="r", status_done="d", status_in_flight="i", open_todos=[],
                       plan="consider caching later; maybe add metrics", critical_context=[], files_touched=[{"path": "f", "action": "edit"}],
                       transcript_ptr="/t", trigger_type="precompact")
    before = db.get_progress(pid)["open_todos"]
    r = sb.run_hook("session_start", {"session_id": "S1", "cwd": str(proj), "source": "compact"})
    after = db.get_progress(pid)
    print("open_todos written by PreCompact:", before)
    print("open_todos after SessionStart    :", after["open_todos"], "| trigger:", after["trigger_type"])
finally:
    sb.cleanup()
