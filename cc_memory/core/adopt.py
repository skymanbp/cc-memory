r"""Adopt a stray database: merge one sub-directory's `memory.db` into its project.

RAW docstring for the reason `core/roots.py` gives: this module spells Windows
paths, and a plain docstring turns `D:\Projects` into the invalid escape `\P`.

WHY THIS EXISTS
---------------
`core/roots.py` PREVENTS a stray: a sub-directory with no database of its own
resolves to the project root. It deliberately never HEALS one, because a stray
and a deliberate nested project are byte-for-byte identical on disk — and an
existing database is terminal at rung 0, so a stray born before the ladder
existed (v2.6.0) keeps being written for as long as sessions run in that
directory. Measured on the reporting machine, 2026-10-13: a database under
`<project>/companion`, born 2026-07-24 (two weeks before the ladder), held
14025 memory rows across 2181 sessions that no session at the project root
ever read. `/cc-mem status` named it; nothing could merge it. The resolver's
docstring said adoption "is an explicit user-confirmed command, never
something a hook does" — and no such command existed. This is that command,
`/cc-mem adopt-stray`.

WHAT ADOPTION IS — and what it is not
-------------------------------------
* EXPLICIT. A dry run (the default) reads both databases and writes nothing;
  `--yes` is required to write. No hook ever imports this module.
* RECOVERABLE. The stray's whole state directory is MOVED, never deleted, to
  `<project>/.ccm/.adopted/<YYYY-MM-DD>/<relative path, "/" -> "__">/`, so its
  `memory.db` (with its `-wal`/`-shm`), PROGRESS.md, session archives and plan
  history stay readable there. The move happens FIRST: once the state directory
  has left the sub-directory, the next hook run there resolves to the project
  root (rung 1), so nothing is written into the stray after it was read. A
  directory the operating system refuses to move (on Windows: a handle open
  inside it) is reported PENDING and nothing is written anywhere.
* NOT A SAVE PATH. The anti-patch contract routes every NEW fact through
  `llm.memory_writer.upsert_smart`. Adoption creates no fact: it relocates rows
  a writer already reconciled in their own database. It therefore keeps every
  row — active and archived — with its timestamps, importance, topic, recall
  and reference counters, and only re-numbers it (`memories.id` is global to a
  database file). What the writer would have decided against the OTHER
  database is decided here only where it is exact: an ACTIVE stray row whose
  `content_hash` equals an active project row is imported ARCHIVED, its
  `supersedes_id` pointing at the project row (the shape consolidation gives a
  merged loser, `MemoryDB.archive_obsolete`), and the project row is
  REINFORCED as an exact-hash restatement is (`importance` max, tags unioned
  through `llm.memory_writer._merged_tags`). Near-duplicates are left to the
  consolidation the import's backlog triggers; no model is called here.
* PROVENANCED AND IDEMPOTENT. Every source row — imported, folded or skipped —
  is recorded in `adopted_rows` (source database, table, source id, new id,
  outcome) in the SAME transaction as the row it describes. A second run on the
  same stray imports nothing; a run cut off half way resumes where it stopped.
  The `source` key is the stray database's ORIGINAL path, canonical; the
  manifest written beside the moved files (`ADOPTED.json`) records it, which is
  how a resumed run finds the moved copy again.
* SHORT TRANSACTIONS. The project database is live while this runs (every hook
  writes it). Each batch of `_CHUNK` rows is one `BEGIN IMMEDIATE` on a
  `MemoryDB._connect` handle, whose busy timeout bounds what a hook waits.

PER TABLE
---------
memories           every row; ids re-numbered; `session_id` and `supersedes_id`
                   re-mapped through the id maps (a link to a row that never
                   existed is dropped); content passed through
                   `core.privacy.clean_for_storage` (idempotent on rows a
                   writer already cleaned, and the write-path guard a row from
                   a pre-v2.5 database never met); a category outside
                   `core.db.CATEGORIES` becomes `note`, as the writer coerces
                   it; exact duplicates folded as above.
sessions           every row; `archive_path` re-pointed into the moved
                   directory, relative to the project's state directory.
session_summaries  every row whose session was imported.
topics             by name: a new name is inserted; an existing name keeps the
                   project's summary (the next consolidation rewrites it from
                   the combined membership) and takes the higher `version`.
keywords           by keyword: frequencies summed, `last_seen` the later one.
directives         by slug: a new slug is inserted with its idle clock started
                   NOW (`turns_at_touch` = the project's `turns_total`) — the
                   stray's turn count is another database's clock, and keeping
                   it would make the directive idle the moment it arrives; an
                   existing slug keeps the project's row.
observations       only what the repository's retention would still keep: rows
                   above the stray's own observer cursor (`obs_watermark` — the
                   rows at or below it were already fed to a model), newest
                   first, up to the head room under
                   `MemoryDB._MAX_OBSERVATIONS`. The rest are counted and
                   skipped, by reason.
progress, plan_active, plans, projects
                   single-row (or retired) state of the stray's own project:
                   never merged over the project's. They stay readable in the
                   moved database, and the report says so.
"""
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from core.db import CATEGORIES, MemoryDB, _readonly_uri, readonly_connect
from core.layout import (DB_FILENAME, MEMORY_DIRNAME, canonical_path,
                         database_owner, is_ccm_dir, state_dir_candidates)
