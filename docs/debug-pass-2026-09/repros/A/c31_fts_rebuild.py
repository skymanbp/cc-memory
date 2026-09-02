"""C31: which caller-supplied queries drive search_fts into a full FTS rebuild?"""
import os, sys, tempfile, shutil, sqlite3
from pathlib import Path
SB = tempfile.mkdtemp(prefix="A_c31_")
for v in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[v] = SB
tempfile.tempdir = SB
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
try:
    from core.db import MemoryDB
    proj = Path(SB) / "proj"; (proj / ".ccm").mkdir(parents=True)
    class Counting(MemoryDB):
        rebuilds = 0
        def _rebuild_fts5(self):
            Counting.rebuilds += 1
            return super()._rebuild_fts5()
    db = Counting(proj / ".ccm" / "memory.db")
    print("fts5 available:", db._fts5_available, "| sqlite", sqlite3.sqlite_version)
    pid = db.upsert_project(str(proj))
    for i in range(50):
        db.insert_memory(pid, None, "note", f"fact number {i} about the deploy key rotation")
    for q in ["", " ", "\x01", "\t", "()", "-", "*", ":", "^", "+", "a\x00b", "NEAR", "(", ")", "a AND", '"'] :
        before = Counting.rebuilds
        try:
            rows = db.search_fts(pid, q, limit=5)
            res = f"{len(rows)} rows"
        except Exception as e:
            res = f"RAISED {type(e).__name__}: {e}"
        print(f"query={q!r:10} -> {res:9} rebuilds+={Counting.rebuilds - before}")
finally:
    shutil.rmtree(SB, ignore_errors=True)
