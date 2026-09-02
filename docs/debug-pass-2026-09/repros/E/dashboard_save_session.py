"""Drive DashboardApp._save_current_session headlessly (Tk dialogs stubbed):
the session row it inserts carries an archive_path no code ever writes."""
import json, os, shutil, sys, tempfile, types
from pathlib import Path
SB = Path(tempfile.mkdtemp(prefix="E_save_"))
for k in ("HOME", "USERPROFILE"): os.environ[k] = str(SB)
(SB / "tmp").mkdir()
for k in ("TMPDIR", "TEMP", "TMP"): os.environ[k] = str(SB / "tmp")
try:
    assert Path.home() == SB
    sys.path.insert(0, "/home/user/cc-memory/cc_memory")
    from ui import dashboard as dash
    from core.db import MemoryDB
    proj = SB / "proj"; (proj / ".ccm").mkdir(parents=True)
    db = MemoryDB(proj / ".ccm" / "memory.db"); pid = db.upsert_project(str(proj))
    tdir = SB / "transcripts"; tdir.mkdir()
    recs = [{"type": "user", "message": {"role": "user", "content": "Please fix the login bug in auth.py."}},
            {"type": "assistant", "message": {"role": "assistant", "content": "We decided to use SQLite for session storage because it needs no server. Fixed the bug: the token expiry was compared in local time. Result: all 42 tests pass in 3.1 seconds."}},
            {"type": "user", "message": {"role": "user", "content": "Great, also set the timeout to 30 seconds in config.py."}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Configured timeout=30 in config.py. The architecture uses a single Flask app with blueprints."}}]
    (tdir / "abcd1234-session.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    app = dash.DashboardApp.__new__(dash.DashboardApp)
    app.db, app.project_id, app.project_path, app.memory_dir = db, pid, proj, proj / ".ccm"
    app._manual_api_key = ""
    app.root = types.SimpleNamespace(config=lambda **k: None, update=lambda: None)
    app.status_var = types.SimpleNamespace(set=lambda s: None)
    app._get_api_key = lambda: ""            # regex leg, no API
    app._optout_blocks_write = lambda: False
    app._refresh = lambda: []
    shown = []
    dash.messagebox = types.SimpleNamespace(askyesno=lambda *a, **k: True,
        showinfo=lambda t, m: shown.append((t, m)), showerror=lambda t, m: shown.append((t, m)), showwarning=lambda t, m: shown.append((t, m)))
    dash._find_transcript_dir = lambda p: tdir
    app._save_current_session()
    print("dialog:", shown[0][0], "|", shown[0][1].splitlines()[0])
    with db._connect() as c:
        rows = [dict(r) for r in c.execute("SELECT id, trigger_type, archive_path, complete, msg_count FROM sessions")]
    for r in rows:
        ap = r["archive_path"]
        print("session row:", r)
        print("  archive file exists at memory_dir/archive_path:", (proj / ".ccm" / ap).exists(), "| sessions/ dir contents:", sorted(str(p.relative_to(proj)) for p in (proj/".ccm"/"sessions").rglob("*")) if (proj/".ccm"/"sessions").exists() else "no sessions/ dir")
    # what the dashboard Sessions tab / web viewer / `/cc-mem sessions` show for it:
    print("  Sessions-tab 'Archive File' column would read:", Path(rows[0]["archive_path"]).name)
finally:
    shutil.rmtree(SB, ignore_errors=True)
    print("sandbox removed:", not SB.exists())
