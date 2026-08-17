#!/usr/bin/env python3
"""
CLI query/management tool for cc-memory databases.

  python -m cc_memory.cli.mem --project . stats
  python -m cc_memory.cli.mem --project . list decisions
  python -m cc_memory.cli.mem --project . search "keyword"
  python -m cc_memory.cli.mem --project . add decision "Chose X" --importance 4 --topic auth
  python -m cc_memory.cli.mem --project . consolidate
  python -m cc_memory.cli.mem --project . progress       # show / regenerate PROGRESS.md

`add` and `import-batch` route through llm.memory_writer.upsert_smart so the
anti-patch reconcile contract is honored from the CLI too.
"""
import argparse
import json
import re
import sqlite3
import sys
import textwrap
from builtins import print as _builtin_print
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio so `mem.py status / list / search` doesn't crash
# when memory content contains emoji / math glyphs on Windows gbk consoles.
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

from core.db import CATEGORIES, MemoryDB


def _resolve_version() -> str:
    """Single-source the version string. Never a hardcoded literal — v2.4.3
    shipped two stale ones in this file (`mem.py:276`, `:984`).

    `core.version` is THE canonical runtime source and resolves under BOTH
    install layouts, because every entry point puts the PACKAGE directory on
    sys.path and then imports flat (`from core.db import MemoryDB`): a
    standalone install is FLAT (ui/installer.py writes TARGET_DIR/core/…, with
    no cc_memory/ segment), so `import cc_memory` raises there. The on-disk
    fallbacks below only matter for a pre-2.5 flat install laid down before
    core/version.py existed.
    """
    try:
        from core.version import __version__ as v
        if v:
            return str(v)
    except ImportError:
        # why: a flat install laid down before core/version.py existed —
        # the on-disk probes below are authoritative there.
        pass
    for probe in (_PKG_ROOT / "core" / "version.py", _PKG_ROOT / "__init__.py"):
        try:
            m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']',
                          probe.read_text(encoding="utf-8"))
            if m:
                return m.group(1)
        except OSError:
            # why: an absent/unreadable probe is not fatal for a version banner
            continue
    try:
        # utf-8-sig, not utf-8: PowerShell's Out-File writes a BOM by default on
        # the primary platform and a BOM makes json.load raise, which used to
        # print "unknown" on an otherwise healthy install. ValueError rather
        # than json.JSONDecodeError, because UnicodeDecodeError (a UTF-16 file)
        # is also a ValueError and was NOT caught before.
        return str(json.loads(
            (_PKG_ROOT / "config.json").read_text(encoding="utf-8-sig")
        ).get("version") or "unknown")
    except (OSError, ValueError, AttributeError):
        # why: same — a missing version must degrade, never abort the CLI
        return "unknown"


__version__ = _resolve_version()


def _resolve_db(project):
    p = Path(project).resolve()
    return p / "memory", p / "memory" / "memory.db", p.name


def _require_db_path(db_path):
    """Refuse a pure READ against a project that has no database.

    Six read-only commands used to create a 140 KB database as a side effect of
    asking a question (search / topics / summary / supersedes / observations /
    mode), while every other command refused. One policy now: a question never
    creates state.
    """
    if not db_path.exists():
        print(f"Error: no memory database at {db_path}")
        sys.exit(1)


def _require_db(db_path):
    _require_db_path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _bounded_limit(value):
    """argparse type for --limit: [1, MemoryDB._MAX_SEARCH_LIMIT].

    SQLite reads a NEGATIVE limit as "no limit", so `--limit -1` printed the
    whole table (register Y5) — the same floor-and-ceiling rule db.search_fts
    already enforces, applied at the argument boundary where the user sees
    the refusal instead of a silent full dump.
    """
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(
            f"--limit must be >= 1 (SQLite reads a negative LIMIT as "
            f"unbounded); got {n}")
    return min(n, MemoryDB._MAX_SEARCH_LIMIT)


def _require_project_id(conn, project):
    """The `projects.id` this --project resolves to — or a visible refusal.

    One memory.db can hold SEVERAL project rows (a directory rename creates a
    second one — register C4 — and nested layouts are real), and `list` /
    `stats` used to query with no project predicate at all: after a rename,
    `list` printed the other project's memories and `stats` counted both
    (register E1). A database whose rows all belong to OTHER paths is a
    finding to report, not a scope to silently widen.
    """
    path = str(Path(project).resolve())
    row = conn.execute("SELECT id FROM projects WHERE path = ?",
                       (path,)).fetchone()
    if row:
        return row["id"]
    others = [r["path"] for r in conn.execute(
        "SELECT path FROM projects ORDER BY last_active DESC").fetchall()]
    print(f"Error: this database has no project row for {path}.")
    if others:
        print("  It holds row(s) for: " + ", ".join(others[:5]))
        # NOT "point --project at the old path" (register r6-C13): after a
        # rename the old path no longer exists, and `_resolve_db` derives the
        # database FROM the --project path — so that advice cannot work. The
        # row is created by the first hook that runs here.
        print("  A renamed project keeps its rows under the old path. Run one "
              "Claude session in this directory (any hook creates the new "
              "row), or inspect the old rows with:")
        print(f"    /cc-mem sql \"SELECT * FROM memories WHERE project_id="
              f"(SELECT id FROM projects WHERE path='{others[0]}') LIMIT 20\"")
    sys.exit(1)


def _neutralize(text):
    """Escape authority markers in anything this CLI prints. Never deletes.

    `/cc-mem` runs as a Bash command inside a Claude session, so its stdout
    lands in the model's context — which makes this a RENDER PATH in exactly
    the sense `CLAUDE.md` means when it says the marker defence "runs on the
    write path and again on every render path". When this function was added
    that sentence named four renderers <!--ce:render_paths:asof--> and this
    file was not among them; the set is computed now (`python
    tools/contracts.py` render_paths) and this file is in it. Measured on
    this repository's own database: 307 active rows, 2 armed; the same row
    prints as `&lt;system-reminder&gt;` through SessionStart and as a live
    tag here.
    Degrades to the raw text if `core.privacy` cannot be imported — a CLI that
    cannot render is worse than one that renders unescaped.
    """
    try:
        from core.privacy import neutralize_markers
        return neutralize_markers(text)
    except Exception:
        # why: printing is this command's whole job; an unavailable defence
        # must not turn a listing into a traceback. But it must not print the
        # LIVE tag either — this fallback used to return the text verbatim,
        # so the one failure mode the defence exists for (armed rows) sailed
        # through exactly when the defence was down (register Y3). Escaping
        # every `<` over-escapes ordinary text; on a display-only path where
        # the sanitizer is UNIMPORTABLE (a broken install), readable-but-ugly
        # beats armed, and duplicating the marker list here is the drift this
        # module refuses everywhere else.
        return str(text).replace("<", "&lt;")


def print(*parts, **kw):        # noqa: A001 — shadowing is the whole point
    """Every line this CLI emits, escaped. Shadows the builtin ON PURPOSE.

    This module has 194 print sites and stdout that lands in a Claude session's
    context. Escaping at the call sites was tried and failed twice: `_trunc`
    got it right, then `cmd_summary` (:1188) and `cmd_inject_show` (:1513)
    shipped printing stored rows raw, and a planted row measured live=1
    escaped=0 through both. A rule that 194 sites must each remember is a rule
    that the 195th breaks, so the unsafe primitive is removed from the module
    instead of being documented.

    `neutralize_markers` is idempotent (verified on tags, banners, controls,
    and already-escaped text), so the call sites that still escape explicitly
    stay correct. Non-str arguments are stringified first — exactly what the
    builtin does — so behaviour is identical apart from the escaping.
    """
    _builtin_print(*[_neutralize(p if isinstance(p, str) else str(p))
                     for p in parts], **kw)


def _trunc(text, w=80):
    text = _neutralize(text)
    return text[:w-1] + "…" if len(text) > w else text


def _table(headers, rows):
    widths = [len(h) for h in headers]
    srows = []
    for row in rows:
        # Escape BEFORE measuring. `_trunc` escapes on the way out, so widths
        # taken from the raw text were a different length than the text they
        # had to hold: a 38-character `<system-reminder>…` becomes 50 escaped
        # and was cut back to 38 — twelve characters of a row the user asked
        # to see, lost to a column that was never full. Idempotent, so the
        # `_trunc` pass below is a no-op on what it receives here.
        sr = [_neutralize(str(v)) if v is not None else "" for v in row]
        srows.append(sr)
        for i, c in enumerate(sr):
            widths[i] = max(widths[i], min(len(c), 60))
    sep = "  ".join("-" * w for w in widths)
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(sep)
    for sr in srows:
        t = [_trunc(c, widths[i]) for i, c in enumerate(sr)]
        print(fmt.format(*t))


# Every module a hook imports at load time. If one is missing the hook dies at
# import, which Claude Code surfaces as nothing at all — so this list is the
# only warning the user gets. It must therefore cover the IMPORT CLOSURE of the
# hooks, not a hand-picked subset: extractor / auth / ccl_backend / consolidate
# / idle and config.json were all absent, so a partial install missing
# core/extractor.py (imported by pre_compact.py and session_start.py) reported
# a clean bill of health while every hook silently failed.
#
# The `cc_memory/` prefix names the PACKAGE root, not a literal directory: a
# standalone install is FLAT (see _inspect_layout).
_REQUIRED_PLUGIN_FILES = [
    "cc_memory/__init__.py",
    "cc_memory/config.json",
    "cc_memory/core/atomic.py",
    "cc_memory/core/db.py",
    "cc_memory/core/logger.py",
    "cc_memory/core/version.py",
    "cc_memory/core/privacy.py",
    "cc_memory/core/modes.py",
    "cc_memory/core/progress.py",
    "cc_memory/core/plan.py",
    "cc_memory/core/encoding_setup.py",
    "cc_memory/core/extractor.py",
    "cc_memory/core/auth.py",
    "cc_memory/core/consolidate.py",
    "cc_memory/core/idle.py",
    # v2.6.0: every hook imports this at MODULE level, so an install missing
    # it does not degrade — all six die at import with a stderr traceback.
    # It was absent from this list, which is what let `status` report an
    # install "healthy" while nothing worked.
    "cc_memory/core/roots.py",
    # v2.8.0, and the SAME shape a second time: two hooks <!--ce:hooks:subset-->
    # (stop, user_prompt) and core/idle.py import this at module level for the
    # per-session marker paths. A new core module has to
    # be registered in three separate places — this list, `ui/installer.py`'s
    # copy manifest and `build_exe.py`'s — and smoke_test asserts parity only
    # between the latter two, so this one is the copy that goes quiet.
    "cc_memory/core/markers.py",
    # v2.8.0: the ONE similarity substrate. llm/memory_writer.py imports it at
    # module level, and every hook imports memory_writer — an install missing
    # it loses all six hooks <!--ce:hooks--> at once.
    "cc_memory/core/textsim.py",
    # v2.10.0, and the SAME shape a THIRD time — the comment two entries above
    # named this list as "the copy that goes quiet" and was then proved right
    # by the very next module added. `hooks/_entry.py` is THE hook entry ladder
    # (stdin parse + the opt-out→anchor gate); all six hook <!--ce:hooks-->
    # scripts below import it at module level, so an install missing this one
    # file loses every hook at import — while `status` walked a list that did
    # not mention it and certified the install healthy. Verified: the six
    # hook <!--ce:hooks--> entries are listed here and each one does
    # `from hooks._entry import …`.
    "cc_memory/hooks/_entry.py",
    "cc_memory/hooks/pre_compact.py",
    "cc_memory/hooks/consolidate_async.py",
    "cc_memory/hooks/session_start.py",
    "cc_memory/hooks/stop.py",
    "cc_memory/hooks/post_tool_use.py",
    "cc_memory/hooks/user_prompt.py",
    "cc_memory/llm/memory_writer.py",
    "cc_memory/llm/ccl_backend.py",
    "cc_memory/llm/parse.py",
    "hooks/hooks.json",
]


