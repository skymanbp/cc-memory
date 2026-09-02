"""Hostile-but-reachable inputs against db.py / layout.py / roots.py entry points."""
import os, sys, tempfile, shutil, sqlite3
from pathlib import Path
SB = tempfile.mkdtemp(prefix="A_host_")
for v in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[v] = SB
tempfile.tempdir = SB
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
def t(label, fn):
    try:
        print(f"{label:48} -> {fn()!r}"[:200])
    except BaseException as e:
        print(f"{label:48} -> RAISED {type(e).__name__}: {str(e)[:100]}")
try:
    from core.db import MemoryDB, _readonly_uri, readonly_connect
    from core import layout, roots
    proj = Path(SB) / "proj"; (proj / ".ccm").mkdir(parents=True)
    db = MemoryDB(proj / ".ccm" / "memory.db")
    pid = db.upsert_project(str(proj))
    mid = db.insert_memory(pid, None, "note", "hello world fact")
    # migration ledger vs schema
    with db._connect() as c:
        names = {r[0] for r in c.execute("SELECT name FROM _migrations")}
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    from core.db import _MIGRATIONS
    print("all migrations applied:", names == {n for n, _ in _MIGRATIONS}, "| tables:", len(tables))
    # limits
    t("search_fts limit=inf", lambda: len(db.search_fts(pid, "hello", limit=float("inf"))))
    t("search_fts limit=nan", lambda: len(db.search_fts(pid, "hello", limit=float("nan"))))
    t("search_fts limit=10**30", lambda: len(db.search_fts(pid, "hello", limit=10**30)))
    t("search_fts limit='7'", lambda: len(db.search_fts(pid, "hello", limit="7")))
    t("search_fts limit=True", lambda: len(db.search_fts(pid, "hello", limit=True)))
    t("get_topics limit='x'", lambda: db.get_topics(pid, limit="x"))
    t("get_topics limit=-5", lambda: db.get_topics(pid, limit=-5))
    t("get_recent_memories limit=-1", lambda: len(db.get_recent_memories(pid, limit=-1)))
    t("get_recent_observations limit=-1", lambda: len(db.get_recent_observations(pid, limit=-1)))
    # ids
    t("get_memory('1')", lambda: bool(db.get_memory("1")))
    t("get_memory(-1)", lambda: db.get_memory(-1))
    t("get_memory(10**30)", lambda: db.get_memory(10**30))
    t("get_memory(None)", lambda: db.get_memory(None))
    t("get_supersede_chain('abc')", lambda: db.get_supersede_chain("abc"))
    t("archive_memory(10**30)", lambda: db.archive_memory(10**30))
    t("bulk_archive([10**30])", lambda: db.bulk_archive([10**30]))
    t("update_plan_status(10**30)", lambda: db.update_plan_status(10**30, "done", project_id=pid))
    # importance
    t("insert_memory importance=9", lambda: db.insert_memory(pid, None, "note", "imp nine", importance=9))
    t("update_memory importance=99 (clamps?)", lambda: db.update_memory(mid, importance=99))
    # directives
    t("upsert_directive all None", lambda: db.upsert_directive(pid, "s", quote=None, demand=None))
    t("upsert_directive times_stated=None", lambda: (db.upsert_directive(pid, "s", times_stated=None), db.list_directives(pid)[0]["times_stated"]))
    t("upsert_directive slug=''", lambda: db.upsert_directive(pid, ""))
    t("upsert_directive slug=None", lambda: db.upsert_directive(pid, None))
    # progress
    t("patch_progress bogus col", lambda: db.patch_progress(pid, nosuch=1))
    t("upsert_progress project_id kw", lambda: db.upsert_progress(pid, project_id=pid))
    t("upsert_progress updated_at kw", lambda: db.upsert_progress(pid, updated_at="x"))
    # uri shapes
    for p in ["/tmp/a b/x.db", "D:/a/x.db", "//srv/share/x.db", "/tmp/q?#%.db", "rel/x.db"]:
        t(f"_readonly_uri({p!r})", lambda p=p: _readonly_uri(p))
    t("readonly_connect(sandbox db) count", lambda: readonly_connect(db.db_path).execute("select count(*) from memories").fetchone()[0])
    t("readonly_connect DELETE", lambda: readonly_connect(db.db_path).execute("delete from memories"))
    t("readonly_connect ATTACH", lambda: readonly_connect(db.db_path).execute("attach ':memory:' as x"))
    # layout / roots non-path shapes
    for v in [None, 123, [1,2], b"/x", "", "a\x00b", 1.5, {"a":1}]:
        t(f"layout.memory_dir({v!r})", lambda v=v: layout.memory_dir(v))
        t(f"roots.project_root({v!r})", lambda v=v: roots.project_root(v))
    t("MemoryDB('a\\x00b/memory.db')", lambda: MemoryDB(Path(SB) / "a\x00b" / "memory.db"))
    t("MemoryDB(nonexistent parent's parent)", lambda: MemoryDB(Path(SB) / "gone" / ".ccm" / "memory.db"))
    t("upsert_project('')", lambda: db.upsert_project(""))
    t("layout.is_ccm_dir(file)", lambda: layout.is_ccm_dir(proj / ".ccm" / "memory.db"))
    t("nested_databases(None)", lambda: roots.nested_databases(None))
finally:
    shutil.rmtree(SB, ignore_errors=True)
