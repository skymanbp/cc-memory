import json, os, shutil, subprocess, sys, tempfile, sqlite3, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
REPO = Path("/home/user/cc-memory")
box = Path(tempfile.mkdtemp(prefix="ccm-conc-")); home = box/"home"; home.mkdir()
for v in ("HOME","USERPROFILE"): os.environ[v] = str(home)
for v in ("TMPDIR","TEMP","TMP"): os.environ[v] = str(box)
for v in ("ANTHROPIC_API_KEY","ANTHROPIC_AUTH_TOKEN","CLAUDE_CODE_OAUTH_TOKEN"): os.environ.pop(v, None)
os.environ["CLAUDE_PLUGIN_ROOT"] = str(REPO)
proj = box/"proj"; proj.mkdir(); subprocess.run(["git","init","-q"], cwd=proj, check=True)
sid="sess-conc-000001"; tp = home/".claude"/"projects"/("-"+str(proj).strip("/").replace("/","-")); tp.mkdir(parents=True)
transcript = tp/f"{sid}.jsonl"
transcript.write_text("\n".join(json.dumps({"type":"user","message":{"role":"user","content":f"msg {i} about the exporter design decision"},"cwd":str(proj),"sessionId":sid}) for i in range(30))+"\n", encoding="utf-8")
def hook(script, extra, cwd=None):
    payload = {"session_id": sid, "transcript_path": str(transcript), "cwd": str(cwd or proj)}; payload.update(extra)
    t0=time.time()
    p = subprocess.run([sys.executable, str(REPO/"cc_memory"/"hooks"/f"{script}.py")], cwd=str(cwd or proj), input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    return script, p.returncode, p.stderr, p.stdout, time.time()-t0
try:
    # cold start: 8 hooks racing to CREATE .ccm/ and the DB at once (first turn of a fresh project)
    jobs = [("user_prompt", {"prompt":"go"})] + [("post_tool_use", {"tool_name":"Edit","tool_input":{"file_path":str(proj/f"f{i}.py")},"tool_response":{}}) for i in range(6)] + [("stop", {"stop_hook_active":False})]
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda j: hook(*j), jobs))
    for r in res:
        flag = "" if (r[1]==0 and not r[2]) else "   <-- VIOLATION"
        print(f"cold  {r[0]:14s} rc={r[1]} {r[4]:.2f}s stderr={r[2][:200]!r}{flag}")
    db = sqlite3.connect(str(proj/".ccm"/"memory.db"))
    print("cold  observations:", db.execute("select count(*) from observations").fetchone()[0], "(expected 6 edits observed?)", "projects:", db.execute("select count(*) from projects").fetchone()[0], "integrity:", db.execute("pragma integrity_check").fetchone()[0])
    print("cold  progress rows:", db.execute("select count(*) from progress").fetchone()[0], db.execute("select current_request, files_touched from progress").fetchall())
    db.close()
    # warm: 16 post_tool_use + 2 stops + 1 pre_compact + 1 consolidate_async all at once
    jobs = [("post_tool_use", {"tool_name":"Read","tool_input":{"file_path":str(proj/f"g{i}.py")},"tool_response":{"type":"text","file":{"content":"x"}}}) for i in range(16)]
    jobs += [("stop", {"stop_hook_active":False})]*2 + [("pre_compact", {"trigger":"auto"}), ("consolidate_async", {"trigger":"auto"})]
    with ThreadPoolExecutor(max_workers=20) as ex:
        res = list(ex.map(lambda j: hook(*j), jobs))
    bad = [r for r in res if r[1]!=0 or r[2]]
    print(f"warm  {len(res)} hooks, violations={len(bad)}, max wall={max(r[4] for r in res):.2f}s")
    for r in bad: print("   VIOLATION", r[0], r[1], r[2][:300])
    for r in res:
        if r[0] in ("stop","pre_compact"): print(f"   {r[0]} stdout: {r[3].strip()[:150]!r}")
    db = sqlite3.connect(str(proj/".ccm"/"memory.db"))
    print("warm  observations:", db.execute("select count(*) from observations").fetchone()[0], "sessions:", db.execute("select count(*) from sessions").fetchone()[0], "integrity:", db.execute("pragma integrity_check").fetchone()[0])
    print("warm  files_touched entries:", len(json.loads(db.execute("select files_touched from progress").fetchone()[0])))
    db.close()
    print(".ccm:", sorted(p.name for p in (proj/".ccm").iterdir()))
    logf = list((home/".claude"/"hooks"/"cc-memory"/"logs").glob("*.log"))
    for f in logf:
        txt = f.read_text(encoding="utf-8", errors="replace")
        errs = [l for l in txt.splitlines() if "ERROR" in l or "WARN" in l or "Traceback" in l]
        print(f"log {f.name}: {len(txt.splitlines())} lines, {len(errs)} error/warn lines")
        for l in errs[:12]: print("   ", l[:220])
finally:
    shutil.rmtree(box, ignore_errors=True)
