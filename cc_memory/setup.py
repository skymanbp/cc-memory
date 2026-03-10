#!/usr/bin/env python3
"""
cc-memory/setup.py  --  One-time setup script
Run:  python setup.py

What it does:
  1. Verifies all plugin files exist
  2. Detects Python path for hook commands
  3. Merges hooks into ~/.claude/settings.json (non-destructive)
  4. Optionally initializes memory/ in a specified project directory
"""
import json
import sys
import shutil
from pathlib import Path


PLUGIN_DIR = Path(__file__).parent
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# The hooks config to merge into settings.json
def make_hooks_config(python_cmd: str) -> dict:
    pre_compact_cmd = f'{python_cmd} "{PLUGIN_DIR / "pre_compact.py"}"'
    session_start_cmd = f'{python_cmd} "{PLUGIN_DIR / "session_start.py"}"'

    return {
        "PreCompact": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": pre_compact_cmd,
                        "timeout": 30,
                    }
                ]
            }
        ],
        "SessionStart": [
            {
                "matcher": "compact",
                "hooks": [
                    {
                        "type": "command",
                        "command": session_start_cmd,
                        "timeout": 10,
                    }
                ]
            }
        ]
    }


def check_files():
    """Verify all required plugin files exist."""
    required = ["db.py", "extractor.py", "pre_compact.py", "session_start.py", "mem.py"]
    missing = [f for f in required if not (PLUGIN_DIR / f).exists()]
    if missing:
        print(f"ERROR: Missing files: {', '.join(missing)}")
        print(f"Plugin directory: {PLUGIN_DIR}")
        sys.exit(1)
    print(f"[OK] All plugin files present in {PLUGIN_DIR}")


def detect_python() -> str:
    """Find the Python command to use."""
    python_path = shutil.which("python3") or shutil.which("python")
    if not python_path:
        print("WARNING: Could not find python3 or python in PATH")
        return "python"
    print(f"[OK] Python found: {python_path}")
    return "python3" if "python3" in python_path else "python"


def merge_hooks(python_cmd: str):
    """Merge hook config into settings.json without overwriting existing settings."""
    hooks_config = make_hooks_config(python_cmd)

    # Read existing settings
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            settings = json.load(f)
        print(f"[OK] Read existing settings from {SETTINGS_PATH}")
    else:
        settings = {}
        print(f"[  ] Creating new settings file at {SETTINGS_PATH}")

    # Merge hooks (don't overwrite other hook events the user may have)
    if "hooks" not in settings:
        settings["hooks"] = {}

    for event, hook_list in hooks_config.items():
        if event in settings["hooks"]:
            # Check if cc-memory hooks already exist
            existing_cmds = []
            for matcher_group in settings["hooks"][event]:
                for h in matcher_group.get("hooks", []):
                    existing_cmds.append(h.get("command", ""))

            if any("cc-memory" in cmd for cmd in existing_cmds):
                print(f"[OK] {event} hook already configured, skipping")
                continue
            else:
                # Append our hooks to existing event hooks
                settings["hooks"][event].extend(hook_list)
                print(f"[OK] Appended cc-memory hook to existing {event} hooks")
        else:
            settings["hooks"][event] = hook_list
            print(f"[OK] Added {event} hook")

    # Write back
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    print(f"[OK] Settings saved to {SETTINGS_PATH}")


def init_project(project_path: str):
    """Initialize memory directory for a project."""
    project = Path(project_path).resolve()
    if not project.exists():
        print(f"ERROR: Project path does not exist: {project}")
        return

    memory_dir = project / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "sessions").mkdir(exist_ok=True)
    (memory_dir / "topics").mkdir(exist_ok=True)

    # Initialize empty DB
    sys.path.insert(0, str(PLUGIN_DIR))
    from db import MemoryDB
    db = MemoryDB(memory_dir / "memory.db")
    db.upsert_project(str(project))
    print(f"[OK] Initialized memory for {project.name}")
    print(f"     Directory: {memory_dir}")
    print(f"     Database:  {memory_dir / 'memory.db'}")

    # Create .gitignore for memory directory
    gitignore = memory_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# cc-memory: exclude database and session archives from git\n"
            "# Keep MEMORY.md and SESSION_HANDOFF.md tracked\n"
            "memory.db\n"
            "memory.db-wal\n"
            "memory.db-shm\n"
            "sessions/\n",
            encoding="utf-8"
        )
        print(f"[OK] Created {gitignore}")


def print_usage():
    print("""
cc-memory setup complete!

How it works:
  1. PreCompact hook fires before context compaction
     -> Reads full conversation transcript
     -> Extracts decisions, results, configs, bugs, tasks
     -> Saves to <project>/memory/memory.db (SQLite)
     -> Writes SESSION_HANDOFF.md + session archive

  2. SessionStart hook fires after compaction
     -> Reads saved memory from SQLite
     -> Injects context summary into Claude's new window
     -> Claude continues with full awareness of prior work

CLI tool for querying memory:
  python {plugin}/mem.py --project <path> stats
  python {plugin}/mem.py --project <path> list decisions
  python {plugin}/mem.py --project <path> search "keyword"
  python {plugin}/mem.py --project <path> sql "SELECT * FROM memories"
  python {plugin}/mem.py --project <path> schema

To initialize memory for a new project:
  python {plugin}/setup.py --init <project-path>
""".format(plugin=PLUGIN_DIR))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="cc-memory setup")
    parser.add_argument("--init", metavar="PROJECT_PATH",
                        help="Initialize memory for a project")
    parser.add_argument("--python", default=None,
                        help="Override Python command (e.g. python3)")
    args = parser.parse_args()

    print("=" * 50)
    print("  cc-memory — Claude Code Memory Plugin Setup")
    print("=" * 50)
    print()

    check_files()
    python_cmd = args.python or detect_python()
    merge_hooks(python_cmd)

    if args.init:
        print()
        init_project(args.init)

    print()
    print_usage()


if __name__ == "__main__":
    main()
