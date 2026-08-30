r"""Where a project's cc-memory state lives — the name, and the one-way move to it.

RAW docstring for the same reason `core/roots.py` carries one: this module
spells Windows paths, and a plain docstring turns `D:\Projects` into the
invalid escape `\P`. Python answers that with a SyntaxWarning on **stderr**,
and Claude Code renders any hook stderr as an error.

WHY THIS EXISTS
---------------
Through v2.12.2 the state directory was the bare literal ``"memory"``, written
out at every use site: both CLIs, the MCP server, the dashboard, the web
viewer, the installer, the consolidation worker and all six hooks
<!--ce:hooks--> each spelled ``Path(cwd) / "memory"`` for itself. That is the
same shape `core/roots.py` was written to end for the project ROOT — a name
living at N call sites has N chances to disagree and no single place to
change. Two things follow, and they are the whole module:

  * the NAME is defined once, here (`MEMORY_DIRNAME`), and
  * "which directory holds this project's memory" is a FUNCTION
    (`memory_dir`), not a join — because for as long as pre-v2.13.0 installs
    exist, the answer depends on what is already on disk.

WHY `.ccm` AND NOT `memory`
---------------------------
``memory`` is an undotted, generic, entirely plausible source directory name.
It sat at the project root beside the user's own code, sorted into the middle
of their file listing, and collided with anything a project already called
``memory`` — a Python package, a Rust module, an assets folder. cc-memory
writes a `.gitignore` INTO that directory precisely because, by name alone, it
is indistinguishable from content.

``.ccm`` is dotted state, beside `.git`, `.venv` and `.claude`, and it matches
the marker this plugin already owns: `core/roots.PIN_MARKER` is ``.ccm-root``.
The two cannot collide — the pin is a FILE named ``.ccm-root``, the state is a
DIRECTORY named ``.ccm`` — so a pinned project now carries two things spelled
the same way on purpose.

A RENAME IS NOT THE MERGE `core/roots.py` REFUSES
-------------------------------------------------
`core/roots.py` says PREVENTION, NOT MIGRATION in capitals, and that is about
a DIFFERENT operation. What it refuses is ADOPTING a stray database: two
`memory.db` files whose `projects` rows each name their own directory are
byte-for-byte indistinguishable from a deliberate nested sub-project, so
picking one destroys data and the pick must be an explicit user command.

Renaming ``memory/`` to ``.ccm/`` merges nothing. One directory, one
`os.rename`, no second database to choose between, contents untouched — and
the generated `.gitignore` keeps working unchanged, because every line in it
(`memory.db`, `sessions/`, `.consolidation.lock`, ...) names an entry INSIDE
the directory and none of them names the directory itself. That asymmetry is
why this migration is automatic and the other one is not.

FAIL-SAFE DIRECTION — the load-bearing decision
-----------------------------------------------
When the move cannot happen — the database is open in another process, the
tree is read-only, a racing hook got there first — `memory_dir` returns the
LEGACY directory, never the new name. Returning the new name would have the
caller create a fresh, empty ``.ccm/`` beside a ``memory/`` holding every
memory the user has, and the project would come up looking brand new while its
history sat one directory away. Degrading to "keep using the old directory"
costs one retry per turn and loses nothing.

IDENTIFICATION, NOT NAME-MATCHING
---------------------------------
``memory`` is a name real projects use for real content. Moving a directory
because of its name alone would corrupt somebody's source tree, so the legacy
directory is migrated only when it is POSITIVELY identified as one cc-memory
wrote: either it carries the `.gitignore` this plugin generates (first line a
literal marker) or it holds a `memory.db` that is a real SQLite file carrying
this schema's tables. A directory named ``memory`` that is neither is left
exactly where it is, and the project gets a fresh ``.ccm/`` beside it.

COST
----
`memory_dir` runs wherever the old join ran — every hook, every CLI command,
every MCP call. The settled case is ONE stat: ``.ccm`` is a directory, return
it. A project with neither directory costs that stat plus the identification
probe's own first stat, and creates nothing. Only an unmigrated project pays
for identification, and only until the rename succeeds.
"""
import os
from pathlib import Path

