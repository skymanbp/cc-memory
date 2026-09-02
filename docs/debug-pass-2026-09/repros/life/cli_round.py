import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
REPO = Path("/home/user/cc-memory")
box = Path(tempfile.mkdtemp(prefix="ccm-cli-")); home = box/"home"; home.mkdir()
for v in ("HOME","USERPROFILE"): os.environ[v] = str(home)
for v in ("TMPDIR","TEMP","TMP"): os.environ[v] = str(box)
for v in ("ANTHROPIC_API_KEY","ANTHROPIC_AUTH_TOKEN","CLAUDE_CODE_OAUTH_TOKEN"): os.environ.pop(v, None)
os.environ["CLAUDE_PLUGIN_ROOT"] = str(REPO)
proj = box/"proj"; proj.mkdir(); subprocess.run(["git","init","-q"], cwd=proj, check=True)
sid="sess-0001-abcdef"; tp = home/".claude"/"projects"/("-"+str(proj).strip("/").replace("/","-")); tp.mkdir(parents=True)
transcript = tp/f"{sid}.jsonl"
transcript.write_text(json.dumps({"type":"user","message":{"role":"user","content":"add exporter"},"cwd":str(proj),"sessionId":sid})+"\n", encoding="utf-8")
def hook(script, extra):
    payload = {"session_id": sid, "transcript_path": str(transcript), "cwd": str(proj)}; payload.update(extra)
    p = subprocess.run([sys.executable, str(REPO/"cc_memory"/"hooks"/f"{script}.py")], cwd=str(proj), input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert p.returncode==0 and not p.stderr, (script, p.returncode, p.stderr)
    return p.stdout
def cli(*args):
    p = subprocess.run([sys.executable, str(REPO/"cc_memory"/"cli"/"mem.py"), "--project", str(proj)]+list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(proj), timeout=120)
    print(f"--- mem.py {' '.join(args)}: rc={p.returncode} stderr={p.stderr.strip()[:200]!r}")
    for l in p.stdout.splitlines()[:30]: print("  $", l[:160])
try:
    hook("user_prompt", {"prompt":"add exporter"})
    hook("post_tool_use", {"tool_name":"Edit","tool_input":{"file_path":str(proj/"a.py")},"tool_response":{}})
    hook("stop", {"stop_hook_active":False})
    hook("pre_compact", {"trigger":"manual"})
    out = hook("session_start", {"source":"startup"})
    print("=== SessionStart injection TAIL ===")
    for l in out.splitlines()[-25:]: print("  >", l[:200])
    print("=== reminder block count:", out.count("<system-reminder>"), out.count("</system-reminder>"), "| mentions memory/:", "memory/" in out, "| mentions .ccm/PROGRESS.md:", ".ccm/PROGRESS.md" in out)
    cli("status"); cli("paths"); cli("plan-status"); cli("inject-show"); cli("list"); cli("progress"); cli("sessions"); cli("directive-list"); cli("stats"); cli("schema")
    print("=== .ccm after all ===", sorted(p.name for p in (proj/".ccm").iterdir()))
finally:
    shutil.rmtree(box, ignore_errors=True)
