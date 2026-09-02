"""D: `/cc-mem consolidate` stamps project_path as the RELATIVE --project spelling,
so the Stop hook's backpressure probe reads the marker as FOREIGN and re-runs."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    proj = sb / "projA"
    mems = [{"content": f"Decision number {i}: use component {i} for subsystem {i} because it scales"}
            for i in range(12)]
    db, pid, _ = _h.seed_project(proj, mems)
    from core.consolidate import read_consolidation_marker, consolidation_backlog

    marker_file = proj / ".ccm" / ".last_consolidation.json"
    hook_cwd = str(proj)           # what hooks/stop.py passes: resolve_project(payload cwd)

    for spelling, cwd in ((".", proj), ("projA", sb), (str(proj), sb)):
        if marker_file.exists():
            marker_file.unlink()
        rc, out, err = _h.mem(["consolidate", "--no-llm"], spelling, cwd=cwd)
        assert rc == 0, (rc, out, err)
        stored = json.loads(marker_file.read_text(encoding="utf-8"))["project_path"]
        seen_by_hook = read_consolidation_marker(proj / ".ccm", hook_cwd)
        due = consolidation_backlog(db, pid, seen_by_hook)
        print(f"--project {spelling!r:<40} marker.project_path={stored!r:<40} "
              f"stop-hook sees marker: {'YES' if seen_by_hook else 'NO (foreign -> {})'}"
              f" | backlog verdict: {due!r}")
finally:
    _h.destroy_sandbox()
