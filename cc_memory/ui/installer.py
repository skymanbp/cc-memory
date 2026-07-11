#!/usr/bin/env python3
"""
cc-memory standalone installer (v2.3).

Self-contained installer that extracts plugin files and configures Claude Code.
Used as PyInstaller exe entry point.

When run as exe, bundled files are in sys._MEIPASS/cc_memory_files/<subdir>/
When run as script, files are in cc_memory/<subdir>/ relative to this file.

Subpackage layout (since v2.1) vs the legacy v2.0 flat layout:
- Plugin layout reorganized into subpackages (core/hooks/llm/ui/cli/mcp).
- hooks/settings paths point to cc_memory/hooks/<name>.py (not flat).
- Old SESSION_HANDOFF.md is migrated to .v2.bak (not deleted).
"""
import json
import os
import shutil
import sys
from pathlib import Path

# ── Resolve bundled / source root ─────────────────────────────────────────

def _get_bundle_root():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "cc_memory_files"
    # As script: walk up from cc_memory/ui/installer.py to repo root,
    # then return cc_memory/ as the "source" mirror of the bundle.
    return Path(__file__).resolve().parent.parent  # cc_memory/


BUNDLE_DIR = _get_bundle_root()
TARGET_DIR = Path.home() / ".claude" / "hooks" / "cc-memory"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Subdirectory contents (relative to cc_memory/ on disk OR cc_memory_files/ in exe)
SUBPACKAGE_FILES = {
    "":      ["__init__.py", "config.json"],
    "core":  ["__init__.py", "auth.py", "consolidate.py", "db.py",
              "encoding_setup.py", "extractor.py", "idle.py", "logger.py",
              "modes.py", "plan.py", "privacy.py", "progress.py"],
    "hooks": ["__init__.py", "consolidate_async.py", "post_tool_use.py",
              "pre_compact.py", "session_start.py", "stop.py", "user_prompt.py"],
    "llm":   ["__init__.py", "ccl_backend.py", "memory_writer.py"],
    "cli":   ["__init__.py", "mem.py", "plan.py"],
    "mcp":   ["__init__.py", "server.py"],
    "ui":    ["__init__.py", "dashboard.py", "installer.py", "web_viewer.py"],
}

# The 5 SYNC single-command hooks (one command each). PreCompact ALSO gets a
# second, async command hook appended in _make_hooks_config (see below).
# PreCompact sync base 80 * 1.5 (Windows mult) = 120s, matching hooks/hooks.json.
# Since v2.3.2 the sync leg only does fast extraction + PROGRESS.md (~1-5s);
# consolidation moved to the async sibling. Keep in lockstep with hooks.json.
HOOK_SCRIPTS = {
    "PreCompact":       ("hooks/pre_compact.py", 80),
    "SessionStart":     ("hooks/session_start.py", 10),
    "Stop":             ("hooks/stop.py", 22),
    "PostToolUse":      ("hooks/post_tool_use.py", 8),
    "UserPromptSubmit": ("hooks/user_prompt.py", 8),
}


# ── Tkinter GUI (preferred) ────────────────────────────────────────────────

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    _HAS_TK = True
except ImportError:
    _HAS_TK = False


def _copy_subpackages(target_dir, log_fn=print):
    """Copy each subpackage's files from BUNDLE_DIR into target_dir/<subdir>/."""
    n_copied = 0
    n_skipped = 0
    for subdir, files in SUBPACKAGE_FILES.items():
        src_root = BUNDLE_DIR / subdir if subdir else BUNDLE_DIR
        dst_root = target_dir / subdir if subdir else target_dir
        dst_root.mkdir(parents=True, exist_ok=True)
        for f in files:
            src = src_root / f
            dst = dst_root / f
            if not src.exists():
                log_fn(f"  SKIP (not found): {subdir}/{f}" if subdir else f"  SKIP: {f}")
                n_skipped += 1
                continue
            shutil.copy2(str(src), str(dst))
            n_copied += 1
            log_fn(f"  Copied: {subdir}/{f}" if subdir else f"  Copied: {f}")
    return n_copied, n_skipped


