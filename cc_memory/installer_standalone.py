#!/usr/bin/env python3
"""
cc-memory Standalone Installer
================================
Self-contained installer that extracts plugin files and configures Claude Code.
This is the entry point for the PyInstaller-built exe.

When run as exe, bundled files are in sys._MEIPASS/cc_memory_files/
When run as script, files are in the same directory.
"""
import sys
import os
import json
import shutil
from pathlib import Path

# ── Resolve bundled files location ──────────────────────────────────────────

def _get_bundle_dir() -> Path:
    """Get directory where bundled plugin files are stored."""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller exe
        return Path(sys._MEIPASS) / "cc_memory_files"
    else:
        # Running as script — files are in the same directory
        return Path(__file__).parent


BUNDLE_DIR = _get_bundle_dir()
PLUGIN_FILES = [
    "auth.py", "db.py", "extractor.py", "pre_compact.py", "session_start.py",
    "stop.py", "mem.py", "plan.py", "dashboard.py", "installer.py",
    "setup.py", "config.json", "skill_template.md"
]
TARGET_DIR = Path.home() / ".claude" / "hooks" / "cc-memory"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


# ── Tkinter GUI ─────────────────────────────────────────────────────────────

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    print("Error: tkinter not available.")
    print("Run with: python installer_standalone.py --cli")
    sys.exit(1)


