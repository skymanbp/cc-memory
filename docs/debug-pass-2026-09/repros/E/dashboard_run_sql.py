"""Drive DashboardApp._run_sql headlessly: a SELECT whose text contains a
write keyword (common in a memory DB) is classified as a write, confirmed, and
then its RESULT ROWS are discarded."""
import os, shutil, sys, tempfile, types
from pathlib import Path
SB = Path(tempfile.mkdtemp(prefix="E_sql_"))
for k in ("HOME", "USERPROFILE"): os.environ[k] = str(SB)
(SB / "tmp").mkdir()
for k in ("TMPDIR", "TEMP", "TMP"): os.environ[k] = str(SB / "tmp")
try:
    assert Path.home() == SB
    sys.path.insert(0, "/home/user/cc-memory/cc_memory")
    from ui import dashboard as dash
    from core.db import MemoryDB
    from llm.memory_writer import upsert_smart
    proj = SB / "proj"; (proj / ".ccm").mkdir(parents=True)
    db = MemoryDB(proj / ".ccm" / "memory.db"); pid = db.upsert_project(str(proj))
    upsert_smart(db, pid, None, category="bug", content="We must never DELETE the cache before the update lands", importance=4)
    upsert_smart(db, pid, None, category="note", content="Ordinary memory about sockets and ports", importance=2)

    app = dash.DashboardApp.__new__(dash.DashboardApp)
    app.db, app.project_id, app.project_path, app.memory_dir = db, pid, proj, proj / ".ccm"
    out = []
    app.sql_output = types.SimpleNamespace(delete=lambda *a: None, insert=lambda idx, text: out.append(text))
    app.status_var = types.SimpleNamespace(set=lambda s: out.append(f"[status] {s}"))
    app._optout_blocks_write = lambda: False
    app._refresh = lambda: []
    dialogs = []
    dash.messagebox = types.SimpleNamespace(askyesno=lambda title, msg: (dialogs.append(title), True)[1],
                                            showwarning=lambda *a, **k: None)
    for q in ("SELECT id, content FROM memories WHERE content LIKE '%delete%'",
              "SELECT id, content FROM memories WHERE content LIKE '%sockets%'"):
        out.clear(); dialogs.clear()
        app.sql_var = types.SimpleNamespace(get=lambda q=q: q)
        app._run_sql()
        print(f"query: {q}")
        print(f"  dialog shown: {dialogs}")
        print(f"  console output: {out!r}")
    # the rows really exist:
    with db._connect() as c:
        print("actual matching rows for '%delete%':", c.execute("SELECT count(*) FROM memories WHERE content LIKE '%delete%'").fetchone()[0])
finally:
    shutil.rmtree(SB, ignore_errors=True)
    print("sandbox removed:", not SB.exists())
