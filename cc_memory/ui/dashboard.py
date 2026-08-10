#!/usr/bin/env python3
"""
cc-memory/dashboard.py -- Visual Memory Management Dashboard
==============================================================
Tkinter-based GUI for browsing, searching, and managing cc-memory databases.

Features:
  - Project selector (auto-discovers projects with memory.db)
  - Memory browser: filter by category, importance, search
  - Plan manager: add/approve/execute/clear plans
  - Session history viewer
  - Keyword vocabulary
  - Stats overview
  - Direct SQL console

Usage:
  python dashboard.py
  python dashboard.py --project D:/Projects/my-project
"""
import argparse, json, re, sqlite3, subprocess, sys, os
# why: this module never references urllib - it is a PyInstaller anchor, not
# dead code, and deleting it silently breaks the frozen dashboard. build_exe.py
# ships core/ and llm/ as --add-data, which PyInstaller never analyses, so the
# `import urllib.request` in llm/ccl_backend.py:25 is invisible to the build.
# Measured on two probe builds of this file's exact import set: without this
# line the Analysis TOC contains no urllib.request, urllib.error, http.client or
# ssl; with it, all four are collected. Losing them kills Tidy Memories and LLM
# Save Session at runtime in the exe - silently, because _extract_via_llm
# swallows the ImportError and falls back to regex. One import is the whole
# anchor: the other three arrive transitively.
import urllib.request  # noqa: F401 -- why: see the frozen-build note above
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent     # cc_memory/ui/
_PKG_ROOT = _HERE.parent                     # cc_memory/
# Support both: running as script (subpackages in same parent) and PyInstaller exe.
if getattr(sys, 'frozen', False):
    _BUNDLE_DIR = Path(sys._MEIPASS) / "cc_memory_files"
    sys.path.insert(0, str(_BUNDLE_DIR))
else:
    sys.path.insert(0, str(_PKG_ROOT))