def _detect_python_cmd():
    return "python3" if shutil.which("python3") else "python"


def _make_hooks_config(target_dir):
    import platform
    win_mult = 1.5 if platform.system() == "Windows" else 1.0
    python_cmd = _detect_python_cmd()

    def _cmd(rel_script, timeout_s, is_async=False, apply_mult=True):
        c = {
            "type": "command",
            "command": f'{python_cmd} "{target_dir / rel_script}"',
            "timeout": int(timeout_s * win_mult) if apply_mult else int(timeout_s),
        }
        if is_async:
            c["async"] = True
        return c

    config = {ev: [{"matcher": "", "hooks": [_cmd(script, t)]}]
              for ev, (script, t) in HOOK_SCRIPTS.items()}

    # PreCompact carries a SECOND, async command hook: background consolidation.
    # It runs off the blocking compaction path (hooks/consolidate_async.py), so a
    # slow consolidation can never surface as "Hook cancelled". Async timeout is a
    # FLAT 300s (apply_mult=False) — a background deadline, not a blocking-UI
    # budget — matching hooks/hooks.json exactly. Keep the two paths in lockstep.
    config["PreCompact"][0]["hooks"].append(
        _cmd("hooks/consolidate_async.py", 300, is_async=True, apply_mult=False))
    return config


def _merge_into_settings(hooks_config, log_fn=print):
    if SETTINGS_PATH.exists():
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    else:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        settings = {}
    settings.setdefault("hooks", {})

    for event, hook_list in hooks_config.items():
        existing = settings["hooks"].get(event, [])
        # Strip any existing cc-memory entries (so re-install upgrades cleanly)
        cleaned = [
            mg for mg in existing
            if not any("cc-memory" in h.get("command", "")
                       for h in (mg.get("hooks", []) if isinstance(mg, dict) else []))
        ]
        settings["hooks"][event] = cleaned + hook_list
        log_fn(f"  {event}: {len(cleaned)} kept + {len(hook_list)} cc-memory")

    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _uninstall_settings(log_fn=print):
    """Remove cc-memory entries from settings.json."""
    if not SETTINGS_PATH.exists():
        log_fn("[  ] settings.json not found — nothing to remove")
        return
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log_fn("[ERR] settings.json is malformed; manual cleanup needed")
        return
    hooks = settings.get("hooks", {})
    for event in list(hooks.keys()):
        hook_list = hooks[event]
        if not isinstance(hook_list, list):
            continue
        cleaned = [
            mg for mg in hook_list
            if not any("cc-memory" in h.get("command", "")
                       for h in (mg.get("hooks", []) if isinstance(mg, dict) else []))
        ]
        if cleaned:
            hooks[event] = cleaned
        else:
            del hooks[event]
    settings["hooks"] = hooks
    # Also clean additionalDirectories
    perms = settings.get("permissions", {})
    add_dirs = perms.get("additionalDirectories", [])
    perms["additionalDirectories"] = [d for d in add_dirs if "cc-memory" not in str(d)]
    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log_fn("[OK] removed cc-memory entries from settings.json")


def _init_project(project_path, log_fn=print):
    """Create memory/ + DB + .gitignore in the given project directory."""
    project = Path(project_path)
    if not project.exists():
        raise FileNotFoundError(project)
    memory_dir = project / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "sessions").mkdir(exist_ok=True)
    (memory_dir / "topics").mkdir(exist_ok=True)

    # Bootstrap DB using the bundled db.py
    src_root = BUNDLE_DIR
    sys.path.insert(0, str(src_root))
    try:
        from core.db import MemoryDB
    except ImportError:
        # Fallback: maybe the bundle root IS the package and db.py is at core/db.py
        sys.path.insert(0, str(src_root / "core"))
        from db import MemoryDB

    db = MemoryDB(memory_dir / "memory.db")
    db.upsert_project(str(project))
    log_fn(f"[OK] DB initialized at {memory_dir / 'memory.db'}")

    gi = memory_dir / ".gitignore"
    if not gi.exists():
        gi.write_text(
            "memory.db\nmemory.db-wal\nmemory.db-shm\nsessions/\n.last_save.json\n",
            encoding="utf-8",
        )
        log_fn(f"[OK] .gitignore written")


