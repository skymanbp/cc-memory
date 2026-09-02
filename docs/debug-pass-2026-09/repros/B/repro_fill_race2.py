"""session_start._refresh_progress_row: the fill-only-empty verdict is a get_progress() read on one
connection; patch_progress() writes on another. A PreCompact rewrite committed between them is
clobbered by tier-2 fallback text (same lost-update class upsert_progress closed for the session tag)."""
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
    db.insert_session_summary(sid, pid, {"completed": "OLD tier-2 completed", "learned": "OLD learned", "next_steps": "OLD a; OLD b"})
    db.mark_session_complete(sid)
    db.tag_progress_session(pid, "S")            # row exists, every content field EMPTY
    real_patch = db.patch_progress
    def racing_patch(project_id, **patch):
        # SessionStart has decided its patch from an empty read; a concurrent PreCompact commits NOW
        db.upsert_progress(project_id, current_request="NEW request", status_done="NEW done (PreCompact)",
                           status_in_flight="NEW in-flight", open_todos=[{"content": "NEW todo", "status": "pending"}],
                           plan="NEW plan", critical_context=[], files_touched=[], transcript_ptr="/new", trigger_type="precompact")
        return real_patch(project_id, **patch)
    db.patch_progress = racing_patch
    ss._refresh_progress_row(db, pid, proj / ".ccm", current_session_id="S")
    row = db.get_progress(pid)
    for k in ("current_request", "status_done", "status_in_flight", "plan", "open_todos", "trigger_type"):
        print(f"{k:16s} = {row[k]!r}")
    print("populated PreCompact fields overwritten by the 'fill-only-empty' refresh:",
          [k for k in ("status_done", "status_in_flight", "plan", "open_todos") if not str(row[k]).startswith(("NEW", "[{'content': 'NEW"))])
finally:
    sb.cleanup()
