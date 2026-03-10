#!/usr/bin/env python3
"""
cc-memory/installer.py -- GUI Installer
=========================================
Tkinter-based one-click installer for cc-memory plugin.

Usage:  python installer.py
"""
import sys, shutil, json, subprocess
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    print("Error: tkinter not available. Use setup.py instead:")
    print(f"  python {_PLUGIN_DIR / 'setup.py'} --init <project-path>")
    sys.exit(1)


class InstallerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("cc-memory Installer")
        self.root.geometry("600x500")
        self.root.resizable(False, False)

        self.log_lines = []
        self._build_ui()
        self._pre_check()

    def _build_ui(self):
        # Title
        ttk.Label(self.root, text="cc-memory — Claude Code Memory Plugin",
                  font=("", 14, "bold")).pack(pady=(20, 5))
        ttk.Label(self.root, text="Automatic save/restore of conversation context",
                  font=("", 10)).pack()

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=20, pady=15)

        # Step 1: Plugin installation
        f1 = ttk.LabelFrame(self.root, text="Step 1: Install Plugin Hooks", padding=10)
        f1.pack(fill=tk.X, padx=20, pady=5)
        self.hooks_status = tk.StringVar(value="Checking...")
        ttk.Label(f1, textvariable=self.hooks_status).pack(anchor=tk.W)
        self.install_btn = ttk.Button(f1, text="Install Hooks", command=self._install_hooks)
        self.install_btn.pack(anchor=tk.E)

        # Step 2: Initialize project
        f2 = ttk.LabelFrame(self.root, text="Step 2: Initialize Project", padding=10)
        f2.pack(fill=tk.X, padx=20, pady=5)

        pf = ttk.Frame(f2)
        pf.pack(fill=tk.X)
        ttk.Label(pf, text="Project:").pack(side=tk.LEFT)
        self.project_var = tk.StringVar()
        ttk.Entry(pf, textvariable=self.project_var, width=45).pack(side=tk.LEFT, padx=5)
        ttk.Button(pf, text="Browse", command=self._browse).pack(side=tk.LEFT)

        self.project_info = tk.StringVar(value="Select a project directory")
        ttk.Label(f2, textvariable=self.project_info, wraplength=500).pack(anchor=tk.W, pady=5)
        self.init_btn = ttk.Button(f2, text="Initialize Project", command=self._init_project)
        self.init_btn.pack(anchor=tk.E)

        # Log area
        ttk.Label(self.root, text="Log:").pack(anchor=tk.W, padx=20, pady=(10, 0))
        self.log_text = tk.Text(self.root, height=8, font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 10))

        # Bottom buttons
        bf = ttk.Frame(self.root)
        bf.pack(fill=tk.X, padx=20, pady=10)
        ttk.Button(bf, text="Open Dashboard", command=self._open_dashboard).pack(side=tk.LEFT)
        ttk.Button(bf, text="Close", command=self.root.quit).pack(side=tk.RIGHT)

    def _log(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _pre_check(self):
        settings_path = Path.home() / ".claude" / "settings.json"
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                hooks = settings.get("hooks", {})
                has_precompact = any(
                    "cc-memory" in h.get("command", "")
                    for mg in hooks.get("PreCompact", [])
                    for h in mg.get("hooks", [])
                )
                if has_precompact:
                    self.hooks_status.set("Hooks already installed")
                    self.install_btn.configure(text="Reinstall", state=tk.NORMAL)
                    self._log("[OK] Hooks already configured in settings.json")
                else:
                    self.hooks_status.set("Hooks not yet installed")
                    self._log("[  ] Hooks not found — click 'Install Hooks'")
            except Exception as e:
                self.hooks_status.set(f"Error reading settings: {e}")
        else:
            self.hooks_status.set("No settings.json found — will create")

    def _install_hooks(self):
        try:
            result = subprocess.run(
                [sys.executable, str(_PLUGIN_DIR / "setup.py")],
                capture_output=True, text=True, timeout=30
            )
            for line in result.stdout.strip().split("\n"):
                self._log(line)
            if result.returncode == 0:
                self.hooks_status.set("Hooks installed successfully")
                self.install_btn.configure(text="Reinstalled")
                messagebox.showinfo("Success", "Hooks installed successfully!")
            else:
                self._log(f"ERROR: {result.stderr}")
                messagebox.showerror("Error", f"Installation failed:\n{result.stderr}")
        except Exception as e:
            self._log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))

    def _browse(self):
        path = filedialog.askdirectory(title="Select project directory")
        if path:
            self.project_var.set(path)
            self._scan_project(path)

    def _scan_project(self, path):
        sys.path.insert(0, str(_PLUGIN_DIR))
        from setup import detect_project_type
        info = detect_project_type(Path(path))

        parts = [f"Type: {info['type']}"]
        if info["language"]:
            parts.append(f"Language: {info['language']}")
        if info["has_claude_md"]:
            parts.append("CLAUDE.md: found")
        if info["has_skills"]:
            n = len([f for f in info["files"] if "skills" in f])
            parts.append(f"Skills: {n}")
        parts.append(f"Files: {info['total_files']}")
        if (Path(path) / "memory" / "memory.db").exists():
            parts.append("Memory: already initialized")

        self.project_info.set(" | ".join(parts))

    def _init_project(self):
        path = self.project_var.get().strip()
        if not path:
            messagebox.showwarning("No Project", "Please select a project directory.")
            return

        try:
            result = subprocess.run(
                [sys.executable, str(_PLUGIN_DIR / "setup.py"), "--init", path],
                capture_output=True, text=True, timeout=30
            )
            for line in result.stdout.strip().split("\n"):
                self._log(line)
            if result.returncode == 0:
                messagebox.showinfo("Success", f"Memory initialized for {Path(path).name}!")
            else:
                self._log(f"ERROR: {result.stderr}")
        except Exception as e:
            self._log(f"ERROR: {e}")

    def _open_dashboard(self):
        project = self.project_var.get().strip()
        cmd = [sys.executable, str(_PLUGIN_DIR / "dashboard.py")]
        if project:
            cmd += ["--project", project]
        subprocess.Popen(cmd)


def main():
    root = tk.Tk()
    app = InstallerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