from core.markers import _is_link
from core.roots import PIN_MARKER

# Where adopted state directories go, under the PROJECT's state directory. The
# generated `.gitignore` there names entries one by one, so this directory
# carries its own ignore-everything file (`_ensure_adopted_root`).
ADOPTED_DIRNAME = ".adopted"

# The record beside every moved directory. It is what lets a run that was cut
# off after the move find its source again.
MANIFEST_NAME = "ADOPTED.json"

# Rows per write transaction. Each batch holds the project database's write
# lock; a hook waits for it inside `busy_timeout` (5 s, `MemoryDB._connect`).
_CHUNK = 400

# Tables adopted row by row, in dependency order: sessions before the rows
# that reference them, memories before the link pass.
TABLE_ORDER = ("sessions", "session_summaries", "memories", "topics",
               "keywords", "directives", "observations")

# Single-row or retired state that is never merged over the project's own.
NOT_MERGED = ("progress", "plan_active", "plans", "projects")

# Outcome names. The skip reasons are sentences because the report prints them.
IMPORTED = "imported"
MERGED_DUPLICATE = "merged-duplicate"
MERGED_BY_NAME = "merged-by-name"
SUMMED = "summed"
KEPT_PROJECT = "kept-project-row"
SKIP_EMPTY = "skipped: empty after the privacy filter"
SKIP_NO_SESSION = "skipped: its session was not imported"
SKIP_FED = ("skipped: already fed to an observer (at or below the stray's "
            "cursor)")
SKIP_CAP = (f"skipped: over the retention cap "
            f"({MemoryDB._MAX_OBSERVATIONS} rows per project)")


class AdoptRefused(Exception):
    """The path given is not something this command may adopt."""


class AdoptPending(Exception):
    """The stray is in use right now; nothing was written."""


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _is_below(child, parent):
    """True when `child` is STRICTLY below `parent`, both canonical."""
    c, p = canonical_path(child), canonical_path(parent)
    if not c or not p or c == p:
        return False
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        # why: different drives on Windows have no common path; such a
        # directory is not below the project, which is the refusal we want
        return False


def _slug(owner, root):
    """`<owner>` relative to `<root>`, path separators spelled `__`."""
    rel = Path(os.path.relpath(canonical_path(owner), canonical_path(root)))
    return "__".join(part for part in rel.parts if part not in ("", "."))


def adopted_root(memory_dir):
    """`<project>/.ccm/.adopted` — the one place adopted directories live."""
    return Path(memory_dir) / ADOPTED_DIRNAME