_HOOK_EVENTS = ("PreCompact", "SessionStart", "Stop",
                "PostToolUse", "UserPromptSubmit")


def _read_user_settings():
    """~/.claude/settings.json as a dict. Never raises; always returns a dict.

    `utf-8-sig`, NOT `utf-8`. PowerShell's `>` and `Out-File` write a UTF-8 BOM
    by default and `json.loads` rejects the leading U+FEFF, so a settings.json
    the user once edited from a PS prompt made ONE healthy marketplace install
    report `[FAIL] No cc-memory install detected.` here — while
    ui/installer.py:687 read the same file as `utf-8-sig` (with a comment naming
    PowerShell) and `skills/ccm-load` reported ACTIVATED. Same machine, three
    verdicts. `utf-8-sig` is a strict superset for READING: it drops a BOM when
    one is present and is byte-identical to `utf-8` otherwise, so this can only
    ever add installs to the report, never remove one.
    """
    path = Path.home() / ".claude" / "settings.json"
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # why: settings.json being malformed is a recoverable diagnostic
        # state — we still want to report on the OTHER layouts (legacy,
        # cache) so the user can see they're broken too; default to {}
        return {}
    return settings if isinstance(settings, dict) else {}


def _marketplace_enabled_key(settings):
    """The `enabledPlugins` key of an ACTIVE plugin install, or None.

    DELIBERATE MIRROR of `ui/installer.py:_marketplace_registration`, down to
    the `key.split("@")[0] == "cc-memory"` match. Testing only the literal
    `"cc-memory@cc-memory"`, as this file used to, disagreed with the installer
    for any other marketplace name — `cc-memory@<anything>` is the same plugin
    as far as Claude Code is concerned, and the installer refuses to install
    alongside it.
    """
    plugins = settings.get("enabledPlugins")
    if isinstance(plugins, dict):
        for key, val in plugins.items():
            if isinstance(key, str) and key.split("@")[0] == "cc-memory" and val:
                return key
    return None


def _settings_hook_events(settings):
    """`{event: first cc-memory command}` registered in settings.json["hooks"].

    settings.json is user-editable, so nothing about its shape is guaranteed and
    every level is type-checked — the installer learned the same lesson at
    `ui/installer.py:_group_commands`.
    """
    found = {}
    hooks_block = settings.get("hooks")
    if not isinstance(hooks_block, dict):
        return found
    for ev in _HOOK_EVENTS:
        entries = hooks_block.get(ev)
        if not isinstance(entries, list):
            continue
        for mg in entries:
            if not isinstance(mg, dict):
                continue
            group = mg.get("hooks")
            for h in group if isinstance(group, list) else []:
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command")
                if isinstance(cmd, str) and "cc-memory" in cmd:
                    found[ev] = cmd
                    break
            if ev in found:
                break
    return found


def _double_registration():
    """`(enabledPlugins key, {event: command})` when BOTH hook registries are
    live at once, else None.

    Claude Code loads a plugin's hooks from its manifest (`hooks/hooks.json`)
    whenever `enabledPlugins` names it; the STANDALONE installer instead writes
    hook commands into `~/.claude/settings.json["hooks"]`. Neither registry
    removes the other, and the normal upgrade sequence reaches the state from
    both directions — an exe user later runs `/plugin`, or a marketplace user
    runs the exe with `--force`, which `ui/installer.py:cli_install` explicitly
    offers.

    `ui/installer.py:cli_install` calls exactly this state a hard FAIL (rc=2,
    "A standalone install registers the SAME hooks a second time"), while
    `/cc-mem status` printed two cheerful `[OK  ]` layouts and said nothing —
    and `/cc-mem status` is the surface a user actually consults. Same machine
    state, opposite verdicts from two shipped surfaces. This makes them agree.
    """
    settings = _read_user_settings()
    key = _marketplace_enabled_key(settings)
    if not key:
        return None
    events = _settings_hook_events(settings)
    if not events:
        return None
    return key, events


def _as_dict(v):
    """Collapse a wrong-typed settings level to {} so `status` can report.

    `_read_user_settings` guarantees only the TOP level is a dict, and its
    sibling `_settings_hook_events` already spells out that nothing about a
    user-edited settings.json's shape is guaranteed below that. This chain
    used to `.get()` through four such levels bare, so the health-check
    command — the one you run precisely when settings.json looks wrong —
    died with `AttributeError: 'list' object has no attribute 'get'` (rc=1,
    raw traceback) instead of printing the report that would diagnose it.
    """
    return v if isinstance(v, dict) else {}


