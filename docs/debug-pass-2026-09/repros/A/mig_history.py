"""Did any _MIGRATIONS entry's SQL (or SCHEMA_SQL) change after it shipped?
The ledger is keyed by NAME, so a changed body never reaches an existing DB."""
import ast, subprocess, sys, re
shas = subprocess.check_output(["git", "log", "--format=%h %ad %s", "--date=short", "--follow", "--", "cc_memory/core/db.py"],
                               cwd="/home/user/cc-memory", text=True).strip().splitlines()
hist = {}
for line in reversed(shas):  # oldest first
    sha = line.split()[0]
    for path in ("cc_memory/core/db.py", "cc_memory/db.py"):
        try:
            src = subprocess.check_output(["git", "show", f"{sha}:{path}"], cwd="/home/user/cc-memory", text=True, stderr=subprocess.DEVNULL)
            break
        except subprocess.CalledProcessError:
            src = None
    if src is None:
        print("no db.py at", line); continue
    tree = ast.parse(src)
    migs, schema = None, None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id == "_MIGRATIONS":
                migs = ast.literal_eval(node.value)
            elif node.targets[0].id == "SCHEMA_SQL":
                schema = ast.literal_eval(node.value)
    hist[sha] = (line, dict(migs or []), schema)
norm = lambda s: " ".join(s.split())
prev = None
for sha, (line, migs, schema) in hist.items():
    if prev is not None:
        pline, pmigs, pschema = hist[prev]
        for name, sql in migs.items():
            if name in pmigs and norm(pmigs[name]) != norm(sql):
                print(f"\n=== CHANGED BODY: {name}\n  from {pline}\n  to   {line}\n  OLD: {norm(pmigs[name])[:300]}\n  NEW: {norm(sql)[:300]}")
        for name in pmigs:
            if name not in migs:
                print(f"\n=== REMOVED entry {name} between {pline} and {line}")
        if pschema and schema and norm(pschema) != norm(schema):
            import difflib
            d = [l for l in difflib.unified_diff(pschema.splitlines(), schema.splitlines(), lineterm="", n=0) if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
            print(f"\n=== SCHEMA_SQL changed {pline} -> {line}:\n  " + "\n  ".join(d[:20]))
    prev = sha
print("\ncommits inspected:", len(hist), "| entries now:", len(hist[prev][1]))
