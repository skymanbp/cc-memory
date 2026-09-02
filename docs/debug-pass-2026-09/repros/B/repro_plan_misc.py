"""core/plan.py: (1) sync_todos_to_steps on a todo list of bare strings -> AttributeError (lost sync);
(2) check_carryover greedy slot consumption -> false REFUSAL of a replacement that carries every step."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core import plan as plan_mod
    from core.textsim import jaccard, shingle_set
    structured = plan_mod.normalize_structured({"goal": "g", "steps": [{"title": "fix the login bug", "status": "pending"}]})
    try:
        plan_mod.sync_todos_to_steps(structured, ["fix the login bug"])
        print("(1) string todos: synced OK")
    except Exception as e:
        print(f"(1) string todos -> {type(e).__name__}: {e}")
    # (2) greedy carryover
    old = plan_mod.normalize_structured({"goal": "g", "steps": [
        {"title": "add unit tests for the auth module", "status": "pending"},
        {"title": "add unit tests for the auth module and the session module", "status": "pending"}]})
    new = {"goal": "g", "steps": [
        {"title": "add unit tests for the auth module and the session module", "status": "pending"},
        {"title": "add unit tests for the auth module; also the session module", "status": "pending"}]}
    s = lambda a, b: round(jaccard(shingle_set(a), shingle_set(b)), 3)
    for o in old["steps"]:
        print("   sims", o["title"][:40], "->", [s(o["title"], n["title"]) for n in new["steps"]])
    v = plan_mod.check_carryover(old, new)
    print("(2) violations:", v or "none")
    print("    every old step has a >=0.5 match in the new plan:",
          all(any(s(o["title"], n["title"]) >= 0.5 for n in new["steps"]) for o in old["steps"]))
finally:
    sb.cleanup()
