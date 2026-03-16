"""
cc-memory/db.py
SQLite database layer for the cc-memory plugin.

Schema design (3NF normalized):
  projects   -- one row per project path
  sessions   -- one row per compaction event
  memories   -- extracted facts, categorized + weighted + topic-tagged
  topics     -- consolidated summaries per topic (L1 in hierarchy)
  keywords   -- auto-detected project vocabulary with frequency
  plans      -- task queue with ordering + feasibility + status

Memory hierarchy (Topic Consolidation):
  Level 0: Global overview (derived from all topic summaries)
  Level 1: Topic summaries (topics table -- always injected)
  Level 2: Active memories (memories table -- injected by relevance)
  Level 3: Archived memories (is_active=0 -- queryable but not injected)
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any


# ---------------------------------------------------------------------------
# DDL -- run once on first connect
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
-- ── projects ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    last_active TEXT    NOT NULL
);

-- ── sessions ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        INTEGER NOT NULL REFERENCES projects(id),
    claude_session_id TEXT,
    trigger_type      TEXT    NOT NULL DEFAULT 'auto',
    compacted_at      TEXT    NOT NULL,
    msg_count         INTEGER NOT NULL DEFAULT 0,
    archive_path      TEXT,
    brief_summary     TEXT
);

-- ── memories ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    session_id  INTEGER          REFERENCES sessions(id),
    category    TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    importance  INTEGER NOT NULL DEFAULT 2 CHECK(importance BETWEEN 1 AND 5),
    tags        TEXT    NOT NULL DEFAULT '[]',
    topic       TEXT,                              -- topic assignment (L1 group)
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);

-- ── topics ──────────────────────────────────────────────────────────────────
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
CREATE TABLE IF NOT EXISTS keywords (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    keyword     TEXT    NOT NULL,
    frequency   INTEGER NOT NULL DEFAULT 1,
    last_seen   TEXT    NOT NULL,
    UNIQUE (project_id, keyword)
);

-- ── plans ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    content     TEXT    NOT NULL,
    exec_order  INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'draft',
    feasibility TEXT,
    result      TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- ── indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_memories_project_active
    ON memories (project_id, is_active);

CREATE INDEX IF NOT EXISTS idx_memories_category
    ON memories (project_id, category, importance DESC);

-- idx_memories_topic created via migration (v1_topic_index)

CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions (project_id, compacted_at DESC);

CREATE INDEX IF NOT EXISTS idx_keywords_freq
    ON keywords (project_id, frequency DESC);

CREATE INDEX IF NOT EXISTS idx_plans_project_order
    ON plans (project_id, exec_order);

CREATE INDEX IF NOT EXISTS idx_plans_status
    ON plans (project_id, status);
"""

