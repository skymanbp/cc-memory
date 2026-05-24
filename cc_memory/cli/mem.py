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
import sqlite3
import sys
import textwrap
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio so `mem.py status / list / search` doesn't crash
# when memory content contains emoji / math glyphs on Windows gbk consoles.
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

from core.db import MemoryDB


def _resolve_db(project):
    p = Path(project).resolve()
    return p / "memory", p / "memory" / "memory.db", p.name


def _require_db(db_path):
    if not db_path.exists():
        print(f"Error: no memory database at {db_path}")
        sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _trunc(text, w=80):
    return text[:w-1] + "…" if len(text) > w else text


def _table(headers, rows):
    widths = [len(h) for h in headers]
    srows = []
    for row in rows:
        sr = [str(v) if v is not None else "" for v in row]
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


_REQUIRED_PLUGIN_FILES = [
    "cc_memory/core/db.py",
    "cc_memory/core/logger.py",
    "cc_memory/core/privacy.py",
    "cc_memory/core/modes.py",
    "cc_memory/core/progress.py",
    "cc_memory/core/encoding_setup.py",
    "cc_memory/hooks/pre_compact.py",
    "cc_memory/hooks/session_start.py",
    "cc_memory/hooks/stop.py",
    "cc_memory/hooks/post_tool_use.py",
    "cc_memory/hooks/user_prompt.py",
    "cc_memory/llm/memory_writer.py",
    "hooks/hooks.json",
]


