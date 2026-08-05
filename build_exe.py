#!/usr/bin/env python3
"""
Build standalone exe files for cc-memory plugin.

Produces:
  dist/cc-memory-installer.exe   (one-click install on any machine, CONSOLE app)
  dist/cc-memory-dashboard.exe   (visual memory management, windowed)

Requirements: pip install pyinstaller

Packaging: bundles three payloads.
  cc_memory_files/<subdir>/   the python package (mirrors ui/installer.py
                              SUBPACKAGE_FILES), laid out FLAT under
                              ~/.claude/hooks/cc-memory/ by the installer.
  cc_memory_surfaces/<rel>    commands/ agents/ skills/ — the user-facing Claude
                              Code surfaces, installed into ~/.claude/. They are
                              NOT part of the package, so without this bundle a
                              standalone install ships no /cc-mem, no /ccm-load
                              and no plan-refiner subagent (PLAN.md then can
                              never be populated).
  cc_memory_meta/hooks.json   the hook-timeout source of truth, read by
                              installer._declared_hook_timeouts().

The installer is built WITHOUT --windowed: its --cli branch is the documented
headless path, and a GUI-subsystem binary prints nothing, returns no exit code
to the shell, and turns any unhandled exception into a modal dialog that hangs
forever with no console to show it. Tk still opens fine from a console app.
"""
import re
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
    "core":  ["__init__.py", "atomic.py", "auth.py", "consolidate.py", "db.py",
              "encoding_setup.py", "extractor.py", "idle.py", "logger.py",
              "modes.py", "plan.py", "privacy.py", "progress.py",
              "version.py"],
    "hooks": ["__init__.py", "consolidate_async.py", "post_tool_use.py",
              "pre_compact.py", "session_start.py", "stop.py", "user_prompt.py"],
    "llm":   ["__init__.py", "ccl_backend.py", "memory_writer.py"],
    "cli":   ["__init__.py", "mem.py", "plan.py"],
    "mcp":   ["__init__.py", "server.py"],
    "ui":    ["__init__.py", "dashboard.py", "installer.py", "web_viewer.py"],
}

# User-facing surfaces. Mirrors ui/installer.py SURFACE_FILES.
SURFACE_FILES = [
    "commands/cc-mem.md",
    "agents/plan-refiner.md",
    "agents/plan-guardian.md",
    "skills/ccm-load/SKILL.md",
    "skills/save-memories/SKILL.md",
]

# Non-package data the installer reads at runtime.
META_FILES = ["hooks/hooks.json"]


def _version():
    """Read the canonical version. Never a literal in this file."""
    for probe in (SRC / "core" / "version.py", SRC / "__init__.py"):
        try:
            txt = probe.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', txt, re.M)
        if m:
            return m.group(1)
    return "unknown"


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


def _surface_file_list():
    """Yield (src_path, dest_subdir_in_bundle) for every shipped surface."""
    pairs = []
    for rel in SURFACE_FILES:
        sub = Path(rel).parent.as_posix()
        pairs.append((ROOT / rel, f"cc_memory_surfaces/{sub}"))
    return pairs


def _meta_file_list():
    return [(ROOT / rel, "cc_memory_meta") for rel in META_FILES]


def _check_files(pairs):
    missing = [str(p) for p, _ in pairs if not p.exists()]
    if missing:
        print("ERROR: Missing files:")
        for m in missing:
            print(f"  {m}")
        sys.exit(1)


def _data_args(pairs):
    # PyInstaller --add-data syntax: "src;dest" on Windows, "src:dest" on Unix
    sep = ";" if sys.platform == "win32" else ":"
    args = []
    for src_path, dest in pairs:
        args.extend(["--add-data", f"{src_path}{sep}{dest}"])
    return args


def _run(cmd, exe_name):
    print(f"Running PyInstaller with {cmd.count('--add-data')} bundled files...")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("\n[FAIL] Build failed")
        sys.exit(1)
    exe_path = DIST / exe_name
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / 1024 / 1024
        print(f"\n[OK] Built: {exe_path} ({size_mb:.1f} MB)")


def build_installer():
    print("=" * 50)
    print(f"  Building cc-memory-installer.exe (v{_version()})")
    print("=" * 50)

    pairs = _flat_file_list() + _surface_file_list() + _meta_file_list()
    _check_files(pairs)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        # Console, NOT --windowed: the installer's --cli branch prints 20+ lines
        # and is the documented headless path. Tk is created on demand in main().
        "--console",
        "--name", "cc-memory-installer",
        "--icon", "NONE",
        "--hidden-import", "sqlite3",
        "--hidden-import", "_sqlite3",
        "--collect-all", "sqlite3",
        *_data_args(pairs),
        str(SRC / "ui" / "installer.py"),
    ]
    _run(cmd, "cc-memory-installer.exe")


def build_dashboard():
    print("=" * 50)
    print(f"  Building cc-memory-dashboard.exe (v{_version()})")
    print("=" * 50)

    # Dashboard needs everything the installer's package bundle has (it imports
    # core + llm), but none of the surfaces/meta — it never installs anything.
    pairs = _flat_file_list()
    _check_files(pairs)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",          # pure GUI: correct here, wrong for the installer
        "--name", "cc-memory-dashboard",
        "--icon", "NONE",
        "--hidden-import", "sqlite3",
        "--hidden-import", "_sqlite3",
        "--collect-all", "sqlite3",
        *_data_args(pairs),
        str(SRC / "ui" / "dashboard.py"),
    ]
    _run(cmd, "cc-memory-dashboard.exe")


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