class StandaloneInstaller:
    def __init__(self, root):
        self.root = root
        self.root.title("cc-memory Installer")
        self.root.geometry("650x580")
        self.root.resizable(False, False)
        self._build_ui()
        self._pre_check()

    def _build_ui(self):
        # Title
        ttk.Label(self.root, text="cc-memory — Claude Code Memory Plugin",
                  font=("", 14, "bold")).pack(pady=(18, 4))
        ttk.Label(self.root, text="Automatic conversation memory for Claude Code",
                  font=("", 10)).pack()

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=20, pady=12)

        # Step 1: Install plugin
        f1 = ttk.LabelFrame(self.root, text="Step 1: Install Plugin (global, one-time)", padding=10)
        f1.pack(fill=tk.X, padx=20, pady=5)

        self.install_status = tk.StringVar(value="Checking...")
        ttk.Label(f1, textvariable=self.install_status, wraplength=550).pack(anchor=tk.W)

        bf1 = ttk.Frame(f1)
        bf1.pack(fill=tk.X, pady=(5, 0))
        self.install_btn = ttk.Button(bf1, text="Install Plugin", command=self._install)
        self.install_btn.pack(side=tk.RIGHT)

        # Step 2: Configure hooks
        f2 = ttk.LabelFrame(self.root, text="Step 2: Configure Hooks (settings.json)", padding=10)
        f2.pack(fill=tk.X, padx=20, pady=5)

        self.hooks_status = tk.StringVar(value="Checking...")
        ttk.Label(f2, textvariable=self.hooks_status, wraplength=550).pack(anchor=tk.W)

        bf2 = ttk.Frame(f2)
        bf2.pack(fill=tk.X, pady=(5, 0))
        self.hooks_btn = ttk.Button(bf2, text="Configure Hooks", command=self._configure_hooks)
        self.hooks_btn.pack(side=tk.RIGHT)

        # Step 3: Initialize project
        f3 = ttk.LabelFrame(self.root, text="Step 3: Initialize Project (per-project)", padding=10)
        f3.pack(fill=tk.X, padx=20, pady=5)

        pf = ttk.Frame(f3)
        pf.pack(fill=tk.X)
        ttk.Label(pf, text="Project:").pack(side=tk.LEFT)
        self.project_var = tk.StringVar()
        ttk.Entry(pf, textvariable=self.project_var, width=48).pack(side=tk.LEFT, padx=5)
        ttk.Button(pf, text="Browse", command=self._browse).pack(side=tk.LEFT)

        self.project_info = tk.StringVar(value="Select a project directory to initialize memory")
        ttk.Label(f3, textvariable=self.project_info, wraplength=550).pack(anchor=tk.W, pady=5)

        bf3 = ttk.Frame(f3)
        bf3.pack(fill=tk.X)
        self.init_btn = ttk.Button(bf3, text="Initialize Project", command=self._init_project)
        self.init_btn.pack(side=tk.RIGHT)

        # Log
        ttk.Label(self.root, text="Log:").pack(anchor=tk.W, padx=20, pady=(8, 0))
        self.log_text = tk.Text(self.root, height=8, font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 8))

        # Bottom
        bf = ttk.Frame(self.root)
        bf.pack(fill=tk.X, padx=20, pady=8)
        ttk.Button(bf, text="Open Dashboard", command=self._open_dashboard).pack(side=tk.LEFT)
        ttk.Button(bf, text="Close", command=self.root.quit).pack(side=tk.RIGHT)

    def _log(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _pre_check(self):
        # Check plugin files
        if TARGET_DIR.exists() and (TARGET_DIR / "db.py").exists():
            self.install_status.set(f"Plugin already installed at {TARGET_DIR}")
            self.install_btn.configure(text="Reinstall")
            self._log(f"[OK] Plugin found at {TARGET_DIR}")
        else:
            self.install_status.set("Plugin not yet installed")
            self._log("[  ] Plugin not found — click 'Install Plugin'")

        # Check hooks
        if SETTINGS_PATH.exists():
            try:
                settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                hooks = settings.get("hooks", {})
                has_hook = any(
                    "cc-memory" in h.get("command", "")
                    for mg in hooks.get("PreCompact", [])
                    for h in mg.get("hooks", [])
                )
                if has_hook:
                    self.hooks_status.set("Hooks already configured")
                    self.hooks_btn.configure(text="Reconfigure")
                    self._log("[OK] Hooks found in settings.json")
                else:
                    self.hooks_status.set("Hooks not yet configured")
                    self._log("[  ] Hooks not found — click 'Configure Hooks'")
            except Exception as e:
                self.hooks_status.set(f"Error reading settings: {e}")
        else:
            self.hooks_status.set("No settings.json — will create on configure")

    def _install(self):
        """Copy plugin files to ~/.claude/hooks/cc-memory/"""
        try:
            TARGET_DIR.mkdir(parents=True, exist_ok=True)
            copied = 0
            for fname in PLUGIN_FILES:
                src = BUNDLE_DIR / fname
                dst = TARGET_DIR / fname
                if src.exists():
                    shutil.copy2(str(src), str(dst))
                    copied += 1
                    self._log(f"  Copied: {fname}")
                else:
                    self._log(f"  SKIP (not found): {fname}")

            self.install_status.set(f"Plugin installed: {copied} files → {TARGET_DIR}")
            self.install_btn.configure(text="Reinstalled")
            self._log(f"[OK] {copied} files installed to {TARGET_DIR}")
            messagebox.showinfo("Success", f"Plugin installed!\n{TARGET_DIR}")
        except Exception as e:
            self._log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))

    def _configure_hooks(self):
        """Merge hook config into settings.json."""
        try:
            # Detect python command
            python_cmd = "python3" if shutil.which("python3") else "python"

            pre_compact_cmd = f'{python_cmd} "{TARGET_DIR / "pre_compact.py"}"'
            session_start_cmd = f'{python_cmd} "{TARGET_DIR / "session_start.py"}"'
            stop_cmd = f'{python_cmd} "{TARGET_DIR / "stop.py"}"'

            hooks_config = {
                "PreCompact": [{
                    "matcher": "",
                    "hooks": [{"type": "command", "command": pre_compact_cmd, "timeout": 30}]
                }],
                "SessionStart": [{
                    "matcher": "",
                    "hooks": [{"type": "command", "command": session_start_cmd, "timeout": 10}]
                }],
                "Stop": [{
                    "matcher": "",
                    "hooks": [{"type": "command", "command": stop_cmd, "timeout": 5}]
                }]
            }

            # Read or create settings
            if SETTINGS_PATH.exists():
                settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            else:
                SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
                settings = {}

            if "hooks" not in settings:
                settings["hooks"] = {}

            for event, hook_list in hooks_config.items():
                existing = settings["hooks"].get(event, [])
                existing_cmds = [
                    h.get("command", "")
                    for mg in existing
                    for h in mg.get("hooks", [])
                ]
                if any("cc-memory" in cmd for cmd in existing_cmds):
                    # Update existing cc-memory hooks
                    settings["hooks"][event] = [
                        mg for mg in existing
                        if not any("cc-memory" in h.get("command", "") for h in mg.get("hooks", []))
                    ] + hook_list
                    self._log(f"  Updated {event} hook")
                else:
                    settings["hooks"].setdefault(event, []).extend(hook_list)
                    self._log(f"  Added {event} hook")

            SETTINGS_PATH.write_text(
                json.dumps(settings, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            self.hooks_status.set("Hooks configured successfully")
            self.hooks_btn.configure(text="Reconfigured")
            self._log(f"[OK] Settings saved to {SETTINGS_PATH}")
            messagebox.showinfo("Success", "Hooks configured!")
        except Exception as e:
            self._log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))

    def _browse(self):
        path = filedialog.askdirectory(title="Select project directory")
        if path:
            self.project_var.set(path)

    def _init_project(self):
        """Initialize memory for a project."""
        path = self.project_var.get().strip()
        if not path:
            messagebox.showwarning("No Project", "Please select a project directory.")
            return

        project = Path(path)
        if not project.exists():
            messagebox.showerror("Error", f"Directory not found: {path}")
            return

        try:
            memory_dir = project / "memory"
            memory_dir.mkdir(exist_ok=True)
            (memory_dir / "sessions").mkdir(exist_ok=True)
            (memory_dir / "topics").mkdir(exist_ok=True)

            # Initialize DB using installed plugin
            sys.path.insert(0, str(TARGET_DIR))
            from db import MemoryDB
            db = MemoryDB(memory_dir / "memory.db")
            db.upsert_project(str(project))

            # Create .gitignore
            gitignore = memory_dir / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(
                    "# cc-memory: exclude database from git\n"
                    "memory.db\nmemory.db-wal\nmemory.db-shm\nsessions/\n",
                    encoding="utf-8"
                )

            # Deploy /save-memories skill
            skill_dir = project / ".claude" / "skills" / "save-memories"
            skill_dst = skill_dir / "skill.md"
            skill_src = TARGET_DIR / "skill_template.md"
            if not skill_dst.exists() and skill_src.exists():
                skill_dir.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(str(skill_src), str(skill_dst))
                self._log(f"[OK] Deployed /save-memories skill")

            self._log(f"[OK] Memory initialized for {project.name}")
            self._log(f"     {memory_dir}")
            self.project_info.set(f"Initialized: {memory_dir}")
            messagebox.showinfo("Success", f"Memory initialized for {project.name}!")
        except Exception as e:
            self._log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))

    def _open_dashboard(self):
        dashboard_path = TARGET_DIR / "dashboard.py"
        if dashboard_path.exists():
            import subprocess
            cmd = [sys.executable, str(dashboard_path)]
            project = self.project_var.get().strip()
            if project:
                cmd += ["--project", project]
            subprocess.Popen(cmd)
        else:
            messagebox.showwarning("Not Found",
                                   "Dashboard not found. Install the plugin first.")