# Migrations for existing databases (run after schema creation)
_MIGRATIONS = [
    # v1: add topic column to memories
    ("v1_topic_column", "ALTER TABLE memories ADD COLUMN topic TEXT"),
    # v1: add topic index
    ("v1_topic_index", "CREATE INDEX IF NOT EXISTS idx_memories_topic ON memories (project_id, topic, is_active)"),
]


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
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _bootstrap(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
        self._run_migrations()
        # Ensure topic column exists for new code paths
        with self._connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
            if "topic" not in cols:
                conn.execute("ALTER TABLE memories ADD COLUMN topic TEXT")

    def _run_migrations(self):
        """Apply pending schema migrations for existing databases."""
        with self._connect() as conn:
            # Create migration tracking table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS _migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)
            applied = {
                r[0] for r in
                conn.execute("SELECT name FROM _migrations").fetchall()
            }
            for name, sql in _MIGRATIONS:
                if name in applied:
                    continue
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # e.g. column already exists
                conn.execute(
                    "INSERT OR IGNORE INTO _migrations (name, applied_at) VALUES (?, ?)",
                    (name, self._now())
                )

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ── projects ─────────────────────────────────────────────────────────────

    def upsert_project(self, cwd: str) -> int:
        path = str(Path(cwd).resolve())
        name = Path(path).name
        now = self._now()
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
        self, project_id: int, claude_session_id: str,
        trigger_type: str, msg_count: int, archive_path: str,
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

    def get_session_count(self, project_id: int) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id = ?",
                (project_id,)
            ).fetchone()[0]

    # ── memories ─────────────────────────────────────────────────────────────

    def insert_memory(
        self, project_id: int, session_id: int, category: str,
        content: str, importance: int = 2, tags: List[str] = None,
        topic: str = None,
    ) -> int:
        now = self._now()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO memories
                   (project_id, session_id, category, content, importance,
                    tags, topic, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, session_id, category, content, importance,
                 json.dumps(tags or [], ensure_ascii=False), topic, now, now)
            )
            return cur.lastrowid

    def get_recent_memories(
        self, project_id: int, sessions_back: int = 3,
        categories: List[str] = None, min_importance: int = 1,
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
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1
                     AND importance >= ?
                   ORDER BY importance DESC, updated_at DESC""",
                (project_id, min_importance)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_active_memories(self, project_id: int) -> List[Dict]:
        """All active memories for consolidation."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1
                   ORDER BY topic, importance DESC, created_at DESC""",
                (project_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_memories_by_topic(
        self, project_id: int, topic: str
    ) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1 AND topic = ?
                   ORDER BY importance DESC, created_at DESC""",
                (project_id, topic)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_unassigned_memories(self, project_id: int) -> List[Dict]:
        """Memories without a topic assignment."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1
                     AND (topic IS NULL OR topic = '')
                   ORDER BY importance DESC, created_at DESC""",
                (project_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_topic_memory_counts(self, project_id: int) -> Dict[str, int]:
        """Return {topic: count} for active memories."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT COALESCE(topic, '_unassigned') AS t, COUNT(*) AS n
                   FROM memories
                   WHERE project_id = ? AND is_active = 1
                   GROUP BY t ORDER BY n DESC""",
                (project_id,)
            ).fetchall()
            return {r["t"]: r["n"] for r in rows}

    def set_memory_topic(self, memory_id: int, topic: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET topic = ?, updated_at = ? WHERE id = ?",
                (topic, self._now(), memory_id)
            )

    def bulk_set_topic(self, memory_ids: List[int], topic: str):
        if not memory_ids:
            return
        now = self._now()
        with self._connect() as conn:
            ph = ",".join("?" * len(memory_ids))
            conn.execute(
                f"UPDATE memories SET topic = ?, updated_at = ? "
                f"WHERE id IN ({ph})",
                [topic, now] + memory_ids
            )

    def archive_memory(self, memory_id: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET is_active = 0, updated_at = ? WHERE id = ?",
                (self._now(), memory_id)
            )

    def bulk_archive(self, memory_ids: List[int]):
        if not memory_ids:
            return
        now = self._now()
        with self._connect() as conn:
            ph = ",".join("?" * len(memory_ids))
            conn.execute(
                f"UPDATE memories SET is_active = 0, updated_at = ? "
                f"WHERE id IN ({ph})",
                [now] + memory_ids
            )

    def delete_memories(self, memory_ids: List[int]):
        """Hard-delete memories (for garbage cleanup)."""
        if not memory_ids:
            return
        with self._connect() as conn:
            ph = ",".join("?" * len(memory_ids))
            conn.execute(
                f"DELETE FROM memories WHERE id IN ({ph})", memory_ids
            )

    def update_importance(self, memory_id: int, importance: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET importance = ?, updated_at = ? WHERE id = ?",
                (max(1, min(5, importance)), self._now(), memory_id)
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

    def delete_topic(self, project_id: int, name: str):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM topics WHERE project_id = ? AND name = ?",
                (project_id, name)
            )

    # ── keywords ─────────────────────────────────────────────────────────────

    def upsert_keywords(self, project_id: int, freq_map: Dict[str, int]):
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

    def delete_plan(self, plan_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))

    def update_plan_content(self, plan_id: int, content: str) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE plans SET content = ?, updated_at = ? WHERE id = ?",
                (content, now, plan_id)
            )

    def reorder_plans(self, project_id: int, plan_ids: List[int]) -> None:
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
            n_topics = conn.execute(
                "SELECT COUNT(*) FROM topics WHERE project_id = ?",
                (project_id,)
            ).fetchone()[0]
            return {
                "n_sessions":     n_sessions,
                "n_memories":     n_memories,
                "n_active_plans": n_plans,
                "n_topics":       n_topics,
                "by_category":    [dict(r) for r in by_cat],
                "last_session":   last_session[0] if last_session else None,
            }

    # ── cross-project (global DB) ─────────────────────────────────────────────

    @classmethod
    def global_db(cls) -> "MemoryDB":
        global_path = Path.home() / ".claude" / "hooks" / "cc-memory" / "global.db"
        return cls(global_path)

    def get_all_projects(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY last_active DESC"
            ).fetchall()
            return [dict(r) for r in rows]