def _find_manifest(memory_dir, source_key):
    """The manifest of an earlier adoption of `source_key`, or None."""
    base = adopted_root(memory_dir)
    if not base.is_dir():
        return None
    for manifest in sorted(base.glob(f"*/*/{MANIFEST_NAME}")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # why: an unreadable manifest is not this source's; the scan goes on
            continue
        if isinstance(data, dict) and data.get("source_key") == source_key:
            return manifest
    return None


def _candidate_dbs(path):
    """Every `memory.db` the user's PATH can mean, most specific first."""
    p = Path(path)
    if p.name == DB_FILENAME:
        return [p]
    if p.name in {c.name for c in state_dir_candidates(".")}:
        return [p / DB_FILENAME]
    return [c / DB_FILENAME for c in state_dir_candidates(p)]


def locate(project_root, memory_dir, raw):
    """What `raw` names: `{db, state_dir, owner, source_key, source_path, manifest}`.

    `raw` may be the stray's directory, its state directory or its `memory.db`;
    a relative path is taken relative to the PROJECT root, the way a user reads
    the `status` line that named it. Raises `AdoptRefused` for anything that is
    not a stray strictly below this project. `manifest` is set when an earlier
    run already moved this source — the run then resumes from the moved copy.
    """
    root = Path(project_root)
    given = Path(raw)
    if not given.is_absolute():
        given = root / given
    candidates = _candidate_dbs(given)
    if not any(c.exists() for c in candidates):
        for cand in candidates:
            key = canonical_path(cand)
            manifest = _find_manifest(memory_dir, key)
            if manifest is not None:
                return {"db": manifest.parent / DB_FILENAME,
                        "state_dir": manifest.parent,
                        "owner": database_owner(cand), "source_key": key,
                        "source_path": str(cand), "manifest": manifest}
    db = next((c for c in candidates if c.is_file()), None)
    if db is None:
        raise AdoptRefused(
            f"no memory database at {given} (looked for "
            f"{', '.join(str(c) for c in candidates)})")
    owner = database_owner(db)
    if owner is None:
        raise AdoptRefused(
            f"{db} is not a state directory's database (expected "
            f"<dir>/{MEMORY_DIRNAME}/{DB_FILENAME}, or the pre-v2.13.0 "
            f"<dir>/memory/{DB_FILENAME})")
    if canonical_path(owner) == canonical_path(root):
        raise AdoptRefused(
            f"{db} is this project's OWN database — there is nothing to merge "
            f"it into")
    if _is_below(db, memory_dir):
        raise AdoptRefused(
            f"{db} is inside this project's own state directory {memory_dir} "
            f"(an adopted copy is never adopted again)")
    if not _is_below(owner, root):
        raise AdoptRefused(
            f"{owner} is not strictly below the project root {root}; only a "
            f"database inside the project can be adopted into it")
    state_dir = db.parent
    for probe in (state_dir, db):
        if _is_link(probe):
            raise AdoptRefused(
                f"{probe} is a symlink/junction; cc-memory never moves or "
                f"reads state through a link")
    if (Path(owner) / PIN_MARKER).exists():
        raise AdoptRefused(
            f"{owner} is pinned as a project in its own right ({PIN_MARKER}). "
            f"Adopting it would not stick — the next session there starts a "
            f"new database. Remove the pin first if it is a stray.")
    if is_ccm_dir(state_dir) is not True:
        raise AdoptRefused(
            f"{state_dir} could not be positively identified as a directory "
            f"cc-memory wrote; it is not moved on a guess")
    return {"db": db, "state_dir": state_dir, "owner": owner,
            "source_key": canonical_path(db), "source_path": str(db),
            "manifest": None}


# ── reading ─────────────────────────────────────────────────────────────────

def _open_readonly(db_path):
    """A read-only handle that never creates side files it does not need.

    `immutable=1` when there is no write-ahead log to replay, so a dry run does
    not leave `-wal`/`-shm` siblings behind (the reason `cli/mem.py
    _count_active_memories` uses it); `mode=ro` through `readonly_connect` when
    a non-empty `-wal` exists, because immutable would ignore its content.
    """
    wal = Path(str(db_path) + "-wal")
    try:
        has_wal = wal.is_file() and wal.stat().st_size > 0
    except OSError:
        # why: an unreadable WAL is treated as present, so its content is
        # replayed by the mode=ro path instead of silently ignored
        has_wal = True
    if has_wal:
        return readonly_connect(db_path)
    uri = _readonly_uri(Path(db_path).resolve().as_posix()) + "&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        # why: a table this database never had reads as "no columns"
        return set()


def _rows(conn, table):
    """Every row of `table` as dicts, by id; [] when the table is absent.

    `table` is always one of this module's own literals (`TABLE_ORDER`),
    never input, which is why it may be interpolated.
    """
    if not _columns(conn, table):
        return []
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]


def read_source(db_path):
    """Everything the import needs from the stray, read in one pass."""
    conn = _open_readonly(db_path)
    try:
        data = {t: _rows(conn, t) for t in TABLE_ORDER}
        mark = 0
        if "obs_watermark" in _columns(conn, "projects"):
            row = conn.execute(
                "SELECT MAX(COALESCE(obs_watermark, 0)) FROM projects").fetchone()
            mark = int(row[0] or 0) if row else 0
        data["_watermark"] = mark
        data["_not_merged"] = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in NOT_MERGED if _columns(conn, t)}
        return data
    finally:
        conn.close()


_COUNT_ROWS = (
    ("memories (active)", "memories", " AND is_active = 1"),
    ("memories (archived)", "memories", " AND is_active = 0"),
    ("sessions", "sessions", ""),
    ("session_summaries", "session_summaries", ""),
    ("topics", "topics", ""),
    ("keywords", "keywords", ""),
    ("directives", "directives", ""),
    ("observations", "observations", ""),
    ("adopted_rows", "adopted_rows", ""),
)


def project_counts(conn, pid):
    """Per-table counts of ONE project's rows — the before/after report.

    `pid` None (no project row yet) reads as all zeros without touching `conn`.
    """
    out = {}
    for label, table, extra in _COUNT_ROWS:
        if pid is None or not _columns(conn, table):
            out[label] = 0
            continue
        out[label] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE project_id = ?{extra}",
            (pid,)).fetchone()[0]
    return out


def _done(conn, source_key, table):
    """{source_id: (new_id, outcome, merged_into)} already recorded."""
    if not _columns(conn, "adopted_rows"):
        return {}
    return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT source_id, new_id, outcome, merged_into FROM adopted_rows "
        "WHERE source = ? AND source_table = ?", (source_key, table))}