def _detect_install_layouts():
    """Detect every cc-memory install layout active on this machine.

    A machine can have more than one (e.g. dev-checkout marketplace +
    a stale marketplace-cache entry). Returns a list of dicts, one per
    layout, with file-presence + hook-registration verdicts inside.

    Recognized:
      - marketplace-directory: extraKnownMarketplaces["cc-memory"].source.type
                               == "directory"; root = source.path (dev checkout)
      - marketplace-cache:     installed_plugins.json[cc-memory@cc-memory][0].installPath
      - legacy-install:        ~/.claude/hooks/cc-memory/ (nested OR flat)

    Detecting TWO of them is not the same as detecting two healthy installs —
    see `_double_registration`, which `cmd_status` reports separately.
    """
    layouts = []
    home = Path.home() / ".claude"

    settings = _read_user_settings()
    enabled_marketplace = bool(_marketplace_enabled_key(settings))

    mp_entry = _as_dict(_as_dict(settings.get("extraKnownMarketplaces")).get("cc-memory"))
    mp_src = _as_dict(mp_entry.get("source"))
    if mp_src.get("type") == "directory" or mp_src.get("source") == "directory":
        mp_path = mp_src.get("path")
        if isinstance(mp_path, str) and mp_path:
            layouts.append(_inspect_layout(
                "marketplace-directory", Path(mp_path),
                hooks_via="plugin-manifest", enabled=enabled_marketplace,
            ))

    inst_path = home / "plugins" / "installed_plugins.json"
    if inst_path.exists():
        try:
            inst = json.loads(inst_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # why: corrupt installed_plugins.json blocks ALL plugin discovery
            # at the Claude Code level too — we surface that elsewhere; here
            # we just skip the cache layout so the report continues
            inst = {}
        entries = _as_dict(_as_dict(inst).get("plugins")).get("cc-memory@cc-memory")
        for entry in (entries if isinstance(entries, list) else []):
            p = _as_dict(entry).get("installPath")
            if not isinstance(p, str) or not p:
                continue
            root = Path(p)
            if not root.exists():
                layouts.append({
                    "layout": "marketplace-cache",
                    "root": root,
                    "pkg_dir": None,
                    "hooks_via": "plugin-manifest",
                    "enabled": enabled_marketplace,
                    "plugin_files_ok": False,
                    "missing_files": ["<installPath does not exist>"],
                    "hooks_json": None,
                    "hooks_registered": [],
                    "broken_reason": f"installPath {root} not found on disk",
                })
                continue
            layouts.append(_inspect_layout(
                "marketplace-cache", root,
                hooks_via="plugin-manifest", enabled=enabled_marketplace,
            ))

    # Accept BOTH standalone shapes. ui/installer.py:_copy_subpackages writes
    # TARGET_DIR/<subdir>/ directly, i.e. a FLAT tree with no cc_memory/
    # segment, so probing only for cc_memory/ made every install this installer
    # produces invisible to `/cc-mem status`.
    legacy = home / "hooks" / "cc-memory"
    if (legacy / "cc_memory").exists() or (legacy / "core" / "db.py").exists():
        layouts.append(_inspect_layout(
            "legacy-install", legacy,
            hooks_via="user-settings",
            enabled=True,  # legacy install doesn't gate on enabledPlugins
            settings_dict=settings,
        ))
    return layouts


def _inspect_layout(layout_name, root: Path,
                    hooks_via: str, enabled: bool,
                    settings_dict=None):
    """Check plugin file presence + hook registration for one install root.

    Two on-disk shapes exist and BOTH are healthy:

      nested (marketplace / dev checkout)   root/cc_memory/core/db.py
      flat   (ui/installer.py standalone)   root/core/db.py

    v2.4.3 taught _detect_install_layouts about the flat shape but left this
    function resolving `root / rel` with rel carrying a literal `cc_memory/`
    prefix — so every standalone install reported 22 of 22 files missing and
    `/cc-mem status` then SKIPPED the API-key check for want of a "functional"
    layout. Resolve the package dir once and strip the prefix accordingly.

    `hooks/hooks.json` is a REPO-level file, not a package file: the standalone
    installer never copies it and it is meaningless when the hooks come from
    ~/.claude/settings.json, so it is only required for plugin-manifest installs.

    Returns the verdict dict, including the resolved `pkg_dir` so callers can
    put the right directory on sys.path.
    """
    pkg_dir = root / "cc_memory" if (root / "cc_memory").is_dir() else root

    missing = []
    for rel in _REQUIRED_PLUGIN_FILES:
        if rel.startswith("cc_memory/"):
            probe = pkg_dir / rel[len("cc_memory/"):]
        elif hooks_via == "plugin-manifest":
            probe = root / rel
        else:
            continue  # repo-level file, not part of a settings.json install
        if not probe.exists():
            missing.append(rel)
    plugin_files_ok = not missing

    hooks_registered = []
    hooks_json_path = None
    if hooks_via == "plugin-manifest":
        hooks_json_path = root / "hooks" / "hooks.json"
        if hooks_json_path.exists():
            try:
                hj = json.loads(hooks_json_path.read_text(encoding="utf-8"))
                hooks_registered = list(hj.get("hooks", {}).keys())
            except (json.JSONDecodeError, OSError):
                # why: malformed hooks.json IS a bug we want to surface, but
                # at the LAYOUT-detection level we just report an empty hook
                # list — the downstream _print_layout_report flags the mismatch
                hooks_registered = []
    elif hooks_via == "user-settings" and settings_dict is not None:
        # ONE implementation of "which events does settings.json[hooks]
        # register for cc-memory", shared with _double_registration. When this
        # scan was inline here, the double-registration verdict had no way to
        # reuse it without a second copy — and a second copy is how two surfaces
        # start disagreeing about the same machine state in the first place.
        hooks_registered = sorted(_settings_hook_events(
            settings_dict if isinstance(settings_dict, dict) else {}))

    return {
        "layout": layout_name,
        "root": root,
        "pkg_dir": pkg_dir,
        "hooks_via": hooks_via,
        "enabled": enabled,
        "plugin_files_ok": plugin_files_ok,
        "missing_files": missing,
        "hooks_json": hooks_json_path,
        "hooks_registered": hooks_registered,
    }


def _print_layout_report(layout: dict) -> bool:
    """Print one layout's status. Returns True iff this layout is fully functional."""
    name = layout["layout"]
    root = layout["root"]
    if "broken_reason" in layout:
        print(f"  [FAIL] {name} at {root}")
        print(f"         {layout['broken_reason']}")
        return False
    files_tag = "OK  " if layout["plugin_files_ok"] else "FAIL"
    enabled_tag = "" if layout["hooks_via"] != "plugin-manifest" else (
        " · enabled" if layout["enabled"] else " · NOT enabled in settings.json"
    )
    shape = "flat" if layout.get("pkg_dir") == root else "nested"
    print(f"  [{files_tag}] {name} at {root} ({shape}){enabled_tag}")
    if layout["missing_files"]:
        print(f"         missing: {', '.join(layout['missing_files'][:5])}"
              + (" ..." if len(layout["missing_files"]) > 5 else ""))

    expected_events = {"PreCompact", "SessionStart", "Stop",
                       "PostToolUse", "UserPromptSubmit"}
    got = set(layout["hooks_registered"])
    n = len(got & expected_events)
    if n == 5:
        via = "hooks/hooks.json" if layout["hooks_via"] == "plugin-manifest" \
              else "~/.claude/settings.json[hooks]"
        print(f"         hooks: 5/5 registered via {via}")
    elif n > 0:
        print(f"         hooks: {n}/5 registered "
              f"(missing: {', '.join(expected_events - got)})")
    else:
        print(f"         hooks: 0/5 — neither hooks.json nor settings.json[hooks] "
              f"registers cc-memory")

    return layout["plugin_files_ok"] and n == 5 and layout["enabled"]


def _count_active_memories(db_file):
    """Active memory rows in `db_file`, or "?" — without writing to it.

    `immutable=1` on top of `mode=ro` is the load-bearing part. Plain
    `mode=ro` still lets SQLite create `-wal` and `-shm` siblings next to a
    WAL database, i.e. the report writes into the very directory it is only
    supposed to NAME. immutable forbids that outright; the cost is that
    content sitting unmerged in a WAL is not counted, which for a "there is
    another database here, roughly this big" line is an acceptable trade and
    is why the number is presented as approximate.

    Counts ACTIVE rows only, matching `get_stats` — the root's own line is
    active-only, and v2.6.0 counted every row here, so the same command
    reported two different sizes for the same database (3725 vs 2607 on the
    reporting machine).
    """
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        # why: unreadable, locked or not a database — it is still worth
        # naming; only its size is unknown
        return "?"
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memories WHERE is_active = 1").fetchone()[0]
    except sqlite3.Error:
        # why: a foreign or pre-v2 schema has no is_active column; the row
        # count is still a useful magnitude
        try:
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        except sqlite3.Error:
            return "?"
    finally:
        conn.close()


def _report_nested_databases(project):
    """Name every separate database below `project`. Never raises.

    The REPORTING half of root anchoring. Resolution never merges or moves a
    database that already exists — a stray born before v2.6.0 and a
    deliberate nested sub-project are byte-for-byte identical on disk, so
    touching either automatically would destroy the other (the reporting
    machine has four genuinely nested ones, the largest holding 3725
    memories). But an unnoticed stray is otherwise invisible: nothing reads
    it and nothing says so. Listing them here — an explicit command, not a
    per-turn hook — is the visibility without the destruction.
    """
    try:
        from core.roots import nested_databases
        nested = nested_databases(project)
    except Exception as exc:
        # why: a diagnostic must never take down the status report it is in
        print(f"  [WARN] Nested-database scan skipped: {exc}")
        return
    for sub in nested:
        n_sub = _count_active_memories(sub / "memory" / "memory.db")
        print(f"  [WARN] Separate database below this project: {sub} "
              f"(~{n_sub} memories). Sessions run there use IT, not this one. "
              f"If that is a stray, move or delete it; if it is a project in "
              f"its own right, nothing to do.")


def cmd_status(args):
    memory_dir, db_path, name = _resolve_db(args.project)
    project = str(Path(args.project).resolve())

    print(f"\n{'='*55}\n  cc-memory v{__version__} Status Check: {name}\n{'='*55}\n")

    layouts = _detect_install_layouts()
    if not layouts:
        print("  [FAIL] No cc-memory install detected.")
        print("         Checked: marketplace-directory (settings.json"
              ".extraKnownMarketplaces),")
        print("                  marketplace-cache (plugins/installed_plugins.json),")
        print("                  legacy install (~/.claude/hooks/cc-memory/)")
    else:
        functional = False
        for layout in layouts:
            if _print_layout_report(layout):
                functional = True
        if not functional:
            print("  [WARN] No fully-functional install layout. Hooks may still "
                  "fire if one source is partially configured.")

    # TWO functional layouts is not twice as healthy. Reported outside the loop
    # above (and outside its `if layouts` guard) because the condition is about
    # the two REGISTRIES, not about the two install trees: settings.json can
    # still register the hooks after the standalone tree itself was deleted.
    dual = _double_registration()
    if dual:
        dual_key, dual_events = dual
        dual_cmd = dual_events[sorted(dual_events)[0]]
        print(f"\n  [FAIL] DOUBLE REGISTRATION — cc-memory's hooks are registered "
              f"TWICE,")
        print(f"         in two independent registries:")
        print(f"           1. plugin      settings.json enabledPlugins"
              f"[\"{dual_key}\"] = true")
        print(f"                          (Claude Code loads hooks/hooks.json "
              f"from the plugin manifest)")
        print(f"           2. standalone  settings.json[\"hooks\"] registers "
              f"{len(dual_events)} event(s):")
        print(f"                          {', '.join(sorted(dual_events))}")
        print(f"                          e.g. {_trunc(dual_cmd, 62)}")
        print("         Two independent hook registries very likely means every "
              "hook fires")
        print("         twice — duplicated PostToolUse observations, two "
              "PreCompact")
        print("         extractions (double LLM spend AND two writes), two Stop "
              "observers")
        print("         (inferred from the two registrations - not directly "
              "observed).")
        print("         ui/installer.py REFUSES to install into this state "
              "(rc=2), so this")
        print("         report and the installer now agree. Keep exactly ONE:")
        print("           - disable the plugin:  /plugin  (keeps the standalone "
              "install)")
        print("           - or drop the standalone registration:  "
              "installer.py --uninstall")
        print("             (removes only the settings.json entries; project "
              "memory/ is kept)")

    active_layout = next((L for L in layouts
                          if "broken_reason" not in L
                          and L.get("plugin_files_ok")), None)

    # BEFORE the missing-database early return, deliberately. The
    # stray-only shape — this directory has no database of its own while a
    # subdirectory does — is the single most damaging layout there is, and
    # v2.6.0 printed "No database" and returned without ever mentioning it.
    _report_nested_databases(project)

    if not db_path.exists():
        print(f"  [FAIL] No database at {db_path}")
        return

    db = MemoryDB(db_path)
    pid = db.upsert_project(project)
    stats = db.get_stats(pid)
    print(f"  [OK]   Database: {stats['n_memories']} memories, "
          f"{stats['n_sessions']} sessions, {stats.get('n_topics', 0)} topics")
    print(f"  [{'OK' if db._fts5_available else 'WARN'}]   FTS5: "
          f"{'available' if db._fts5_available else 'unavailable (using LIKE fallback)'}")
    print(f"  [INFO] Observations: {db.get_observation_count(pid)} recorded")

    last_save = memory_dir / ".last_save.json"
    if last_save.exists():
        try:
            info = json.loads(last_save.read_text(encoding="utf-8"))
            ts = info.get("timestamp", "?")
            ok = info.get("success", False)
            status = "OK" if ok else "FAIL"
            method = info.get("method", "?")
            ni = info.get("n_inserted", info.get("n_saved", 0))
            stale_warn = ""
            from datetime import datetime, timedelta
            try:
                last_dt = datetime.fromisoformat(
                    ts.replace("Z", "+00:00").split(".")[0]
                )
                age = datetime.now() - last_dt.replace(tzinfo=None)
                if age > timedelta(days=14):
                    stale_warn = (
                        f"  [WARN] STALE ({age.days}d old — "
                        f"check for direct MEMORY.md edits)"
                    )
            except (ValueError, TypeError):
                # why: malformed timestamp shouldn't suppress the rest of the
                # status report; we leave stale_warn empty and continue
                stale_warn = ""
            print(f"  [{status:4}] Last save: {ts} (+{ni} via {method}){stale_warn}")
        except (json.JSONDecodeError, OSError):
            print(f"  [WARN] Last save status unreadable")
    else:
        print(f"  [INFO] No save recorded yet")

    prog = db.get_progress(pid)
    if prog:
        cr = (prog.get("current_request") or "")[:60]
        print(f"  [OK]   PROGRESS: \"{cr}\"")
    else:
        print(f"  [INFO] No progress recorded yet")

    if active_layout:
        # Use the RESOLVED package dir — hardcoding root/"cc_memory" broke the
        # moment _inspect_layout learned about flat standalone installs.
        pkg_dir = active_layout.get("pkg_dir") or (active_layout["root"] / "cc_memory")
        sys.path.insert(0, str(pkg_dir))
        try:
            from core.auth import get_api_key
            key, source = get_api_key()
            if key:
                print(f"  [OK]   API key: {source} ({key[:8]}...)")
            else:
                reason = "OAuth expired" if source == "oauth_expired" else "not found"
                print(f"  [WARN] API key: {reason}")
        except Exception as e:
            print(f"  [WARN] API key check failed: {e}")
    else:
        print(f"  [SKIP] API key check (no functional install layout)")

    mode = db.get_project_mode(pid)
    print(f"\n  Mode: {mode}")
    print(f"{'='*55}")


def cmd_stats(args):
    # Every query below carries `project_id = ?` (register E1): one DB file
    # can hold several project rows, and unscoped counts summed them all.
    _, db_path, name = _resolve_db(args.project)
    conn = _require_db(db_path)
    pid = _require_project_id(conn, args.project)
    print(f"\n{'='*50}\n  Memory stats: {name}\n{'='*50}")
    s = conn.execute(
        "SELECT COUNT(*) n, MIN(compacted_at) first, MAX(compacted_at) last "
        "FROM sessions WHERE project_id = ?", (pid,)
    ).fetchone()
    print(f"\nSessions: {s['n']}")
    if s['first']:
        print(f"  First : {s['first'][:16]}\n  Last  : {s['last'][:16]}")
    m = conn.execute("SELECT COUNT(*) n FROM memories WHERE is_active=1 "
                     "AND project_id = ?", (pid,)).fetchone()
    a = conn.execute("SELECT COUNT(*) n FROM memories WHERE is_active=0 "
                     "AND project_id = ?", (pid,)).fetchone()
    print(f"\nMemories: {m['n']} active, {a['n']} archived")
    print("\nBy category:")
    by_cat = conn.execute(
        """SELECT category, COUNT(*) cnt, AVG(importance) avg_imp, MAX(importance) max_imp
           FROM memories WHERE is_active=1 AND project_id = ?
           GROUP BY category ORDER BY cnt DESC""", (pid,)
    ).fetchall()
    _table(["Category", "Count", "Avg Imp", "Max Imp"],
           [(r["category"], r["cnt"], f"{r['avg_imp']:.1f}", r["max_imp"]) for r in by_cat])

    topics = conn.execute("SELECT COUNT(*) n FROM topics WHERE project_id = ?",
                          (pid,)).fetchone()
    if topics["n"]:
        print(f"\nTopics: {topics['n']}")
        by_topic = conn.execute(
            """SELECT COALESCE(topic, '(none)') AS t, COUNT(*) cnt
               FROM memories WHERE is_active=1 AND project_id = ?
               GROUP BY t ORDER BY cnt DESC LIMIT 15""", (pid,)
        ).fetchall()
        _table(["Topic", "Count"], [(r["t"], r["cnt"]) for r in by_topic])

    # Supersede chain summary
    superseded = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE supersedes_id IS NOT NULL "
        "AND project_id = ?", (pid,)
    ).fetchone()
    if superseded["n"]:
        print(f"\nSupersede chains: {superseded['n']} update events recorded "
              f"(anti-patch in action — see docs/CONTRACTS.md#anti-patch-contract)")

    kw = conn.execute(
        "SELECT keyword, frequency FROM keywords WHERE project_id = ? "
        "ORDER BY frequency DESC LIMIT 10", (pid,)
    ).fetchall()
    if kw:
        print("\nTop keywords: " + ", ".join(f"{r['keyword']}({r['frequency']})" for r in kw))
    conn.close()


