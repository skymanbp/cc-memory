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
import argparse, json, sqlite3, sys, os
from datetime import datetime
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent
# Support both: running as script (db.py in same dir) and PyInstaller exe (in cc_memory_files/)
if getattr(sys, 'frozen', False):
    _BUNDLE_DIR = Path(sys._MEIPASS) / "cc_memory_files"
    sys.path.insert(0, str(_BUNDLE_DIR))
else:
    sys.path.insert(0, str(_PLUGIN_DIR))
from db import MemoryDB

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, scrolledtext
except ImportError:
    print("Error: tkinter is not available. Install python3-tk.")
    sys.exit(1)


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
        ttk.Button(top, text="Browse...", command=self._browse_project).pack(side=tk.LEFT)
        ttk.Button(top, text="Init New Project", command=self._init_new_project).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text="Refresh", command=self._refresh).pack(side=tk.LEFT, padx=5)

        # Notebook tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self._build_memories_tab()
        self._build_plans_tab()
        self._build_sessions_tab()
        self._build_keywords_tab()
        self._build_sql_tab()
        self._build_stats_tab()

        # Status bar
        self.status_var = tk.StringVar(value="Select a project to begin")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor=tk.W, padding=3).pack(fill=tk.X)

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
        ttk.Combobox(filt, textvariable=self.mem_cat_var, width=12,
                     values=["all","decision","result","config","bug","task","arch","note"],
                     state="readonly").pack(side=tk.LEFT, padx=5)

        ttk.Label(filt, text="Min Imp:").pack(side=tk.LEFT, padx=(10,0))
        self.mem_imp_var = tk.StringVar(value="1")
        ttk.Spinbox(filt, textvariable=self.mem_imp_var, from_=1, to=5,
                    width=3).pack(side=tk.LEFT, padx=5)

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

    # ── Project Management ───────────────────────────────────────────────────

    def _auto_discover_projects(self):
        """Scan common project directories for memory.db files."""
        search_dirs = []
        # Check D:/Projects/ and common locations
        for d in ["D:/Projects", "C:/Projects", str(Path.home() / "Projects"),
                   str(Path.home() / "repos"), str(Path.home() / "dev")]:
            if Path(d).exists():
                search_dirs.append(Path(d))

        projects = []
        for sd in search_dirs:
            try:
                for child in sd.iterdir():
                    db_path = child / "memory" / "memory.db"
                    if db_path.exists():
                        projects.append(str(child))
            except PermissionError:
                pass

        self.project_combo["values"] = projects
        if projects:
            self.project_combo.set(projects[0])
            self._load_project(projects[0])

    def _browse_project(self):
        path = filedialog.askdirectory(title="Select project directory")
        if path:
            self._load_project(path)

    def _on_project_selected(self, event):
        self._load_project(self.project_var.get())

    def _load_project(self, project_path):
        self.project_path = Path(project_path).resolve()
        db_path = self.project_path / "memory" / "memory.db"

        if not db_path.exists():
            # Initialize
            (self.project_path / "memory").mkdir(parents=True, exist_ok=True)

        self.db = MemoryDB(db_path)
        self.project_id = self.db.upsert_project(str(self.project_path))
        self.project_var.set(str(self.project_path))

        # Update combo values
        vals = list(self.project_combo["values"])
        sp = str(self.project_path)
        if sp not in vals:
            vals.append(sp)
            self.project_combo["values"] = vals

        self._refresh()
        self.status_var.set(f"Loaded: {self.project_path.name}")

    def _refresh(self):
        if not self.db:
            return
        self._load_memories()
        self._load_plans()
        self._load_sessions()
        self._load_keywords()
        self._load_stats()

    # ── Data Loading ─────────────────────────────────────────────────────────

    def _load_memories(self):
        if not self.db:
            return
        for item in self.mem_tree.get_children():
            self.mem_tree.delete(item)

        search = self.mem_search_var.get().strip()
        cat = self.mem_cat_var.get()
        min_imp = int(self.mem_imp_var.get())

        with self.db._connect() as conn:
            params = [self.project_id, min_imp]
            cat_clause = ""
            search_clause = ""

            if cat and cat != "all":
                cat_clause = "AND category = ?"
                params.append(cat)
            if search:
                search_clause = "AND content LIKE ?"
                params.append(f"%{search}%")

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

    # ── SQL Console ──────────────────────────────────────────────────────────

    def _run_sql(self):
        if not self.db:
            messagebox.showwarning("No Project", "Load a project first.")
            return

        self.sql_output.delete("1.0", tk.END)
        query = self.sql_var.get().strip()
        if not query:
            return

        try:
            with self.db._connect() as conn:
                rows = conn.execute(query).fetchall()
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
        except sqlite3.Error as e:
            self.sql_output.insert("1.0", f"SQL Error: {e}\n\nTables: projects, sessions, memories, topics, keywords, plans")

    # ── Dialogs ──────────────────────────────────────────────────────────────

    def _add_memory_dialog(self):
        if not self.db:
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Add Memory")
        dlg.geometry("500x300")
        dlg.transient(self.root)

        ttk.Label(dlg, text="Category:").grid(row=0, column=0, padx=10, pady=5, sticky=tk.W)
        cat_var = tk.StringVar(value="note")
        ttk.Combobox(dlg, textvariable=cat_var, width=15,
                     values=["decision","result","config","bug","task","arch","note"],
                     state="readonly").grid(row=0, column=1, padx=10, pady=5, sticky=tk.W)

        ttk.Label(dlg, text="Importance:").grid(row=1, column=0, padx=10, pady=5, sticky=tk.W)
        imp_var = tk.StringVar(value="3")
        ttk.Spinbox(dlg, textvariable=imp_var, from_=1, to=5, width=5).grid(
            row=1, column=1, padx=10, pady=5, sticky=tk.W)

        ttk.Label(dlg, text="Content:").grid(row=2, column=0, padx=10, pady=5, sticky=tk.NW)
        content_text = tk.Text(dlg, height=8, width=50)
        content_text.grid(row=2, column=1, padx=10, pady=5)

        def save():
            content = content_text.get("1.0", tk.END).strip()
            if not content:
                return
            self.db.insert_memory(
                self.project_id, None, cat_var.get(), content,
                int(imp_var.get()), ["manual", "dashboard"]
            )
            dlg.destroy()
            self._load_memories()

        ttk.Button(dlg, text="Save", command=save).grid(row=3, column=1, pady=10)

    def _add_plan_dialog(self):
        if not self.db:
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Add Plans")
        dlg.geometry("600x400")
        dlg.transient(self.root)

        ttk.Label(dlg, text="Enter plans (one per line):").pack(padx=10, pady=5, anchor=tk.W)
        plan_text = tk.Text(dlg, height=15, width=70)
        plan_text.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)

        def save():
            lines = plan_text.get("1.0", tk.END).strip().split("\n")
            lines = [l.strip() for l in lines if l.strip()]
            if not lines:
                return
            for content in lines:
                self.db.add_plan(self.project_id, content)
            dlg.destroy()
            self._load_plans()
            self.status_var.set(f"Added {len(lines)} plan(s)")

        ttk.Button(dlg, text="Add All", command=save).pack(pady=10)

    def _approve_plans(self):
        if not self.db:
            return
        selected = self.plan_tree.selection()
        for item in selected:
            values = self.plan_tree.item(item, "values")
            plan_id = int(values[0])
            self.db.update_plan_status(plan_id, "ready")
        self._load_plans()

    def _approve_all_plans(self):
        if not self.db:
            return
        plans = self.db.get_plans(self.project_id, statuses=["draft", "evaluating"])
        for p in plans:
            self.db.update_plan_status(p["id"], "ready")
        self._load_plans()
        self.status_var.set(f"Approved {len(plans)} plan(s)")

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
        """Mark selected plans as executing."""
        if not self.db:
            return
        ids = self._get_selected_plan_ids()
        if not ids:
            messagebox.showinfo("No Selection", "Select plan(s) to execute.")
            return
        for pid in ids:
            self.db.update_plan_status(pid, "executing")
        self._load_plans()
        self.status_var.set(f"Executing {len(ids)} plan(s)")

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
            for pid in ids:
                self.db.update_plan_status(pid, "done", note or None, field="result")
            dlg.destroy()
            self._load_plans()
            self.status_var.set(f"Marked {len(ids)} plan(s) done")

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
            for pid in ids:
                self.db.update_plan_status(pid, "failed", reason or None, field="result")
            dlg.destroy()
            self._load_plans()
            self.status_var.set(f"Marked {len(ids)} plan(s) failed")

        bf = ttk.Frame(dlg)
        bf.pack(pady=10)
        ttk.Button(bf, text="Mark Failed", command=do_fail).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

    def _skip_plans(self):
        """Skip selected plans."""
        if not self.db:
            return
        ids = self._get_selected_plan_ids()
        for pid in ids:
            self.db.update_plan_status(pid, "skipped")
        self._load_plans()
        self.status_var.set(f"Skipped {len(ids)} plan(s)")

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
                self.db.update_plan_content(plan_id, new_content)
            new_feas = feas_var.get().strip()
            if new_feas != (plan.get("feasibility") or ""):
                self.db.update_plan_status(plan_id, plan["status"], new_feas, field="feasibility")
            new_result = result_var.get().strip()
            if new_result != (plan.get("result") or ""):
                self.db.update_plan_status(plan_id, plan["status"], new_result, field="result")
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
        for pid in ids:
            self.db.delete_plan(pid)
        self._load_plans()
        self.status_var.set(f"Deleted {len(ids)} plan(s)")

    def _plan_context_menu(self, event):
        """Show right-click context menu on plan tree."""
        item = self.plan_tree.identify_row(event.y)
        if item:
            if item not in self.plan_tree.selection():
                self.plan_tree.selection_set(item)
            self.plan_menu.post(event.x_root, event.y_root)

    def _init_new_project(self):
        """Initialize memory for a new project directory with auto-detection."""
        path = filedialog.askdirectory(title="Select project to initialize")
        if not path:
            return
        project = Path(path)
        memory_dir = project / "memory"
        if (memory_dir / "memory.db").exists():
            messagebox.showinfo("Already Initialized",
                                f"{project.name} already has memory.\nLoading it now.")
            self._load_project(str(project))
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
            # Create memory directory
            memory_dir = project / "memory"
            memory_dir.mkdir(parents=True, exist_ok=True)
            (memory_dir / "sessions").mkdir(exist_ok=True)
            (memory_dir / "topics").mkdir(exist_ok=True)

            # .gitignore
            gitignore = memory_dir / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(
                    "# cc-memory: exclude database from git\n"
                    "memory.db\nmemory.db-wal\nmemory.db-shm\nsessions/\n",
                    encoding="utf-8"
                )

            # Initialize DB and save confirmed memories
            db = MemoryDB(memory_dir / "memory.db")
            pid = db.upsert_project(str(project))

            saved = 0
            for var, cat, content, imp in mem_vars:
                if var.get():
                    db.insert_memory(pid, None, cat, content, imp, ["auto-detected", "init"])
                    saved += 1

            # Save keywords
            if scan.get("keywords"):
                db.upsert_keywords(pid, scan["keywords"])

            # Create CLAUDE.md
            if self._create_claude_md_var.get():
                claude_md = _generate_claude_md(project, scan)
                (project / "CLAUDE.md").write_text(claude_md, encoding="utf-8")

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
            except Exception:
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
        except Exception:
            pass
    elif (project / "pyproject.toml").exists():
        try:
            text = (project / "pyproject.toml").read_text(encoding="utf-8")
            for line in text.split("\n"):
                if line.strip().startswith("name") and "=" in line:
                    pkg_name = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.strip().startswith("description") and "=" in line:
                    pkg_desc = line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    elif (project / "requirements.txt").exists():
        try:
            text = (project / "requirements.txt").read_text(encoding="utf-8")
            deps = [l.split("==")[0].split(">=")[0].split("[")[0].strip()
                    for l in text.strip().split("\n")
                    if l.strip() and not l.startswith("#") and not l.startswith("-")][:15]
            if deps:
                add_mem("config", f"Python dependencies: {', '.join(deps)}", 2)
        except Exception:
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

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="cc-memory Dashboard")
    parser.add_argument("--project", help="Initial project path")
    args = parser.parse_args()

    root = tk.Tk()
    app = DashboardApp(root, initial_project=args.project)
    root.mainloop()


if __name__ == "__main__":
    main()
