#!/usr/bin/env python3
"""
Build standalone exe files for cc-memory plugin.

Produces:
  dist/cc-memory-installer.exe   (one-click install on any machine)
  dist/cc-memory-dashboard.exe   (visual memory management)

Requirements: pip install pyinstaller

v2.1 changes: bundles the subpackage layout (cc_memory/{core,hooks,llm,cli,mcp,ui}/)
into cc_memory_files/ so the installer can mirror it under ~/.claude/hooks/cc-memory/.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "cc_memory"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


# Subpackage files to bundle. Mirrors ui/installer.py SUBPACKAGE_FILES.
SUBPACKAGE_FILES = {
    "":      ["__init__.py", "config.json"],
    "core":  ["__init__.py", "auth.py", "consolidate.py", "db.py",
              "encoding_setup.py", "extractor.py", "idle.py", "logger.py",
              "modes.py", "plan.py", "privacy.py", "progress.py"],
    "hooks": ["__init__.py", "post_tool_use.py", "pre_compact.py",
              "session_start.py", "stop.py", "user_prompt.py"],
    "llm":   ["__init__.py", "ccl_backend.py", "memory_writer.py"],
    "cli":   ["__init__.py", "mem.py", "plan.py"],
    "mcp":   ["__init__.py", "server.py"],
    "ui":    ["__init__.py", "dashboard.py", "installer.py", "web_viewer.py"],
}


def _flat_file_list():
    """Yield (src_path, dest_subdir_in_bundle) for every plugin file."""
    pairs = []
    for subdir, files in SUBPACKAGE_FILES.items():
        for f in files:
            if subdir:
                pairs.append((SRC / subdir / f, f"cc_memory_files/{subdir}"))
            else:
                pairs.append((SRC / f, "cc_memory_files"))
    return pairs


def _check_files():
    missing = [str(p) for p, _ in _flat_file_list() if not p.exists()]
    if missing:
        print("ERROR: Missing files:")
        for m in missing:
            print(f"  {m}")
        sys.exit(1)


def build_installer():
    print("=" * 50)
    print("  Building cc-memory-installer.exe (v2.1)")
    print("=" * 50)

    _check_files()

    data_args = []
    for src_path, dest in _flat_file_list():
        # PyInstaller --add-data syntax: "src;dest" on Windows, "src:dest" on Unix
        sep = ";" if sys.platform == "win32" else ":"
        data_args.extend(["--add-data", f"{src_path}{sep}{dest}"])

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "cc-memory-installer",
        "--icon", "NONE",
        "--hidden-import", "sqlite3",
        "--hidden-import", "_sqlite3",
        "--collect-all", "sqlite3",
        *data_args,
        str(SRC / "ui" / "installer.py"),
    ]

    print(f"Running PyInstaller with {len(data_args)//2} bundled files...")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode == 0:
        exe_path = DIST / "cc-memory-installer.exe"
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / 1024 / 1024
            print(f"\n[OK] Built: {exe_path} ({size_mb:.1f} MB)")
    else:
        print("\n[FAIL] Build failed")
        sys.exit(1)


def build_dashboard():
    print("=" * 50)
    print("  Building cc-memory-dashboard.exe (v2.1)")
    print("=" * 50)

    _check_files()

    # Dashboard needs everything the installer does (it imports core + llm).
    data_args = []
    for src_path, dest in _flat_file_list():
        sep = ";" if sys.platform == "win32" else ":"
        data_args.extend(["--add-data", f"{src_path}{sep}{dest}"])

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "cc-memory-dashboard",
        "--icon", "NONE",
        "--hidden-import", "sqlite3",
        "--hidden-import", "_sqlite3",
        "--collect-all", "sqlite3",
        *data_args,
        str(SRC / "ui" / "dashboard.py"),
    ]

    print(f"Running PyInstaller with {len(data_args)//2} bundled files...")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode == 0:
        exe_path = DIST / "cc-memory-dashboard.exe"
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / 1024 / 1024
            print(f"\n[OK] Built: {exe_path} ({size_mb:.1f} MB)")
    else:
        print("\n[FAIL] Build failed")
        sys.exit(1)


if __name__ == "__main__":
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
