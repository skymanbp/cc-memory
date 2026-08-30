#!/usr/bin/env python3
"""
MCP stdio JSON-RPC server.

Exposes cc-memory search and management tools via Model Context Protocol.

Protocol: JSON-RPC 2.0 over stdio (stdin/stdout).
stdout is RESERVED for JSON-RPC frames. All logging goes to file (core.logger).

Wire contract
-------------
* stdin AND stdout are forced to UTF-8 with LF-only newlines BEFORE the handles
  are captured. Without that the child inherits the OS locale codec (gbk /
  cp1252 on Windows): non-ASCII is silently mangled on the way IN, and on the
  way OUT a single un-encodable glyph replaces the ENTIRE result batch with an
  error. One emoji is enough — and so is the `U+21BB` supersede marker that
  cc-memory emits itself (see core/encoding_setup.py).
* Every message that PARSES and carries a non-null `id` gets exactly one
  response frame — including semantically malformed ones, and including a
  `notifications/*` method that arrived WITH an id. JSON-RPC 2.0 defines a
  Notification as a message without an id, so an id makes it a Request no
  matter what the method is called; a message with no id gets silence.
  Consuming an id without answering blocks a client that has no timeout,
  forever. (`"params": null` is legal — params is optional and many clients
  serialize omission as null.)
  The one unavoidable gap is a frame that does not parse or is refused for
  length: its id is unknowable, so the reply carries `"id": null` as JSON-RPC
  2.0 §5 requires. That is a deliberate trade — guessing an id out of unparsed
  bytes would let a mis-read answer the WRONG pending call.
* NOTHING escapes main(). An escaping exception prints a traceback on stderr,
  which is rendered as error UI, and exits rc=1 — orphaning every in-flight AND
  future id at once. `json.loads` raises well beyond JSONDecodeError: a
  4301-digit integer raises plain ValueError (CPython's int-conversion limit)
  and ~3000 levels of nesting raise RecursionError. Both were reachable through
  an advertised tool argument (`memory_search.limit`) BEFORE validation ran.
  Frames are also length-capped, so a multi-megabyte line is drained rather
  than buffered whole.
* Frames are strict RFC 8259 in both directions: `allow_nan=False` on the way
  out, and `NaN` / `Infinity` / out-of-range floats rejected with -32700 on the
  way in. json's own defaults are lenient both ways, so a client id of `NaN`
  used to be echoed verbatim into a frame that JS `JSON.parse`, Go and serde
  all refuse.
* `tools/call` arguments are validated against the advertised `inputSchema`
  (required / type / enum / bounds / lengths) and rejected with -32602 instead
  of being coerced, clamped, or ignored.

Privacy
-------
config.json's `excluded_projects` is enforced HERE as well, not only in the
six hooks <!--ce:hooks-->. This server ships ENABLED BY DEFAULT
(`.claude-plugin/plugin.json` `mcpServers`) on every marketplace /
dev-checkout install and is model-driven
exactly like a hook, so through v2.5.1 a project the user had opted out of
still answered memory_search / memory_get_details / memory_recent /
memory_topics / memory_stats / progress_get with its stored content, and still
accepted memory_add and progress_regenerate WRITES. config.json promises a
listed directory gets "no memory.db … no PROGRESS.md and no context injection";
three of those clauses were false whenever this server was loaded.

One gate, in `_get_db`, which all eight database-touching handlers reach.
`initialize`, `tools/list` and `ping` sit deliberately outside it: they touch no
project, and refusing the handshake would break the client for every project
rather than for the excluded one.

Tools:
  memory_search       FTS5 search (compact results)
  memory_get_details  Batch fetch full details by IDs (active rows only)
  memory_add          Add a memory via anti-patch upsert (memory_writer)
  memory_stats        Project statistics
  memory_topics       List topic summaries
  memory_recent       Recent memories with filters
  progress_get        Read PROGRESS.md state (forced-handoff support)
  progress_regenerate Force-rewrite memory/PROGRESS.md

Registration is manual (nothing reads config.json's inert `mcp.auto_register`).
The shipped `mcpServers` entry is `.claude-plugin/plugin.json`, which points at
`${CLAUDE_PLUGIN_ROOT}/cc_memory/mcp/server.py` (marketplace / dev-checkout
layout). A standalone install is FLAT — ui/installer.py copies this file to
`<install>/mcp/server.py` with no `cc_memory/` segment — so a hand-written
entry for that layout has to point there instead.
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# ── stdio encoding: MUST run before the handles are captured ───────────────
try:
    from core.encoding_setup import enable_utf8_io
    enable_utf8_io()
except Exception:
    # why: enable_utf8_io is best-effort belt (it also covers stderr); the
    # explicit reconfigure loop below is the load-bearing fix and has to run
    # even if that import fails on a partial install.
    pass

for _s in (sys.stdin, sys.stdout):
    _rc = getattr(_s, "reconfigure", None)
    if _rc is not None:
        try:
            _rc(encoding="utf-8", errors="replace", newline="\n")
        except (ValueError, OSError):
            pass  # why: stream detached / not a TextIOWrapper

_original_stdout = sys.stdout
_original_stdin = sys.stdin

from core.logger import get_logger
from core.db import CATEGORIES
_log = get_logger("mcp")


def _resolve_version() -> str:
    """Version string, resolvable from BOTH install layouts.

    `core.version` works under the flat standalone layout (TARGET_DIR/core/…)
    and under the repo/marketplace layout, because every entry point puts the
    package directory on sys.path. `cc_memory` is only importable when the
    wheel is installed. The text scan is the last resort for a pre-2.5 flat
    install that predates core/version.py.
    """
    try:
        from core.version import __version__ as v
        return v
    except ImportError:
        # why: pre-2.5 flat installs have no core/version.py — fall through
        pass
    try:
        from cc_memory import __version__ as v
        return v
    except ImportError:
        # why: the standalone installer lays the tree out FLAT, so there is no
        # importable `cc_memory` package — fall through to the text scan
        pass
    for cand in (_PKG_ROOT / "core" / "version.py", _PKG_ROOT / "__init__.py"):
        try:
            text = cand.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
        if m:
            return m.group(1)
    try:
        # Last resort: config.json carries the same literal and the standalone
        # installer always copies it into the package root. utf-8-sig, not
        # utf-8: PowerShell's Out-File writes a BOM by default on the primary
        # platform, and a BOM makes json.load raise — which used to drop this
        # server's advertised version to "unknown" on an otherwise healthy
        # install. Read this way inline rather than via core.modes.read_config
        # so server boot stays lazy (see _MIN_CONTENT_LEN's note).
        cfg = json.loads((_PKG_ROOT / "config.json").read_text(encoding="utf-8-sig"))
        if isinstance(cfg.get("version"), str):
            return cfg["version"]
    except (OSError, ValueError, AttributeError):
        # why: a missing or garbled config.json must never stop the server booting
        pass
    return "unknown"


_VERSION = _resolve_version()

# Protocol versions this server actually implements. `initialize` echoes the
# client's value when it is one of these, else answers with the default —
# real negotiation instead of parroting one constant at every input.
_PROTOCOL_DEFAULT = "2024-11-05"
_PROTOCOL_SUPPORTED = ("2024-11-05", "2025-06-18")

_CATEGORIES = list(CATEGORIES)  # single source: core.db (register M3)

# Devnull sink installed over sys.stdout in main(); module-level so it is never
# garbage-collected out from under a late writer.
_devnull = None


# Ceilings for the advertised numeric arguments. An argument with a `minimum`
# and no `maximum` is unbounded by contract: `limit` used to accept 1e6 and the
# handler passed it straight to SQL. _MAX_IDS keeps the memory_get_details
# `IN (...)` list under SQLITE_MAX_VARIABLE_NUMBER; _MAX_LIMIT stays well under
# core.db.MemoryDB._MAX_SEARCH_LIMIT (1000) so the schema is the binding
# constraint and the DB clamp is only a backstop.
_MAX_LIMIT = 200
_MAX_SESSIONS_BACK = 50
_MAX_IDS = 200

# Mirrors llm.memory_writer.MIN_CONTENT_LEN. NOT imported: the writer pulls in
# core.db + core.privacy, and this module keeps every handler import lazy so a
# partial install still boots far enough to answer tools/list. Advertising
# minLength 1 while the writer dropped anything under 10 meant memory_add
# could report a successful call for a write that never happened.
_MIN_CONTENT_LEN = 10


TOOLS = [
    {
        "name": "memory_search",
        "description": "Search project memories using full-text search. "
                       "Returns compact results — use memory_get_details for full content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1,
                          "description": "Search query (must be non-empty). "
                                         "Plain text is matched literally; fts5 "
                                         "operators (AND / OR / NEAR / \"phrase\") "
                                         "are honoured when the expression parses."},
                "project": {"type": "string", "minLength": 1,
                            "description": "Project path (default: cwd)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT,
                          "default": 20,
                          "description": f"Max results (default: 20, max: {_MAX_LIMIT})"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_get_details",
        "description": "Get full details of ACTIVE memories by IDs. Superseded / "
                       "archived rows are never returned; requested ids that did "
                       "not resolve come back in \"missing\".",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"},
                        "minItems": 1, "maxItems": _MAX_IDS},
                "project": {"type": "string", "minLength": 1},
            },
            "required": ["ids"],
        },
    },
    {
        "name": "memory_add",
        "description": "Add a memory via the anti-patch upsert path (merge / "
                       "supersede / insert depending on similarity to existing).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(_CATEGORIES)},
                "content": {"type": "string", "minLength": _MIN_CONTENT_LEN,
                            "description": f"Memory text, at least "
                                           f"{_MIN_CONTENT_LEN} characters after "
                                           f"trimming (shorter content is dropped "
                                           f"by the writer, not stored)"},
                "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                "topic": {"type": "string"},
                "project": {"type": "string", "minLength": 1},
            },
            "required": ["category", "content", "importance"],
        },
    },
    {
        "name": "memory_stats",
        "description": "Get project memory statistics.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "minLength": 1}},
        },
    },
    {
        "name": "memory_topics",
        "description": ("List topic summaries for the project, newest first. "
                        "Bounded: at most `limit` topics (default 50, max 200) "
                        "and 2000 characters of each body. The reply reports "
                        "`total` and `truncated` so you can tell a complete "
                        "list from a clipped one."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "memory_recent",
        "description": "Get recent memories with optional filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "minLength": 1},
                "sessions_back": {"type": "integer", "minimum": 1,
                                  "maximum": _MAX_SESSIONS_BACK, "default": 3},
                "min_importance": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
                "category": {"type": "string", "enum": list(_CATEGORIES)},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT,
                          "default": 20},
            },
        },
    },
    {
        "name": "progress_get",
        "description": "Read the current PROGRESS.md state (structured fields).",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "minLength": 1}},
        },
    },
    {
        "name": "progress_regenerate",
        "description": "Force-rewrite memory/PROGRESS.md from the SQL state.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "minLength": 1}},
        },
    },
]

_TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ── argument validation (the advertised schema is now enforced) ────────────

def _type_ok(value, jtype) -> bool:
    if jtype == "string":
        return isinstance(value, str)
    if jtype == "integer":
        # bool is a subclass of int — `true` is not an integer argument
        return isinstance(value, int) and not isinstance(value, bool)
    if jtype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if jtype == "array":
        return isinstance(value, list)
    if jtype == "object":
        return isinstance(value, dict)
    if jtype == "boolean":
        return isinstance(value, bool)
    return True


def _validate_tool_args(tool_name, args):
    """Enforce a tool's inputSchema. Returns an error string, or None if valid.

    Explicit JSON nulls count as "not supplied" — required keys still fail,
    optional ones are dropped by the caller so handler defaults apply.
    """
    tool = _TOOLS_BY_NAME.get(tool_name)
    if tool is None:
        return f"unknown tool: {tool_name}"
    if not isinstance(args, dict):
        return f"'arguments' must be an object, got {type(args).__name__}"
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})

    for key in schema.get("required", []):
        if args.get(key) is None:
            return f"missing required argument: '{key}'"

    for key, value in args.items():
        spec = props.get(key)
        if spec is None or value is None:
            continue  # unknown keys are ignored; nulls mean "absent"
        jtype = spec.get("type")
        if jtype and not _type_ok(value, jtype):
            return f"'{key}' must be of type {jtype}, got {type(value).__name__}"
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            return f"'{key}' must be one of: {', '.join(str(e) for e in enum)}"
        if jtype == "array":
            item_type = (spec.get("items") or {}).get("type")
            if item_type and not all(_type_ok(v, item_type) for v in value):
                return f"every item of '{key}' must be of type {item_type}"
            min_items = spec.get("minItems")
            if min_items is not None and len(value) < min_items:
                return f"'{key}' must contain at least {min_items} item(s)"
            max_items = spec.get("maxItems")
            if max_items is not None and len(value) > max_items:
                return (f"'{key}' must contain at most {max_items} item(s) "
                        f"(got {len(value)})")
        if jtype == "string":
            min_len = spec.get("minLength")
            if min_len is not None and len(value.strip()) < min_len:
                if min_len == 1:
                    return f"'{key}' must be a non-empty string"
                # why not the generic message: minLength is 10 on memory_add's
                # `content`, and "must be non-empty" for a 4-character string
                # is actively misleading about what would fix it.
                return (f"'{key}' must be at least {min_len} characters after "
                        f"trimming (got {len(value.strip())})")
        if jtype in ("integer", "number"):
            lo, hi = spec.get("minimum"), spec.get("maximum")
            if lo is not None and value < lo:
                return f"'{key}' must be >= {lo} (got {value})"
            if hi is not None and value > hi:
                return f"'{key}' must be <= {hi} (got {value})"
    return None


# ── db resolution ──────────────────────────────────────────────────────────

def _get_db(project_path=None):
    """Open the project DB. Returns (db, project_id, error_message).

    THE privacy choke point. All eight database-touching handlers reach the DB
    through here, so config.json's `excluded_projects` opt-out is applied once,
    in one place, via `core.modes.is_excluded` — the same single implementation
    all six hooks <!--ce:hooks--> call. Do not add a handler that opens a DB
    path itself; that is a privacy regression, not a style nit (see
    core/modes.py).

    The gate runs BEFORE the existence check, so it also covers an excluded
    project that has no DB yet, and it covers the `project`-omitted case
    (which resolves to this server's cwd — normally the project directory
    Claude Code launched it in).

    The refusal is an ERROR — the dispatcher turns a truthy "error" into
    `isError: true` — and not an empty success. `{"results": []}` would assert
    that the project HAS no memories, which is untrue and is an invitation to
    helpfully store one; and `{"action": "inserted"}` for a write that never
    happened is the exact lie `_is_failed_result` exists to prevent. The
    wording is also deliberately distinct from the missing-database message
    below: that one IS worth retrying after an init, and this one never is.

    `is_excluded` is imported lazily, beside MemoryDB, so a partial install
    still boots far enough to answer tools/list. That also fails CLOSED: if
    core/modes.py is unimportable the ImportError propagates to
    `_dispatch_tool_call`, which answers `isError` — rather than serving a
    project whose opt-out status could not be determined.
    """
    from core.db import MemoryDB
    # Lazy beside the other two, for the reason the docstring gives. Aliased
    # because this function binds a local named `db_path`.
    from core.layout import db_path as resolve_db_path
    from core.modes import is_excluded
    if project_path is None:
        project = os.getcwd()          # absent == "this server's cwd"
    elif isinstance(project_path, str) and project_path.strip():
        project = project_path
    else:
        # why not `project_path or os.getcwd()`: "" / 0 / [] / false all used to
        # fall through to cwd and silently answer for a DIFFERENT project.
        return None, None, (f"invalid 'project' argument {project_path!r}: "
                            "expected a non-empty path string")
    if is_excluded(project):
        # Logged (unlike the per-turn hooks, which are silent because they fire
        # on every prompt): a model-driven tool call is rare, and a refusal the
        # user cannot explain afterwards is worse than a log line. The path is
        # the caller's own argument, so echoing it discloses nothing the caller
        # did not already supply — no stored content is ever read.
        _log.info(f"refused: {project} is in config.json excluded_projects")
        return None, None, (
            f"Project opted out of cc-memory: {project} is listed in "
            "config.json 'excluded_projects' (or lies beneath a listed "
            "directory). Its memories are neither readable nor writable "
            "through any cc-memory tool. This is a standing user setting, not "
            "a transient failure — do not retry, and do not try another "
            "cc-memory tool for this path.")
    # Anchor AFTER is_excluded, never before — resolving first would widen a
    # per-subdirectory opt-out to its unexcluded parent and serve a project the
    # user opted out of. This is the same ordering rule test_surfaces asserts
    # for all six hooks <!--ce:hooks-->. Until v2.7.1 this surface did not
    # anchor at all: it is the one MODEL-FACING WRITE path, so `memory_add`
    # with no `project` stored into whatever directory the server process
    # happened to sit in, split from
    # the database every hook was using for the same repo.
    project = _anchor_mcp_project(project)
    # SCOPE GATE (user-ratified). Every one of the eight tools takes a free-form
    # `project` path and nothing compared it against the project this server was
    # launched for, so a model that has been indirectly prompt-injected while
    # working in project A could call
    # `memory_add(project="<B>", importance=5, content=...)` and plant a
    # permanent row that B's SessionStart renders into its "Critical (unmerged)"
    # layer at every future session — while A's database stays empty and the
    # session the user is watching records nothing. Driven over real stdio: A
    # ended with 0 active memories, B with 1.
    #
    # Cross-project access was never a documented capability of this surface
    # (`project`'s own schema description reads "default: cwd"), and `/cc-mem
    # --project` still reaches any project from a human-driven CLI. Compared
    # AFTER anchoring so a subdirectory of the server's own project resolves to
    # the same root and is accepted.
    own = _server_root()
    if own is not None and not _same_root(project, own):
        _log.info(f"refused: {project} is outside this server's project {own}")
        return None, None, (
            f"Out of scope: this cc-memory server serves {own}. The 'project' "
            f"argument {project} names a different project, and cc-memory tools "
            f"cannot read or write across projects. Drop the argument to use "
            f"this project, or run the CLI with --project for another one.")
    db_path = resolve_db_path(project)
    if not db_path.exists():
        return None, None, f"No memory database found for this project: {project}"
    db = MemoryDB(db_path)
    pid = db.upsert_project(project)
    return db, pid, None


def _same_root(a, b) -> bool:
    """Do two spellings name the same directory?

    `Path(a) != Path(b)` was the first version and it refused the server's OWN
    project whenever the model spelled it relatively. `core.roots.project_root`
    deliberately returns the ORIGINAL, UNRESOLVED input when the answer is the
    input itself — that is what keeps a symlinked project directory working —
    so `anchor_project(".")` is the string `"."` while `_server_root()` is
    absolute, and the gate then reported `.` as "a different project". Not a
    hypothetical spelling: `commands/cc-mem.md` makes `--project .` the
    plugin's own canonical invocation and this tool's `project` property is
    documented as "default: cwd", so a model being explicit about "here"
    writes `"."`. Driven over real stdio: `.` and `./` refused, absolute and
    subdirectory spellings accepted.

    `anchor_project` already resolves both sides for its own announce check;
    this is the same comparison. normcase because the primary platform is
    case-insensitive, realpath so two spellings through a link agree.
    """
    try:
        return (os.path.normcase(os.path.realpath(str(a)))
                == os.path.normcase(os.path.realpath(str(b))))
    except (OSError, ValueError):
        # why: an unresolvable spelling is not this project — refuse, do not
        # crash. The caller's message already tells the user which project the
        # server serves.
        return False


_SERVER_ROOT = None      # resolved once, on first use; None means "unknown"


def _server_root():
    """The project this server process was launched for, anchored, or None.

    Resolved ONCE and cached: `os.getcwd()` is stable for the process, and
    re-anchoring per call would make the gate above depend on a value a later
    `os.chdir` could move. None (an unresolvable cwd) deliberately DISABLES the
    gate rather than refusing everything — a server that cannot name its own
    project must not become a server that answers nothing.
    """
    global _SERVER_ROOT
    if _SERVER_ROOT is None:
        try:
            _SERVER_ROOT = Path(_anchor_mcp_project(os.getcwd()))
        except Exception as exc:
            # why: cwd can be gone (deleted directory) — fail OPEN to the
            # pre-gate behaviour rather than taking every tool down.
            _log.error_tb("could not resolve this server's own project", exc)
            return None
    return _SERVER_ROOT


def _anchor_mcp_project(project):
    """Anchor a project path for the MCP surface, announcing via the LOG.

    Never `print`: this server speaks JSON-RPC on stdout, so a redirection
    notice written there would corrupt the framing and break the session. The
    caller still learns of the redirection — `_log` is the same channel this
    module already uses for opt-out refusals.
    """
    try:
        from core.roots import anchor_project
        return anchor_project(project, announce=_log.info)
    except Exception as exc:
        # why: a resolver that will not load must not take the whole tool call
        # down; the raw path is exactly the pre-v2.7.1 behaviour
        _log.error_tb(f"project-root anchoring unavailable for {project}", exc)
        return project


def _resolve_session_id(db, pid):
    """Session id to attribute an MCP write to.

    `db.get_recent_memories` filters on the last N session ids, so a row stored
    with session_id NULL is invisible to memory_recent and to the SessionStart
    "Recent" injection layer at importance 1-3. Reuse the project's most recent
    session; only create a row when the project has none at all (creating one
    per add would distort every "last session" computation in the hooks).
    """
    recent = db.get_recent_session_ids(pid, 1)
    if recent:
        return recent[0]
    try:
        sid = db.insert_session(pid, None, "mcp", 0, None, "MCP session")
        # Complete at birth: this row is an attribution container with no
        # pending transcript work behind it (insert_session now writes
        # complete=0 as a claim for the hooks that DO have such work —
        # register X6 — and this caller has none).
        db.mark_session_complete(sid)
        return sid
    except Exception as e:
        # why: attribution is a convenience for recall — failing to create the
        # session row must not lose the memory write itself.
        _log.error(f"could not create an mcp session row: {e}")
        return None


# ── tool handlers ──────────────────────────────────────────────────────────
# A handler returns a plain dict. A truthy "error" key marks the call failed
# (the dispatcher then sets isError: true), so the model is never told that a
# missing database or a bad argument was a success.

def handle_memory_search(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    query = args.get("query", "")
    limit = args.get("limit", 20)
    results = db.search_fts(pid, query, limit=limit)
    compact = []
    for m in results:
        snippet = m["content"][:80] + "..." if len(m["content"]) > 80 else m["content"]
        compact.append({
            "id": m["id"], "category": m["category"],
            "importance": m["importance"], "snippet": snippet,
            "topic": m.get("topic", ""),
        })
    return {"results": compact, "count": len(compact)}


def handle_memory_get_details(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    ids = list(args.get("ids", []))
    if not ids:
        return {"results": [], "missing": [], "count": 0}
    with db._connect() as conn:
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM memories "
            f"WHERE id IN ({ph}) AND project_id = ? AND is_active = 1",
            ids + [pid]
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d.setdefault("supersedes_id", None)
        results.append(d)
    found = {d.get("id") for d in results}
    # Superseded/archived rows are excluded (anti-patch contract at the read
    # boundary); report the ids that did not resolve so a stale id is visible
    # instead of silently returning retracted content.
    missing = [i for i in ids if i not in found]
    return {"results": results, "missing": missing, "count": len(results)}


def handle_memory_add(args):
    """Anti-patch upsert via memory_writer."""
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    from llm.memory_writer import upsert_smart, regenerate_memory_index
    session_id = _resolve_session_id(db, pid)
    result = upsert_smart(
        db, pid, session_id,
        category=args["category"],
        content=args["content"],
        importance=args.get("importance", 3),
        tags=["mcp"],
        topic=args.get("topic", ""),
    )
    # db.db_path.parent, NOT a re-derived `args.get("project") or os.getcwd()`:
    # _get_db anchors, so recomputing the path here from the raw argument sent
    # the row to the anchored ROOT database while regenerating MEMORY.md into
    # the UNANCHORED subdirectory. `x or os.getcwd()` is also the exact idiom
    # _get_db's own comment rejects. Deriving from the database that was
    # actually opened makes the two structurally incapable of disagreeing.
    try:
        regenerate_memory_index(db, pid, db.db_path.parent)
    except Exception as e:
        # why: MEMORY.md is a generated convenience artifact; a failure to
        # rewrite it must not fail (or hide) the write that already succeeded.
        _log.error(f"MEMORY.md regen after MCP add failed: {e}")
    return result


def handle_memory_stats(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    stats = db.get_stats(pid)
    stats["n_observations"] = db.get_observation_count(pid)
    return stats


_TOPICS_DEFAULT = 50
_TOPIC_BODY_CHARS = 2000


def handle_memory_topics(args):
    """List topic summaries. BOUNDED — it was the one list tool that was not.

    Every other list tool here caps its result; this one returned every topic
    with its full body, measured at 272 KB / ~68 000 tokens against a real
    project database and growing with the project. A tool result that size is a
    context-window denial of service that reads like an answer, and the caller
    is a model that cannot decline it. Rows are capped, each body is truncated
    with a visible marker, and `truncated` tells the caller what it did not get
    so it can ask for a narrower slice instead of silently reasoning on a
    partial list.
    """
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    limit = args.get("limit", _TOPICS_DEFAULT)
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = _TOPICS_DEFAULT
    rows = db.get_topics(pid, limit=limit)
    clipped = 0
    for r in rows:
        body = r.get("content") or ""
        if len(body) > _TOPIC_BODY_CHARS:
            r["content"] = body[:_TOPIC_BODY_CHARS] + "\n[... truncated]"
            clipped += 1
    total = db.get_stats(pid).get("n_topics", len(rows))
    return {"topics": rows, "returned": len(rows), "total": total,
            "truncated": {"rows": max(0, total - len(rows)),
                          "bodies": clipped}}


def handle_memory_recent(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    cats = [args["category"]] if args.get("category") else None
    results = db.get_recent_memories(
        pid,
        sessions_back=args.get("sessions_back", 3),
        categories=cats,
        min_importance=args.get("min_importance", 2),
        limit=args.get("limit", 20),
    )
    return {"results": results, "count": len(results)}


def handle_progress_get(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    prog = db.get_progress(pid)
    return prog or {"empty": True}


def handle_progress_regenerate(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    from core.progress import write_progress_md
    # Same reason as handle_memory_add: derive from the opened database, never
    # re-derive from the raw argument, or PROGRESS.md lands in a directory the
    # database it summarises does not live in.
    path = write_progress_md(db, pid, db.db_path.parent)
    return {"path": str(path), "status": "regenerated"}


_HANDLERS = {
    "memory_search":        handle_memory_search,
    "memory_get_details":   handle_memory_get_details,
    "memory_add":           handle_memory_add,
    "memory_stats":         handle_memory_stats,
    "memory_topics":        handle_memory_topics,
    "memory_recent":        handle_memory_recent,
    "progress_get":         handle_progress_get,
    "progress_regenerate":  handle_progress_regenerate,
}


# ── framing ────────────────────────────────────────────────────────────────

# Longest line accepted as one frame. json.loads on a peer-sized line is an
# unbounded allocation whose failure mode (MemoryError) escapes main() exactly
# like the parse errors above. readline(cap) bounds the READ, so an over-cap
# line is drained in cap-sized chunks instead of being materialised whole.
_MAX_LINE_CHARS = 1 << 20


def _dumps(obj):
    """RFC 8259 serializer. `NaN` / `Infinity` are not JSON — json's default
    (`allow_nan=True`) emits them anyway, and every strict parser rejects the
    frame that comes back."""
    return json.dumps(obj, ensure_ascii=False, allow_nan=False)


def _send(obj):
    try:
        text = _dumps(obj)
    except ValueError as e:
        # why: a non-finite float reached the response (most plausibly echoed
        # from a client id). Dropping the frame would orphan the id, so answer
        # it with -32603 and an id sanitized to something serializable.
        _log.error(f"non-serializable response frame: {e}")
        rid = obj.get("id") if isinstance(obj, dict) else None
        if isinstance(rid, bool) or not isinstance(rid, (str, int)):
            rid = None
        text = _dumps({"jsonrpc": "2.0", "id": rid,
                       "error": {"code": -32603,
                                 "message": f"Internal error: response not "
                                            f"serializable as JSON ({e})"}})
    try:
        _original_stdout.write(text + "\n")
        _original_stdout.flush()
    except (OSError, ValueError) as e:
        # why: the peer closed the pipe (or stdout got detached). There is no
        # channel left to report on, and raising here would abort the loop that
        # still has to drain stdin and exit cleanly.
        _log.error(f"could not write response frame: {e}")


def _error(req_id, code, message):
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _is_failed_result(result):
    """True when a handler's dict describes a call that did NOT do its job.

    `{"action": "skipped", "id": null}` is memory_writer reporting a write that
    never happened (`reason: "too_short"`). Delivered in a bare success frame it
    reads to the model as a stored memory. A `skipped` WITH an id is the
    opposite case — `reason: "hash_match"`, the content is already stored under
    that id — and stays a success.
    """
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return True
    return result.get("action") == "skipped" and result.get("id") is None


def _defang(obj):
    """Neutralise authority markers in every string a tool result carries.

    THIS SERVER IS A RENDER PATH. `CLAUDE.md` states the marker defence "runs
    on the write path via `clean_for_storage` and again on every render
    path". When this function was added it named four renderers
    <!--ce:render_paths:asof--> — `core/progress.py`,
    `hooks/session_start.py`, `core/plan.py`, `llm/memory_writer.py` — and
    this file was not among them; the set is computed now (`python
    tools/contracts.py` render_paths) and this server is in it. Its `text`
    block is read by the model as authoritative context exactly the
    way SessionStart's stdout is. Measured on this repository's own database:
    307 active rows, 2 of them armed; the same row renders as
    `&lt;system-reminder&gt;` through SessionStart and as a live
    `<system-reminder>` through here.

    One choke point rather than a fifth hand-maintained call site: every tool
    result goes out through `_send_tool_result`, so the four read handlers
    cannot drift apart from each other the way the render paths already did.
    Escape, never delete — `neutralize_markers` is reversible for a human
    reader and keeps the row's meaning intact.
    """
    from core.privacy import neutralize_markers
    if isinstance(obj, str):
        return neutralize_markers(obj)
    if isinstance(obj, dict):
        # KEYS too (register r6-C6): json.dumps renders keys into the same
        # model-facing text block as values, and a stored topic name — a
        # model-reachable string — becomes a dict key in memory_topics /
        # memory_stats results. Escaping values while emitting keys raw left
        # half the surface armed.
        return {_defang(k) if isinstance(k, str) else k: _defang(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_defang(v) for v in obj]
    return obj


def _send_tool_result(req_id, result, is_error=False):
    try:
        text = json.dumps(_defang(result), ensure_ascii=False,
                          allow_nan=False, default=str)
    except ValueError as e:
        # why: a non-finite float inside a tool result would go out as bare NaN.
        # Report the failure in-band rather than hand the model JSON that its
        # own client cannot parse.
        _log.error(f"non-serializable tool result: {e}")
        text = _dumps({"error": f"result not serializable as JSON: {e}"})
        is_error = True
    payload = {"content": [{"type": "text", "text": text}]}
    if is_error:
        payload["isError"] = True
    _send({"jsonrpc": "2.0", "id": req_id, "result": payload})


def _negotiate_protocol(requested):
    if isinstance(requested, str) and requested in _PROTOCOL_SUPPORTED:
        return requested
    return _PROTOCOL_DEFAULT


def _dispatch_tool_call(req_id, params):
    tool_name = params.get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        # A non-string name used to raise TypeError inside dict.get (unhashable
        # list/dict) and consume the id without any reply.
        _error(req_id, -32602, "Invalid params: 'name' must be a non-empty string")
        return
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        _error(req_id, -32601, f"Unknown tool: {tool_name}")
        return
    tool_args = params.get("arguments")
    if tool_args is None:
        tool_args = {}
    problem = _validate_tool_args(tool_name, tool_args)
    if problem:
        _error(req_id, -32602, f"Invalid params: {problem}")
        return
    # Drop explicit nulls so each handler's `.get(key, default)` still applies.
    tool_args = {k: v for k, v in tool_args.items() if v is not None}
    try:
        result = handler(tool_args)
    except Exception as e:
        _log.error(f"MCP tool error: {tool_name}: {e}")
        _send_tool_result(req_id, {"error": str(e)}, is_error=True)
        return
    _send_tool_result(req_id, result, is_error=_is_failed_result(result))


def _handle_request(req):
    """Answer one Request; return in silence for a Notification.

    The id decides, and it decides FIRST — for every method, not just the two
    branches that used to check. JSON-RPC 2.0 §4.1: a Notification is a message
    with no `id` and the server MUST NOT reply to it. Only
    `notifications/initialized` and the unknown-method branch honoured that, so
    a bare `{"jsonrpc":"2.0","method":"ping"}` was answered with
    `{"jsonrpc":"2.0","id":null,"result":{}}` — and so were `initialize`,
    `tools/list` and `tools/call`. An unsolicited `"id": null` Response is not
    a JSON-RPC Response at all: §5 reserves that id for the one case where the
    request's id could not be DETERMINED (an unparsable or over-length frame,
    which is exactly what `_process_line` / `_serve` use it for). Emitting it
    for a well-formed notification claims that reserved slot for a frame no
    client is waiting on, and a strict client is entitled to treat it as a
    response to a call it never made.

    `req.get("id")` cannot distinguish an absent id from an explicit
    `"id": null`, and the two are deliberately treated alike: §4 discourages a
    null Request id precisely because it is unanswerable, and the wire contract
    at the top of this file has always been written in terms of a "non-null"
    id.

    Consequence worth stating out loud: an id-less `tools/call` is now DROPPED,
    not executed-then-answered-with-null. That is the safe direction and not
    merely the convenient one — half this tool table mutates the user's
    database (`memory_add`, `progress_regenerate`), and a write whose outcome
    can never be reported back is worse than no write. MCP defines `tools/call`
    as a Request; the only Notification it defines,
    `notifications/initialized`, is a no-op here anyway.
    """
    method = req.get("method", "")
    req_id = req.get("id")
    if req_id is None:
        return                 # Notification (or a null id): no reply, ever.

    # JSON-RPC 2.0 §4: the `jsonrpc` member is MANDATORY and must be exactly
    # "2.0". Nothing checked it, so `{"id":1,"method":"ping"}` (member absent)
    # and `"jsonrpc":"1.0"` were both answered with ordinary success frames —
    # measured on the real stdio server. -32600 is the code the spec reserves
    # for it, and answering (rather than dropping) is required because the id
    # above proves this is a Request: silence hangs a client with no timeout,
    # the failure mode the id-first rule in this docstring exists to prevent.
    if req.get("jsonrpc") != "2.0":
        _error(req_id, -32600,
               f"Invalid Request: jsonrpc member must be \"2.0\", got "
               f"{req.get('jsonrpc')!r}")
        return

    params = req.get("params")
    if params is None:
        params = {}            # "params": null is legal JSON-RPC 2.0
    if not isinstance(params, dict):
        # Answered for EVERY method, `notifications/*` included: an id makes
        # this a Request regardless of the method name. Keying this off the
        # method prefix instead made the reply depend on the TYPE of a field
        # already rejected: `notifications/cancelled` with `"params": {}` got
        # -32601 while `"params": []` got nothing at all.
        _error(req_id, -32602,
               f"Invalid params: expected an object, got {type(params).__name__}")
        return

    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": req_id,
               "result": {
                   "protocolVersion": _negotiate_protocol(params.get("protocolVersion")),
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "cc-memory", "version": _VERSION},
               }})
    elif method == "notifications/initialized":
        # Reached only WITH an id (the guard above returned otherwise). A
        # conforming client never puts one on a notification; this one did, so
        # the message is a Request and leaving it unanswered hangs a client
        # that has no timeout.
        _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        _dispatch_tool_call(req_id, params)
    elif method == "ping":
        _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    else:
        _error(req_id, -32601, f"Method not found: {method}")


def _parent_heartbeat(interval=30):
    """POSIX-only orphan reaper.

    On Windows a child's getppid() keeps returning the dead parent's pid
    (measured: unchanged 20s after the parent exited), so this can never fire
    there — main() does not start the thread on nt. stdin EOF reaps the server
    in well under a second on every platform anyway; this is only a backstop
    for a POSIX parent that dies while leaving the pipe open.
    """
    ppid = os.getppid()
    while True:
        time.sleep(interval)
        if os.getppid() != ppid:
            _log.info("parent died, exiting MCP server")
            os._exit(0)


def _reject_constant(name):
    """json.loads' hook for the bare `NaN` / `Infinity` / `-Infinity` tokens.

    They are json-the-python-module extensions, not JSON. Accepting them let a
    client id of `NaN` through, and it was echoed straight back into a frame no
    conforming parser will read.
    """
    raise ValueError(f"{name} is not valid JSON (RFC 8259)")


_INF = float("inf")


def _strict_float(text):
    """parse_float hook: `1e400` overflows to inf without ever passing through
    _reject_constant (that hook only sees the bare NaN/Infinity TOKENS), so the
    converted value has to be range-checked as well. NaN cannot arise here —
    no numeric literal produces it — so ±inf is the whole check."""
    value = float(text)
    if value == _INF or value == -_INF:
        raise ValueError(f"number out of range for JSON: {text}")
    return value


def _read_frame(stream):
    """Read one capped line. Returns (text, oversized); ("", False) at EOF.

    readline(limit) bounds the ALLOCATION, not just the returned slice, so an
    over-cap line is drained in limit-sized chunks and never materialised
    whole. A short chunk with no trailing newline is the last line before EOF —
    a legitimate frame, not an over-cap one.

    The limit is `_MAX_LINE_CHARS + 1`, one char of headroom for the
    terminating newline. Reading with exactly `_MAX_LINE_CHARS` made a line of
    EXACTLY the cap indistinguishable from one that continues — readline
    returned a full buffer with the '\\n' still unread — so a frame at the cap
    was refused with "frame exceeds the 1048576-character limit", a message
    about a limit it had only reached. Measured before: 1048575 answered,
    1048576 refused. The refusal now means what it says: strictly longer than
    `_MAX_LINE_CHARS`.
    """
    limit = _MAX_LINE_CHARS + 1
    chunk = stream.readline(limit)
    if not chunk:
        return "", False
    if chunk.endswith("\n") or len(chunk) < limit:
        return chunk, False
    while True:
        more = stream.readline(limit)
        if not more or more.endswith("\n"):
            return chunk, True


def _process_line(line):
    try:
        req = json.loads(line, parse_constant=_reject_constant,
                         parse_float=_strict_float)
    except (ValueError, RecursionError) as e:
        # why: json.loads raises far more than JSONDecodeError (which is only a
        # ValueError subclass). A 4301-digit integer raises plain ValueError
        # from CPython's int-conversion limit and ~3000 levels of nesting raise
        # RecursionError; both were reachable through the advertised
        # `memory_search.limit` argument BEFORE validation, and both escaped
        # main() — rc=1, traceback on stderr, every id orphaned.
        _log.error(f"unparsable frame ({type(e).__name__}): {line[:100]}")
        _error(None, -32700, f"Parse error: {e}")
        return
    if not isinstance(req, dict):
        _error(None, -32600,
               f"Invalid Request: expected a JSON object, got {type(req).__name__}")
        return
    try:
        _handle_request(req)
    except Exception as e:
        # why: never consume a request id without answering — a client with
        # no timeout blocks forever. Log, then reply -32603.
        _log.error(f"MCP error: {e}")
        rid = req.get("id")
        if rid is not None:
            _error(rid, -32603, f"Internal error: {e}")


def _serve():
    """Read/dispatch loop. One bad frame never ends the session."""
    while True:
        try:
            line, oversized = _read_frame(_original_stdin)
        except (OSError, ValueError, UnicodeError, MemoryError) as e:
            # why: the read side itself failed (pipe reset, detached handle,
            # allocation failure). Nothing is left to read, so stop the loop the
            # way EOF does instead of unwinding out of main().
            _log.error(f"stdin read failed ({type(e).__name__}): {e}")
            return
        if not line:
            return                                  # EOF
        if oversized:
            _log.error(f"frame over {_MAX_LINE_CHARS} chars refused")
            _error(None, -32700,
                   f"Parse error: frame exceeds the {_MAX_LINE_CHARS}-character limit")
            continue
        line = line.strip()
        if not line:
            continue
        try:
            _process_line(line)
        except Exception as e:
            # why: backstop. _process_line answers its own id for everything it
            # anticipates; this catches what it cannot (e.g. MemoryError while
            # building a reply) so the NEXT frame is still served.
            _log.error(f"frame dropped ({type(e).__name__}): {e}")


def main():
    global _devnull
    _log.info(f"MCP server starting (v{_VERSION})")
    _devnull = open(os.devnull, "w")
    sys.stdout = _devnull
    # why: a stray print() is absorbed by sys.stdout, but anything reaching for
    # sys.__stdout__ would write straight into the frame stream and corrupt it.
    sys.__stdout__ = _devnull
    if os.name != "nt":
        threading.Thread(target=_parent_heartbeat, daemon=True).start()
    try:
        _serve()
    except (KeyboardInterrupt, SystemExit):
        _log.info("MCP server interrupted")
    except BaseException as e:
        # why: NOTHING may escape main(). An escaping exception prints a
        # traceback on stderr — rendered as error UI — and exits rc=1, which
        # orphans every in-flight and future id at once. BaseException rather
        # than Exception so that even an exotic escape lands here; it is logged
        # and the process still exits 0, because a server that stops quietly is
        # recoverable and one that crashes loudly is not.
        _log.error(f"fatal in serve loop ({type(e).__name__}): {e}")
    _log.info("MCP server exiting")


if __name__ == "__main__":
    main()
