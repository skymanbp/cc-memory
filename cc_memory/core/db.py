"""
SQLite database layer.

Schema (3NF normalized, see docs/ARCHITECTURE.md §4 "Database schema"):
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
import contextlib
import hashlib
import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
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
    #
    # NOTE — supersedes_id deliberately declares NO FOREIGN KEY, and that is not
    # an oversight to "fix later". PRAGMA foreign_keys IS ON (see _connect), but
    # SQLite cannot add a REFERENCES clause to an existing column: retrofitting
    # one means a full table rebuild (create-new / copy / drop / rename) on every
    # install in the field, which is out of scope for a point release. The
    # consequence is that a HARD DELETE of a superseded row leaves a dangling
    # supersedes_id that nothing catches — so every delete path must archive
    # (is_active = 0) via archive_memory / bulk_archive / archive_obsolete
    # instead of DELETE. delete_memories() is the one exception and is for
    # user-driven purges only.
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

    # ── v4 migrations (live plan anchor) ─────────────────────────────────────

    # Single live plan per project. ExitPlanMode raw output lands in `raw`,
    # the refiner subagent normalises it into `structured` (JSON). TodoWrite
    # syncs step status mechanically (no LLM). Drift counters drive the
    # guardian-nudge logic in the Stop hook.
    ("v4_plan_active", """
        CREATE TABLE IF NOT EXISTS plan_active (
            project_id                INTEGER PRIMARY KEY REFERENCES projects(id),
            raw                       TEXT    DEFAULT '',
            structured                TEXT    DEFAULT '',
            active_step               INTEGER DEFAULT 0,
            edits_since_last_guardian INTEGER DEFAULT 0,
            turns_since_last_guardian INTEGER DEFAULT 0,
            last_guardian_at          TEXT    DEFAULT '',
            last_refined_at           TEXT    DEFAULT '',
            needs_refine              INTEGER DEFAULT 0,
            created_at                TEXT    NOT NULL,
            updated_at                TEXT    NOT NULL
        )"""),

    # ── v5 migrations (session annotation) ───────────────────────────────────

    # Tag the progress row with which Claude session owns it. Without this,
    # multi-session workflows can't tell from PROGRESS.md whether they're
    # reading their own write or a stale write from a different session.
    # Both columns are TEXT (Claude session UUIDs + ISO timestamps).
    ("v5_progress_session_id",
     "ALTER TABLE progress ADD COLUMN current_session_id TEXT DEFAULT ''"),
    ("v5_progress_session_started_at",
     "ALTER TABLE progress ADD COLUMN session_started_at TEXT DEFAULT ''"),

    # ── v6 migrations (reference-aware aging) ────────────────────────────────

    # last_referenced_at records the last time a memory was INJECTED into a
    # session (SessionStart). "Effective age" for the staleness/decay net is
    # computed as now - COALESCE(last_referenced_at, created_at). We key on
    # created_at (immutable) NOT updated_at — because maintenance ops
    # (decay, bulk_set_topic, archive) bump updated_at and would otherwise
    # corrupt the age signal. A referenced fact stays "young"; an
    # un-injected low-importance fact ages out. NULL until first injection.
    ("v6_last_referenced_at",
     "ALTER TABLE memories ADD COLUMN last_referenced_at TEXT"),
    ("v6_last_referenced_idx",
     "CREATE INDEX IF NOT EXISTS idx_memories_lastref "
     "ON memories (project_id, is_active, last_referenced_at)"),

    # ── v7 migrations (concurrency / identity, v2.8.0) ───────────────────────

    # plan_active optimistic concurrency: every UPDATE bumps `revision`, and a
    # writer that computed its state from a read passes the revision it read
    # (update_plan_if_revision) — rowcount 0 means the plan changed underneath
    # it. Closes the stale-TodoWrite-sync resurrection of a replaced plan
    # (register X4). The partial unique index on active memories deliberately
    # does NOT live in this ledger: the ledger records INTENT, not state
    # (_detect_fts5's lesson), so the index is presence-checked and healed in
    # _bootstrap instead — see _ensure_active_hash_unique.
    ("v7_plan_revision",
     "ALTER TABLE plan_active ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"),

    # A sessions row used to be published BEFORE its memories / summary
    # existed, and _get_saved_session_ids read the bare row as "saved" — a
    # process killed between insert_session and the writes made that
    # transcript permanently skipped (register X6). DEFAULT 0 + a one-shot
    # backfill, in that order (register r6-A1): DEFAULT 1 re-opened the same
    # hole for MIXED versions — hooks load code from the working tree at fire
    # time, so during an upgrade a still-running v2.7 hook INSERTs without
    # the column and the default became its receipt. With DEFAULT 0 that
    # insert reads as an unreceipted claim and the retroactive pass simply
    # re-transcribes it (idempotent through upsert_smart — the safe
    # direction). The backfill receipts the PRE-upgrade rows, whose writers
    # only ever inserted after deciding to save. A dev DB that applied the
    # earlier DEFAULT 1 spelling keeps that default (its ledger marker skips
    # the ALTER); only the mixed-version window differs there, and a dev
    # checkout's hooks run tree code.
    ("v7_sessions_complete",
     "ALTER TABLE sessions ADD COLUMN complete INTEGER NOT NULL DEFAULT 0"),
    ("v7_sessions_complete_backfill",
     "UPDATE sessions SET complete = 1"),

    # The two indexes the round-7 recency predicates need. Both of those
    # queries run on hooks with HARD host timeouts, and both were written
    # against columns no index covered — a correctness fix that made the hot
    # path quadratic is not a fix, it is a trade nobody agreed to.
    #
    # idx_memories_session: `get_recent_session_ids`'s
    # `EXISTS (SELECT 1 FROM memories m WHERE m.session_id = s.id AND
    # m.is_active = 1)` planned as `SCAN m USING INDEX idx_memories_active_hash`
    # once per candidate session, and the ORDER BY forces a temp b-tree so the
    # LIMIT prunes nothing. Measured at 300 sessions / 5 000 memories:
    # 2.68 ms with 0 unreceipted claims, 47.41 ms with 150. It backs the
    # SessionStart injection (15 s budget) and every MCP write.
    #
    # idx_sessions_sid: `get_recent_sessions`'s per-`claude_session_id`
    # correlated `MAX(s2.id)` scanned all of the project's rows per row, on a
    # table whose only index is (project_id, compacted_at DESC). Measured
    # against the same query with the dedup removed: 250 sessions 11.16 ms vs
    # 0.04, 1 000 143.06 vs 0.15, 2 000 557.68 vs 0.29 — quadratic in the one
    # number that only ever grows. `write_progress_md` calls it on the Stop
    # hook every single turn.
    ("v7_memories_session_idx",
     "CREATE INDEX IF NOT EXISTS idx_memories_session "
     "ON memories (session_id, is_active)"),
    ("v7_sessions_sid_idx",
     "CREATE INDEX IF NOT EXISTS idx_sessions_sid "
     "ON sessions (project_id, claude_session_id, id)"),

    # The Stop observer's watermark, moved out of a per-SESSION temp marker.
    # Observations are per PROJECT and are deleted only by PreCompact, while
    # the marker was keyed by session id — so EVERY new session started with no
    # watermark and replayed the project's whole unconsumed backlog, 20 rows
    # and one Anthropic call per Stop, over rows an earlier session had already
    # analysed. `core/markers.py` also documents that a marker is ALLOWED to
    # vanish (it refuses a parent that is not private), and in that state the
    # replay is permanent: measured 6 consecutive Stops, 6 identical LLM calls,
    # all fed ids 1-20, with ids 21-60 never entering a prompt. `upsert_smart`
    # deduplicates the replies, so the cost is not duplicate rows — it is a
    # wasted call per turn and a "realtime" observer that never reaches the
    # live session. The watermark is the one piece of that loop that has to be
    # durable, so it lives where the observations do.
    ("v7_projects_obs_watermark",
     "ALTER TABLE projects ADD COLUMN obs_watermark INTEGER NOT NULL DEFAULT 0"),

    # ── v8 migrations (directive ledger) ─────────────────────────────────────

    # The standing-instruction ledger. Motivating incident (lore_disaster,
    # 2026-08-15): a full-transcript audit of 416 deduped user messages found
    # a mod mechanic the user had demanded SIX separate times with zero
    # implementation, and a pause rule stated THREE times that was violated
    # the first time it mattered. Neither was detectable, because nothing in
    # this project — or in ccm — ever recorded what the user asked for.
    #
    # Why a table and not plan steps: a plan step is a unit of EXECUTION and
    # dies when the plan is replaced or the step is marked done. A directive
    # is a unit of INTENT and outlives every plan; folding one into the other
    # is exactly how the six-times-repeated mechanic vanished — it was never
    # a step in the plan that happened to be active.
    #
    # `source` mirrors the Scanned/Manual split that keeps a rescan from
    # destroying hand annotation: 'user' rows are authored from what the user
    # actually said and are never rewritten by machinery; 'derived' rows may
    # be refreshed by tooling. `times_stated` is the repetition count — the
    # single strongest signal of importance that a plan cannot express.
    ("v8_directives", """
        CREATE TABLE IF NOT EXISTS directives (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id    INTEGER NOT NULL REFERENCES projects(id),
            slug          TEXT    NOT NULL,
            quote         TEXT    NOT NULL DEFAULT '',
            demand        TEXT    NOT NULL DEFAULT '',
            kind          TEXT    NOT NULL DEFAULT 'standing',
            status        TEXT    NOT NULL DEFAULT 'active',
            times_stated  INTEGER NOT NULL DEFAULT 1,
            source        TEXT    NOT NULL DEFAULT 'user',
            evidence      TEXT    NOT NULL DEFAULT '',
            first_seen_at TEXT    NOT NULL,
            last_seen_at  TEXT    NOT NULL,
            closed_at     TEXT    NOT NULL DEFAULT '',
            created_at    TEXT    NOT NULL,
            updated_at    TEXT    NOT NULL
        )"""),
    ("v8_directives_slug_idx",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_directives_slug "
     "ON directives (project_id, slug)"),
    ("v8_directives_status_idx",
     "CREATE INDEX IF NOT EXISTS idx_directives_status "
     "ON directives (project_id, status)"),

    # ── v9: a MONOTONIC turn clock, so directive idleness has a real baseline ─
    #
    # v2.11.1 measured a directive's idleness from
    # `plan_active.turns_since_last_guardian`, which is a project-wide counter
    # that every active directive was stamped with — so one recorded ten
    # seconds ago was announced as "no progress for 40 turns" and refused the
    # user's turn over it. The v2.11.1 fix was a "has it been touched since the
    # guardian window opened?" guard: it removed the false positive but it is
    # an approximation, because that counter RESETS (`/cc-mem plan-check` and
    # every plan replacement zero it). Under it, a directive genuinely untouched
    # for 100 turns looks fresh again the moment anyone runs a guardian check —
    # the ledger silently forgives exactly the neglect it exists to surface.
    #
    # `turns_total` is therefore a SECOND counter that is only ever incremented
    # (`bump_plan_turn_counter` bumps both; nothing resets this one), and each
    # directive records the value it was last touched at. Idleness becomes
    # subtraction between two monotonic numbers, which no reset can distort.
    # Both DEFAULT 0, so an upgraded database reads every existing directive as
    # touched at turn 0 — i.e. as old as the project, which is the safe
    # direction for a ledger whose job is to notice neglect.
    ("v9_plan_turns_total",
     "ALTER TABLE plan_active ADD COLUMN turns_total INTEGER NOT NULL "
     "DEFAULT 0"),
    ("v9_directives_turns_at_touch",
     "ALTER TABLE directives ADD COLUMN turns_at_touch INTEGER NOT NULL "
     "DEFAULT 0"),
]


# THE category vocabulary (register M3). One tuple, imported by every
# validator, argparse choice list, combobox, MCP schema and LLM prompt that
# names the categories — it existed as 13+ hand-synced literals across 9
# files, four of them inside prompt text, which is the same prose-enumeration
# disease `tools/doc_claims.py` exists to kill in the docs. The single
# deliberate literal left is the web viewer's in-browser JS constant, which
# cannot import Python; its comment points here.
CATEGORIES = ("decision", "result", "config", "bug", "task", "arch", "note")


def _readonly_uri(posix_path):
    """`file:` URI with `?mode=ro` for a RESOLVED path in as_posix() form.

    Pure, so every platform can test every shape (a test that only runs on
    the platform whose shape it checks is how the POSIX form shipped broken
    from v2.8.0 through v2.12.0):
        `/tmp/a b/x.db`      -> `file:/tmp/a%20b/x.db?mode=ro`   (POSIX)
        `D:/a/x.db`          -> `file:/D%3A/a/x.db?mode=ro`      (drive)
        `//srv/share/x.db`   -> `file://srv/share/x.db?mode=ro`  (UNC, authority)
    The rule is simply "add the slash only when the path does not start with
    one": a POSIX path and a UNC path both already carry theirs, and adding a
    second to a POSIX path is exactly what turned its first segment into an
    authority.
    """
    import urllib.parse
    prefix = "file:" if posix_path.startswith("/") else "file:/"
    return prefix + urllib.parse.quote(posix_path) + "?mode=ro"


def readonly_connect(db_path):
    """An ENGINE-enforced read-only connection (register E2).

    The one implementation behind every user-facing SQL console
    (`ui/dashboard.py` and `cli/mem.py sql`). Their lexical classifiers
    remain as UX — deciding when to warn — but enforcement lives here, in
    the engine, where a classifier miss cannot reach: a single-statement
    CTE-DML shape passed the dashboard's regex as "read-only" while SQLite
    happily ran the DELETE (measured). On `mode=ro` the same statement fails
    with "attempt to write a readonly database". The authorizer closes the
    one road out of ro — ATTACHing a second, writable database.

    The URI is built by `_readonly_uri`, which has to get THREE path shapes
    right and got only the two Windows ones right until v2.12.1: drive paths
    (`file:/D%3A/...`, verified: colon and spaces percent-encode cleanly and
    `?mode=ro` is accepted) and UNC paths, which take the AUTHORITY form the
    SQLite URI documentation defines — `file://server/share/...` maps back to
    `\\\\server\\share\\...` (register r6-C7: prefixing the extra slash turned
    it into the local path `/server/share/...`, which cannot exist, so
    read-only consoles failed outright on mapped-share projects). A POSIX
    absolute path was given the drive-path prefix, producing
    `file://tmp/x/memory.db` — SQLite reads `tmp` as an authority and raises
    `invalid uri authority`, so `/cc-mem sql` and the dashboard console never
    worked on Linux or macOS. Measured on the Linux gate lanes the first time
    a smoke test drove `sql` as a subprocess (v2.12.1). query_only was
    evaluated as an alternative and REJECTED: it is a pragma a later
    statement can switch back off, and it allowed `PRAGMA journal_mode=DELETE`
    — a file write — where mode=ro refuses at the engine.
    """
    conn = sqlite3.connect(_readonly_uri(Path(db_path).resolve().as_posix()),
                           uri=True)
    conn.row_factory = sqlite3.Row

    def _authorize(action, *_rest):
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(_authorize)
    return conn


class MemoryDB:
    """Project-local SQLite wrapper. See module docstring for schema."""

    # CLASS-level DEFAULT only; every write below is to the INSTANCE.
    # It described a per-DATABASE property from class state, so opening
    # a second project whose index was missing flipped the flag for the
    # first one's live handle too — measured, an existing project's
    # search silently switched from MATCH to LIKE semantics mid-process
    # ('toke' matched 'token' under LIKE, 0 rows under MATCH).
    _fts5_available = False

    @staticmethod
    def _is_reparse(p):
        """True for a symlink OR a Windows junction (or an un-lstat-able
        path). is_symlink() is an lstat and misses junctions; isjunction is
        3.12+, and on older interpreters only the symlink half applies —
        recorded as a residual, not silently absorbed."""
        try:
            if Path(p).is_symlink():
                return True
        except OSError:
            return True
        isj = getattr(os.path, "isjunction", None)
        if isj is None:
            return False
        try:
            return bool(isj(str(p)))
        except OSError:
            return True

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        # Fail-closed link refusal at THE choke every surface passes through
        # (register r6-C2, extending Y1): core/roots refuses a linked
        # memory/ as project IDENTITY and ensure_memory_dir refuses it on
        # the hook write path — but the MCP server, the dashboard's
        # projects.json registry and the web viewer all construct MemoryDB
        # DIRECTLY, and through a link every one of their writes landed at
        # the link's target. Probing here covers every present and future
        # opener; two lstats per open is the cost.
        for probe in (self.db_path.parent, self.db_path):
            if self._is_reparse(probe):
                raise OSError(
                    f"{probe} is a symlink/junction; cc-memory refuses to "
                    f"operate through links (privacy fail-closed — use a "
                    f"real directory, or pin exotic layouts with .ccm-root)")
        # No parents=True: this line creates memory/, and with parents=True it
        # also recreated a PROJECT directory the user had deleted, turning a
        # vanished project into an empty shell on the next hook. The stdlib
        # raises FileNotFoundError for exactly this case, so the backstop costs
        # a flag rather than a branch. `core.progress.ensure_memory_dir` is the
        # named choke point; this catches anything that opens a database
        # without going through it.
        self.db_path.parent.mkdir(exist_ok=True)
        self._ensure_gitignore()
        self._bootstrap()

    def _ensure_gitignore(self):
        """Write memory/.gitignore beside the database, always.

        This belongs HERE because this is the line that brings a `memory/`
        directory into existence — every caller that creates a database goes
        through it. Leaving the ignore file to callers meant each new creator
        forgot: `cli/mem.py` has thirteen `MemoryDB(...)` sites and none of
        them wrote it, so a first `/cc-mem add` in a git repo left a 143 KB
        `memory.db` git-trackable — `git add -A` staged the binary, which is
        exactly how one rode into three commits of a sibling repository.

        Idempotent and additive by design (see `ensure_memory_gitignore`): on
        an existing project it early-returns unless an older install is
        missing a line, so opening a database costs one read, not a write.

        Imported lazily because `core.progress` imports `core.db` at module
        scope; by the time any instance is constructed, this module is fully
        loaded, so the cycle cannot bite.
        """
        try:
            from core.progress import ensure_memory_gitignore
            ensure_memory_gitignore(self.db_path.parent)
        except Exception:
            # why: an ignore file that cannot be written must never stop the
            # database from opening — that would turn a cosmetic failure into
            # total data loss. ensure_memory_gitignore already swallows OSError
            # itself; this catches an unavailable import on a partial install.
            pass

    @contextlib.contextmanager
    def _connect(self):
        """Yield a connection that COMMITS on success, ROLLS BACK on error, and
        — unlike `sqlite3.Connection.__exit__` — ALWAYS CLOSES.

        This is a context manager, not a factory: every call site in the
        package consumes it as `with self._connect() as conn:`, so the `with`
        kept working unchanged when the close was added (at that change, 66
        sites here + 15 elsewhere; the count grows with the schema, so it is
        not restated — `grep -c "self\\._connect()"` is authoritative).

        WHY it had to change. `sqlite3.Connection.__exit__` commits or rolls
        back but does NOT close (documented CPython behaviour). Each call site
        therefore leaked one connection, which its own statement-cache reference
        cycle then kept alive until the cyclic GC happened to run. Measured at
        v2.5.1: 4 live `sqlite3.Connection` objects after `MemoryDB(...)`, 5
        after one `upsert_project()`, 25 after 20 further `insert_memory()`
        calls — one per operation, linear and unbounded. `ui/dashboard.py`,
        `ui/web_viewer.py` and `mcp/server.py` each hold a MemoryDB for their
        whole process lifetime, so every refresh / request / tool call added a
        handle carrying this block's 256 MiB `mmap_size`; and on Windows an open
        handle makes memory.db undeletable (`PermissionError [WinError 32]` on
        `shutil.rmtree`, which is why both test suites have to sweep
        `gc.get_objects()` before their teardown).

        WHAT MUST NOT CHANGE is the transaction semantics — every write path in
        the plugin depends on them, so they reproduce `Connection.__exit__`
        exactly:
          - normal exit (including leaving the block via `return`, which most
            of the call sites do) -> commit;
          - ANY exception -> rollback, then re-raise. `__exit__` keys off
            `exc_type` alone, so it rolls back for BaseException too
            (KeyboardInterrupt / GeneratorExit included) — hence `except
            BaseException` and not `except Exception` here.
        Only the `close()` in the `finally` is new behaviour.

        Nesting stays legal: `search_fts` -> `_match_fts` -> `_rebuild_fts5`
        opens an inner connection inside an outer one. Each gets its own handle,
        WAL lets the inner writer proceed under the outer reader, and
        `busy_timeout` covers the cross-process case (the PreCompact sync leg
        running alongside the async consolidation leg).
        """
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            # READ the result. `journal_mode` is the one pragma that returns
            # the mode it ended up in, and it keeps the OLD mode SILENTLY when
            # the change is refused — measured here: an invalid mode returns
            # the previous value and raises nothing. SQLite refuses WAL on
            # network filesystems (sqlite.org/wal.html: WAL needs shared
            # memory, which hosts cannot share), and this codebase explicitly
            # contemplates projects on a `net use` share or a USB stick
            # (`core/roots.py`). Without this check the refusal surfaced later
            # as a "disk I/O error" from the first real statement, which every
            # hook swallows on its way to exit 0 — so cc-memory recorded
            # nothing for that project, forever, and said so only in a log.
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is not None and str(mode[0]).lower() != "wal":
                # Rollback journal works where WAL does not; degrade loudly
                # rather than fail every subsequent statement.
                conn.execute("PRAGMA journal_mode = DELETE")
                self._warn_journal_mode(mode[0])
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA temp_store = memory")
            conn.execute("PRAGMA mmap_size = 268435456")
            conn.execute("PRAGMA cache_size = 10000")
        except BaseException:
            # why: a PRAGMA that fails (locked file, damaged header, read-only
            # volume) must not leave the half-configured handle open — that is
            # exactly the leak this wrapper exists to close. The caller still
            # sees the original error, unchanged.
            conn.close()
            raise

        try:
            yield conn
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                # why: the connection is already broken; a rollback error
                # raised from here would MASK the caller's real exception
                pass
            raise
        else:
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                # why: a close() failure must never turn a with-block that
                # otherwise succeeded into a raising one (hook contract:
                # hooks never raise)
                pass

    def _bootstrap(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
        self._run_migrations()
        self._detect_fts5()
        self._ensure_active_hash_unique()
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

    _FTS_TRIGGERS = ("memories_fts_ai", "memories_fts_ad", "memories_fts_au")

    def _disable_fts5(self, conn, drop_triggers=False):
        """Declare FTS unavailable — and, for ONE condition, remove its triggers.

        Setting the flag alone is not a fallback, it is a write outage. The
        three triggers below name `memories_fts`, so if they were created by an
        earlier open on an FTS5-capable sqlite and this build has no such
        module, EVERY insert, update and delete on `memories` fails at prepare
        time with `no such module: fts5` — measured on all three verbs — while
        `_fts5_available = False` makes `/cc-mem status` report a benign
        search-only LIKE fallback. The whole anti-patch write path is dead and
        the status line says the index is merely slower.

        `DROP TRIGGER` is plain DDL and works with the module absent; `DROP
        TABLE` on the virtual table would not, which is why the declaration
        stays and only the triggers go. `_detect_fts5` notices the missing
        triggers on a later healthy open and resynchronises with a `rebuild`.

        `drop_triggers` defaults to FALSE, and the asymmetry is the whole
        point: the flag is per-INSTANCE while the DROP is per-DATABASE-FILE and
        permanent. Dropping on every DatabaseError turned one handle's local
        failure into a schema change under every other open handle — and
        `_rebuild_fts5` is reachable from a plain READ, so an ordinary
        `search_fts` could do it. Measured with two handles on one file: B
        disabled, A kept `_fts5_available = True`, A's next write bypassed the
        now-triggerless index, and A's `search_fts` for it ran a MATCH that
        SUCCEEDED and returned [] — `_match_fts` hands back a list, not None,
        so the documented LIKE fallback never fired and the MCP server reported
        the memory does not exist. Only `_setup_fts5`'s module-missing branch
        passes True, because that is the one state where LEAVING the triggers
        is itself the outage.
        """
        self._fts5_available = False
        if not drop_triggers:
            return
        for name in self._FTS_TRIGGERS:
            try:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            except sqlite3.DatabaseError:
                # why: best effort. A trigger we cannot drop leaves the outage
                # in place, but raising here would turn a degraded search into
                # a failed open.
                pass

    def _fts_triggers_present(self, conn) -> bool:
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
                "AND name IN (?, ?, ?)", self._FTS_TRIGGERS).fetchone()[0]
        except sqlite3.DatabaseError:
            return False
        return n == len(self._FTS_TRIGGERS)

    def _setup_fts5(self, conn):
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(test_col)")
            conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        except sqlite3.DatabaseError:
            # why: FTS5 not compiled into this sqlite build — fall back to LIKE
            # in search_fts, and drop the triggers that would otherwise make
            # every `memories` write fail. DatabaseError, not OperationalError:
            # the latter is a SUBCLASS of the former, so catching only it lets
            # a bare DatabaseError (SQLITE_CORRUPT_VTAB — what a damaged index
            # actually raises) escape into the caller.
            #
            # The ONE call that drops the triggers: this build has no fts5
            # module, so every trigger naming `memories_fts` makes every
            # `memories` write fail at prepare time. See _disable_fts5 for why
            # no other caller may.
            self._disable_fts5(conn, drop_triggers=True)
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
            self._fts5_available = True
        except sqlite3.DatabaseError:
            # why: FTS5 setup race, DDL conflict, or an already-damaged index —
            # degrade to LIKE search and take the triggers with it (see
            # _disable_fts5; a half-built index whose triggers survive is a
            # write outage, not a slower search).
            self._disable_fts5(conn)

    def _detect_fts5(self):
        """Observe whether the index exists — and REBUILD it if it does not.

        The `_migrations` ledger records INTENT, not state: `_run_migrations`
        writes the row unconditionally after the `try`, and `_setup_fts5`
        swallows its own `OperationalError` INTERNALLY and returns. So a
        database first opened on a sqlite build without FTS5 gets `v2_fts5`
        marked applied with no index behind it, and because the ledger is
        consulted before the work, the index is never created again — on any
        later run, on any later version, forever.

        That is not "slightly worse ranking". The `LIKE` fallback needs a
        CONTIGUOUS substring, so ordinary multi-word queries return NOTHING
        (measured on `The deploy key is rotated monthly by the release bot`:
        `deploy rotated` FTS 1 / LIKE 0, `key monthly` FTS 1 / LIKE 0) — and
        `mcp/server.py:_is_failed_result` counts an empty result set as a
        SUCCESS, so the model is told the project has no such memory rather
        than that search is broken.

        Repairing here rather than in the ledger follows the precedent the
        `topic` column already sets: a presence check that heals, independent
        of what the ledger claims. Re-running the seed is safe — the CREATEs
        are `IF NOT EXISTS` and the backfill is `INSERT OR IGNORE`.
        """
        try:
            with self._connect() as conn:
                # A REAL query, not `SELECT rowid … LIMIT 0`. The old probe
                # validated the virtual-table DECLARATION and never touched a
                # shadow table, so an index whose `memories_fts_data` is gone
                # answered it happily — measured: probe OK, and the very next
                # genuine MATCH raised `DatabaseError: database disk image is
                # malformed` (SQLITE_CORRUPT_VTAB). A health check that cannot
                # observe the unhealthy state is not a health check. The token
                # is deliberately one no memory contains, so the probe stays
                # O(1) whatever the index holds.
                conn.execute(
                    "SELECT rowid FROM memories_fts "
                    "WHERE memories_fts MATCH ? LIMIT 1",
                    ("ccmemoryftshealthprobe",)).fetchall()
                if self._fts_triggers_present(conn):
                    self._fts5_available = True
                    return
                # The index is healthy but the triggers are not there: a
                # previous open ran `_disable_fts5`, and every `memories` write
                # since then bypassed the index. Rebuild rather than re-run the
                # `INSERT OR IGNORE` seed — an external-content table does not
                # enforce rowid uniqueness, so re-seeding would DOUBLE every
                # existing entry instead of filling the gap.
                self._setup_fts5(conn)
                if self._fts5_available:
                    conn.execute(
                        "INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
                return
        except sqlite3.DatabaseError:
            # why: the table is missing or corrupted. Fall through to the
            # repair below rather than accepting a permanent LIKE fallback.
            # DatabaseError so SQLITE_CORRUPT_VTAB lands here too — under the
            # old OperationalError guard a corrupt index reported HEALTHY and
            # every search RAISED to its three callers instead of falling back.
            self._fts5_available = False
        try:
            with self._connect() as conn:
                self._setup_fts5(conn)
        except sqlite3.DatabaseError:
            # why: this sqlite genuinely has no FTS5, or the DDL raced. LIKE
            # search still answers; the next open probes again.
            self._fts5_available = False

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

    _ACTIVE_HASH_INDEX = "idx_memories_active_hash"

    def _ensure_active_hash_unique(self):
        """Definition-check + heal the active-row uniqueness backstop (v2.8.0).

        The transactional write path (reconcile_upsert) is the primary fix for
        concurrent duplicate stacking — two savers of the same sentence both
        decided INSERT and the anti-patch contract's "never stacks" was false
        under concurrency (register X1, measured: 2 active rows, same hash).
        This partial unique index is the ENGINE-level backstop that stops any
        future code path that bypasses the transaction from re-growing them.

        Deliberately NOT a _MIGRATIONS entry: the ledger records intent, not
        state (_detect_fts5's lesson) — a CREATE that failed once would be
        marked applied and never retried. Presence is one sqlite_master probe
        per open, and the heal is idempotent:
          1. duplicate active rows per (project_id, content_hash) are ARCHIVED
             — keep max (importance, id), link losers to the survivor so the
             supersede chain stays walkable and the motion is recoverable;
          2. CREATE UNIQUE INDEX ... WHERE is_active = 1.
        NULL hashes are distinct under SQLite UNIQUE, so an unbackfilled row
        cannot trip the constraint.

        The probe checks the index's DEFINITION, not its name — see
        `_active_hash_index_state`.
        """
        try:
            with self._connect() as conn:
                state = self._active_hash_index_state(conn)
                if state == "canonical":
                    return
                if state == "wrong":
                    # A same-NAME index with a DIFFERENT definition used to
                    # satisfy the presence probe, so the heal returned and the
                    # invariant was self-certified forever (measured: a
                    # non-unique index of the same name left 2 active rows on
                    # one hash, and a bypass insert made it 3 — an engine
                    # UNIQUE would have raised). The name is ours, so a
                    # non-canonical object under it is ours to replace.
                    self._db_warn(
                        f"active-hash index {self._ACTIVE_HASH_INDEX} exists "
                        f"with a NON-CANONICAL definition; dropping and "
                        f"rebuilding it")
                    conn.execute(f"DROP INDEX {self._ACTIVE_HASH_INDEX}")
                dupes = [dict(r) for r in conn.execute(
                    """SELECT project_id, content_hash FROM memories
                       WHERE is_active = 1 AND content_hash IS NOT NULL
                       GROUP BY project_id, content_hash
                       HAVING COUNT(*) > 1""").fetchall()]
            for d in dupes:
                with self._connect() as conn:
                    rows = [dict(r) for r in conn.execute(
                        """SELECT id, importance, content FROM memories
                           WHERE project_id = ? AND content_hash = ?
                             AND is_active = 1""",
                        (d["project_id"], d["content_hash"])).fetchall()]
                if len(rows) < 2:
                    continue
                survivor = max(rows, key=lambda r: (r["importance"], r["id"]))
                losers = [r["id"] for r in rows if r["id"] != survivor["id"]]
                # expected_contents (register r6-A3): the verdict above is a
                # snapshot, and this loop runs on EVERY open concurrent with
                # live hooks — a loser rewritten into a distinct fact between
                # the read and this write must fail its guard and stay.
                n = self.archive_obsolete(
                    losers, canonical_id=survivor["id"],
                    expected_contents={r["id"]: r["content"] for r in rows
                                       if r["id"] != survivor["id"]})
                self._db_warn(
                    f"active-hash heal: archived duplicate active row(s) "
                    f"{losers} -> kept #{survivor['id']} ({n} archived, "
                    f"recoverable; project {d['project_id']})")
            with self._connect() as conn:
                conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS "
                    f"{self._ACTIVE_HASH_INDEX} "
                    f"ON memories(project_id, content_hash) "
                    f"WHERE is_active = 1")
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            # why: a concurrent writer racing the heal, or a read-only volume,
            # can fail the CREATE — and a duplicate inserted BETWEEN the
            # dedupe pass and the CREATE surfaces as IntegrityError, not
            # OperationalError (register r6-A2: the narrower except let it
            # escape MemoryDB.__init__). Either way the next open probes
            # sqlite_master again — the same healing contract _detect_fts5
            # uses. The dedupe half is idempotent, so a partial heal never
            # needs undoing.
            pass

    _ACTIVE_HASH_COLS = ("project_id", "content_hash")

    @classmethod
    def _active_hash_index_state(cls, conn):
        """"absent" | "wrong" | "canonical" for the active-hash index.

        A NAME probe is not an invariant probe. `CREATE UNIQUE INDEX IF NOT
        EXISTS` is a no-op against a same-name object of ANY shape, so an
        index that is merely named `idx_memories_active_hash` — non-unique,
        wrong columns, or no partial predicate — used to make this method
        return immediately and certify a constraint the engine was not
        enforcing. Measured: two active rows on one content_hash survived the
        open, and a bypass insert made it three.

        Checked via pragma (authoritative for uniqueness, partiality and
        column set) plus one substring test on the stored SQL, because
        `PRAGMA index_list` reports only THAT a partial predicate exists, not
        what it says.
        """
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (cls._ACTIVE_HASH_INDEX,)).fetchone()
        if row is None:
            return "absent"
        entry = [r for r in conn.execute("PRAGMA index_list(memories)")
                 if r[1] == cls._ACTIVE_HASH_INDEX]
        if not entry:
            return "wrong"
        _seq, _name, is_unique, _origin, is_partial = entry[0][:5]
        if not is_unique or not is_partial:
            return "wrong"
        cols = tuple(r[2] for r in conn.execute(
            f"PRAGMA index_info({cls._ACTIVE_HASH_INDEX})"))
        if cols != cls._ACTIVE_HASH_COLS:
            return "wrong"
        sql = (row["sql"] if not isinstance(row, tuple) else row[0]) or ""
        normalized = " ".join(sql.lower().split())
        if "where is_active = 1" not in normalized:
            return "wrong"
        return "canonical"

    def _rebuild_fts5(self):
        if not self._fts5_available:
            return
        try:
            with self._connect() as conn:
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        except sqlite3.DatabaseError:
            # why: rebuild failed (corrupted index) — mark unavailable so search
            # falls back to LIKE instead of repeatedly hitting the broken index.
            # DatabaseError, not OperationalError: rebuilding a genuinely
            # damaged index is precisely the case that raises the PARENT class,
            # so the narrower guard let the very failure this handler exists
            # for escape into the caller.
            with self._connect() as conn:
                self._disable_fts5(conn)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ── projects ─────────────────────────────────────────────────────────────

    def upsert_project(self, cwd: str) -> int:
        path = str(Path(cwd).resolve())
        name = Path(path).name
        now = self._now()
        with self._connect() as conn:
            # ONE statement, not check-then-insert. `projects.path` is UNIQUE
            # and this was a read followed by a write with no lock between:
            # two first-touchers of the same project both saw no row and both
            # inserted, and the loser got `IntegrityError: UNIQUE constraint
            # failed: projects.path` raised out of a method whose contract is
            # UPSERT. Reproduced against this exact method with two real
            # connections. `busy_timeout` cannot help — the SELECT takes no
            # write lock, and by the time the loser inserts the winner has
            # committed, so this is a constraint violation, not contention.
            #
            # This is the identical shape round 6 fixed for `plan_active` (see
            # `_insert_plan_row`'s ON CONFLICT DO NOTHING); the fix was applied
            # there and nowhere else, leaving it in the FIRST database call
            # every hook, the MCP server, the CLI and the dashboard make.
            #
            # DO UPDATE, not DO NOTHING, because the `last_active` bump is the
            # point of the call when the row already exists. `name` is
            # deliberately NOT overwritten: the stored name is whatever the
            # project was first registered as, exactly as before.
            conn.execute(
                "INSERT INTO projects (path, name, created_at, last_active) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET last_active = excluded.last_active",
                (path, name, now, now)
            )
            return conn.execute(
                "SELECT id FROM projects WHERE path = ?", (path,)
            ).fetchone()["id"]

    # ── sessions ─────────────────────────────────────────────────────────────

    def insert_session(self, project_id, claude_session_id, trigger_type,
                       msg_count, archive_path, brief_summary):
        """Insert a session row with complete=0 — a CLAIM, not a receipt.

        The row used to be readable as "this transcript was saved" the moment
        it committed, while the memories / summary / progress writes were
        still minutes of LLM calls away — a kill in that window made
        `_get_saved_session_ids` skip the transcript forever (register X6).
        Callers flip the flag with `mark_session_complete` AFTER every
        dependent write has landed; readers that mean "saved" filter on
        complete=1. Legacy rows carry the migration default 1.
        """
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO sessions
                   (project_id, claude_session_id, trigger_type, compacted_at,
                    msg_count, archive_path, brief_summary, complete)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (project_id, claude_session_id, trigger_type, self._now(),
                 msg_count, archive_path, brief_summary)
            )
            return cur.lastrowid

    def mark_session_complete(self, session_id):
        """The receipt half of insert_session's claim. Returns rowcount."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE sessions SET complete = 1 WHERE id = ?",
                (session_id,)).rowcount

    # ── Ordering: `id`, never a timestamp string ────────────────────────────
    # `_now()` is `datetime.now()` — NAIVE LOCAL time — and every "most
    # recent" query below used to sort on it as a string. Local wall time is
    # not monotonic: it repeats an hour at every DST fall-back and steps back
    # on any NTP correction, so the newest row sorted LAST. Measured with the
    # clock stepped back one hour between two inserts:
    #   get_recent_session_ids(1)            -> [1]   (the OLDER session)
    #   get_recent_memories(sessions_back=1) -> 0 rows, with one active memory
    #   get_stats()['last_session']          -> the older timestamp
    # PROGRESS.md §0 then attributes the handoff to the wrong session. The
    # `id` column is INTEGER PRIMARY KEY AUTOINCREMENT — monotonic by
    # construction and already the documented tiebreaker for the
    # second-resolution collisions elsewhere in this file. The timestamps stay
    # as DISPLAY values; they are no longer asked to order anything.
    def get_recent_session_ids(self, project_id, n=3):
        """Ids of the last N sessions worth a recency slot.

        r6-A6 introduced `complete = 1` so an unreceipted CLAIM row — a killed
        compaction, a mid-flight one — could not consume a slot and push every
        truly-saved session out of the window. That intent is right; the proxy
        was too coarse. `complete` answers "did the receipt land", and the
        question this reader is actually asking is "does this session have
        anything worth showing".

        The gap is not hypothetical and it is not only about host kills.
        `hooks/pre_compact.py` commits the extracted memories against the
        session id and withholds the receipt when `insert_session_summary`
        raises — deliberately, so the transcript is retried. Those memories are
        already durable, already active, and were pinned to a row this filter
        then hid forever: measured 1 active memory, `get_recent_session_ids()`
        `[]`, `get_recent_memories()` 0 rows. Nothing repairs it, because no
        code path anywhere in the package rewrites `memories.session_id` (grep
        for `SET session_id`: zero hits), and the retried extraction answers
        the re-derived fact with a hash SKIP or an in-place MERGE — neither of
        which re-attributes the row.

        So: receipted, OR carrying at least one active memory. An empty killed
        claim still consumes nothing, which is the whole of what r6-A6 bought.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM sessions s WHERE s.project_id = ? "
                "AND (s.complete = 1 OR EXISTS (SELECT 1 FROM memories m "
                "     WHERE m.session_id = s.id AND m.is_active = 1)) "
                "ORDER BY s.id DESC LIMIT ?",
                (project_id, n)
            ).fetchall()
            return [r["id"] for r in rows]

    def get_session_count(self, project_id):
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id = ?",
                (project_id,)
            ).fetchone()[0]

    def count_memories_since(self, project_id, row_id=0, since_ts=""):
        """Memories written after a consolidation watermark (v2.12.0).

        Prefers the ROW-ID watermark (monotonic, clock-immune — the same
        reasoning that moved the observer watermark off ISO timestamps);
        falls back to `created_at > since_ts` for a marker written before
        `last_memory_id` existed, and to the full count when there is no
        watermark at all — a project that has never consolidated IS one big
        backlog, which is the correct reading.
        """
        with self._connect() as conn:
            if row_id:
                sql = ("SELECT COUNT(*) FROM memories "
                       "WHERE project_id = ? AND id > ?")
                params = (project_id, row_id)
            elif since_ts:
                sql = ("SELECT COUNT(*) FROM memories "
                       "WHERE project_id = ? AND created_at > ?")
                params = (project_id, since_ts)
            else:
                sql = "SELECT COUNT(*) FROM memories WHERE project_id = ?"
                params = (project_id,)
            return int(conn.execute(sql, params).fetchone()[0])

    def max_memory_id(self, project_id):
        """Highest memories.id this project holds (0 when empty) — the
        watermark `count_memories_since` reads back."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(id) FROM memories WHERE project_id = ?",
                (project_id,)).fetchone()
            return int(row[0] or 0)

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

        ONE transaction, deliberately. This used to be insert_memory() +
        archive_memory(), each opening and COMMITTING its own connection — a
        process killed between the two left BOTH rows active (measured), i.e.
        the new fact and the fact it replaces contradicting each other in
        every render until a consolidation happened to notice. `_connect`
        commits on clean exit and rolls back on any exception, so the pair is
        now atomic: both land or neither does.

        Archive-then-insert, in THAT order (v2.8.0): compute_content_hash
        folds case and surrounding whitespace, so old and new content that
        differ only there share a hash — and the active-row unique index
        (_ensure_active_hash_unique) would refuse the insert while the old
        row is still active. Archiving first keeps every interleaving legal;
        the transaction still makes the pair all-or-nothing.
        """
        now = self._now()
        content_hash = self.compute_content_hash(new_content)
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET is_active = 0, updated_at = ? WHERE id = ?",
                (now, old_id)
            )
            cur = conn.execute(
                """INSERT INTO memories
                   (project_id, session_id, category, content, importance,
                    tags, topic, content_hash, supersedes_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, session_id, category, new_content, importance,
                 json.dumps(tags or [], ensure_ascii=False), topic,
                 content_hash, old_id, now, now)
            )
            return cur.lastrowid

    def reconcile_upsert(self, project_id, session_id, category, content,
                         importance=3, tags=None, topic=None, *,
                         high_sim, mid_sim, max_candidates,
                         pick, merge_fields, supersede_fields):
        """The anti-patch decision tree — read, decide AND write in ONE
        `BEGIN IMMEDIATE` transaction.

        MECHANISM only. Thresholds, the similarity function and the tag-union
        policy belong to `llm.memory_writer.upsert_smart`, the single policy
        owner, and arrive as parameters:
          pick(candidates)        -> (row|None, sim)  best similar active row
          merge_fields(row)       -> {importance, topic, tags}  MERGE branch
          supersede_fields(row)   -> {importance, topic, tags}  SUPERSEDE
        None of the callables may touch the database — they are pure functions
        of their arguments, called while this transaction holds the write
        lock.

        WHY a transaction: the writer used to run find_by_hash, the similarity
        scan and the branch write across 3+ self-committing connections, so
        two concurrent savers of the same sentence both observed an empty
        table and both INSERTed — the contract's "never stacks" was false
        under concurrency (register X1, measured: actions=['inserted',
        'inserted'], 2 active rows with one hash). BEGIN IMMEDIATE takes the
        write lock BEFORE the first read, so the decision and the write are
        one atom; cross-process waiters are bounded by busy_timeout.

        The SUPERSEDE branch archives the old row BEFORE inserting the new
        one — same reasoning as supersede_memory: the case-folding hash plus
        the active-row unique index make insert-first illegal in one corner.

        sqlite3.IntegrityError (the unique-index backstop catching a path
        that raced around us — reachable only for non-transactional callers,
        kept for defence) degrades to a hash_match SKIP against the row that
        won.
        """
        now = self._now()
        h = self.compute_content_hash(content)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT id FROM memories WHERE project_id = ? "
                    "AND content_hash = ? AND is_active = 1 LIMIT 1",
                    (project_id, h)).fetchone()
                if row:
                    return {"action": "skipped", "id": row["id"],
                            "similarity": 1.0, "reason": "hash_match"}
                candidates = []
                if topic:
                    # LIMIT, like the category branch below. This was the one
                    # candidate query in the file with none, so `max_candidates`
                    # bounded every path except the one a busy project actually
                    # takes, and the whole fetch happened inside BEGIN IMMEDIATE
                    # — i.e. holding the write lock. The picker slices to 500
                    # anyway, so the rows past the limit were read, decoded and
                    # discarded; measured 5 000 rows fetched for a 500-row
                    # decision. (This is a cost bound, not the DoS the audit
                    # claimed: 5 000 x 4 KiB measured 0.204 s, and crossing the
                    # Stop hook's 22 s budget needed 250 x 1 MiB memories.)
                    candidates = [dict(r) for r in conn.execute(
                        """SELECT * FROM memories
                           WHERE project_id = ? AND is_active = 1 AND topic = ?
                           ORDER BY importance DESC, created_at DESC
                           LIMIT ?""",
                        (project_id, topic, max_candidates)).fetchall()]
                if not candidates:
                    candidates = [dict(r) for r in conn.execute(
                        """SELECT * FROM memories
                           WHERE project_id = ? AND is_active = 1
                             AND category = ?
                           ORDER BY updated_at DESC LIMIT ?""",
                        (project_id, category, max_candidates)).fetchall()]
                similar, sim = pick(candidates) if candidates else (None, 0.0)
                if similar is not None and sim >= high_sim:
                    f = merge_fields(similar)
                    conn.execute(
                        """UPDATE memories SET content = ?, content_hash = ?,
                           importance = ?, topic = ?, tags = ?, updated_at = ?
                           WHERE id = ?""",
                        (content, h, f["importance"], f["topic"],
                         json.dumps(f["tags"], ensure_ascii=False), now,
                         similar["id"]))
                    return {"action": "merged", "id": similar["id"],
                            "similarity": sim, "old_id": similar["id"]}
                if similar is not None and sim >= mid_sim:
                    f = supersede_fields(similar)
                    conn.execute(
                        "UPDATE memories SET is_active = 0, updated_at = ? "
                        "WHERE id = ?", (now, similar["id"]))
                    cur = conn.execute(
                        """INSERT INTO memories
                           (project_id, session_id, category, content,
                            importance, tags, topic, content_hash,
                            supersedes_id, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (project_id, session_id, category, content,
                         f["importance"],
                         json.dumps(f["tags"], ensure_ascii=False),
                         f["topic"], h, similar["id"], now, now))
                    return {"action": "superseded", "id": cur.lastrowid,
                            "similarity": sim, "old_id": similar["id"]}
                cur = conn.execute(
                    """INSERT INTO memories
                       (project_id, session_id, category, content, importance,
                        tags, topic, content_hash, supersedes_id,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                    (project_id, session_id, category, content, importance,
                     json.dumps(tags or [], ensure_ascii=False), topic, h,
                     now, now))
                return {"action": "inserted", "id": cur.lastrowid,
                        "similarity": sim if similar is not None else 0.0,
                        "old_id": None}
        except sqlite3.IntegrityError:
            winner = self.find_by_hash(project_id, h)
            if winner is None:
                # why (register r6-A13): only the active-hash unique index can
                # make this a benign duplicate race, and its winner is by
                # definition findable by hash. No winner means the violation
                # was something else — a foreign key, a CHECK — and swallowing
                # it would report a successful-looking skip for a write that
                # was silently LOST. Re-raise the caller's real error.
                raise
            self._db_warn(
                f"reconcile_upsert: unique-index backstop caught a concurrent "
                f"identical save (project {project_id}); treating as "
                f"hash_match skip against #{winner['id']}")
            return {"action": "skipped", "id": winner["id"],
                    "similarity": 1.0, "reason": "hash_match"}

    _journal_warned = False

    @staticmethod
    def _db_warn(msg):
        """File-only warning; never raises (hook paths call this).

        The ONE lazy-logger sink for this module. It existed as three
        near-identical inline blocks (_warn_journal_mode, _warn_cycle, and the
        heal path) — the same copy-drift disease `is_excluded`'s two copies
        had, just smaller.
        """
        try:
            from core.logger import get_logger
            get_logger("db").warn(msg)
        except Exception:
            # why: diagnostics must never break the database operation that
            # just happened; an unavailable logger costs the message only.
            pass

    def _warn_journal_mode(self, got):
        """Log a WAL refusal ONCE per process.

        Once, because `_connect` runs on every database operation and a hook
        that logged this per call would turn a diagnostic into the noise that
        hides it. The class attribute is deliberate: the condition is a
        property of the filesystem, not of one handle.
        """
        if MemoryDB._journal_warned:
            return
        MemoryDB._journal_warned = True
        self._db_warn(
            f"{self.db_path} would not take WAL (got {got!r}); using a "
            f"rollback journal instead. WAL does not work on network "
            f"filesystems — is this project on a mapped share?")

    def get_memory(self, memory_id):
        """One row by id, active or archived, or None.

        Deliberately unfiltered on `is_active`: callers that need to know
        whether a row is still live read the column. `core/consolidate.py`'s
        dedup uses this to re-check its chosen survivor AFTER the LLM judge
        call, because the candidate set was read before a network round-trip
        that the Stop hook's idle reorg can mutate underneath.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return dict(row) if row else None

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
        """Memories from the last N sessions PLUS every session-less memory.

        `session_id IS NULL` is not an edge case — it is what ALL FOUR manual
        save paths produce (cli/mem.py add, mcp/server.py memory_add,
        ui/dashboard.py, ui/web_viewer.py POST /api/memory, plus the
        save-memories skill), because a `sessions` row only exists after a
        compaction. The pre-v2.5 filter was a bare `AND session_id IN (...)`,
        which NULL can never satisfy, so everything the user saved by hand was
        invisible to every consumer of this method: SessionStart injection
        (hooks/session_start.py), the web viewer, and MCP memory_recent.
        """
        session_ids = self.get_recent_session_ids(project_id, sessions_back)
        params = [project_id, min_importance]
        if session_ids:
            ph = ",".join("?" * len(session_ids))
            session_clause = f"AND (session_id IN ({ph}) OR session_id IS NULL)"
            params += session_ids
        else:
            # why: a project that has never compacted still has manual saves,
            # and "IN ()" is a syntax error in SQLite — drop the IN arm
            session_clause = "AND session_id IS NULL"
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
                      {session_clause}
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
                   ORDER BY importance DESC, updated_at DESC, id DESC""",
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

    # SQLite refuses a statement with more bound variables than its compiled
    # SQLITE_MAX_VARIABLE_NUMBER — "too many SQL variables", measured here at
    # 32767 on this interpreter, and the cap is 999 on builds before 3.32.
    # Every `id IN (?,?,...)` writer below therefore feeds ids through
    # `_id_chunks`; 900 leaves room for the non-id parameters under even the
    # oldest cap. Each caller still wraps ALL its chunks in ONE _connect(), so
    # the operation stays a single transaction.
    _SQL_VAR_CHUNK = 900

    @classmethod
    def _id_chunks(cls, ids):
        for i in range(0, len(ids), cls._SQL_VAR_CHUNK):
            yield ids[i:i + cls._SQL_VAR_CHUNK]

    def bulk_set_topic(self, memory_ids, topic):
        if not memory_ids:
            return
        now = self._now()
        with self._connect() as conn:
            for chunk in self._id_chunks(list(memory_ids)):
                ph = ",".join("?" * len(chunk))
                conn.execute(
                    f"UPDATE memories SET topic = ?, updated_at = ? "
                    f"WHERE id IN ({ph})",
                    [topic, now] + chunk
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
            for chunk in self._id_chunks(list(memory_ids)):
                ph = ",".join("?" * len(chunk))
                conn.execute(
                    f"UPDATE memories SET is_active = 0, updated_at = ? "
                    f"WHERE id IN ({ph})",
                    [now] + chunk
                )

    def archive_if_unchanged(self, id_content_pairs):
        """Archive each id ONLY while its content is what the verdict saw.

        For verdicts computed from a SNAPSHOT (cleanup_garbage and the other
        pairwise consolidation stages): the reader, the decision loop and the
        write are separate transactions, and the Stop hook's idle reorg runs
        them CONCURRENT with the PreCompact writer. A row repaired in that
        window — its garbage content merged over with good content — used to
        be archived anyway (measured). Guarding on what the verdict was
        computed FROM makes a stale verdict a no-op instead of data loss.

        `id_content_pairs` is `(id, content)` — the EXACT text the caller
        judged, not its `content_hash`. The first version of this guard used
        the hash, and `compute_content_hash` digests
        `content.strip().lower()`: it is a DEDUP identity, deliberately blind
        to case and to surrounding whitespace. A concurrent rewrite that
        changed only those therefore passed the guard and was archived anyway
        — measured, `'Deploy Key Is ROTATED Monthly'` rewritten to
        `'deploy key is rotated monthly'` kept hash `0ee710fb3f267a24` and
        went `is_active=0`. Asking a dedup identity to double as a VERSION
        identity is the mistake; comparing the text itself cannot have a
        blind spot. Returns how many rows were actually archived.
        """
        if not id_content_pairs:
            return 0
        now = self._now()
        n = 0
        with self._connect() as conn:
            for mid, content in id_content_pairs:
                cur = conn.execute(
                    "UPDATE memories SET is_active = 0, updated_at = ? "
                    "WHERE id = ? AND is_active = 1 AND content = ?",
                    (now, mid, content)
                )
                n += cur.rowcount
        return n

    def update_if_unchanged(self, memory_id, expected_content, *,
                            content=None, importance=None, topic=None,
                            tags=None, category=None, _conn=None):
        """update_memory guarded on the EXACT content the caller's verdict
        saw. Returns rowcount: 0 means the row changed (or was archived)
        between the read and this write, and the stale verdict became a no-op
        instead of an overwrite.

        The write-side twin of archive_if_unchanged, for the one consolidation
        stage that REWRITES a row from a snapshot: semantic_dedup computes a
        canonical merge during a network round-trip, and a concurrent
        PreCompact correction landing in that window used to be clobbered by
        the stale canonical (register X2 — survivor content ended as the
        verdict text, the correction gone). Text comparison, not
        content_hash: the hash is a case-folding DEDUP identity and must not
        double as a VERSION identity (see archive_if_unchanged).
        """
        now = self._now()
        fields, params = [], []
        if content is not None:
            fields += ["content = ?", "content_hash = ?"]
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
            return 0
        fields.append("updated_at = ?")
        params += [now, memory_id, expected_content]
        ctx = (contextlib.nullcontext(_conn) if _conn is not None
               else self._connect())
        with ctx as conn:
            cur = conn.execute(
                f"UPDATE memories SET {', '.join(fields)} "
                f"WHERE id = ? AND is_active = 1 AND content = ?",
                params
            )
            return cur.rowcount

    def apply_dedup_verdict(self, survivor_id, expected_survivor_content,
                            canonical, tags, losers, expected_loser_contents):
        """One BEGIN IMMEDIATE for a dedup group's whole write phase.

        semantic_dedup used to run three transactions — freshness re-read,
        loser archive, survivor rewrite — so a survivor write failing AFTER
        the archive left the group HALF-APPLIED: losers inactive against a
        survivor the verdict no longer described (recorded as a limit in the
        r6 triage; closed here). Under the write lock:
          1. the survivor must still be active with the exact content the
             judge saw, or the whole group is a no-op and every loser stays
             active;
          2. losers archive with the same per-row content guards as before
             (archive_obsolete composed onto THIS connection, so the chain
             link and the cycle guard are the single implementation);
          3. the survivor rewrite runs under a SAVEPOINT — a canonical that
             hash-collides with an active row OUTSIDE the group rolls back
             only the rewrite (the survivor keeps its wording; the losers
             stay archived, because they are duplicates of the surviving
             ROW, not of its wording); any other failure propagates and
             rolls the whole group back.
        Returns {"archived": n, "wrote": 0|1, "skipped": str|None}.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT content FROM memories WHERE id = ? AND is_active = 1",
                (survivor_id,)).fetchone()
            if row is None or row["content"] != expected_survivor_content:
                return {"archived": 0, "wrote": 0,
                        "skipped": "survivor_changed"}
            n = self.archive_obsolete(
                losers, canonical_id=survivor_id,
                expected_contents=expected_loser_contents, _conn=conn)
            conn.execute("SAVEPOINT dedup_rewrite")
            try:
                wrote = self.update_if_unchanged(
                    survivor_id, expected_survivor_content,
                    content=canonical, tags=tags, _conn=conn)
                conn.execute("RELEASE dedup_rewrite")
            except sqlite3.IntegrityError:
                # why: the canonical can hash-collide (case-fold) with an
                # active row OUTSIDE the group; losing only the merged
                # WORDING is the graceful degradation, and raising would
                # abort the whole consolidation run from a hook
                conn.execute("ROLLBACK TO dedup_rewrite")
                conn.execute("RELEASE dedup_rewrite")
                # wrote=0, not 1. The rewrite was ROLLED BACK — the survivor
                # keeps its own wording — so reporting 1 states the opposite
                # of what happened. (The retired consolidate.py code set a
                # local `wrote = 1` here purely to suppress its own warn line;
                # this method reports through `skipped` instead, which frees
                # the field to be honest. Caught by codex round-7 review.)
                return {"archived": n, "wrote": 0,
                        "skipped": "canonical_collision"}
            return {"archived": n, "wrote": wrote, "skipped": None}

    def merge_topic_variant(self, project_id, memory_ids, canonical,
                            drop_summary):
        """Relabel a variant's memories to the canonical topic AND drop the
        variant's topics-table summary row in ONE transaction.

        canonicalize_topics used to commit the relabel and the summary
        delete separately (recorded as a limit in the r6 triage: a kill
        between them stranded a summary for a label no memory carries, and
        the next consolidate_topics regenerates same-name summaries only,
        never removes vanished ones). `drop_summary` is the variant label
        whose summary row dies with the relabel, or None to only relabel.
        """
        now = self._now()
        with self._connect() as conn:
            for chunk in self._id_chunks(list(memory_ids or [])):
                ph = ",".join("?" * len(chunk))
                conn.execute(
                    f"UPDATE memories SET topic = ?, updated_at = ? "
                    f"WHERE id IN ({ph})",
                    [canonical, now] + chunk)
            if drop_summary is not None:
                conn.execute(
                    "DELETE FROM topics WHERE project_id = ? AND name = ?",
                    (project_id, drop_summary))

    _CLAIM_GC_DAYS = 7

    def gc_stale_claims(self, project_id, older_than_days=_CLAIM_GC_DAYS):
        """DELETE unreceipted session claims old enough that their writer is
        certainly dead — and only when NOTHING references them.

        complete=0 rows from killed compactions accumulated forever (the r6
        triage recorded it as a limit: harmless to reads once A6 filtered
        them, but unbounded). A claim is garbage only if it is old, still
        unreceipted, AND left no DATABASE lineage: a kill can land after
        memories or a summary were attached, and those claims keep their row
        so the lineage stays traceable. Returns rows deleted.

        `archive_path` is NOT such a trace, and treating it as one made this
        collector a no-op against its own dominant input. `hooks/pre_compact.py`
        stamps a non-empty `archive_path` into the row at INSERT time — before
        the long LLM leg that is the actual kill window — so EVERY killed
        compaction arrives here already carrying one. Measured: a claim in the
        exact shape the caller produces, aged past the cutoff, collected 0.
        The only test of the mechanism built its deletable fixture with
        `archive_path = ""`, a shape no caller ever writes, so the gate was
        green against a case that cannot occur.

        The archive FILE is untouched and remains on disk: it is the durable,
        human-readable trace, and the row that pointed at it holds nothing the
        filename does not. Deleting the pointer while keeping the document is
        the trade this makes, and it is why `sessions/` is swept by nothing.
        """
        cutoff = (datetime.now()
                  - timedelta(days=older_than_days)).isoformat(sep="T")
        with self._connect() as conn:
            cur = conn.execute(
                """DELETE FROM sessions
                   WHERE project_id = ? AND complete = 0
                     AND compacted_at < ?
                     AND id NOT IN (SELECT session_id FROM memories
                                    WHERE session_id IS NOT NULL)
                     AND id NOT IN (SELECT session_id FROM session_summaries
                                    WHERE session_id IS NOT NULL)""",
                (project_id, cutoff))
            return cur.rowcount

    def delete_memories(self, memory_ids):
        if not memory_ids:
            return
        with self._connect() as conn:
            for chunk in self._id_chunks(list(memory_ids)):
                ph = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM memories WHERE id IN ({ph})", chunk)

    # ── reference-aware aging (v6) ──────────────────────────────────────────

    def bump_last_referenced(self, memory_ids):
        """Mark memories as referenced NOW (called from SessionStart when a
        memory is injected into context). Keeps referenced facts 'young' for
        the staleness net. No-op on empty list. Does NOT touch updated_at."""
        ids = [i for i in (memory_ids or []) if i is not None]
        if not ids:
            return
        now = self._now()
        with self._connect() as conn:
            for chunk in self._id_chunks(ids):
                ph = ",".join("?" * len(chunk))
                conn.execute(
                    f"UPDATE memories SET last_referenced_at = ? "
                    f"WHERE id IN ({ph})",
                    [now] + chunk
                )

    def archive_obsolete(self, stale_ids, canonical_id=None, *,
                         require_never_referenced=False,
                         expected_contents=None, max_importance=None,
                         _conn=None):
        """Archive memories as obsolete (is_active=0), optionally linking each
        to the canonical memory that replaced it via supersedes_id (forward
        history pointer). Used by the staleness/dedup consolidation stages.

        Distinct from supersede_memory: NO new row is inserted (the canonical
        already exists), so this never duplicates content. Distinct from
        bulk_archive: it sets supersedes_id so get_supersede_chain can still
        trace the lineage.

        Snapshot-verdict guards (v2.8.0) — every caller's verdict is computed
        from a read that happened BEFORE this write, so the write re-asserts
        the predicate the verdict rests on:
          require_never_referenced=True adds `last_referenced_at IS NULL` —
            the staleness net's own criterion. Without it, a row SessionStart
            injected (and bumped) between the snapshot and this write was
            archived anyway: live context pointing at an inactive row
            (register X3, measured).
          expected_contents={id: content} adds `content = ?` per row — the
            dedup judges' criterion. A row rewritten during the LLM
            round-trip is no longer the row the verdict judged.
          max_importance=N adds `importance <= ?` — the staleness net also
            selects on LOW importance, and an importance-only bump with
            content unchanged slipped both other guards (the r6 triage
            recorded it as a limit; closed by re-asserting the predicate).
        `_conn` composes this write into a caller-held transaction
        (apply_dedup_verdict); the caller owns commit/rollback then.
        Returns the number of rows ACTUALLY archived (rowcount), not the
        number asked for — `len(ids)` was a lie whenever a guard, a missing
        row or an already-archived row made an UPDATE a no-op.
        """
        ids = [i for i in (stale_ids or []) if i is not None and i != canonical_id]
        if not ids:
            return 0
        now = self._now()
        guard = " AND is_active = 1"
        guard_params = []
        if require_never_referenced:
            guard += " AND last_referenced_at IS NULL"
        if max_importance is not None:
            guard += " AND importance <= ?"
            guard_params.append(max_importance)
        archived = 0
        ctx = (contextlib.nullcontext(_conn) if _conn is not None
               else self._connect())
        with ctx as conn:

            def _archive(id_list, link_to):
                """UPDATE each id under the guards; returns rows touched.

                COALESCE, never overwrite: a loser produced by an earlier
                SUPERSEDE already carries a link to the row IT replaced, and
                an unconditional `supersedes_id = ?` destroyed that link —
                the older version became unreachable from every chain walk
                (measured: chain [2,1] became [2,3], #1 orphaned). The slot
                records the FIRST lineage fact it learns; when it is already
                occupied, the replaced-by fact is logged instead (see the
                prior-link warn in the caller below).
                """
                n = 0
                set_sql = "is_active = 0, updated_at = ?"
                set_params = [now]
                if link_to is not None:
                    set_sql = ("is_active = 0, "
                               "supersedes_id = COALESCE(supersedes_id, ?), "
                               "updated_at = ?")
                    set_params = [link_to, now]
                if expected_contents is None:
                    for chunk in self._id_chunks(id_list):
                        ph = ",".join("?" * len(chunk))
                        cur = conn.execute(
                            f"UPDATE memories SET {set_sql} "
                            f"WHERE id IN ({ph}){guard}",
                            set_params + chunk + guard_params)
                        n += cur.rowcount
                else:
                    for mid in id_list:
                        cur = conn.execute(
                            f"UPDATE memories SET {set_sql} "
                            f"WHERE id = ?{guard} AND content = ?",
                            set_params + [mid] + guard_params
                            + [expected_contents.get(mid, "")])
                        n += cur.rowcount
                return n

            linked = ids
            if canonical_id is not None:
                # Cycle guard. `supersedes_id` links must stay a DAG: a row
                # already ON the canonical's backward chain that is then
                # pointed AT the canonical closes a loop (A->B->A) — and that
                # state is reachable, because a supersede killed between its
                # two halves used to leave both rows active, after which a
                # dedup pass could legitimately pick the OLD row's successor
                # as canonical for it (constructed and measured). The chain
                # walker survives on its seen-guard, but the lineage it
                # returns is garbage. Rows that would close a loop are
                # archived WITHOUT the link, and logged.
                chain_ids, cur_id = set(), canonical_id
                while cur_id is not None and cur_id not in chain_ids:
                    chain_ids.add(cur_id)
                    row = conn.execute(
                        "SELECT supersedes_id FROM memories WHERE id = ?",
                        (cur_id,)).fetchone()
                    cur_id = row["supersedes_id"] if row else None
                looped = [i for i in ids if i in chain_ids]
                linked = [i for i in ids if i not in chain_ids]
                if looped:
                    self._warn_cycle(looped, canonical_id)
                    archived += _archive(looped, None)
                # An occupied slot keeps its original link (COALESCE above);
                # the "replaced by canonical" fact is then recorded nowhere,
                # so say so in the log rather than silently dropping it.
                prior = []
                for chunk in self._id_chunks(linked):
                    ph = ",".join("?" * len(chunk))
                    prior += [r["id"] for r in conn.execute(
                        f"SELECT id FROM memories WHERE id IN ({ph}) "
                        f"AND supersedes_id IS NOT NULL", chunk).fetchall()]
                if prior:
                    self._db_warn(
                        f"archive_obsolete: {prior} already carry a "
                        f"supersedes link; kept it. Their replacement by "
                        f"#{canonical_id} is recorded only here.")
                archived += _archive(linked, canonical_id)
            else:
                archived += _archive(ids, None)
            return archived

    def _warn_cycle(self, looped, canonical_id):
        """Log a refused supersede link (cycle guard). See _db_warn."""
        self._db_warn(
            f"archive_obsolete: refused supersedes link(s) {looped} -> "
            f"#{canonical_id}: each is already on the canonical's own "
            f"chain, and linking would close a cycle. Archived unlinked.")

    def get_referenced_id_set(self, project_id):
        """Return the set of memory ids that have EVER been injected
        (last_referenced_at IS NOT NULL). Used by the staleness net to spare
        any fact that was surfaced to a session."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM memories WHERE project_id = ? "
                "AND last_referenced_at IS NOT NULL",
                (project_id,)
            ).fetchall()
            return {r["id"] for r in rows}

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

    # (`is_duplicate_hash` was deleted here. It answered "does this hash
    # exist?" on its OWN connection, which is the pre-transaction shape the
    # anti-patch contract removed: the hash check now happens inside
    # `reconcile_upsert`'s single `BEGIN IMMEDIATE`, because a check on one
    # connection and an insert on another is precisely how two concurrent
    # savers of the same sentence both observed an empty table and both
    # inserted. It had zero callers repo-wide — a dead helper whose signature
    # still advertised the unsafe pattern is an invitation to re-adopt it.)

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
                   ORDER BY id DESC LIMIT ?""",
                (project_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_observations_since(self, project_id, since_id, limit=None):
        """Observations written AFTER `since_id`, oldest first.

        `limit` bounds the OLDEST end, which is the only useful end for a
        catch-up reader: it lets one caller replace a two-branch
        "resume-from-watermark OR scan-the-recent-N" pair with a single query
        that is always oldest-first. `hooks/stop.py` needed exactly that —
        its fallback branch ordered by id DESC, so on its first Stop of a
        session it fed the model the NEWEST rows and then watermarked past the
        older ones it had never shown anybody.

        The watermark is the monotonic row id, not a timestamp. It was
        `timestamp > ?` against `_now()` — naive LOCAL time — so a clock that
        stepped BACK (DST fall-back, NTP correction) made every observation
        written afterwards compare as OLDER than the watermark and vanish
        from extraction. `cleanup_observations` then deleted them, because
        its own bound is a later wall-clock reading that they also sort
        below. Measured with the clock stepped back one hour: 3 observations
        written, 0 of 3 seen by extraction, 3 of 3 deleted — destroyed
        without ever reaching the LLM. `observations.id` is INTEGER PRIMARY
        KEY AUTOINCREMENT, so it cannot go backwards.

        A caller holding an old TIMESTAMP watermark (a marker written by a
        pre-v2.8.0 hook) passes a string; `_as_row_id` treats anything
        non-numeric as "no watermark", which degrades to the recent-N scan
        the callers already fall back to.
        """
        since = self._as_row_id(since_id)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM observations
                   WHERE project_id = ? AND id > ? AND is_private = 0
                   ORDER BY id ASC LIMIT ?""",
                (project_id, since, -1 if limit is None else max(0, int(limit)))
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _as_row_id(value):
        """A watermark as an int row id; anything else means 'from the start'."""
        try:
            return int(value)
        except (TypeError, ValueError):
            # why: a pre-v2.8.0 marker holds an ISO timestamp. Reading it as
            # "no watermark" replays recent observations, which is harmless
            # (extraction is idempotent through upsert_smart); reading it as a
            # HIGH watermark would hide them, which is the defect being fixed.
            return 0

    def observer_watermark(self, project_id, window=0):
        """The Stop observer's durable cursor into `observations`.

        Returns the row id the observer has consumed up to. A project that has
        never run one (0) is SEEDED to `max(0, newest_id - window)` and the
        seed is persisted, so a first run — including the first run after an
        upgrade — starts at the live end of the queue instead of replaying a
        backlog that a previous session, or PreCompact, already turned into
        memories. Within that window the reader stays oldest-first, which is
        the ordering register r6-B3 exists to protect.

        Seeding here rather than in the hook keeps the read and the write of
        this cursor in one place; the hook's job is to feed a model, not to
        know how a cursor is initialised.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT obs_watermark FROM projects WHERE id = ?",
                (project_id,)).fetchone()
            mark = int((row["obs_watermark"] if row else 0) or 0)
            if mark > 0:
                return mark
            newest = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM observations WHERE project_id = ?",
                (project_id,)).fetchone()[0]
            seed = max(0, int(newest) - max(0, int(window)))
            if seed:
                conn.execute(
                    "UPDATE projects SET obs_watermark = ? WHERE id = ?",
                    (seed, project_id))
            return seed

    def advance_observer_watermark(self, project_id, row_id):
        """Move the observer cursor forward. NEVER backward.

        `MAX(...)` in SQL, not a read-then-write: two sessions in one project
        share this cursor by design (that is the point of moving it off a
        per-session marker), and a slower one finishing second must not rewind
        what a faster one already consumed.
        """
        try:
            row_id = int(row_id)
        except (TypeError, ValueError):
            return
        if row_id <= 0:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET obs_watermark = MAX(COALESCE(obs_watermark, 0), ?) "
                "WHERE id = ?", (row_id, project_id))

    def cleanup_observations(self, project_id, before_id):
        """Delete this project's observations at or below row id `before_id`.

        INCLUSIVE ('<=', not '<') on purpose: the caller passes the id of the
        last row it CONSUMED, so that row must go too.

        The bound used to be a wall-clock string, which is what let a
        backwards clock step delete rows extraction had never seen (see
        `get_observations_since`). It also needed a paragraph of reasoning
        about same-second collisions to be correct at all; an id bound needs
        none — it deletes exactly what was read and nothing that arrived
        after.
        """
        before = self._as_row_id(before_id)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM observations WHERE project_id = ? AND id <= ?",
                (project_id, before)
            )
            return cur.rowcount

    def get_observation_count(self, project_id):
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM observations WHERE project_id = ?",
                (project_id,)
            ).fetchone()[0]

    # Retention ceiling for unconsumed observations. Extraction FAILURE no
    # longer deletes them (register C1 — a credential outage used to destroy
    # every observation unread), so a persistent failure needs a bound or the
    # table grows without one. 5000 is ~2 orders above a busy session's
    # output; the cap is enforced at the consumer (pre_compact), and hitting
    # it is LOGGED by the caller — no silent caps.
    _MAX_OBSERVATIONS = 5000

    def trim_observations(self, project_id, cap=_MAX_OBSERVATIONS):
        """Delete the OLDEST observations beyond `cap` rows. Returns rows
        deleted (0 while under the cap). Callers must log a nonzero return."""
        with self._connect() as conn:
            cur = conn.execute(
                """DELETE FROM observations WHERE project_id = ? AND id <= (
                       SELECT id FROM observations WHERE project_id = ?
                       ORDER BY id DESC LIMIT 1 OFFSET ?)""",
                (project_id, project_id, cap)
            )
            return cur.rowcount

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

    def get_latest_summary(self, project_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_summaries WHERE project_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (project_id,)
            ).fetchone()
            return dict(row) if row else None

    # ── progress (per-project, single row, ALWAYS overwrite) ────────────────

    def upsert_progress(self, project_id, **fields):
        """Overwrite the project's progress row. Anti-patch: never appends.

        Session-annotation cols (current_session_id / session_started_at) are
        deliberately EXCLUDED from defaults — they're managed by
        tag_progress_session() and preserved here across full-rewrites unless
        the caller explicitly passes them. This guarantees a PreCompact upsert
        from session A doesn't wipe a tag a UserPromptSubmit just wrote for
        the same session.
        """
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

        if "current_session_id" in fields and "session_started_at" in fields:
            merged["current_session_id"] = fields["current_session_id"]
            merged["session_started_at"] = fields["session_started_at"]
            need_tag_read = False
        else:
            need_tag_read = True

        with self._connect() as conn:
            # BEGIN IMMEDIATE so the tag read below and the write share ONE
            # transaction. The preserve-the-session-tag guarantee this method
            # documents was a cross-process lost update: `get_progress` opened
            # and CLOSED its own connection, so a `tag_progress_session` landing
            # between that read and this write was silently clobbered by the
            # stale merged value — and PROGRESS.md §0 then told the next Claude,
            # in the plugin's own voice, that another session's todos and files
            # were its own. `busy_timeout` serialises statements, never
            # sequences.
            conn.execute("BEGIN IMMEDIATE")
            if need_tag_read:
                row = conn.execute(
                    "SELECT current_session_id, session_started_at "
                    "FROM progress WHERE project_id = ?", (project_id,)
                ).fetchone()
                if "current_session_id" not in fields:
                    merged["current_session_id"] = (
                        (row["current_session_id"] if row else "") or "")
                if "session_started_at" not in fields:
                    merged["session_started_at"] = (
                        (row["session_started_at"] if row else "") or "")
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

        Bootstrap + patch are ONE transaction. The old shape was three — a
        get_progress() read, an upsert_progress() bootstrap when absent, then
        the UPDATE, each on its own connection — so concurrent hooks
        bootstrapping the same fresh project could interleave: B's stale "row
        absent" verdict replayed the default row OVER A's already-landed patch
        (reproduced: A's current_request came back ''). INSERT OR IGNORE
        leans on `project_id INTEGER PRIMARY KEY` and the schema's column
        DEFAULTs — verified identical to upsert_progress's defaults dict —
        so there is no window between the existence check and the write.
        Same BEGIN IMMEDIATE discipline as upsert_progress above.
        """
        if not fields:
            return
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
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO progress (project_id, updated_at) "
                "VALUES (?, ?)",
                (project_id, now)
            )
            conn.execute(
                f"UPDATE progress SET {set_clause} WHERE project_id = ?",
                params
            )

    def tag_progress_session(self, project_id, claude_session_id):
        """Mark `progress.current_session_id` with the active Claude session.

        Semantics:
          - Empty/None claude_session_id → no-op (some hooks don't get sid).
          - Stored sid == new sid → no-op (idempotent within a session).
          - Stored sid differs (or empty) → write new sid AND reset
            session_started_at to _now(), marking the boundary.

        Bootstraps an empty progress row if absent. Distinct from
        upsert_progress (full rewrite) and patch_progress (arbitrary fields):
        this helper is the ONLY caller that should touch session_started_at,
        keeping the boundary semantics consistent across all hook write paths.
        """
        if not claude_session_id:
            return
        cur = self.get_progress(project_id) or {}
        stored = (cur.get("current_session_id") or "").strip()
        if stored == claude_session_id:
            return
        self.patch_progress(
            project_id,
            current_session_id=claude_session_id,
            session_started_at=self._now(),
        )

    def get_recent_sessions(self, project_id, n=5):
        """Recent compacted sessions for PROGRESS.md §0 timeline.

        Joins `sessions` with the most recent `session_summaries` row per
        session (LEFT JOIN — sessions without a summary still appear). Sorted
        newest-first. Each row: claude_session_id, trigger_type, compacted_at,
        msg_count, brief_summary, summary_completed, summary_next_steps.

        The join key is pinned to ONE summary row per session (register D9):
        a bare `ON ss.session_id = s.id` fans out — a session with 3 summary
        rows occupied 3 of the LIMIT's slots (measured), so the §0 timeline
        showed the same session thrice and dropped older ones. MAX(id) is the
        newest summary by creation order (id, never a timestamp — see the
        ordering note above insert_session's readers).

        THE SAME FAN-OUT EXISTS ONE LEVEL UP, and D9 fixed only the lower one.
        `sessions` holds one row per COMPACTION, not per session, so a long
        session consumes as many of the five slots as it compacted — and the
        header this feeds calls them "Prior sessions". Measured on this repo's
        own shipped PROGRESS.md: two of the listed prior sessions were both
        `#317addcc`. Pinning to the newest compaction per `claude_session_id`
        makes the timeline show five distinct SESSIONS, which is what it
        claims. Rows with NO identity to collapse on are kept individually —
        and that means NULL *or the empty string*, not NULL alone. The first
        version of this predicate tested `IS NULL`, which is true of NULL and
        false of the sentinel the plugin's own hook actually writes:
        `hooks/pre_compact.py` reads `data.get("session_id", "")` and coerces a
        non-string to `""`, so every compaction whose payload lacked a usable
        session id carried `''` — and `'' IS NULL` is false, so they all
        collapsed onto ONE row and the timeline showed a single entry no matter
        how many compactions there had been. Measured: 5 distinct `''`
        compactions returned 1 row, the NULL control returned 5.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT s.id, s.claude_session_id, s.trigger_type,
                          s.compacted_at, s.msg_count, s.brief_summary,
                          ss.completed  AS summary_completed,
                          ss.next_steps AS summary_next_steps
                   FROM sessions s
                   LEFT JOIN session_summaries ss
                     ON ss.id = (SELECT MAX(id) FROM session_summaries
                                 WHERE session_id = s.id)
                   WHERE s.project_id = ? AND s.complete = 1
                     AND (COALESCE(s.claude_session_id, '') = ''
                          OR s.id = (SELECT MAX(s2.id) FROM sessions s2
                                     WHERE s2.project_id = s.project_id
                                       AND s2.complete = 1
                                       AND s2.claude_session_id
                                           = s.claude_session_id))
                   ORDER BY s.id DESC
                   LIMIT ?""",
                (project_id, n)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── plan_active (live plan anchor, v4) ──────────────────────────────────

    def get_plan_active(self, project_id):
        """Return the live plan row as a dict with `structured` decoded, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plan_active WHERE project_id = ?",
                (project_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["structured"] = json.loads(d.get("structured") or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            # why: malformed JSON — return raw + empty struct so the refiner
            # can retry rather than crashing the read path
            d["structured"] = {}
        return d

    # The writable plan_active columns. Shared by the two writers below so an
    # unknown field is refused IDENTICALLY on both branches — it used to be
    # silently dropped by INSERT and an sqlite3.OperationalError from UPDATE
    # (register D8), so the same caller bug had two different outcomes
    # depending on whether the row happened to exist yet.
    _PLAN_ACTIVE_FIELDS = frozenset((
        "raw", "structured", "active_step", "edits_since_last_guardian",
        "turns_since_last_guardian", "last_guardian_at", "last_refined_at",
        "needs_refine"))

    @classmethod
    def _check_plan_fields(cls, fields):
        unknown = set(fields) - cls._PLAN_ACTIVE_FIELDS
        if unknown:
            raise ValueError(
                f"unknown plan_active field(s): {sorted(unknown)} — writable "
                f"columns are {sorted(cls._PLAN_ACTIVE_FIELDS)}")
        if "structured" in fields and isinstance(fields["structured"], (dict, list)):
            fields["structured"] = json.dumps(fields["structured"], ensure_ascii=False)
        return fields

    def upsert_plan_active(self, project_id, **fields):
        """Insert-or-update the single plan_active row. Fields not provided
        retain their existing values; the row is bootstrapped if absent.

        Every UPDATE bumps `revision` (v7) — the optimistic-concurrency
        counter that update_plan_if_revision compares against. INSERT starts
        at 1. Unknown fields raise ValueError on BOTH branches (register D8).
        """
        fields = self._check_plan_fields(fields)
        now = self._now()
        existing = self.get_plan_active(project_id)
        with self._connect() as conn:
            if existing is None:
                # ON CONFLICT DO NOTHING + rowcount, then FALL THROUGH to the
                # UPDATE (register r6-A5): the absence check above and this
                # insert are separate transactions, so two first writers both
                # observed no row and the loser's bare INSERT raised
                # IntegrityError out of a method whose contract is upsert.
                # The loser now degrades to exactly what upsert means — its
                # fields UPDATE the winner's row, bumping revision. Callers
                # that must NOT last-write-win a creation race use
                # insert_plan_if_absent + the CAS instead.
                if self._insert_plan_row(conn, project_id, fields, now):
                    return
            if not fields:
                return
            set_clause = (", ".join(f"{k} = ?" for k in fields)
                          + ", revision = revision + 1, updated_at = ?")
            params = list(fields.values()) + [now, project_id]
            conn.execute(
                f"UPDATE plan_active SET {set_clause} WHERE project_id = ?",
                params,
            )

    @staticmethod
    def _insert_plan_row(conn, project_id, fields, now):
        """INSERT the plan row iff absent, on the caller's connection.
        Returns True iff THIS statement created it (rowcount)."""
        row = {
            "project_id": project_id,
            "raw": "",
            "structured": "",
            "active_step": 0,
            "edits_since_last_guardian": 0,
            "turns_since_last_guardian": 0,
            "last_guardian_at": "",
            "last_refined_at": "",
            "needs_refine": 0,
            "created_at": now,
            "updated_at": now,
        }
        row.update(fields)
        cur = conn.execute(
            """INSERT INTO plan_active
               (project_id, raw, structured, active_step,
                edits_since_last_guardian, turns_since_last_guardian,
                last_guardian_at, last_refined_at, needs_refine,
                revision, created_at, updated_at)
               VALUES (:project_id, :raw, :structured, :active_step,
                       :edits_since_last_guardian, :turns_since_last_guardian,
                       :last_guardian_at, :last_refined_at, :needs_refine,
                       1, :created_at, :updated_at)
               ON CONFLICT(project_id) DO NOTHING""",
            row,
        )
        return bool(cur.rowcount)

    def insert_plan_if_absent(self, project_id, **fields):
        """Create the plan row iff none exists; True iff this caller won.

        The CREATION half of the revision protocol (register r6-B2): a loser
        returns False and must re-read + CAS against the winner's row —
        upsert_plan_active's fall-through would instead last-write-win the
        race, which is correct for captures but not for a gated replacement.
        """
        fields = self._check_plan_fields(fields)
        with self._connect() as conn:
            return self._insert_plan_row(conn, project_id, fields, self._now())

    def update_plan_if_revision(self, project_id, expected_revision, **fields):
        """CAS write against the plan_active row: applies `fields` ONLY while
        `revision` is still the value the caller read, bumping it on success.
        Returns rowcount — 0 means the plan changed underneath the caller and
        the stale write became a no-op.

        The choke point for every read-modify-write of the plan slot. Without
        it, a TodoWrite sync that read PLAN A, stalled, and wrote after PLAN A
        had been REPLACED (with every step dispositioned through the R610
        gate) resurrected PLAN A wholesale — the one door the gate cannot see
        (register X4, measured).
        """
        fields = self._check_plan_fields(fields)
        if not fields:
            return 0
        now = self._now()
        set_clause = (", ".join(f"{k} = ?" for k in fields)
                      + ", revision = revision + 1, updated_at = ?")
        params = list(fields.values()) + [now, project_id, expected_revision]
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE plan_active SET {set_clause} "
                f"WHERE project_id = ? AND revision = ?",
                params,
            )
            return cur.rowcount

    def clear_plan_active(self, project_id):
        """Empty the live plan slot, KEEPING the row as a tombstone.

        Was a DELETE. Under the v7 revision CAS a delete re-opens the ABA
        window the CAS exists to close: a stale writer holding revision N
        could match a RECREATED row whose fresh INSERT restarted the counter
        at 1 == N, and the resurrection (register X4) would be back through
        the cleared-and-replanned path. The tombstone keeps `revision`
        monotonic across clears; an empty `structured` fails
        is_valid_structured, so every reader already renders it as "no active
        plan".
        """
        with self._connect() as conn:
            conn.execute(
                """UPDATE plan_active
                   SET raw = '', structured = '', active_step = 0,
                       needs_refine = 0, revision = revision + 1,
                       updated_at = ?
                   WHERE project_id = ?""",
                (self._now(), project_id)
            )

    def bump_plan_edit_counter(self, project_id, n=1):
        """Atomically increment edits_since_last_guardian. No-op if no plan."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE plan_active
                   SET edits_since_last_guardian = edits_since_last_guardian + ?,
                       updated_at = ?
                   WHERE project_id = ?""",
                (n, self._now(), project_id),
            )

    def bump_plan_turn_counter(self, project_id, n=1):
        """Atomically increment BOTH turn counters.

        `turns_since_last_guardian` is the drift counter and is reset by every
        guardian check and plan replacement. `turns_total` (v9) is never reset
        by anything — it is the monotonic clock directive idleness is measured
        against, because a resettable counter forgives neglect the moment
        somebody runs `/cc-mem plan-check`.
        """
        with self._connect() as conn:
            conn.execute(
                """UPDATE plan_active
                   SET turns_since_last_guardian = turns_since_last_guardian + ?,
                       turns_total = turns_total + ?,
                       updated_at = ?
                   WHERE project_id = ?""",
                (n, n, self._now(), project_id),
            )

    def reset_plan_guardian_counters(self, project_id):
        """Mark a guardian check just happened: reset both counters + timestamp."""
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """UPDATE plan_active
                   SET edits_since_last_guardian = 0,
                       turns_since_last_guardian = 0,
                       last_guardian_at = ?,
                       updated_at = ?
                   WHERE project_id = ?""",
                (now, now, project_id),
            )

    # ── directives (v8) ──────────────────────────────────────────────────────
    # A directive is a unit of INTENT and outlives every plan. See the
    # v8_directives migration comment for why this is not plan steps.

    def upsert_directive(self, project_id, slug, **fields):
        """Insert-or-update one directive, keyed by (project_id, slug).

        Re-stating an existing directive bumps `times_stated` and refreshes
        `last_seen_at` rather than creating a second row: the repetition count
        IS the signal, so it must accumulate on one row. A caller that wants
        to correct a field passes it explicitly; anything omitted is kept.
        """
        now = self._now()
        allowed = ("quote", "demand", "kind", "status", "times_stated",
                   "source", "evidence", "closed_at")
        # WRITE-PATH CLEANING, the same half `upsert_smart` performs for a
        # memory. `quote`, `demand` and `evidence` are free text that is later
        # interpolated into the Stop hook's block `reason` — a decision payload
        # the harness feeds back to Claude, which is a HIGHER-authority channel
        # than PROGRESS.md. Escaping only at render leaves rows already stored
        # by v2.11.0 armed, which is exactly why the marker defence is applied
        # on both sides rather than one. Imported lazily: db.py deliberately
        # keeps no module-level dependency on core.privacy.
        from core.privacy import clean_for_storage
        for _slot in ("quote", "demand", "evidence"):
            if fields.get(_slot) is not None:
                fields[_slot] = clean_for_storage(str(fields[_slot]))
        # BEGIN IMMEDIATE, exactly as `reconcile_upsert` does for `memories`.
        # The read-then-write below is the correct POLICY — "a slot the caller
        # omitted keeps its stored value" cannot be expressed by an
        # `ON CONFLICT DO UPDATE` arm, because `excluded.*` has already had the
        # NOT NULL column defaults applied and can no longer be distinguished
        # from an explicit empty string (tried, and it wiped `demand`/`quote`
        # on every bare re-statement). What was actually wrong was the
        # ISOLATION: `sqlite3` takes no write lock for a SELECT, so two
        # concurrent creators of one slug both saw None and both INSERTed, and
        # the loser died on `idx_directives_slug` with an IntegrityError out of
        # a hook. `times_stated` — the entire point of this table — is exactly
        # the counter a lost write corrupts. One write lock, taken up front,
        # makes the whole read-decide-write atomic without changing the policy.
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # v9: stamp the monotonic turn clock INSIDE the same transaction,
            # read here rather than passed in — every caller would otherwise
            # have to know that a directive's idleness is measured in plan
            # turns, and the one that forgot would write a row that can never
            # be seen as idle. Absent plan row -> 0, which reads as "touched at
            # the beginning of time" and is the safe direction for a ledger
            # whose job is to notice neglect.
            turns_now = self._project_turns_total(conn, project_id)
            if row := conn.execute(
                    "SELECT * FROM directives WHERE project_id = ? AND slug = ?",
                    (project_id, slug)).fetchone():
                sets, params = [], []
                for key in allowed:
                    if key in fields and fields[key] is not None:
                        sets.append(f"{key} = ?")
                        params.append(fields[key])
                if "times_stated" not in fields:
                    sets.append("times_stated = times_stated + 1")
                sets += ["last_seen_at = ?", "updated_at = ?",
                         "turns_at_touch = ?"]
                params += [now, now, turns_now, project_id, slug]
                conn.execute(
                    f"UPDATE directives SET {', '.join(sets)} "
                    "WHERE project_id = ? AND slug = ?", params)
                return "updated"
            data = {k: fields.get(k) for k in allowed}
            conn.execute(
                """INSERT INTO directives
                   (project_id, slug, quote, demand, kind, status,
                    times_stated, source, evidence, first_seen_at,
                    last_seen_at, closed_at, created_at, updated_at,
                    turns_at_touch)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, slug,
                 data["quote"] or "", data["demand"] or "",
                 data["kind"] or "standing", data["status"] or "active",
                 int(data["times_stated"] or 1),
                 data["source"] or "user", data["evidence"] or "",
                 now, now, data["closed_at"] or "", now, now, turns_now))
            return "created"

    @staticmethod
    def _project_turns_total(conn, project_id):
        """The project's monotonic turn count, or 0 when it has no plan row."""
        row = conn.execute(
            "SELECT turns_total FROM plan_active WHERE project_id = ?",
            (project_id,)).fetchone()
        return int(row["turns_total"]) if row else 0

    def list_directives(self, project_id, status=None):
        """Directives for a project, most-repeated first. status=None → all."""
        sql = "SELECT * FROM directives WHERE project_id = ?"
        params = [project_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY times_stated DESC, id ASC"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def edit_directive(self, project_id, slug, **fields):
        """Correct a directive's fields WITHOUT bumping `times_stated`.

        v2.12.0, from the Autoshop field report: `directive-add` was the only
        way to fix a typo or a rotten reference in `demand`, and it counts
        every call as a re-statement — nine reference repairs there inflated
        the count on nine rows, and `directive-list` orders by that count, so
        the MOST-EDITED directives floated to the top regardless of how often
        the user actually asked for them. An edit is maintenance of the
        record, not a statement of intent, and must not touch the signal.

        Refuses (returns 0) when the slug does not exist: an edit that
        silently creates would be `upsert_directive` with the count semantics
        stripped — a second write path with divergent defaults.
        `last_seen_at` is deliberately NOT touched either: it records when
        the user last STATED the directive. `turns_at_touch` IS stamped —
        any write to the row is attention, and the idleness clock measures
        neglect, not statements. Same write-path cleaning as
        `upsert_directive`, for the same reason (the text reaches the Stop
        hook's block `reason`, a higher-authority channel than PROGRESS.md).
        """
        now = self._now()
        allowed = ("quote", "demand", "kind", "status", "source", "evidence")
        from core.privacy import clean_for_storage
        sets, params = [], []
        for key in allowed:
            if key in fields and fields[key] is not None:
                value = fields[key]
                if key in ("quote", "demand", "evidence"):
                    value = clean_for_storage(str(value))
                sets.append(f"{key} = ?")
                params.append(value)
        if not sets:
            return 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            turns_now = self._project_turns_total(conn, project_id)
            sets += ["updated_at = ?", "turns_at_touch = ?"]
            params += [now, turns_now, project_id, slug]
            cur = conn.execute(
                f"UPDATE directives SET {', '.join(sets)} "
                "WHERE project_id = ? AND slug = ?", params)
            return cur.rowcount

    def set_directive_status(self, project_id, slug, status, evidence=""):
        """Close/reopen a directive. Closing without evidence is refused by the
        CLI layer, not here — the DB records what it is told.

        Stamps `turns_at_touch` too (v9): a status change IS progress on the
        directive, so it must restart the idleness clock. Without it, reopening
        a closed directive produced a row that was instantly "idle" by however
        many turns had passed while it was closed.
        """
        now = self._now()
        closed = now if status in ("done", "superseded", "dropped") else ""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            turns_now = self._project_turns_total(conn, project_id)
            cur = conn.execute(
                """UPDATE directives
                   SET status = ?, closed_at = ?, evidence = ?, updated_at = ?,
                       turns_at_touch = ?
                   WHERE project_id = ? AND slug = ?""",
                (status, closed, evidence, now, turns_now, project_id, slug))
            return cur.rowcount

    # ── FTS5 search ─────────────────────────────────────────────────────────

    # Hard ceiling on any search LIMIT. A caller-supplied limit is a hint, not a
    # licence to materialise the table: search_fts(pid, q, limit=10**6) used to
    # fetch every active row (measured 400/400 on a 400-row project, driven from
    # the MCP tool argument). SQLite also reads a NEGATIVE limit as "no limit",
    # so the floor is as load-bearing as the ceiling.
    _MAX_SEARCH_LIMIT = 1000

    @staticmethod
    def _like_escape(text):
        """Neutralise LIKE metacharacters, for use with ESCAPE '\\'.

        Order matters: the backslash is doubled FIRST, else the backslashes
        introduced for % and _ get escaped a second time. Unescaped, a query of
        "%" matched every row and a query of "_" matched every row with at least
        one character — a full table dump from a one-character search.
        """
        return (text.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_"))

    def _match_fts(self, conn, project_id, query, limit):
        """Run the MATCH query. Returns rows, or None if FTS could not answer.

        A malformed MATCH expression is a property of the USER'S QUERY, not
        evidence of a broken index: `"`, `AND`, `a OR`, `NEAR/`, `x*"y` and `%`
        every one raise sqlite3.OperationalError out of the fts5 parser
        ('unterminated string', 'fts5: syntax error near ...'). Answering that
        by rebuilding the whole index (measured: 6/6 such queries triggered a
        rebuild) turns a read-only search into an unbounded WRITE on
        caller-chosen input — ~52 ms at 20k rows, and it fixes nothing, because
        the query is still malformed on the retry.

        So a parse failure is retried ONCE as a quoted phrase, which is always
        syntactically valid fts5 and searches for the user's text literally.
        Only when THAT fails too is the index itself the suspect, and only then
        do we rebuild (once) and let the caller fall through to LIKE.
        """
        sql = """SELECT m.* FROM memories m
                 JOIN memories_fts f ON m.id = f.rowid
                 WHERE memories_fts MATCH ?
                   AND m.project_id = ? AND m.is_active = 1
                 ORDER BY rank
                 LIMIT ?"""
        for expr in (query, '"' + query.replace('"', '""') + '"'):
            try:
                rows = conn.execute(sql, (expr, project_id, limit)).fetchall()
                if not rows and not self._fts_triggers_present(conn):
                    # An EMPTY match over an index nothing maintains is not an
                    # answer. Another process — an fts5-less interpreter opening
                    # this file is the deterministic case — can drop the
                    # triggers while this handle still believes FTS is healthy;
                    # every write since then bypassed the index, and a MATCH
                    # that SUCCEEDS and returns nothing would be reported to the
                    # caller as "no such memory". Returning None routes it to
                    # the LIKE fallback instead. One sqlite_master read, and
                    # only when the index already answered empty.
                    return None
                return [dict(r) for r in rows]
            except sqlite3.DatabaseError:
                # why: fts5 rejected this expression, OR the index is damaged.
                # The next iteration retries the always-valid quoted-phrase
                # form; if that fails as well we leave the loop and treat the
                # INDEX, not the query, as broken. DatabaseError because a
                # corrupt index raises SQLITE_CORRUPT_VTAB as a BARE
                # DatabaseError, which the OperationalError guard did not
                # catch — so `search_fts`'s documented LIKE fallback was
                # unreachable and every search RAISED to the CLI, the MCP
                # server and the web viewer instead.
                continue
        self._rebuild_fts5()
        return None

    def search_fts(self, project_id, query, limit=30):
        query = query if isinstance(query, str) else str(query or "")
        # Strip C0 controls before the query reaches MATCH. fts5 receives the
        # expression as a C string, so a NUL TRUNCATES it: both forms tried in
        # `_match_fts` then fail (`fts5: syntax error near ""` and
        # `unterminated string`), and its double-failure branch concludes the
        # INDEX is broken and runs a full `'rebuild'`. That turns a read into
        # an unbounded write on caller-chosen input — reachable from the web
        # viewer's `?q=%00` and from the `memory_search` MCP tool, whose
        # `minLength: 1` a lone NUL satisfies. These characters tokenise to
        # nothing in fts5, so removing them changes no legitimate result.
        query = "".join(c for c in query if c >= " " or c in "\t\n\r")
        try:
            limit = min(max(1, int(limit)), self._MAX_SEARCH_LIMIT)
        except (TypeError, ValueError):
            # why: a non-numeric limit is a caller bug, not a reason to fail a
            # read — use the documented default instead of raising.
            limit = 30
        with self._connect() as conn:
            if self._fts5_available:
                rows = self._match_fts(conn, project_id, query, limit)
                if rows is not None:
                    return rows
            pat = "%" + self._like_escape(query) + "%"
            rows = conn.execute(
                """SELECT * FROM memories
                   WHERE project_id = ? AND is_active = 1
                     AND (content LIKE ? ESCAPE '\\'
                          OR tags LIKE ? ESCAPE '\\'
                          OR COALESCE(topic, '') LIKE ? ESCAPE '\\')
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
            # Same check-then-insert race as `upsert_project`, against the
            # `UNIQUE (project_id, name)` on `topics` — and reachable from the
            # consolidation leg, which runs CONCURRENTLY with the PreCompact
            # sync leg by design. The version bump moves into the conflict
            # clause so it still counts exactly one revision per call.
            conn.execute(
                "INSERT INTO topics (project_id, name, content, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id, name) DO UPDATE SET "
                "content = excluded.content, updated_at = excluded.updated_at, "
                "version = topics.version + 1",
                (project_id, name, content, now)
            )

    def get_topics(self, project_id, limit=None):
        """Topic summaries, newest first. `limit` bounds the row count.

        Unbounded by default because five of the seven callers render into a
        budgeted artifact and do their own trimming. The two that hand the
        result to a CLIENT — `mcp/server.py`'s `memory_topics` and the web
        viewer's `/api/topics` — pass a limit: `memory_topics` is a
        model-invokable tool that returned every topic with its full body,
        measured at 272 KB (~68 000 tokens) from a real project database, and a
        tool result that large is a context-window denial of service dressed as
        an answer.
        """
        sql = ("SELECT name, content, updated_at, version FROM topics "
               "WHERE project_id = ? ORDER BY updated_at DESC")
        params = [project_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, min(int(limit), self._MAX_SEARCH_LIMIT)))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # delete_topic lived here until 2026-08-09: canonicalize_topics was its
    # only caller, and merge_topic_variant absorbed the DELETE into the same
    # transaction as the relabel (a kill between the two commits stranded a
    # summary for a label no memory carries — the r6 triage's recorded limit).

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

    def update_plan_status(self, plan_id, status, notes=None,
                           field="feasibility", *, project_id):
        """Set a plan's status (and optionally one notes column).

        Returns cur.rowcount: 0 means the UPDATE matched nothing. Callers MUST
        surface that — `cli/plan.py done 9999 ghost` used to print
        "Plan #9999 -> done: ghost" and exit 0 because this method discarded
        the rowcount.

        `project_id` is REQUIRED and KEYWORD-ONLY as of v2.5.3. `plans.id` is
        global to the DB FILE, not to a project, and one memory.db can hold
        several project rows (a directory rename creates a second one, and
        the dashboard opens arbitrary databases via its projects.json
        registry), so an unscoped call rewrites whatever row owns that
        id — including another project's status and result columns. Through
        v2.5.2 it merely *defaulted* to None "so the pre-v2.5 signature stays
        callable", which meant the cross-project write stayed one forgotten
        argument away and no test could catch it; README and CLAUDE.md both
        recorded that as a known unfixed limit for two releases. Every one of
        the 11 call sites in the tree already passed it as a keyword, so making
        it mandatory cost nothing and closed the hole: a caller that does not
        know its project now fails loudly at the call, not silently in another
        project's data.

        `field` names a column and so cannot be a bound parameter; it is
        whitelisted rather than interpolated blind. `project_id` is a bound
        parameter.
        """
        if field not in ("feasibility", "result"):
            field = "feasibility"
        now = self._now()
        with self._connect() as conn:
            if notes is not None:
                cur = conn.execute(
                    f"UPDATE plans SET status = ?, {field} = ?, updated_at = ? "
                    f"WHERE id = ? AND project_id = ?",
                    [status, notes, now, plan_id, project_id]
                )
            else:
                cur = conn.execute(
                    "UPDATE plans SET status = ?, updated_at = ? "
                    "WHERE id = ? AND project_id = ?",
                    [status, now, plan_id, project_id]
                )
            return cur.rowcount

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

    def delete_plan(self, plan_id, *, project_id):
        """Delete one plan row. Returns cur.rowcount (0 = matched nothing).

        `project_id` is REQUIRED and KEYWORD-ONLY (v2.5.3), for the same reason
        as `update_plan_status`: `plans.id` is global to the DB FILE, not to a
        project, and one memory.db can hold several project rows (renames;
        the dashboard's projects.json registry). Unscoped, a stale or
        typo'd id deletes whatever row owns it — including another project's.
        This is a DELETE, so that loss is unrecoverable.
        """
        with self._connect() as conn:
            return conn.execute(
                "DELETE FROM plans WHERE id = ? AND project_id = ?",
                [plan_id, project_id]).rowcount

    def update_plan_content(self, plan_id, content, *, project_id):
        """Rewrite one plan's content. Returns cur.rowcount (0 = no match).

        `project_id` is REQUIRED and KEYWORD-ONLY (v2.5.3) — see `delete_plan`.
        """
        with self._connect() as conn:
            return conn.execute(
                "UPDATE plans SET content = ?, updated_at = ? "
                "WHERE id = ? AND project_id = ?",
                [content, self._now(), plan_id, project_id]).rowcount

    def reorder_plans(self, project_id, plan_ids):
        """Renumber exec_order to match the given id sequence.

        Returns the number of rows actually updated. Callers compare it against
        len(plan_ids) to detect ids that do not belong to this project —
        `cli/plan.py reorder 9999 8888` used to print "Reordered Plans" and
        exit 0 while changing nothing, because this method discarded rowcount.
        """
        now = self._now()
        updated = 0
        with self._connect() as conn:
            for order, pid in enumerate(plan_ids, 1):
                cur = conn.execute(
                    "UPDATE plans SET exec_order = ?, updated_at = ? "
                    "WHERE id = ? AND project_id = ?",
                    (order, now, pid, project_id)
                )
                updated += cur.rowcount
        return updated

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
                "ORDER BY id DESC LIMIT 1",
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

