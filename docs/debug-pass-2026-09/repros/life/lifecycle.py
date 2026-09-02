import json, os, shutil, subprocess, sys, tempfile, time, sqlite3
from pathlib import Path
REPO = Path("/home/user/cc-memory")
box = Path(tempfile.mkdtemp(prefix="ccm-life-"))
home = box / "home"; home.mkdir()
for v in ("HOME","USERPROFILE","TMPDIR","TEMP","TMP"):
    os.environ[v] = str(home if v in ("HOME","USERPROFILE") else box)
for v in ("ANTHROPIC_API_KEY","ANTHROPIC_AUTH_TOKEN","CLAUDE_CODE_OAUTH_TOKEN"):
    os.environ.pop(v, None)
os.environ["CLAUDE_PLUGIN_ROOT"] = str(REPO)
proj = box / "proj"; proj.mkdir()
subprocess.run(["git","init","-q"], cwd=proj, check=True)
(proj/"app.py").write_text("print('hi')\n", encoding="utf-8")
(proj/"README.md").write_text("# demo\n", encoding="utf-8")
sid = "sess-0001-abcdef"
tp = home/".claude"/"projects"/("-" + str(proj).strip("/").replace("/","-"))
tp.mkdir(parents=True)
transcript = tp / f"{sid}.jsonl"
def rec(kind, content, i):
    msg = {"role": kind, "content": content if kind=="user" else [{"type":"text","text":content}]}
    return json.dumps({"type": kind, "message": msg, "cwd": str(proj), "sessionId": sid,
                       "timestamp": f"2026-09-01T10:{i:02d}:00.000Z", "uuid": f"u{i}"}, ensure_ascii=False)
lines = []
lines.append(rec("user", "Please add a CSV exporter to app.py and never push to main directly. 数据库用 SQLite。", 1))
lines.append(rec("assistant", "I will add export_csv() to app.py using the csv module.", 2))
lines.append(rec("user", "Also the API base URL is https://api.example.com/v2 and tests run with `python -m pytest -q`.", 3))
lines.append(rec("assistant", "Noted. Decision: keep the exporter synchronous. TODO: add unit tests for export_csv.", 4))
lines.append("null"); lines.append("42")
lines.append(rec("user", "<private>my token is sk-secret-123</private> remember the exporter must escape commas", 5))
lines.append(rec("assistant", "Done: export_csv escapes commas via csv.writer. Blocked on: CI credentials.", 6))
transcript.write_text("\n".join(lines)+"\n", encoding="utf-8")

def hook(name, extra, cwd=None, timeout_declared=None):
    payload = {"session_id": sid, "transcript_path": str(transcript), "cwd": str(cwd or proj),
               "hook_event_name": name}
    payload.update(extra)
    script = {"UserPromptSubmit":"user_prompt","PostToolUse":"post_tool_use","Stop":"stop",
              "PreCompact":"pre_compact","PreCompactAsync":"consolidate_async","SessionStart":"session_start"}[name]
    t0 = time.time()
    p = subprocess.run([sys.executable, str(REPO/"cc_memory"/"hooks"/f"{script}.py")], cwd=str(cwd or proj),
                       input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=400)
    dt = time.time()-t0
    flag = "" if (p.returncode==0 and not p.stderr) else "  <-- CONTRACT VIOLATION"
    print(f"[{name:16s}] rc={p.returncode} {dt:5.2f}s stdout={len(p.stdout)}B stderr={len(p.stderr)}B{flag}")
    if p.stderr: print("   STDERR:", p.stderr[:600])
    return p.stdout

def show(path, n=60):
    print(f"----- {path.relative_to(box)} -----")
    if not path.exists(): print("(missing)"); return
    txt = path.read_text(encoding="utf-8", errors="replace")
    for l in txt.splitlines()[:n]: print("  |", l)
    if len(txt.splitlines())>n: print(f"  | ... ({len(txt.splitlines())} lines)")

