"""Simulate what Installer._open_dashboard does inside the PyInstaller onefile
exe: sys.executable IS the installer binary, so the spawned command re-enters
installer.main() with the dashboard path as its argv.  A stand-in 'exe' that
re-executes installer.py reproduces the argv handling exactly."""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path
SB = Path(tempfile.mkdtemp(prefix="E_frozen_"))
env = dict(os.environ)
for k in ("HOME", "USERPROFILE"): env[k] = str(SB)
(SB / "tmp").mkdir()
for k in ("TMPDIR", "TEMP", "TMP"): env[k] = str(SB / "tmp")
try:
    inst = "/home/user/cc-memory/cc_memory/ui/installer.py"
    fake_exe = SB / "cc-memory-installer.exe"     # what sys.executable is when frozen
    fake_exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{inst}" "$@"\n'); fake_exe.chmod(0o755)
    target_dir = SB / ".claude" / "hooks" / "cc-memory"
    (target_dir / "ui").mkdir(parents=True)
    dashboard_path = target_dir / "ui" / "dashboard.py"
    dashboard_path.write_text("print('dashboard would start')\n")
    # verbatim shape of installer.py Installer._open_dashboard:
    #     cmd = [sys.executable, str(dashboard_path)]; if project: cmd += ["--project", project]
    cmd = [str(fake_exe), str(dashboard_path), "--project", str(SB / "proj")]
    r = subprocess.run(cmd, env=env, capture_output=True, encoding="utf-8", timeout=60)
    print("rc =", r.returncode)
    print("stdout first lines:", r.stdout.splitlines()[:2])
    print("dashboard started:", "dashboard would start" in r.stdout)
finally:
    shutil.rmtree(SB, ignore_errors=True)
    print("sandbox removed:", not SB.exists())