# ── CLI fallback ────────────────────────────────────────────────────────────

def cli_install():
    """CLI mode for headless systems."""
    print("=" * 50)
    print("  cc-memory — Claude Code Memory Plugin Installer")
    print("=" * 50)

    # Step 1: Copy files
    print(f"\n[1/2] Installing plugin to {TARGET_DIR}...")
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    for fname in PLUGIN_FILES:
        src = BUNDLE_DIR / fname
        if src.exists():
            shutil.copy2(str(src), str(TARGET_DIR / fname))
            print(f"  Copied: {fname}")

    # Step 2: Configure hooks
    print(f"\n[2/2] Configuring hooks in {SETTINGS_PATH}...")
    python_cmd = "python3" if shutil.which("python3") else "python"
    pre_compact_cmd = f'{python_cmd} "{TARGET_DIR / "pre_compact.py"}"'
    session_start_cmd = f'{python_cmd} "{TARGET_DIR / "session_start.py"}"'
    stop_cmd = f'{python_cmd} "{TARGET_DIR / "stop.py"}"'

    if SETTINGS_PATH.exists():
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    else:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        settings = {}

    settings.setdefault("hooks", {})
    settings["hooks"]["PreCompact"] = [{
        "matcher": "", "hooks": [{"type": "command", "command": pre_compact_cmd, "timeout": 30}]
    }]
    settings["hooks"]["SessionStart"] = [{
        "matcher": "", "hooks": [{"type": "command", "command": session_start_cmd, "timeout": 10}]
    }]
    settings["hooks"]["Stop"] = [{
        "matcher": "", "hooks": [{"type": "command", "command": stop_cmd, "timeout": 5}]
    }]

    SETTINGS_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    print("[OK] Hooks configured")

    print("\n" + "=" * 50)
    print("  Installation complete!")
    print("=" * 50)
    print(f"\nTo initialize a project:")
    print(f"  python \"{TARGET_DIR / 'setup.py'}\" --init <project-path>")
    print(f"\nTo open dashboard:")
    print(f"  python \"{TARGET_DIR / 'dashboard.py'}\"")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    if "--cli" in sys.argv:
        cli_install()
    else:
        root = tk.Tk()
        StandaloneInstaller(root)
        root.mainloop()


if __name__ == "__main__":
    main()