# Junction-aware link probe — the same shared implementation `core/roots.py`
# and `core/progress.py` use. `markers.py` is pure stdlib and imports nothing
# back into core, so this stays a leaf-ward edge: markers <- layout <- roots.
from core.markers import _is_link as _markers_is_link

# THE name. Everything else in this package asks for it rather than spelling
# it, which is the entire point of the module.
MEMORY_DIRNAME = ".ccm"

# The pre-v2.13.0 name, kept for identification and migration only. Nothing
# else may join this to build a path: `migrate_legacy_dir` is the only
# function allowed to return it, and only when the rename could not happen.
LEGACY_MEMORY_DIRNAME = "memory"

# The database file, unchanged by the rename — `.ccm/memory.db`. The state
# DIRECTORY was renamed; the artifacts inside it were not, so a user's
# `.gitignore`, their backups, and any path they have bookmarked below the
# directory all keep resolving.
DB_FILENAME = "memory.db"

# First line of the `.gitignore` `core/progress.py` generates into the state
# directory, and this module's cheapest positive identification. Defined HERE
# and imported there, not the other way round: `progress.py` pulls in
# `core/db.py` and the whole atomic-write stack, and `roots.py` — which is on
# every hook's path — must not pay for that just to learn a directory name.
CCM_GITIGNORE_MARKER = "# cc-memory: generated state, not content"

# Tables this schema always has. Any ONE of them could plausibly exist in an
# unrelated SQLite file; all three together, in a file named `memory.db`,
# inside a directory named `memory`, is this plugin's database. Consulted only
# when the `.gitignore` marker is absent (the user deleted it, or the install
# predates it).
_IDENTIFYING_TABLES = ("memories", "projects", "sessions")

# How much of the `.gitignore` to read before giving up on the marker. The
# marker is the FIRST line of every file this plugin writes; the slack is for
# a user who prepended their own comment.
_GITIGNORE_PROBE_BYTES = 4096

# SQLite's file magic. Cheap pre-filter so an unrelated `memory.db` — a JSON
# dump, a lock file, a text note — never reaches `sqlite3.connect`.
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _safe_is_dir(path):
    """`path.is_dir()` that cannot raise.

    Same contract as `core/roots._exists`, and the same reasons: permission
    denied on a parent, a stale handle, ELOOP, an embedded NUL, an unreachable
    UNC share. Any of those must cost this ONE probe, never the caller.
    """
    try:
        return path.is_dir()
    except Exception:
        return False


def _safe_is_file(path):
    """`path.is_file()` that cannot raise. See `_safe_is_dir`."""
    try:
        return path.is_file()
    except Exception:
        return False


def _safe_link(path):
    """`_markers_is_link` that cannot raise; an unprobeable path counts as one.

    Fail CLOSED, matching `core/roots._has_db` and
    `core/progress.ensure_memory_dir`: a path that cannot even be lstat'd is
    exactly the one that must not be renamed, nor identified as ours.
    """
    try:
        return _markers_is_link(path)
    except Exception:
        return True


def _safe_path(value):
    """`Path(value)` that cannot itself raise — the fallback's fallback.

    The twin of `core/roots._safe_path`, and it exists because this module
    reproduced the exact defect that one documents: `memory_dir`'s handler
    caught the TypeError from a non-path `project_root` and then re-raised it
    by calling `Path(project_root)` again on the way out. Measured before this
    function existed: `memory_dir(123)` and `memory_dir([1, 2])` both escaped
    a function whose docstring promises it never raises. A hook payload
    carrying `{"cwd": 123}` is the shape that produces it.

    `Path(".")` is the same degradation `core/roots.py` chose: every caller's
    existence check then fails and the hook exits 0 quietly.
    """
    try:
        return Path(value)
    except Exception:
        # why: `value` was not path-like at all (a number, a list). Returning
        # the current directory keeps the never-raises contract true.
        return Path(".")


def _log(log, level, message):
    """Emit a diagnostic, or do nothing. Never raises.

    `log` is a `core.logger` instance or None. A broken logger must not be
    what breaks the hook it is describing — the rule `hooks/_entry.py` states
    for the shared entry ladder.
    """
    if log is None:
        return
    try:
        getattr(log, level)(message)
    except Exception:
        # why: a diagnostic is never load-bearing; see the docstring.
        pass