def cmd_list(args):
    _, db_path, name = _resolve_db(args.project)
    conn = _require_db(db_path)
    # Scoped to THIS project's row (register E1): after a directory rename
    # the same DB holds a second project row, and the unscoped query listed
    # the other project's memories under this one's name. Session recency by
    # id, not the compacted_at string (see core/db.py's ordering note).
    pid = _require_project_id(conn, args.project)
    cat = args.category
    recent = [r[0] for r in conn.execute(
        "SELECT id FROM sessions WHERE project_id = ? "
        "ORDER BY id DESC LIMIT ?", (pid, args.sessions)
    ).fetchall()]

    # Session-less memories (session_id IS NULL) are what EVERY manual save path
    # writes — `mem add`, MCP memory_add, the Tk dashboard, the web viewer,
    # skills/save-memories. An INNER JOIN on sessions plus `session_id IN (...)`
    # made all of them permanently invisible to `list`, at every importance.
    params = [pid]
    if recent:
        ph = ",".join("?" * len(recent))
        sess_sql = f"(m.session_id IN ({ph}) OR m.session_id IS NULL)"
        params += recent
    else:
        sess_sql = "m.session_id IS NULL"
    cat_sql = ""
    if cat and cat != "all":
        cat_sql = "AND m.category = ?"
        params.append(cat)
    params.append(args.limit)

    rows = conn.execute(
        f"""SELECT m.id, m.category, m.importance, m.content, m.topic, s.compacted_at
            FROM memories m LEFT JOIN sessions s ON m.session_id=s.id
            WHERE m.is_active=1 AND m.project_id = ? AND {sess_sql} {cat_sql}
            ORDER BY m.importance DESC, m.created_at DESC LIMIT ?""", params
    ).fetchall()

    print(f"\nMemories for {name}" + (f" [{cat}]" if cat and cat != "all" else "") + ":\n")
    if not rows:
        print("  (none)")
    for r in rows:
        d = r["compacted_at"][:10] if r["compacted_at"] else "-"
        stars = "*" * r["importance"] + "." * (5 - r["importance"])
        topic = f"[{r['topic']}]" if r["topic"] else ""
        print(f"  [{r['id']:4d}] {stars}  {r['category']:<10}  {topic:<12}  {d}  "
              f"{_trunc(r['content'], 75)}")
    conn.close()


def cmd_search(args):
    _, db_path, name = _resolve_db(args.project)
    _require_db_path(db_path)
    db = MemoryDB(db_path)
    pid = db.upsert_project(args.project)
    rows = db.search_fts(pid, args.query, limit=30)
    fts_tag = "(FTS5)" if db._fts5_available else "(LIKE)"
    print(f"\nSearch '{args.query}' in {name} {fts_tag}:\n")
    if not rows:
        print("  (no matches)")
        return
    for r in rows:
        d = (r.get("created_at") or "?")[:10]
        c = r["content"]
        idx = c.lower().find(args.query.lower())
        if idx >= 0:
            s, e = max(0, idx-20), min(len(c), idx+len(args.query)+40)
            # _neutralize here too: this branch builds the snippet by slicing
            # and never reaches _trunc, so it is the one search result that
            # would still print a live marker.
            snip = (("..." if s > 0 else "") + _neutralize(c[s:e])
                    + ("..." if e < len(c) else ""))
        else:
            snip = _trunc(c, 90)
        topic = f"[{_neutralize(str(r.get('topic', '')))}]" if r.get("topic") else ""
        print(f"  [{r['id']:4d}] {'*'*r['importance']:<5}  {r['category']:<10}  "
              f"{topic:<12}  {d}  {snip}")


def cmd_sessions(args):
    _, db_path, name = _resolve_db(args.project)
    conn = _require_db(db_path)
    # Register E1's exact defect, closed for `list`/`stats` and missed here:
    # one memory.db can hold several project rows, and an unpredicated query
    # printed the OTHER project's sessions — archive_path basenames included —
    # under this project's heading.
    pid = _require_project_id(conn, args.project)
    rows = conn.execute(
        """SELECT s.id, s.trigger_type, s.compacted_at, s.msg_count,
                  COUNT(m.id) n_mem, s.archive_path
           FROM sessions s LEFT JOIN memories m ON m.session_id=s.id AND m.is_active=1
           WHERE s.project_id = ?
           GROUP BY s.id ORDER BY s.compacted_at DESC LIMIT 10""",
        (pid,)
    ).fetchall()
    print(f"\nSessions for {name}:\n")
    _table(["ID", "Trigger", "Compacted At", "Msgs", "Memories", "Archive"],
           [(r["id"], r["trigger_type"], r["compacted_at"][:16],
             r["msg_count"], r["n_mem"],
             Path(r["archive_path"]).name if r["archive_path"] else "-") for r in rows])
    conn.close()


# ── `sql` is a READ-ONLY query tool ─────────────────────────────────────────
# cmd_sql never commits, and sqlite3 opens an implicit transaction for DML
# only. So `UPDATE ...` was silently rolled back at close() (reported success,
# changed nothing) while `DROP TABLE topics` ran in autocommit and destroyed
# data permanently — MemoryDB then re-created the empty table so nothing even
# looked wrong. Refuse anything that is not a single read statement.
_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_SQL_WRITE_RE = re.compile(
    r"\b(insert|update|delete|replace|drop|alter|create|attach|detach|"
    r"vacuum|reindex|begin|commit|rollback|savepoint|release)\b", re.I)
_SQL_READ_HEADS = ("select", "pragma", "explain", "with", "values")

# SQLite accepts BOTH `PRAGMA name = value` and `PRAGMA name(value)` for every
# settable pragma, and a handful of pragmas ACT on the file with NO argument at
# all. Testing only for "=" let every one of these through as "read-only" —
# each ran rc=0, printed "(no rows)" and PERSISTED: `PRAGMA user_version(7)`
# 0->7, `PRAGMA application_id(1234)` 0->1234, `PRAGMA journal_mode(DELETE)`
# (drops the WAL both PreCompact legs rely on) and `PRAGMA optimize` (creates a
# sqlite_stat1 table in the user's memory.db). PRAGMAs run in autocommit, so
# cmd_sql's trailing rollback() cannot undo any of it.
#
# The parenthesised form is also how the read-only INTROSPECTION pragmas take
# their argument, so those are allow-listed BY NAME rather than by syntax.
#
# DELIBERATE DUPLICATE of ui/dashboard.py:99-137 (`_pragma_is_read_only` plus
# both frozensets), which fixed this class first: that module is a Tk GUI the
# CLI must never import. The two name lists are hand-synced — a divergence
# means one surface refuses what the other happily runs.
_PRAGMA_READ_WITH_ARG = frozenset((
    "table_info", "table_xinfo", "table_list", "index_info", "index_list",
    "index_xinfo", "foreign_key_list", "foreign_key_check", "collation_list",
    "database_list", "compile_options", "function_list", "module_list",
    "pragma_list", "integrity_check", "quick_check", "freelist_count",
    "page_count",
))
_PRAGMA_WRITES_BARE = frozenset((
    "optimize", "wal_checkpoint", "incremental_vacuum", "shrink_memory",
))
_PRAGMA_HEAD_RE = re.compile(
    r"pragma\s+(?:[A-Za-z_]\w*\s*\.\s*)?([A-Za-z_]\w*)\s*(.*)", re.I | re.S)


def _pragma_write_reason(q):
    """Why this PRAGMA can change the database file, or None if it cannot.

    An unrecognised spelling is reported as a write: a false refusal costs one
    retyped query, a false allow costs the user's memory.db.
    """
    m = _PRAGMA_HEAD_RE.match(q.strip())
    if not m:
        return "the PRAGMA name is unrecognized (refused as a possible write)"
    name = m.group(1).lower()
    arg = m.group(2).strip().rstrip(";").strip()
    if arg:
        if name in _PRAGMA_READ_WITH_ARG:
            return None
        return (f"'PRAGMA {name}' with an argument SETS it — `= value` and "
                f"`(value)` are both setters")
    if name in _PRAGMA_WRITES_BARE:
        return f"'PRAGMA {name}' writes to the database even with no argument"
    return None


def _read_only_sql_error(query):
    """Return a human-readable reason the query is rejected, or None if it is a
    single read-only statement."""
    q = _SQL_COMMENT_RE.sub(" ", query or "").strip()
    if not q:
        return "the query is empty"
    q = q.rstrip(";").strip()
    if not q:
        return "the query is empty"
    if ";" in q:
        return ("only ONE statement at a time (found a ';' separator; a ';' "
                "inside a string literal trips this too — by design)")
    head = q.split(None, 1)[0].lower()
    if head not in _SQL_READ_HEADS:
        return f"'{head.upper()}' is not a read-only statement"
    if head == "pragma":
        pragma_err = _pragma_write_reason(q)
        if pragma_err:
            return pragma_err
    if head in ("with", "explain", "values") and _SQL_WRITE_RE.search(q):
        return "the statement contains a data- or schema-modifying keyword"
    return None


def cmd_sql(args):
    _, db_path, _ = _resolve_db(args.project)
    err = _read_only_sql_error(args.query)
    if err:
        print(f"\nRefused: {err}.")
        print("  `sql` is a READ-ONLY query tool "
              "(SELECT / PRAGMA / EXPLAIN / WITH ... SELECT).")
        print("  To change memory use `/cc-mem add`, `/cc-mem cleanup`, "
              "`/cc-mem consolidate` or `/cc-mem encoding-check --apply`.")
        sys.exit(1)
    _require_db_path(db_path)
    # ENGINE-enforced read-only (register E2): the lexical gate above is the
    # explainer, `mode=ro` is the guard. A statement shape the regex misses
    # fails inside SQLite with "attempt to write a readonly database" instead
    # of committing. One implementation, shared with the dashboard console —
    # see core.db.readonly_connect.
    from core.db import readonly_connect
    conn = readonly_connect(db_path)
    print(f"\nSQL: {args.query}\n")
    try:
        rows = conn.execute(args.query).fetchall()
    except sqlite3.Error as e:
        print(f"SQL Error: {e}")
        conn.close()
        sys.exit(1)
    if not rows:
        print("(no rows)")
    else:
        _table(list(rows[0].keys()), [list(r) for r in rows])
        print(f"\n({len(rows)} rows)")
    conn.close()


def cmd_add(args):
    """Add memory via the anti-patch writer (NOT direct insert)."""
    from llm.memory_writer import (upsert_smart, regenerate_memory_index,
                                   MIN_CONTENT_LEN)
    memory_dir, db_path, _ = _resolve_db(args.project)
    from core.progress import ensure_memory_dir
    ensure_memory_dir(memory_dir)
    db = MemoryDB(db_path)
    pid = db.upsert_project(args.project)
    tags = args.tags.split(",") if args.tags else ["manual"]
    topic = args.topic if args.topic else ""
    result = upsert_smart(
        db, pid, None,
        category=args.category,
        content=args.content,
        importance=args.importance,
        tags=tags,
        topic=topic,
    )
    action = result.get("action", "?")
    mid = result.get("id")
    reason = result.get("reason")
    sim = result.get("similarity")

    # `similarity` is ABSENT (not 0.0) when no comparison was made. Printing a
    # fabricated `sim=0.00` made a too_short drop — which saved nothing —
    # indistinguishable from a hash_match skip, which found a real row.
    head = f"[{action}]"
    if mid is not None:
        head += f" #{mid}"
    if reason:
        head += f" ({reason})"
    if sim is not None:
        head += f" sim={sim:.2f}"

    if action == "skipped" and reason == "too_short":
        print(f"{head}  NOTHING SAVED — content must be at least "
              f"{MIN_CONTENT_LEN} characters after cleaning; got "
              f"{len((args.content or '').strip())}.")
        sys.exit(1)

    regenerate_memory_index(db, pid, memory_dir)
    print(f"{head}  {'*'*args.importance} [{args.category}] {args.content}")


def cmd_keywords(args):
    _, db_path, name = _resolve_db(args.project)
    conn = _require_db(db_path)
    # Same register-E1 scope defect as cmd_sessions: `keywords.project_id` is
    # NOT NULL and cmd_stats already filters it; this listing did not.
    pid = _require_project_id(conn, args.project)
    rows = conn.execute(
        "SELECT keyword, frequency, last_seen FROM keywords "
        "WHERE project_id = ? ORDER BY frequency DESC LIMIT 40",
        (pid,)
    ).fetchall()
    print(f"\nVocabulary for {name}:\n")
    _table(["Keyword", "Freq", "Last Seen"],
           [(r["keyword"], r["frequency"], r["last_seen"][:10]) for r in rows])
    conn.close()


