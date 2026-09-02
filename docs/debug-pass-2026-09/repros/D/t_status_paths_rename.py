"""D: (1) status/paths/read commands create nothing on an empty dir;
(2) after a directory rename, `status`/`search` silently create the new project row and
    thereby destroy the diagnostic `stats`/`list` were written to give."""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    empty = sb / "empty"; empty.mkdir()
    for a in (["status"], ["paths"], ["paths", "--json"], ["stats"], ["list"], ["search", "x"],
              ["sql", "select 1"], ["plan-status"], ["directive-list"], ["topics"], ["summary"],
              ["mode"], ["observations"], ["inject-show"], ["inject-usage"], ["encoding-check"],
              ["supersedes", "1"], ["progress"], ["cleanup"], ["consolidate", "--no-llm"], ["plan-show"]):
        rc, out, err = _h.mem(a, empty, cwd=empty)
        if _h.is_traceback(err):
            print("TRACEBACK", a, err[-300:])
    created = sorted(str(p.relative_to(empty)) for p in empty.rglob("*"))
    for a in (["list"], ["status"], ["list", "--all"]):
        _h.plan(a, empty, cwd=empty)
    created2 = sorted(str(p.relative_to(empty)) for p in empty.rglob("*"))
    print("(1) files created by read-only mem.py commands on an empty dir:", created or "NONE")
    print("    files created by plan.py list/status:", created2 or "NONE")
    home_created = sorted(str(p.relative_to(Path.home())) for p in Path.home().rglob("*") if p.is_file())
    print("    files under sandbox HOME:", home_created)

    # (2) rename scenario
    proj = sb / "projA"
    db, pid, ids = _h.seed_project(proj, [
        {"content": f"Decision {i}: the widget factory uses pattern {i} for assembly"} for i in range(3)])
    del db
    new = sb / "projA_renamed"
    os.rename(proj, new)
    print("\n(2) renamed projA -> projA_renamed; DB holds a row for the OLD path only")
    rc, out, err = _h.mem(["stats"], new, cwd=new); print(f"  stats  rc={rc}: {out.strip()[:200]!r}")
    rc, out, err = _h.mem(["list"], new, cwd=new);  print(f"  list   rc={rc}: {out.strip().splitlines()[0][:100]!r}")
    rc, out, err = _h.mem(["status"], new, cwd=new)
    print(f"  status rc={rc}: " + " | ".join(l.strip() for l in out.splitlines() if "Database" in l or "No database" in l))
    rc, out, err = _h.mem(["stats"], new, cwd=new); print(f"  stats  rc={rc} AFTER status: {out.strip()[:160]!r}")
    rc, out, err = _h.mem(["list"], new, cwd=new);  print(f"  list   rc={rc} AFTER status: {out.strip()[:120]!r}")
    rc, out, err = _h.mem(["sql", "SELECT id, path FROM projects", "--json"], new); print("  projects table:", out.strip().replace("\n", " ")[:200])
    rc, out, err = _h.mem(["search", "widget"], new); print(f"  search widget rc={rc}: {out.strip().splitlines()[-1][:80]!r}")
finally:
    _h.destroy_sandbox()
