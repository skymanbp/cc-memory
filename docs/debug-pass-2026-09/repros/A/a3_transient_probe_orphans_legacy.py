"""A3: a TRANSIENT identification failure (lock timeout / EACCES on the probe)
during the first v2.13 resolution of a legacy memory/ takes the UNSAFE branch
(fresh .ccm/) and outcome 1 then makes it permanent: memory/ with all history is
never consulted again by any surface."""
import os, sys, tempfile, shutil, sqlite3, time
from pathlib import Path
SB = tempfile.mkdtemp(prefix="A_a3_")
for v in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[v] = SB
tempfile.tempdir = SB
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
try:
    from core.db import MemoryDB
    from core import layout, roots
    from core.progress import ensure_memory_dir
    proj = Path(SB) / "proj"; (proj / "memory").mkdir(parents=True)     # pre-v2.13.0 install
    db = MemoryDB(proj / "memory" / "memory.db")
    pid = db.upsert_project(str(proj))
    for i in range(25):
        db.insert_memory(pid, None, "decision", f"important fact #{i}")
    del db
    # A pre-v2.13.0 install whose .gitignore predates the marker line, or whose
    # marker file the user removed -> identification rests on the DB probe alone.
    (proj / "memory" / ".gitignore").unlink()
    # Rollback-journal mode: the documented fallback for network shares
    # (`_warn_journal_mode`), and the one mode in which a writer's lock blocks readers.
    raw = sqlite3.connect(str(proj / "memory" / "memory.db"), isolation_level=None)
    raw.execute("PRAGMA journal_mode=DELETE")
    print("identified while idle          :", layout.is_ccm_dir(proj / "memory"))
    raw.execute("BEGIN EXCLUSIVE")                 # a concurrent writer mid-commit
    t0 = time.time()
    chosen = layout.memory_dir(proj)               # <- the FIRST hook of the session resolves
    print(f"memory_dir() under a {time.time()-t0:.1f}s lock ->", chosen.name,
          "| memory/ still holds the db:", (proj / "memory" / "memory.db").exists())
    raw.execute("COMMIT"); raw.close()             # lock gone 1s later
    ensure_memory_dir(chosen)                      # what every hook does next
    MemoryDB(chosen / "memory.db")                 # ...and then opens the database
    print("after that hook: .ccm/ exists   :", (proj / ".ccm").is_dir(), "| .ccm/memory.db rows:",
          sqlite3.connect(str(proj/'.ccm'/'memory.db')).execute("select count(*) from memories").fetchone()[0])
    print("identified now (lock gone)     :", layout.is_ccm_dir(proj / "memory"))
    print("memory_dir() now               :", layout.memory_dir(proj).name, "   <- outcome 1: permanent")
    print("find_memory_dir() now          :", layout.find_memory_dir(proj).name)
    print("project_root(proj/src) rung 0  :", roots._has_db(proj), "-> anchored on the empty .ccm/")
    print("legacy memory/memory.db rows   :", sqlite3.connect(str(proj/'memory'/'memory.db')).execute("select count(*) from memories").fetchone()[0], "  (orphaned; no surface reports it)")
    print("nested_databases(proj)         :", roots.nested_databases(proj))
finally:
    shutil.rmtree(SB, ignore_errors=True)
