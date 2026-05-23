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

    # === v2.2 features: forced-reminder RESUME PROTOCOL ====================
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cc_memory" / "hooks"))
    from hooks.session_start import _build_forced_reminder, _refresh_progress_row
    reminder = _build_forced_reminder(mem_dir)
    assert "RESUME PROTOCOL" in reminder, "forced reminder missing RESUME PROTOCOL block"
    assert "继续" in reminder, "resume signal whitelist missing Chinese tokens"
    assert "resume" in reminder.lower(), "resume signal whitelist missing English tokens"
    assert "open_todos[0]" in reminder.lower() or "todos[0]" in reminder.lower(), \
        "forced reminder doesn't direct Claude to open_todos[0]"
    print("[OK] forced reminder contains RESUME PROTOCOL + signal whitelist")

    # === v2.2 features: fill-only-empty progress refresh ===================
    # Build a SECOND test project with an EMPTY progress row + session_summary
    # so we can verify _refresh_progress_row populates the right fields.
    tmp2 = Path(tempfile.mkdtemp(prefix="cc-memory-refresh-"))
    mem2 = tmp2 / "memory"; mem2.mkdir(parents=True, exist_ok=True)
    db2 = MemoryDB(mem2 / "memory.db")
    pid2 = db2.upsert_project(str(tmp2))

    # Seed critical memory (importance >= 4) + a session_summary
    db2.insert_memory(pid2, None, "decision",
                      "Use PostgreSQL with pgvector for embeddings (must-remember)",
                      importance=5, tags=["critical"], topic="db")
    sid2 = db2.insert_session(pid2, "fake-claude-sid", "auto", 42, "", "")
    db2.insert_session_summary(sid2, pid2, {
        "request": "Set up vector search",
        "investigated": "src/embed.py",
        "learned": "pgvector index on cosine distance is 4x faster than IVFFlat",
        "completed": "Migrated schema; reindexed 12k rows",
        "next_steps": "Add hybrid BM25+vector ranker; Wire up reranker; Add eval harness",
        "notes": "",
        "files_read": ["src/embed.py", "tests/test_embed.py"],
        "files_modified": ["src/embed.py", "src/search.py"],
    })
    # Seed observations (so files_touched can be derived)
    db2.insert_observation(pid2, "s", "Read",  "src/embed.py", "")
    db2.insert_observation(pid2, "s", "Edit",  "src/search.py", "")
    db2.insert_observation(pid2, "s", "Write", "tests/test_search.py", "")

    # Pre-condition: progress row is completely empty
    db2.upsert_progress(pid2)  # writes default empties
    pre = db2.get_progress(pid2)
    assert not pre["critical_context"] and not pre["status_done"] \
        and not pre["plan"] and not pre["files_touched"] \
        and not pre["open_todos"], "precondition: empty progress row"

    # Run refresh
    _refresh_progress_row(db2, pid2, mem2)

    post = db2.get_progress(pid2)
    assert len(post["critical_context"]) == 1, \
        f"expected 1 critical context, got {len(post['critical_context'])}"
    assert "PostgreSQL" in post["critical_context"][0]["content"]
    assert "Migrated schema" in post["status_done"]
    assert "pgvector index" in post["status_in_flight"]
    assert "hybrid BM25" in post["plan"]
    assert len(post["open_todos"]) == 3, \
        f"open_todos derived from next_steps split: expected 3, got {len(post['open_todos'])}"
    assert any(t["content"].startswith("Add hybrid") for t in post["open_todos"])
    assert len(post["files_touched"]) >= 2
    assert post["trigger_type"] == "session_start_refresh"
    print(f"[OK] _refresh_progress_row fills empty fields: "
          f"crit={len(post['critical_context'])}, "
          f"todos={len(post['open_todos'])}, "
          f"files={len(post['files_touched'])}")

    # === v2.2 features: extract_latest_todo_state (last-wins) ==============
    from core.extractor import extract_latest_todo_state

    def _mk_tu(name, **inp):
        return {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": name, "input": inp}
        ]}}

    msgs_todo = [
        _mk_tu("TodoWrite", todos=[
            {"content": "task A", "status": "pending",   "activeForm": "Doing A"},
            {"content": "task B", "status": "pending",   "activeForm": "Doing B"},
        ]),
        _mk_tu("TodoWrite", todos=[
            {"content": "task A", "status": "completed",   "activeForm": "Doing A"},
            {"content": "task B", "status": "in_progress", "activeForm": "Doing B"},
            {"content": "task C", "status": "pending",     "activeForm": "Doing C"},
        ]),
    ]
    snap = extract_latest_todo_state(msgs_todo)
    assert len(snap) == 3, f"expected last-wins=3, got {len(snap)} (stacked?)"
    assert snap[0]["status"] == "completed" and snap[2]["content"] == "task C"
    print(f"[OK] extract_latest_todo_state: last-wins, {len(snap)} items (no stacking)")

    msgs_cleared = msgs_todo + [_mk_tu("TodoWrite", todos=[])]
    assert extract_latest_todo_state(msgs_cleared) == [], "empty TodoWrite should clear"
    print("[OK] extract_latest_todo_state: explicit empty TodoWrite clears list")

    assert extract_latest_todo_state(
        [{"message": {"role": "user", "content": "hi"}}]
    ) == []
    print("[OK] extract_latest_todo_state: no TodoWrite ever ran returns []")

    # === v2.2 features: tier-3 transcript fallback =========================
    # Build a synthetic prior-session JSONL and monkey-patch
    # find_latest_transcript so _refresh_progress_row's tier-3 code path
    # mines it. Verifies: open_todos + files_touched + transcript_ptr all
    # get populated when DB sources have nothing to offer.
    tmp4 = Path(tempfile.mkdtemp(prefix="cc-memory-tier3-"))
    mem4 = tmp4 / "memory"; mem4.mkdir(parents=True, exist_ok=True)
    db4 = MemoryDB(mem4 / "memory.db")
    pid4 = db4.upsert_project(str(tmp4))

    import json as _json
    prior_jsonl = tmp4 / "prior_session.jsonl"
    prior_msgs_data = [
        {"message": {"role": "user", "content": "build feature X"}},
        _mk_tu("TodoWrite", todos=[
            {"content": "Implement step 1", "status": "completed", "activeForm": "Doing 1"},
            {"content": "Implement step 2", "status": "pending",   "activeForm": "Doing 2"},
            {"content": "Write tests",      "status": "pending",   "activeForm": "Writing"},
        ]),
        _mk_tu("Edit",  file_path="src/feature_x.py"),
        _mk_tu("Write", file_path="tests/test_feature_x.py"),
    ]
    with open(prior_jsonl, "w", encoding="utf-8") as fh:
        for m in prior_msgs_data:
            fh.write(_json.dumps(m) + "\n")

    import core.extractor as _ex_mod
    orig_find = _ex_mod.find_latest_transcript
    _ex_mod.find_latest_transcript = lambda *a, **kw: prior_jsonl
    try:
        db4.upsert_progress(pid4)  # empty defaults
        _refresh_progress_row(db4, pid4, mem4,
                              current_session_id="not-the-fake-session")
    finally:
        _ex_mod.find_latest_transcript = orig_find

    post4 = db4.get_progress(pid4)
    assert post4["open_todos"], "tier-3 must fill open_todos from transcript"
    assert len(post4["open_todos"]) == 2, \
        f"expected 2 (1 completed filtered out), got {len(post4['open_todos'])}"
    assert any("step 2" in t["content"] for t in post4["open_todos"])
    assert any("Write tests" in t["content"] for t in post4["open_todos"])
    assert post4["transcript_ptr"] == str(prior_jsonl.resolve())
    assert post4["files_touched"], "tier-3 must fill files_touched from transcript"
    assert any("feature_x.py" in f["path"] for f in post4["files_touched"])
    print(f"[OK] tier-3 transcript fallback: "
          f"{len(post4['open_todos'])} todos + "
          f"{len(post4['files_touched'])} files + transcript_ptr set")

    # === Fill-only-empty contract: pre-set fields are NOT overwritten ======
    tmp3 = Path(tempfile.mkdtemp(prefix="cc-memory-fillonly-"))
    mem3 = tmp3 / "memory"; mem3.mkdir(parents=True, exist_ok=True)
    db3 = MemoryDB(mem3 / "memory.db")
    pid3 = db3.upsert_project(str(tmp3))

    # Pre-populate with non-empty values (simulating PreCompact's full rewrite)
    db3.upsert_progress(pid3,
                        status_done="Already recorded by PreCompact",
                        plan="Authoritative plan from PreCompact",
                        open_todos=[{"content": "PreCompact todo", "priority": "high",
                                     "status": "pending"}])
    # Add session_summary that WOULD overwrite if not for fill-only contract
    sid3 = db3.insert_session(pid3, "s3", "auto", 10, "", "")
    db3.insert_session_summary(sid3, pid3, {
        "completed": "STALE SHOULD NOT APPEAR",
        "next_steps": "STALE STEPS",
        "files_read": [], "files_modified": [],
    })

    _refresh_progress_row(db3, pid3, mem3)
    after = db3.get_progress(pid3)
    assert after["status_done"] == "Already recorded by PreCompact", \
        "fill-only-empty violated: status_done was overwritten"
    assert after["plan"] == "Authoritative plan from PreCompact", \
        "fill-only-empty violated: plan was overwritten"
    assert len(after["open_todos"]) == 1 and after["open_todos"][0]["content"] == "PreCompact todo", \
        "fill-only-empty violated: open_todos was overwritten"
    print("[OK] fill-only-empty contract: non-empty fields preserved")

    print("\n===== ALL SMOKE TESTS PASSED =====")
    print(f"Test project preserved at: {tmp}")
    print("\nProduced files:")
    for f in sorted(mem_dir.rglob("*")):
        if f.is_file():
            rel = f.relative_to(mem_dir).as_posix()
            print(f"  memory/{rel}  ({f.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