# ── per-row policy, shared by the dry run and the real run ─────────────────

def _clean(text):
    from core.privacy import clean_for_storage
    return clean_for_storage(str(text or ""))


def _tags_list(raw):
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        # why: a malformed tags cell carries no tags; the row itself is kept
        return []
    return [t for t in val if isinstance(t, str)] if isinstance(val, list) else []


def _memory_fields(row):
    """The stray row as the project will store it, or None when it is empty."""
    content = _clean(row.get("content"))
    if not content.strip():
        return None
    category = row.get("category")
    if category not in CATEGORIES:
        category = "note"
    try:
        importance = max(1, min(5, int(row.get("importance") or 2)))
    except (TypeError, ValueError):
        # why: the schema default, as the writer applies to a missing value
        importance = 2
    return {
        "content": content,
        "hash": MemoryDB.compute_content_hash(content),
        "category": category,
        "importance": importance,
        "tags": _tags_list(row.get("tags")),
        "topic": row.get("topic"),
        "active": 1 if row.get("is_active", 1) else 0,
        "created_at": row.get("created_at") or _now(),
        "updated_at": row.get("updated_at") or row.get("created_at") or _now(),
        "last_referenced_at": row.get("last_referenced_at"),
        "recall_count": int(row.get("recall_count") or 0),
    }


def _observation_plan(rows, watermark, project_obs):
    """{source id: skip reason} for every row NOT imported — retention, once."""
    skip = {}
    fresh = []
    for r in rows:
        if int(r.get("id") or 0) <= watermark:
            skip[r["id"]] = SKIP_FED
        else:
            fresh.append(r)
    room = max(0, MemoryDB._MAX_OBSERVATIONS - int(project_obs or 0))
    fresh.sort(key=lambda r: int(r["id"]))
    for r in fresh[:max(0, len(fresh) - room)]:
        skip[r["id"]] = SKIP_CAP
    return skip


def _new_report(loc, data):
    return {
        "source": loc["source_path"],
        "owner": str(loc["owner"]),
        "tables": {t: {"source": len(data.get(t) or []), "already": 0,
                       "outcomes": Counter()} for t in TABLE_ORDER},
        "not_merged": dict(data.get("_not_merged") or {}),
    }


def _find_pid(conn, project_root):
    """The project's row id in a READ-ONLY handle — canonical path match."""
    if not _columns(conn, "projects"):
        return None
    want = canonical_path(project_root)
    for r in conn.execute("SELECT id, path FROM projects"):
        if canonical_path(r[1]) == want:
            return r[0]
    return None


def dry_run(project_db, project_root, loc, data):
    """The plan against the live project database. Writes NOTHING.

    Read through `_open_readonly`, never `MemoryDB`: constructing one would
    run the schema bootstrap (and create `adopted_rows`) on the user's live
    database for a command that promised to write nothing.
    """
    report = _new_report(loc, data)
    report["dry_run"] = True
    conn = _open_readonly(project_db) if Path(project_db).is_file() else None
    try:
        pid = _find_pid(conn, project_root) if conn is not None else None
        report["before"] = project_counts(conn, pid)
        key = loc["source_key"]
        done = {t: (_done(conn, key, t) if conn is not None else {})
                for t in TABLE_ORDER}

        def existing(sql):
            if conn is None or pid is None:
                return set()
            return {r[0] for r in conn.execute(sql, (pid,))}

        for t in TABLE_ORDER:
            report["tables"][t]["already"] = len(done[t])
        tally = {t: report["tables"][t]["outcomes"] for t in TABLE_ORDER}
        new_sessions = {r["id"] for r in data["sessions"]}
        for r in data["sessions"]:
            if r["id"] not in done["sessions"]:
                tally["sessions"][IMPORTED] += 1
        for r in data["session_summaries"]:
            if r["id"] in done["session_summaries"]:
                continue
            ok = r.get("session_id") in new_sessions
            tally["session_summaries"][IMPORTED if ok else SKIP_NO_SESSION] += 1
        hashes = existing("SELECT content_hash FROM memories "
                          "WHERE project_id = ? AND is_active = 1")
        for r in data["memories"]:
            if r["id"] in done["memories"]:
                continue
            f = _memory_fields(r)
            if f is None:
                tally["memories"][SKIP_EMPTY] += 1
            elif f["active"] and f["hash"] in hashes:
                tally["memories"][MERGED_DUPLICATE] += 1
            else:
                if f["active"]:
                    hashes.add(f["hash"])
                tally["memories"][IMPORTED] += 1
        names = existing("SELECT name FROM topics WHERE project_id = ?")
        for r in data["topics"]:
            if r["id"] not in done["topics"]:
                tally["topics"][MERGED_BY_NAME if r.get("name") in names
                                else IMPORTED] += 1
        words = existing("SELECT keyword FROM keywords WHERE project_id = ?")
        for r in data["keywords"]:
            if r["id"] not in done["keywords"]:
                tally["keywords"][SUMMED if r.get("keyword") in words
                                  else IMPORTED] += 1
        slugs = existing("SELECT slug FROM directives WHERE project_id = ?")
        for r in data["directives"]:
            if r["id"] not in done["directives"]:
                tally["directives"][KEPT_PROJECT if r.get("slug") in slugs
                                    else IMPORTED] += 1
        todo = [r for r in data["observations"]
                if r["id"] not in done["observations"]]
        skip = _observation_plan(todo, data["_watermark"],
                                 report["before"]["observations"])
        for r in todo:
            tally["observations"][skip.get(r["id"], IMPORTED)] += 1
    finally:
        if conn is not None:
            conn.close()
    return report


