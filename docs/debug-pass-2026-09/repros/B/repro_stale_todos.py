"""session_start._refresh_progress_row tier 3: after a compaction whose PreCompact legitimately
wrote open_todos=[] (nothing pending), SessionStart(source=compact) excludes the CURRENT transcript
and mines the newest OTHER session's transcript, resurrecting that session's stale todos into
PROGRESS.md §3 -- which the RESUME PROTOCOL then orders the next Claude to auto-execute."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core.db import MemoryDB
    from core.extractor import mangle_project_path
    proj = sb.proj; (proj / ".ccm").mkdir()
    db = MemoryDB(proj / ".ccm" / "memory.db"); pid = db.upsert_project(str(proj))
    resolved = str(proj.resolve())
    # What PreCompact of the CURRENT session (S2) just wrote: everything populated, no pending todos.
    db.tag_progress_session(pid, "S2-current")
    db.upsert_progress(pid, current_request="S2 task: polish docs", status_done="docs polished",
                       status_in_flight="-", open_todos=[], plan="none", critical_context=[],
                       files_touched=[{"path": "README.md", "action": "edit"}],
                       transcript_ptr="/x/S2-current.jsonl", trigger_type="precompact")
    # An OLD, unrelated session S1 whose transcript is still on disk (weeks old), with a pending todo.
    tdir = sb.home / ".claude" / "projects" / mangle_project_path(resolved); tdir.mkdir(parents=True)
    rec = lambda **k: json.dumps(dict(cwd=resolved, **k))
    old = [rec(type="user", message={"role": "user", "content": "S1: migrate the database"}),
           rec(type="assistant", message={"role": "assistant", "content": [
               {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
                   {"content": "DROP the legacy users table (S1 stale todo)", "status": "pending", "priority": "high"}]}}]})]
    (tdir / "S1-old.jsonl").write_text("\n".join(old) + "\n")
    os.utime(tdir / "S1-old.jsonl", (time.time() - 30*86400,) * 2)
    cur = [rec(type="user", message={"role": "user", "content": "S2 task: polish docs"})]
    (tdir / "S2-current.jsonl").write_text("\n".join(cur) + "\n")

    r = sb.run_hook("session_start", {"session_id": "S2-current", "cwd": str(proj), "source": "compact",
                                      "transcript_path": str(tdir / "S2-current.jsonl")})
    print("rc", r["rc"], "stderr", repr(r["err"][:80]))
    prog = (proj / ".ccm" / "PROGRESS.md").read_text()
    sec3 = prog.split("## 3. Open Todos")[1].split("## 4.")[0]
    print("PROGRESS.md §3 after SessionStart(compact):", sec3.strip())
    row = db.get_progress(pid)
    print("progress.open_todos now:", row["open_todos"], "| trigger_type:", row["trigger_type"])
finally:
    sb.cleanup()
