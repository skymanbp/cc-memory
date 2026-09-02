"""Re-run a subset of the matrix and dump every ERROR the hooks swallowed into their log."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core.db import MemoryDB
    from core import plan as plan_mod
    proj = sb.proj; (proj / ".ccm").mkdir()
    db = MemoryDB(proj / ".ccm" / "memory.db"); pid = db.upsert_project(str(proj))
    plan_mod.apply_refined_plan(db, pid, {"goal": "g", "steps": [{"title": "fix bug", "status": "pending"}]}, memory_dir=proj/".ccm")
    base = {"session_id": "s", "cwd": str(proj)}
    cases = {
      "ptu tool_input_bad_cmd": ("post_tool_use", dict(base, tool_name="Bash", tool_input={"command": 5}, tool_response="o")),
      "ptu tool_input_str":     ("post_tool_use", dict(base, tool_name="Bash", tool_input="str", tool_response="o")),
      "ptu todos_strings":      ("post_tool_use", dict(base, tool_name="TodoWrite", tool_input={"todos": ["fix bug"]})),
      "ptu todos_nondict":      ("post_tool_use", dict(base, tool_name="TodoWrite", tool_input={"todos": [1, None]})),
      "ptu sensitive_bad_cmd":  ("post_tool_use", dict(base, tool_name="Bash", tool_input={"command": ["git","push"]})),
      "ptu exitplan_int":       ("post_tool_use", dict(base, tool_name="ExitPlanMode", tool_input={"plan": 7})),
      "ptu read_ok":            ("post_tool_use", dict(base, tool_name="Read", tool_input={"file_path": "/a"}, tool_response={"x": 1})),
      "up prompt_int":          ("user_prompt", dict(base, prompt=5)),
      "stop wellformed":        ("stop", base),
      "ss wellformed":          ("session_start", base),
    }
    for label, (hook, pl) in cases.items():
        r = sb.run_hook(hook, pl)
        assert r["rc"] == 0 and not r["err"], (label, r)
    logs = list((sb.home / ".claude" / "hooks" / "cc-memory" / "logs").glob("*.log"))
    text = "".join(p.read_text() for p in logs)
    errs = [l for l in text.splitlines() if "[ERROR]" in l or "Error" in l]
    print("\n".join(errs[:40]) or "(no errors logged)")
finally:
    sb.cleanup()
