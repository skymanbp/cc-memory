"""
cc-memory/db.py
SQLite database layer for the cc-memory plugin.

Schema design (3NF normalized):
  projects   — one row per project path
  sessions   — one row per compaction event
  memories   — extracted facts, categorized + weighted
  topics     — long-term knowledge blobs per topic name
  keywords   — auto-detected project vocabulary with frequency
  plans      — task queue with ordering + feasibility + status

SQL features used:
  - Foreign keys (PRAGMA foreign_keys = ON)
  - WAL journal mode (concurrent reads)
  - Partial indexes (is_active = 1)
  - Aggregation queries (GROUP BY, COUNT, MAX)
  - Upsert (INSERT OR REPLACE / UPDATE)
  - Parameterized queries throughout (SQL injection safe)
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any


# ---------------------------------------------------------------------------
# DDL — run once on first connect
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
-- ── projects ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    NOT NULL UNIQUE,   -- resolved absolute path
    name        TEXT    NOT NULL,          -- basename of path
    created_at  TEXT    NOT NULL,
    last_active TEXT    NOT NULL
);

-- ── sessions ────────────────────────────────────────────────────────────────
-- One row per PreCompact event (= one compaction = one saved snapshot)
CREATE TABLE IF NOT EXISTS sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        INTEGER NOT NULL REFERENCES projects(id),
    claude_session_id TEXT,               -- from hook stdin
    trigger_type      TEXT    NOT NULL DEFAULT 'auto',  -- 'auto' | 'manual'
    compacted_at      TEXT    NOT NULL,
    msg_count         INTEGER NOT NULL DEFAULT 0,
    archive_path      TEXT,               -- relative path to .md archive
    brief_summary     TEXT                -- first 1000 chars of archive
);

-- ── memories ────────────────────────────────────────────────────────────────
-- Individual extracted facts.  category drives display priority.
-- importance 1-5: 1=noise, 3=normal, 5=critical (never forget)
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    session_id  INTEGER          REFERENCES sessions(id),
    category    TEXT    NOT NULL,   -- decision|result|config|bug|task|arch|note
    content     TEXT    NOT NULL,
    importance  INTEGER NOT NULL DEFAULT 2 CHECK(importance BETWEEN 1 AND 5),
    tags        TEXT    NOT NULL DEFAULT '[]',   -- JSON array of strings
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1       -- 0 = superseded / archived
);

-- ── topics ──────────────────────────────────────────────────────────────────
-- Long-form knowledge blocks, keyed by name (e.g. 'CNN', 'GNN', 'fusion').
-- Upserted on each session: version increments on every update.
CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    name        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (project_id, name)
);

-- ── keywords ────────────────────────────────────────────────────────────────
-- Project-specific vocabulary, auto-detected from transcripts.
-- frequency accumulates across sessions → top-N = project language model.
CREATE TABLE IF NOT EXISTS keywords (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    keyword     TEXT    NOT NULL,
    frequency   INTEGER NOT NULL DEFAULT 1,
    last_seen   TEXT    NOT NULL,
    UNIQUE (project_id, keyword)
);

-- ── plans ───────────────────────────────────────────────────────────────────
-- Task queue: user fills plans, Claude evaluates feasibility, user triggers exec.
-- Status flow: draft → evaluating → ready → executing → done | failed | skipped
CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    content     TEXT    NOT NULL,          -- what to do (natural language)
    exec_order  INTEGER NOT NULL DEFAULT 0,-- execution sequence (1, 2, 3...)
    status      TEXT    NOT NULL DEFAULT 'draft',
    feasibility TEXT,                      -- Claude's evaluation notes
    result      TEXT,                      -- execution output / notes
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- ── indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_memories_project_active
    ON memories (project_id, is_active);

CREATE INDEX IF NOT EXISTS idx_memories_category
    ON memories (project_id, category, importance DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions (project_id, compacted_at DESC);

CREATE INDEX IF NOT EXISTS idx_keywords_freq
    ON keywords (project_id, frequency DESC);

CREATE INDEX IF NOT EXISTS idx_plans_project_order
    ON plans (project_id, exec_order);

CREATE INDEX IF NOT EXISTS idx_plans_status
    ON plans (project_id, status);
"""


