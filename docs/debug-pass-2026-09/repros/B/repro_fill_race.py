"""session_start._refresh_progress_row: fill-only-empty is decided on a get_progress() read on one
connection and written by patch_progress() on another -- a PreCompact full rewrite landing between
the two is overwritten by the tier-2 fallback values (the same cross-process lost update
upsert_progress took BEGIN IMMEDIATE to close for the session tag)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core.db import MemoryDB
    import hooks.session_start as ss
    proj = sb.proj; (proj / ".ccm").mkdir()
    db = MemoryDB(proj / ".ccm" / "memory.db"); pid = db.upsert_project(str(proj))
    sid = db.insert_session(pid, "old", "auto", 1, "", "")
    db.insert_session_summary(sid, pid, {"completed": "OLD tier-2 completed text", "learned": "OLD learned",
                                          "next_steps": "OLD step A; OLD step B"})
    db.mark_session_complete(sid)
    # progress row exists but is EMPTY (a fresh project mid-session)
    db.tag_progress_session(pid, "S")
    real_get = db.get_progress
    def racing_get(project_id):
        cur = real_get(project_id)
        # a concurrent PreCompact (other session / racing hook) commits the authoritative rewrite
        # AFTER SessionStart's read and BEFORE its patch:
        db.upsert_progress(project_id, current_request="NEW request", status_done="NEW done (PreCompact)",
                           status_in_flight="NEW in-flight", open_todos=[{"content": "NEW todo", "status": "pending"}],
                           plan="NEW plan", critical_context=[], files_touched=[], transcript_ptr="/new", trigger_type="precompact")
        return cur
    db.get_progress = racing_get
    ss._refresh_progress_row(db, pid, proj / ".ccm", current_session_id="S")
    row = real_get(pid)
    for k in ("status_done", "status_in_flight", "plan", "open_todos", "trigger_type"):
        print(f"{k:16s} = {row[k]!r}")
    print("PreCompact's populated fields survived:", row["status_done"].startswith("NEW"))
finally:
    sb.cleanup()
