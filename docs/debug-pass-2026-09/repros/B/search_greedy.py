"""check_carryover: _consume_carry assigns old steps to new slots greedily in old-step order. Search for a
replacement where EVERY old step has a >=bar match in the new plan, yet the gate refuses (false violation)."""
import itertools, os, random, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core import plan as plan_mod
    from core.textsim import jaccard, shingle_set
    s = lambda a, b: jaccard(shingle_set(a), shingle_set(b))
    words = "add remove update refactor the auth session billing cache module tests for and docs config layer".split()
    random.seed(7); found = None
    for _ in range(40000):
        a = " ".join(random.sample(words, random.randint(3, 6)))
        b = " ".join(random.sample(words, random.randint(3, 6)))
        n1 = " ".join(random.sample(words, random.randint(3, 7)))
        n2 = " ".join(random.sample(words, random.randint(3, 7)))
        old = {"goal": "g", "steps": [{"id": 1, "title": a, "status": "pending"}, {"id": 2, "title": b, "status": "pending"}]}
        new = {"goal": "g", "steps": [{"title": n1, "status": "pending"}, {"title": n2, "status": "pending"}]}
        # a perfect matching exists?
        ok = (s(a, n1) >= .5 and s(b, n2) >= .5) or (s(a, n2) >= .5 and s(b, n1) >= .5)
        if ok and plan_mod.check_carryover(old, new):
            found = (a, b, n1, n2, plan_mod.check_carryover(old, new)); break
    if found:
        a, b, n1, n2, v = found
        print("old A:", a, "| old B:", b); print("new 1:", n1, "| new 2:", n2)
        print(f"sims A->(1,2)={s(a,n1):.3f},{s(a,n2):.3f}  B->(1,2)={s(b,n1):.3f},{s(b,n2):.3f}")
        print("a valid one-to-one carry exists:", True); print("gate verdict:", v)
    else:
        print("no greedy false-refusal found in 40k random pairs")
finally:
    sb.cleanup()