def state_dir_candidates(project_root):
    """`(.ccm, memory)` under `project_root` — new name first, both spellings.

    The read-only half of this module, and the one `core/roots.py` uses. A
    resolver must recognise an UNMIGRATED project as a project: rung 0 and
    rung 1 both ask "does this directory own a database", and a project whose
    rename has not happened yet (or could not) still owns one, under the old
    name. Returning both names in preference order lets that predicate stay a
    pure probe — it never migrates, never writes, and never needs to.

    Coerces through `_safe_path`, so a non-path `project_root` yields a pair
    of harmless relative candidates instead of a TypeError out of the one
    function every hook calls.
    """
    root = _safe_path(project_root)
    return (root / MEMORY_DIRNAME, root / LEGACY_MEMORY_DIRNAME)


def find_memory_dir(project_root):
    """The state directory as it already is ON DISK. Never migrates, never writes.

    The read-side twin of `memory_dir`, and the split is deliberate: migration
    is a WRITE, and a surface that is merely LOOKING must not perform one.
    `ui/dashboard.py` enumerates every sibling of a project to populate its
    picker and `cli/mem.py status` counts memories across a whole projects
    folder — routing those through `memory_dir` would rename the state
    directory of every project on the machine because the user opened a list.
    Migration belongs to the surfaces that are about to write: the hooks, the
    CLI commands that act on one named project, the installer's init.

    Cheaper than `memory_dir` on purpose — two stats, no identification. A
    read does not need to PROVE the directory is ours before reading the
    database inside it; it needs to find the database that is there. Returns
    the new name when neither exists, so callers can `.exists()` the result.
    """
    new, old = state_dir_candidates(project_root)
    if _safe_is_dir(new):
        return new
    if _safe_is_file(old / DB_FILENAME):
        return old
    return new


def find_db_path(project_root):
    """`memory.db` under `find_memory_dir` — read-side, no migration."""
    return find_memory_dir(project_root) / DB_FILENAME


def _has_ccm_gitignore(directory):
    """True when `directory/.gitignore` carries this plugin's marker line."""
    gi = directory / ".gitignore"
    if _safe_link(gi) or not _safe_is_file(gi):
        return False
    try:
        with open(gi, "rb") as fh:
            head = fh.read(_GITIGNORE_PROBE_BYTES)
    except (OSError, ValueError):
        # why: an unreadable courtesy file proves nothing either way, and the
        # database probe is the other half of the identification.
        return False
    return CCM_GITIGNORE_MARKER.encode("utf-8") in head


def _has_ccm_database(path):
    """True when `path` is a SQLite file carrying THIS schema's tables.

    Three gates, cheapest first: a real file (never a link — the same
    fail-closed identity rule `core/roots._has_db` applies), SQLite's magic
    bytes, then the tables. The magic-byte pre-filter is not an optimisation
    only: without it an unrelated or absent `memory.db` reaches
    `sqlite3.connect`, which CREATES the file it was asked about and leaves
    journal files beside it — a probe that manufactures its own evidence.
    """
    if _safe_link(path) or not _safe_is_file(path):
        return False
    try:
        with open(path, "rb") as fh:
            if fh.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
                return False
    except (OSError, ValueError):
        # why: unreadable is not identified, and leaving the directory alone
        # is the safe direction for a probe whose YES triggers a rename.
        return False
    # Lazy imports: `core/roots.py` imports this module and is on every hook's
    # path, so the settled case must not pay to parse `core/db.py` or
    # `sqlite3`. Only an unmigrated project whose marker file is gone gets
    # this far.
    import sqlite3
    from core.db import _readonly_uri
    try:
        uri = _readonly_uri(Path(path).resolve().as_posix())
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            conn.close()
    except Exception:
        # why: a locked, corrupt or unopenable database is not identified.
        # `mode=ro` also guarantees this probe can never create the file it is
        # asking about, on any of the three path shapes `_readonly_uri` covers.
        return False
    names = {row[0] for row in rows}
    return all(table in names for table in _IDENTIFYING_TABLES)


