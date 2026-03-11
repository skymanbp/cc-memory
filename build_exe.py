#!/usr/bin/env python3
"""
build_exe.py — Build standalone exe files for cc-memory plugin.

Produces:
  dist/cc-memory-installer.exe   (one-click install on any machine)
  dist/cc-memory-dashboard.exe   (visual memory management)

Requirements: pip install pyinstaller
"""
import subprocess, sys, shutil
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "cc_memory"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

def build_installer():
    """Build the self-contained installer exe."""
    print("=" * 50)
    print("  Building cc-memory-installer.exe")
    print("=" * 50)

    # Plugin files to bundle as data
    plugin_files = [
        "auth.py", "db.py", "extractor.py", "pre_compact.py", "session_start.py",
        "stop.py", "mem.py", "plan.py", "dashboard.py", "installer.py",
        "setup.py", "config.json", "skill_template.md"
    ]

    # Verify all files exist
    for f in plugin_files:
        if not (SRC / f).exists():
            print(f"ERROR: Missing {SRC / f}")
            sys.exit(1)

    # Build data args: --add-data "src;dest_folder_in_bundle"
    data_args = []
    for f in plugin_files:
        src_path = str(SRC / f)
        data_args.extend(["--add-data", f"{src_path};cc_memory_files"])

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "cc-memory-installer",
        "--icon", "NONE",
        *data_args,
        str(SRC / "installer_standalone.py"),
    ]

    print(f"Running: {' '.join(cmd[:6])} ...")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode == 0:
        exe_path = DIST / "cc-memory-installer.exe"
        size_mb = exe_path.stat().st_size / 1024 / 1024
        print(f"\n[OK] Built: {exe_path} ({size_mb:.1f} MB)")
    else:
        print("\n[FAIL] Build failed")
        sys.exit(1)


def build_dashboard():
    """Build the dashboard exe."""
    print("=" * 50)
    print("  Building cc-memory-dashboard.exe")
    print("=" * 50)

    # Dashboard needs db.py, auth.py, config.json, plan.py, extractor.py
    data_args = [
        "--add-data", f"{SRC / 'auth.py'};cc_memory_files",
        "--add-data", f"{SRC / 'db.py'};cc_memory_files",
        "--add-data", f"{SRC / 'config.json'};cc_memory_files",
        "--add-data", f"{SRC / 'plan.py'};cc_memory_files",
        "--add-data", f"{SRC / 'extractor.py'};cc_memory_files",
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "cc-memory-dashboard",
        "--icon", "NONE",
        *data_args,
        str(SRC / "dashboard.py"),
    ]

    print(f"Running: {' '.join(cmd[:6])} ...")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode == 0:
        exe_path = DIST / "cc-memory-dashboard.exe"
        size_mb = exe_path.stat().st_size / 1024 / 1024
        print(f"\n[OK] Built: {exe_path} ({size_mb:.1f} MB)")
    else:
        print("\n[FAIL] Build failed")
        sys.exit(1)


if __name__ == "__main__":
    # Clean previous builds
    for d in [BUILD, DIST]:
        if d.exists():
            shutil.rmtree(d)

    build_installer()
    print()
    build_dashboard()

    print("\n" + "=" * 50)
    print("  Build complete!")
    print("=" * 50)
    print(f"\n  Installer: {DIST / 'cc-memory-installer.exe'}")
    print(f"  Dashboard: {DIST / 'cc-memory-dashboard.exe'}")
    print("\nCopy these to any machine with Claude Code installed.")