# ── the move ────────────────────────────────────────────────────────────────

def _ensure_adopted_root(memory_dir):
    base = adopted_root(memory_dir)
    base.mkdir(parents=True, exist_ok=True)
    gi = base / ".gitignore"
    if not gi.exists():
        # Everything below is a moved database and its session transcripts;
        # none of it is content. `*` ignores this file too, by design.
        gi.write_text("# cc-memory: adopted state, not content\n*\n",
                      encoding="utf-8")
    return base


def _target_dir(memory_dir, project_root, owner, today):
    base = _ensure_adopted_root(memory_dir) / today
    base.mkdir(parents=True, exist_ok=True)
    slug = _slug(owner, project_root) or "stray"
    target = base / slug
    n = 2
    while target.exists():
        target = base / f"{slug}-{n}"
        n += 1
    return target


def _probe_idle(db_path):
    """Raise AdoptPending when another process holds the stray's write lock."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
    except sqlite3.Error as exc:
        raise AdoptPending(f"{db_path} cannot be opened right now ({exc})")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except sqlite3.OperationalError as exc:
        raise AdoptPending(
            f"{db_path} is mid-write in another process ({exc}); retry when "
            f"no session is running in {Path(db_path).parent.parent}")
    finally:
        conn.close()


def move_stray(loc, project_root, memory_dir, today=None):
    """Move the stray's state directory under `.adopted/`; returns the new `loc`.

    The manifest is written INTO the state directory first and travels with
    it, so there is no moment at which the moved files exist without the
    record that names their source. A refused rename leaves everything where
    it was (the manifest is withdrawn) and raises `AdoptPending`.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    _probe_idle(loc["db"])
    target = _target_dir(memory_dir, project_root, loc["owner"], today)
    state_dir = Path(loc["state_dir"])
    manifest = state_dir / MANIFEST_NAME
    record = {"source_key": loc["source_key"],
              "source_path": loc["source_path"],
              "owner": str(loc["owner"]),
              "project_root": str(project_root),
              "moved_at": _now(), "moved_to": str(target),
              "status": "moved"}
    manifest.write_text(json.dumps(record, indent=1, ensure_ascii=True),
                        encoding="utf-8")
    try:
        os.rename(str(state_dir), str(target))
    except OSError as exc:
        try:
            manifest.unlink()
        except OSError:
            pass  # why: a manifest left in the un-moved dir is inert (only .adopted/ is scanned); the refusal below must not be masked
        raise AdoptPending(
            f"could not move {state_dir} to {target} ({exc}). On Windows a "
            f"process with a file open inside it — a Claude Code session, the "
            f"dashboard, an MCP server — blocks the move; close it and retry")
    return {**loc, "db": target / DB_FILENAME, "state_dir": target,
            "manifest": target / MANIFEST_NAME}


def _archive_remapper(original_state_dir, moved_state_dir, memory_dir):
    """`sessions.archive_path` of the stray -> the same file, after the move.

    PreCompact stores the path RELATIVE to the state directory it wrote into;
    the project's readers resolve it against the PROJECT's state directory, so
    the moved file is spelled relative to that. An absolute path inside the
    stray's old state directory is re-pointed the same way; any other absolute
    path is kept as it was.
    """
    try:
        rel_dir = Path(moved_state_dir).resolve().relative_to(
            Path(memory_dir).resolve())
    except ValueError:
        # why: a moved directory outside the project's state dir cannot be
        # spelled relative to it; the absolute form is the honest fallback
        rel_dir = Path(moved_state_dir)
    orig = canonical_path(original_state_dir)

    def remap(value):
        if not value:
            return value
        p = Path(value)
        if p.is_absolute():
            c = canonical_path(p)
            if orig and c.startswith(orig + os.sep):
                return str(rel_dir / Path(os.path.relpath(c, orig)))
            return value
        return str(rel_dir / p)
    return remap


# ── the import ──────────────────────────────────────────────────────────────