try:
    print("=== SESSION 1 ===")
    out = hook("UserPromptSubmit", {"prompt": "Please add a CSV exporter to app.py and never push to main directly."})
    if out: print("   UPS stdout (should be empty):", out[:200])
    print("   state dir:", sorted(p.name for p in proj.iterdir()))
    out = hook("PostToolUse", {"tool_name":"Read","tool_input":{"file_path":str(proj/"app.py")},"tool_response":{"type":"text","file":{"content":"print('hi')\n","filePath":str(proj/"app.py")}}})
    out = hook("PostToolUse", {"tool_name":"Edit","tool_input":{"file_path":str(proj/"app.py"),"old_string":"print('hi')","new_string":"import csv\nprint('hi')"},"tool_response":{"filePath":str(proj/"app.py")}})
    out = hook("PostToolUse", {"tool_name":"Bash","tool_input":{"command":"python -m pytest -q"},"tool_response":{"stdout":"3 passed","stderr":"","interrupted":False}})
    out = hook("PostToolUse", {"tool_name":"TodoWrite","tool_input":{"todos":[{"content":"add export_csv","status":"completed","activeForm":"adding"},{"content":"unit tests for export_csv","status":"pending","activeForm":"testing"}]},"tool_response":{"oldTodos":[],"newTodos":[]}})
    out = hook("Stop", {"stop_hook_active": False})
    print("   Stop stdout:", out.strip()[:400])
    out = hook("UserPromptSubmit", {"prompt": "now write the tests"})
    out = hook("Stop", {"stop_hook_active": False})
    print("   Stop stdout:", out.strip()[:400])
    out = hook("PreCompact", {"trigger":"auto","custom_instructions":""})
    print("   PreCompact stdout:", out.strip()[:400])
    out = hook("PreCompactAsync", {"trigger":"auto","custom_instructions":""})
    if out: print("   async stdout (should be empty):", out[:200])
    ccm = proj/".ccm"
    print("   .ccm listing:", sorted(p.name for p in ccm.iterdir()) if ccm.exists() else "MISSING")
    show(ccm/"PROGRESS.md", 80)
    show(ccm/"MEMORY.md", 40)
    show(ccm/".gitignore", 30)
    db = sqlite3.connect(str(ccm/"memory.db"))
    for t in ("projects","sessions","memories","observations","progress","plan_active","directives","session_summaries","topics","keywords"):
        try: print(f"   {t}: {db.execute(f'select count(*) from {t}').fetchone()[0]}")
        except Exception as e: print("   ", t, "ERR", e)
    print("   memories:", [ (r[0], r[1][:70]) for r in db.execute("select category, content from memories").fetchall()])
    print("   progress row:", db.execute("select current_request, status_done, status_in_flight, open_todos, files_touched, trigger_type from progress").fetchall())
    db.close()
    for f in sorted(ccm.glob("*.json"))+sorted(ccm.glob(".*.json")):
        print(f"   {f.name}: {f.read_text(encoding='utf-8')[:300]}")
    print("\n=== SESSION 2 (SessionStart after compact, then new plan) ===")
    sid2 = "sess-0002-zyxwvu"
    out = hook("SessionStart", {"source":"compact"})
    print("   SessionStart injection (%d B):" % len(out))
    for l in out.splitlines()[:70]: print("  >", l[:160])
    plan_text = "# Plan\n\n1. Add unit tests for export_csv in tests/test_app.py\n2. Wire CI to run pytest\n3. Tag v0.2\n"
    out = hook("PostToolUse", {"tool_name":"ExitPlanMode","tool_input":{"plan":plan_text},"tool_response":{"plan":plan_text,"isAgentApproved":True}})
    show(ccm/"PLAN.md", 40)
    out = hook("Stop", {"stop_hook_active": False})
    print("   Stop stdout after unrefined plan:", out.strip()[:700])
    try:
        j = json.loads(out); print("   Stop stdout is a JSON doc:", list(j.keys()))
    except Exception as e: print("   Stop stdout NOT JSON:", e)
    print("\n=== CLI ===")
    for args in (["status","--project",str(proj)],["paths","--project",str(proj)],["plan-status","--project",str(proj)],["inject-show","--project",str(proj)],["list","--project",str(proj)]):
        p = subprocess.run([sys.executable, str(REPO/"cc_memory"/"cli"/"mem.py")]+args, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(proj))
        print(f"--- mem.py {' '.join(args[:1])}: rc={p.returncode} stderr={p.stderr.strip()[:300]!r}")
        for l in p.stdout.splitlines()[:25]: print("  $", l[:150])
    print("\n=== leftovers in sandbox HOME ===")
    for p in sorted(home.rglob("*")):
        if p.is_file(): print("   ", p.relative_to(home), p.stat().st_size)
finally:
    shutil.rmtree(box, ignore_errors=True)
