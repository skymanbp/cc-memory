import _harness
try:
    import os
    from pathlib import Path
    from core.db import MemoryDB
    from core import consolidate as C
    from core.roots import anchor_project

    root = Path(_harness.SB)/"realproj"; (root/".ccm").mkdir(parents=True)
    db = MemoryDB(root/".ccm"/"memory.db"); pid = db.upsert_project(str(root))
    for i in range(60):  # over BACKLOG_ROWS (50)
        db.insert_memory(pid, None, "note", f"Independent distinct fact number {i} about subsystem xyz", importance=3)

    memory_dir = root/".ccm"
    abs_cwd = str(root.resolve())   # what Claude Code passes hooks (absolute)

    # ---- Simulate the CLI: run `/cc-mem --project . consolidate` from inside the project ----
    old = os.getcwd()
    os.chdir(str(root))
    try:
        cli_project = anchor_project(".")          # exactly what main() stores in args.project
    finally:
        os.chdir(old)
    print("CLI anchor_project('.') ->", repr(cli_project))

    # CLI writes the marker with str(args.project)  (cli/mem.py:1269)
    results = {"final_active": 60, "final_topics": 0}
    C.write_consolidation_marker(db, pid, memory_dir, str(cli_project), results)
    written = (memory_dir/".last_consolidation.json").read_text()
    print("marker on disk:", written)

    # ---- Hook side reads with the absolute cwd Claude Code hands it ----
    m = C.read_consolidation_marker(memory_dir, abs_cwd)
    print("hook read_consolidation_marker(abs_cwd) ->", m if m else "{} (FOREIGN -> treated never-run)")
    reason = C.consolidation_backlog(db, pid, m)
    print("backlog reason after a JUST-COMPLETED manual consolidate:", repr(reason))
    print("REDUNDANT KICK?", reason is not None)

    print("\n--- control: if the CLI had written the resolved absolute path ---")
    C.write_consolidation_marker(db, pid, memory_dir, abs_cwd, results)
    m2 = C.read_consolidation_marker(memory_dir, abs_cwd)
    print("hook read ->", "matched" if m2 else "foreign")
    print("backlog reason:", repr(C.consolidation_backlog(db, pid, m2)))
finally:
    _harness.cleanup()