def cmd_topics(args):
    _, db_path, name = _resolve_db(args.project)
    _require_db_path(db_path)
    db = MemoryDB(db_path)
    pid = db.upsert_project(args.project)
    topics = db.get_topics(pid)
    counts = db.get_topic_memory_counts(pid)
    print(f"\n{'='*50}\n  Topics for {name}\n{'='*50}\n")
    if not topics:
        print("  No topics yet. Run 'consolidate' to create them.")
        return
    for t in topics:
        n = counts.get(t["name"], 0)
        print(f"  [{_neutralize(str(t['name']))}] (v{t['version']}, {n} memories, "
              f"updated {t['updated_at'][:10]})")
        for line in textwrap.wrap(_neutralize(t["content"]), width=78):
            print(f"    {line}")
        print()


def cmd_consolidate(args):
    from core import consolidate as consolidate_mod
    from core.consolidate import run_consolidation
    _, db_path, _ = _resolve_db(args.project)
    if not db_path.exists():
        print(f"Error: no memory database at {db_path} — nothing to consolidate.")
        sys.exit(1)
    # `verbose=True` only ever reached core.logger's FILE sink, so the CLI ran
    # a multi-stage LLM pipeline in total silence. Setting `_cli_echo` routes
    # those two narration points to stdout for THIS process only; hooks never
    # set it, so the async PreCompact leg's stdout stays empty (hook contract).
    consolidate_mod._cli_echo = print
    print(f"\n{'='*50}\n  Consolidating memory for {args.project}\n{'='*50}\n")
    use_llm = not args.no_llm
    results = run_consolidation(args.project, use_llm=use_llm, verbose=True)
    if not results:
        print("\n[FAIL] consolidation returned no results.")
        sys.exit(1)
    print(f"\n{'='*50}\n  Results:")
    for k, v in results.items():
        print(f"    {k}: {v}")
    print(f"{'='*50}")


def cmd_cleanup(args):
    from core.consolidate import cleanup_garbage, merge_near_duplicates, assign_topics_auto
    from llm.memory_writer import regenerate_memory_index
    memory_dir = Path(args.project).resolve() / "memory"
    # Refuse rather than create, exactly like its sibling `cmd_consolidate`
    # (its `if not db_path.exists()` guard). A bare line range was cited here
    # and had already rotted onto an unrelated SELECT: tools/citation_check.py
    # only scans the files in its TRACKED list, all of which are docs, so a
    # citation inside source is checked by nobody. Name the symbol instead.
    # `MemoryDB(...)` creates, so cleanup used to *initialise* a
    # project that had never used cc-memory and then report "Final: 0 active
    # memories, MEMORY.md regenerated" — a success line for work that could not
    # have happened. Two commands that both operate on existing memories must
    # not disagree about whether there have to be any.
    if not (memory_dir / "memory.db").exists():
        print(f"Error: no memory database at {memory_dir / 'memory.db'} — "
              f"nothing to clean up.")
        sys.exit(1)
    db = MemoryDB(memory_dir / "memory.db")
    pid = db.upsert_project(args.project)
    print(f"\n{'='*50}\n  Cleanup for {Path(args.project).name}\n{'='*50}\n")
    print(f"  Garbage archived: {cleanup_garbage(db, pid)}")
    print(f"  Duplicates archived: {merge_near_duplicates(db, pid)}")
    print(f"  Topics assigned: {assign_topics_auto(db, pid)}")
    regenerate_memory_index(db, pid, memory_dir)
    stats = db.get_stats(pid)
    print(f"\n  Final: {stats['n_memories']} active memories, "
          f"MEMORY.md regenerated")


def cmd_schema(args):
    _, db_path, _ = _resolve_db(args.project)
    conn = _require_db(db_path)
    print("\n=== Database Schema ===\n")
    for r in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
        "ORDER BY type DESC, name"
    ).fetchall():
        print(f"-- {r['type'].upper()}: {r['name']}")
        if r["sql"]:
            print(r["sql"])
        print()
    conn.close()


def cmd_observations(args):
    _, db_path, name = _resolve_db(args.project)
    _require_db_path(db_path)
    db = MemoryDB(db_path)
    pid = db.upsert_project(args.project)
    obs = db.get_recent_observations(pid, limit=args.limit)
    print(f"\nObservations for {name} ({len(obs)} shown):\n")
    if not obs:
        print("  (none)")
        return
    for o in obs:
        ts = (o["timestamp"] or "")[:19]
        inp = _trunc(o.get("tool_input", ""), 80)
        print(f"  {ts}  [{o['tool_name']:<8}]  {inp}")


def cmd_mode(args):
    _, db_path, _ = _resolve_db(args.project)
    _require_db_path(db_path)
    db = MemoryDB(db_path)
    pid = db.upsert_project(args.project)
    if args.mode_name:
        from core.modes import VALID_MODES
        if args.mode_name not in VALID_MODES:
            print(f"Invalid mode '{args.mode_name}'. "
                  f"Choose from: {', '.join(sorted(VALID_MODES))}")
            sys.exit(1)
        db.set_project_mode(pid, args.mode_name)
        print(f"Mode set to: {args.mode_name}")
    else:
        print(f"Current mode: {db.get_project_mode(pid)}")
        from core.modes import list_modes
        print("\nAvailable modes:")
        for m in list_modes():
            print(f"  {m['name']:<12} {m['description']}")


def cmd_progress(args):
    """Regenerate PROGRESS.md from DB and print a preview."""
    from core.progress import write_progress_md
    memory_dir, db_path, name = _resolve_db(args.project)
    if not db_path.exists():
        print(f"Error: no memory database at {db_path}")
        sys.exit(1)
    db = MemoryDB(db_path)
    pid = db.upsert_project(args.project)
    try:
        progress_path = write_progress_md(db, pid, memory_dir)
        print(f"\nRegenerated: {progress_path}\n")
        print(progress_path.read_text(encoding="utf-8"))
    except OSError as e:
        # why: this was the ONLY write_progress_md call site with no handler —
        # stop.py, user_prompt.py and mcp/server.py all wrap theirs. It could
        # already raise on a read-only PROGRESS.md, and v2.5.2's atomic write
        # adds a second, rarer trigger (a transient Windows sharing violation
        # when another process holds the file open). An unhandled traceback out
        # of `/cc-mem progress` is a worse answer than a diagnosis, and the
        # previous COMPLETE PROGRESS.md is still on disk either way.
        print(f"Error: could not rewrite PROGRESS.md ({e})")
        print(f"       The previous {memory_dir / 'PROGRESS.md'} is unchanged.")
        sys.exit(1)


def cmd_supersedes(args):
    """Show the supersede chain for a memory ID.

    Refuses a foreign id for the same reason `cmd_archive` does three
    functions down: `memories.id` is global to the DB file, and this command
    used to print the FULL content of another project's memory under this
    project's --project — into a Claude session's context, since /cc-mem
    output is a render path.
    """
    _, db_path, _ = _resolve_db(args.project)
    _require_db_path(db_path)
    db = MemoryDB(db_path)
    pid = db.upsert_project(str(Path(args.project).resolve()))
    head = db.get_memory(args.memory_id)
    if head is not None and head["project_id"] != pid:
        print(f"Error: memory {args.memory_id} belongs to a different project")
        sys.exit(1)
    chain = db.get_supersede_chain(args.memory_id)
    if not chain:
        print(f"No memory with id {args.memory_id}")
        sys.exit(1)
    print(f"\nSupersede chain for #{args.memory_id} ({len(chain)} versions, newest first):\n")
    for i, m in enumerate(chain):
        active = "ACTIVE" if m["is_active"] else "archived"
        when = (m.get("updated_at") or "")[:16]
        print(f"  v{len(chain)-i}  #{m['id']:4d}  [{active}]  {when}  "
              f"{m['category']:<10}  {_trunc(m['content'], 70)}")


def cmd_archive(args):
    """Retire memories by id — archive (recoverable), never DELETE.

    This closes a genuine dead end, not a convenience gap. When a stored
    memory turns out to be WRONG the user had no supported way to retire it:
    `sql` is read-only by design, `add` reconciles only when the new text
    scores similar enough to the old, and — before v2.8.0's CJK-aware
    substrate — a Chinese correction of a Chinese fact scored 0.23 and was
    INSERTED beside the thing it corrected. The only remaining route was to
    bypass the CLI entirely and call `db.bulk_archive` by hand, which is how
    this maintainer actually did it (ids 291/293/294 on a live database).

    Archive, matching `cleanup_garbage` and `core/db.py`'s standing rule that
    every retirement path keeps the row: a hard DELETE strands any
    `supersedes_id` pointing at it, and the chain becomes unwalkable.
    `--supersedes` records WHICH memory replaced these, so the lineage stays
    traceable exactly as an ordinary supersede would.
    """
    _, db_path, _ = _resolve_db(args.project)
    _require_db_path(db_path)
    db = MemoryDB(db_path)
    pid = db.upsert_project(str(Path(args.project).resolve()))

    rows, foreign, missing, already = [], [], [], []
    for mid in args.memory_ids:
        row = db.get_memory(mid)
        if row is None:
            missing.append(mid)
        elif row["project_id"] != pid:
            # `memories.id` is global to the DB file, exactly like `plans.id`
            # — the defect v2.5.3 closed on the three plan mutators. A bare
            # id from another project must not be archivable through this
            # project's --project.
            foreign.append(mid)
        elif not row["is_active"]:
            already.append(mid)
        else:
            rows.append(row)

    for label, ids in (("no such memory", missing),
                       ("belongs to a different project", foreign),
                       ("already archived", already)):
        if ids:
            print(f"  [skip] {label}: {ids}")
    if not rows:
        print("Nothing to archive.")
        sys.exit(1 if (missing or foreign) else 0)

    canonical = None
    if args.supersedes is not None:
        if args.supersedes in {r["id"] for r in rows} \
                or args.supersedes in set(args.memory_ids):
            # `archive N --supersedes N` used to print "[archived] 0 row(s)"
            # and exit 0 — archive_obsolete filters the canonical out of its
            # id list, so the command reported success while doing nothing
            # (register Y4). A row cannot supersede itself; refuse loudly.
            print(f"Error: --supersedes {args.supersedes} is in the list "
                  f"being archived — a memory cannot supersede itself")
            sys.exit(1)
        canonical = db.get_memory(args.supersedes)
        if canonical is None or canonical["project_id"] != pid:
            print(f"Error: --supersedes {args.supersedes} is not a memory of "
                  f"this project")
            sys.exit(1)
        if not canonical["is_active"]:
            print(f"Error: --supersedes {args.supersedes} is itself archived; "
                  f"pointing lineage at a retired row hides both")
            sys.exit(1)

    print(f"\nArchiving {len(rows)} memor{'y' if len(rows) == 1 else 'ies'}"
          + (f", superseded by #{args.supersedes}" if canonical else "") + ":\n")
    for row in rows:
        print(f"  #{row['id']:4d}  {row['category']:<10} "
              f"{_trunc(row['content'], 66)}")
    n = db.archive_obsolete([r["id"] for r in rows],
                            canonical_id=args.supersedes)
    memory_dir = db_path.parent
    try:
        from llm.memory_writer import regenerate_memory_index
        regenerate_memory_index(db, pid, memory_dir)
    except Exception as exc:
        # why: MEMORY.md is a projection of rows that are already committed;
        # a stale index is a cosmetic problem, and the next write refreshes
        # it. Reporting it beats failing the archive that did happen.
        print(f"  [warn] MEMORY.md not regenerated ({exc})")
    print(f"\n[archived] {n} row(s), is_active=0 — recoverable. "
          f"`/cc-mem supersedes <id>` still walks the chain.")
    if n == 0:
        # archive_obsolete now reports rows ACTUALLY archived; zero after the
        # validations above means a concurrent writer got there first — an
        # outcome, but not the requested one (register Y4's other half).
        sys.exit(1)