def is_ccm_dir(directory):
    """True when `directory` is a state directory cc-memory itself wrote.

    Positive identification, never name-matching — see the module docstring.
    A link is refused outright: `core/progress.ensure_memory_dir` already
    refuses to WRITE through a linked state directory, and renaming one would
    move the link rather than the data it points at.
    """
    directory = _safe_path(directory)
    if _safe_link(directory) or not _safe_is_dir(directory):
        return False
    return (_has_ccm_gitignore(directory)
            or _has_ccm_database(directory / DB_FILENAME))


def migrate_legacy_dir(project_root, log=None):
    """Rename `<root>/memory` to `<root>/.ccm`. Returns the directory to USE.

    The write half. Returns a `Path` the caller may create and write into, and
    NEVER raises — this runs inside hooks, where an exception surfaces as an
    error in the user's session.

    Four outcomes, in the order they are decided:

      1. ``.ccm`` already exists -> return it. The settled case, one stat.
         Decided FIRST and unconditionally: once the new directory exists it
         is the answer, whatever else is on disk. A leftover ``memory/`` beside
         it — a restored backup, a half-finished manual move, a directory the
         user genuinely owns — is not this function's business.
      2. No identifiable legacy directory -> return ``.ccm``. A fresh project,
         or one whose ``memory/`` belongs to somebody else. Nothing is created
         here; the caller's `ensure_memory_dir` does that.
      3. Rename succeeds -> return ``.ccm``.
      4. Rename fails -> return ``memory/``, the fail-safe direction. See the
         module docstring: handing back the new name would strand the user's
         database one directory away behind a brand-new empty one.
    """
    new, old = state_dir_candidates(project_root)
    if _safe_is_dir(new):
        return new
    if not is_ccm_dir(old):
        return new
    try:
        # os.rename, not os.replace: `replace` overwrites an existing target,
        # and the whole safety of this move is that it cannot. `new` was just
        # probed, so this is belt-and-braces against a racing process — which
        # is exactly what the two race branches below are for.
        os.rename(str(old), str(new))
        _log(log, "info",
             f"cc-memory state directory migrated: {old} -> {new}")
        return new
    except FileExistsError:
        # A concurrent process won the race between the probe and here; its
        # rename is as good as ours.
        return new if _safe_is_dir(new) else old
    except FileNotFoundError:
        # The same race seen from the other side: the source was moved out
        # from under us.
        return new if _safe_is_dir(new) else old
    except OSError as exc:
        # The realistic failure on the primary platform: Windows refuses to
        # rename a directory while a handle inside it is open, so a second
        # Claude Code session, the dashboard, or an MCP server holding
        # memory.db keeps the old name alive. Retried for free on the next
        # call; until then the legacy directory is the live one.
        _log(log, "warn",
             f"cc-memory state directory not migrated ({exc}); "
             f"continuing to use {old}")
        return old if _safe_is_dir(old) else new


def memory_dir(project_root, log=None):
    """THE state directory for `project_root` — `<root>/.ccm`, migrating once.

    The single answer every surface asks for, replacing the
    ``Path(cwd) / "memory"`` join each of them used to spell. Returns a `Path`
    that does not necessarily exist yet: creation stays with
    `core/progress.ensure_memory_dir`, the one writer that also makes
    `sessions/`, `topics/` and the `.gitignore`.

    Never raises, so it is safe everywhere the old join stood.
    """
    try:
        return migrate_legacy_dir(project_root, log=log)
    except Exception as exc:
        # why: this function stands where a bare path join used to, and a join
        # cannot fail. Degrading to the un-migrated new name is the same
        # outcome as a brand-new project, never a crash inside a hook.
        _log(log, "warn", f"state directory resolution failed ({exc}); "
                          f"using {MEMORY_DIRNAME} as given")
        # _safe_path, NOT Path: this handler re-raised the very TypeError it
        # catches until `_safe_path` existed — see that function's docstring.
        return _safe_path(project_root) / MEMORY_DIRNAME


def db_path(project_root, log=None):
    """`<root>/.ccm/memory.db`, through the same migration. Never raises."""
    return memory_dir(project_root, log=log) / DB_FILENAME