def _detect_install_layouts():
    """Detect every cc-memory install layout active on this machine.

    A machine can have more than one (e.g. dev-checkout marketplace +
    a stale marketplace-cache entry). Returns a list of dicts, one per
    layout, with file-presence + hook-registration verdicts inside.

    Recognized:
      - marketplace-directory: extraKnownMarketplaces["cc-memory"].source.type
                               == "directory"; root = source.path (dev checkout)
      - marketplace-cache:     installed_plugins.json[cc-memory@cc-memory][0].installPath
      - legacy-install:        ~/.claude/hooks/cc-memory/cc_memory/ exists
    """
    layouts = []
    home = Path.home() / ".claude"

    settings = {}
    settings_path = home / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # why: settings.json being malformed is a recoverable diagnostic
            # state — we still want to report on the OTHER layouts (legacy,
            # cache) so the user can see they're broken too; default to {}
            settings = {}

    enabled_marketplace = bool(
        settings.get("enabledPlugins", {}).get("cc-memory@cc-memory", False)
    )

    mp_entry = settings.get("extraKnownMarketplaces", {}).get("cc-memory", {})
    mp_src = mp_entry.get("source", {})
    if mp_src.get("type") == "directory" or mp_src.get("source") == "directory":
        mp_path = mp_src.get("path")
        if mp_path:
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
        for entry in inst.get("plugins", {}).get("cc-memory@cc-memory", []):
            p = entry.get("installPath")
            if not p:
                continue
            root = Path(p)
            if not root.exists():
                layouts.append({
                    "layout": "marketplace-cache",
                    "root": root,
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

    legacy = home / "hooks" / "cc-memory"
    if (legacy / "cc_memory").exists():
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
    """Check plugin file presence + hook registration for one install root."""
    missing = [rel for rel in _REQUIRED_PLUGIN_FILES if not (root / rel).exists()]
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
                # at the LAYOUT-detection level we just report 0 hooks — the
                # downstream _print_layout_report will flag the mismatch
                hooks_registered = []
    elif hooks_via == "user-settings" and settings_dict is not None:
        hooks_block = settings_dict.get("hooks", {})
        for ev in ("PreCompact", "SessionStart", "Stop",
                   "PostToolUse", "UserPromptSubmit"):
            for mg in hooks_block.get(ev, []):
                if not isinstance(mg, dict):
                    continue
                for h in mg.get("hooks", []):
                    if "cc-memory" in (h.get("command") or ""):
                        hooks_registered.append(ev)
                        break
                if ev in hooks_registered:
                    break

    return {
        "layout": layout_name,
        "root": root,
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
    print(f"  [{files_tag}] {name} at {root}{enabled_tag}")
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


def cmd_status(args):
    memory_dir, db_path, name = _resolve_db(args.project)
    project = str(Path(args.project).resolve())

    print(f"\n{'='*55}\n  cc-memory v2.1 Status Check: {name}\n{'='*55}\n")

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

    active_layout = next((L for L in layouts
                          if "broken_reason" not in L
                          and L.get("plugin_files_ok")), None)

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
        sys.path.insert(0, str(active_layout["root"] / "cc_memory"))
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
    _, db_path, name = _resolve_db(args.project)
    conn = _require_db(db_path)
    print(f"\n{'='*50}\n  Memory stats: {name}\n{'='*50}")
    s = conn.execute(
        "SELECT COUNT(*) n, MIN(compacted_at) first, MAX(compacted_at) last FROM sessions"
    ).fetchone()
    print(f"\nSessions: {s['n']}")
    if s['first']:
        print(f"  First : {s['first'][:16]}\n  Last  : {s['last'][:16]}")
    m = conn.execute("SELECT COUNT(*) n FROM memories WHERE is_active=1").fetchone()
    a = conn.execute("SELECT COUNT(*) n FROM memories WHERE is_active=0").fetchone()
    print(f"\nMemories: {m['n']} active, {a['n']} archived")
    print("\nBy category:")
    by_cat = conn.execute(
        """SELECT category, COUNT(*) cnt, AVG(importance) avg_imp, MAX(importance) max_imp
           FROM memories WHERE is_active=1 GROUP BY category ORDER BY cnt DESC"""
    ).fetchall()
    _table(["Category", "Count", "Avg Imp", "Max Imp"],
           [(r["category"], r["cnt"], f"{r['avg_imp']:.1f}", r["max_imp"]) for r in by_cat])

    topics = conn.execute("SELECT COUNT(*) n FROM topics").fetchone()
    if topics["n"]:
        print(f"\nTopics: {topics['n']}")
        by_topic = conn.execute(
            """SELECT COALESCE(topic, '(none)') AS t, COUNT(*) cnt
               FROM memories WHERE is_active=1 GROUP BY t ORDER BY cnt DESC LIMIT 15"""
        ).fetchall()
        _table(["Topic", "Count"], [(r["t"], r["cnt"]) for r in by_topic])

    # Supersede chain summary
    superseded = conn.execute(
        "SELECT COUNT(*) n FROM memories WHERE supersedes_id IS NOT NULL"
    ).fetchone()
    if superseded["n"]:
        print(f"\nSupersede chains: {superseded['n']} update events recorded "
              f"(anti-patch in action — see docs/MEMORY_RULES.md)")

    kw = conn.execute(
        "SELECT keyword, frequency FROM keywords ORDER BY frequency DESC LIMIT 10"
    ).fetchall()
    if kw:
        print("\nTop keywords: " + ", ".join(f"{r['keyword']}({r['frequency']})" for r in kw))
    conn.close()


def cmd_list(args):
    _, db_path, name = _resolve_db(args.project)
    conn = _require_db(db_path)
    cat = args.category
    recent = [r[0] for r in conn.execute(
        "SELECT id FROM sessions ORDER BY compacted_at DESC LIMIT ?", (args.sessions,)
    ).fetchall()]
    if not recent:
        print("No sessions found.")
        return
    ph = ",".join("?" * len(recent))
    params = list(recent)
    cat_sql = ""
    if cat and cat != "all":
        cat_sql = "AND category = ?"
        params.append(cat)
    params.append(args.limit)

    rows = conn.execute(
        f"""SELECT m.id, m.category, m.importance, m.content, m.topic, s.compacted_at
            FROM memories m JOIN sessions s ON m.session_id=s.id
            WHERE m.is_active=1 AND m.session_id IN ({ph}) {cat_sql}
            ORDER BY m.importance DESC, m.created_at DESC LIMIT ?""", params
    ).fetchall()

    print(f"\nMemories for {name}" + (f" [{cat}]" if cat and cat != "all" else "") + ":\n")
    for r in rows:
        d = r["compacted_at"][:10] if r["compacted_at"] else "?"
        stars = "*" * r["importance"] + "." * (5 - r["importance"])
        topic = f"[{r['topic']}]" if r["topic"] else ""
        print(f"  [{r['id']:4d}] {stars}  {r['category']:<10}  {topic:<12}  {d}  "
              f"{_trunc(r['content'], 75)}")
    conn.close()


def cmd_search(args):
    _, db_path, name = _resolve_db(args.project)
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
            snip = ("..." if s > 0 else "") + c[s:e] + ("..." if e < len(c) else "")
        else:
            snip = _trunc(c, 90)
        topic = f"[{r.get('topic', '')}]" if r.get("topic") else ""
        print(f"  [{r['id']:4d}] {'*'*r['importance']:<5}  {r['category']:<10}  "
              f"{topic:<12}  {d}  {snip}")


def cmd_sessions(args):
    _, db_path, name = _resolve_db(args.project)
    conn = _require_db(db_path)
    rows = conn.execute(
        """SELECT s.id, s.trigger_type, s.compacted_at, s.msg_count,
                  COUNT(m.id) n_mem, s.archive_path
           FROM sessions s LEFT JOIN memories m ON m.session_id=s.id AND m.is_active=1
           GROUP BY s.id ORDER BY s.compacted_at DESC LIMIT 10"""
    ).fetchall()
    print(f"\nSessions for {name}:\n")
    _table(["ID", "Trigger", "Compacted At", "Msgs", "Memories", "Archive"],
           [(r["id"], r["trigger_type"], r["compacted_at"][:16],
             r["msg_count"], r["n_mem"],
             Path(r["archive_path"]).name if r["archive_path"] else "-") for r in rows])
    conn.close()


def cmd_sql(args):
    _, db_path, _ = _resolve_db(args.project)
    conn = _require_db(db_path)
    print(f"\nSQL: {args.query}\n")
    try:
        rows = conn.execute(args.query).fetchall()
        if not rows:
            print("(no rows)")
            return
        _table(list(rows[0].keys()), [list(r) for r in rows])
        print(f"\n({len(rows)} rows)")
    except sqlite3.Error as e:
        print(f"SQL Error: {e}")
    conn.close()


def cmd_add(args):
    """Add memory via the anti-patch writer (NOT direct insert)."""
    from llm.memory_writer import upsert_smart, regenerate_memory_index
    memory_dir, db_path, _ = _resolve_db(args.project)
    memory_dir.mkdir(parents=True, exist_ok=True)
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
    regenerate_memory_index(db, pid, memory_dir)
    action = result["action"]
    mid = result.get("id")
    sim = result.get("similarity", 0.0)
    print(f"[{action}] #{mid}  sim={sim:.2f}  {'*'*args.importance} "
          f"[{args.category}] {args.content}")


def cmd_keywords(args):
    _, db_path, name = _resolve_db(args.project)
    conn = _require_db(db_path)
    rows = conn.execute(
        "SELECT keyword, frequency, last_seen FROM keywords ORDER BY frequency DESC LIMIT 40"
    ).fetchall()
    print(f"\nVocabulary for {name}:\n")
    _table(["Keyword", "Freq", "Last Seen"],
           [(r["keyword"], r["frequency"], r["last_seen"][:10]) for r in rows])
    conn.close()


def cmd_topics(args):
    _, db_path, name = _resolve_db(args.project)
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
        print(f"  [{t['name']}] (v{t['version']}, {n} memories, "
              f"updated {t['updated_at'][:10]})")
        for line in textwrap.wrap(t["content"], width=78):
            print(f"    {line}")
        print()


def cmd_consolidate(args):
    from core.consolidate import run_consolidation
    print(f"\n{'='*50}\n  Consolidating memory for {args.project}\n{'='*50}\n")
    use_llm = not args.no_llm
    results = run_consolidation(args.project, use_llm=use_llm, verbose=True)
    print(f"\n{'='*50}\n  Results:")
    for k, v in results.items():
        print(f"    {k}: {v}")
    print(f"{'='*50}")


def cmd_cleanup(args):
    from core.consolidate import cleanup_garbage, merge_near_duplicates, assign_topics_auto
    from llm.memory_writer import regenerate_memory_index
    memory_dir = Path(args.project).resolve() / "memory"
    db = MemoryDB(memory_dir / "memory.db")
    pid = db.upsert_project(args.project)
    print(f"\n{'='*50}\n  Cleanup for {Path(args.project).name}\n{'='*50}\n")
    print(f"  Garbage deleted: {cleanup_garbage(db, pid)}")
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
    db = MemoryDB(db_path)
    pid = db.upsert_project(args.project)
    if args.mode_name:
        from core.modes import VALID_MODES
        if args.mode_name not in VALID_MODES:
            print(f"Invalid mode. Choose from: {', '.join(sorted(VALID_MODES))}")
            return
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
        print(f"No database at {db_path}")
        return
    db = MemoryDB(db_path)
    pid = db.upsert_project(args.project)
    progress_path = write_progress_md(db, pid, memory_dir)
    print(f"\nRegenerated: {progress_path}\n")
    print(progress_path.read_text(encoding="utf-8"))


def cmd_supersedes(args):
    """Show the supersede chain for a memory ID."""
    _, db_path, _ = _resolve_db(args.project)
    db = MemoryDB(db_path)
    chain = db.get_supersede_chain(args.memory_id)
    if not chain:
        print(f"No memory with id {args.memory_id}")
        return
    print(f"\nSupersede chain for #{args.memory_id} ({len(chain)} versions, newest first):\n")
    for i, m in enumerate(chain):
        active = "ACTIVE" if m["is_active"] else "archived"
        when = (m.get("updated_at") or "")[:16]
        print(f"  v{len(chain)-i}  #{m['id']:4d}  [{active}]  {when}  "
              f"{m['category']:<10}  {_trunc(m['content'], 70)}")


def cmd_summary(args):
    _, db_path, name = _resolve_db(args.project)
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
    web_main()


def make_parser():
    p = argparse.ArgumentParser(prog="cc-memory", description="cc-memory CLI v2.1",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True, help="Project root path")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Health check")
    sub.add_parser("stats", help="Database statistics")

    pl = sub.add_parser("list", help="List memories")
    pl.add_argument("category", nargs="?", default="all",
                    choices=["all", "decision", "result", "config", "bug", "task", "arch", "note"])
    pl.add_argument("--limit", type=int, default=20)
    pl.add_argument("--sessions", type=int, default=5)

    ps = sub.add_parser("search", help="Search memories")
    ps.add_argument("query")

    sub.add_parser("sessions", help="List sessions")

    pq = sub.add_parser("sql", help="Raw SQL query")
    pq.add_argument("query")

    pa = sub.add_parser("add", help="Add memory (anti-patch upsert)")
    pa.add_argument("category",
                    choices=["decision", "result", "config", "bug", "task", "arch", "note"])
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

    po = sub.add_parser("observations", help="List recent tool observations")
    po.add_argument("--limit", type=int, default=30)

    pm = sub.add_parser("mode", help="Show/set project mode")
    pm.add_argument("mode_name", nargs="?", default=None)

    sub.add_parser("summary", help="Latest session summary")

    pv = sub.add_parser("serve", help="Launch web dashboard")
    pv.add_argument("--port", type=int, default=9377)

    return p


def main():
    args = make_parser().parse_args()
    dispatch = {
        "status": cmd_status, "stats": cmd_stats, "list": cmd_list, "search": cmd_search,
        "sessions": cmd_sessions, "sql": cmd_sql, "add": cmd_add,
        "keywords": cmd_keywords, "topics": cmd_topics,
        "consolidate": cmd_consolidate, "cleanup": cmd_cleanup,
        "schema": cmd_schema, "progress": cmd_progress, "supersedes": cmd_supersedes,
        "observations": cmd_observations, "mode": cmd_mode,
        "summary": cmd_summary, "serve": cmd_serve,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
