"""F probe: drive all six hooks (a) normally and (b) as a uid with NO passwd entry and NO HOME.
Hook contract: rc 0 and EMPTY stderr, always."""
import os, sys, json, shutil, tempfile, subprocess, sqlite3
SB = tempfile.mkdtemp(prefix="F-nohome-")
for k in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[k] = SB
tempfile.tempdir = None
HOOKS = "/home/user/cc-memory/cc_memory/hooks"
try:
    proj = os.path.join(SB, "proj"); os.makedirs(proj)
    tmpd = os.path.join(SB, "tmp"); os.makedirs(tmpd)
    tr = os.path.join(SB, "t.jsonl")
    with open(tr, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "cwd": proj, "message": {"role": "user", "content": "hello please fix the bug in cli.py"}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "cwd": proj, "message": {"role": "assistant", "content": [{"type": "text", "text": "Done: fixed cli.py, decided to use sqlite"}]}}) + "\n")
    sid = "sess-F-0001"
    payloads = {
        "user_prompt.py":       {"session_id": sid, "cwd": proj, "prompt": "please refactor the parser"},
        "post_tool_use.py":     {"session_id": sid, "cwd": proj, "tool_name": "Edit", "tool_input": {"file_path": proj + "/a.py"}, "tool_response": {"ok": True}},
        "stop.py":              {"session_id": sid, "cwd": proj, "transcript_path": tr},
        "session_start.py":     {"session_id": sid, "cwd": proj, "source": "startup", "transcript_path": tr},
        "pre_compact.py":       {"session_id": sid, "cwd": proj, "transcript_path": tr, "trigger": "manual"},
        "consolidate_async.py": {"session_id": sid, "cwd": proj, "transcript_path": tr, "trigger": "manual"},
    }
    order = ["user_prompt.py", "post_tool_use.py", "stop.py", "session_start.py", "pre_compact.py", "consolidate_async.py"]
    def drive(label, env, pre=None):
        print(f"== {label}")
        bad = []
        for h in order:
            r = subprocess.run([sys.executable, os.path.join(HOOKS, h)], input=json.dumps(payloads[h]), capture_output=True,
                               encoding="utf-8", errors="replace", env=env, cwd=proj, timeout=120, preexec_fn=pre)
            ok = r.returncode == 0 and r.stderr == ""
            print(f"  {h:<22} rc={r.returncode} stderr={r.stderr.strip()[:160]!r}" + ("" if ok else "   <-- CONTRACT VIOLATION"))
            if not ok: bad.append(h)
        return bad
    base_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    base_env["TMPDIR"] = tmpd
    b1 = drive("as root, HOME set (control)", base_env)
    print("  db exists:", os.path.exists(os.path.join(proj, ".ccm", "memory.db")))
    for root_, dirs, files in os.walk(SB):
        os.chmod(root_, 0o777)
        for f in files: os.chmod(os.path.join(root_, f), 0o666)
    nohome = {k: v for k, v in base_env.items() if k not in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")}
    def become(): os.setgid(54321); os.setuid(54321)
    b2 = drive("as uid 54321 (no passwd entry), HOME unset", nohome, pre=become)
    print("\ncontrol violations:", b1 or "none"); print("no-home violations:", b2 or "none")
finally:
    shutil.rmtree(SB, ignore_errors=True)