# ---------------------------------------------------------------------------
# MemoryDB
# ---------------------------------------------------------------------------
class MemoryDB:
    """Thin wrapper around a project-local SQLite database."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    # ── internal helpers ────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")   # allow concurrent readers
        conn.execute("PRAGMA synchronous = NORMAL")  # safe + fast
        return conn

    def _bootstrap(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ── projects ─────────────────────────────────────────────────────────────

    def upsert_project(self, cwd: str) -> int:
        """Return project.id, creating the row if needed."""
        path = str(Path(cwd).resolve())
        name = Path(path).name
        now  = self._now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM projects WHERE path = ?", (path,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE projects SET last_active = ? WHERE id = ?",
                    (now, row["id"])
                )
                return row["id"]
            cur = conn.execute(
                "INSERT INTO projects (path, name, created_at, last_active) "
                "VALUES (?, ?, ?, ?)",
                (path, name, now, now)
            )
            return cur.lastrowid

    def get_project_by_path(self, cwd: str) -> Optional[Dict]:
        path = str(Path(cwd).resolve())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE path = ?", (path,)
            ).fetchone()
            return dict(row) if row else None

    # ── sessions ─────────────────────────────────────────────────────────────

    def insert_session(
        self,
        project_id: int,
        claude_session_id: str,
        trigger_type: str,
        msg_count: int,
        archive_path: str,
        brief_summary: str,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO sessions
                   (project_id, claude_session_id, trigger_type, compacted_at,
                    msg_count, archive_path, brief_summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project_id, claude_session_id, trigger_type, self._now(),
                 msg_count, archive_path, brief_summary)
            )
            return cur.lastrowid

    def get_recent_session_ids(self, project_id: int, n: int = 3) -> List[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE project_id = ? "
                "ORDER BY compacted_at DESC LIMIT ?",
                (project_id, n)
            ).fetchall()
            return [r["id"] for r in rows]

    # ── memories ─────────────────────────────────────────────────────────────

    def insert_memory(
        self,
        project_id: int,
        session_id: int,
        category: str,
        content: str,
        importance: int = 2,
        tags: List[str] = None,
    ) -> int:
        now = self._now()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO memories
                   (project_id, session_id, category, content, importance,
                    tags, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, session_id, category, content, importance,
                 json.dumps(tags or [], ensure_ascii=False), now, now)
            )
            return cur.lastrowid

    def get_recent_memories(
        self,
        project_id: int,
        sessions_back: int = 3,
        categories: List[str] = None,
        min_importance: int = 1,
        limit: int = 30,
    ) -> List[Dict]:
        session_ids = self.get_recent_session_ids(project_id, sessions_back)
        if not session_ids:
            return []
        ph = ",".join("?" * len(session_ids))
        params: List[Any] = [project_id, min_importance] + session_ids

        cat_clause = ""
        if categories:
            cat_ph = ",".join("?" * len(categories))
            cat_clause = f"AND category IN ({cat_ph})"
            params += categories

        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM memories
                    WHERE project_id = ? AND is_active = 1
                      AND importance >= ?
                      AND session_id IN ({ph})
                      {cat_clause}
                    ORDER BY importance DESC, created_at DESC
                    LIMIT ?""",
                params
            ).fetchall()
            return [dict(r) for r in rows]

    def get_critical_memories(
        self, project_id: int, min_importance: int = 4
    ) -> List[Dict]:
        """All-time high-importance memories (not session-filtered)."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1
                     AND importance >= ?
                   ORDER BY importance DESC, updated_at DESC""",
                (project_id, min_importance)
            ).fetchall()
            return [dict(r) for r in rows]

    def archive_memory(self, memory_id: int):
        """Mark a memory as superseded (soft-delete)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET is_active = 0, updated_at = ? WHERE id = ?",
                (self._now(), memory_id)
            )

    # ── topics ───────────────────────────────────────────────────────────────

    def upsert_topic(self, project_id: int, name: str, content: str):
        now = self._now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, version FROM topics "
                "WHERE project_id = ? AND name = ?",
                (project_id, name)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE topics SET content = ?, updated_at = ?, version = ? "
                    "WHERE id = ?",
                    (content, now, existing["version"] + 1, existing["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO topics (project_id, name, content, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (project_id, name, content, now)
                )

    def get_topics(self, project_id: int) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, content, updated_at, version FROM topics "
                "WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── keywords ─────────────────────────────────────────────────────────────

    def upsert_keywords(self, project_id: int, freq_map: Dict[str, int]):
        """Increment frequency counters for each keyword."""
        now = self._now()
        with self._connect() as conn:
            for kw, delta in freq_map.items():
                conn.execute(
                    """INSERT INTO keywords (project_id, keyword, frequency, last_seen)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(project_id, keyword)
                       DO UPDATE SET
                           frequency = frequency + excluded.frequency,
                           last_seen = excluded.last_seen""",
                    (project_id, kw, delta, now)
                )

    def get_top_keywords(self, project_id: int, n: int = 40) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT keyword FROM keywords WHERE project_id = ? "
                "ORDER BY frequency DESC LIMIT ?",
                (project_id, n)
            ).fetchall()
            return [r["keyword"] for r in rows]

    # ── plans ────────────────────────────────────────────────────────────────

    def add_plan(self, project_id: int, content: str, exec_order: int = 0) -> int:
        now = self._now()
        with self._connect() as conn:
            if exec_order <= 0:
                # Auto-assign next order number
                row = conn.execute(
                    "SELECT COALESCE(MAX(exec_order), 0) + 1 AS next_order "
                    "FROM plans WHERE project_id = ? "
                    "AND status NOT IN ('done', 'failed', 'skipped')",
                    (project_id,)
                ).fetchone()
                exec_order = row["next_order"]
            cur = conn.execute(
                """INSERT INTO plans
                   (project_id, content, exec_order, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'draft', ?, ?)""",
                (project_id, content, exec_order, now, now)
            )
            return cur.lastrowid

    def get_plans(self, project_id: int,
                  statuses: List[str] = None) -> List[Dict]:
        with self._connect() as conn:
            if statuses:
                ph = ",".join("?" * len(statuses))
                rows = conn.execute(
                    f"SELECT * FROM plans WHERE project_id = ? "
                    f"AND status IN ({ph}) ORDER BY exec_order",
                    [project_id] + statuses
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM plans WHERE project_id = ? "
                    "ORDER BY exec_order",
                    (project_id,)
                ).fetchall()
            return [dict(r) for r in rows]

    def get_active_plans(self, project_id: int) -> List[Dict]:
        return self.get_plans(project_id,
                              statuses=["draft", "evaluating", "ready", "executing"])

    def update_plan_status(self, plan_id: int, status: str,
                           notes: Optional[str] = None,
                           field: str = "feasibility") -> None:
        now = self._now()
        with self._connect() as conn:
            if notes is not None:
                conn.execute(
                    f"UPDATE plans SET status = ?, {field} = ?, updated_at = ? "
                    f"WHERE id = ?",
                    (status, notes, now, plan_id)
                )
            else:
                conn.execute(
                    "UPDATE plans SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, plan_id)
                )

    def get_next_plan(self, project_id: int) -> Optional[Dict]:
        """Get the next plan to execute (lowest exec_order with status='ready')."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plans WHERE project_id = ? AND status = 'ready' "
                "ORDER BY exec_order LIMIT 1",
                (project_id,)
            ).fetchone()
            return dict(row) if row else None

    def clear_done_plans(self, project_id: int) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM plans WHERE project_id = ? "
                "AND status IN ('done', 'failed', 'skipped')",
                (project_id,)
            )
            return cur.rowcount

    def reorder_plans(self, project_id: int, plan_ids: List[int]) -> None:
        """Reorder plans by providing the new sequence of plan IDs."""
        now = self._now()
        with self._connect() as conn:
            for order, pid in enumerate(plan_ids, 1):
                conn.execute(
                    "UPDATE plans SET exec_order = ?, updated_at = ? "
                    "WHERE id = ? AND project_id = ?",
                    (order, now, pid, project_id)
                )

    # ── analytics / stats ────────────────────────────────────────────────────

    def get_stats(self, project_id: int) -> Dict:
        with self._connect() as conn:
            n_sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id = ?",
                (project_id,)
            ).fetchone()[0]
            n_memories = conn.execute(
                "SELECT COUNT(*) FROM memories "
                "WHERE project_id = ? AND is_active = 1",
                (project_id,)
            ).fetchone()[0]
            by_cat = conn.execute(
                """SELECT category, COUNT(*) AS n, AVG(importance) AS avg_imp
                   FROM memories
                   WHERE project_id = ? AND is_active = 1
                   GROUP BY category
                   ORDER BY n DESC""",
                (project_id,)
            ).fetchall()
            last_session = conn.execute(
                "SELECT compacted_at FROM sessions WHERE project_id = ? "
                "ORDER BY compacted_at DESC LIMIT 1",
                (project_id,)
            ).fetchone()
            n_plans = conn.execute(
                "SELECT COUNT(*) FROM plans WHERE project_id = ? "
                "AND status NOT IN ('done', 'failed', 'skipped')",
                (project_id,)
            ).fetchone()[0]
            return {
                "n_sessions":   n_sessions,
                "n_memories":   n_memories,
                "n_active_plans": n_plans,
                "by_category":  [dict(r) for r in by_cat],
                "last_session": last_session[0] if last_session else None,
            }

    # ── cross-project (global DB) ─────────────────────────────────────────────

    @classmethod
    def global_db(cls) -> "MemoryDB":
        """Return a handle to the global cross-project database."""
        global_path = Path.home() / ".claude" / "hooks" / "cc-memory" / "global.db"
        return cls(global_path)

    def get_all_projects(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY last_active DESC"
            ).fetchall()
            return [dict(r) for r in rows]