from core.db import CATEGORIES, MemoryDB
from core.encoding_setup import enable_utf8_io
from core.extractor import (
    build_extraction,
    group_sentences,
    load_transcript,
)
from core.progress import ensure_memory_dir
from llm.memory_writer import upsert_smart, upsert_batch, regenerate_memory_index

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, scrolledtext
except ImportError:
    print("Error: tkinter is not available. Install python3-tk.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

def _registry_path() -> Path:
    """Return the file that stores the dashboard's project registry.

    A frozen (PyInstaller) build must NOT keep it next to the bundle: both
    ``sys._MEIPASS`` and its parent live under the OS temp directory, so Disk
    Cleanup silently eats the user's project list. Frozen builds therefore use
    the per-user config directory, derived at runtime — never a hardcoded home
    path. Script runs keep the historical location inside the package (it is
    gitignored and writable for a source checkout).
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
            root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        else:
            base = os.environ.get("XDG_CONFIG_HOME")
            root = Path(base) if base else Path.home() / ".config"
        cfg = root / "cc-memory"
        try:
            cfg.mkdir(parents=True, exist_ok=True)
            return cfg / "projects.json"
        except OSError:
            # why: an unwritable config root must not stop the GUI from
            # starting; fall back to the bundle dir and let the guarded save
            # path surface the failure as a status-bar warning instead
            return _PKG_ROOT / "projects.json"
    return _PKG_ROOT / "projects.json"


_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_SQL_WRITE_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|"
    r"attach|detach|vacuum|reindex|begin|commit|rollback)\b", re.I)

# SQLite accepts BOTH `PRAGMA name = value` and `PRAGMA name(value)` for every
# settable pragma. Looking only for "=" therefore let `PRAGMA user_version(7)`
# and `PRAGMA application_id(1234)` through as "read-only": they ran, COMMITTED
# and printed "(no rows returned)" with no confirmation dialog and no rowcount.
# The parenthesised form is also how the read-only introspection pragmas take
# their argument, so those are allow-listed BY NAME rather than by syntax.
_PRAGMA_READ_WITH_ARG = frozenset((
    "table_info", "table_xinfo", "table_list", "index_info", "index_list",
    "index_xinfo", "foreign_key_list", "foreign_key_check", "collation_list",
    "database_list", "compile_options", "function_list", "module_list",
    "pragma_list", "integrity_check", "quick_check", "freelist_count",
    "page_count",
))
# ...and these ACT on the file although they take no argument at all
# (`PRAGMA optimize` created a sqlite_stat1 table in the user's database).
_PRAGMA_WRITES_BARE = frozenset((
    "optimize", "wal_checkpoint", "incremental_vacuum", "shrink_memory",
))
_PRAGMA_HEAD_RE = re.compile(
    r"pragma\s+(?:[A-Za-z_]\w*\s*\.\s*)?([A-Za-z_]\w*)\s*(.*)", re.I | re.S)


def _pragma_is_read_only(body: str) -> bool:
    """True only for a PRAGMA that cannot change the file.

    Unrecognised spellings are reported as writes — the caller then asks for
    confirmation, which is the safe direction.
    """
    m = _PRAGMA_HEAD_RE.match(body.strip())
    if not m:
        return False
    name = m.group(1).lower()
    arg = m.group(2).strip().rstrip(";").strip()
    if arg:
        # ANY argument — "= value", "(value)" or a bare value — is a set,
        # except for the introspection pragmas that report on the schema.
        return name in _PRAGMA_READ_WITH_ARG
    return name not in _PRAGMA_WRITES_BARE


def _sql_is_read_only(query: str) -> bool:
    """True only for statements that cannot modify the database.

    Deliberately conservative — anything that is not plainly a SELECT /
    EXPLAIN / read PRAGMA / CTE-SELECT is reported as a write so the caller
    asks for confirmation first. A false "write" costs one dialog; a false
    "read" costs the user's memories (``DELETE FROM memories`` used to run and
    commit from the SQL console while printing "(no rows returned)").
    """
    body = _SQL_COMMENT_RE.sub(" ", query).strip()
    if not body:
        return True
    head = re.match(r"[A-Za-z]+", body)
    kw = head.group(0).lower() if head else ""
    if kw not in ("select", "with", "explain", "pragma"):
        return False
    if kw == "pragma" and not _pragma_is_read_only(body):
        return False
    return _SQL_WRITE_RE.search(body) is None


# ---------------------------------------------------------------------------
# Dashboard Application
# ---------------------------------------------------------------------------
class DashboardApp:
    def __init__(self, root, initial_project=None):
        self.root = root
        self.root.title("cc-memory Dashboard")
        self.root.geometry("1000x700")
        self.root.minsize(800, 500)

        self.db = None
        self.project_id = None
        self.project_path = None
        self._manual_api_key = ""  # Set via Settings dialog
        self._projects_file = _registry_path()

        self._build_ui()

        if initial_project:
            self._load_project(initial_project)
        else:
            self._auto_discover_projects()

    # ── UI Construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar: project selector
        top = ttk.Frame(self.root, padding=5)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Project:").pack(side=tk.LEFT)
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(top, textvariable=self.project_var, width=60)
        self.project_combo.pack(side=tk.LEFT, padx=5)
        self.project_combo.bind("<<ComboboxSelected>>", self._on_project_selected)
        # `Browse...` exposes _browse_project, which was implemented and wired
        # to no widget at all until now.
        ttk.Button(top, text="Browse...", command=self._browse_project).pack(side=tk.LEFT)
        ttk.Button(top, text="Manage...", command=self._manage_projects).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Init New", command=self._init_new_project).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Save Session", command=self._save_current_session).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Tidy Memories", command=self._tidy_memories).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Refresh", command=self._refresh).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Settings", command=self._show_settings).pack(side=tk.RIGHT, padx=5)

        # Notebook tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self._build_memories_tab()
        self._build_progress_tab()
        self._build_plans_tab()
        self._build_sessions_tab()
        self._build_keywords_tab()
        self._build_sql_tab()
        self._build_stats_tab()

        # Status bar
        self.status_var = tk.StringVar(value="Select a project to begin")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor=tk.W, padding=3).pack(fill=tk.X)

    # ── API Key Management ─────────────────────────────────────────────────

    def _get_api_key(self) -> str:
        """Get API key from: manual setting > env var > Claude OAuth token."""
        if self._manual_api_key:
            return self._manual_api_key
        from core.auth import get_api_key
        key, _source = get_api_key()
        return key

    def _show_settings(self):
        """Show settings dialog for API key configuration."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Settings")
        dlg.geometry("600x200")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="Anthropic API Key", font=("", 11, "bold")).pack(
            padx=15, pady=(15, 5), anchor=tk.W)
        ttk.Label(dlg, text="Required for Tidy Memories and LLM-powered Save Session.\n"
                  "Auto-detected from Claude OAuth. Only set manually if auto-detection fails.",
                  wraplength=560, font=("", 9)).pack(padx=15, anchor=tk.W)

        key_var = tk.StringVar(value=self._manual_api_key)
        key_entry = ttk.Entry(dlg, textvariable=key_var, width=70, show="*")
        key_entry.pack(padx=15, pady=10, fill=tk.X)

        # Show current source
        from core.auth import get_api_key as _gak
        if self._manual_api_key:
            src_label = f"Current: ...{self._manual_api_key[-8:]} (manual)"
            src_color = ""
        else:
            _k, _src = _gak()
            if _k:
                src_label = f"Current: ...{_k[-8:]} (from {_src})"
                src_color = ""
            elif _src == "oauth_expired":
                src_label = "Claude OAuth token expired — restart Claude Code to refresh"
                src_color = "orange"
            else:
                src_label = "No API key found"
                src_color = "red"
        lbl_kw = {"text": src_label, "font": ("", 9)}
        if src_color:
            lbl_kw["foreground"] = src_color
        ttk.Label(dlg, **lbl_kw).pack(padx=15, anchor=tk.W)

        def save():
            self._manual_api_key = key_var.get().strip()
            dlg.destroy()
            src = "manual" if self._manual_api_key else ("auto" if self._get_api_key() else "none")
            self.status_var.set(f"API key: {src}")

        bf = ttk.Frame(dlg)
        bf.pack(pady=10)
        ttk.Button(bf, text="Save", command=save).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

    def _build_memories_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Memories")

        # Filters
        filt = ttk.Frame(frame, padding=5)
        filt.pack(fill=tk.X)
        ttk.Label(filt, text="Search:").pack(side=tk.LEFT)
        self.mem_search_var = tk.StringVar()
        search_entry = ttk.Entry(filt, textvariable=self.mem_search_var, width=30)
        search_entry.pack(side=tk.LEFT, padx=5)
        search_entry.bind("<Return>", lambda e: self._load_memories())

        ttk.Label(filt, text="Category:").pack(side=tk.LEFT, padx=(10,0))
        self.mem_cat_var = tk.StringVar(value="all")
        cat_combo = ttk.Combobox(filt, textvariable=self.mem_cat_var, width=12,
                     values=["all"] + list(CATEGORIES),
                     state="readonly")
        cat_combo.pack(side=tk.LEFT, padx=5)
        cat_combo.bind("<<ComboboxSelected>>", lambda e: self._load_memories())

        ttk.Label(filt, text="Min Imp:").pack(side=tk.LEFT, padx=(10,0))
        self.mem_imp_var = tk.StringVar(value="1")
        # state="readonly": an editable spinbox could be blanked, and
        # int("") raised inside the Tk callback — invisible in the
        # --windowed exe, which has no console to print the traceback to.
        # _load_memories() also parses defensively; both halves are needed.
        imp_spin = ttk.Spinbox(filt, textvariable=self.mem_imp_var, from_=1, to=5,
                    width=3, command=self._load_memories, state="readonly")
        imp_spin.pack(side=tk.LEFT, padx=5)

        ttk.Button(filt, text="Search", command=self._load_memories).pack(side=tk.LEFT, padx=5)
        ttk.Button(filt, text="Add Memory", command=self._add_memory_dialog).pack(side=tk.RIGHT)

        # Treeview
        cols = ("id", "cat", "imp", "content", "date")
        self.mem_tree = ttk.Treeview(frame, columns=cols, show="headings", height=20)
        self.mem_tree.heading("id", text="ID")
        self.mem_tree.heading("cat", text="Category")
        self.mem_tree.heading("imp", text="Imp")
        self.mem_tree.heading("content", text="Content")
        self.mem_tree.heading("date", text="Date")
        self.mem_tree.column("id", width=40)
        self.mem_tree.column("cat", width=80)
        self.mem_tree.column("imp", width=35)
        self.mem_tree.column("content", width=600)
        self.mem_tree.column("date", width=90)

        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.mem_tree.yview)
        self.mem_tree.configure(yscrollcommand=scroll.set)
        self.mem_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scroll.pack(fill=tk.Y, side=tk.RIGHT)

    def _build_plans_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Plans")

        # Toolbar row 1: lifecycle actions
        tb1 = ttk.Frame(frame, padding=(5, 5, 5, 0))
        tb1.pack(fill=tk.X)
        ttk.Button(tb1, text="Add Plan", command=self._add_plan_dialog).pack(side=tk.LEFT)
        ttk.Separator(tb1, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(tb1, text="Approve", command=self._approve_plans).pack(side=tk.LEFT)
        ttk.Button(tb1, text="Approve All", command=self._approve_all_plans).pack(side=tk.LEFT, padx=3)
        ttk.Separator(tb1, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(tb1, text="Execute", command=self._execute_plans).pack(side=tk.LEFT)
        ttk.Button(tb1, text="Mark Done", command=self._mark_plan_done).pack(side=tk.LEFT, padx=3)
        ttk.Button(tb1, text="Mark Failed", command=self._mark_plan_failed).pack(side=tk.LEFT)
        ttk.Separator(tb1, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(tb1, text="Edit", command=self._edit_plan_dialog).pack(side=tk.LEFT)
        ttk.Button(tb1, text="Delete", command=self._delete_plans).pack(side=tk.LEFT, padx=3)
        ttk.Button(tb1, text="Clear Done", command=self._clear_done_plans).pack(side=tk.LEFT, padx=3)
        ttk.Button(tb1, text="Refresh", command=self._load_plans).pack(side=tk.RIGHT)

        # Treeview
        cols = ("id", "order", "status", "content", "result")
        self.plan_tree = ttk.Treeview(frame, columns=cols, show="headings", height=15,
                                       selectmode="extended")
        self.plan_tree.heading("id", text="ID")
        self.plan_tree.heading("order", text="Order")
        self.plan_tree.heading("status", text="Status")
        self.plan_tree.heading("content", text="Content")
        self.plan_tree.heading("result", text="Eval / Result")
        self.plan_tree.column("id", width=40)
        self.plan_tree.column("order", width=50)
        self.plan_tree.column("status", width=80)
        self.plan_tree.column("content", width=420)
        self.plan_tree.column("result", width=280)

        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.plan_tree.yview)
        self.plan_tree.configure(yscrollcommand=scroll.set)
        self.plan_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(5, 0), pady=5)
        scroll.pack(fill=tk.Y, side=tk.RIGHT, padx=(0, 5), pady=5)

        # Double-click to edit
        self.plan_tree.bind("<Double-1>", lambda e: self._edit_plan_dialog())

        # Right-click context menu
        self.plan_menu = tk.Menu(self.plan_tree, tearoff=0)
        self.plan_menu.add_command(label="Edit...", command=self._edit_plan_dialog)
        self.plan_menu.add_separator()
        self.plan_menu.add_command(label="Approve", command=self._approve_plans)
        self.plan_menu.add_command(label="Execute", command=self._execute_plans)
        self.plan_menu.add_command(label="Mark Done", command=self._mark_plan_done)
        self.plan_menu.add_command(label="Mark Failed", command=self._mark_plan_failed)
        self.plan_menu.add_command(label="Skip", command=self._skip_plans)
        self.plan_menu.add_separator()
        self.plan_menu.add_command(label="Delete", command=self._delete_plans)
        self.plan_tree.bind("<Button-3>", self._plan_context_menu)

    def _build_sessions_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Sessions")

        cols = ("id", "trigger", "date", "msgs", "archive")
        self.sess_tree = ttk.Treeview(frame, columns=cols, show="headings", height=15)
        self.sess_tree.heading("id", text="ID")
        self.sess_tree.heading("trigger", text="Trigger")
        self.sess_tree.heading("date", text="Date")
        self.sess_tree.heading("msgs", text="Messages")
        self.sess_tree.heading("archive", text="Archive File")
        self.sess_tree.column("id", width=40)
        self.sess_tree.column("trigger", width=70)
        self.sess_tree.column("date", width=140)
        self.sess_tree.column("msgs", width=70)
        self.sess_tree.column("archive", width=300)
        self.sess_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _build_keywords_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Keywords")

        cols = ("keyword", "freq", "last_seen")
        self.kw_tree = ttk.Treeview(frame, columns=cols, show="headings", height=20)
        self.kw_tree.heading("keyword", text="Keyword")
        self.kw_tree.heading("freq", text="Frequency")
        self.kw_tree.heading("last_seen", text="Last Seen")
        self.kw_tree.column("keyword", width=200)
        self.kw_tree.column("freq", width=80)
        self.kw_tree.column("last_seen", width=120)
        self.kw_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _build_sql_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="SQL Console")

        top = ttk.Frame(frame, padding=5)
        top.pack(fill=tk.X)
        ttk.Label(top, text="SQL:").pack(side=tk.LEFT)
        self.sql_var = tk.StringVar(value="SELECT * FROM memories WHERE is_active=1 ORDER BY importance DESC LIMIT 20")
        sql_entry = ttk.Entry(top, textvariable=self.sql_var, width=80)
        sql_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        sql_entry.bind("<Return>", lambda e: self._run_sql())
        ttk.Button(top, text="Run", command=self._run_sql).pack(side=tk.LEFT)

        self.sql_output = scrolledtext.ScrolledText(frame, height=25, font=("Consolas", 10))
        self.sql_output.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _build_stats_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Stats")
        self.stats_text = scrolledtext.ScrolledText(frame, height=25, font=("Consolas", 11))
        self.stats_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _build_progress_tab(self):
        """Read-only view of the two post-v2.0 SQL anchors.

        `progress` (one row per project) is the source of truth behind
        memory/PROGRESS.md; `plan_active` (also one row) backs memory/PLAN.md.
        Both shipped as headline features and neither was visible anywhere in
        this GUI — the "Plans" tab is the unrelated legacy v2.0 `plans` queue.
        Read-only by design: PROGRESS.md is owned by the hooks and PLAN.md by
        `/cc-mem plan-*`, so editing it here would fight the writers.
        """
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Progress / Plan")

        bar = ttk.Frame(frame, padding=(5, 5, 5, 0))
        bar.pack(fill=tk.X)
        ttk.Label(bar, font=("", 9),
                  text="Read-only. PROGRESS.md is written by the hooks; "
                       "PLAN.md by Claude's plan mode / /cc-mem plan-*.").pack(side=tk.LEFT)
        ttk.Button(bar, text="Refresh",
                   command=self._load_progress_plan).pack(side=tk.RIGHT)

        self.progress_text = scrolledtext.ScrolledText(
            frame, height=25, font=("Consolas", 10), wrap=tk.WORD)
        self.progress_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.progress_text.config(state=tk.DISABLED)

    # ── Project Management ───────────────────────────────────────────────────

    # ── Project Registry (persistent JSON) ──────────────────────────────────

    def _load_project_registry(self, existing_only: bool = False) -> list:
        """Load saved project paths from projects.json.

        Returns EVERY saved path by default. The old version dropped entries
        whose directory did not currently exist, and the caller wrote that
        filtered list straight back — so unplugging a drive (or a VPN drop on
        a network share) permanently deleted the project from the registry.
        Existence is a *display* concern; pass ``existing_only=True`` where a
        path is about to be opened.

        SHAPE is validated, not just JSON syntax. A file that parses but is not
        ``{"projects": [str, ...]}`` is now treated exactly like an unparseable
        one — backed up, then ignored — because the docstring's promise that a
        broken registry stays recoverable was false for every wrong shape:
        ``["D:/a","D:/b"]`` silently returned [] and was overwritten with NO
        .bak, and ``{"projects": "D:/a"}`` iterated the string CHARACTER by
        character and persisted 'D', ':', '/', 'a' as four projects (a dict
        value persisted its keys the same way).
        """
        if not self._projects_file.exists():
            return []
        try:
            data = json.loads(self._projects_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(
                    f"top level is {type(data).__name__}, expected an object")
            saved = data.get("projects", [])
            if not isinstance(saved, list):
                raise ValueError(
                    f'"projects" is {type(saved).__name__}, expected an array')
            projects = [p for p in saved if isinstance(p, str) and p.strip()]
        except (json.JSONDecodeError, UnicodeDecodeError, OSError,
                ValueError) as e:
            # A corrupt registry used to be silently replaced by the rescan,
            # which loses every manually added path outside the scan roots.
            # Keep a copy so it stays recoverable.
            self._backup_broken_registry(e)
            return []
        if existing_only:
            projects = [p for p in projects if Path(p).exists()]
        return projects

    def _backup_broken_registry(self, err):
        """Preserve an unusable projects.json before anything overwrites it."""
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = self._projects_file.with_name(
                self._projects_file.name + f".corrupt-{stamp}.bak")
            backup.write_bytes(self._projects_file.read_bytes())
            note = f"Project registry unreadable ({err}); backed up to {backup.name}"
        except OSError as copy_err:
            note = f"Project registry unreadable ({err}); backup failed ({copy_err})"
        if hasattr(self, "status_var"):
            self.status_var.set(note)

    def _save_project_registry(self, projects: list):
        """Save project paths to projects.json.

        Never raises: this runs from __init__ via _auto_discover_projects, and
        a read-only config dir used to abort dashboard startup entirely.
        """
        try:
            self._projects_file.parent.mkdir(parents=True, exist_ok=True)
            self._projects_file.write_text(
                json.dumps({"projects": projects}, indent=2, ensure_ascii=False),
                encoding="utf-8")
        except OSError as e:
            # why: the registry is a convenience cache; losing it degrades the
            # combobox to "whatever the scan finds" but must never prevent the
            # user from opening the dashboard or a project
            if hasattr(self, "status_var"):
                self.status_var.set(f"Could not save project list: {e}")

    def _add_to_registry(self, project_path: str):
        """Add a project to the persistent registry (dedup)."""
        projects = self._load_project_registry()
        resolved = str(Path(project_path).resolve())
        # Dedup (case-insensitive on Windows)
        existing_lower = {p.lower() for p in projects}
        if resolved.lower() not in existing_lower:
            projects.append(resolved)
            self._save_project_registry(projects)
        return projects

    def _auto_discover_projects(self):
        """Load saved projects + scan common directories for new ones."""
        # Start with the FULL saved registry — including paths that are
        # currently unreachable, so they survive this write.
        projects = self._load_project_registry()
        known_lower = {p.lower() for p in projects}

        # Scan common locations for undiscovered projects
        search_dirs = []
        for d in ["D:/Projects", "C:/Projects", str(Path.home() / "Projects"),
                   str(Path.home() / "repos"), str(Path.home() / "dev")]:
            if Path(d).exists():
                search_dirs.append(Path(d))

        for sd in search_dirs:
            try:
                for child in sd.iterdir():
                    db_path = child / "memory" / "memory.db"
                    if db_path.exists():
                        resolved = str(child.resolve())
                        if resolved.lower() not in known_lower:
                            projects.append(resolved)
                            known_lower.add(resolved.lower())
            except PermissionError:
                # why: an unreadable scan root (permissions, offline share) is
                # not an error for discovery — the registry still stands
                pass

        # Persist the union; the pruning happens for DISPLAY only, below.
        self._save_project_registry(projects)

        available = [p for p in projects if Path(p).exists()]
        self.project_combo["values"] = available or projects
        # Launch is READ-ONLY. Opening a project is not a passive act: it
        # creates memory/ + sessions/ + topics/, writes (and migrates) the
        # .gitignore and runs the schema migrations inside memory.db.
        # Auto-loading available[0] did all of that on EVERY launch, to
        # whichever project happened to sort first — one the user never picked.
        # Show the list and wait for a choice. `--project` and the combobox
        # remain the ways to open one, because those are choices.
        if available:
            self.status_var.set(
                f"{len(available)} project(s) known — pick one in the Project "
                f"box to open it (none is opened automatically)")
        elif projects:
            self.status_var.set(
                f"{len(projects)} saved project(s), none currently reachable — "
                f"use Browse... or Manage...")

    def _browse_project(self):
        path = filedialog.askdirectory(title="Select project directory")
        if path:
            self._add_to_registry(path)
            self._load_project(path)

    def _manage_projects(self):
        """Dialog to add, remove, and reorder project directories."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Manage Projects")
        dlg.geometry("700x450")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="Project Directories", font=("", 11, "bold")).pack(
            padx=15, pady=(10, 5), anchor=tk.W)
        ttk.Label(dlg, text="Projects with memory.db are managed here. "
                  "Add new directories or remove stale ones.",
                  wraplength=660, font=("", 9)).pack(padx=15, anchor=tk.W)

        # Listbox with scrollbar
        list_frame = ttk.Frame(dlg)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set,
                             selectmode=tk.EXTENDED, font=("Consolas", 10))
        scrollbar.config(command=listbox.yview)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Populate
        projects = self._load_project_registry()
        for p in projects:
            has_db = (Path(p) / "memory" / "memory.db").exists()
            prefix = "" if has_db else "[no DB] "
            listbox.insert(tk.END, f"{prefix}{p}")

        # Status label
        status_label = ttk.Label(dlg, text=f"{len(projects)} project(s)", font=("", 9))
        status_label.pack(padx=15, anchor=tk.W)

        # Buttons
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=15, pady=(0, 10))

        def add_dir():
            path = filedialog.askdirectory(title="Add project directory")
            if not path:
                return
            resolved = str(Path(path).resolve())
            # Check for duplicate
            current = list(listbox.get(0, tk.END))
            current_paths = [c.replace("[no DB] ", "") for c in current]
            if resolved.lower() in {p.lower() for p in current_paths}:
                messagebox.showinfo("Duplicate", "This project is already in the list.")
                return
            has_db = (Path(resolved) / "memory" / "memory.db").exists()
            prefix = "" if has_db else "[no DB] "
            listbox.insert(tk.END, f"{prefix}{resolved}")
            status_label.config(text=f"{listbox.size()} project(s)")

        def remove_selected():
            sel = listbox.curselection()
            if not sel:
                return
            if not messagebox.askyesno("Remove", f"Remove {len(sel)} project(s) from list?\n"
                                       "(This does NOT delete any files.)"):
                return
            for i in reversed(sel):
                listbox.delete(i)
            status_label.config(text=f"{listbox.size()} project(s)")

        def move_up():
            sel = listbox.curselection()
            if not sel or sel[0] == 0:
                return
            for i in sel:
                text = listbox.get(i)
                listbox.delete(i)
                listbox.insert(i - 1, text)
                listbox.selection_set(i - 1)

        def move_down():
            sel = listbox.curselection()
            if not sel or sel[-1] >= listbox.size() - 1:
                return
            for i in reversed(sel):
                text = listbox.get(i)
                listbox.delete(i)
                listbox.insert(i + 1, text)
                listbox.selection_set(i + 1)

        def scan_dir():
            """Scan a parent directory for projects with memory.db."""
            path = filedialog.askdirectory(title="Select parent directory to scan")
            if not path:
                return
            current = list(listbox.get(0, tk.END))
            current_paths = {c.replace("[no DB] ", "").lower() for c in current}
            found = 0
            try:
                for child in Path(path).iterdir():
                    if child.is_dir() and (child / "memory" / "memory.db").exists():
                        resolved = str(child.resolve())
                        if resolved.lower() not in current_paths:
                            listbox.insert(tk.END, resolved)
                            current_paths.add(resolved.lower())
                            found += 1
            except PermissionError:
                pass
            status_label.config(text=f"{listbox.size()} project(s) ({found} new)")

        def save_and_close():
            items = list(listbox.get(0, tk.END))
            # Strip [no DB] prefix
            paths = [item.replace("[no DB] ", "") for item in items]
            self._save_project_registry(paths)
            # Update combo. Editing the LIST must not open a project: the old
            # `if paths and not self.project_path` branch loaded paths[0] with
            # the UNFILTERED list, so a stale entry the user had deliberately
            # kept for reference got created from scratch (memory/, .gitignore,
            # memory.db) or blew up on an unreachable drive. Opening stays an
            # explicit choice in the Project box.
            self.project_combo["values"] = paths
            dlg.destroy()
            self.status_var.set(f"Saved {len(paths)} project(s)")

        ttk.Button(btn_frame, text="Add Directory", command=add_dir).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Scan Folder", command=scan_dir).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Remove", command=remove_selected).pack(side=tk.LEFT, padx=2)
        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(btn_frame, text="Up", command=move_up, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Down", command=move_down, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(btn_frame, text="Save & Close", command=save_and_close).pack(side=tk.RIGHT, padx=2)
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT, padx=2)

    def _on_project_selected(self, event):
        self._load_project(self.project_var.get())

    def _ensure_memory_dir(self, project) -> Path:
        """Create memory/ + its subdirs AND the .gitignore.

        Every path that can bring a project into existence funnels through
        here. Previously only "Init New" wrote the ignore file, so selecting an
        uninitialised project in the combobox — or a registry auto-load, or
        Manage.../Save & Close — left memory.db, -wal and -shm sitting
        un-ignored in the user's repo.

        The PROJECT directory itself is never created. ``mkdir(parents=True)``
        happily materialised the whole tree for a path that no longer exists,
        so selecting a deleted-but-remembered registry entry silently
        RECREATED the project as an empty shell — memory.db, .gitignore,
        sessions/ and topics/ included — instead of reporting it gone. Raises
        FileNotFoundError instead; _load_project turns that into a dialog.

        The rule lives in `core.progress.ensure_memory_dir` now. Keeping the
        correct version here as a private method is why six other creators
        each shipped their own parents=True copy and kept resurrecting.
        """
        return ensure_memory_dir(Path(project) / "memory")

    def _set_busy(self, busy: bool, msg: str = ""):
        """Toggle a wait cursor + status text around a long main-thread job."""
        try:
            self.root.config(cursor="watch" if busy else "")
        except tk.TclError:
            # why: some window managers reject cursor changes; a missing busy
            # cursor must never abort the work it was only decorating
            pass
        if msg:
            self.status_var.set(msg)
        self.root.update()

    def _load_project(self, project_path):
        """Open a project. Reports failure in the UI; never raises.

        This was the one callback with no guard at all. A registry entry on an
        unavailable drive raises ``FileNotFoundError: [WinError 3] ... 'Q:\\'``
        out of _ensure_memory_dir, and Tk routes an uncaught callback exception
        to report_callback_exception → stderr, which a --windowed PyInstaller
        build does not have: no dialog, no status change, the click simply did
        nothing, forever. Three routes arrive here with unverified paths — the
        combobox, Manage.../Save & Close, and --project on the command line.

        Nothing is assigned until the open succeeds, so a failed load leaves
        the previously loaded project intact instead of half-swapping to one
        that could not be opened (project_path used to be overwritten first).
        """
        # Anchoring belongs HERE, not only in main(): this is the single choke
        # point all five routes pass through (combobox, Manage.../Save & Close,
        # the registry, Init New, and --project), and the one place that turns
        # a path into a database. Anchoring only --project left the four GUI
        # routes planting a full memory/memory.db in whatever subdirectory the
        # user happened to browse to — and rung 0 then pinned every hook to it.
        # The opt-out is checked on the RAW pick, before anchoring, so a
        # per-subdirectory exclusion is not widened to its parent.
        notice = self._opt_out_notice(project_path)
        if notice:
            self._report_opt_out(project_path, notice)
            return
        try:
            resolved = Path(self._anchor(project_path)).resolve()
            memory_dir = self._ensure_memory_dir(resolved)
            db = MemoryDB(memory_dir / "memory.db")
            project_id = db.upsert_project(str(resolved))
        except Exception as e:
            # why: every failure mode here — missing drive, deleted directory,
            # permission denied, corrupt or locked memory.db — must reach the
            # user as a dialog. The alternative is Tk's stderr traceback, which
            # the frozen GUI build discards. The app keeps its old project.
            self._report_project_error("open", project_path, e)
            return

        self.project_path = resolved
        self.db = db
        self.project_id = project_id
        self.project_var.set(str(resolved))

        # Persist to registry + update combo
        projects = self._add_to_registry(str(resolved))
        self.project_combo["values"] = projects

        errors = self._refresh()
        if errors:
            self.status_var.set(f"Loaded: {resolved.name} — but {errors[0]}")
        else:
            self.status_var.set(f"Loaded: {resolved.name}")

    @staticmethod
    def _anchor(project_path):
        """Project root for a GUI-picked path; the raw path if unresolvable."""
        try:
            from core.roots import anchor_project
            return anchor_project(project_path)
        except Exception:
            # why: the dashboard must still open a project if the resolver
            # cannot load; the raw path is the pre-v2.8.0 behaviour
            return project_path

    @staticmethod
    def _opt_out_notice(project_path):
        """Refusal text when the user picked an opted-out project, else None."""
        try:
            from core.modes import cli_opt_out_notice
            return cli_opt_out_notice(project_path)
        except Exception:
            # why: an unavailable opt-out check must not block the GUI; the
            # hooks and the MCP server enforce it independently on every write
            return None

    def _report_opt_out(self, project_path, notice):
        """Report a privacy opt-out as a SETTING, not as a failure.

        Routing this through `_report_project_error` produced an error dialog
        titled "Project unavailable" that blamed an unplugged drive and told
        the user to drop the entry with Manage… — a false cause and the wrong
        remedy. The project is fine and the entry should stay; the thing to
        change is config.json. Informational, not an error icon.
        """
        try:
            self.status_var.set(f"Opted out: {project_path}")
        except tk.TclError:
            # why: a torn-down root must not turn a reported setting into an
            # unreported exception
            pass
        messagebox.showinfo(
            "Project opted out of cc-memory",
            f"{project_path}\n\n{notice}\n\nNothing was created or read. The "
            f"entry stays in the project list — this is a standing setting, "
            f"not a failure, so removing the entry would not change it.")

    def _report_project_error(self, verb, project_path, err):
        """Surface a project-level failure in the status bar AND a dialog."""
        try:
            self.status_var.set(f"Could not {verb} {project_path}: {err}")
        except tk.TclError:
            # why: a torn-down root must not turn a reported error into a
            # second, unreported one
            pass
        messagebox.showerror(
            "Project unavailable",
            f"Could not {verb} this project:\n\n{project_path}\n\n{err}\n\n"
            "It is still in the project list — an unplugged drive or a "
            "disconnected share can come back, and cc-memory will not create a "
            "project directory that does not exist. Use Manage... to drop the "
            "entry for good.")

    def _refresh(self):
        """Repaint every tab. Returns a list of per-loader failure strings.

        Each loader is isolated. One broken tab used to poison the whole app:
        a user-confirmed `DROP TABLE memories` in the SQL console made
        _load_memories raise, which aborted _refresh before the other five
        loaders ran, left the status bar unset, and made EVERY later refresh
        raise too for the rest of the session.
        """
        if not self.db:
            return []
        errors = []
        for label, loader in (
            ("memories", self._load_memories),
            ("progress/plan", self._load_progress_plan),
            ("plans", self._load_plans),
            ("sessions", self._load_sessions),
            ("keywords", self._load_keywords),
            ("stats", self._load_stats),
        ):
            try:
                loader()
            except Exception as e:
                # why: a refresh only redraws what the user just did; naming
                # the tab that failed is useful, aborting the remaining five
                # tabs (and every future refresh) is not
                errors.append(f"{label} tab failed: {e}")
        return errors

    # ── Data Loading ─────────────────────────────────────────────────────────

    def _load_memories(self):
        if not self.db:
            return
        for item in self.mem_tree.get_children():
            self.mem_tree.delete(item)

        search = self.mem_search_var.get().strip()
        cat = self.mem_cat_var.get()
        try:
            min_imp = max(1, min(int(self.mem_imp_var.get()), 5))
        except (TypeError, ValueError):
            # why: a blank or hand-edited Min Imp used to raise ValueError
            # inside the Tk callback, which the --windowed exe shows nowhere
            # at all — the filter simply stopped responding
            min_imp = 1
            self.mem_imp_var.set("1")

        with self.db._connect() as conn:
            params = [self.project_id, min_imp]
            cat_clause = ""
            search_clause = ""

            if cat and cat != "all":
                cat_clause = "AND category = ?"
                params.append(cat)
            if search:
                # Same LIKE contract as every other search surface. Unescaped,
                # this box over-matched on any query containing a LIKE
                # metacharacter: `50%` also returned "500 units", `snake_case`
                # also returned "snakeXcase", and a one-character `%` or `_`
                # dumped every row in the project. core/db.py's `_like_escape`
                # is THE implementation (search_fts pairs it with ESCAPE '\'
                # at core/db.py:1216-1222, and CLI / MCP / web viewer all reach
                # it through there) — a second copy here would be one more
                # thing to keep in sync.
                search_clause = "AND content LIKE ? ESCAPE '\\'"
                params.append(f"%{MemoryDB._like_escape(search)}%")

            params.append(200)
            rows = conn.execute(
                f"""SELECT id, category, importance, content, created_at
                    FROM memories
                    WHERE project_id = ? AND is_active = 1 AND importance >= ?
                    {cat_clause} {search_clause}
                    ORDER BY importance DESC, created_at DESC LIMIT ?""",
                params
            ).fetchall()

        for r in rows:
            content = r["content"]
            if len(content) > 100:
                content = content[:97] + "..."
            date = r["created_at"][:10] if r["created_at"] else ""
            self.mem_tree.insert("", tk.END, values=(
                r["id"], r["category"], "*" * r["importance"], content, date
            ))

        self.status_var.set(f"Memories: {len(rows)} shown")

    def _load_plans(self):
        if not self.db:
            return
        for item in self.plan_tree.get_children():
            self.plan_tree.delete(item)

        plans = self.db.get_plans(self.project_id)
        for p in plans:
            content = p["content"]
            if len(content) > 80:
                content = content[:77] + "..."
            # Show result if done/failed, otherwise show feasibility
            info = ""
            if p.get("result"):
                info = p["result"]
            elif p.get("feasibility"):
                info = p["feasibility"]
            if len(info) > 50:
                info = info[:47] + "..."
            self.plan_tree.insert("", tk.END, values=(
                p["id"], p["exec_order"], p["status"], content, info
            ))

    def _load_sessions(self):
        if not self.db:
            return
        for item in self.sess_tree.get_children():
            self.sess_tree.delete(item)

        with self.db._connect() as conn:
            rows = conn.execute(
                "SELECT id, trigger_type, compacted_at, msg_count, archive_path "
                "FROM sessions WHERE project_id = ? ORDER BY compacted_at DESC LIMIT 50",
                (self.project_id,)
            ).fetchall()

        for r in rows:
            archive = Path(r["archive_path"]).name if r["archive_path"] else "-"
            self.sess_tree.insert("", tk.END, values=(
                r["id"], r["trigger_type"], r["compacted_at"][:16],
                r["msg_count"], archive
            ))

    def _load_keywords(self):
        if not self.db:
            return
        for item in self.kw_tree.get_children():
            self.kw_tree.delete(item)

        with self.db._connect() as conn:
            rows = conn.execute(
                "SELECT keyword, frequency, last_seen FROM keywords "
                "WHERE project_id = ? ORDER BY frequency DESC LIMIT 100",
                (self.project_id,)
            ).fetchall()

        for r in rows:
            self.kw_tree.insert("", tk.END, values=(
                r["keyword"], r["frequency"], r["last_seen"][:10]
            ))

    def _load_stats(self):
        if not self.db:
            return
        self.stats_text.delete("1.0", tk.END)

        stats = self.db.get_stats(self.project_id)
        text = f"""Project: {self.project_path.name}
Path: {self.project_path}

Sessions:      {stats['n_sessions']}
Memories:      {stats['n_memories']}
Active Plans:  {stats['n_active_plans']}
Last Session:  {stats['last_session'] or 'Never'}

Category Breakdown:
"""
        for row in stats["by_category"]:
            text += f"  {row['category']:<12} {row['n']:>4} entries  (avg importance {row['avg_imp']:.1f})\n"

        # Top keywords
        kws = self.db.get_top_keywords(self.project_id, 20)
        if kws:
            text += f"\nTop Keywords:\n  {', '.join(kws)}\n"

        # Critical memories
        critical = self.db.get_critical_memories(self.project_id, min_importance=5)
        if critical:
            text += f"\nCritical Memories ({len(critical)}):\n"
            for m in critical:
                text += f"  [{m['category']}] {m['content'][:80]}\n"

        self.stats_text.insert("1.0", text)

    def _load_progress_plan(self):
        """Render the `progress` row and the `plan_active` row (read-only).

        These are the SQL sources of truth behind memory/PROGRESS.md and
        memory/PLAN.md. Neither was reachable from this GUI before.
        """
        if not hasattr(self, "progress_text"):
            return
        # Marker neutralisation on EVERY stored slot below (register E3): this
        # tab renders memory content, LLM summaries and plan text — the same
        # model-reachable columns every other render path escapes — and it was
        # the one surface printing them raw. A copied-out live tag re-enters
        # a Claude session as an authority marker; escaped, it stays prose.
        try:
            from core.privacy import neutralize_inline as _ni, \
                neutralize_markers as _nm
        except Exception:
            # why: a read-only view must render even on a broken install;
            # over-escaping every '<' is the same fail-closed fallback the
            # CLI's _neutralize uses (register Y3)
            def _ni(t):
                return str(t).replace("<", "&lt;")
            _nm = _ni
        self.progress_text.config(state=tk.NORMAL)
        self.progress_text.delete("1.0", tk.END)
        if not self.db or self.project_id is None:
            self.progress_text.insert("1.0", "Load a project first.")
            self.progress_text.config(state=tk.DISABLED)
            return

        out = ["=" * 74,
               "PROGRESS   (SQL source of truth for memory/PROGRESS.md)",
               "=" * 74, ""]
        prog = self.db.get_progress(self.project_id)
        if not prog:
            out.append("(no progress row yet — PreCompact writes it at the first "
                       "compaction of this project)")
        else:
            out.append(f" 1. current_request  : {_ni(prog.get('current_request') or '(empty)')}")
            out.append(f" 2. status_done      : {_ni(prog.get('status_done') or '(none yet)')}")
            out.append(f" 3. status_in_flight : {_ni(prog.get('status_in_flight') or '(none)')}")
            out.append(f" 4. status_blocked   : {_ni(prog.get('status_blocked') or '(none)')}")

            todos = prog.get("open_todos") or []
            out.append(f" 5. open_todos       : {len(todos)}")
            for t in todos[:20]:
                if isinstance(t, dict):
                    mark = "[ ]" if t.get("status", "pending") == "pending" else "[~]"
                    out.append(f"        {mark} {_ni(str(t.get('priority', 'medium'))):<6} "
                               f"{_ni(str(t.get('content', '')))}")
                else:
                    # why: no shipped writer emits bare strings today, but the
                    # MCP progress_regenerate tool is a public surface that can
                    # — a read-only view must render them, not raise
                    out.append(f"        [ ] {_ni(str(t))}")

            plan_lines = _nm(prog.get("plan") or "").splitlines() or ["(none)"]
            out.append(f" 6. plan             : {plan_lines[0]}")
            for ln in plan_lines[1:20]:
                out.append(f"                       {ln}")

            crit = prog.get("critical_context") or []
            out.append(f" 7. critical_context : {len(crit)}")
            for m in crit[:10]:
                if isinstance(m, dict):
                    out.append(f"        #{m.get('id', '?')} [{_ni(str(m.get('category', '')))}] "
                               f"{_ni(str(m.get('content') or ''))[:90]}")
                else:
                    out.append(f"        {_ni(str(m))[:90]}")

            files = prog.get("files_touched") or []
            out.append(f" 8. files_touched    : {len(files)}")
            for f in files[:20]:
                if isinstance(f, dict):
                    out.append(f"        {_ni(str(f.get('action', '?'))):<8} {_ni(str(f.get('path', '')))}")
                else:
                    out.append(f"        {_ni(str(f))}")

            out.append(f" 9. transcript_ptr   : {_ni(prog.get('transcript_ptr') or '(none)')}")
            out.append(f"10. updated_at       : {prog.get('updated_at') or '-'}")
            out.append(f"11. trigger_type     : {prog.get('trigger_type') or '-'}")
            out.append(f"    session tag      : "
                       f"{prog.get('current_session_id') or '(untagged)'}"
                       f"  started {prog.get('session_started_at') or '-'}")

        out += ["", "=" * 74,
                "PLAN   (SQL source of truth for memory/PLAN.md)",
                "=" * 74, ""]
        pa = self.db.get_plan_active(self.project_id)
        if not pa:
            out.append("(no live plan — capture one with Claude's plan mode, "
                       "or `/cc-mem plan-set`)")
        else:
            structured = pa.get("structured")
            if not isinstance(structured, dict):
                structured = {}
            steps = structured.get("steps")
            steps = steps if isinstance(steps, list) else []
            active = pa.get("active_step") or 0

            if pa.get("needs_refine"):
                out.append("** PENDING REFINEMENT — this raw plan has not been through")
                out.append("** @plan-refiner, so the structured view below (and the")
                out.append("** generated PLAN.md) may still describe the PREVIOUS plan.")
                out.append("")
                out.append("Raw capture:")
                for ln in _nm(pa.get("raw") or "(empty)").splitlines()[:40]:
                    out.append(f"    {ln}")
                out.append("")

            if structured.get("goal"):
                out.append(f"Goal: {_ni(str(structured['goal']))}")
                sc = structured.get("success_criteria")
                if isinstance(sc, list) and sc:
                    out.append("Success criteria:")
                    for c in sc[:10]:
                        out.append(f"  - {_ni(str(c))}")
                done = sum(1 for s in steps
                           if isinstance(s, dict) and s.get("status") == "done")
                out.append("")
                out.append(f"Steps ({done}/{len(steps)} done):")
                glyphs = {"done": "[x]", "in_progress": "[~]", "pending": "[ ]",
                          "blocked": "[!]", "dropped": "[-]", "skipped": "[-]"}
                for s in steps:
                    if not isinstance(s, dict):
                        continue
                    glyph = glyphs.get(s.get("status", "pending"), "[ ]")
                    mark = ("  <-- ACTIVE"
                            if s.get("id") == active and s.get("status") != "done"
                            else "")
                    line = f"  {s.get('id', '?')}. {glyph} {_ni(str(s.get('title', '')))}{mark}"
                    if s.get("notes"):
                        line += f"  — {_ni(str(s['notes']))}"
                    out.append(line)
            elif not pa.get("needs_refine"):
                out.append("(no structured plan yet; raw capture follows)")
                for ln in _nm(pa.get("raw") or "(empty)").splitlines()[:40]:
                    out.append(f"    {ln}")

            out += ["",
                    f"active_step               : {active or 'none'}",
                    f"needs_refine              : {bool(pa.get('needs_refine'))}",
                    f"last_refined_at           : {pa.get('last_refined_at') or '-'}",
                    f"last_guardian_at          : {pa.get('last_guardian_at') or '-'}",
                    f"edits_since_last_guardian : {pa.get('edits_since_last_guardian', 0)}",
                    f"turns_since_last_guardian : {pa.get('turns_since_last_guardian', 0)}"]

        self.progress_text.insert("1.0", "\n".join(out))
        self.progress_text.config(state=tk.DISABLED)

    # ── SQL Console ──────────────────────────────────────────────────────────

    def _sql_table_hint(self) -> str:
        """Live table/view list from sqlite_master.

        The old hardcoded hint named 6 of the 17 objects a v2.4 database
        actually holds — it omitted every table added since v2.0 (observations,
        progress, plan_active, session_summaries, _migrations, memories_fts*).
        """
        try:
            with self.db._connect() as conn:
                names = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                    "ORDER BY name").fetchall()]
        except sqlite3.Error:
            # why: the hint is decoration on an error message; a second
            # failure must not replace the real SQL error the user needs
            return ""
        return ("\n\nTables: " + ", ".join(names)) if names else ""

    def _run_sql(self):
        if not self.db:
            messagebox.showwarning("No Project", "Load a project first.")
            return

        self.sql_output.delete("1.0", tk.END)
        query = self.sql_var.get().strip()
        if not query:
            return

        # `MemoryDB._connect()` is a context manager that COMMITS on a clean
        # exit (and, since v2.5.2, closes — it used to leak the handle). Either
        # way the `with` block below commits, so a destructive statement typed
        # here used to wipe the table and report "(no rows returned)" with no
        # prompt of any kind. Confirm every write, and report its rowcount.
        read_only = _sql_is_read_only(query)
        if not read_only:
            if self._optout_blocks_write():
                self.sql_output.insert("1.0", "(refused — project is opted "
                                              "out via excluded_projects)")
                return
            proj = self.project_path.name if self.project_path else "this project"
            preview = query if len(query) <= 400 else query[:400] + "..."
            if not messagebox.askyesno(
                "Confirm write statement",
                f"This is NOT a read-only query. It will be COMMITTED to "
                f"{proj}'s memory database and CANNOT be undone:\n\n"
                f"{preview}\n\n"
                f"Run it anyway?"):
                self.sql_output.insert("1.0", "(cancelled — nothing was executed)")
                self.status_var.set("SQL write cancelled")
                return

        try:
            if read_only:
                # ENGINE-enforced (register E2): `_sql_is_read_only` decides
                # whether to CONFIRM; it must never be what decides whether a
                # write can happen. A single-statement CTE-DML shape passed
                # it as read-only and SQLite committed the DELETE with no
                # dialog (measured). On core.db.readonly_connect's mode=ro
                # connection the same statement fails inside the engine, and
                # the authorizer refuses ATTACH — the one road out of ro.
                from core.db import readonly_connect
                conn = readonly_connect(self.db.db_path)
                try:
                    cur = conn.execute(query)
                    rows = cur.fetchall()
                    affected = cur.rowcount
                finally:
                    conn.close()
            else:
                with self.db._connect() as conn:
                    cur = conn.execute(query)
                    rows = cur.fetchall()
                    affected = cur.rowcount
        except sqlite3.Error as e:
            self.sql_output.insert("1.0", f"SQL Error: {e}{self._sql_table_hint()}")
            return

        if not read_only:
            shown = affected if isinstance(affected, int) and affected >= 0 else "n/a"
            self.sql_output.insert(
                "1.0", f"Statement executed and COMMITTED.\nRows affected: {shown}")
            # A confirmed-but-destructive DDL used to take the whole app down
            # with it: `DROP TABLE memories` made this _refresh() raise from
            # its first loader, so the status line below never ran and every
            # later refresh raised too. _refresh is now per-loader isolated and
            # hands back what broke instead of propagating it.
            errors = self._refresh()
            note = f"SQL write committed (rowcount={shown})"
            if errors:
                note += f" — {errors[0]}"
                self.sql_output.insert(
                    tk.END,
                    "\n\nWARNING — the database no longer matches what this "
                    "dashboard expects:\n  " + "\n  ".join(errors))
            self.status_var.set(note)
            return

        if not rows:
            self.sql_output.insert("1.0", "(no rows returned)")
            return

        headers = list(rows[0].keys())
        # Calculate column widths
        widths = [len(h) for h in headers]
        str_rows = []
        for r in rows:
            sr = [str(v) if v is not None else "NULL" for v in list(r)]
            str_rows.append(sr)
            for i, c in enumerate(sr):
                widths[i] = max(widths[i], min(len(c), 50))

        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        output = fmt.format(*headers) + "\n"
        output += "  ".join("-" * w for w in widths) + "\n"
        for sr in str_rows:
            truncated = [c[:widths[i]] for i, c in enumerate(sr)]
            output += fmt.format(*truncated) + "\n"
        output += f"\n({len(rows)} rows)"

        self.sql_output.insert("1.0", output)

    # ── Tidy Memories (LLM-powered cleanup) ─────────────────────────────────

    def _tidy_memories(self):
        """Use Haiku API to review all memories and suggest cleanup."""
        if not self.db or not self.project_path:
            messagebox.showwarning("No Project", "Load a project first.")
            return

        api_key = self._get_api_key()
        if not api_key:
            messagebox.showerror(
                "No API Key",
                "No API key found.\n\n"
                "Set ANTHROPIC_API_KEY env var, or click Settings to enter one manually.")
            return

        # Load all active memories
        with self.db._connect() as conn:
            rows = conn.execute(
                "SELECT id, category, importance, content, created_at "
                "FROM memories WHERE project_id = ? AND is_active = 1 "
                "ORDER BY importance DESC, id",
                (self.project_id,)
            ).fetchall()

        if not rows:
            messagebox.showinfo("Empty", "No memories to tidy.")
            return

        # Build memory list for LLM
        mem_lines = []
        for r in rows:
            mem_lines.append(
                f"ID:{r['id']} | {r['category']} | imp={r['importance']} | {r['content']}"
            )
        mem_text = "\n".join(mem_lines)

        self.status_var.set("Analyzing memories with LLM...")
        self.root.update()

        # Call Haiku API
        prompt = f"""\
Review these {len(rows)} memories from a project database. For each memory, decide:
- KEEP: valuable, unique, self-contained information
- DELETE: garbage, noise, debug output, fragments, duplicates, or meta-discussion about the memory system itself
- MERGE: two or more memories that say the same thing (keep the better one, delete others)

Output a JSON object with:
- "delete": list of memory IDs to delete, with brief reason
- "merge": list of {{"keep_id": int, "delete_ids": [int], "reason": str}}
- "summary": one sentence summary of what was cleaned

Be aggressive about removing noise. Only KEEP memories that would be genuinely useful in a future conversation.

Memories:
{mem_text}"""

        try:
            from llm.ccl_backend import call_llm
            from llm.parse import extract_json
            text_content = call_llm(
                "You are a memory database curator. Output ONLY valid JSON, no markdown.",
                prompt, api_key, max_tokens=3000, timeout=25,
            )
            analysis = extract_json(text_content, kind="object")
            if analysis is None:
                raise ValueError("the model returned no parseable JSON object")
        except Exception as e:
            self.status_var.set("Ready")
            messagebox.showerror("API Error", f"LLM analysis failed:\n\n{e}")
            return

        # Collect all IDs to delete. EVERY value below is LLM-controlled and
        # none of it is covered by the try/except above (which wraps only
        # call_llm + json.loads), so plausible model output used to raise
        # straight out of this Tk callback — no dialog, no traceback the
        # --windowed exe could show, status bar frozen on "Analyzing memories
        # with LLM...". Verified live: [1,2,3] -> AttributeError,
        # {"id":"abc"} -> ValueError, delete_ids:[null] -> TypeError.
        if not isinstance(analysis, dict):
            self.status_var.set("Ready")
            messagebox.showerror(
                "Unusable LLM output",
                f"The model returned a JSON {type(analysis).__name__}, not the "
                'object with "delete" / "merge" / "summary" keys it was asked '
                "for.\n\nNothing was changed.")
            return

        def _as_id(value):
            """An LLM value -> a positive int row id, or None if it is not one."""
            if value is None or isinstance(value, bool):
                return None
            if isinstance(value, int):
                return value if value > 0 else None
            if isinstance(value, float):
                return int(value) if value > 0 and value.is_integer() else None
            if isinstance(value, str):
                try:
                    n = int(value.strip().lstrip("#").strip())
                except ValueError:
                    return None
                return n if n > 0 else None
            return None

        delete_ids = set()
        reasons = {}
        malformed = 0

        raw_delete = analysis.get("delete") or []
        if not isinstance(raw_delete, list):
            raw_delete, malformed = [], malformed + 1
        for item in raw_delete:
            if isinstance(item, dict):
                did = _as_id(item.get("id", item.get("ID")))
                reason = item.get("reason", "")
                reason = reason if isinstance(reason, str) else str(reason)
            else:
                did, reason = _as_id(item), ""
            if did is None:
                malformed += 1
                continue
            delete_ids.add(did)
            if reason:
                reasons[did] = reason

        raw_merge = analysis.get("merge") or []
        if not isinstance(raw_merge, list):
            raw_merge, malformed = [], malformed + 1
        for merge in raw_merge:
            if not isinstance(merge, dict):
                malformed += 1
                continue
            keep = _as_id(merge.get("keep_id"))
            dids = merge.get("delete_ids")
            if not isinstance(dids, list):
                dids = [dids]
            for raw in dids:
                did = _as_id(raw)
                if did is None or did == keep:
                    # why: `did == keep` means the model listed the row it
                    # elected to KEEP among the rows to archive — obeying that
                    # would retire the surviving half of its own merge
                    malformed += 1
                    continue
                delete_ids.add(did)
                reasons[did] = f"Merged into #{keep if keep else '?'}"

        # Only ids that are actually on screen can be reviewed. The confirm
        # dialog skipped the rest while its button still counted them, so
        # {"delete":[{"id":99999}]} produced a dialog with zero checkboxes and
        # a button reading "Archive Selected (1)".
        known = {r["id"] for r in rows}
        unknown = sorted(i for i in delete_ids if i not in known)
        delete_ids &= known

        notes = []
        if unknown:
            listed = ", ".join("#%d" % i for i in unknown[:8])
            notes.append(f"{len(unknown)} suggested id(s) are not active "
                         f"memories of this project and were ignored ({listed})")
        if malformed:
            notes.append(f"{malformed} malformed entr"
                         f"{'y' if malformed == 1 else 'ies'} ignored")

        if not delete_ids:
            self.status_var.set("Ready")
            if notes:
                messagebox.showwarning(
                    "Nothing to archive",
                    "The model's reply contained no usable memory id.\n\n- "
                    + "\n- ".join(notes))
            else:
                messagebox.showinfo("All Clean", "LLM found no garbage to remove.")
            return

        summary = analysis.get("summary", "")
        if not isinstance(summary, str):
            summary = str(summary)
        if notes:
            summary = (summary + "\n" if summary else "") + " | ".join(notes)

        # Show confirmation dialog
        self._show_tidy_confirm(rows, delete_ids, reasons, summary)

    def _show_tidy_confirm(self, all_rows, delete_ids, reasons, summary):
        """Show dialog with LLM suggestions for user to confirm."""
        id_to_row = {r["id"]: r for r in all_rows}
        # A row that is not in all_rows cannot be rendered, and a button
        # labelled "Archive Selected (1)" above an empty list is a trap: it
        # promised an action it could not perform and then silently destroyed
        # the dialog. Count only what is actually on screen — and if that is
        # nothing, say so instead of opening an empty dialog.
        shown_ids = [mid for mid in sorted(delete_ids) if mid in id_to_row]
        if not shown_ids:
            self.status_var.set("Ready")
            messagebox.showinfo(
                "Nothing to archive",
                "None of the suggested memories are in this project's active "
                "list, so there is nothing to review.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Tidy Memories — Review")
        dlg.geometry("850x600")
        dlg.transient(self.root)
        dlg.grab_set()

        # Summary
        ttk.Label(dlg, text=f"LLM suggests removing {len(shown_ids)} of "
                            f"{len(all_rows)} memories",
                  font=("", 12, "bold")).pack(pady=(10, 2))
        if summary:
            ttk.Label(dlg, text=summary, wraplength=780, font=("", 9)).pack(pady=(0, 8))

        # Scrollable list with checkboxes
        frame = ttk.LabelFrame(dlg, text="Memories to archive (uncheck to keep)",
                               padding=8)
        frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        canvas = tk.Canvas(frame)
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        check_vars = []  # (BooleanVar, memory_id, content the verdict saw)

        for mid in shown_ids:
            row = id_to_row[mid]

            var = tk.BooleanVar(value=True)
            rf = ttk.Frame(scroll_frame)
            rf.pack(fill=tk.X, pady=1)

            ttk.Checkbutton(rf, variable=var).pack(side=tk.LEFT)

            content = row["content"][:90].replace("\n", " ")
            reason = reasons.get(mid, "")
            label_text = f"#{mid} [{row['category']}|{'*'*row['importance']}] {content}"
            ttk.Label(rf, text=label_text, wraplength=500, font=("Consolas", 9)).pack(side=tk.LEFT, padx=3)

            if reason:
                ttk.Label(rf, text=f"({reason})", foreground="gray",
                          font=("", 8)).pack(side=tk.LEFT, padx=3)

            # FULL content, not the 90-char display slice: the guard below
            # compares against exactly what the tidy scan judged.
            check_vars.append((var, mid, row["content"]))

        # Buttons
        bf = ttk.Frame(dlg, padding=8)
        bf.pack(fill=tk.X, padx=15)

        def do_delete():
            if self._optout_blocks_write():
                dlg.destroy()
                return
            picked = [(mid, content) for var, mid, content in check_vars
                      if var.get()]
            if not picked:
                dlg.destroy()
                return

            # Archive (is_active = 0) instead of DELETE — see the anti-patch
            # contract. And CONDITIONAL on the content this dialog showed
            # (register X7): the verdict was computed from a snapshot taken
            # before the user finished reading the dialog, and a row repaired
            # by a concurrent hook in that window was archived anyway —
            # measured, "old garbage..." shown, "valuable repaired content"
            # archived. archive_if_unchanged makes the stale tick a no-op,
            # and the count reported below is what actually happened.
            n = self.db.archive_if_unchanged(picked)

            dlg.destroy()
            # MEMORY.md is a GENERATED artifact. _refresh() only repaints the
            # trees, so without this the file kept advertising the retired
            # rows (header count included) until the next hook run.
            regenerate_memory_index(self.db, self.project_id,
                                    self.project_path / "memory")
            self._refresh()
            skipped = len(picked) - n
            self.status_var.set(f"Archived {n} memories"
                                + (f" ({skipped} changed since review, kept)"
                                   if skipped else ""))
            messagebox.showinfo(
                "Done",
                f"Archived {n} of {len(picked)} selected (is_active = 0).\n"
                + (f"{skipped} were modified while this dialog was open and "
                   f"were KEPT — re-run Tidy to review their new content.\n"
                   if skipped else "")
                + "\nRows are retired, not erased — supersedes_id provenance "
                  "chains stay intact and MEMORY.md has been regenerated.")

        # len(check_vars), never len(delete_ids): the label must count the
        # checkboxes the user can actually see and untick.
        ttk.Button(bf, text=f"Archive Selected ({len(check_vars)})",
                   command=do_delete).pack(side=tk.RIGHT, padx=5)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        self.status_var.set("Ready")

    # ── Dialogs ──────────────────────────────────────────────────────────────

    def _optout_blocks_write(self):
        """True (plus a dialog) when this project is opted out RIGHT NOW.

        Re-checked at write time, not only at project load (register r6-C3):
        the dashboard runs for hours over a cached MemoryDB, and a project
        added to config.json excluded_projects after load kept accepting
        Add-Memory / Save-Session / Tidy / SQL writes. Reads stay available —
        the opt-out's contract is that nothing NEW is recorded.
        """
        try:
            # cli_opt_out_notice, NOT a direct is_excluded call: the shared
            # gate is what keeps every surface's opt-out spelling identical
            # (three inline copies are how it drifted before), and
            # test_surfaces enforces exactly this routing.
            from core.modes import cli_opt_out_notice
        except Exception:
            # why: a broken opt-out import must not brick the GUI; the hooks
            # enforce the same control on every hook path regardless
            return False
        notice = (cli_opt_out_notice(str(self.project_path))
                  if self.project_path else None)
        if notice:
            messagebox.showwarning("cc-memory", notice)
            return True
        return False

    def _add_memory_dialog(self):
        if not self.db:
            # Was a silent no-op while Tidy / Save Session warn — the button
            # simply appeared broken.
            messagebox.showwarning("No Project", "Load a project first.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Add Memory")
        dlg.geometry("520x340")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="Category:").grid(row=0, column=0, padx=10, pady=5, sticky=tk.W)
        cat_var = tk.StringVar(value="note")
        ttk.Combobox(dlg, textvariable=cat_var, width=15,
                     values=list(CATEGORIES),
                     state="readonly").grid(row=0, column=1, padx=10, pady=5, sticky=tk.W)

        ttk.Label(dlg, text="Importance:").grid(row=1, column=0, padx=10, pady=5, sticky=tk.W)
        imp_var = tk.StringVar(value="3")
        # readonly: clearing this field used to raise ValueError inside the Tk
        # callback — swallowed to a stderr the --windowed exe does not have, so
        # the dialog just sat there and nothing was ever saved.
        ttk.Spinbox(dlg, textvariable=imp_var, from_=1, to=5, width=5,
                    state="readonly").grid(row=1, column=1, padx=10, pady=5, sticky=tk.W)

        ttk.Label(dlg, text="Content:").grid(row=2, column=0, padx=10, pady=5, sticky=tk.NW)
        content_text = tk.Text(dlg, height=8, width=50)
        content_text.grid(row=2, column=1, padx=10, pady=5)

        def save():
            if self._optout_blocks_write():
                return
            content = content_text.get("1.0", tk.END).strip()
            if not content:
                messagebox.showwarning("Empty", "Enter some content first.")
                return
            try:
                importance = max(1, min(int(imp_var.get()), 5))
            except (TypeError, ValueError):
                # why: defence in depth behind state="readonly" — a
                # programmatically-set variable can still hold garbage, and
                # this callback must never die silently
                importance = 3
            # Anti-patch: route through upsert_smart so MERGE/SUPERSEDE/INSERT
            # is decided by similarity, not by the caller. See docs/CONTRACTS.md#anti-patch-contract.
            result = upsert_smart(
                self.db, self.project_id, None,
                category=cat_var.get(),
                content=content,
                importance=importance,
                tags=["manual", "dashboard"],
            )
            regenerate_memory_index(self.db, self.project_id, self.project_path / "memory")
            msg = f"Add Memory: {result['action']} #{result.get('id')}"
            sim = result.get("similarity")
            # Only report a similarity that was actually COMPUTED. The writer
            # returns `sim if similar else 0.0` (llm/memory_writer.py:157) and
            # _find_similar only keeps a candidate on `s > best_sim`
            # (:87-92), so a genuine comparison result is always > 0 — a 0.0
            # here means no comparison happened at all, and printing
            # "(sim=0.00)" invented a measurement that was never taken.
            if isinstance(sim, (int, float)) and not isinstance(sim, bool) and sim > 0:
                msg += f" (sim={sim:.2f})"
            if result.get("reason"):
                msg += f" [{result['reason']}]"
            dlg.destroy()
            # _load_memories() ends by setting the status bar to
            # "Memories: N shown", so reporting the outcome BEFORE it meant the
            # MERGE / SUPERSEDE / INSERT feedback was overwritten before anyone
            # could read it. Repaint first, report last — the order do_delete
            # already gets right.
            self._load_memories()
            self.status_var.set(msg)

        bf = ttk.Frame(dlg)
        bf.grid(row=3, column=1, pady=10, sticky=tk.W)
        ttk.Button(bf, text="Save", command=save).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

    def _add_plan_dialog(self):
        if not self.db:
            messagebox.showwarning("No Project", "Load a project first.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Add Plans")
        dlg.geometry("600x400")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="Enter plans (one per line):").pack(padx=10, pady=5, anchor=tk.W)
        plan_text = tk.Text(dlg, height=15, width=70)
        plan_text.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)

        def save():
            lines = plan_text.get("1.0", tk.END).strip().split("\n")
            lines = [l.strip() for l in lines if l.strip()]
            if not lines:
                messagebox.showwarning("Empty", "Enter at least one plan line.")
                return
            for content in lines:
                self.db.add_plan(self.project_id, content)
            dlg.destroy()
            self._load_plans()
            self.status_var.set(f"Added {len(lines)} plan(s)")

        bf = ttk.Frame(dlg)
        bf.pack(pady=10)
        ttk.Button(bf, text="Add All", command=save).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

    def _approve_plans(self):
        if not self.db:
            return
        selected = self.plan_tree.selection()
        # project_id scopes every UPDATE below. `plans.id` is unique per DB
        # FILE, not per project, and one memory.db can hold several projects
        # (core/db.py:1306-1314), so an unscoped id rewrote whatever row owned
        # it — including another project's status and result columns. Scoped, a
        # stale or foreign id matches nothing and the rowcount says so.
        done = 0
        for item in selected:
            values = self.plan_tree.item(item, "values")
            plan_id = int(values[0])
            done += self.db.update_plan_status(
                plan_id, "ready", project_id=self.project_id) or 0
        self._load_plans()
        if selected:
            self.status_var.set(f"Approved {done} of {len(selected)} plan(s)")

    def _approve_all_plans(self):
        if not self.db:
            return
        plans = self.db.get_plans(self.project_id, statuses=["draft", "evaluating"])
        done = 0
        for p in plans:
            done += self.db.update_plan_status(
                p["id"], "ready", project_id=self.project_id) or 0
        self._load_plans()
        self.status_var.set(f"Approved {done} of {len(plans)} plan(s)")

    def _clear_done_plans(self):
        if not self.db:
            return
        n = self.db.clear_done_plans(self.project_id)
        self._load_plans()
        self.status_var.set(f"Cleared {n} completed plan(s)")

    def _get_selected_plan_ids(self):
        """Get list of selected plan IDs from treeview."""
        return [int(self.plan_tree.item(item, "values")[0])
                for item in self.plan_tree.selection()]

    def _execute_plans(self):
        """Launch Claude Code CLI with selected plan content."""
        if not self.db:
            return
        ids = self._get_selected_plan_ids()
        if not ids:
            messagebox.showinfo("No Selection", "Select plan(s) to execute.")
            return

        # Gather plan contents
        plans_text = []
        for pid in ids:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT content FROM plans WHERE id = ?", (pid,)
                ).fetchone()
                if row:
                    plans_text.append(row["content"])

        if not plans_text:
            return

        # Build the prompt for Claude Code
        if len(plans_text) == 1:
            prompt = plans_text[0]
        else:
            prompt = "Execute these tasks in order:\n" + "\n".join(
                f"{i+1}. {t}" for i, t in enumerate(plans_text))

        # Add project context
        proj_dir = str(self.project_path) if self.project_path else ""

        if not messagebox.askyesno(
            "Execute in Claude Code",
            f"Launch Claude Code with this plan?\n\n"
            f"{prompt[:300]}{'...' if len(prompt) > 300 else ''}\n\n"
            f"Project: {proj_dir}"):
            return

        # Launch Claude Code in a new console window FIRST. The old order
        # marked every selected plan `executing` before Popen and the except
        # branch never rolled it back, so a missing `claude` binary left the
        # plans wedged in `executing` with nothing running.
        try:
            kwargs = {"cwd": proj_dir}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(["claude", prompt], **kwargs)
        except Exception as e:
            self._load_plans()
            self.status_var.set("Launch failed — plan statuses unchanged")
            messagebox.showerror("Error", f"Failed to launch Claude Code:\n\n{e}\n\n"
                                 "Make sure 'claude' is on your PATH.")
            return

        marked = 0
        for pid in ids:
            marked += self.db.update_plan_status(
                pid, "executing", project_id=self.project_id) or 0
        self._load_plans()
        self.status_var.set(
            f"Launched Claude Code for {len(ids)} plan(s) "
            f"({marked} marked executing)")

    def _mark_plan_done(self):
        """Mark selected plans as done, optionally with a result note."""
        if not self.db:
            return
        ids = self._get_selected_plan_ids()
        if not ids:
            messagebox.showinfo("No Selection", "Select plan(s) to mark done.")
            return

        # Ask for optional result note
        dlg = tk.Toplevel(self.root)
        dlg.title("Mark Done")
        dlg.geometry("450x150")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text=f"Result note for {len(ids)} plan(s) (optional):").pack(
            padx=10, pady=(10, 5), anchor=tk.W)
        result_var = tk.StringVar()
        ttk.Entry(dlg, textvariable=result_var, width=55).pack(padx=10, fill=tk.X)

        def do_done():
            note = result_var.get().strip()
            n = 0
            for pid in ids:
                n += self.db.update_plan_status(
                    pid, "done", note or None, field="result",
                    project_id=self.project_id) or 0
            dlg.destroy()
            self._load_plans()
            self.status_var.set(f"Marked {n} of {len(ids)} plan(s) done")

        bf = ttk.Frame(dlg)
        bf.pack(pady=10)
        ttk.Button(bf, text="Done", command=do_done).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

    def _mark_plan_failed(self):
        """Mark selected plans as failed."""
        if not self.db:
            return
        ids = self._get_selected_plan_ids()
        if not ids:
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Mark Failed")
        dlg.geometry("450x150")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text=f"Failure reason for {len(ids)} plan(s) (optional):").pack(
            padx=10, pady=(10, 5), anchor=tk.W)
        reason_var = tk.StringVar()
        ttk.Entry(dlg, textvariable=reason_var, width=55).pack(padx=10, fill=tk.X)

        def do_fail():
            reason = reason_var.get().strip()
            n = 0
            for pid in ids:
                n += self.db.update_plan_status(
                    pid, "failed", reason or None, field="result",
                    project_id=self.project_id) or 0
            dlg.destroy()
            self._load_plans()
            self.status_var.set(f"Marked {n} of {len(ids)} plan(s) failed")

        bf = ttk.Frame(dlg)
        bf.pack(pady=10)
        ttk.Button(bf, text="Mark Failed", command=do_fail).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

    def _skip_plans(self):
        """Skip selected plans."""
        if not self.db:
            return
        ids = self._get_selected_plan_ids()
        n = 0
        for pid in ids:
            n += self.db.update_plan_status(
                pid, "skipped", project_id=self.project_id) or 0
        self._load_plans()
        self.status_var.set(f"Skipped {n} of {len(ids)} plan(s)")

    def _edit_plan_dialog(self):
        """Edit the content of a selected plan."""
        if not self.db:
            return
        ids = self._get_selected_plan_ids()
        if len(ids) != 1:
            messagebox.showinfo("Select One", "Select exactly one plan to edit.")
            return
        plan_id = ids[0]

        # Get current plan data
        plans = self.db.get_plans(self.project_id)
        plan = next((p for p in plans if p["id"] == plan_id), None)
        if not plan:
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Edit Plan #{plan_id}")
        dlg.geometry("600x350")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text=f"Status: {plan['status']}  |  Order: {plan['exec_order']}",
                  font=("", 9)).pack(padx=10, pady=(10, 5), anchor=tk.W)

        ttk.Label(dlg, text="Content:").pack(padx=10, anchor=tk.W)
        content_text = tk.Text(dlg, height=6, width=70, font=("Consolas", 10))
        content_text.pack(padx=10, pady=5, fill=tk.X)
        content_text.insert("1.0", plan["content"])

        ttk.Label(dlg, text="Evaluation notes:").pack(padx=10, anchor=tk.W)
        feas_var = tk.StringVar(value=plan.get("feasibility") or "")
        ttk.Entry(dlg, textvariable=feas_var, width=70).pack(padx=10, fill=tk.X)

        ttk.Label(dlg, text="Result:").pack(padx=10, pady=(5, 0), anchor=tk.W)
        result_var = tk.StringVar(value=plan.get("result") or "")
        ttk.Entry(dlg, textvariable=result_var, width=70).pack(padx=10, fill=tk.X)

        def save():
            new_content = content_text.get("1.0", tk.END).strip()
            if new_content and new_content != plan["content"]:
                self.db.update_plan_content(plan_id, new_content,
                                            project_id=self.project_id)
            new_feas = feas_var.get().strip()
            if new_feas != (plan.get("feasibility") or ""):
                self.db.update_plan_status(plan_id, plan["status"], new_feas,
                                           field="feasibility",
                                           project_id=self.project_id)
            new_result = result_var.get().strip()
            if new_result != (plan.get("result") or ""):
                self.db.update_plan_status(plan_id, plan["status"], new_result,
                                           field="result",
                                           project_id=self.project_id)
            dlg.destroy()
            self._load_plans()

        bf = ttk.Frame(dlg)
        bf.pack(pady=10)
        ttk.Button(bf, text="Save", command=save).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

    def _delete_plans(self):
        """Delete selected plans."""
        if not self.db:
            return
        ids = self._get_selected_plan_ids()
        if not ids:
            return
        if not messagebox.askyesno("Delete Plans",
                                    f"Delete {len(ids)} plan(s)? This cannot be undone."):
            return
        # project_id scopes the DELETE: plans.id is global to the DB file, and
        # one memory.db can hold several projects (this dashboard switches
        # between them), so an unscoped id can reach another project's row.
        deleted = sum(self.db.delete_plan(pid, project_id=self.project_id)
                      for pid in ids)
        self._load_plans()
        if deleted != len(ids):
            self.status_var.set(
                f"Deleted {deleted} of {len(ids)} plan(s) — "
                f"{len(ids) - deleted} did not belong to this project")
        else:
            self.status_var.set(f"Deleted {deleted} plan(s)")

    def _plan_context_menu(self, event):
        """Show right-click context menu on plan tree."""
        item = self.plan_tree.identify_row(event.y)
        if item:
            if item not in self.plan_tree.selection():
                self.plan_tree.selection_set(item)
            self.plan_menu.post(event.x_root, event.y_root)

    _EXTRACTION_PROMPT = """\
You are a memory extraction system. Given a Claude Code conversation transcript, extract the most important information worth remembering across sessions.

Output a JSON array of objects: {"category": str, "content": str, "importance": int}
- category: """ + "|".join(CATEGORIES) + """
- content: one concise, self-contained sentence with specific values
- importance: 1-5 (5=critical, 4=important, 3=useful)

Rules: Only conclusions, not process. Self-contained. Specific values. 5-15 items max.
Output ONLY valid JSON array."""

    def _build_transcript_summary(self, messages, max_chars=12000):
        """Condensed transcript for the LLM prompt. Delegates to THE one
        implementation in core.extractor (register M2) — this was the third
        near-identical copy, and the only one still filling from the OLDEST
        record, i.e. still carrying the staleness bug v2.4.2 fixed in the
        other two."""
        from core.extractor import summarize_transcript
        return summarize_transcript(messages, max_chars=max_chars)

    def _extract_via_llm(self, messages, api_key):
        """Call Haiku API for structured extraction. Returns list of dicts or None."""
        transcript_text = self._build_transcript_summary(messages)
        if len(transcript_text) < 100:
            return None

        try:
            from llm.ccl_backend import call_llm
            from llm.parse import extract_json
            text_content = call_llm(
                self._EXTRACTION_PROMPT,
                f"Extract memories:\n\n{transcript_text}",
                api_key, max_tokens=2000, timeout=25,
            )
            memories = extract_json(text_content, kind="array")
            if memories is None:
                return None

            valid = []
            for m in memories:
                if not isinstance(m, dict):
                    continue
                cat = m.get("category", "note")
                content = m.get("content", "").strip()
                imp = m.get("importance", 3)
                if not content or len(content) < 10:
                    continue
                if cat not in CATEGORIES:
                    cat = "note"
                valid.append({"category": cat, "content": content,
                              "importance": max(1, min(int(imp), 5))})
            return valid if valid else None
        except Exception:
            # why: deliberately broad, and `None` is a documented outcome, not a
            # swallowed bug — this is the OPTIONAL leg of a two-leg extraction
            # and the caller (`_save_current_session`, the `if api_key:` branch)
            # reads None as "fall back to the regex extractor", which needs no
            # credentials and always works. The failure surface genuinely is
            # open-ended: ImportError on a partial install, urllib / ssl /
            # socket errors and RuntimeError out of call_llm when every
            # credential leg fails, ValueError from json.loads, and
            # AttributeError / TypeError / ValueError from the model's own
            # untrusted fields (`content` not a str, `importance` not a
            # number). All of them mean one thing here — no LLM result — and
            # raising instead would kill "Save Session" from inside a Tk
            # callback, which the --windowed exe cannot show at all.
            return None

    def _save_current_session(self):
        """Manually save memories from the most recent Claude Code transcript."""
        if not self.db or not self.project_path:
            messagebox.showwarning("No Project", "Load a project first.")
            return

        # Find the transcript directory for this project
        transcript_dir = _find_transcript_dir(self.project_path)
        if not transcript_dir:
            messagebox.showerror(
                "No Transcripts",
                f"Could not find Claude Code transcript directory for:\n"
                f"{self.project_path}\n\n"
                f"Looked in: {Path.home() / '.claude' / 'projects'}\n\n"
                f"Only an exact (case-insensitive) match on the project path is\n"
                f"accepted. cc-memory will not guess a directory by name — that\n"
                f"is how another project's transcript used to get ingested here.")
            return

        # Find the most recent JSONL file
        jsonl_files = sorted(
            transcript_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not jsonl_files:
            messagebox.showinfo("No Transcripts", "No JSONL transcript files found.")
            return

        latest = jsonl_files[0]
        mtime = datetime.fromtimestamp(latest.stat().st_mtime)

        # Confirm with user. The matched DIRECTORY is shown, not just a UUID
        # filename: it is the only thing that makes a wrong match visible
        # before an unrelated project's history is written into this DB.
        if not messagebox.askyesno(
            "Save Session",
            f"Extract memories from the most recent transcript?\n\n"
            f"Transcript dir: {transcript_dir}\n"
            f"File: {latest.name}\n"
            f"Modified: {mtime.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Size: {latest.stat().st_size / 1024:.0f} KB\n\n"
            f"Memories will be written to: {self.project_path / 'memory'}"):
            return

        try:
            # Parsing runs on the Tk main thread (the extractor is stdlib-only
            # and deliberately synchronous), so give the user a busy cursor and
            # a size hint rather than a frozen window.
            self._set_busy(True, f"Parsing transcript {latest.name} "
                                 f"({latest.stat().st_size / 1024:.0f} KB)...")
            messages = load_transcript(str(latest))
            if not messages:
                self._set_busy(False, "Ready")
                messagebox.showinfo("Empty", "Transcript is empty or could not be parsed.")
                return

            if self._optout_blocks_write():
                self._set_busy(False, "Ready")
                return
            self._set_busy(True, "Extracting memories...")

            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            file_ts = now.strftime("%Y%m%d_%H%M%S")

            # Anti-patch: all writes go through upsert_batch which calls
            # upsert_smart per item (MERGE/SUPERSEDE/INSERT by similarity)
            # and regenerates MEMORY.md once at the end. The manual dedup
            # set is no longer needed — the writer handles it via hash +
            # similarity. See docs/CONTRACTS.md#anti-patch-contract.

            # Try LLM extraction first, fallback to regex
            memories = None
            method = "regex"
            api_key = self._get_api_key()
            if api_key:
                memories = self._extract_via_llm(messages, api_key)
                if memories:
                    method = "llm"

            if memories:
                session_id = self.db.insert_session(
                    project_id=self.project_id,
                    claude_session_id=latest.stem,
                    trigger_type=f"manual_dashboard_{method}",
                    msg_count=len(messages),
                    archive_path=f"sessions/{now.strftime('%Y/%m')}/session_{file_ts}.md",
                    brief_summary=f"Manual save ({method}) at {timestamp}",
                )
                batch = [
                    {"category": m["category"], "content": m["content"],
                     "importance": m["importance"], "tags": [method, "manual"]}
                    for m in memories
                ]
            else:
                project_kw = self.db.get_top_keywords(self.project_id, 40)
                ext = build_extraction(messages, project_kw)
                session_id = self.db.insert_session(
                    project_id=self.project_id,
                    claude_session_id=latest.stem,
                    trigger_type="manual_dashboard_regex",
                    msg_count=ext["msg_count"],
                    archive_path=f"sessions/{now.strftime('%Y/%m')}/session_{file_ts}.md",
                    brief_summary=f"Manual save (regex) at {timestamp}",
                )
                cat_base_imp = {
                    "decision": 3, "result": 3, "arch": 3,
                    "config": 2, "bug": 4, "task": 2, "note": 1,
                }
                grouped = group_sentences(ext["sentences"])
                batch = []
                for cat, items in grouped.items():
                    base = cat_base_imp.get(cat, 2)
                    for text, imp in items[:10]:
                        batch.append({
                            "category": cat, "content": text,
                            "importance": min(max(imp, base), 5),
                            "tags": ["regex", "manual"],
                        })
                for metric in ext["metrics"][:10]:
                    batch.append({
                        "category": "result", "content": metric,
                        "importance": 3, "tags": ["metric", "manual"],
                    })
                if ext.get("keywords"):
                    self.db.upsert_keywords(self.project_id, ext["keywords"])

            counts = upsert_batch(
                self.db, self.project_id, session_id, batch,
                memory_dir=self.project_path / "memory",
            )
            # Receipt after the memories landed (register X6): insert_session
            # writes complete=0, and _get_saved_session_ids only believes a
            # row this flag confirms — an abort between the insert above and
            # here leaves the transcript eligible for retroactive save.
            self.db.mark_session_complete(session_id)
            mem_count = counts.get("inserted", 0)
            merged = counts.get("merged", 0)
            superseded = counts.get("superseded", 0)
            skipped = counts.get("skipped", 0)

            self._set_busy(False)
            self._refresh()
            messagebox.showinfo(
                "Saved",
                f"Extraction method: {method.upper()}\n\n"
                f"  Inserted:    {mem_count}\n"
                f"  Merged:      {merged}  (overwrote existing high-similarity)\n"
                f"  Superseded:  {superseded} (archived older + linked new)\n"
                f"  Skipped:     {skipped} (exact duplicates)\n\n"
                f"Source:  {transcript_dir}\n"
                f"Session: {latest.stem[:8]}...")

        except Exception as e:
            self._set_busy(False, "Ready")
            messagebox.showerror("Error", f"Failed to extract memories:\n\n{e}")

    def _init_new_project(self):
        """Initialize memory for a new project directory with auto-detection."""
        path = filedialog.askdirectory(title="Select project to initialize")
        if not path:
            return
        # Init New reaches _ensure_memory_dir directly, so the gate inside
        # _load_project does not cover it: this is the route that CREATES.
        # Opt-out on the raw pick (before anchoring, so a per-subdirectory
        # exclusion is not widened), then anchor — picking a subdirectory of an
        # existing project must initialise that project, not plant a second
        # database one level down.
        notice = self._opt_out_notice(path)
        if notice:
            self._report_opt_out(path, notice)
            return
        project = Path(self._anchor(path)).resolve()
        memory_dir = project / "memory"
        if (memory_dir / "memory.db").exists():
            self._load_project(str(project))
            if not (project / "CLAUDE.md").exists():
                if messagebox.askyesno("Generate CLAUDE.md?",
                                       f"{project.name} already has memory but no CLAUDE.md.\n\n"
                                       f"Scan project and generate CLAUDE.md?"):
                    scan = _scan_project_deep(project)
                    claude_md = _generate_claude_md(project, scan)
                    (project / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
                    messagebox.showinfo("Done", "CLAUDE.md created!")
            else:
                self.status_var.set(f"Loaded: {project.name} (already initialized)")
            return

        # Scan project and show confirmation dialog
        scan = _scan_project_deep(project)
        self._show_init_confirm_dialog(project, scan)

    def _show_init_confirm_dialog(self, project, scan):
        """Show dialog with detected info and suggested memories for user confirmation."""
        dlg = tk.Toplevel(self.root)
        dlg.title(f"Initialize: {project.name}")
        dlg.geometry("750x650")
        dlg.transient(self.root)
        dlg.grab_set()

        # Header
        ttk.Label(dlg, text=f"Project: {project.name}",
                  font=("", 12, "bold")).pack(pady=(10, 2))
        ttk.Label(dlg, text=f"Path: {project}",
                  font=("", 9)).pack(pady=(0, 5))

        # Detection summary
        summary = scan["summary"]
        sf = ttk.LabelFrame(dlg, text="Detected Structure", padding=8)
        sf.pack(fill=tk.X, padx=15, pady=5)
        ttk.Label(sf, text=summary, wraplength=680, justify=tk.LEFT).pack(anchor=tk.W)

        # Suggested memories with checkboxes
        mf = ttk.LabelFrame(dlg, text="Suggested Initial Memories (uncheck to skip)", padding=8)
        mf.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        canvas = tk.Canvas(mf)
        scrollbar = ttk.Scrollbar(mf, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        mem_vars = []  # (BooleanVar, category, content, importance)
        for mem in scan["suggested_memories"]:
            var = tk.BooleanVar(value=True)
            row = ttk.Frame(scroll_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Checkbutton(row, variable=var).pack(side=tk.LEFT)
            cat_label = f"[{mem['category']}|{'*'*mem['importance']}]"
            ttk.Label(row, text=cat_label, width=14, font=("Consolas", 9)).pack(side=tk.LEFT)
            ttk.Label(row, text=mem["content"], wraplength=550, justify=tk.LEFT).pack(
                side=tk.LEFT, padx=5)
            mem_vars.append((var, mem["category"], mem["content"], mem["importance"]))

        # CLAUDE.md option
        cf = ttk.Frame(dlg, padding=8)
        cf.pack(fill=tk.X, padx=15, pady=5)
        self._create_claude_md_var = tk.BooleanVar(value=not (project / "CLAUDE.md").exists())
        cb_text = "Create CLAUDE.md (project instructions for Claude Code)"
        if (project / "CLAUDE.md").exists():
            cb_text = "CLAUDE.md already exists — skip"
        ttk.Checkbutton(cf, text=cb_text,
                        variable=self._create_claude_md_var).pack(anchor=tk.W)
        if (project / "CLAUDE.md").exists():
            self._create_claude_md_var.set(False)

        # Buttons
        bf = ttk.Frame(dlg, padding=8)
        bf.pack(fill=tk.X, padx=15)

        def do_init():
            # Anti-patch: route initial-scan memories through the writer.
            # On a fresh project these all INSERT (no similar), but if the
            # user re-inits an existing project the writer will merge sensibly.
            batch = [
                {"category": cat, "content": content, "importance": imp,
                 "tags": ["auto-detected", "init"]}
                for var, cat, content, imp in mem_vars if var.get()
            ]
            try:
                # Single shared helper: memory/ + sessions/ + topics/ + the
                # .gitignore, identical on every path that can create a
                # project. This used to be the ONLY place the ignore file was
                # written. It now REFUSES a directory that no longer exists,
                # which can happen between the picker and this click.
                memory_dir = self._ensure_memory_dir(project)

                # Initialize DB and save confirmed memories
                db = MemoryDB(memory_dir / "memory.db")
                pid = db.upsert_project(str(project))
                counts = upsert_batch(db, pid, None, batch, memory_dir=memory_dir)
                saved = (counts.get("inserted", 0) + counts.get("merged", 0)
                         + counts.get("superseded", 0))

                # Save keywords
                if scan.get("keywords"):
                    db.upsert_keywords(pid, scan["keywords"])

                # Create CLAUDE.md
                if self._create_claude_md_var.get():
                    claude_md = _generate_claude_md(project, scan)
                    (project / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
            except Exception as e:
                # why: same rule as _load_project — a Tk callback that raises
                # reports to stderr, which the --windowed exe does not have, so
                # "Initialize" would appear to do nothing at all
                dlg.destroy()
                self._report_project_error("initialize", project, e)
                return

            dlg.destroy()
            self._load_project(str(project))

            parts = [f"Saved {saved} memories"]
            if self._create_claude_md_var.get():
                parts.append("created CLAUDE.md")
            messagebox.showinfo("Success",
                                f"Memory initialized for {project.name}!\n\n"
                                + ", ".join(parts))

        ttk.Button(bf, text="Initialize", command=do_init).pack(side=tk.RIGHT, padx=5)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)


# ---------------------------------------------------------------------------
# Transcript Finder
# ---------------------------------------------------------------------------

def _find_transcript_dir(project_path: Path) -> "Path | None":
    """
    Find the Claude Code transcript directory for a project.

    Claude Code stores transcripts at ``~/.claude/projects/<slug>/`` where
    <slug> is the absolute project path with EVERY non-alphanumeric character
    replaced by '-'. The old mangling only handled ':', '\\' and '/', so any
    project path containing '_' or '.' failed the exact match outright — of
    179 real slug directories on the development machine, zero contain either
    character.

    Exact match, then case-insensitive match, then None. There is deliberately
    NO fuzzy fallback: the removed `proj_name in d.name` branch accepted any
    slug merely CONTAINING the project's basename, so "Save Session" on a
    project called `cc`, `core` or `data` silently harvested — and permanently
    stored — an unrelated project's transcript. Guessing is never safe in a
    tool that persists what it reads and re-injects it every session. The
    sibling `extractor.find_latest_transcript` follows the same rule.
    """
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None

    # e.g. d:\Projects\cc-memory → d--Projects-cc-memory
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project_path.resolve()))

    # Try exact match first
    candidate = claude_projects / slug
    if candidate.is_dir():
        return candidate

    # Case-insensitive match (Windows paths may differ in casing)
    slug_lower = slug.lower()
    for d in claude_projects.iterdir():
        if d.is_dir() and d.name.lower() == slug_lower:
            return d

    return None


# ---------------------------------------------------------------------------
# Project Scanning & CLAUDE.md Generation
# ---------------------------------------------------------------------------

def _scan_project_deep(project: Path) -> dict:
    """Deep scan a project directory to detect structure, language, frameworks, and suggest memories."""
    result = {
        "summary": "",
        "suggested_memories": [],
        "keywords": {},
        "language": None,
        "framework": None,
        "project_type": "unknown",
        "has_claude_md": (project / "CLAUDE.md").exists(),
        "has_git": (project / ".git").exists(),
    }

    add_mem = lambda cat, content, imp=3: result["suggested_memories"].append(
        {"category": cat, "content": content, "importance": imp})

    # ── Language & framework detection ──
    lang_markers = {
        "pyproject.toml": ("Python", None), "setup.py": ("Python", None),
        "setup.cfg": ("Python", None), "requirements.txt": ("Python", None),
        "Pipfile": ("Python", "pipenv"),
        "package.json": ("JavaScript/TypeScript", "Node.js"),
        "tsconfig.json": ("TypeScript", "Node.js"),
        "Cargo.toml": ("Rust", "Cargo"), "go.mod": ("Go", None),
        "pom.xml": ("Java", "Maven"), "build.gradle": ("Java", "Gradle"),
        "Gemfile": ("Ruby", "Bundler"), "composer.json": ("PHP", "Composer"),
        "CMakeLists.txt": ("C/C++", "CMake"), "Makefile": ("C/C++", "Make"),
        "*.sln": ("C#", ".NET"), "mix.exs": ("Elixir", "Mix"),
    }

    for marker, (lang, fw) in lang_markers.items():
        if "*" in marker:
            if list(project.glob(marker)):
                result["language"] = lang
                result["framework"] = fw
                break
        elif (project / marker).exists():
            result["language"] = lang
            result["framework"] = fw
            break

    # ── Project type detection ──
    has_notebooks = bool(list(project.rglob("*.ipynb"))[:1])
    has_src = (project / "src").exists()
    has_lib = (project / "lib").exists() or (project / "pkg").exists()
    has_tests = (project / "tests").exists() or (project / "test").exists()
    has_docs = (project / "docs").exists() or (project / "doc").exists()

    if has_notebooks:
        result["project_type"] = "notebook/research"
    elif has_src and has_lib:
        result["project_type"] = "application+library"
    elif has_src:
        result["project_type"] = "application"
    elif has_lib:
        result["project_type"] = "library"
    elif result["language"]:
        result["project_type"] = f"{result['language']} project"

    # ── Count files by extension ──
    ext_counts = {}
    total_files = 0
    try:
        for f in project.rglob("*"):
            if f.is_file() and ".git" not in f.parts and "node_modules" not in f.parts \
                    and "__pycache__" not in f.parts and ".venv" not in f.parts:
                total_files += 1
                ext = f.suffix.lower()
                if ext:
                    ext_counts[ext] = ext_counts.get(ext, 0) + 1
                if total_files > 50000:
                    break
    except (PermissionError, OSError):
        pass

    top_exts = sorted(ext_counts.items(), key=lambda x: -x[1])[:8]

    # ── Detect specific structures ──
    has_docker = (project / "Dockerfile").exists() or (project / "docker-compose.yml").exists()
    has_ci = any((project / p).exists() for p in [
        ".github/workflows", ".gitlab-ci.yml", ".circleci", "Jenkinsfile"])
    has_readme = (project / "README.md").exists() or (project / "readme.md").exists()
    has_env = (project / ".env").exists() or (project / ".env.example").exists()
    has_venv = (project / ".venv").exists() or (project / "venv").exists()

    # ── Detect key config files ──
    config_files = []
    for name in ["config.py", "config.js", "config.ts", "settings.py", "constants.py",
                 ".eslintrc.json", "webpack.config.js", "vite.config.ts", "next.config.js",
                 "jest.config.js", "pytest.ini", "tox.ini", ".flake8", "mypy.ini",
                 "tsconfig.json", "tailwind.config.js"]:
        matches = list(project.rglob(name))[:3]
        config_files.extend(str(m.relative_to(project)) for m in matches)

    # ── Detect entry points ──
    entry_points = []
    for name in ["main.py", "app.py", "index.py", "index.js", "index.ts",
                 "main.go", "main.rs", "Main.java", "manage.py", "server.py"]:
        matches = list(project.rglob(name))[:2]
        entry_points.extend(str(m.relative_to(project)) for m in matches)

    # ── Detect important directories ──
    important_dirs = []
    for d in ["src", "lib", "pkg", "app", "api", "core", "models", "utils",
              "components", "pages", "routes", "services", "hooks",
              "tests", "test", "docs", "scripts", "data", "config"]:
        if (project / d).exists() and (project / d).is_dir():
            important_dirs.append(d)

    # ── Read README for project description ──
    readme_desc = None
    for rname in ["README.md", "readme.md", "README.rst", "README.txt"]:
        rpath = project / rname
        if rpath.exists():
            try:
                text = rpath.read_text(encoding="utf-8", errors="ignore")[:2000]
                # Extract first meaningful paragraph
                lines = text.split("\n")
                desc_lines = []
                started = False
                for line in lines:
                    stripped = line.strip()
                    if not started:
                        # Skip title lines (# heading, === underline, blank)
                        if stripped and not stripped.startswith("#") and not all(
                                c in "=-~" for c in stripped):
                            started = True
                            desc_lines.append(stripped)
                    elif stripped:
                        desc_lines.append(stripped)
                    elif desc_lines:
                        break
                if desc_lines:
                    readme_desc = " ".join(desc_lines)[:200]
            except OSError:
                # why: `read_text` is the ONLY statement in this block that can
                # raise — `errors="ignore"` rules out UnicodeDecodeError and
                # everything after it is pure str work — so OSError (permission
                # denied, a directory named README.md, an offline share) is the
                # whole surface. A README this scanner cannot read costs one
                # suggested memory, not the scan.
                pass
            break

    # ── Read package.json / pyproject.toml for metadata ──
    pkg_name = None
    pkg_desc = None
    if (project / "package.json").exists():
        try:
            pkg = json.loads((project / "package.json").read_text(encoding="utf-8"))
            pkg_name = pkg.get("name")
            pkg_desc = pkg.get("description")
            deps = list(pkg.get("dependencies", {}).keys())[:15]
            dev_deps = list(pkg.get("devDependencies", {}).keys())[:10]
            if deps:
                add_mem("config", f"Dependencies: {', '.join(deps)}", 2)
            if dev_deps:
                add_mem("config", f"Dev dependencies: {', '.join(dev_deps)}", 1)
        except (OSError, ValueError, AttributeError, RecursionError):
            # why: this is a STRANGER'S manifest and each named class is one
            # real way it defeats the block — OSError: unreadable file;
            # ValueError: json.loads on malformed JSON, and its UnicodeDecodeError
            # subclass on a non-UTF-8 file; RecursionError: json.loads on a
            # deeply nested document (a RuntimeError, not a ValueError — the
            # same pair `_process_line` catches in mcp/server.py:792);
            # AttributeError: it parsed but is
            # not the assumed shape, so `.get` / `.keys()` hit a list or a str.
            # A manifest we cannot read costs a suggested memory; it must not
            # abort a scan the user launched from an unguarded Tk callback.
            pass
    elif (project / "pyproject.toml").exists():
        try:
            text = (project / "pyproject.toml").read_text(encoding="utf-8")
            for line in text.split("\n"):
                if line.strip().startswith("name") and "=" in line:
                    pkg_name = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.strip().startswith("description") and "=" in line:
                    pkg_desc = line.split("=", 1)[1].strip().strip('"').strip("'")
        except (OSError, UnicodeDecodeError):
            # why: `read_text` is the only raiser — this is a text scan, not a
            # TOML parse, and `"=" in line` already guarantees the split has an
            # index 1. Unlike the README read above there is no errors="ignore",
            # so a latin-1 pyproject.toml raises UnicodeDecodeError.
            pass
    elif (project / "requirements.txt").exists():
        try:
            text = (project / "requirements.txt").read_text(encoding="utf-8")
            deps = [l.split("==")[0].split(">=")[0].split("[")[0].strip()
                    for l in text.strip().split("\n")
                    if l.strip() and not l.startswith("#") and not l.startswith("-")][:15]
            if deps:
                add_mem("config", f"Python dependencies: {', '.join(deps)}", 2)
        except (OSError, UnicodeDecodeError):
            # why: same two, same reason as the pyproject read above —
            # `read_text` can fail on the file or on its encoding; the list
            # comprehension after it is pure str splitting and cannot raise.
            pass

    # ── Build suggested memories ──

    # Project identity
    proj_desc = pkg_desc or readme_desc
    if proj_desc:
        add_mem("arch", f"Project description: {proj_desc}", 4)
    add_mem("arch",
            f"Project type: {result['project_type']}, language: {result['language'] or 'mixed'}"
            + (f", framework: {result['framework']}" if result['framework'] else ""),
            3)

    # File structure
    if important_dirs:
        add_mem("arch", f"Key directories: {', '.join(important_dirs)}", 3)
    add_mem("config", f"Total files: {total_files}", 2)
    if top_exts:
        ext_str = ", ".join(f"{ext}({n})" for ext, n in top_exts[:5])
        add_mem("config", f"File types: {ext_str}", 2)

    # Entry points
    if entry_points:
        add_mem("arch", f"Entry points: {', '.join(entry_points)}", 3)
    if config_files:
        add_mem("config", f"Config files: {', '.join(config_files[:5])}", 2)

    # Infrastructure
    if has_docker:
        add_mem("config", "Docker: Dockerfile/docker-compose present", 2)
    if has_ci:
        add_mem("config", "CI/CD: pipeline configuration detected", 2)
    if has_tests:
        add_mem("config", "Tests: test directory present", 2)
    if has_venv:
        add_mem("config", "Virtual environment: .venv or venv present", 1)
    if result["has_git"]:
        add_mem("config", "Version control: Git repository", 1)
    if result["has_claude_md"]:
        add_mem("config", "CLAUDE.md exists — Claude Code project instructions configured", 3)

    # Skills
    skills_dir = project / ".claude" / "skills"
    if skills_dir.exists():
        skill_files = [f.name for f in skills_dir.iterdir() if f.is_file()]
        if skill_files:
            add_mem("config", f"Claude skills: {', '.join(skill_files)}", 2)

    # Notebooks
    if has_notebooks:
        notebooks = list(project.rglob("*.ipynb"))[:10]
        nb_names = [str(nb.relative_to(project)) for nb in notebooks]
        add_mem("arch", f"Notebooks: {', '.join(nb_names)}", 3)

    # ── Build keywords ──
    for d in important_dirs:
        result["keywords"][d] = 1
    if pkg_name:
        result["keywords"][pkg_name] = 2
    for ep in entry_points:
        name = Path(ep).stem
        if len(name) > 2:
            result["keywords"][name] = 1
    for cf in config_files:
        name = Path(cf).stem
        if len(name) > 2:
            result["keywords"][name] = 1

    # ── Build summary string ──
    parts = [f"Type: {result['project_type']}"]
    if result["language"]:
        parts.append(f"Language: {result['language']}")
    if result["framework"]:
        parts.append(f"Framework: {result['framework']}")
    parts.append(f"Files: {total_files}")
    if important_dirs:
        parts.append(f"Dirs: {', '.join(important_dirs[:6])}")
    if has_docker:
        parts.append("Docker")
    if has_ci:
        parts.append("CI/CD")
    if has_tests:
        parts.append("Tests")
    if result["has_claude_md"]:
        parts.append("CLAUDE.md")
    if result["has_git"]:
        parts.append("Git")
    result["summary"] = " | ".join(parts)

    return result


def _generate_claude_md(project: Path, scan: dict) -> str:
    """Generate a CLAUDE.md template based on detected project structure."""
    name = project.name
    lang = scan.get("language") or "unknown"
    ptype = scan.get("project_type", "project")
    framework = scan.get("framework")

    sections = []

    # Header
    sections.append(f"# CLAUDE.md — Project Instructions for Claude Code\n")
    sections.append(f"## Project: {name}\n")

    desc_mem = next((m for m in scan["suggested_memories"]
                     if m["category"] == "arch" and "description:" in m["content"].lower()), None)
    if desc_mem:
        desc = desc_mem["content"].replace("Project description: ", "")
        sections.append(f"{desc}\n")

    sections.append(f"- **Type**: {ptype}")
    sections.append(f"- **Language**: {lang}")
    if framework:
        sections.append(f"- **Framework**: {framework}")
    sections.append("")

    # Key directories
    dir_mem = next((m for m in scan["suggested_memories"]
                    if "Key directories" in m["content"]), None)
    if dir_mem:
        dirs = dir_mem["content"].replace("Key directories: ", "")
        sections.append(f"## Project Structure\n")
        sections.append(f"Key directories: `{dirs}`\n")

    # Entry points
    ep_mem = next((m for m in scan["suggested_memories"]
                   if "Entry points" in m["content"]), None)
    if ep_mem:
        eps = ep_mem["content"].replace("Entry points: ", "")
        sections.append(f"Entry points: `{eps}`\n")

    # Development guidelines (language-specific)
    sections.append("## Development Guidelines\n")

    if lang == "Python":
        sections.append("- Use type hints where appropriate")
        sections.append("- Follow PEP 8 style conventions")
        sections.append("- Use `encoding='utf-8'` when reading/writing files")
    elif lang in ("JavaScript/TypeScript", "TypeScript"):
        sections.append("- Follow existing code style and linting rules")
        sections.append("- Use TypeScript types where available")
    elif lang == "Rust":
        sections.append("- Run `cargo check` before committing")
        sections.append("- Follow Rust API guidelines")
    elif lang == "Go":
        sections.append("- Run `go vet` and `go fmt` before committing")
    else:
        sections.append("- Follow existing code conventions")

    sections.append("- Read files before modifying them")
    sections.append("- Do not delete or overwrite data files without asking")
    sections.append("")

    # Data integrity
    sections.append("## Data & Safety Rules\n")
    sections.append("- Never delete cached data or model files without asking")
    sections.append("- Never overwrite existing files without reading them first")
    sections.append("- Never fabricate data, results, or citations")
    sections.append("")

    # CLAUDE.md is PROJECT INSTRUCTIONS — Claude Code loads it as authority at
    # every session, which makes it the highest-value target in the tree, and
    # this generator interpolates a stranger's text into it. `desc` comes from
    # a cloned repository's `package.json` "description" (or pyproject's) via
    # `scan["suggested_memories"]`, which is the list built BEFORE any
    # `upsert_batch`, so `clean_for_storage` has not run on it: the storage
    # cleaner protects the database, not this file. Same for the key-directory
    # and entry-point slots, which come from names on disk.
    #
    # `neutralize_document`, on the ASSEMBLED text, for the same reason the
    # artifact renderers (PROGRESS.md, PLAN.md, MEMORY.md, the SessionStart
    # injection) use it: two independently clean values can complete a
    # marker across a join the renderer itself wrote.
    try:
        from core.privacy import neutralize_document
        return neutralize_document("\n".join(sections))
    except ImportError:
        # why: dashboard.py is launched standalone often enough that a partial
        # install must still produce a file — but never an unswept one. Escape
        # the two delimiters by hand rather than emitting raw authority markup.
        return ("\n".join(sections)
                .replace("<", "&lt;").replace(">", "&gt;"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # The dashboard prints (and renders) memory content that can contain any
    # unicode glyph — including the ↻ supersede marker cc-memory emits itself.
    # On a gbk console that raised UnicodeEncodeError before the GUI ever
    # appeared. This was one of five entry points that never called it.
    enable_utf8_io()

    parser = argparse.ArgumentParser(description="cc-memory Dashboard")
    parser.add_argument("--project", help="Initial project path")
    args = parser.parse_args()

    # Anchor like every other entry point. The dashboard opens the project with
    # MemoryDB(memory_dir / "memory.db"), which CREATES, so `--project <subdir>`
    # planted a stray there and rung 0 then pinned all six hooks
    # <!--ce:hooks:asof--> to it. Printed, not silent, for the same reason the
    # CLIs print it: an explicit --project is an instruction. `is not None` so
    # `--project ""` anchors too.
    if args.project is not None:
        # Opt-out BEFORE anchoring, exactly like the hooks and the two CLIs.
        try:
            from core.modes import cli_opt_out_notice
            notice = cli_opt_out_notice(args.project)
            if notice:
                print(f"[cc-memory] {notice}")
                return
        except ImportError:
            # why: the dashboard must still open if the opt-out check cannot
            # load; hooks and the MCP server enforce it independently
            pass
        try:
            from core.roots import anchor_project
            args.project = anchor_project(
                args.project, announce=lambda m: print(f"[cc-memory] {m}"))
        except Exception as exc:
            # why: the dashboard must still open if the resolver cannot load;
            # the raw path is exactly the pre-v2.8.0 behaviour
            print(f"[cc-memory] project-root anchoring unavailable ({exc}); "
                  f"using {args.project} as given")

    root = tk.Tk()
    app = DashboardApp(root, initial_project=args.project)
    root.mainloop()


if __name__ == "__main__":
    main()