def cmd_summary(args):
    _, db_path, name = _resolve_db(args.project)
    _require_db_path(db_path)
    db = MemoryDB(db_path)
    pid = db.upsert_project(args.project)
    s = db.get_latest_summary(pid)
    if not s:
        print("No session summary available.")
        return
    print(f"\n{'='*50}\n  Session Summary for {name}\n{'='*50}\n")
    for field in ["request", "investigated", "learned", "completed", "next_steps", "notes"]:
        val = s.get(field, "")
        if val:
            print(f"  {field.title()}: {val}")


def cmd_serve(args):
    from ui.web_viewer import main as web_main
    sys.argv = ["web_viewer.py", "--project", args.project, "--port", str(args.port)]
    # web_viewer.main() defines --no-open and honors it; not forwarding it meant
    # every `/cc-mem serve` force-opened a browser tab, including when Claude
    # Code ran the command itself.
    if getattr(args, "no_open", False):
        sys.argv.append("--no-open")
    web_main()


# ── /cc-mem plan-* subcommands (v2.2) ──────────────────────────────────────

def _plan_db(project):
    """Open the project DB. Returns (db, pid, memory_dir).

    No mkdir: db_path.exists() implies memory/ exists, so the old
    `memory_dir.mkdir(...)` after the sys.exit(1) was dead code.
    """
    memory_dir, db_path, _ = _resolve_db(project)
    if not db_path.exists():
        print(f"Error: no memory database at {db_path}")
        sys.exit(1)
    db = MemoryDB(db_path)
    pid = db.upsert_project(str(Path(project).resolve()))
    return db, pid, memory_dir


def cmd_plan_show(args):
    """Regenerate memory/PLAN.md from SQL and print it. No-op-safe if no plan."""
    from core.plan import write_plan_md
    db, pid, memory_dir = _plan_db(args.project)
    path = write_plan_md(db, pid, memory_dir)
    print(path.read_text(encoding="utf-8"))


def _print_raw_plan(raw, max_lines=20):
    lines = raw.splitlines() or ["(empty)"]
    for line in lines[:max_lines]:
        print(f"    | {line}")
    if len(lines) > max_lines:
        print(f"    | ... ({len(lines) - max_lines} more line(s))")


def cmd_plan_status(args):
    """One-screen summary of the live plan: counters, active step, freshness."""
    from core.plan import is_live_plan, is_valid_structured
    db, pid, _ = _plan_db(args.project)
    # is_live_plan, NOT truthiness: `plan-clear` keeps a TOMBSTONE row, so a
    # bare `if not row` fell through to the "raw plan captured" branch below and
    # reported `Raw plan captured (0 chars) - NOT yet refined` for a plan the
    # user had just deliberately dropped. Same root as the Stop hook's.
    row = db.get_plan_active(pid)
    if not is_live_plan(row):
        print("No active plan. Enter Claude's plan mode or run "
              "`/cc-mem plan-set --raw '<text>'`.")
        return
    structured = row.get("structured") or {}
    raw = (row.get("raw") or "").strip()
    refined_ok = is_valid_structured(structured)

    # FRESHNESS FIRST. `needs_refine` is set by capture_exit_plan_mode (the
    # PostToolUse ExitPlanMode path AND `plan-set --raw`) and cleared ONLY by
    # apply_refined_plan. Testing is_valid_structured() first meant a brand-new
    # raw plan laid over a refined one printed the OLD goal and the new text was
    # invisible in plan-status, plan-show and PLAN.md alike.
    if row.get("needs_refine"):
        if refined_ok:
            print("[!] PENDING REFINEMENT — a NEWER raw plan is stored. "
                  "The structured plan below is the PREVIOUS, now-stale one.")
        else:
            print("[!] PENDING REFINEMENT — raw plan captured, not yet refined.")
        print(f"  Raw plan ({len(raw)} chars), last update {row.get('updated_at')}:")
        _print_raw_plan(raw)
        print("  Next step: invoke the @plan-refiner subagent, then "
              "`/cc-mem plan-set --from-refiner` with its JSON output.")
        if not refined_ok:
            return
        print()
        print("  --- previous (stale) structured plan ---")
    elif not refined_ok:
        print(f"Raw plan captured ({len(raw)} chars) — NOT yet refined.")
        print(f"  Last update: {row.get('updated_at')}")
        print("  Next step: invoke the @plan-refiner subagent, then "
              "`/cc-mem plan-set --from-refiner` with its JSON output.")
        return

    steps = structured.get("steps", [])
    n_done = sum(1 for s in steps if s.get("status") == "done")
    print(f"Goal: {structured.get('goal')}")
    print(f"Progress: {n_done}/{len(steps)} steps done · active step #{row.get('active_step', 0)}")
    print(f"Refined: {row.get('last_refined_at') or '(never)'} "
          f"by {structured.get('refined_by', 'unknown')}")
    print(f"Last guardian check: {row.get('last_guardian_at') or '(never)'}")
    print(f"Counters since last check: {row.get('turns_since_last_guardian', 0)} turns, "
          f"{row.get('edits_since_last_guardian', 0)} edits")


def cmd_plan_set(args):
    """Three mutually exclusive modes:
      --from-refiner   : read JSON structured plan from stdin, store it
      --raw '<text>'   : capture a raw plan, mark needs_refine=1
      --raw-file FILE  : same, but read raw from a file
    """
    from core import plan as plan_mod
    db, pid, memory_dir = _plan_db(args.project)

    if args.from_refiner:
        try:
            structured = json.loads(sys.stdin.read())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[FAIL] stdin is not valid JSON: {e}")
            sys.exit(1)
        # why: the R610 gate guards `steps` only. Snapshot the outgoing plan
        # BEFORE the replacement so the advisory below can name the criteria
        # that did not obviously survive — afterwards the old plan is out of
        # the slot and lives on only in memory/.plan_history/.
        outgoing = (db.get_plan_active(pid) or {}).get("structured") or {}
        try:
            result = plan_mod.apply_refined_plan(db, pid, structured,
                                                 memory_dir=memory_dir)
        except (ValueError, TypeError, AttributeError, KeyError, IndexError) as e:
            # why: a refiner subagent is an LLM. A top-level JSON array, a
            # missing key or a wrong-typed field is a realistic failure mode and
            # must surface as a CLI error, not a raw traceback.
            print(f"[FAIL] refined plan rejected: {type(e).__name__}: {e}")
            sys.exit(1)
        print(f"[OK] Plan stored — goal: {result['goal']!r}")
        print(f"     {len(result['steps'])} steps · PLAN.md regenerated at "
              f"{memory_dir / 'PLAN.md'}")
        lost = plan_mod.unmatched_criteria(outgoing, structured)
        if lost:
            total = len([c for c in (outgoing.get("success_criteria") or [])
                         if isinstance(c, str) and c.strip()])
            print(f"[!] carryover advisory — {len(lost)} of {total} previous "
                  f"success_criteria have no close match in the replacement.")
            print("    The R610 gate covers `steps`, so these did not block "
                  "the write. Retiring a criterion is fine; losing one "
                  "silently is not. Confirm each was deliberate:")
            for c in lost:
                one = " ".join(c.split())
                print(f"      - {one[:150]}{'…' if len(one) > 150 else ''}")
            print("    `context` is free text and is NOT compared at all — "
                  "re-read it yourself. The outgoing plan is archived under "
                  "memory/.plan_history/.")
        return

    if args.raw_file:
        try:
            raw = Path(args.raw_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"[FAIL] cannot read --raw-file {args.raw_file}: {e}")
            sys.exit(1)
    elif args.raw is not None:
        raw = args.raw
    else:
        print("[FAIL] need one of: --from-refiner, --raw '<text>', --raw-file PATH")
        sys.exit(1)

    # Report what is actually STORED. core.plan.capture_exit_plan_mode strips
    # and drops whitespace-only text, so `--raw "   "` used to print
    # "[OK] Raw plan captured (3 chars)" with nothing stored, and a trailing
    # newline made the count disagree with plan-status by one.
    raw = raw.strip()
    if not raw:
        print("[FAIL] the plan text is empty (whitespace only) — nothing stored.")
        sys.exit(1)
    stored = plan_mod.capture_exit_plan_mode(db, pid, raw, memory_dir=memory_dir)
    if stored is not None and not stored:
        print("[FAIL] core.plan refused the plan text — nothing stored.")
        sys.exit(1)
    print(f"[OK] Raw plan captured ({len(raw)} chars) at memory/.plan_raw.md.")
    print("     Next: invoke @plan-refiner subagent, then "
          "`/cc-mem plan-set --from-refiner < <its-json-output>`.")


def cmd_plan_clear(args):
    from core.plan import archive_plan, is_live_plan, unfinished_steps
    db, pid, memory_dir = _plan_db(args.project)
    row = db.get_plan_active(pid)
    # a TOMBSTONE is not a plan: clearing twice used to archive an empty one
    if not is_live_plan(row):
        print("No active plan to clear.")
        return
    # R610 carryover gate (clear face): dropping a plan that still has
    # unfinished steps without a recorded reason is the silent-sink
    # failure mode. Mandatory --reason; archived append-only either way.
    pending = unfinished_steps(row.get("structured") or {})
    reason = (getattr(args, "reason", "") or "").strip()
    if pending and not reason:
        print(f"[FAIL] carryover gate: the active plan still has "
              f"{len(pending)} unfinished step(s):")
        for s in pending:
            print(f"    - #{s.get('id')} {s.get('title')}")
        print("  Clearing would silently sink them. Re-run with "
              "--reason \"<why these steps are being dropped>\" -- the "
              "reason is recorded in memory/.plan_history/.")
        sys.exit(1)
    archive_plan(row, memory_dir, event="clear", reason=reason)
    db.clear_plan_active(pid)
    plan_md = memory_dir / "PLAN.md"
    if plan_md.exists():
        plan_md.unlink()
    raw_md = memory_dir / ".plan_raw.md"
    if raw_md.exists():
        raw_md.unlink()
    print("[OK] Active plan cleared (archived to memory/.plan_history/).")


def cmd_plan_replan(args):
    """Force a re-refine: re-arm needs_refine=1 against the current raw text.
    No-op if no raw is stored."""
    db, pid, memory_dir = _plan_db(args.project)
    row = db.get_plan_active(pid)
    if not row or not (row.get("raw") or "").strip():
        print("[FAIL] no raw plan stored. Use `/cc-mem plan-set --raw` first.")
        sys.exit(1)
    db.upsert_plan_active(pid, needs_refine=1)
    # Same reason as core/plan.py's writer: the refiner subagent Reads this
    # file from another process, and write_text truncates before it writes.
    from core.atomic import write_atomic, _DERIVED_BUDGET_S
    write_atomic(memory_dir / ".plan_raw.md", row["raw"],
                 budget_s=_DERIVED_BUDGET_S)
    print("[OK] Marked for re-refining. Invoke @plan-refiner subagent on "
          "memory/.plan_raw.md, then `/cc-mem plan-set --from-refiner`.")


