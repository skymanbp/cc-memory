"""F probe: lay the package out FLAT (as ui/installer.py does) and run every entry point from there."""
import os, sys, json, shutil, tempfile, subprocess, importlib.util
SB = tempfile.mkdtemp(prefix="F-flat-")
for k in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[k] = SB
tempfile.tempdir = None
SRC = "/home/user/cc-memory/cc_memory"
try:
    spec = importlib.util.spec_from_file_location("installer_probe", os.path.join(SRC, "ui", "installer.py"))
    inst = importlib.util.module_from_spec(spec); spec.loader.exec_module(inst)
    FLAT = os.path.join(SB, ".claude", "hooks", "cc-memory"); os.makedirs(FLAT)
    n = 0
    for sub, files in inst.SUBPACKAGE_FILES.items():
        d = os.path.join(FLAT, sub) if sub else FLAT; os.makedirs(d, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(SRC, sub, f) if sub else os.path.join(SRC, f), os.path.join(d, f)); n += 1
    print(f"flat copy: {n} files under {FLAT}")
    missing = sorted(set(p.relative_to(SRC).as_posix() for p in __import__('pathlib').Path(SRC).rglob('*.py') if '__pycache__' not in p.parts)
                     - {(f"{s}/{f}" if s else f) for s, fs in inst.SUBPACKAGE_FILES.items() for f in fs})
    print("runtime .py NOT in SUBPACKAGE_FILES:", missing or "none")
    proj = os.path.join(SB, "proj"); os.makedirs(proj)
    tr = os.path.join(SB, "t.jsonl"); open(tr, "w").write(json.dumps({"type": "user", "cwd": proj, "message": {"role": "user", "content": "hi"}}) + "\n")
    sid = "flat-sess"
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    def run(rel, stdin=None, args=()):
        r = subprocess.run([sys.executable, os.path.join(FLAT, rel), *args], input=stdin, capture_output=True,
                           encoding="utf-8", errors="replace", env=env, cwd=proj, timeout=120)
        return r
    print("== hooks from the flat tree (rc must be 0, stderr empty)")
    payload = {"session_id": sid, "cwd": proj, "transcript_path": tr, "trigger": "manual", "source": "startup",
               "prompt": "hello there friend", "tool_name": "Edit", "tool_input": {"file_path": proj + "/x.py"}, "tool_response": {}}
    for h in ["user_prompt.py", "post_tool_use.py", "stop.py", "session_start.py", "pre_compact.py", "consolidate_async.py"]:
        r = run(f"hooks/{h}", json.dumps(payload))
        print(f"  {h:<22} rc={r.returncode} stderr={r.stderr.strip()[:200]!r}")
    print("  db created:", os.path.exists(os.path.join(proj, ".ccm", "memory.db")))
    print("== CLIs / MCP / viewer from the flat tree")
    for rel, args, stdin in [("cli/mem.py", ["--help"], None), ("cli/plan.py", ["--help"], None),
                             ("cli/mem.py", ["--project", proj, "status"], None), ("cli/mem.py", ["--project", proj, "paths"], None),
                             ("ui/web_viewer.py", ["--help"], None),
                             ("mcp/server.py", [], json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")]:
        r = run(rel, stdin, args)
        first = (r.stdout.strip().splitlines() or [""])[0][:100]
        print(f"  {rel:<16} {' '.join(args)[:30]:<30} rc={r.returncode} out={first!r} err={r.stderr.strip()[:120]!r}")
    r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {FLAT!r}); from core.version import __version__; print(__version__); import ui.installer, ui.web_viewer, mcp.server, cli.mem, cli.plan, hooks._entry; print('imports ok')"],
                       capture_output=True, encoding="utf-8", errors="replace", env=env, cwd=proj)
    print("  flat imports:", r.stdout.strip().replace("\n", " | "), r.stderr.strip()[:200])
    r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {FLAT!r}); import ui.dashboard"],
                       capture_output=True, encoding="utf-8", errors="replace", env=env, cwd=proj)
    print("  ui.dashboard import (no tkinter on this box):", r.stderr.strip().splitlines()[-1][:120] if r.stderr else "ok")
finally:
    shutil.rmtree(SB, ignore_errors=True)
