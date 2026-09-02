"""L11: a MemoryDB constructed against memory/ (rename refused/legacy) fails on
every operation once another surface's memory_dir() renames the directory."""
import os, sys, tempfile, shutil
from pathlib import Path
SB = tempfile.mkdtemp(prefix="A_l11_")
for v in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[v] = SB
tempfile.tempdir = SB
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
try:
    from core.db import MemoryDB
    from core import layout
    proj = Path(SB) / "proj"; (proj / "memory").mkdir(parents=True)   # pre-v2.13.0 layout
    db = MemoryDB(proj / "memory" / "memory.db")                       # long-lived holder (dashboard / web viewer)
    pid = db.upsert_project(str(proj))
    db.insert_memory(pid, None, "note", "the one fact")
    print("holder db_path      :", db.db_path.relative_to(SB))
    print("is_ccm_dir(memory/) :", layout.is_ccm_dir(proj / "memory"))
    new = layout.memory_dir(proj)                                      # another surface (a hook) resolves -> rename
    print("memory_dir(proj)    :", new.relative_to(SB), "| memory/ still exists:", (proj/'memory').exists())
    try:
        print("holder get_stats    :", db.get_stats(pid))
    except Exception as e:
        print("holder get_stats    : RAISED", type(e).__name__, "-", e)
    try:
        db.insert_memory(pid, None, "note", "a second fact")
        print("holder insert       : ok")
    except Exception as e:
        print("holder insert       : RAISED", type(e).__name__, "-", e)
    print("anything created at old path?", (proj / "memory").exists())
finally:
    shutil.rmtree(SB, ignore_errors=True)