# ── GUI ─────────────────────────────────────────────────────────────────────

class Installer:
    def __init__(self, root):
        self.root = root
        self.root.title("cc-memory Installer (v2.3)")
        self.root.geometry("680x600")
        self.root.resizable(False, False)
        self._build_ui()
        self._pre_check()

    def _build_ui(self):
        ttk.Label(self.root, text="cc-memory — Claude Code Memory Plugin (v2.3)",
                  font=("", 14, "bold")).pack(pady=(18, 4))
        ttk.Label(self.root,
                  text="Anti-patch reconcile-on-write · Forced PROGRESS.md handoff",
                  font=("", 10)).pack()

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=20, pady=12)

        # Step 1
        f1 = ttk.LabelFrame(self.root, text="Step 1: Install Plugin (global)", padding=10)
        f1.pack(fill=tk.X, padx=20, pady=5)
        self.install_status = tk.StringVar(value="Checking...")
        ttk.Label(f1, textvariable=self.install_status, wraplength=580).pack(anchor=tk.W)
        bf1 = ttk.Frame(f1)
        bf1.pack(fill=tk.X, pady=(5, 0))
        self.install_btn = ttk.Button(bf1, text="Install Plugin", command=self._install)
        self.install_btn.pack(side=tk.RIGHT)

        # Step 2
        f2 = ttk.LabelFrame(self.root, text="Step 2: Configure Hooks (settings.json)", padding=10)
        f2.pack(fill=tk.X, padx=20, pady=5)
        self.hooks_status = tk.StringVar(value="Checking...")
        ttk.Label(f2, textvariable=self.hooks_status, wraplength=580).pack(anchor=tk.W)
        bf2 = ttk.Frame(f2)
        bf2.pack(fill=tk.X, pady=(5, 0))
        self.hooks_btn = ttk.Button(bf2, text="Configure Hooks", command=self._configure_hooks)
        self.hooks_btn.pack(side=tk.RIGHT)

        # Step 3
        f3 = ttk.LabelFrame(self.root, text="Step 3: Initialize Project (per-project)", padding=10)
        f3.pack(fill=tk.X, padx=20, pady=5)
        pf = ttk.Frame(f3)
        pf.pack(fill=tk.X)
        ttk.Label(pf, text="Project:").pack(side=tk.LEFT)
        self.project_var = tk.StringVar()
        ttk.Entry(pf, textvariable=self.project_var, width=48).pack(side=tk.LEFT, padx=5)
        ttk.Button(pf, text="Browse", command=self._browse).pack(side=tk.LEFT)
        self.project_info = tk.StringVar(
            value="Auto-init also runs on first user message — this is just for explicit init.")
        ttk.Label(f3, textvariable=self.project_info, wraplength=580).pack(anchor=tk.W, pady=5)
        bf3 = ttk.Frame(f3)
        bf3.pack(fill=tk.X)
        self.init_btn = ttk.Button(bf3, text="Initialize Project", command=self._init_project_btn)
        self.init_btn.pack(side=tk.RIGHT)

        # Log
        ttk.Label(self.root, text="Log:").pack(anchor=tk.W, padx=20, pady=(8, 0))
        self.log_text = tk.Text(self.root, height=10, font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 8))

        # Bottom
        bf = ttk.Frame(self.root)
        bf.pack(fill=tk.X, padx=20, pady=8)
        ttk.Button(bf, text="Open Dashboard", command=self._open_dashboard).pack(side=tk.LEFT)
        ttk.Button(bf, text="Uninstall", command=self._uninstall).pack(side=tk.LEFT, padx=8)
        ttk.Button(bf, text="Close", command=self.root.quit).pack(side=tk.RIGHT)

    def _log(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _pre_check(self):
        if (TARGET_DIR / "core" / "db.py").exists():
            self.install_status.set(f"Plugin already installed (v2.3 layout) at {TARGET_DIR}")
            self.install_btn.configure(text="Reinstall")
            self._log(f"[OK] Plugin found at {TARGET_DIR}")
        elif (TARGET_DIR / "db.py").exists():
            self.install_status.set("OLD v2.0 layout detected. Click Install to upgrade to v2.3.")
            self.install_btn.configure(text="Upgrade to v2.3")
            self._log("[WARN] Found v2.0 flat-layout files; upgrading to v2.3 subpackage layout.")
        else:
            self.install_status.set("Plugin not yet installed")
            self._log("[  ] Plugin not found — click 'Install Plugin'")

        if SETTINGS_PATH.exists():
            try:
                settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                has_hook = any(
                    "cc-memory" in h.get("command", "")
                    for mg in settings.get("hooks", {}).get("PreCompact", [])
                    for h in mg.get("hooks", [])
                )
                if has_hook:
                    self.hooks_status.set("Hooks already configured")
                    self.hooks_btn.configure(text="Reconfigure")
                    self._log("[OK] Hooks found in settings.json")
                else:
                    self.hooks_status.set("Hooks not configured")
                    self._log("[  ] Hooks not found")
            except json.JSONDecodeError:
                self.hooks_status.set("settings.json malformed — manual cleanup needed")
        else:
            self.hooks_status.set("No settings.json — will create on configure")

    def _install(self):
        try:
            # On upgrade: blow away any old flat-layout files at TARGET_DIR
            # so they don't conflict with the new subpackage imports.
            if (TARGET_DIR / "db.py").exists():
                self._log("[upgrade] Removing old v2.0 flat-layout files...")
                for child in list(TARGET_DIR.iterdir()):
                    if child.is_file() and child.suffix == ".py":
                        child.unlink()

            TARGET_DIR.mkdir(parents=True, exist_ok=True)
            n_copied, n_skipped = _copy_subpackages(TARGET_DIR, self._log)
            self.install_status.set(
                f"Plugin installed: {n_copied} files (skipped {n_skipped}) -> {TARGET_DIR}"
            )
            self.install_btn.configure(text="Reinstalled")
            self._log(f"[OK] {n_copied} files installed")
            messagebox.showinfo("Success", f"Plugin installed!\n{TARGET_DIR}")
        except Exception as e:
            self._log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))

    def _configure_hooks(self):
        try:
            hooks_config = _make_hooks_config(TARGET_DIR)
            _merge_into_settings(hooks_config, self._log)
            self.hooks_status.set("Hooks configured")
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

    def _init_project_btn(self):
        path = self.project_var.get().strip()
        if not path:
            messagebox.showwarning("No Project", "Please select a project directory.")
            return
        try:
            _init_project(path, self._log)
            self.project_info.set(f"Initialized: {Path(path) / 'memory'}")
            messagebox.showinfo("Success", f"Memory initialized for {Path(path).name}!")
        except Exception as e:
            self._log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))

    def _open_dashboard(self):
        dashboard_path = TARGET_DIR / "ui" / "dashboard.py"
        if dashboard_path.exists():
            import subprocess
            cmd = [sys.executable, str(dashboard_path)]
            project = self.project_var.get().strip()
            if project:
                cmd += ["--project", project]
            subprocess.Popen(cmd)
        else:
            messagebox.showwarning("Not Found", "Dashboard not found. Install the plugin first.")

    def _uninstall(self):
        if not messagebox.askyesno(
            "Confirm Uninstall",
            "This will remove:\n"
            "  - hook files at ~/.claude/hooks/cc-memory/\n"
            "  - cc-memory entries in settings.json\n"
            "  - log files\n\n"
            "Project memory/ directories are PRESERVED.\n\nContinue?"
        ):
            return

        _uninstall_settings(self._log)

        if TARGET_DIR.exists():
            try:
                shutil.rmtree(str(TARGET_DIR))
                self._log(f"[OK] Removed {TARGET_DIR}")
            except OSError as e:
                self._log(f"[ERR] Could not remove {TARGET_DIR}: {e}")

        # Temp files
        import tempfile
        tmp = Path(tempfile.gettempdir())
        n_tmp = 0
        for pattern in ["cc_mem_turns_*", "cc_mem_prompt_*", "cc_mem_eval_*",
                        "cc_memory_reminded_*", "cc_mem_idle_*"]:
            for f in tmp.glob(pattern):
                try:
                    f.unlink()
                    n_tmp += 1
                except OSError:
                    # why: orphan temp file; ignore (best-effort cleanup)
                    continue
        if n_tmp:
            self._log(f"[OK] Removed {n_tmp} temp files")

        self.install_status.set("Plugin not installed")
        self.hooks_status.set("Hooks not configured")
        self.install_btn.configure(text="Install Plugin")
        self.hooks_btn.configure(text="Configure Hooks")
        messagebox.showinfo("Uninstalled",
                            "cc-memory has been removed.\n"
                            "Project memory/ directories preserved.\n"
                            "Reinstall anytime.")


