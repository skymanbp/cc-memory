#!/usr/bin/env python3
"""
cc-memory/mem.py  --  CLI query tool for cc-memory databases
Usage:
  python mem.py --project D:/Projects/my-project stats
  python mem.py --project D:/Projects/my-project list decisions
  python mem.py --project D:/Projects/my-project search "GNN F1"
  python mem.py --project D:/Projects/my-project sessions
  python mem.py --project D:/Projects/my-project sql "SELECT * FROM memories LIMIT 5"
  python mem.py --project D:/Projects/my-project add decision "Chose D1 GNN" --importance 4
  python mem.py --project D:/Projects/my-project keywords
  python mem.py --project D:/Projects/my-project topics
  python mem.py --project D:/Projects/my-project consolidate
  python mem.py --project D:/Projects/my-project cleanup
  python mem.py --project D:/Projects/my-project schema
"""
import argparse, json, sqlite3, sys, textwrap
from datetime import datetime
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))
from db import MemoryDB


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
    return text[:w-1] + "\u2026" if len(text) > w else text


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


# ── Commands ─────────────────────────────────────────────────────────────────

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
    _table(["Category","Count","Avg Imp","Max Imp"],
           [(r["category"], r["cnt"], f"{r['avg_imp']:.1f}", r["max_imp"]) for r in by_cat])

    # Topic stats
    topics = conn.execute("SELECT COUNT(*) n FROM topics").fetchone()
    if topics["n"]:
        print(f"\nTopics: {topics['n']}")
        by_topic = conn.execute(
            """SELECT COALESCE(topic, '(none)') AS t, COUNT(*) cnt
               FROM memories WHERE is_active=1
               GROUP BY t ORDER BY cnt DESC LIMIT 15"""
        ).fetchall()
        _table(["Topic", "Count"], [(r["t"], r["cnt"]) for r in by_topic])

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
        print("No sessions found."); return

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
        print(f"  [{r['id']:4d}] {stars}  {r['category']:<10}  {topic:<12}  {d}  {_trunc(r['content'], 75)}")
    conn.close()


