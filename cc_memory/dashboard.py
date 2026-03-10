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

        # Toolbar
        tb = ttk.Frame(frame, padding=5)
        tb.pack(fill=tk.X)
        ttk.Button(tb, text="Add Plan", command=self._add_plan_dialog).pack(side=tk.LEFT)
        ttk.Button(tb, text="Approve Selected", command=self._approve_plans).pack(side=tk.LEFT, padx=5)
        ttk.Button(tb, text="Approve All", command=self._approve_all_plans).pack(side=tk.LEFT)
        ttk.Button(tb, text="Clear Done", command=self._clear_done_plans).pack(side=tk.LEFT, padx=5)
        ttk.Button(tb, text="Refresh", command=self._load_plans).pack(side=tk.RIGHT)

        # Treeview
        cols = ("id", "order", "status", "content", "feasibility")
        self.plan_tree = ttk.Treeview(frame, columns=cols, show="headings", height=15)
        self.plan_tree.heading("id", text="ID")
        self.plan_tree.heading("order", text="Order")
        self.plan_tree.heading("status", text="Status")
        self.plan_tree.heading("content", text="Content")
        self.plan_tree.heading("feasibility", text="Evaluation")
        self.plan_tree.column("id", width=40)
        self.plan_tree.column("order", width=50)
        self.plan_tree.column("status", width=80)
        self.plan_tree.column("content", width=450)
        self.plan_tree.column("feasibility", width=250)
        self.plan_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

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
            if len(content) > 70:
                content = content[:67] + "..."
            feas = (p.get("feasibility") or "")[:40]
            self.plan_tree.insert("", tk.END, values=(
                p["id"], p["exec_order"], p["status"], content, feas
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

    def _init_new_project(self):
        """Initialize memory for a new project directory."""
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

        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "sessions").mkdir(exist_ok=True)
        (memory_dir / "topics").mkdir(exist_ok=True)

        # Create .gitignore
        gitignore = memory_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "# cc-memory: exclude database from git\n"
                "memory.db\nmemory.db-wal\nmemory.db-shm\nsessions/\n",
                encoding="utf-8"
            )

        self._load_project(str(project))
        messagebox.showinfo("Success",
                            f"Memory initialized for {project.name}!\n\n"
                            f"Directory: {memory_dir}")


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
