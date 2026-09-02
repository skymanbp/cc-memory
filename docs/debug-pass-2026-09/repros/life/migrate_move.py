import json, os, shutil, subprocess, sys, tempfile, sqlite3
from pathlib import Path
REPO = Path("/home/user/cc-memory")
box = Path(tempfile.mkdtemp(prefix="ccm-mig-")); home = box/"home"; home.mkdir()
for v in ("HOME","USERPROFILE"): os.environ[v] = str(home)
for v in ("TMPDIR","TEMP","TMP"): os.environ[v] = str(box)
for v in ("ANTHROPIC_API_KEY","ANTHROPIC_AUTH_TOKEN","CLAUDE_CODE_OAUTH_TOKEN"): os.environ.pop(v, None)
os.environ["CLAUDE_PLUGIN_ROOT"] = str(REPO)
sys.path.insert(0, str(REPO/"cc_memory"))
from core.db import MemoryDB
from core.progress import ensure_memory_gitignore
from llm import memory_writer
def hook(script, proj, extra, sid="sess-mig-000001"):
    payload = {"session_id": sid, "transcript_path": str(box/"none.jsonl"), "cwd": str(proj)}; payload.update(extra)
    p = subprocess.run([sys.executable, str(REPO/"cc_memory"/"hooks"/f"{script}.py")], cwd=str(proj), input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    flag = "" if (p.returncode==0 and not p.stderr) else "   <-- VIOLATION"
    print(f"  [{script:14s}] rc={p.returncode} stdout={len(p.stdout)}B{flag} {p.stderr[:200]}")
    return p.stdout
def cli(proj, *args):
    p = subprocess.run([sys.executable, str(REPO/"cc_memory"/"cli"/"mem.py"), "--project", str(proj)]+list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(proj), timeout=120)
    return p.returncode, p.stdout, p.stderr
try:
    print("=== SCENARIO 1: legacy memory/ project ===")
    proj = box/"legacy"; proj.mkdir(); subprocess.run(["git","init","-q"], cwd=proj, check=True)
    legacy = proj/"memory"; legacy.mkdir()
    ensure_memory_gitignore(legacy)  # the plugin's marker
    db = MemoryDB(legacy/"memory.db"); pid = db.upsert_project(str(proj))
    r = memory_writer.upsert_smart(db, pid, None, "decision", "We decided to keep the exporter synchronous for now", 4)
    print("  seeded memory:", r if not isinstance(r, dict) else r.get("action"), "| rows:", db.count_memories(pid) if hasattr(db,"count_memories") else "?")
    del db
    out = hook("session_start", proj, {"source":"startup"})
    print("  after SessionStart: memory/ exists:", legacy.exists(), "| .ccm exists:", (proj/".ccm").exists(), "| injected memories line:", [l for l in out.splitlines() if "Injected" in l][:1])
    hook("user_prompt", proj, {"prompt":"hello"})
    print("  after UserPromptSubmit: memory/ exists:", legacy.exists(), "| .ccm exists:", (proj/".ccm").exists(), "| .ccm has db:", (proj/".ccm"/"memory.db").exists())
    (box/"none.jsonl").write_text(json.dumps({"type":"user","message":{"role":"user","content":"hi"},"cwd":str(proj)})+"\n", encoding="utf-8")
    hook("pre_compact", proj, {"trigger":"manual"})
    mm = (proj/".ccm"/"MEMORY.md").read_text(encoding="utf-8") if (proj/".ccm"/"MEMORY.md").exists() else ""
    print("  MEMORY.md archive links:", [l.strip() for l in mm.splitlines() if "sessions/" in l][:3])
    rc, o, e = cli(proj, "list"); print("  cli list rc", rc, "| has seeded memory:", "synchronous" in o)
    print("=== SCENARIO 1b: memory/ is the user's own package ===")
    proj2 = box/"pkgproj"; proj2.mkdir(); subprocess.run(["git","init","-q"], cwd=proj2, check=True)
    (proj2/"memory").mkdir(); (proj2/"memory"/"__init__.py").write_text("x=1\n", encoding="utf-8")
    hook("user_prompt", proj2, {"prompt":"hello"})
    print("  user package intact:", (proj2/"memory"/"__init__.py").exists(), "| .ccm created:", (proj2/".ccm"/"memory.db").exists())

    print("\n=== SCENARIO 2: project directory moved ===")
    p1 = box/"alpha"; p1.mkdir(); subprocess.run(["git","init","-q"], cwd=p1, check=True)
    hook("user_prompt", p1, {"prompt":"build the thing"})
    rc,o,e = cli(p1, "add", "decision", "Use SQLite for the local cache because it is zero-config"); print("  cli add rc", rc, e.strip()[:100])
    rc,o,e = cli(p1, "add", "config", "API base URL is https://api.example.com/v2"); print("  cli add rc", rc, e.strip()[:100])
    hook("pre_compact", p1, {"trigger":"manual"})
    rc,o,e = cli(p1, "list"); print("  before move: list shows", o.count("│") or sum(1 for l in o.splitlines() if l.strip().startswith(("[", "#", "1", "2"))), "lines; contains SQLite:", "SQLite" in o)
    p2 = box/"alpha-renamed"; shutil.move(str(p1), str(p2))
    out = hook("session_start", p2, {"source":"startup"})
    print("  after move, SessionStart:", [l for l in out.splitlines() if "Injected" in l or "cc-memory" in l][:3])
    rc,o,e = cli(p2, "list"); print("  after move: cli list contains SQLite:", "SQLite" in o, "| rc", rc)
    rc,o,e = cli(p2, "status"); print("  status db line:", [l.strip() for l in o.splitlines() if "Database" in l])
    db = sqlite3.connect(str(p2/".ccm"/"memory.db"))
    print("  projects table:", db.execute("select id, path, name from projects").fetchall())
    print("  memories by project:", db.execute("select project_id, count(*) from memories group by project_id").fetchall())
    print("  progress rows by project:", db.execute("select project_id from progress").fetchall())
    db.close()
    prog = (p2/".ccm"/"PROGRESS.md").read_text(encoding="utf-8")
    print("  PROGRESS.md header path line:", [l for l in prog.splitlines() if "Generated" in l][:1])
finally:
    shutil.rmtree(box, ignore_errors=True)