def cmd_plan_check(args):
    """Recommend a guardian check. Resets the guardian counters so the next
    nudge waits a full interval. The CLI itself doesn't invoke the subagent —
    the calling Claude is expected to follow up with `Task(...)`."""
    from core.plan import (is_live_plan, is_valid_structured,
                           raw_pending_refinement, write_plan_md)
    db, pid, memory_dir = _plan_db(args.project)
    row = db.get_plan_active(pid)
    # a TOMBSTONE is not a plan: checking one reset counters for nothing
    if not is_live_plan(row):
        print("No active plan to check.")
        return
    # FRESHNESS FIRST. core.plan.raw_pending_refinement's contract is that every
    # live-plan renderer consults it BEFORE the structured form; plan-check was
    # the one surface that never did. Testing is_valid_structured() alone
    # briefed the guardian on the PREVIOUS, now-superseded plan — printing its
    # goal and progress while `plan-status` and the PLAN.md this command had
    # just rewritten both said "stale" — and reset the counters, silencing the
    # drift nudge for a plan nobody is working from. Refusing here has NO side
    # effects on purpose: PLAN.md was already regenerated by whichever path
    # stored the raw text (capture_exit_plan_mode).
    if raw_pending_refinement(row):
        print("[!] PENDING REFINEMENT — a NEWER raw plan is stored, so the "
              "structured plan is stale. Nothing to check yet.")
        print("    Guardian counters NOT reset: a drift check against a "
              "superseded plan is meaningless, and resetting would silence "
              "the nudge for the plan that is actually live.")
        print("    Next: invoke the @plan-refiner subagent on "
              "memory/.plan_raw.md, then `/cc-mem plan-set --from-refiner`, "
              "then re-run `/cc-mem plan-check`.")
        return
    if not is_valid_structured(row.get("structured")):
        print("No refined plan to check.")
        return
    # Refresh PLAN.md so the guardian subagent reads the current state
    write_plan_md(db, pid, memory_dir)
    db.reset_plan_guardian_counters(pid)
    steps = row["structured"]["steps"]
    print(f"PLAN guardian check requested.")
    print(f"  Goal: {row['structured']['goal']}")
    print(f"  Progress: {sum(1 for s in steps if s['status']=='done')}/{len(steps)} done")
    print(f"  Active step: #{row.get('active_step', 0)}")
    print()
    print("Now invoke the plan-guardian subagent:")
    print("  Task(subagent_type='plan-guardian', prompt='Read memory/PLAN.md and")
    print("       memory/PROGRESS.md, compare against the last 20 messages of this")
    print("       session, and report: (a) current step alignment, (b) any drift,")
    print("       (c) recommended next action. ≤150 words.')")


def cmd_directive_list(args):
    """Standing user directives, loudest (most-repeated) first."""
    db, pid, _memory_dir = _plan_db(args.project)
    status = None if args.status == "all" else args.status
    rows = db.list_directives(pid, status=status)
    if not rows:
        print(f"No {args.status} directives recorded.")
        print("  Record one: /cc-mem directive-add <slug> --demand '...' "
              "--quote '<the user's words>'")
        return
    print(f"{len(rows)} {args.status} directive(s), most-repeated first:\n")
    for row in rows:
        mark = {"active": "[ ]", "done": "[x]"}.get(row["status"], "[~]")
        print(f"  {mark} {row['slug']}  ×{row['times_stated']}  "
              f"({row['kind']})")
        if row.get("demand"):
            print(f"        {row['demand'][:96]}")
        if row.get("quote"):
            print(f"        原话: {row['quote'][:88]}")
        if row.get("evidence"):
            print(f"        证据: {row['evidence'][:88]}")


def cmd_directive_add(args):
    """Record a directive. Re-adding the same slug bumps its repetition count
    rather than creating a second row — the count IS the importance signal."""
    db, pid, _memory_dir = _plan_db(args.project)
    # ONLY the flags actually supplied. `upsert_directive` keeps any field it
    # is not given, but argparse defaults are '' / 'standing', not None — so
    # re-stating a directive with `directive-add <slug>` and no flags passed
    # three non-None values and WIPED the quote and the demand and reset the
    # kind. Re-statement is the one operation this ledger exists for: the row
    # that survived carried a repetition count and no longer said what the
    # user had asked for. Sentinel defaults keep "omitted" distinguishable
    # from "explicitly set to empty".
    fields = {}
    if args.quote:
        fields["quote"] = args.quote
    if args.demand:
        fields["demand"] = args.demand
    if args.kind is not None:
        fields["kind"] = args.kind
    if args.times is not None:
        fields["times_stated"] = args.times
    action = db.upsert_directive(pid, args.slug, **fields)
    row = [r for r in db.list_directives(pid) if r["slug"] == args.slug][0]
    print(f"[OK] directive {action}: {args.slug} "
          f"(stated ×{row['times_stated']}, {row['kind']}, {row['status']})")


def cmd_directive_close(args):
    """Close a directive. Evidence is MANDATORY: a directive closed on an
    assertion is exactly the failure this ledger exists to prevent — the point
    is not to record that someone believed it was done."""
    if not args.evidence.strip():
        print("[FAIL] --evidence is required to close a directive.")
        print("       Give something checkable: a commit sha, file:line, or "
              "a gate name. 'done' on its own is what let a demand stated six "
              "times reach zero implementation.")
        sys.exit(1)
    db, pid, _memory_dir = _plan_db(args.project)
    n = db.set_directive_status(pid, args.slug, args.status,
                                evidence=args.evidence)
    if not n:
        print(f"[FAIL] no directive named {args.slug!r} in this project.")
        sys.exit(1)
    print(f"[OK] {args.slug} -> {args.status} (evidence recorded)")


def cmd_dashboard(args):
    """Launch the Tkinter dashboard for this project.

    Resolves dashboard.py relative to this file so it works under both
    marketplace install (plugin tree in source repo) and standalone install
    (~/.claude/hooks/cc-memory/), without env vars or hardcoded paths.
    """
    import subprocess
    dashboard_py = Path(__file__).resolve().parent.parent / "ui" / "dashboard.py"
    if not dashboard_py.exists():
        print(f"[FAIL] dashboard.py not found at expected location: {dashboard_py}")
        sys.exit(1)
    cmd = [sys.executable, str(dashboard_py), "--project", args.project]
    # Detach properly. Popen(cmd) alone lets the GUI child INHERIT this
    # process's stdout/stderr pipes, so any caller that captures output —
    # which is exactly how Claude Code runs the CLI — blocks until the user
    # closes the window, long after mem.py itself has exited.
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    print(f"[OK] Launched dashboard for project: {args.project}")


# ── observability + encoding (v2.3) ─────────────────────────────────────────

def cmd_inject_show(args):
    """Dump memory/.last_inject.json — exactly what the last SessionStart
    injected (ground truth, independent of whether Claude echoed it)."""
    memory_dir, db_path, _ = _resolve_db(args.project)
    manifest = memory_dir / ".last_inject.json"
    if not manifest.exists():
        print("No inject manifest yet. It is written on the next SessionStart "
              "(open a new Claude Code session in this project).")
        return
    data = json.loads(manifest.read_text(encoding="utf-8"))
    print(f"Last injection — session {data.get('session_id','?')[:8] or '(none)'} "
          f"at {data.get('ts','?')}")
    print(f"  memories injected : {data.get('n_injected_memories', 0)} "
          f"(critical={len(data.get('critical_ids', []))}, "
          f"timeline={len(data.get('timeline_ids', []))})")
    print(f"  topics            : {', '.join(data.get('topic_names', [])) or '(none)'}")
    print(f"  PROGRESS.md preview: {'yes' if data.get('progress_preview_included') else 'no'}")
    print(f"  size              : {data.get('total_chars', 0)} chars "
          f"(~{data.get('est_tokens', 0)} tokens)")
    if data.get("critical_ids"):
        print(f"  critical ids      : {data['critical_ids']}")
    if data.get("timeline_ids"):
        print(f"  timeline ids      : {data['timeline_ids']}")


def cmd_inject_usage(args):
    """Deterministic read-signals: did Claude actually consult the injected
    context this session? Reports ONLY signals that can't be faked:
      - Read-tool observations targeting PROGRESS.md / MEMORY.md
      - whether the forced-reminder ack string appears in the latest turn
    (No per-memory #id guessing — ids are never shown to Claude, so that
    would be a coincidence detector, not real usage.)"""
    memory_dir, db_path, _ = _resolve_db(args.project)
    _require_db_path(db_path)
    db = MemoryDB(db_path)
    pid = db.upsert_project(str(Path(args.project).resolve()))
    obs = db.get_recent_observations(pid, limit=200)
    reads = [o for o in obs if o["tool_name"] == "Read" and o["tool_input"]]
    progress_reads = [o for o in reads if "PROGRESS.md" in o["tool_input"]]
    memory_reads = [o for o in reads if "MEMORY.md" in o["tool_input"]]
    print("cc-memory usage signals (this project, recent observations):")
    print(f"  PROGRESS.md Read by Claude : {len(progress_reads)} time(s)"
          + (f" — last {progress_reads[0]['timestamp']}" if progress_reads else ""))
    print(f"  MEMORY.md   Read by Claude : {len(memory_reads)} time(s)"
          + (f" — last {memory_reads[0]['timestamp']}" if memory_reads else ""))
    if not (progress_reads or memory_reads):
        print("  (no handoff-file reads observed — either a fresh session, or "
              "Claude hasn't consulted the injected context yet)")
    man = memory_dir / ".last_inject.json"
    if man.exists():
        d = json.loads(man.read_text(encoding="utf-8"))
        print(f"  last injection             : {d.get('n_injected_memories', 0)} memories "
              f"at {d.get('ts','?')} (see `/cc-mem inject-show`)")


# Tables scanned by encoding-check, with the text columns that matter. The
# table names come from THIS literal dict, never from user input, so they are
# safe to interpolate; every value stays parameterized.
_ENCODING_SCAN = {
    "memories": ["content", "topic"],
    "topics": ["name", "content"],
    "progress": ["current_request", "plan", "status_done", "status_in_flight"],
    "observations": ["tool_input", "tool_output"],
}
# `memories` is the only table encoding-check can act on, and --apply
# quarantines by flipping is_active=0. Scanning archived rows too meant a
# quarantined row was re-reported forever and "Re-run with --apply" never
# stopped being printed — the command could not converge.
# AND-form: every scan query now leads with `WHERE project_id = ?` (all four
# tables carry the column), so this predicate composes onto it.
_ENCODING_SCAN_PREDICATE = {"memories": " AND is_active = 1"}


def _count_fffd(row, cols):
    n = 0
    for c in cols:
        try:
            v = row[c] or ""
        except (IndexError, KeyError):
            # why: the column list is per-table but schemas migrate; a missing
            # column must not abort the scan of the columns that do exist
            continue
        n += v.count("�")
    return n


def cmd_encoding_check(args):
    """Read-only scan for genuine U+FFFD corruption across text tables.

    On a healthy DB this finds 0 in memories/topics/progress (CJK/unicode is
    NOT corruption). Any FFFD in observations is transient (cleaned after
    extraction) and harmless. With --apply, quarantines (is_active=0) ONLY
    corrupted ACTIVE memory rows — never guesses a repair."""
    _, db_path, _ = _resolve_db(args.project)
    _require_db_path(db_path)
    db = MemoryDB(db_path)
    pid = db.upsert_project(str(Path(args.project).resolve()))
    corrupt_memory_ids = []
    quarantined_ids = []
    with db._connect() as conn:
        for table, cols in _ENCODING_SCAN.items():
            # `WHERE project_id = ?` on every table: this scan used to run
            # unpredicated, so the counts summed EVERY project in the file and
            # --apply below archived another project's rows by bare id — the
            # cmd_archive guard ("memories.id is global to the DB file")
            # existed three functions up while the one command that WRITES
            # skipped it. `pid` was resolved and then never used.
            q = (f"SELECT * FROM {table} WHERE project_id = ?"
                 + _ENCODING_SCAN_PREDICATE.get(table, ""))
            try:
                rows = conn.execute(q, (pid,)).fetchall()
            except sqlite3.Error:
                # why: a table absent on an older schema version is not a
                # failure of the scan — report the tables that do exist
                continue
            total = 0
            hit_rows = 0
            for r in rows:
                cnt = _count_fffd(r, cols)
                total += cnt
                if cnt:
                    hit_rows += 1
                    if table == "memories":
                        corrupt_memory_ids.append(r["id"])
            scope = " (active only)" if table in _ENCODING_SCAN_PREDICATE else ""
            print(f"  {table:14s}{scope} rows={len(rows):4d}  "
                  f"U+FFFD chars={total}  rows_with_fffd={hit_rows}")

        try:
            for r in conn.execute(
                "SELECT * FROM memories WHERE is_active = 0 AND project_id = ?",
                (pid,)
            ).fetchall():
                if _count_fffd(r, _ENCODING_SCAN["memories"]):
                    quarantined_ids.append(r["id"])
        except sqlite3.Error:
            # why: same as above — an old schema without is_active just means
            # there is nothing already-quarantined to report
            pass

    if quarantined_ids:
        print(f"\nAlready quarantined (is_active=0) corrupted rows: "
              f"{quarantined_ids} — no action needed, recoverable.")
    if corrupt_memory_ids:
        print(f"\nCorrupted ACTIVE memory rows: {corrupt_memory_ids}")
        if args.apply:
            n = db.archive_obsolete(corrupt_memory_ids)
            print(f"[applied] quarantined {n} corrupted memory rows "
                  f"(is_active=0, recoverable). Re-running encoding-check "
                  f"now reports 0 active corrupted rows.")
        else:
            print("Re-run with --apply to quarantine them (is_active=0, recoverable).")
    else:
        print("\nNo corrupted ACTIVE memory/topic/progress rows. "
              "Any observation FFFD is transient + harmless.")