# ── CLI fallback ───────────────────────────────────────────────────────────

def cli_install():
    print("=" * 50)
    print("  cc-memory v2.3 — CLI Installer")
    print("=" * 50)
    print(f"\n[1/2] Installing plugin to {TARGET_DIR}...")
    # Remove old flat-layout files
    if (TARGET_DIR / "db.py").exists():
        print("  [upgrade] removing old v2.0 flat-layout files...")
        for child in list(TARGET_DIR.iterdir()):
            if child.is_file() and child.suffix == ".py":
                child.unlink()
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    n_copied, n_skipped = _copy_subpackages(TARGET_DIR)
    print(f"  Copied {n_copied}, skipped {n_skipped}")

    print(f"\n[2/2] Configuring hooks in {SETTINGS_PATH}...")
    hooks_config = _make_hooks_config(TARGET_DIR)
    _merge_into_settings(hooks_config)
    print("[OK] 5 hooks configured (PreCompact, SessionStart, Stop, PostToolUse, UserPromptSubmit)")

    print("\n" + "=" * 50)
    print("  cc-memory v2.3 installation complete!")
    print("=" * 50)
    print(f"\nQuery a project:")
    print(f"  python \"{TARGET_DIR / 'cc_memory' / 'cli' / 'mem.py'}\" --project <path> status")
    print(f"\nDashboard:")
    print(f"  python \"{TARGET_DIR / 'cc_memory' / 'ui' / 'dashboard.py'}\" --project <path>")


