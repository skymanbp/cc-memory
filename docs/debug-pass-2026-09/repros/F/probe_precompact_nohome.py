"""F probe: WHERE does pre_compact lose PROGRESS.md when no home resolves? Instrument the logger (which has
nowhere to write in that state) to echo to stdout, then run the hook's main() in-process as the passwd-less uid."""
import os, sys, json, shutil, tempfile, subprocess, textwrap
SB = tempfile.mkdtemp(prefix="F-pcnh-")
for k in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[k] = SB
tempfile.tempdir = None
HOOKS = "/home/user/cc-memory/cc_memory/hooks"
try:
    proj = os.path.join(SB, "proj"); os.makedirs(proj); tmpd = os.path.join(SB, "tmp"); os.makedirs(tmpd)
    tr = os.path.join(SB, "t.jsonl")
    with open(tr, "w", encoding="utf-8") as fh:
        for i in range(4):
            fh.write(json.dumps({"type": "user", "cwd": proj, "message": {"role": "user", "content": f"turn {i}: fix the parser in cli.py"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "cwd": proj, "message": {"role": "assistant", "content": [{"type": "text", "text": f"Fixed cli.py {i}"}]}}) + "\n")
    env = dict(os.environ); env["TMPDIR"] = tmpd; env["ANTHROPIC_API_KEY"] = "sk-ant-api03-PROBE-NOT-REAL"
    subprocess.run([sys.executable, f"{HOOKS}/user_prompt.py"], input=json.dumps({"session_id": "s1", "cwd": proj, "prompt": "hi"}), capture_output=True, env=env, cwd=proj, encoding="utf-8")
    for root_, dirs, files in os.walk(SB):
        os.chmod(root_, 0o777)
        for f in files: os.chmod(os.path.join(root_, f), 0o666)
    nohome = {k: v for k, v in env.items() if k not in ("HOME", "USERPROFILE")}
    for f in (".ccm/PROGRESS.md", ".ccm/.last_save.json"):
        fp = os.path.join(proj, f); os.path.exists(fp) and os.remove(fp)
    print("PROGRESS.md before hook:", os.path.exists(os.path.join(proj, ".ccm", "PROGRESS.md")))
    payload = json.dumps({"session_id": "s1", "cwd": proj, "transcript_path": tr, "trigger": "manual"})
    driver = textwrap.dedent(f"""
        import os, sys, io, runpy
        os.setgid(54321); os.setuid(54321); os.chdir({proj!r})
        sys.path.insert(0, "/home/user/cc-memory/cc_memory")
        import core.logger as L
        def _echo(self, level, msg):
            if level in ("WARN", "ERROR"): print(f"  [LOG {{level}}] {{msg[:600]}}", file=sys.stderr)
        L.Logger._write = _echo
        sys.stdin = io.TextIOWrapper(io.BytesIO({payload!r}.encode()))
        try:
            runpy.run_path({HOOKS + "/pre_compact.py"!r}, run_name="__main__")
        except SystemExit as e:
            print("  exit code:", e.code, file=sys.stderr)
    """)
    r = subprocess.run([sys.executable, "-c", driver], capture_output=True, encoding="utf-8", errors="replace", env=nohome, cwd=proj, timeout=180)
    print("stdout:", r.stdout.strip()[:200] or "(empty — no status line)")
    print(r.stderr[-3500:])
    print("PROGRESS.md after hook:", os.path.exists(os.path.join(proj, ".ccm", "PROGRESS.md")))
    ls = os.path.join(proj, ".ccm", ".last_save.json"); print(".last_save.json:", open(ls).read()[:300] if os.path.exists(ls) else None)
    for r_, _, fs in os.walk(os.path.join(proj, ".ccm", "sessions")):
        for f in fs: print("archive left behind:", f, os.path.getsize(os.path.join(r_, f)), "bytes")
finally:
    shutil.rmtree(SB, ignore_errors=True)
