"""user_prompt.py turn-1 seeding stores a slash command as progress.current_request. The documented
activation (`/ccm-load` as the first message) therefore writes PROGRESS.md §1 = 'ccm-load', and
_refresh_progress_row is fill-only-empty so nothing repairs it until the first compaction.
pre_compact._first_user_request deliberately skips slash-command scaffolding for exactly this reason."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    for first in ("/ccm-load", "/cc-mem status", "/compact"):
        proj = sb.root / ("p_" + first.strip("/").replace(" ", "_").replace("-", "")); proj.mkdir()
        r = sb.run_hook("user_prompt", {"session_id": "S-" + proj.name, "cwd": str(proj), "prompt": first})
        assert r["rc"] == 0 and not r["err"], r
        prog = (proj / ".ccm" / "PROGRESS.md").read_text()
        sec1 = prog.split("## 1. Current Request")[1].split("## 2.")[0].strip()
        print(f"first prompt {first!r:20s} -> PROGRESS.md §1 Current Request: {sec1!r}")
    # the real request arrives on turn 2 and is NOT seeded (turn_count != 1)
    proj = sb.root / "p_ccmload"
    r = sb.run_hook("user_prompt", {"session_id": "S-p_ccmload", "cwd": str(proj), "prompt": "Refactor the billing module to use decimal math"})
    sec1 = (proj / ".ccm" / "PROGRESS.md").read_text().split("## 1. Current Request")[1].split("## 2.")[0].strip()
    print(f"after the real turn-2 request           -> §1 still: {sec1!r}")
finally:
    sb.cleanup()
