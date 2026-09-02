"""F probe: with ANTHROPIC_API_KEY SET but no resolvable home, does the explicit env key survive?
Then: what does pre_compact do in that state (it exits 0 -- but does extraction/PROGRESS.md happen)?"""
import os, sys, json, shutil, tempfile, subprocess, textwrap
SB = tempfile.mkdtemp(prefix="F-authenv-")
for k in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[k] = SB
tempfile.tempdir = None
HOOKS = "/home/user/cc-memory/cc_memory/hooks"
try:
    proj = os.path.join(SB, "proj"); os.makedirs(proj); tmpd = os.path.join(SB, "tmp"); os.makedirs(tmpd)
    tr = os.path.join(SB, "t.jsonl")
    with open(tr, "w", encoding="utf-8") as fh:
        for i in range(6):
            fh.write(json.dumps({"type": "user", "cwd": proj, "message": {"role": "user", "content": f"turn {i}: please fix the parser bug in cli.py and use sqlite for storage"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "cwd": proj, "message": {"role": "assistant", "content": [{"type": "text", "text": f"Fixed cli.py line {i}; decided on sqlite; result: tests pass"}]}}) + "\n")
    env = dict(os.environ); env["TMPDIR"] = tmpd; env["ANTHROPIC_API_KEY"] = "sk-ant-api03-PROBE-NOT-A-REAL-KEY"
    # seed the project as root first (control), then hand it to the passwd-less uid
    subprocess.run([sys.executable, f"{HOOKS}/user_prompt.py"], input=json.dumps({"session_id": "s1", "cwd": proj, "prompt": "hi"}), capture_output=True, env=env, cwd=proj, encoding="utf-8")
    for root_, dirs, files in os.walk(SB):
        os.chmod(root_, 0o777)
        for f in files: os.chmod(os.path.join(root_, f), 0o666)
    nohome = {k: v for k, v in env.items() if k not in ("HOME", "USERPROFILE")}
    def become(): os.setgid(54321); os.setuid(54321)
    print("== get_api_key() with ANTHROPIC_API_KEY set, HOME unset, uid without passwd entry")
    child = textwrap.dedent("""
        import os, sys; os.setgid(54321); os.setuid(54321)
        sys.path.insert(0, "/home/user/cc-memory/cc_memory")
        from core.auth import get_api_key, get_api_candidates
        print("ANTHROPIC_API_KEY in env:", bool(os.environ.get("ANTHROPIC_API_KEY")))
        for fn in (get_api_candidates, get_api_key):
            try: print(fn.__name__, "->", fn())
            except Exception as e: print(fn.__name__, "RAISED", type(e).__name__, "-", e)
        from core.logger import _log_dir; print("core.logger._log_dir() (the sibling that was fixed) ->", _log_dir())
    """)
    r = subprocess.run([sys.executable, "-c", child], capture_output=True, encoding="utf-8", errors="replace", env=nohome)
    print(textwrap.indent(r.stdout + r.stderr, "    "))
    print("== pre_compact in that state (no network is ever attempted: the raise happens before any candidate is returned)")
    for label, e in (("control: HOME set", env), ("HOME unset, passwd-less uid", nohome)):
        for f in (".ccm/PROGRESS.md", ".ccm/.last_save.json"):
            fp = os.path.join(proj, f)
            if os.path.exists(fp): os.remove(fp)
        r = subprocess.run([sys.executable, f"{HOOKS}/pre_compact.py"], input=json.dumps({"session_id": "s1", "cwd": proj, "transcript_path": tr, "trigger": "manual"}),
                           capture_output=True, encoding="utf-8", errors="replace", env=e, cwd=proj, timeout=180, preexec_fn=(become if e is nohome else None))
        ls = os.path.join(proj, ".ccm", ".last_save.json")
        info = json.load(open(ls)) if os.path.exists(ls) else None
        print(f"  [{label}] rc={r.returncode} stderr={r.stderr.strip()[:100]!r} stdout={r.stdout.strip()[:120]!r}")
        print(f"      PROGRESS.md exists={os.path.exists(os.path.join(proj, '.ccm', 'PROGRESS.md'))}  .last_save.json={ {k: info[k] for k in info if k in ('method','memories_saved','count','extracted','trigger','error','reason')} if info else None}")
finally:
    shutil.rmtree(SB, ignore_errors=True)
