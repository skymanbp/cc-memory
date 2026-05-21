"""
SQLite database layer.

Schema (3NF normalized, see docs/ARCHITECTURE.md §3):
  projects          one row per project path
  sessions          one row per compaction event
  memories          extracted facts (category + importance + topic + content_hash + supersedes_id)
  topics            consolidated summaries (L1 in hierarchy)
  keywords          auto-detected project vocabulary with frequency
  plans             execution queue (status: draft/evaluating/ready/executing/done/failed/skipped)
  observations      raw PostToolUse events (cleaned up after extraction)
  session_summaries 6-field structured summary per session
  progress          per-project PROGRESS.md backing store (single row per project)
  _migrations       migration tracking

Memory hierarchy:
  L0 Global overview   (derived from all topic summaries)
  L1 Topic summaries   (topics table — always injected)
  L2 Active memories   (memories table — injected by relevance)
  L3 Archived          (is_active=0 — queryable but not injected)

Anti-patch contract (v2.1):
  Memory updates flow through llm.memory_writer.upsert_smart, which uses
  `update_memory` (modify in place) or `supersede_memory` (archive+link)
  instead of appending. The supersedes_id column forms the update chain.
"""
import hashlib
import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    last_active TEXT    NOT NULL
);

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

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    session_id  INTEGER          REFERENCES sessions(id),
    category    TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    importance  INTEGER NOT NULL DEFAULT 2 CHECK(importance BETWEEN 1 AND 5),
    tags        TEXT    NOT NULL DEFAULT '[]',
    topic       TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    name        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (project_id, name)
);

CREATE TABLE IF NOT EXISTS keywords (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    keyword     TEXT    NOT NULL,
    frequency   INTEGER NOT NULL DEFAULT 1,
    last_seen   TEXT    NOT NULL,
    UNIQUE (project_id, keyword)
);

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

_MIGRATIONS = [
    ("v1_topic_column", "ALTER TABLE memories ADD COLUMN topic TEXT"),
    ("v1_topic_index",
     "CREATE INDEX IF NOT EXISTS idx_memories_topic ON memories (project_id, topic, is_active)"),

    ("v2_content_hash", "ALTER TABLE memories ADD COLUMN content_hash TEXT"),
    ("v2_content_hash_idx",
     "CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories (project_id, content_hash)"),

    ("v2_observations", """
        CREATE TABLE IF NOT EXISTS observations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id),
            session_id  TEXT,
            tool_name   TEXT    NOT NULL,
            tool_input  TEXT,
            tool_output TEXT,
            timestamp   TEXT    NOT NULL,
            is_private  INTEGER NOT NULL DEFAULT 0
        )"""),
    ("v2_observations_idx",
     "CREATE INDEX IF NOT EXISTS idx_obs_project_ts ON observations (project_id, timestamp DESC)"),

    ("v2_session_summaries", """
        CREATE TABLE IF NOT EXISTS session_summaries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL REFERENCES sessions(id),
            project_id      INTEGER NOT NULL REFERENCES projects(id),
            request         TEXT,
            investigated    TEXT,
            learned         TEXT,
            completed       TEXT,
            next_steps      TEXT,
            notes           TEXT,
            files_read      TEXT    DEFAULT '[]',
            files_modified  TEXT    DEFAULT '[]',
            created_at      TEXT    NOT NULL
        )"""),

    ("v2_project_mode",
     "ALTER TABLE projects ADD COLUMN mode TEXT NOT NULL DEFAULT 'code'"),

    ("v2_fts5", "__FTS5_SETUP__"),
    ("v2_backfill_hash", "__BACKFILL_HASH__"),

    # ── v3 migrations (anti-patch + forced handoff) ──────────────────────────

    # Supersede chain: when memory_writer.upsert_smart replaces an older memory,
    # the new row references the old via supersedes_id (and old is archived).
    # This preserves the update history instead of stacking N copies.
    ("v3_supersedes",
     "ALTER TABLE memories ADD COLUMN supersedes_id INTEGER"),
    ("v3_supersedes_idx",
     "CREATE INDEX IF NOT EXISTS idx_memories_supersedes ON memories (supersedes_id)"),

    # PROGRESS.md backing store: one row per project, ALWAYS overwritten,
    # never appended. SOT for memory/PROGRESS.md.
    ("v3_progress", """
        CREATE TABLE IF NOT EXISTS progress (
            project_id        INTEGER PRIMARY KEY REFERENCES projects(id),
            current_request   TEXT    DEFAULT '',
            status_done       TEXT    DEFAULT '',
            status_in_flight  TEXT    DEFAULT '',
            status_blocked    TEXT    DEFAULT '',
            open_todos        TEXT    DEFAULT '[]',
            plan              TEXT    DEFAULT '',
            critical_context  TEXT    DEFAULT '[]',
            files_touched     TEXT    DEFAULT '[]',
            transcript_ptr    TEXT    DEFAULT '',
            updated_at        TEXT    NOT NULL,
            trigger_type      TEXT    DEFAULT ''
        )"""),
]