def cmd_search(args):
    _, db_path, name = _resolve_db(args.project)
    conn = _require_db(db_path)
    pat = f"%{args.query}%"
    rows = conn.execute(
        """SELECT m.id, m.category, m.importance, m.content, m.topic, s.compacted_at
           FROM memories m LEFT JOIN sessions s ON m.session_id=s.id
           WHERE m.is_active=1 AND (m.content LIKE ? OR m.tags LIKE ? OR m.topic LIKE ?)
           ORDER BY m.importance DESC, m.created_at DESC LIMIT 30""", (pat, pat, pat)
    ).fetchall()
    print(f"\nSearch '{args.query}' in {name}:\n")
    if not rows:
        print("  (no matches)"); return
    for r in rows:
        d = r["compacted_at"][:10] if r["compacted_at"] else "?"
        c = r["content"]
        idx = c.lower().find(args.query.lower())
        if idx >= 0:
            s, e = max(0, idx-20), min(len(c), idx+len(args.query)+40)
            snip = ("..." if s > 0 else "") + c[s:e] + ("..." if e < len(c) else "")
        else:
            snip = _trunc(c, 90)
        topic = f"[{r['topic']}]" if r["topic"] else ""
        print(f"  [{r['id']:4d}] {'*'*r['importance']:<5}  {r['category']:<10}  {topic:<12}  {d}  {snip}")
    conn.close()


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
    _table(["ID","Trigger","Compacted At","Msgs","Memories","Archive"],
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
            print("(no rows)"); return
        _table(list(rows[0].keys()), [list(r) for r in rows])
        print(f"\n({len(rows)} rows)")
    except sqlite3.Error as e:
        print(f"SQL Error: {e}")
        print("\nTables: projects, sessions, memories, topics, keywords, _migrations")
    conn.close()


def cmd_add(args):
    _, db_path, _ = _resolve_db(args.project)
    memory_dir = Path(args.project).resolve() / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    db = MemoryDB(db_path)
    pid = db.upsert_project(args.project)
    tags = args.tags.split(",") if args.tags else ["manual"]
    topic = args.topic if hasattr(args, 'topic') and args.topic else ""
    mid = db.insert_memory(pid, None, args.category, args.content,
                           args.importance, tags, topic=topic)
    print(f"Added #{mid}: {'*'*args.importance} [{args.category}] {args.content}")


def cmd_keywords(args):
    _, db_path, name = _resolve_db(args.project)
    conn = _require_db(db_path)
    rows = conn.execute(
        "SELECT keyword, frequency, last_seen FROM keywords ORDER BY frequency DESC LIMIT 40"
    ).fetchall()
    print(f"\nVocabulary for {name}:\n")
    _table(["Keyword","Freq","Last Seen"], [(r["keyword"], r["frequency"], r["last_seen"][:10]) for r in rows])
    conn.close()


def cmd_topics(args):
    """Show all topic summaries."""
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
        print(f"  [{t['name']}] (v{t['version']}, {n} memories, updated {t['updated_at'][:10]})")
        # Word-wrap the summary
        for line in textwrap.wrap(t["content"], width=78):
            print(f"    {line}")
        print()


def cmd_consolidate(args):
    """Run full consolidation pipeline."""
    from consolidate import run_consolidation
    print(f"\n{'='*50}\n  Consolidating memory for {args.project}\n{'='*50}\n")

    use_llm = not args.no_llm
    results = run_consolidation(args.project, use_llm=use_llm, verbose=True)

    print(f"\n{'='*50}")
    print("  Consolidation results:")
    for k, v in results.items():
        print(f"    {k}: {v}")
    print(f"{'='*50}")


def cmd_cleanup(args):
    """Run garbage cleanup + dedup only (no LLM needed)."""
    from consolidate import cleanup_garbage, merge_near_duplicates, assign_topics_auto
    memory_dir = Path(args.project).resolve() / "memory"
    db = MemoryDB(memory_dir / "memory.db")
    pid = db.upsert_project(args.project)

    print(f"\n{'='*50}\n  Cleanup for {Path(args.project).name}\n{'='*50}\n")

    n_garbage = cleanup_garbage(db, pid)
    print(f"  Garbage deleted: {n_garbage}")

    n_dedup = merge_near_duplicates(db, pid)
    print(f"  Duplicates archived: {n_dedup}")

    n_topics = assign_topics_auto(db, pid)
    print(f"  Topics assigned: {n_topics}")

    stats = db.get_stats(pid)
    print(f"\n  Final: {stats['n_memories']} active memories")


def cmd_schema(args):
    _, db_path, _ = _resolve_db(args.project)
    conn = _require_db(db_path)
    print("\n=== Database Schema ===\n")
    for r in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name").fetchall():
        print(f"-- {r['type'].upper()}: {r['name']}")
        if r["sql"]: print(r["sql"])
        print()
    conn.close()


# ── Parser ───────────────────────────────────────────────────────────────────

def make_parser():
    p = argparse.ArgumentParser(prog="mem.py", description="cc-memory CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python mem.py --project D:/Projects/my-project stats
              python mem.py --project D:/Projects/my-project list decisions --limit 10
              python mem.py --project D:/Projects/my-project search "F1=0.741"
              python mem.py --project D:/Projects/my-project topics
              python mem.py --project D:/Projects/my-project consolidate
              python mem.py --project D:/Projects/my-project cleanup
              python mem.py --project D:/Projects/my-project sql "SELECT topic, COUNT(*) FROM memories GROUP BY topic"
              python mem.py --project D:/Projects/my-project add decision "Chose D1 GNN" --importance 4 --topic gnn
        """))
    p.add_argument("--project", required=True, help="Project root path")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="Database statistics + topic distribution")

    pl = sub.add_parser("list", help="List memories")
    pl.add_argument("category", nargs="?", default="all",
                    choices=["all","decision","result","config","bug","task","arch","note"])
    pl.add_argument("--limit", type=int, default=20)
    pl.add_argument("--sessions", type=int, default=5, help="Sessions to look back")

    ps = sub.add_parser("search", help="Search memories")
    ps.add_argument("query")

    sub.add_parser("sessions", help="List sessions")

    pq = sub.add_parser("sql", help="Raw SQL query")
    pq.add_argument("query")

    pa = sub.add_parser("add", help="Add memory manually")
    pa.add_argument("category", choices=["decision","result","config","bug","task","arch","note"])
    pa.add_argument("content")
    pa.add_argument("--importance", type=int, default=3, choices=range(1,6), metavar="1-5")
    pa.add_argument("--tags", default="manual")
    pa.add_argument("--topic", default="", help="Topic tag (e.g. cnn, gnn, pipeline)")

    sub.add_parser("keywords", help="Project keyword vocabulary")
    sub.add_parser("topics", help="Show all topic summaries")

    pc = sub.add_parser("consolidate", help="Run full consolidation (cleanup + dedup + topic summarization)")
    pc.add_argument("--no-llm", action="store_true", help="Skip LLM summarization")

    sub.add_parser("cleanup", help="Quick cleanup: garbage + dedup + topic assignment (no LLM)")
    sub.add_parser("schema", help="Show DB schema")
    return p


if __name__ == "__main__":
    args = make_parser().parse_args()
    dispatch = {
        "stats": cmd_stats, "list": cmd_list, "search": cmd_search,
        "sessions": cmd_sessions, "sql": cmd_sql, "add": cmd_add,
        "keywords": cmd_keywords, "topics": cmd_topics,
        "consolidate": cmd_consolidate, "cleanup": cmd_cleanup,
        "schema": cmd_schema,
    }
    dispatch[args.command](args)