def _record(conn, pid, key, table, source_id, new_id, outcome, merged_into):
    conn.execute(
        "INSERT INTO adopted_rows (project_id, source, source_table, "
        "source_id, new_id, outcome, merged_into, adopted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, key, table, source_id, new_id, outcome, merged_into, _now()))


def _import_rows(db, pid, key, table, rows, insert_one, tally):
    """Insert `rows` through `insert_one`, `_CHUNK` per transaction, once each.

    The already-recorded set is re-read INSIDE each transaction: a second run
    started beside this one cannot import a row twice, because the write lock
    is taken before the read.
    """
    for i in range(0, len(rows), _CHUNK):
        chunk = rows[i:i + _CHUNK]
        with db._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ph = ",".join("?" * len(chunk))
            seen = {r[0] for r in conn.execute(
                f"SELECT source_id FROM adopted_rows WHERE source = ? "
                f"AND source_table = ? AND source_id IN ({ph})",
                [key, table] + [r["id"] for r in chunk])}
            for row in chunk:
                if row["id"] in seen:
                    continue
                new_id, outcome, merged = insert_one(conn, row)
                _record(conn, pid, key, table, row["id"], new_id, outcome,
                        merged)
                tally[outcome] += 1


def _chain_contains(conn, start_id, needle, limit=1000):
    """Is `needle` on the supersede chain walked back from `start_id`?"""
    cur, seen = start_id, set()
    while cur is not None and cur not in seen and len(seen) < limit:
        if cur == needle:
            return True
        seen.add(cur)
        row = conn.execute("SELECT supersedes_id FROM memories WHERE id = ?",
                           (cur,)).fetchone()
        cur = row[0] if row else None
    return False


def _link_memories(db, key, rows):
    """Re-map `supersedes_id` through the id map; returns links written.

    A second pass, because a link may point FORWARD (consolidation links a
    loser to its later survivor). `AND supersedes_id IS NULL` makes the pass
    idempotent and keeps any link already present — the COALESCE rule of
    `MemoryDB.archive_obsolete`. A folded duplicate with no lineage of its own
    is linked to the project row it duplicates. A link that would close a
    cycle is not written.
    """
    with db._connect() as conn:
        done = _done(conn, key, "memories")
    src_link = {r["id"]: r.get("supersedes_id") for r in rows}
    pairs = []
    for src, (new, _outcome, merged) in done.items():
        if new is None:
            continue
        target = None
        s = src_link.get(src)
        if s is not None and s in done and done[s][0] is not None:
            target = done[s][0]
        if target is None and merged is not None:
            target = merged
        if target is not None and target != new:
            pairs.append((target, new))
    written = 0
    for i in range(0, len(pairs), _CHUNK):
        with db._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for target, new in pairs[i:i + _CHUNK]:
                if _chain_contains(conn, target, new):
                    continue
                written += conn.execute(
                    "UPDATE memories SET supersedes_id = ? "
                    "WHERE id = ? AND supersedes_id IS NULL",
                    (target, new)).rowcount
    return written


def _resolution(owner):
    """Where a session in the stray's directory resolves to NOW."""
    try:
        from core.roots import project_root as _project_root
        return str(_project_root(str(owner)))
    except Exception as exc:
        # why: a diagnostic line of the report; the import already committed
        return f"unresolved ({exc})"


def adopt(db, pid, project_root, memory_dir, loc, today=None):
    """Move the stray (unless an earlier run did), import it, return the report."""
    report = {"dry_run": False}
    with db._connect() as conn:
        before = project_counts(conn, pid)
    if loc["manifest"] is None:
        original_state_dir = loc["state_dir"]
        loc = move_stray(loc, project_root, memory_dir, today=today)
        resumed = False
    else:
        prior = json.loads(Path(loc["manifest"]).read_text(encoding="utf-8"))
        original_state_dir = Path(prior.get("source_path", "")).parent
        resumed = True
    data = read_source(loc["db"])
    report.update(_new_report(loc, data))
    report.update({"before": before, "resumed": resumed,
                   "moved_to": str(loc["state_dir"])})
    key = loc["source_key"]
    tally = {t: report["tables"][t]["outcomes"] for t in TABLE_ORDER}
    with db._connect() as conn:
        for t in TABLE_ORDER:
            report["tables"][t]["already"] = len(_done(conn, key, t))

    def pending(table):
        with db._connect() as conn:
            done = _done(conn, key, table)
        return [r for r in data[table] if r["id"] not in done]

    remap = _archive_remapper(original_state_dir, loc["state_dir"], memory_dir)

    def ins_session(conn, r):
        complete = r.get("complete")
        cur = conn.execute(
            "INSERT INTO sessions (project_id, claude_session_id, trigger_type, "
            "compacted_at, msg_count, archive_path, brief_summary, complete) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, r.get("claude_session_id"), r.get("trigger_type") or "auto",
             r.get("compacted_at") or _now(), int(r.get("msg_count") or 0),
             remap(r.get("archive_path")), r.get("brief_summary"),
             1 if complete is None else int(complete)))
        return cur.lastrowid, IMPORTED, None
    _import_rows(db, pid, key, "sessions", pending("sessions"), ins_session,
                 tally["sessions"])
    with db._connect() as conn:
        smap = {s: n for s, (n, _o, _m) in _done(conn, key, "sessions").items()
                if n is not None}

    def ins_summary(conn, r):
        sid = smap.get(r.get("session_id"))
        if sid is None:
            return None, SKIP_NO_SESSION, None
        cur = conn.execute(
            "INSERT INTO session_summaries (session_id, project_id, request, "
            "investigated, learned, completed, next_steps, notes, files_read, "
            "files_modified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, pid, r.get("request"), r.get("investigated"),
             r.get("learned"), r.get("completed"), r.get("next_steps"),
             r.get("notes"), r.get("files_read") or "[]",
             r.get("files_modified") or "[]", r.get("created_at") or _now()))
        return cur.lastrowid, IMPORTED, None
    _import_rows(db, pid, key, "session_summaries",
                 pending("session_summaries"), ins_summary,
                 tally["session_summaries"])

    from llm.memory_writer import _merged_tags

    def ins_memory(conn, r):
        f = _memory_fields(r)
        if f is None:
            return None, SKIP_EMPTY, None
        active, merged = f["active"], None
        if active:
            dup = conn.execute(
                "SELECT id, importance, tags FROM memories WHERE project_id = ? "
                "AND content_hash = ? AND is_active = 1 LIMIT 1",
                (pid, f["hash"])).fetchone()
            if dup is not None:
                # REINFORCE, as `upsert_smart` treats an exact-hash
                # restatement: importance max, tags unioned, content untouched.
                active, merged = 0, dup["id"]
                old_tags = _tags_list(dup["tags"])
                new_tags = _merged_tags(old_tags, f["tags"])
                new_imp = max(int(dup["importance"]), f["importance"])
                if new_tags != old_tags or new_imp != int(dup["importance"]):
                    conn.execute(
                        "UPDATE memories SET importance = ?, tags = ?, "
                        "updated_at = ? WHERE id = ?",
                        (new_imp, json.dumps(new_tags, ensure_ascii=False),
                         _now(), dup["id"]))
        cur = conn.execute(
            "INSERT INTO memories (project_id, session_id, category, content, "
            "importance, tags, topic, content_hash, supersedes_id, created_at, "
            "updated_at, is_active, last_referenced_at, recall_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (pid, smap.get(r.get("session_id")), f["category"], f["content"],
             f["importance"], json.dumps(f["tags"], ensure_ascii=False),
             f["topic"], f["hash"], f["created_at"], f["updated_at"], active,
             f["last_referenced_at"], f["recall_count"]))
        return cur.lastrowid, (MERGED_DUPLICATE if merged else IMPORTED), merged
    _import_rows(db, pid, key, "memories", pending("memories"), ins_memory,
                 tally["memories"])
    report["links"] = _link_memories(db, key, data["memories"])

    def ins_topic(conn, r):
        row = conn.execute(
            "SELECT id, version FROM topics WHERE project_id = ? AND name = ?",
            (pid, r.get("name"))).fetchone()
        version = int(r.get("version") or 1)
        if row is not None:
            if version > int(row["version"]):
                conn.execute("UPDATE topics SET version = ? WHERE id = ?",
                             (version, row["id"]))
            return row["id"], MERGED_BY_NAME, row["id"]
        cur = conn.execute(
            "INSERT INTO topics (project_id, name, content, updated_at, version) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, r.get("name"), _clean(r.get("content")),
             r.get("updated_at") or _now(), version))
        return cur.lastrowid, IMPORTED, None
    _import_rows(db, pid, key, "topics", pending("topics"), ins_topic,
                 tally["topics"])

    def ins_keyword(conn, r):
        row = conn.execute(
            "SELECT id FROM keywords WHERE project_id = ? AND keyword = ?",
            (pid, r.get("keyword"))).fetchone()
        seen = r.get("last_seen") or _now()
        freq = int(r.get("frequency") or 1)
        if row is not None:
            conn.execute(
                "UPDATE keywords SET frequency = frequency + ?, "
                "last_seen = MAX(last_seen, ?) WHERE id = ?",
                (freq, seen, row["id"]))
            return row["id"], SUMMED, row["id"]
        cur = conn.execute(
            "INSERT INTO keywords (project_id, keyword, frequency, last_seen) "
            "VALUES (?, ?, ?, ?)", (pid, r.get("keyword"), freq, seen))
        return cur.lastrowid, IMPORTED, None
    _import_rows(db, pid, key, "keywords", pending("keywords"), ins_keyword,
                 tally["keywords"])

    def ins_directive(conn, r):
        row = conn.execute(
            "SELECT id FROM directives WHERE project_id = ? AND slug = ?",
            (pid, r.get("slug"))).fetchone()
        if row is not None:
            return row["id"], KEPT_PROJECT, row["id"]
        now = _now()
        cur = conn.execute(
            "INSERT INTO directives (project_id, slug, quote, demand, kind, "
            "status, times_stated, source, evidence, first_seen_at, "
            "last_seen_at, closed_at, created_at, updated_at, turns_at_touch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, r.get("slug"), _clean(r.get("quote")),
             _clean(r.get("demand")), r.get("kind") or "standing",
             r.get("status") or "active", int(r.get("times_stated") or 1),
             r.get("source") or "user", _clean(r.get("evidence")),
             r.get("first_seen_at") or now, r.get("last_seen_at") or now,
             r.get("closed_at") or "", r.get("created_at") or now,
             r.get("updated_at") or now,
             MemoryDB._project_turns_total(conn, pid)))
        return cur.lastrowid, IMPORTED, None
    _import_rows(db, pid, key, "directives", pending("directives"),
                 ins_directive, tally["directives"])

    obs_todo = pending("observations")
    with db._connect() as conn:
        n_obs = project_counts(conn, pid)["observations"]
    skip = _observation_plan(obs_todo, data["_watermark"], n_obs)

    def ins_observation(conn, r):
        if r["id"] in skip:
            return None, skip[r["id"]], None
        cur = conn.execute(
            "INSERT INTO observations (project_id, session_id, tool_name, "
            "tool_input, tool_output, timestamp, is_private) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, r.get("session_id"), r.get("tool_name") or "?",
             r.get("tool_input"), r.get("tool_output"),
             r.get("timestamp") or _now(), int(r.get("is_private") or 0)))
        return cur.lastrowid, IMPORTED, None
    _import_rows(db, pid, key, "observations", obs_todo, ins_observation,
                 tally["observations"])

    with db._connect() as conn:
        report["after"] = project_counts(conn, pid)
    manifest = Path(loc["manifest"])
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # why: rewritten from what this run knows; the manifest is a record
        record = {"source_key": key, "source_path": loc["source_path"]}
    record.update({"status": "adopted", "adopted_at": _now(),
                   "outcomes": {t: dict(tally[t]) for t in TABLE_ORDER}})
    manifest.write_text(json.dumps(record, indent=1, ensure_ascii=True),
                        encoding="utf-8")
    try:
        from llm.memory_writer import regenerate_memory_index
        regenerate_memory_index(db, pid, Path(memory_dir))
        report["memory_md"] = "regenerated"
    except Exception as exc:
        # why: MEMORY.md is a projection of rows that are already committed;
        # the next write refreshes it, and the import must not be reported as
        # failed over a derived file
        report["memory_md"] = f"not regenerated ({exc})"
    report["resolves_to"] = _resolution(loc["owner"])
    return report


