"""D: does `/cc-mem search ""` (no minLength on the CLI) rebuild the FTS index (a WRITE) and dump rows?"""
import sys, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    proj = sb / "projA"
    db, pid, ids = _h.seed_project(proj, [
        {"content": f"Decision {i}: subsystem {i} uses strategy {i} because of constraint {i}"} for i in range(5)])
    held = sqlite3.connect(str(proj / ".ccm" / "memory.db"))
    dv = lambda: held.execute("PRAGMA data_version").fetchone()[0]
    for q in ("", '"', "NEAR(", "alpha OR", "Decision", ""):
        before = dv()
        rc, out, err = _h.mem(["search", q], proj, cwd=proj)
        rows = [l for l in out.splitlines() if l.strip().startswith("[")]
        print(f"search {q!r:<12} rc={rc} rows_printed={len(rows):<2} db_changed_by_this_READ={dv() != before} "
              f"tb={_h.is_traceback(err)}")
    held.close()
finally:
    _h.destroy_sandbox()
