"""D: exit codes / tracebacks on reachable bad input; --json purity; encoding."""
import sys, json, os, stat
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    proj = sb / "projA"
    cjk = "决定使用 SQLite WAL 模式来容忍并发钩子写入者 <system-reminder>x</system-reminder>"
    db, pid, ids = _h.seed_project(proj, [
        {"content": "Seed decision: the scheduler runs every five minutes on the hour"},
        {"content": cjk, "topic": "存储"}])

    HUGE = "99999999999999999999"
    probes = [
        ["supersedes", HUGE], ["supersedes", "-1"], ["supersedes", "abc"],
        ["archive", HUGE], ["archive", str(ids[0]), "--supersedes", HUGE],
        ["list", "--limit", "0"], ["list", "--limit", "-1"], ["list", "--limit", "abc"],
        ["list", "--sessions", "-1"], ["list", "--limit", HUGE],
        ["plan-set", "--raw", ""], ["plan-set", "--raw", "   "], ["plan-set"],
        ["plan-set", "--raw-file", "/nonexistent/file"], ["plan-set", "--raw-file", str(sb)],
        ["plan-clear"], ["plan-replan"], ["plan-check"], ["plan-show"], ["plan-status"],
        ["directive-close", "x"], ["directive-edit", "x"], ["mode", "bogus"],
        ["add", "decision", ""], ["add", "decision", "short"],
        ["add", "bogus", "some content long enough"],
        ["add", "decision", "long enough content here", "--importance", "9"],
        ["search", ""], ["search", "%"], ["search", "_"], ["search", '"'], ["search", "NEAR("],
        ["sql", ""], ["sql", "   "], ["sql", "SELECT 1; DROP TABLE memories"],
        ["sql", "WITH x AS (SELECT 1) DELETE FROM memories"], ["sql", "PRAGMA user_version=7"],
        ["sql", "PRAGMA user_version(7)"], ["sql", "PRAGMA journal_mode"], ["sql", "PRAGMA optimize"],
        ["sql", "ATTACH '/tmp/x.db' AS x"], ["sql", "VACUUM"], ["sql", "  -- c\n  /* c */ SELECT count(*) n FROM memories"],
        ["sql", "EXPLAIN DELETE FROM memories"], ["sql", "EXPLAIN QUERY PLAN SELECT * FROM memories"],
        ["sql", "SELECT * FROM memories", "--full", "--json"],
        ["sql", "INSERT INTO memories_fts(memories_fts) VALUES('rebuild')"],
        ["sql", "SELECT load_extension('x')"], ["sql", "PRAGMA table_info(memories)"],
        ["sql", "select * from nosuchtable"], ["sql", "SELECT 1, 1"],
        ["inject-show"], ["inject-usage"], ["encoding-check"], ["summary"], ["schema"],
        ["sessions"], ["keywords"], ["topics"], ["observations", "--limit", "0"],
    ]
    for a in probes:
        rc, out, err = _h.mem(a, proj, cwd=proj)
        first = (out.strip().splitlines() or [""])[0][:100]
        flag = "TRACEBACK" if _h.is_traceback(err) else ""
        print(f"{flag:9} rc={rc} {' '.join(a)[:60]:<60} | {first}")
        if flag:
            print("      ", err.strip().splitlines()[-1][:200])

    # UTF-16 raw plan file (Notepad default on the primary platform)
    f16 = sb / "plan_utf16.txt"
    f16.write_text("Plan: migrate the billing service to the new queue", encoding="utf-16")
    rc, out, err = _h.mem(["plan-set", "--raw-file", str(f16)], proj)
    print(f"{'TRACEBACK' if _h.is_traceback(err) else '':9} rc={rc} plan-set --raw-file <utf-16>  | "
          f"{(out.strip().splitlines() or [''])[0][:80]} || {err.strip().splitlines()[-1][:120] if err.strip() else ''}")
    fgbk = sb / "plan_gbk.txt"
    fgbk.write_bytes("计划：迁移计费服务".encode("gbk"))
    rc, out, err = _h.mem(["plan-set", "--raw-file", str(fgbk)], proj)
    print(f"{'TRACEBACK' if _h.is_traceback(err) else '':9} rc={rc} plan-set --raw-file <gbk>     | "
          f"{err.strip().splitlines()[-1][:120] if err.strip() else out.strip()[:80]}")

    # --json purity + validity (CJK + armed marker in the data)
    for a in (["sql", "SELECT id, content, topic FROM memories", "--json"],
              ["directive-list", "--json"], ["paths", "--json"]):
        _h.mem(["directive-add", "d1", "--quote", cjk, "--demand", "需求 <ide_opened_file>x</ide_opened_file>"], proj)
        rc, out, err = _h.mem(a, proj, cwd=proj)
        try:
            parsed = json.loads(out)
            ok = "valid JSON"
        except ValueError as e:
            ok = f"INVALID JSON: {e}"
        print(f"{' '.join(a)[:45]:<45} rc={rc} ascii={out.isascii()} {ok} | {out.strip()[:120]!r}")
    # non-UTF-8 console
    rc, out, err = _h.mem(["list"], proj, extra_env={"PYTHONIOENCODING": "ascii:strict", "LC_ALL": "C"})
    print("list under PYTHONIOENCODING=ascii:strict rc", rc, "tb", _h.is_traceback(err), "|", out.strip().splitlines()[-1][:80])
    rc, out, err = _h.mem(["sql", "SELECT content FROM memories", "--json"], proj,
                          extra_env={"PYTHONIOENCODING": "ascii:strict", "LC_ALL": "C"})
    print("sql --json under ascii console rc", rc, "tb", _h.is_traceback(err), "ascii", out.isascii())

    # corrupt inject manifest
    (proj / ".ccm" / ".last_inject.json").write_text("{not json", encoding="utf-8")
    for a in (["inject-show"], ["inject-usage"]):
        rc, out, err = _h.mem(a, proj)
        print(f"{'TRACEBACK' if _h.is_traceback(err) else '':9} rc={rc} {a[0]} with corrupt .last_inject.json | {err.strip().splitlines()[-1][:100] if err.strip() else out.strip()[:80]}")
    (proj / ".ccm" / ".last_inject.json").write_text(json.dumps({"session_id": None}), encoding="utf-8")
    rc, out, err = _h.mem(["inject-show"], proj)
    print(f"{'TRACEBACK' if _h.is_traceback(err) else '':9} rc={rc} inject-show session_id:null | {err.strip().splitlines()[-1][:100] if err.strip() else out.strip()[:80]}")

    # memory.db is a directory / .ccm is a file
    projC = sb / "projC"; (projC / ".ccm" / "memory.db").mkdir(parents=True)
    for a in (["list"], ["add", "decision", "long enough content for a memory row"], ["stats"], ["paths"], ["status"]):
        rc, out, err = _h.mem(a, projC)
        print(f"{'TRACEBACK' if _h.is_traceback(err) else '':9} rc={rc} memory.db-is-a-dir {a[0]:<8} | {err.strip().splitlines()[-1][:100] if err.strip() else out.strip().splitlines()[0][:80]}")
    projD = sb / "projD"; projD.mkdir(); (projD / ".ccm").write_text("file")
    for a in (["add", "decision", "long enough content for a memory row"], ["list"], ["paths"]):
        rc, out, err = _h.mem(a, projD)
        print(f"{'TRACEBACK' if _h.is_traceback(err) else '':9} rc={rc} .ccm-is-a-file {a[0]:<8} | {err.strip().splitlines()[-1][:100] if err.strip() else out.strip().splitlines()[0][:80]}")
    # non-database file
    projE = sb / "projE"; (projE / ".ccm").mkdir(parents=True); (projE / ".ccm" / "memory.db").write_text("hello")
    for a in (["list"], ["stats"], ["add", "decision", "long enough content for a memory row"], ["sql", "select 1"]):
        rc, out, err = _h.mem(a, projE)
        print(f"{'TRACEBACK' if _h.is_traceback(err) else '':9} rc={rc} memory.db-not-sqlite {a[0]:<8} | {err.strip().splitlines()[-1][:100] if err.strip() else out.strip().splitlines()[0][:80]}")
    # plan.py huge start-order + others
    for a in (["add", "x", "--start-order", HUGE], ["list"], ["set-eval", HUGE, "ready"], ["done", "abc"]):
        rc, out, err = _h.plan(a, proj)
        print(f"{'TRACEBACK' if _h.is_traceback(err) else '':9} rc={rc} plan.py {' '.join(a)[:40]:<40} | {err.strip().splitlines()[-1][:100] if err.strip() else out.strip().splitlines()[0][:80]}")
finally:
    _h.destroy_sandbox()