def cli_uninstall():
    print("=" * 50)
    print("  cc-memory v2.3 — Uninstall")
    print("=" * 50)
    _uninstall_settings()
    if TARGET_DIR.exists():
        try:
            shutil.rmtree(str(TARGET_DIR))
            print(f"[OK] Removed {TARGET_DIR}")
        except OSError as e:
            print(f"[ERR] Could not remove {TARGET_DIR}: {e}")
    else:
        print("[  ] Plugin directory not found")
    import tempfile
    tmp = Path(tempfile.gettempdir())
    n = 0
    for pat in ["cc_mem_turns_*", "cc_mem_prompt_*", "cc_mem_eval_*",
                "cc_memory_reminded_*", "cc_mem_idle_*"]:
        for f in tmp.glob(pat):
            try:
                f.unlink()
                n += 1
            except OSError:
                # why: temp file already gone or locked; skip
                continue
    if n:
        print(f"[OK] Removed {n} temp files")
    print("\n[OK] cc-memory uninstalled. Project memory/ data preserved.")


def main():
    if "--uninstall" in sys.argv:
        cli_uninstall()
    elif "--cli" in sys.argv or not _HAS_TK:
        cli_install()
    else:
        root = tk.Tk()
        Installer(root)
        root.mainloop()


if __name__ == "__main__":
    main()
