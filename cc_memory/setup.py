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
    import platform
    win_mult = 1.5 if platform.system() == "Windows" else 1.0

    def _hook(script, base_timeout):
        return {
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": f'{python_cmd} "{PLUGIN_DIR / script}"',
                "timeout": int(base_timeout * win_mult),
            }]
        }

    return {
        "PreCompact":       [_hook("pre_compact.py", 30)],
        "SessionStart":     [_hook("session_start.py", 10)],
        "Stop":             [_hook("stop.py", 15)],  # observer LLM call needs time
        "PostToolUse":      [_hook("post_tool_use.py", 5)],
        "UserPromptSubmit": [_hook("user_prompt.py", 5)],
    }


def check_files():
    """Verify all required plugin files exist."""
    required = ["auth.py", "db.py", "extractor.py", "pre_compact.py", "session_start.py",
                 "stop.py", "mem.py", "skill_template.md",
                 "post_tool_use.py", "user_prompt.py", "privacy.py", "logger.py", "modes.py",
                 "mcp_server.py", "web_viewer.py"]
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


def detect_project_type(project: Path) -> dict:
    """Auto-detect project structure and characteristics."""
    info = {"type": "unknown", "language": None, "framework": None,
            "has_claude_md": False, "has_skills": False, "files": []}

    # Detect CLAUDE.md
    if (project / "CLAUDE.md").exists():
        info["has_claude_md"] = True
        info["files"].append("CLAUDE.md")

    # Detect .claude/skills/
    skills_dir = project / ".claude" / "skills"
    if skills_dir.exists():
        info["has_skills"] = True
        info["files"].extend([f".claude/skills/{f.name}" for f in skills_dir.iterdir()
                              if f.is_file()])

    # Detect language/framework
    markers = {
        "pyproject.toml": ("python", None),
        "setup.py": ("python", None),
        "requirements.txt": ("python", None),
        "package.json": ("javascript", "node"),
        "tsconfig.json": ("typescript", "node"),
        "Cargo.toml": ("rust", "cargo"),
        "go.mod": ("go", None),
        "pom.xml": ("java", "maven"),
        "build.gradle": ("java", "gradle"),
    }
    for marker, (lang, fw) in markers.items():
        if (project / marker).exists():
            info["language"] = lang
            if fw:
                info["framework"] = fw
            info["files"].append(marker)
            break

    # Detect project type
    if (project / ".ipynb_checkpoints").exists() or list(project.rglob("*.ipynb")):
        info["type"] = "notebook/research"
    elif (project / "src").exists():
        info["type"] = "application"
    elif (project / "lib").exists() or (project / "pkg").exists():
        info["type"] = "library"
    elif info["language"]:
        info["type"] = f"{info['language']} project"

    # Count files
    try:
        py_count = len(list(project.rglob("*.py")))
        all_count = sum(1 for _ in project.rglob("*") if _.is_file())
        info["py_files"] = py_count
        info["total_files"] = min(all_count, 99999)  # cap for huge repos
    except (PermissionError, OSError):
        info["py_files"] = 0
        info["total_files"] = 0

    return info


def init_project(project_path: str):
    """Initialize memory directory for a project with auto-detection."""
    project = Path(project_path).resolve()
    if not project.exists():
        print(f"ERROR: Project path does not exist: {project}")
        return

    memory_dir = project / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "sessions").mkdir(exist_ok=True)
    (memory_dir / "topics").mkdir(exist_ok=True)

    # Initialize DB
    sys.path.insert(0, str(PLUGIN_DIR))
    from db import MemoryDB
    db = MemoryDB(memory_dir / "memory.db")
    pid = db.upsert_project(str(project))
    print(f"[OK] Initialized memory for {project.name}")
    print(f"     Directory: {memory_dir}")
    print(f"     Database:  {memory_dir / 'memory.db'}")

    # Auto-detect project structure
    print("\n[..] Scanning project structure...")
    info = detect_project_type(project)
    print(f"[OK] Project type: {info['type']}")
    if info["language"]:
        print(f"     Language:    {info['language']}")
    if info["framework"]:
        print(f"     Framework:   {info['framework']}")
    if info["has_claude_md"]:
        print(f"     CLAUDE.md:   found")
    if info["has_skills"]:
        print(f"     Skills:      {len([f for f in info['files'] if 'skills' in f])} found")
    print(f"     Files:       {info['total_files']} total, {info['py_files']} .py")

    # Seed initial memories from detected info
    if info["type"] != "unknown":
        db.insert_memory(pid, None, "arch",
                         f"Project type: {info['type']}, language: {info['language'] or 'mixed'}",
                         importance=3, tags=["auto-detected", "init"])
    if info["has_claude_md"]:
        db.insert_memory(pid, None, "config",
                         "CLAUDE.md exists — Claude Code project instructions are configured",
                         importance=3, tags=["auto-detected", "init"])
    if info["has_skills"]:
        skill_names = [f.split("/")[-1] for f in info["files"] if "skills" in f]
        db.insert_memory(pid, None, "config",
                         f"Skills configured: {', '.join(skill_names)}",
                         importance=2, tags=["auto-detected", "init"])

    # Seed project-specific keywords from file names
    keywords = {}
    for f in info["files"]:
        name = Path(f).stem
        if len(name) > 2:
            keywords[name] = 1
    if keywords:
        db.upsert_keywords(pid, keywords)
        print(f"[OK] Seeded {len(keywords)} keywords from project files")

    # Create .gitignore
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

    # Deploy /save-memories skill
    skill_dir = project / ".claude" / "skills" / "save-memories"
    skill_dst = skill_dir / "skill.md"
    skill_src = PLUGIN_DIR / "skill_template.md"
    if not skill_dst.exists() and skill_src.exists():
        skill_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(skill_src), str(skill_dst))
        print(f"[OK] Deployed /save-memories skill to {skill_dir}")

    # Deploy /mem-status skill
    status_dir = project / ".claude" / "skills" / "mem-status"
    status_dst = status_dir / "skill.md"
    status_src = PLUGIN_DIR / "skill_status.md"
    if not status_dst.exists() and status_src.exists():
        status_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(status_src), str(status_dst))
        print(f"[OK] Deployed /mem-status skill to {status_dir}")


def print_usage():
    print("""
cc-memory v2.0 setup complete!

How it works:
  1. PostToolUse  — captures every tool call as observation (real-time)
  2. PreCompact   — extracts memories via LLM before compaction
  3. SessionStart — injects context with progressive disclosure
  4. Stop         — reminds to /save-memories after 8+ turns
  5. UserPromptSubmit — tracks conversation turns

  All hooks log to ~/.claude/hooks/cc-memory/logs/ (never stderr).

CLI:
  python {plugin}/mem.py --project <path> stats
  python {plugin}/mem.py --project <path> search "keyword"
  python {plugin}/mem.py --project <path> observations
  python {plugin}/mem.py --project <path> topics

Web dashboard:
  python {plugin}/web_viewer.py --project <path>

MCP server (register in ~/.claude/mcp.json):
  python {plugin}/mcp_server.py

Init new project:
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