def format_report(report, project_root):
    """The lines `/cc-mem adopt-stray` prints, for both modes."""
    verb = "would" if report.get("dry_run") else "did"
    lines = [f"adopt-stray: {report['source']}",
             f"  stray directory : {report['owner']}",
             f"  project         : {project_root}"]
    if report.get("moved_to"):
        lines.append(f"  moved to        : {report['moved_to']}"
                     + ("  (resumed: moved by an earlier run)"
                        if report.get("resumed") else ""))
    lines.append(f"\n  per table ({verb} import / fold / skip; 'already' = "
                 f"recorded by an earlier run):")
    for t in TABLE_ORDER:
        info = report["tables"][t]
        parts = ", ".join(f"{k} {v}" for k, v in sorted(info["outcomes"].items()))
        lines.append(f"    {t:<18} source {info['source']:>6}  already "
                     f"{info['already']:>6}  {parts or 'nothing new'}")
    if report.get("links") is not None:
        lines.append(f"    supersede links re-mapped: {report['links']}")
    if report.get("not_merged"):
        lines.append("  not merged (the project keeps its own; readable in "
                     "the moved database): "
                     + ", ".join(f"{k} {v}" for k, v in
                                 sorted(report["not_merged"].items())))
    before, after = report.get("before") or {}, report.get("after") or {}
    lines.append("\n  project counts " + ("before -> after:" if after
                                         else "(now):"))
    for label, n in before.items():
        lines.append(f"    {label:<20} {n}"
                     + (f" -> {after.get(label)}" if after else ""))
    if report.get("memory_md"):
        lines.append(f"  MEMORY.md: {report['memory_md']}")
    if report.get("resolves_to"):
        lines.append(f"  a session in {report['owner']} now resolves to "
                     f"{report['resolves_to']}")
    if report.get("dry_run"):
        lines.append("\n  dry run: nothing was written. Re-run with --yes to "
                     "move the stray under .adopted/ and import it.")
    return lines
