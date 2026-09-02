"""D: do argparse defaults make a re-run overwrite stored fields?"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    proj = sb / "projA"
    db, pid, ids = _h.seed_project(proj, [
        {"content": "Seed decision: the scheduler runs every five minutes on the hour"}])

    def row(slug):
        return [r for r in db.list_directives(pid) if r["slug"] == slug][0]

    _h.mem(["directive-add", "pause-rule", "--quote", "please pause on quota",
            "--demand", "Pause when the quota hits 90 percent", "--kind", "process",
            "--times", "3"], proj)
    r0 = row("pause-rule")
    rc, out, err = _h.mem(["directive-add", "pause-rule"], proj)   # bare re-statement
    r1 = row("pause-rule")
    print("directive-add bare re-run: quote kept:", r1["quote"] == r0["quote"],
          "| demand kept:", r1["demand"] == r0["demand"], "| kind kept:", r1["kind"] == r0["kind"],
          f"| times {r0['times_stated']}->{r1['times_stated']}")
    rc, out, err = _h.mem(["directive-add", "pause-rule", "--quote", ""], proj)
    print("directive-add --quote '' (explicit empty): quote now:", repr(row("pause-rule")["quote"]))
    rc, out, err = _h.mem(["directive-edit", "pause-rule", "--demand", ""], proj)
    print("directive-edit --demand '': demand now:", repr(row("pause-rule")["demand"]),
          "times:", row("pause-rule")["times_stated"])
    rc, out, err = _h.mem(["directive-add", "pause-rule", "--times", "0"], proj)
    print("directive-add --times 0 -> times_stated:", row("pause-rule")["times_stated"], "rc", rc)
    rc, out, err = _h.mem(["directive-add", "pause-rule", "--times", "-7"], proj)
    print("directive-add --times -7 -> times_stated:", row("pause-rule")["times_stated"], "rc", rc)
    rc, out, err = _h.mem(["directive-edit", "pause-rule", "--status", "blocked"], proj)
    _h.show("directive-edit --status blocked", rc, out, err, 300)
    rc, out, err = _h.mem(["directive-add", "pause-rule"], proj)
    print("re-add while blocked -> status:", row("pause-rule")["status"], "rc", rc)
    rc, out, err = _h.mem(["directive-close", "pause-rule", "--evidence", "commit abc"], proj)
    rc, out, err = _h.mem(["directive-add", "pause-rule"], proj)
    print("re-add after close -> status:", row("pause-rule")["status"],
          "closed_at:", repr(row("pause-rule")["closed_at"]), "rc", rc)
    for a in (["directive-close", "pause-rule"], ["directive-close", "pause-rule", "--evidence", "   "],
              ["directive-edit", "pause-rule"], ["directive-edit", "nope", "--demand", "x"],
              ["directive-close", "nope", "--evidence", "x"]):
        rc, out, err = _h.mem(a, proj)
        print(f"{' '.join(a):<55} rc={rc} tb={_h.is_traceback(err)} | {out.strip().splitlines()[0][:90] if out.strip() else ''}")

    # add twice
    c = "Chose Postgres over MySQL for the ledger because of jsonb indexing"
    rc1, o1, _ = _h.mem(["add", "decision", c, "--importance", "5", "--topic", "db"], proj)
    rc2, o2, _ = _h.mem(["add", "decision", c], proj)
    print("add twice:", o1.strip()[:60], "|", o2.strip()[:60])
    m = [r for r in db.search_fts(pid, "Postgres")][0]
    print("  importance after 2nd add (default 3):", m["importance"], "topic:", repr(m["topic"]),
          "tags:", m["tags"])
    # plan-set raw twice, archive twice
    _h.mem(["plan-set", "--raw", "Plan v1: do X then Y"], proj)
    rc, out, err = _h.mem(["plan-set", "--raw", "Plan v1: do X then Y"], proj)
    print("plan-set --raw twice rc", rc, out.strip().splitlines()[0][:80])
    rc, out, err = _h.mem(["archive", str(ids[0])], proj)
    rc, out, err = _h.mem(["archive", str(ids[0])], proj)
    print("archive twice rc", rc, out.strip().splitlines()[0][:80])
finally:
    _h.destroy_sandbox()