class _EscapingParser(argparse.ArgumentParser):
    """argparse, routed through this module's escaping `print`.

    `_print_message` is argparse's single output funnel — usage, --help and
    the `error()` path all reach the stream through it — so overriding it
    covers every message argparse can emit, including ones added by a future
    stdlib version. Overriding `error()` alone would not.

    It matters because argparse ECHOES the offending argument: an invalid
    subcommand spelled `<system-reminder>PWN</system-reminder>` was reproduced
    printing that tag live, and `commands/cc-mem.md` hands this command's
    output straight back to Claude to summarise. The module's `print` writes to
    stdout, which is also where the rest of this CLI's output goes; nothing
    parses these streams (verified: no hook, skill or agent subprocesses
    `/cc-mem`), so merging them costs nothing and closes the bypass.
    """

    def _print_message(self, message, file=None):
        if message:
            print(message, end="")


def make_parser():
    p = _EscapingParser(prog="cc-memory",
        description=f"cc-memory CLI v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True, help="Project root path")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Health check")
    sub.add_parser("stats", help="Database statistics")

    pl = sub.add_parser("list", help="List memories")
    pl.add_argument("category", nargs="?", default="all",
                    choices=["all"] + list(CATEGORIES))
    pl.add_argument("--limit", type=_bounded_limit, default=20)
    pl.add_argument("--sessions", type=int, default=5)

    ps = sub.add_parser("search", help="Search memories")
    ps.add_argument("query")

    sub.add_parser("sessions", help="List sessions")

    pq = sub.add_parser("sql", help="Raw SQL query (READ-ONLY: SELECT/PRAGMA/"
                                    "EXPLAIN/WITH...SELECT)")
    pq.add_argument("query")

    pa = sub.add_parser("add", help="Add memory (anti-patch upsert)")
    pa.add_argument("category",
                    choices=list(CATEGORIES))
    pa.add_argument("content")
    pa.add_argument("--importance", type=int, default=3, choices=range(1, 6), metavar="1-5")
    pa.add_argument("--tags", default="manual")
    pa.add_argument("--topic", default="", help="Topic tag (e.g. auth, ui, pipeline)")

    sub.add_parser("keywords", help="Project keyword vocabulary")
    sub.add_parser("topics", help="Show all topic summaries")

    pc = sub.add_parser("consolidate", help="Full consolidation (LLM-backed)")
    pc.add_argument("--no-llm", action="store_true")

    sub.add_parser("cleanup", help="Lightweight no-LLM cleanup")
    sub.add_parser("schema", help="Show DB schema")
    sub.add_parser("progress", help="Regenerate memory/PROGRESS.md from DB")

    psup = sub.add_parser("supersedes", help="Show supersede chain for a memory ID")
    psup.add_argument("memory_id", type=int)

    par = sub.add_parser(
        "archive",
        help="Retire memories by id (is_active=0, recoverable — never DELETE)")
    par.add_argument("memory_ids", type=int, nargs="+",
                     help="Memory ids to archive")
    par.add_argument("--supersedes", type=int, default=None, metavar="ID",
                     help="Record that this memory replaces the archived ones "
                          "(sets supersedes_id, keeping the chain walkable)")

    po = sub.add_parser("observations", help="List recent tool observations")
    po.add_argument("--limit", type=_bounded_limit, default=30)

    pm = sub.add_parser("mode", help="Show/set project mode")
    pm.add_argument("mode_name", nargs="?", default=None)

    sub.add_parser("summary", help="Latest session summary")

    pv = sub.add_parser("serve", help="Launch web dashboard")
    pv.add_argument("--port", type=int, default=9377)
    pv.add_argument("--no-open", action="store_true",
                    help="Don't open a browser tab (forwarded to web_viewer)")

    sub.add_parser("dashboard", help="Launch the Tkinter GUI dashboard")

    # ── plan-* subcommands (v2.2) ──────────────────────────────────────────
    sub.add_parser("plan-show", help="Regenerate memory/PLAN.md and print it")
    sub.add_parser("plan-status", help="Show plan counters / freshness summary")

    pps = sub.add_parser("plan-set", help="Set a raw or refined plan")
    # Mutually exclusive: --from-refiner used to silently win over --raw, so a
    # caller passing both got neither an error nor the plan it supplied.
    pps_mode = pps.add_mutually_exclusive_group()
    pps_mode.add_argument("--raw", help="Raw plan markdown/text (verbatim)")
    pps_mode.add_argument("--raw-file", help="Read raw plan from a file")
    pps_mode.add_argument("--from-refiner", action="store_true",
                          help="Read structured JSON plan from stdin (refiner output)")

    ppc = sub.add_parser(
        "plan-clear",
        help="Drop the active plan + delete PLAN.md (archived; unfinished "
             "steps require --reason — R610 carryover gate)")
    ppc.add_argument("--reason", default="",
                     help="Required when the plan has unfinished steps: why "
                          "they are being dropped (recorded in .plan_history)")
    sub.add_parser("plan-replan", help="Re-arm needs_refine on the current raw")
    sub.add_parser("plan-check",
                   help="Reset guardian counters + emit subagent invocation hint")

    # ── directive ledger (v2.11.0) ─────────────────────────────────────────
    # A directive is what the USER asked for; a plan step is what WE decided
    # to do about it. Keeping them in one table is how a demand stated six
    # separate times still reached zero implementation unnoticed — it was
    # never a step in whichever plan happened to be active.
    pdl = sub.add_parser("directive-list",
                         help="Standing user directives, most-repeated first")
    pdl.add_argument("--status", default="active",
                     choices=["active", "done", "superseded", "dropped", "all"],
                     help="Filter by status (default: active)")
    pda = sub.add_parser("directive-add",
                         help="Record a directive (re-adding bumps its count)")
    pda.add_argument("slug", help="Stable short id, e.g. pause-on-quota")
    pda.add_argument("--quote", default="", help="The user's verbatim words")
    pda.add_argument("--demand", default="", help="What it requires, one line")
    # default=None, NOT "standing": these defaults are how a bare re-statement
    # (`directive-add <slug>`) used to overwrite a directive's kind. The INSERT
    # path in db.upsert_directive already falls back with `or "standing"`, so
    # a genuinely new directive still lands as standing.
    pda.add_argument("--kind", default=None,
                     choices=["standing", "feature", "process", "oneoff"],
                     help="Directive kind (default on creation: standing)")
    pda.add_argument("--times", type=int, default=None,
                     help="Set the repetition count outright (transcript audit)")
    pdc = sub.add_parser("directive-close",
                         help="Close a directive — evidence is mandatory")
    pdc.add_argument("slug")
    pdc.add_argument("--evidence", default="",
                     help="Where it landed: commit / file:line / gate name")
    pdc.add_argument("--status", default="done",
                     choices=["done", "superseded", "dropped"])

    # ── observability + encoding (v2.3) ────────────────────────────────────
    sub.add_parser("inject-show", help="Show what the last SessionStart injected")
    sub.add_parser("inject-usage", help="Deterministic signals: did Claude read the memory?")
    pe = sub.add_parser("encoding-check", help="Scan for U+FFFD corruption (read-only)")
    pe.add_argument("--apply", action="store_true",
                    help="Quarantine corrupted memory rows (is_active=0, recoverable)")

    return p


def _anchor_project(raw):
    """Resolve `--project` to the project ROOT, announcing any redirection.

    Every subcommand reads `args.project`, so anchoring once here is what
    makes "one project, one database" true at the CLI too. Until v2.7.0 the
    hooks anchored and the CLI did not, so `/cc-mem add` run from a
    subdirectory created exactly the stray database the hooks had just been
    taught to refuse — and rung 0 (an existing database is terminal) then
    pinned all six hooks <!--ce:hooks:asof--> to it permanently. The same gap
    made `status` report "No database" for a subdirectory it was using.

    The redirection is PRINTED, never silent: an explicit `--project` is an
    instruction, and quietly doing something else to it would be worse than
    the bug. Never raises — `project_root` cannot, and a failed import
    leaves the raw value in place.
    """
    try:
        from core.roots import anchor_project
    except Exception as exc:
        # why: the CLI must keep working even if the resolver cannot load;
        # the raw path is exactly the pre-v2.7.0 behaviour
        print(f"[cc-memory] project-root anchoring unavailable ({exc}); "
              f"using {raw} as given")
        return raw
    return anchor_project(raw, announce=lambda m: print(f"[cc-memory] {m}"))


def _refuse_if_excluded(project):
    """Print the standing refusal and return True when `project` is opted out.

    Exit 0, not an error code: an opt-out is a setting the user chose, not a
    failure. Fails OPEN only if `core.modes` itself cannot be imported, which
    would otherwise make an unrelated install problem look like an opt-out.
    """
    try:
        from core.modes import cli_opt_out_notice
    except Exception:
        # why: a CLI that cannot load the opt-out check must still work; the
        # hooks and the MCP server enforce it independently
        return False
    notice = cli_opt_out_notice(project)
    if notice:
        print(f"[cc-memory] {notice}")
        return True
    return False


def main():
    args = make_parser().parse_args()
    # `is not None`, NOT truthiness: `--project ""` is falsy, so the old guard
    # skipped anchoring entirely and every subcommand then resolved "" to the
    # CLI's own cwd — unanchored, which is the one thing this call prevents.
    if getattr(args, "project", None) is not None:
        # Opt-out BEFORE anchoring, exactly like the hooks: anchoring first
        # would resolve an excluded SUBDIRECTORY up to its unexcluded parent
        # and thereby serve a path the user opted out of.
        if _refuse_if_excluded(args.project):
            return
        args.project = _anchor_project(args.project)
    dispatch = {
        "status": cmd_status, "stats": cmd_stats, "list": cmd_list, "search": cmd_search,
        "sessions": cmd_sessions, "sql": cmd_sql, "add": cmd_add,
        "keywords": cmd_keywords, "topics": cmd_topics,
        "consolidate": cmd_consolidate, "cleanup": cmd_cleanup,
        "schema": cmd_schema, "progress": cmd_progress, "supersedes": cmd_supersedes,
        "archive": cmd_archive,
        "observations": cmd_observations, "mode": cmd_mode,
        "summary": cmd_summary, "serve": cmd_serve, "dashboard": cmd_dashboard,
        "plan-show": cmd_plan_show, "plan-status": cmd_plan_status,
        "plan-set": cmd_plan_set, "plan-clear": cmd_plan_clear,
        "plan-replan": cmd_plan_replan, "plan-check": cmd_plan_check,
        "directive-list": cmd_directive_list,
        "directive-add": cmd_directive_add,
        "directive-close": cmd_directive_close,
        "inject-show": cmd_inject_show, "inject-usage": cmd_inject_usage,
        "encoding-check": cmd_encoding_check,
    }
    try:
        dispatch[args.command](args)
    except FileNotFoundError as exc:
        # why: the project directory is GONE. `core.progress.ensure_memory_dir`
        # and `MemoryDB.__init__` refuse to recreate it (v2.8.0) and raise
        # instead — correct behaviour that reached the user as a 9-line
        # traceback from `/cc-mem add`, while `cli/plan.py` printed one clean
        # line for the identical case. The boundary belongs on the ONE line
        # every subcommand passes through, not on the subcommands that happen
        # to have been noticed: thirteen `MemoryDB(...)` sites in this file
        # alone can raise it, and a fourteenth would have been missed.
        print(f"Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
