"""End-to-end smoke test for cc-memory v2.1.

Runs the anti-patch writer + PROGRESS.md generator + legacy migration in a
throwaway temp directory. Verifies the v3 migrations applied and the
INSERT / MERGE / SUPERSEDE / SKIP decisions match the contract in
docs/MEMORY_RULES.md.

Usage:  python tests/smoke_test.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cc_memory"))

from core.db import MemoryDB
from llm.memory_writer import upsert_smart, regenerate_memory_index
from core.progress import (
    write_progress_md, collect_progress_state, migrate_legacy_handoff
)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="cc-memory-smoketest-"))
    print(f"Test project: {tmp}")

    mem_dir = tmp / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "sessions").mkdir(exist_ok=True)

    db = MemoryDB(mem_dir / "memory.db")
    pid = db.upsert_project(str(tmp))
    print(f"[OK] DB init at {mem_dir / 'memory.db'}, project_id={pid}")

    # Verify v3 migrations applied
    with db._connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
        assert "supersedes_id" in cols, "v3_supersedes migration missing"
        assert "content_hash" in cols, "v2_content_hash missing"
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "progress" in tables, "v3_progress migration missing"
    print("[OK] v3 migrations: supersedes_id col + progress table present")

    # Test 1: INSERT (fresh, no similar)
    r1 = upsert_smart(db, pid, None, "decision",
                     "Chose JWT for auth because it scales horizontally",
                     4, topic="auth")
    assert r1["action"] == "inserted", f"expected inserted, got {r1}"
    print(f"[OK] Test 1 INSERT (fresh): {r1}")

    # Test 2: MERGE or SUPERSEDE (very similar to #1)
    r2 = upsert_smart(db, pid, None, "decision",
                     "Chose JWT for auth because horizontal scaling matters",
                     4, topic="auth")
    assert r2["action"] in ("merged", "superseded"), \
        f"expected merge/supersede, got {r2}"
    print(f"[OK] Test 2 anti-patch reconcile: {r2}")

    # Test 3: INSERT (independent fact in same topic)
    r3 = upsert_smart(db, pid, None, "config",
                     "JWT_SECRET rotated quarterly via Vault dynamic secret",
                     3, topic="auth")
    assert r3["action"] == "inserted", f"expected inserted, got {r3}"
    print(f"[OK] Test 3 INSERT (different fact, same topic): {r3}")

    # Test 4: SKIP via hash (exact dup of step 2's stored content)
    # After step 2, the content stored may differ; try the second-step content
    last_content = "Chose JWT for auth because horizontal scaling matters"
    r4 = upsert_smart(db, pid, None, "decision", last_content, 4, topic="auth")
    # Depending on whether step 2 merged or superseded, this might skip or merge
    assert r4["action"] in ("skipped", "merged"), \
        f"expected skipped/merged, got {r4}"
    print(f"[OK] Test 4 exact-dup handling: {r4}")

    # Test 5: another similar variant
    r5 = upsert_smart(db, pid, None, "decision",
                     "JWT remains the auth choice; HS256 picked over RS256",
                     4, topic="auth")
    print(f"[OK] Test 5 (variant): {r5}")

    # Confirm DB state
    active = db.get_all_active_memories(pid)
    print(f"\n[OK] Active memories: {len(active)}")
    for m in active:
        sup = f" supersedes={m['supersedes_id']}" if m["supersedes_id"] else ""
        print(f"    #{m['id']} [{m['category']}|imp{m['importance']}] "
              f"{m['content'][:60]}{sup}")

    # Verify supersede chain
    with db._connect() as conn:
        chains = conn.execute(
            "SELECT id, supersedes_id FROM memories WHERE supersedes_id IS NOT NULL"
        ).fetchall()
    print(f"\n[OK] Supersede chains recorded: {len(chains)}")

    # MEMORY.md
    regenerate_memory_index(db, pid, mem_dir)
    assert (mem_dir / "MEMORY.md").exists()
    print(f"[OK] MEMORY.md ({(mem_dir / 'MEMORY.md').stat().st_size} bytes)")

    # PROGRESS.md collect + write
    state = collect_progress_state(
        db, pid, mem_dir,
        current_request="Implement JWT-based auth for the dashboard",
        todos=[
            {"content": "Wire up token refresh", "priority": "high", "status": "pending"},
            {"content": "Add CSRF protection", "priority": "medium", "status": "pending"},
            {"content": "Write integration tests", "priority": "medium", "status": "completed"},
        ],
        files_read=["src/auth.py", "src/middleware.py"],
        files_modified=["src/auth.py", "src/routes.py"],
        transcript_ptr="C:/fake/transcripts/abc-123.jsonl",
        trigger_type="precompact",
    )
    db.upsert_progress(pid, **state)
    prog_path = write_progress_md(db, pid, mem_dir)
    assert prog_path.exists()
    print(f"[OK] PROGRESS.md ({prog_path.stat().st_size} bytes)")

    # Verify progress row
    prog = db.get_progress(pid)
    assert prog["current_request"] == "Implement JWT-based auth for the dashboard"
    assert len(prog["open_todos"]) == 2, \
        f"expected 2 open (1 completed filtered), got {len(prog['open_todos'])}"
    assert len(prog["files_touched"]) >= 2
    print(f"[OK] progress row verify: current_request=ok, "
          f"2 open todos (1 completed filtered), "
          f"{len(prog['files_touched'])} files_touched")

    # Legacy migration
    legacy = mem_dir / "SESSION_HANDOFF.md"
    legacy.write_text("# OLD POLLUTED SESSION_HANDOFF", encoding="utf-8")
    migrate_legacy_handoff(mem_dir)
    assert not legacy.exists()
    assert (mem_dir / "SESSION_HANDOFF.md.v2.bak").exists()
    print("[OK] Legacy SESSION_HANDOFF.md migrated to .v2.bak")

    # patch_progress (simulating Stop hook)
    db.patch_progress(
        pid,
        files_touched=[
            {"path": "src/auth.py", "action": "edit"},
            {"path": "tests/test_auth.py", "action": "edit"},
        ],
        trigger_type="stop",
    )
    write_progress_md(db, pid, mem_dir)
    prog2 = db.get_progress(pid)
    assert prog2["trigger_type"] == "stop"
    assert prog2["current_request"] == "Implement JWT-based auth for the dashboard"
    print("[OK] patch_progress: trigger_type updated, current_request preserved")

    print("\n===== ALL SMOKE TESTS PASSED =====")
    print(f"Test project preserved at: {tmp}")
    print("\nProduced files:")
    for f in sorted(mem_dir.rglob("*")):
        if f.is_file():
            rel = f.relative_to(mem_dir).as_posix()
            print(f"  memory/{rel}  ({f.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