class MemoryDB:
    """Project-local SQLite wrapper. See module docstring for schema."""

    _fts5_available = False

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA temp_store = memory")
        conn.execute("PRAGMA mmap_size = 268435456")
        conn.execute("PRAGMA cache_size = 10000")
        return conn

    def _bootstrap(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
        self._run_migrations()
        self._detect_fts5()
        # why: defensive — if v1_topic_column migration was skipped/lost we
        # still want the topic column to exist (used by all read paths)
        with self._connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
            if "topic" not in cols:
                conn.execute("ALTER TABLE memories ADD COLUMN topic TEXT")

    def _run_migrations(self):
        with self._connect() as conn:
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
                    if sql == "__FTS5_SETUP__":
                        self._setup_fts5(conn)
                    elif sql == "__BACKFILL_HASH__":
                        self._backfill_content_hash(conn)
                    else:
                        conn.execute(sql)
                except sqlite3.OperationalError:
                    # why: ALTER TABLE re-runs on an already-migrated DB throw
                    # "duplicate column" — record as applied so we don't retry
                    pass
                conn.execute(
                    "INSERT OR IGNORE INTO _migrations (name, applied_at) VALUES (?, ?)",
                    (name, self._now())
                )

    def _setup_fts5(self, conn):
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(test_col)")
            conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        except sqlite3.OperationalError:
            # why: FTS5 not compiled into this sqlite build — fall back to LIKE in search_fts
            self.__class__._fts5_available = False
            return

        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, tags, topic, content=memories, content_rowid=id)
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content, tags, topic)
                    VALUES (new.id, new.content, new.tags, COALESCE(new.topic, ''));
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, tags, topic)
                    VALUES ('delete', old.id, old.content, old.tags, COALESCE(old.topic, ''));
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, tags, topic)
                    VALUES ('delete', old.id, old.content, old.tags, COALESCE(old.topic, ''));
                    INSERT INTO memories_fts(rowid, content, tags, topic)
                    VALUES (new.id, new.content, new.tags, COALESCE(new.topic, ''));
                END
            """)
            conn.execute("""
                INSERT OR IGNORE INTO memories_fts(rowid, content, tags, topic)
                SELECT id, content, tags, COALESCE(topic, '') FROM memories
            """)
            self.__class__._fts5_available = True
        except sqlite3.OperationalError:
            # why: FTS5 setup race or DDL conflict — degrade to LIKE search
            self.__class__._fts5_available = False

    def _detect_fts5(self):
        try:
            with self._connect() as conn:
                conn.execute("SELECT rowid FROM memories_fts LIMIT 0")
                self.__class__._fts5_available = True
        except sqlite3.OperationalError:
            # why: FTS table missing or corrupted; LIKE fallback handles search
            self.__class__._fts5_available = False

    def _backfill_content_hash(self, conn):
        rows = conn.execute(
            "SELECT id, content FROM memories WHERE content_hash IS NULL"
        ).fetchall()
        for row in rows:
            h = self.compute_content_hash(row["content"])
            conn.execute(
                "UPDATE memories SET content_hash = ? WHERE id = ?",
                (h, row["id"])
            )

    def _rebuild_fts5(self):
        if not self._fts5_available:
            return
        try:
            with self._connect() as conn:
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        except sqlite3.OperationalError:
            # why: rebuild failed (corrupted index) — mark unavailable so search
            # falls back to LIKE instead of repeatedly hitting the broken index
            self.__class__._fts5_available = False

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

    def insert_session(self, project_id, claude_session_id, trigger_type,
                       msg_count, archive_path, brief_summary):
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

    def get_recent_session_ids(self, project_id, n=3):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE project_id = ? "
                "ORDER BY compacted_at DESC LIMIT ?",
                (project_id, n)
            ).fetchall()
            return [r["id"] for r in rows]

    def get_session_count(self, project_id):
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id = ?",
                (project_id,)
            ).fetchone()[0]

    # ── memories: insert / read ──────────────────────────────────────────────

    def insert_memory(self, project_id, session_id, category, content,
                      importance=2, tags=None, topic=None, supersedes_id=None):
        """Direct insert. Most callers should go through llm.memory_writer.upsert_smart."""
        now = self._now()
        content_hash = self.compute_content_hash(content)
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO memories
                   (project_id, session_id, category, content, importance,
                    tags, topic, content_hash, supersedes_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, session_id, category, content, importance,
                 json.dumps(tags or [], ensure_ascii=False), topic,
                 content_hash, supersedes_id, now, now)
            )
            return cur.lastrowid

    def update_memory(self, memory_id, content=None, importance=None,
                      topic=None, tags=None, category=None):
        """Modify a memory IN PLACE (anti-patch: no new row, no stacking)."""
        now = self._now()
        fields, params = [], []
        if content is not None:
            fields.append("content = ?")
            fields.append("content_hash = ?")
            params += [content, self.compute_content_hash(content)]
        if importance is not None:
            fields.append("importance = ?")
            params.append(max(1, min(5, importance)))
        if topic is not None:
            fields.append("topic = ?")
            params.append(topic)
        if tags is not None:
            fields.append("tags = ?")
            params.append(json.dumps(tags, ensure_ascii=False))
        if category is not None:
            fields.append("category = ?")
            params.append(category)
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(now)
        params.append(memory_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE memories SET {', '.join(fields)} WHERE id = ?",
                params
            )

    def supersede_memory(self, old_id, new_content, project_id, session_id,
                         category, importance=3, tags=None, topic=None):
        """Archive old memory and insert new one linked via supersedes_id.

        Use when the new content is a strict improvement / consolidation of the
        old (different enough to merit a row, but logically the same fact).
        Preserves history while keeping the live set clean.
        """
        new_id = self.insert_memory(
            project_id, session_id, category, new_content,
            importance=importance, tags=tags, topic=topic,
            supersedes_id=old_id,
        )
        self.archive_memory(old_id)
        return new_id

    def get_supersede_chain(self, memory_id):
        """Walk backwards through supersedes_id links. Returns list newest-first."""
        chain = []
        with self._connect() as conn:
            cur_id = memory_id
            seen = set()
            while cur_id and cur_id not in seen:
                seen.add(cur_id)
                row = conn.execute(
                    "SELECT * FROM memories WHERE id = ?", (cur_id,)
                ).fetchone()
                if not row:
                    break
                chain.append(dict(row))
                cur_id = row["supersedes_id"]
        return chain

    def get_recent_memories(self, project_id, sessions_back=3,
                            categories=None, min_importance=1, limit=30):
        session_ids = self.get_recent_session_ids(project_id, sessions_back)
        if not session_ids:
            return []
        ph = ",".join("?" * len(session_ids))
        params = [project_id, min_importance] + session_ids
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

    def get_critical_memories(self, project_id, min_importance=4):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1
                     AND importance >= ?
                   ORDER BY importance DESC, updated_at DESC""",
                (project_id, min_importance)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_active_memories(self, project_id):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1
                   ORDER BY topic, importance DESC, created_at DESC""",
                (project_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_memories_by_topic(self, project_id, topic):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1 AND topic = ?
                   ORDER BY importance DESC, created_at DESC""",
                (project_id, topic)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_unassigned_memories(self, project_id):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1
                     AND (topic IS NULL OR topic = '')
                   ORDER BY importance DESC, created_at DESC""",
                (project_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_topic_memory_counts(self, project_id):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT COALESCE(topic, '_unassigned') AS t, COUNT(*) AS n
                   FROM memories
                   WHERE project_id = ? AND is_active = 1
                   GROUP BY t ORDER BY n DESC""",
                (project_id,)
            ).fetchall()
            return {r["t"]: r["n"] for r in rows}

    def set_memory_topic(self, memory_id, topic):
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET topic = ?, updated_at = ? WHERE id = ?",
                (topic, self._now(), memory_id)
            )

    def bulk_set_topic(self, memory_ids, topic):
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

    def archive_memory(self, memory_id):
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET is_active = 0, updated_at = ? WHERE id = ?",
                (self._now(), memory_id)
            )

    def bulk_archive(self, memory_ids):
        if not memory_ids:
            return
        now = self._now()
        with self._connect() as conn:
            ph = ",".join("?" * len(memory_ids))
            conn.execute(
                f"UPDATE memories SET is_active = 0, updated_at = ? WHERE id IN ({ph})",
                [now] + memory_ids
            )

    def delete_memories(self, memory_ids):
        if not memory_ids:
            return
        with self._connect() as conn:
            ph = ",".join("?" * len(memory_ids))
            conn.execute(f"DELETE FROM memories WHERE id IN ({ph})", memory_ids)

    def update_importance(self, memory_id, importance):
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET importance = ?, updated_at = ? WHERE id = ?",
                (max(1, min(5, importance)), self._now(), memory_id)
            )

    # ── content hash + dedup ────────────────────────────────────────────────

    @staticmethod
    def compute_content_hash(content):
        normalized = content.strip().lower().encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()[:16]

    def is_duplicate_hash(self, project_id, content_hash):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM memories WHERE project_id = ? AND content_hash = ? "
                "AND is_active = 1 LIMIT 1",
                (project_id, content_hash)
            ).fetchone()
            return row is not None

    def find_by_hash(self, project_id, content_hash):
        """Return the active memory matching this hash, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE project_id = ? AND content_hash = ? "
                "AND is_active = 1 LIMIT 1",
                (project_id, content_hash)
            ).fetchone()
            return dict(row) if row else None

    # ── observations (PostToolUse) ──────────────────────────────────────────

    def insert_observation(self, project_id, session_id, tool_name,
                           tool_input="", tool_output="", is_private=0):
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO observations
                   (project_id, session_id, tool_name, tool_input,
                    tool_output, timestamp, is_private)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project_id, session_id, tool_name,
                 tool_input, tool_output, self._now(), is_private)
            )
            return cur.lastrowid

    def get_recent_observations(self, project_id, limit=50):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM observations
                   WHERE project_id = ? AND is_private = 0
                   ORDER BY timestamp DESC LIMIT ?""",
                (project_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_observations_since(self, project_id, since_ts):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM observations
                   WHERE project_id = ? AND timestamp > ? AND is_private = 0
                   ORDER BY timestamp ASC""",
                (project_id, since_ts)
            ).fetchall()
            return [dict(r) for r in rows]

    def cleanup_observations(self, project_id, before_ts):
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM observations WHERE project_id = ? AND timestamp < ?",
                (project_id, before_ts)
            )
            return cur.rowcount

    def get_observation_count(self, project_id):
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM observations WHERE project_id = ?",
                (project_id,)
            ).fetchone()[0]

    # ── session summaries ───────────────────────────────────────────────────

    def insert_session_summary(self, session_id, project_id, summary):
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO session_summaries
                   (session_id, project_id, request, investigated, learned,
                    completed, next_steps, notes, files_read, files_modified,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, project_id,
                 summary.get("request", ""),
                 summary.get("investigated", ""),
                 summary.get("learned", ""),
                 summary.get("completed", ""),
                 summary.get("next_steps", ""),
                 summary.get("notes", ""),
                 json.dumps(summary.get("files_read", []), ensure_ascii=False),
                 json.dumps(summary.get("files_modified", []), ensure_ascii=False),
                 self._now())
            )
            return cur.lastrowid

    def get_session_summary(self, session_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_summaries WHERE session_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_latest_summary(self, project_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_summaries WHERE project_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (project_id,)
            ).fetchone()
            return dict(row) if row else None

    # ── progress (per-project, single row, ALWAYS overwrite) ────────────────

    def upsert_progress(self, project_id, **fields):
        """Overwrite the project's progress row. Anti-patch: never appends."""
        now = self._now()
        defaults = {
            "current_request": "",
            "status_done": "",
            "status_in_flight": "",
            "status_blocked": "",
            "open_todos": "[]",
            "plan": "",
            "critical_context": "[]",
            "files_touched": "[]",
            "transcript_ptr": "",
            "trigger_type": "",
        }
        for k, v in fields.items():
            if isinstance(v, (list, dict)):
                fields[k] = json.dumps(v, ensure_ascii=False)
        merged = {**defaults, **fields}
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT project_id FROM progress WHERE project_id = ?",
                (project_id,)
            ).fetchone()
            if existing:
                cols = list(merged.keys()) + ["updated_at"]
                set_clause = ", ".join(f"{c} = ?" for c in cols)
                params = list(merged.values()) + [now, project_id]
                conn.execute(
                    f"UPDATE progress SET {set_clause} WHERE project_id = ?",
                    params
                )
            else:
                cols = ["project_id"] + list(merged.keys()) + ["updated_at"]
                placeholders = ",".join("?" * len(cols))
                params = [project_id] + list(merged.values()) + [now]
                conn.execute(
                    f"INSERT INTO progress ({','.join(cols)}) VALUES ({placeholders})",
                    params
                )

    def get_progress(self, project_id):
        """Return the project's progress row as dict (with JSON fields parsed), or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM progress WHERE project_id = ?", (project_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("open_todos", "critical_context", "files_touched"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except (json.JSONDecodeError, TypeError):
                # why: legacy/corrupted row — fall back to empty list rather
                # than crash the read path (PROGRESS.md generation must work)
                d[k] = []
        return d

    def patch_progress(self, project_id, **fields):
        """Update only specified fields without touching others.

        Used by Stop-hook to drip-update files_touched and open_todos each turn
        while leaving the full state intact. Distinct from upsert_progress which
        is the PreCompact full rewrite.
        """
        if not fields:
            return
        # Bootstrap empty row first if absent
        if not self.get_progress(project_id):
            self.upsert_progress(project_id)
        now = self._now()
        serialized = {}
        for k, v in fields.items():
            if isinstance(v, (list, dict)):
                serialized[k] = json.dumps(v, ensure_ascii=False)
            else:
                serialized[k] = v
        set_clause = ", ".join(f"{c} = ?" for c in serialized.keys()) + ", updated_at = ?"
        params = list(serialized.values()) + [now, project_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE progress SET {set_clause} WHERE project_id = ?",
                params
            )

    # ── FTS5 search ─────────────────────────────────────────────────────────

    def search_fts(self, project_id, query, limit=30):
        with self._connect() as conn:
            if self._fts5_available:
                try:
                    rows = conn.execute(
                        """SELECT m.* FROM memories m
                           JOIN memories_fts f ON m.id = f.rowid
                           WHERE memories_fts MATCH ?
                             AND m.project_id = ? AND m.is_active = 1
                           ORDER BY rank
                           LIMIT ?""",
                        (query, project_id, limit)
                    ).fetchall()
                    return [dict(r) for r in rows]
                except sqlite3.OperationalError:
                    # why: query parse error or corrupted FTS index — try
                    # rebuild once, then fall through to LIKE search
                    self._rebuild_fts5()
            pat = "%" + query + "%"
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1
                     AND (content LIKE ? OR tags LIKE ? OR COALESCE(topic, '') LIKE ?)
                   ORDER BY importance DESC, created_at DESC
                   LIMIT ?""",
                (project_id, pat, pat, pat, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── project mode ────────────────────────────────────────────────────────

    def get_project_mode(self, project_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT mode FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            return row["mode"] if row and row["mode"] else "code"

    def set_project_mode(self, project_id, mode):
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET mode = ? WHERE id = ?",
                (mode, project_id)
            )

    # ── topics ───────────────────────────────────────────────────────────────

    def upsert_topic(self, project_id, name, content):
        now = self._now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, version FROM topics WHERE project_id = ? AND name = ?",
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

    def get_topics(self, project_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, content, updated_at, version FROM topics "
                "WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_topic(self, project_id, name):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM topics WHERE project_id = ? AND name = ?",
                (project_id, name)
            )

    # ── keywords ─────────────────────────────────────────────────────────────

    def upsert_keywords(self, project_id, freq_map):
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

    def get_top_keywords(self, project_id, n=40):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT keyword FROM keywords WHERE project_id = ? "
                "ORDER BY frequency DESC LIMIT ?",
                (project_id, n)
            ).fetchall()
            return [r["keyword"] for r in rows]

    # ── plans ────────────────────────────────────────────────────────────────

    def add_plan(self, project_id, content, exec_order=0):
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

    def get_plans(self, project_id, statuses=None):
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
                    "SELECT * FROM plans WHERE project_id = ? ORDER BY exec_order",
                    (project_id,)
                ).fetchall()
            return [dict(r) for r in rows]

    def get_active_plans(self, project_id):
        return self.get_plans(project_id,
                              statuses=["draft", "evaluating", "ready", "executing"])

    def update_plan_status(self, plan_id, status, notes=None, field="feasibility"):
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

    def get_next_plan(self, project_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plans WHERE project_id = ? AND status = 'ready' "
                "ORDER BY exec_order LIMIT 1",
                (project_id,)
            ).fetchone()
            return dict(row) if row else None

    def clear_done_plans(self, project_id):
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM plans WHERE project_id = ? "
                "AND status IN ('done', 'failed', 'skipped')",
                (project_id,)
            )
            return cur.rowcount

    def delete_plan(self, plan_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))

    def update_plan_content(self, plan_id, content):
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE plans SET content = ?, updated_at = ? WHERE id = ?",
                (content, now, plan_id)
            )

    def reorder_plans(self, project_id, plan_ids):
        now = self._now()
        with self._connect() as conn:
            for order, pid in enumerate(plan_ids, 1):
                conn.execute(
                    "UPDATE plans SET exec_order = ?, updated_at = ? "
                    "WHERE id = ? AND project_id = ?",
                    (order, now, pid, project_id)
                )

    # ── analytics / stats ────────────────────────────────────────────────────

    def get_stats(self, project_id):
        with self._connect() as conn:
            n_sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id = ?",
                (project_id,)
            ).fetchone()[0]
            n_memories = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE project_id = ? AND is_active = 1",
                (project_id,)
            ).fetchone()[0]
            by_cat = conn.execute(
                """SELECT category, COUNT(*) AS n, AVG(importance) AS avg_imp
                   FROM memories WHERE project_id = ? AND is_active = 1
                   GROUP BY category ORDER BY n DESC""",
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

    def get_all_projects(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY last_active DESC"
            ).fetchall()
            return [dict(r) for r in rows]
