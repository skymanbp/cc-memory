"""D: drive cli/plan.py end to end."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    proj = sb / "projA"; proj.mkdir()
    def P(a, cwd=None, project=None):
        rc, out, err = _h.plan(a, project if project is not None else proj, cwd=cwd or proj)
        flag = "TRACEBACK" if _h.is_traceback(err) else ""
        print(f"{flag:9} rc={rc} {' '.join(a)[:45]:<45} | {(out.strip().splitlines() or [''])[0][:90]}"
              + (f"\n           {err.strip().splitlines()[-1][:150]}" if flag else ""))
        return rc, out, err
    P(["list"]); P(["status"])
    print("  created after read-only list/status:", sorted(str(x.relative_to(proj)) for x in proj.rglob("*")) or "NONE")
    P(["add", "Task one", "Task two", "计划三"])
    print("  .ccm/.gitignore exists:", (proj / ".ccm" / ".gitignore").exists())
    P(["list"]); P(["evaluate"]); P(["set-eval", "1", "ready", "ok"]); P(["set-eval", "1", "ready"])
    P(["approve"]); P(["approve", "--all"]); P(["exec", "--next", "3"]); P(["exec", "--next"]); P(["exec", "3"])
    P(["done", "1", "did it"]); P(["done", "1"]); P(["approve", "1"]); P(["set-eval", "1", "ready"])
    P(["fail", "2", "why"]); P(["skip", "3"]); P(["done", "99"]); P(["reorder", "1", "1"]); P(["reorder", "2", "3"])
    P(["clear"]); P(["list", "--all"]); P(["evaluate", "1"])
    P(["list"], project=""); P(["add", "sub task"], cwd=proj / ".ccm", project=".")
    P(["list"], project=str(sb / "nonexistent"))
    P(["add", "x"], project=str(sb / "nonexistent"))
finally:
    _h.destroy_sandbox()
