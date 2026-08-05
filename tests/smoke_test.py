"""End-to-end smoke test for cc-memory v2.1.

Runs the anti-patch writer + PROGRESS.md generator + legacy migration in a
throwaway temp directory. Verifies the v3 migrations applied and the
INSERT / MERGE / SUPERSEDE / SKIP decisions match the contract in
docs/CONTRACTS.md#anti-patch-contract.

Hermetic by construction, the same way tests/test_surfaces.py is: HOME /
USERPROFILE / HOMEDRIVE / HOMEPATH / TEMP / TMP / TMPDIR *and*
`tempfile.tempdir` are redirected into one sandbox root BEFORE cc_memory is
imported, because `core.logger` resolves
`Path.home()/".claude"/"hooks"/"cc-memory"/"logs"` at IMPORT time. Until this
was added, a plain run appended to the MAINTAINER'S REAL ~/.claude log (the
proof: with HOME redirected, that same file appears under the redirect) and
left 14 `cc-mem*` directories behind in the real %TEMP%. The real ~/.claude is
unreachable for the whole run, asserted below; the sandbox is removed at the
end and a leak this cannot clean is a test failure, not a shrug.

Usage:  python tests/smoke_test.py
"""
import gc
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# ── sandbox: must be installed BEFORE importing anything from cc_memory ─────
_SANDBOX = Path(tempfile.mkdtemp(prefix="cc-memory-smokebox-"))
_HOME = _SANDBOX / "home"
_TMP = _SANDBOX / "tmp"
_HOME.mkdir(parents=True, exist_ok=True)
_TMP.mkdir(parents=True, exist_ok=True)
_drive, _rest = os.path.splitdrive(str(_HOME))
os.environ.update({
    "USERPROFILE": str(_HOME),      # ntpath.expanduser checks this first
    "HOME": str(_HOME),             # posixpath.expanduser
    "HOMEDRIVE": _drive or "",
    "HOMEPATH": _rest or str(_HOME),
    "TEMP": str(_TMP),
    "TMP": str(_TMP),
    "TMPDIR": str(_TMP),
})
# env alone is not enough in-process: tempfile caches gettempdir() on first use,
# and mkdtemp() above already primed it with the REAL temp dir.
tempfile.tempdir = str(_TMP)
assert Path.home() == _HOME, (
    f"sandbox home not in effect (Path.home()={Path.home()}); refusing to run "
    f"against the real ~/.claude")


def _cleanup_sandbox():
    """Close every sqlite handle this process opened, then REMOVE the sandbox.

    Every connection alive in this process was opened by this suite, so closing
    them all is exactly "close your own handles". It is needed because
    `MemoryDB._connect()` is consumed as `with self._connect() as conn:`
    throughout the package and sqlite3's context manager COMMITS BUT DOES NOT
    CLOSE; the connection then survives inside its own statement-cache
    reference cycle and keeps memory.db open, which on Windows is a hard
    PermissionError [WinError 32] on rmtree.

    Deliberate literal twin of tests/test_surfaces.py:_cleanup_sandbox --
    these two files are standalone scripts that cannot import each other, and
    a shared helper module would have to live inside the package under test.
    """
    for _conn in [o for o in gc.get_objects()
                  if isinstance(o, sqlite3.Connection)]:
        try:
            _conn.close()
        except sqlite3.Error:
            # why: an already-closed or mid-statement handle. The only goal is
            # releasing the OS file handle before rmtree, and one we cannot
            # release is caught by the rmtree check below anyway.
            pass
    gc.collect()          # breaks the statement-cache cycles described above
    # core.logger caches Logger objects in a module-level dict and each keeps an
    # OPEN append handle on <home>/.claude/hooks/cc-memory/logs/cc-memory-*.log
    # for the life of the process. That path is inside the sandbox home, so it
    # blocks rmtree before it ever reaches the databases.
    try:
        from core import logger as _logger_mod
        for _lg in list(getattr(_logger_mod, "_loggers", {}).values()):
            _lg.close()
    except ImportError:
        # why: teardown must work even if the package never became importable
        # (a failure during bootstrap) -- there is then nothing to close
        pass
    tempfile.tempdir = None
    try:
        shutil.rmtree(_SANDBOX)
    except OSError as exc:
        left = sorted(str(p) for p in _SANDBOX.rglob("*") if p.is_file())
        raise AssertionError(
            f"sandbox {_SANDBOX} survived cleanup ({exc}); {len(left)} file(s) "
            f"leaked into the real %TEMP%: {left[:10]}")


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cc_memory"))

from core.encoding_setup import enable_utf8_io
# Explicitly, and BEFORE this file's first line of output -- the same rule this
# suite pins on its two siblings further down. Keep the phrase "p r i n t ("
# out of every line above this one: the checker compares code lines, and a
# docstring mentioning it would read as output emitted before the call.
enable_utf8_io()

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

    # === v2.2 features: enable_utf8_io is callable + idempotent ============
    from core.encoding_setup import enable_utf8_io
    enable_utf8_io()
    enable_utf8_io()  # idempotent
    print("[OK] enable_utf8_io() runs + is idempotent")

    # === v2.2 features: status checker layout inspector ====================
    # Build a fake plugin tree with all required files + hooks.json, and a
    # second one missing two files, to verify _inspect_layout's verdict.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cc_memory" / "cli"))
    from cli.mem import _inspect_layout, _REQUIRED_PLUGIN_FILES, _print_layout_report

    good_root = Path(tempfile.mkdtemp(prefix="cc-memory-fakeplugin-"))
    for rel in _REQUIRED_PLUGIN_FILES:
        target = good_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel == "hooks/hooks.json":
            target.write_text(_json.dumps({
                "hooks": {
                    "PreCompact": [], "SessionStart": [], "Stop": [],
                    "PostToolUse": [], "UserPromptSubmit": [],
                }
            }), encoding="utf-8")
        else:
            target.write_text("# stub\n", encoding="utf-8")

    verdict = _inspect_layout("marketplace-directory", good_root,
                              hooks_via="plugin-manifest", enabled=True)
    assert verdict["plugin_files_ok"] is True, "all files present should be OK"
    assert verdict["missing_files"] == []
    assert set(verdict["hooks_registered"]) == {
        "PreCompact", "SessionStart", "Stop", "PostToolUse", "UserPromptSubmit"
    }, f"got hooks: {verdict['hooks_registered']}"
    assert _print_layout_report(verdict) is True, "fully-functional layout should report True"
    print(f"[OK] _inspect_layout (good): files_ok + 5/5 hooks registered")

    # Bad layout: drop two files
    (good_root / "cc_memory/core/db.py").unlink()
    (good_root / "hooks/hooks.json").unlink()
    bad_verdict = _inspect_layout("marketplace-directory", good_root,
                                  hooks_via="plugin-manifest", enabled=True)
    assert bad_verdict["plugin_files_ok"] is False
    assert "cc_memory/core/db.py" in bad_verdict["missing_files"]
    assert bad_verdict["hooks_registered"] == []
    assert _print_layout_report(bad_verdict) is False
    print(f"[OK] _inspect_layout (bad): correctly reports missing files + 0/5 hooks")

    # === v2.2 features: MEMORY.md warning block ============================
    mem_text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "AUTO-GENERATED by cc-memory" in mem_text, \
        "MEMORY.md missing strong warning header"
    assert "DO NOT EDIT THIS FILE BY HAND" in mem_text
    assert "/cc-mem add" in mem_text
    print("[OK] MEMORY.md regenerated with strong DO-NOT-EDIT warning block")

    # === v2.2 features: live plan anchor ====================================
    # Verify the full plan lifecycle: v4 migration → capture → refine →
    # TodoWrite sync → guardian nudge thresholds → sensitive-tool bump.
    from core import plan as plan_mod

    tmp_plan = Path(tempfile.mkdtemp(prefix="cc-memory-plan-"))
    mem_p = tmp_plan / "memory"; mem_p.mkdir(parents=True, exist_ok=True)
    db_p = MemoryDB(mem_p / "memory.db")
    pid_p = db_p.upsert_project(str(tmp_plan))

    # v4 migration applied?
    with db_p._connect() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "plan_active" in tables, "v4_plan_active migration missing"
        cols = [r[1] for r in conn.execute("PRAGMA table_info(plan_active)").fetchall()]
        for col in ("raw", "structured", "active_step", "needs_refine",
                    "edits_since_last_guardian", "turns_since_last_guardian"):
            assert col in cols, f"plan_active.{col} missing"
    print("[OK] v4 migration: plan_active table + all expected columns")

    # Capture: simulate ExitPlanMode firing
    raw = (
        "Implement JWT auth for the dashboard.\n\n"
        "Steps:\n"
        "1. Wire up token refresh\n"
        "2. Add CSRF protection\n"
        "3. Write integration tests in tests/test_auth.py\n\n"
        "Success: all routes return 401 without token; tests pass."
    )
    plan_mod.capture_exit_plan_mode(db_p, pid_p, raw, memory_dir=mem_p)
    row = db_p.get_plan_active(pid_p)
    assert row["raw"] == raw
    assert row["needs_refine"] == 1
    assert (mem_p / ".plan_raw.md").exists()
    print("[OK] capture_exit_plan_mode: raw stored, needs_refine=1, .plan_raw.md written")

    # Refine: apply a simulated refiner output
    refined = {
        "goal": "Implement JWT auth for the dashboard",
        "success_criteria": [
            "All routes return 401 without token",
            "tests in tests/test_auth.py pass",
        ],
        "steps": [
            {"id": 1, "title": "Wire up token refresh",   "status": "pending", "notes": ""},
            {"id": 2, "title": "Add CSRF protection",     "status": "pending", "notes": ""},
            {"id": 3, "title": "Write integration tests", "status": "pending", "notes": ""},
        ],
        "context": "JWT chosen over sessions for horizontal scaling.",
    }
    result = plan_mod.apply_refined_plan(db_p, pid_p, refined, memory_dir=mem_p)
    assert plan_mod.is_valid_structured(result), "refined plan failed validation"
    row = db_p.get_plan_active(pid_p)
    assert row["needs_refine"] == 0
    assert row["last_refined_at"]
    assert row["active_step"] == 1, f"expected active_step=1 (first pending), got {row['active_step']}"
    assert (mem_p / "PLAN.md").exists()
    plan_md_text = (mem_p / "PLAN.md").read_text(encoding="utf-8")
    assert "Implement JWT auth" in plan_md_text
    assert "DO NOT EDIT" in plan_md_text
    print("[OK] apply_refined_plan: structured stored, needs_refine=0, PLAN.md generated")

    # Schema validation should reject malformed plans
    try:
        plan_mod.apply_refined_plan(db_p, pid_p, {"goal": ""}, memory_dir=mem_p)
        assert False, "empty goal should have raised"
    except ValueError:
        pass
    print("[OK] apply_refined_plan: rejects malformed plans")

    # TodoWrite sync
    todos = [
        {"content": "Wire up token refresh", "status": "completed", "activeForm": "Wiring"},
        {"content": "Add CSRF protection",   "status": "in_progress", "activeForm": "Adding CSRF"},
        {"content": "Random unrelated task", "status": "pending",     "activeForm": "Doing random"},
    ]
    info = plan_mod.apply_todowrite_sync(db_p, pid_p, todos, memory_dir=mem_p)
    assert info["n_matched"] == 2, f"expected 2 matches, got {info['n_matched']}"
    assert info["n_unmatched"] == 1, f"expected 1 unmatched (drift signal), got {info['n_unmatched']}"
    row = db_p.get_plan_active(pid_p)
    steps = row["structured"]["steps"]
    assert steps[0]["status"] == "done"
    assert steps[1]["status"] == "in_progress"
    assert steps[2]["status"] == "pending"  # untouched
    assert row["active_step"] == 2
    print(f"[OK] sync_todos_to_steps: {info['n_matched']} matched, "
          f"{info['n_unmatched']} unmatched, active=#{row['active_step']}")

    # Done steps don't regress
    plan_mod.apply_todowrite_sync(db_p, pid_p, [
        {"content": "Wire up token refresh", "status": "pending", "activeForm": "X"},
    ], memory_dir=mem_p)
    assert db_p.get_plan_active(pid_p)["structured"]["steps"][0]["status"] == "done", \
        "done step regressed to pending"
    print("[OK] done steps don't regress on TodoWrite re-sync")

    # Guardian nudge thresholds
    row = db_p.get_plan_active(pid_p)
    nudge, reason = plan_mod.should_nudge_guardian(row)
    assert not nudge, f"should not nudge on fresh plan: {reason}"

    # Bump turns past threshold
    for _ in range(10):
        db_p.bump_plan_turn_counter(pid_p)
    row = db_p.get_plan_active(pid_p)
    nudge, reason = plan_mod.should_nudge_guardian(row, turn_threshold=8)
    assert nudge and "turn_threshold" in reason, f"turn nudge missing: {reason}"
    print(f"[OK] guardian nudge: triggered on turn threshold ({reason})")

    # Reset, then bump edits
    db_p.reset_plan_guardian_counters(pid_p)
    for _ in range(15):
        db_p.bump_plan_edit_counter(pid_p)
    row = db_p.get_plan_active(pid_p)
    nudge, reason = plan_mod.should_nudge_guardian(row, edit_threshold=12)
    assert nudge and "edit_threshold" in reason, f"edit nudge missing: {reason}"
    print(f"[OK] guardian nudge: triggered on edit threshold ({reason})")

    # Sensitive tool detection
    assert plan_mod.is_sensitive_tool_call("Bash", {"command": "git push origin main"})
    assert plan_mod.is_sensitive_tool_call("Bash", {"command": "rm -rf node_modules"})
    assert plan_mod.is_sensitive_tool_call("Bash", {"command": "npm publish"})
    assert not plan_mod.is_sensitive_tool_call("Bash", {"command": "git status"})
    assert not plan_mod.is_sensitive_tool_call("Bash", {"command": "ls -la"})
    assert not plan_mod.is_sensitive_tool_call("Edit", {"file_path": "/x"})
    print("[OK] is_sensitive_tool_call: matches git push / rm -rf / publish, not status/ls/Edit")

    # needs_refine=1 should NOT trigger guardian nudge (refiner nudge takes priority)
    db_p.upsert_plan_active(pid_p, needs_refine=1)
    row = db_p.get_plan_active(pid_p)
    nudge, reason = plan_mod.should_nudge_guardian(row)
    assert not nudge and reason == "needs_refine_first", \
        f"expected needs_refine_first, got {reason}"
    print("[OK] guardian suppressed while needs_refine=1 (refiner takes priority)")

    # plan-clear pathway
    db_p.upsert_plan_active(pid_p, needs_refine=0)
    db_p.clear_plan_active(pid_p)
    assert db_p.get_plan_active(pid_p) is None
    print("[OK] clear_plan_active: row deleted")

    # === v5 features: session annotation on progress row ====================
    # Verifies the v5 migration + tag_progress_session semantics + §0 render.
    tmp_s = Path(tempfile.mkdtemp(prefix="cc-memory-session-tag-"))
    mem_s = tmp_s / "memory"; mem_s.mkdir(parents=True, exist_ok=True)
    db_s = MemoryDB(mem_s / "memory.db")
    pid_s = db_s.upsert_project(str(tmp_s))

    # v5 migration applied?
    with db_s._connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(progress)").fetchall()]
        assert "current_session_id" in cols, "v5_progress_session_id missing"
        assert "session_started_at" in cols, "v5_progress_session_started_at missing"
    print("[OK] v5 migration: progress.current_session_id + session_started_at present")

    # 1) Empty progress + first tag → both fields set
    db_s.upsert_progress(pid_s)  # bootstrap empty row
    prog0 = db_s.get_progress(pid_s)
    assert prog0["current_session_id"] == ""
    assert prog0["session_started_at"] == ""

    SID_A = "aaaa1111-2222-3333-4444-555566667777"
    db_s.tag_progress_session(pid_s, SID_A)
    prog1 = db_s.get_progress(pid_s)
    assert prog1["current_session_id"] == SID_A
    started_a = prog1["session_started_at"]
    assert started_a, "started_at should be set on first tag"
    print(f"[OK] tag_progress_session: first call sets sid + started_at ({started_a})")

    # 2) Same sid again → no-op (started_at preserved)
    db_s.tag_progress_session(pid_s, SID_A)
    prog2 = db_s.get_progress(pid_s)
    assert prog2["session_started_at"] == started_a, \
        f"idempotency violated: started_at changed from {started_a} to {prog2['session_started_at']}"
    print("[OK] tag_progress_session: idempotent on same sid (started_at preserved)")

    # 3) Empty / None sid → no-op
    db_s.tag_progress_session(pid_s, "")
    db_s.tag_progress_session(pid_s, None)
    prog3 = db_s.get_progress(pid_s)
    assert prog3["current_session_id"] == SID_A
    print("[OK] tag_progress_session: empty/None sid is no-op")

    # 4) Different sid → new tag + new started_at
    import time as _time
    _time.sleep(1.05)  # ensure timestamp differs at second resolution
    SID_B = "bbbb9999-8888-7777-6666-555544443333"
    db_s.tag_progress_session(pid_s, SID_B)
    prog4 = db_s.get_progress(pid_s)
    assert prog4["current_session_id"] == SID_B
    assert prog4["session_started_at"] != started_a, "new session must reset started_at"
    started_b = prog4["session_started_at"]
    print(f"[OK] tag_progress_session: switching sid resets started_at "
          f"({started_a} -> {started_b})")

    # 5) upsert_progress preserves tag when caller doesn't pass session fields
    db_s.upsert_progress(pid_s, current_request="some new request",
                          status_done="something completed")
    prog5 = db_s.get_progress(pid_s)
    assert prog5["current_session_id"] == SID_B, \
        f"upsert wiped the tag: expected {SID_B}, got {prog5['current_session_id']!r}"
    assert prog5["session_started_at"] == started_b, \
        "upsert wiped started_at"
    assert prog5["current_request"] == "some new request"
    print("[OK] upsert_progress preserves session tag across full-rewrite")

    # 6) get_recent_sessions returns the right shape (with summaries joined)
    sess_id1 = db_s.insert_session(pid_s, SID_A, "auto", 100, "", "Session A archive")
    db_s.insert_session_summary(sess_id1, pid_s, {
        "request": "first thing",
        "completed": "First session got JWT wired up",
        "next_steps": "Add CSRF; Write tests",
        "files_read": [], "files_modified": [],
    })
    sess_id2 = db_s.insert_session(pid_s, "cccc0000", "manual", 42, "", "Older session")
    recent = db_s.get_recent_sessions(pid_s, n=5)
    assert len(recent) == 2
    assert recent[0]["claude_session_id"] == "cccc0000" or recent[0]["claude_session_id"] == SID_A, \
        f"got unexpected first session: {recent[0]}"
    # The session with a summary should have summary_completed populated
    sess_a = next(r for r in recent if r["claude_session_id"] == SID_A)
    assert "JWT" in (sess_a["summary_completed"] or "")
    print(f"[OK] get_recent_sessions: {len(recent)} rows, summary JOIN works")

    # 7) PROGRESS.md render contains §0 with current sid + prior session
    write_progress_md(db_s, pid_s, mem_s)
    prog_md = (mem_s / "PROGRESS.md").read_text(encoding="utf-8")
    assert "## 0. Session" in prog_md, "§0 Session section missing from PROGRESS.md"
    assert "Current session" in prog_md
    # Short SID is first 8 chars of SID_B = "bbbb9999"
    assert "bbbb9999" in prog_md, "current short sid not rendered"
    assert "Prior sessions" in prog_md, "prior sessions block missing"
    assert "JWT" in prog_md or "first thing" in prog_md, \
        "prior session summary not rendered"
    # SID_A is the current's PRIOR session (it was inserted into `sessions` after
    # SID_B took over), so SID_A should appear in the timeline.
    assert "aaaa1111" in prog_md, "prior session sid not in timeline"
    print("[OK] PROGRESS.md §0: current sid + prior session timeline rendered")

    # 8) Untagged → graceful render
    db_s2 = MemoryDB(Path(tempfile.mkdtemp(prefix="cc-mem-untag-")) / "memory.db")
    pid_s2 = db_s2.upsert_project("/tmp/untagged-proj")
    db_s2.upsert_progress(pid_s2)
    tmp_mem = Path(tempfile.mkdtemp(prefix="cc-mem-untag-mem-"))
    write_progress_md(db_s2, pid_s2, tmp_mem)
    untagged_md = (tmp_mem / "PROGRESS.md").read_text(encoding="utf-8")
    assert "no session tagged" in untagged_md, "untagged path should say so explicitly"
    assert "no prior compacted sessions" in untagged_md
    print("[OK] PROGRESS.md §0: untagged + empty-history path renders gracefully")

    # === v2.3 features: memory-quality (dedup / staleness / topic / aging) ===
    import core.consolidate as C

    # v6 migration present
    tmp_q = Path(tempfile.mkdtemp(prefix="cc-mem-quality-"))
    mem_q = tmp_q / "memory"; mem_q.mkdir(parents=True, exist_ok=True)
    db_q = MemoryDB(mem_q / "memory.db")
    pid_q = db_q.upsert_project(str(tmp_q))
    with db_q._connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
        assert "last_referenced_at" in cols, "v6_last_referenced_at migration missing"
    print("[OK] v6 migration: memories.last_referenced_at present")

    # Step 0 helpers
    assert C.is_decodable("normal text here") is True
    assert C.is_decodable("���������������� mostly fffd ��������") is False
    assert C.is_decodable("") is False
    assert C.is_decodable("有效的中文内容不是乱码") is True, "valid CJK must be decodable"
    print("[OK] is_decodable: rejects FFFD-dominated, accepts CJK")

    g = C.BudgetGate(total_s=45, safety_s=8)
    assert g.can_spend(10) is True
    gu = C.BudgetGate.unbounded_gate()
    assert gu.can_spend(1e9) is True and gu.remaining() == float("inf")
    g0 = C.BudgetGate(total_s=5, safety_s=8)  # already over budget
    assert g0.can_spend(1) is False, "exhausted gate must refuse"
    print("[OK] BudgetGate: bounded/unbounded/exhausted behave correctly")

    # _nominate_groups: NO giant transitive cluster from a shared hub token
    def _mk(idv, cat, content, imp=3):
        return {"id": idv, "category": cat, "content": content,
                "importance": imp, "created_at": "2026-01-01T00:00:00",
                "tags": "[]", "topic": ""}
    # 5 rows all sharing 'cc-memory settings hooks' but DISTINCT facts +
    # 2 genuine dups. Hub tokens must NOT chain the distinct ones into one blob.
    hub = [
        _mk(1, "config", "cc-memory stores settings in settings.json hooks block"),
        _mk(2, "config", "cc-memory memory database lives at memory/memory.db path"),
        _mk(3, "config", "cc-memory installer copies hooks into settings.json on setup"),
        _mk(4, "config", "cc-memory uninstall removes hooks from settings.json file"),
        _mk(5, "decision", "cc-memory uses SQLite with settings hooks for storage"),
        # genuine near-dup pair (same fact reworded), same category:
        _mk(6, "arch", "The plugin captures memories at every conversation boundary hook"),
        _mk(7, "arch", "The plugin captures memories at each conversation boundary via hooks"),
    ]
    groups = C._nominate_groups(hub, floor=0.30, max_group=4, max_groups=12)
    for grp in groups:
        assert len(grp) <= 4, f"group exceeded max size: {len(grp)}"
        cats = {m["category"] for m in grp}
        assert len(cats) == 1, f"cross-category group formed: {cats}"
    # the genuine arch pair (6,7) should be nominated together
    arch_grouped = any({6, 7} <= {m["id"] for m in grp} for grp in groups)
    assert arch_grouped, "genuine reworded-dup pair (6,7) not nominated"
    # no single group should swallow all 5 config hub rows
    assert not any(len([m for m in grp if m["category"] == "config"]) >= 5
                   for grp in groups), "hub tokens created a giant cross-fact cluster"
    print(f"[OK] _nominate_groups: {len(groups)} groups, all <=4 + same-category, "
          f"genuine dup paired, no hub mega-cluster")

    # mojibake rows are skipped by nomination
    moji = [_mk(10, "note", "����������������������������������������"),
            _mk(11, "note", "����������������������������������������")]
    assert C._nominate_groups(moji) == [], "mojibake rows must be skipped"
    print("[OK] _nominate_groups: skips non-decodable (mojibake) rows")

    # semantic_dedup no-ops gracefully without an API key (don't assume one)
    sd = C.semantic_dedup(db_q, pid_q, use_llm=False)
    assert sd["memories_archived"] == 0
    print("[OK] semantic_dedup: safe no-op when use_llm=False")

    # Step 4: decay_and_archive — durable spared, old+low+unreferenced archived
    from datetime import datetime as _dt, timedelta as _td
    old = (_dt.now() - _td(days=200)).isoformat(timespec="seconds")
    recent = _dt.now().isoformat(timespec="seconds")
    with db_q._connect() as conn:
        # durable, important, recent → keep
        conn.execute("INSERT INTO memories (project_id,category,content,importance,tags,created_at,updated_at,is_active) VALUES (?,?,?,?,?,?,?,1)",
                     (pid_q, "arch", "Durable architecture invariant still true", 4, "[]", recent, recent))
        # old + low importance + never referenced → archive net catches it
        conn.execute("INSERT INTO memories (project_id,category,content,importance,tags,created_at,updated_at,is_active) VALUES (?,?,?,?,?,?,?,1)",
                     (pid_q, "note", "Ancient trivial note nobody referenced", 1, "[]", old, old))
        # old + low BUT referenced → spared
        conn.execute("INSERT INTO memories (project_id,category,content,importance,tags,created_at,updated_at,last_referenced_at,is_active) VALUES (?,?,?,?,?,?,?,?,1)",
                     (pid_q, "note", "Old but injected recently so still relevant", 1, "[]", old, old, recent))
    da = C.decay_and_archive(db_q, pid_q)
    active = {m["id"]: m for m in db_q.get_all_active_memories(pid_q)}
    contents = {m["content"] for m in active.values()}
    assert "Durable architecture invariant still true" in contents, "durable row wrongly archived"
    assert "Ancient trivial note nobody referenced" not in contents, "old+low+unref not archived"
    assert "Old but injected recently so still relevant" in contents, "referenced row wrongly archived"
    assert da["archived_stale"] == 1, f"expected 1 stale archived, got {da['archived_stale']}"
    print(f"[OK] decay_and_archive: durable+referenced spared, old+low+unref archived ({da})")

    # Step 0 db helpers: bump_last_referenced + get_referenced_id_set + archive_obsolete
    fresh_id = db_q.insert_memory(pid_q, None, "note", "reference me please", importance=2)
    assert fresh_id not in db_q.get_referenced_id_set(pid_q)
    db_q.bump_last_referenced([fresh_id])
    assert fresh_id in db_q.get_referenced_id_set(pid_q)
    surv = db_q.insert_memory(pid_q, None, "note", "survivor canonical fact", importance=3)
    loser = db_q.insert_memory(pid_q, None, "note", "loser duplicate fact", importance=2)
    n = db_q.archive_obsolete([loser], canonical_id=surv)
    assert n == 1
    loser_active = {m["id"] for m in db_q.get_all_active_memories(pid_q)}
    assert loser not in loser_active, "archive_obsolete didn't archive"
    chain = db_q.get_supersede_chain(loser)
    assert any(c["id"] == loser and c["supersedes_id"] == surv for c in chain), \
        "archive_obsolete didn't set forward supersedes_id link"
    print("[OK] bump_last_referenced + get_referenced_id_set + archive_obsolete(forward-link)")

    # Step 5: canonicalize_topics — cc-memory family merges, distinct memory-* stays
    tmp_t = Path(tempfile.mkdtemp(prefix="cc-mem-topic-"))
    mem_t = tmp_t / "memory"; mem_t.mkdir(parents=True, exist_ok=True)
    db_t = MemoryDB(mem_t / "memory.db")
    pid_t = db_t.upsert_project(str(tmp_t))
    topic_seed = [
        ("cc-memory", "fact a about the plugin"),
        ("cc-memory-fixes", "fact b about fixes"),
        ("cc-memory backend", "fact c about backend"),
        ("memory-bloat", "distinct fact about bloat problem"),
        ("memory-injection", "distinct fact about injection layer"),
    ]
    for tp, ct in topic_seed:
        mid = db_t.insert_memory(pid_t, None, "note", ct, importance=3, topic=tp)
    merged = C.canonicalize_topics(db_t, pid_t)
    final_topics = set(db_t.get_topic_memory_counts(pid_t).keys())
    # cc-memory family collapses to one
    ccmem_family = {t for t in final_topics if t.startswith("cc-memory") or t == "cc-memory"}
    assert len(ccmem_family) == 1, f"cc-memory family not unified: {ccmem_family}"
    # distinct memory-* survive as their own topics (hub-token guard)
    assert "memory-bloat" in final_topics, "memory-bloat wrongly merged via hub token"
    assert "memory-injection" in final_topics, "memory-injection wrongly merged via hub token"
    print(f"[OK] canonicalize_topics: cc-memory family unified ({merged} merged), "
          f"distinct memory-* preserved")

    # archive_consolidated content-dup guard: distinct facts sharing a topic are NOT archived
    tmp_a = Path(tempfile.mkdtemp(prefix="cc-mem-archcon-"))
    mem_a = tmp_a / "memory"; mem_a.mkdir(parents=True, exist_ok=True)
    db_a = MemoryDB(mem_a / "memory.db")
    pid_a = db_a.upsert_project(str(tmp_a))
    distinct_facts = [
        "JWT tokens expire after fifteen minutes by configuration",
        "PostgreSQL connection pool capped at twenty workers",
        "The dashboard renders charts with a canvas backend",
        "Nightly backups upload to an offsite bucket at 3am",
        "Rate limiting uses a sliding window of sixty seconds",
        "Email delivery routes through an SMTP relay on port 465",
        "Search indexes rebuild incrementally every six hours",
        "Feature flags load from a YAML file at boot time",
    ]
    for fct in distinct_facts:  # 8 genuinely-distinct facts, same topic, > cap 5
        db_a.insert_memory(pid_a, None, "note", fct, importance=3, topic="shared")
    db_a.upsert_topic(pid_a, "shared", "summary of shared topic")
    n_arch = C.archive_consolidated(db_a, pid_a, keep_per_topic=5)
    assert n_arch == 0, f"content-dup guard failed: archived {n_arch} distinct facts"
    print("[OK] archive_consolidated: distinct facts sharing a topic NOT archived (content guard)")

    # ...and the guard DOES archive a genuine content near-duplicate over the cap
    db_a.insert_memory(pid_a, None, "note",
                       "JWT tokens expire after fifteen minutes by config setting",
                       importance=2, topic="shared")
    n_arch2 = C.archive_consolidated(db_a, pid_a, keep_per_topic=5)
    assert n_arch2 >= 1, "content-dup guard should archive a genuine near-duplicate"
    print(f"[OK] archive_consolidated: genuine content near-dup IS archived ({n_arch2})")

    # === v2.3.2: async consolidation off the blocking compaction path ========
    import json as _json2
    import inspect as _inspect
    import importlib as _il
    _REPO = Path(__file__).resolve().parent.parent

    # (a) call_llm gained a bounded `fallback_timeout`; _worst_call_cost is honest
    from llm.ccl_backend import call_llm as _call_llm
    assert "fallback_timeout" in _inspect.signature(_call_llm).parameters, \
        "call_llm must accept fallback_timeout (bounded Ollama fallback)"
    # v2.3.4: worst case = 2 Anthropic candidates (env + OAuth fall-through)
    # x haiku timeout + the fallback leg reservation.
    assert C._worst_call_cost(20, 20) == 60.0 and C._worst_call_cost(25, 20) == 70.0, \
        "_worst_call_cost must reserve 2 Anthropic legs + fallback"
    print("[OK] v2.3.4 call_llm.fallback_timeout + _worst_call_cost honest cost model")

    # (b) consolidate_topics is budget-gated: an EXHAUSTED gate must NOT start an
    #     LLM call it can't afford, yet still summarize every topic via the
    #     no-LLM fallback (closes the pre-2.3.2 ungated-loop → "Hook cancelled").
    assert "budget" in _inspect.signature(C.consolidate_topics).parameters, \
        "consolidate_topics must accept a budget"
    tmp_ct = Path(tempfile.mkdtemp(prefix="cc-mem-ctopic-"))
    mem_ct = tmp_ct / "memory"; mem_ct.mkdir(parents=True, exist_ok=True)
    db_ct = MemoryDB(mem_ct / "memory.db")
    pid_ct = db_ct.upsert_project(str(tmp_ct))
    for i in range(3):
        db_ct.insert_memory(pid_ct, None, "note",
                            f"topic-alpha fact number {i} with specific detail",
                            importance=3, topic="alpha")
    exhausted = C.BudgetGate(total_s=1, safety_s=8)  # remaining < 0 → refuses all
    assert exhausted.can_spend(
        C._worst_call_cost(C._SUMMARY_HAIKU_S, C._SUMMARY_FALLBACK_S)) is False
    n_ct = C.consolidate_topics(db_ct, pid_ct, use_llm=True, budget=exhausted)
    assert n_ct >= 1 and "alpha" in {t["name"] for t in db_ct.get_topics(pid_ct)}, \
        "consolidate_topics must fallback-summarize the topic under an exhausted budget"
    print("[OK] v2.3.2 consolidate_topics: budget-gated, fallback-summarizes when exhausted")

    # (c) hooks.json: PreCompact carries TWO command hooks; 2nd is async + 300s
    hj = _json2.loads((_REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    pc_hooks = hj["hooks"]["PreCompact"][0]["hooks"]
    assert len(pc_hooks) == 2, f"PreCompact must declare 2 hooks, got {len(pc_hooks)}"
    sync_h = [h for h in pc_hooks if "pre_compact.py" in h["command"]]
    async_h = [h for h in pc_hooks if "consolidate_async.py" in h["command"]]
    assert sync_h and not sync_h[0].get("async"), "sync leg must be pre_compact.py (not async)"
    assert async_h and async_h[0].get("async") is True and async_h[0]["timeout"] == 300, \
        "consolidate_async.py must be async:true with timeout 300"
    print("[OK] v2.3.2 hooks.json: PreCompact = sync pre_compact + async consolidate (300s)")

    # (d) installer emits the same 2-hook PreCompact shape + ships the new file
    from ui import installer as _inst
    ipc = _inst._make_hooks_config(Path("/tmp/cc-mem-install"))["PreCompact"][0]["hooks"]
    assert len(ipc) == 2 and any(h.get("async") for h in ipc), \
        "installer PreCompact must emit sync + async hooks"
    assert "consolidate_async.py" in _inst.SUBPACKAGE_FILES["hooks"], \
        "installer must ship consolidate_async.py"
    assert "cc_memory/hooks/consolidate_async.py" in _REQUIRED_PLUGIN_FILES, \
        "layout inspector must require consolidate_async.py"
    print("[OK] v2.3.2 installer: 2-hook PreCompact parity + ships/requires consolidate_async.py")

    # (e) consolidate_async hook: importable + marker/lock/interval primitives
    _ca = _il.import_module("hooks.consolidate_async")
    assert _ca._auto_interval() >= 1
    tmp_ca = Path(tempfile.mkdtemp(prefix="cc-mem-async-"))
    lock = tmp_ca / ".consolidation.lock"
    assert _ca._acquire_lock(lock) is True, "first lock acquire must succeed"
    assert _ca._acquire_lock(lock) is False, "second acquire (fresh lock) must fail"
    _ca._release_lock(lock)
    assert not lock.exists(), "release must remove the lock"
    marker = tmp_ca / ".last_consolidation.json"
    assert _ca._read_marker(marker) == {}, "missing marker reads as {}"
    _ca._write_marker(marker, {"last_session_count": 42})
    assert _ca._read_marker(marker)["last_session_count"] == 42
    print("[OK] v2.3.2 consolidate_async: importable + lock/marker/interval logic")

    # === i18n: documentation multilingual drift gate =========================
    # Import the dev checker (lives in tools/, outside the package) and assert no
    # tracked English doc changed without its translation being refreshed. See
    # docs/ARCHITECTURE.md#9-documentation-language-convention-i18n. STALE/ORPHAN/NO-MARKER are hard failures; MISSING-TRANSLATION
    # is a soft warning (translations are produced on demand) and does not gate.
    sys.path.insert(0, str(_REPO / "tools"))
    import i18n_check
    _i18n = i18n_check.classify(_REPO)
    _drift = [r for r in _i18n if r.state in ("STALE", "ORPHAN", "NO-MARKER")]
    assert not _drift, \
        f"i18n drift detected: {[(r.state, r.english_rel or r.zh_rel) for r in _drift]}"
    _zh = _REPO / "README.zh.md"
    assert _zh.exists(), "README.zh.md missing (reference translation must be committed)"
    _mk = i18n_check.parse_marker(_zh)
    assert _mk is not None, "README.zh.md has no valid i18n marker on line 1"
    assert _mk["digest"] == i18n_check.hash_source(_REPO / "README.md"), \
        "README.zh.md marker hash != current README.md (stale translation)"
    print("[OK] i18n: README.zh.md in-sync with README.md; no drift across tracked docs")

    # === v2.4.2: bounded transcript window (hook-safe) =======================
    # An unbounded transcript read is what killed PreCompact on large projects:
    # a 2.11 GiB transcript parses at ~25 MiB/s (~88s) against a 120s budget, so
    # the hook was killed mid-write. load_transcript_window reads a head+tail
    # slice instead. Contract: identical to the unbounded loader for normal
    # files, exact record count when truncated, never admits a partial record.
    import json as _json3
    import importlib.util as _ilu
    from datetime import datetime as _dt2, timedelta as _td2
    from core.extractor import (load_transcript as _lt,
                                load_transcript_window as _ltw)
    _tr_dir = tmp / "transcripts"
    _tr_dir.mkdir(exist_ok=True)

    _small = _tr_dir / "small.jsonl"
    _recs = [{"type": "user", "message": {"role": "user", "content": f"msg {i}"}}
             for i in range(50)]
    _small.write_text("\n".join(_json3.dumps(r) for r in _recs) + "\n", encoding="utf-8")
    _w = _ltw(str(_small))
    assert _w.truncated is False, "small transcript must not be truncated"
    assert _w.messages == _lt(str(_small)), "bounded read diverged from unbounded on a small file"
    assert _w.total_records == 50, _w.total_records

    # Force truncation with a tiny tail budget: head+tail must both be present,
    # total_records must stay EXACT (it is counted, not inferred from the window).
    _w2 = _ltw(str(_small), head_records=3, tail_bytes=512)
    assert _w2.truncated is True, "tail_bytes=512 should have truncated"
    assert _w2.total_records == 50, f"record count degraded to {_w2.total_records}"
    assert len(_w2.messages) < 50, "truncated window should hold fewer records"
    assert _w2.head[0]["message"]["content"] == "msg 0", "head lost the FIRST record"
    assert _w2.tail[-1]["message"]["content"] == "msg 49", "tail lost the LAST record"
    assert all(isinstance(m, dict) for m in _w2.messages), "partial record leaked through"
    assert _ltw(str(_tr_dir / "nope.jsonl")).messages == [], "missing file must degrade to empty"
    print("[OK] v2.4.2 bounded window: small-file parity, exact count, head+tail intact")

    # --- window edge cases (each one was a real defect found in review) ------
    # (a) head and tail ranges must never overlap: duplicated records inflate
    #     keyword frequencies, and db.upsert_keywords ACCUMULATES, so a dupe
    #     permanently poisons the project vocabulary.
    _sz = _small.stat().st_size
    _rl = _sz // 50
    _wo = _ltw(str(_small), head_records=5, tail_bytes=_sz - 3 * _rl)
    assert len(_wo.messages) <= _wo.total_records, \
        f"records double-counted: {len(_wo.messages)} > {_wo.total_records}"
    assert _wo.messages == _wo.head + _wo.tail, "messages != head + tail"
    _contents = [m["message"]["content"] for m in _wo.messages]
    assert len(_contents) == len(set(_contents)), f"duplicate records: {_contents}"
    # (b) messages == head + tail must hold in the NON-truncated branch too
    assert _w.messages == _w.head + _w.tail, "non-truncated branch breaks the invariant"
    # (c) count must survive a missing trailing newline and blank separators
    _nt = _tr_dir / "notrail.jsonl"
    _nt.write_bytes(b"\n".join(_json3.dumps(r).encode() for r in _recs))  # no trailing \n
    assert _ltw(str(_nt), tail_bytes=1).total_records == 50, \
        f"no-trailing-newline undercount: {_ltw(str(_nt), tail_bytes=1).total_records}"
    _bl = _tr_dir / "blank.jsonl"
    _bl.write_bytes(b"\n\n".join(_json3.dumps(r).encode() for r in _recs) + b"\n")
    assert _ltw(str(_bl), tail_bytes=1).total_records == 50, \
        f"blank lines double-counted: {_ltw(str(_bl), tail_bytes=1).total_records}"
    # (d) a fat opening record must not re-materialise an unbounded read
    _fat = _tr_dir / "fat.jsonl"
    _fat.write_bytes(b"\n".join(
        [_json3.dumps({"type": "user", "message": {"role": "user", "content": "X" * (9 << 20)}}).encode()]
        + [_json3.dumps(r).encode() for r in _recs]) + b"\n")
    _wf = _ltw(str(_fat), head_records=40, tail_bytes=1 << 20)
    assert sum(len(_json3.dumps(m)) for m in _wf.head) < (9 << 20), \
        "oversized head record was materialised; head is not byte-bounded"
    # (e) a seek landing exactly on a record boundary must KEEP that record
    _uni = _tr_dir / "uniform.jsonl"
    _urecs = [('{"i":%02d,"pad":"%s"}' % (i, "y" * 40)).encode() for i in range(20)]
    _uni.write_bytes(b"\n".join(_urecs) + b"\n")
    _urlen = len(_urecs[0]) + 1
    assert [m["i"] for m in _ltw(str(_uni), head_records=3, tail_bytes=5 * _urlen).tail] \
        == [15, 16, 17, 18, 19], "boundary-aligned seek dropped a whole record"
    print("[OK] v2.4.2 window edges: no overlap, byte-bounded head, exact count, boundary-safe")

    # === v2.4.2: LLM summary reads the RECENT end, not the oldest ============
    # Regression guard for the silent 70-day staleness bug: the summary filled
    # its 12k budget from the OLDEST record and broke, so on a long session the
    # extractor only ever saw the session's opening minutes.
    _pc_spec = _ilu.spec_from_file_location(
        "_pc_sm", _REPO / "cc_memory" / "hooks" / "pre_compact.py")
    _pc = _ilu.module_from_spec(_pc_spec)
    _pc_spec.loader.exec_module(_pc)
    _long = [{"type": "user", "message": {"role": "user", "content": f"line {i} " + "x" * 200}}
             for i in range(400)]
    _summary = _pc._build_transcript_summary(_long, max_chars=2000)
    assert "line 399" in _summary, "summary dropped the MOST RECENT message"
    assert "line 0 " not in _summary, "summary still anchored to the oldest message"
    assert "earlier messages omitted" in _summary, "omission notice missing"
    # _first_user_request must see past leading meta rows (queue-operation etc.)
    _meta = [{"type": "attachment", "message": {"content": ""}} for _ in range(5)]
    _meta.append({"type": "user", "message": {"role": "user", "content": "the real request"}})
    assert _pc._first_user_request(_meta) == "the real request", \
        "_first_user_request still blind past the first 5 meta records"
    print("[OK] v2.4.2 summary anchored to recent end + first-request survives meta rows")

    # === v2.4.2: killed-run visibility ======================================
    # A hook killed by the host timeout dies on TerminateProcess: no except, no
    # finally, so .last_save.json kept describing the PREVIOUS success and the
    # failure was invisible. A start marker that outlives the run proves a kill.
    _ss_spec = _ilu.spec_from_file_location(
        "_ss_sm", _REPO / "cc_memory" / "hooks" / "session_start.py")
    _ss = _ilu.module_from_spec(_ss_spec)
    _ss_spec.loader.exec_module(_ss)
    (mem_dir / ".last_save.json").write_text(_json3.dumps({
        "timestamp": "2026-08-04 18:28:59", "trigger": "auto", "method": "llm",
        "n_inserted": 9, "n_merged": 0, "n_superseded": 1, "success": True,
    }), encoding="utf-8")
    _foot = _ss._build_footer(db, pid, mem_dir)
    assert "(auto)" in _foot, "auto-trigger not surfaced — auto compaction stays invisible"
    _old_ts = (_dt2.now() - _td2(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
    _pc._write_attempt(mem_dir, "manual", "sid-x", 2262308959)
    assert (mem_dir / ".pre_compact_attempt.json").exists(), "start marker not written"
    (mem_dir / ".pre_compact_attempt.json").write_text(_json3.dumps({
        "started_at": _old_ts, "trigger": "manual", "session_id": "sid-x",
        "pid": 999999, "transcript_bytes": 2262308959}), encoding="utf-8")
    assert "DID NOT FINISH" in _ss._build_footer(db, pid, mem_dir), \
        "killed PreCompact not reported"
    # ...but a run still in flight must not be flagged
    (mem_dir / ".pre_compact_attempt.json").write_text(_json3.dumps({
        "started_at": _dt2.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trigger": "manual", "pid": 1, "transcript_bytes": 10}), encoding="utf-8")
    assert "DID NOT FINISH" not in _ss._build_footer(db, pid, mem_dir), \
        "false positive on an in-flight PreCompact"
    _pc._clear_attempt(mem_dir)
    assert not (mem_dir / ".pre_compact_attempt.json").exists(), "marker not cleared"
    # A GARBLED marker must never escape _build_footer: an AttributeError there
    # aborts build_context() and silently drops the ENTIRE injection payload
    # (no memories, no PROGRESS preview, no forced reminder).
    for _bad in ('[1,2,3]', '42', '"a string"',
                 '{"started_at":"2020-01-01 00:00:00","transcript_bytes":"big"}',
                 '{"started_at":"2020-01-01 00:00:00","transcript_bytes":null}',
                 '{"started_at":"not-a-date"}', '{not json'):
        (mem_dir / ".pre_compact_attempt.json").write_text(_bad, encoding="utf-8")
        _ss._build_footer(db, pid, mem_dir)  # must not raise
    _pc._clear_attempt(mem_dir)
    # The summary's omitted count must be transcript-relative, not window-relative
    _msgs = [{"type": "user", "message": {"role": "user", "content": f"line {i} " + "x" * 200}}
             for i in range(400)]
    import re as _re2
    _grab = lambda s: int(_re2.search(r"\[\.\.\.(\d+) earlier messages omitted", s).group(1))
    _win_rel = _grab(_pc._build_transcript_summary(_msgs, max_chars=2000))
    _abs_rel = _grab(_pc._build_transcript_summary(_msgs, max_chars=2000, total_records=100000))
    assert _win_rel < len(_msgs), f"window-relative count out of range: {_win_rel}"
    assert _abs_rel == 100000 - (len(_msgs) - _win_rel), \
        f"omitted count ignored total_records: got {_abs_rel}, window-relative was {_win_rel}"
    assert _abs_rel > _win_rel * 100, \
        "omitted count is still window-relative — it understates by the whole omitted middle"
    print("[OK] v2.4.2 robustness: garbled marker survivable, omitted count transcript-relative")
    print("[OK] v2.4.2 visibility: auto trigger shown, kill detected, no in-flight false positive")

    # ========================================================================
    # v2.5.0 regression coverage. Every block below pins a defect that SHIPPED
    # in v2.4.3 and was closed this release; each one fails against that tree.
    # ========================================================================

    # === v2.5.0 (1): privacy filtering fails CLOSED ==========================
    # Pre-v2.5 `strip_private` was `re.sub(r"<private>.*?</private>", "", t)`
    # behind a `t.count("<private>") > 100` ReDoS guard that RETURNED THE TEXT
    # UNCHANGED — the filter leaked exactly when the payload looked adversarial,
    # on BOTH guarded paths (core/extractor.py's LLM prompt and
    # llm/memory_writer.py's storage write). The cap was also calibrated on the
    # wrong signal: well-formed tags are cheap for the regex engine, an
    # UNTERMINATED one is the quadratic case (measured 16000 tags = 9517 ms).
    from core.privacy import (strip_private as _v5_strip,
                              strip_context_tags as _v5_strip_ctx,
                              clean_for_storage as _v5_clean,
                              has_private as _v5_has_priv)
    _v5_leak = "".join("keep%d <private>SECRET%d</private> " % (i, i)
                       for i in range(101))
    assert _v5_leak.count("<private>") == 101, "fixture must exceed the old cap"
    assert _v5_has_priv(_v5_leak) is True
    assert "SECRET" not in _v5_strip(_v5_leak), \
        "101 <private> tags came back verbatim — the tag cap still fails OPEN"
    assert "SECRET" not in _v5_clean(_v5_leak), \
        "clean_for_storage (the storage + LLM-prompt gate) leaked above the cap"
    assert "keep100" in _v5_strip(_v5_leak), "non-private text must survive"
    # ...and the same must hold for the anti-recursion tag at 101 spans
    _v5_ctx_leak = "".join("ok%d <cc-memory-context>BLOB</cc-memory-context> " % i
                           for i in range(101))
    assert "BLOB" not in _v5_clean(_v5_ctx_leak), \
        "cc-memory-context spans leaked above the cap (recursive re-storage)"
    # fail CLOSED: a dangling open tag drops the remainder rather than emit it
    assert _v5_strip("public prefix <private>everything here is secret") \
        == "public prefix", "unterminated <private> emitted its remainder"
    assert _v5_strip_ctx("kept <cc-memory-context>injected blob") == "kept"
    assert _v5_clean("a <private>x</private> b <private>dangling") == "a  b"
    # text with no open tag is returned byte-identical (no gratuitous .strip())
    assert _v5_strip("  no tags at all  ") == "  no tags at all  "
    # linear, not quadratic: this exact input was 5744 ms pre-v2.5
    _v5_bomb = "<private>x" * 16000
    _v5_t0 = _time.perf_counter()
    _v5_bomb_out = _v5_clean(_v5_bomb)
    _v5_ms = (_time.perf_counter() - _v5_t0) * 1000.0
    assert _v5_bomb_out == "", "unterminated-tag bomb was not dropped"
    assert _v5_ms < 1000.0, \
        f"16000 unterminated <private> tags took {_v5_ms:.0f} ms — quadratic again"
    print(f"[OK] v2.5.0 privacy fails CLOSED: 101 tags stripped, dangling tag "
          f"drops remainder, 16000-tag bomb in {_v5_ms:.2f} ms")

    # === v2.5.0 (2): session-less memories are visible =======================
    # `sessions` rows only exist after a compaction, so ALL FOUR manual save
    # paths (cli/mem.py add, mcp/server.py memory_add, ui/dashboard.py,
    # ui/web_viewer.py) write session_id NULL. The pre-v2.5 filter was a bare
    # `AND session_id IN (...)`, which NULL can never satisfy — everything the
    # user saved by hand was invisible to SessionStart injection, the web viewer
    # and MCP memory_recent.
    _v5_np = Path(tempfile.mkdtemp(prefix="cc-mem-nullsid-"))
    (_v5_np / "memory").mkdir(parents=True, exist_ok=True)
    _v5_db = MemoryDB(_v5_np / "memory" / "memory.db")
    _v5_pid = _v5_db.upsert_project(str(_v5_np))
    _v5_r = upsert_smart(_v5_db, _v5_pid, None, "decision",
                         "Manual save made before this project ever compacted",
                         4, topic="manual")
    assert _v5_r["action"] == "inserted", _v5_r
    _v5_recent = _v5_db.get_recent_memories(_v5_pid)
    assert len(_v5_recent) == 1 and _v5_recent[0]["session_id"] is None, \
        f"a project with NO sessions row hid its manual save: {_v5_recent}"
    # ...and it stays visible once real sessions DO exist (the IN-clause branch)
    _v5_sid = _v5_db.insert_session(_v5_pid, "sid-real", "auto", 5, "", "")
    _v5_db.insert_memory(_v5_pid, _v5_sid, "note",
                         "Memory extracted by a real compaction", importance=3)
    _v5_recent2 = _v5_db.get_recent_memories(_v5_pid)
    assert {m["session_id"] for m in _v5_recent2} == {None, _v5_sid}, \
        f"session-less row dropped once a sessions row existed: {_v5_recent2}"
    print("[OK] v2.5.0 get_recent_memories returns session_id NULL rows "
          "(both the no-session and the IN-clause branch)")

    # === v2.5.0 (3): hook LLM budget arithmetic ==============================
    # `urlopen(timeout=)` is a PER-SOCKET-OPERATION timeout: it covers neither
    # DNS nor the TLS handshake, so per-leg timeouts alone never bounded a hook.
    # Each LLM-calling hook now captures _HOOK_T0 BEFORE its package imports and
    # passes an ABSOLUTE deadline into call_llm. This block is what makes
    # raising a timeout constant without raising the matching hooks/hooks.json
    # budget (or vice versa) turn the suite red.
    _v5_budget = {}
    for _v5_ev, _v5_groups in hj["hooks"].items():
        for _v5_g in _v5_groups:
            for _v5_h in _v5_g["hooks"]:
                _v5_m = _re2.search(r"hooks/(\w+)\.py", _v5_h["command"])
                if _v5_m and not _v5_h.get("async"):
                    _v5_budget[_v5_m.group(1)] = _v5_h["timeout"]
    assert {"stop", "pre_compact", "session_start"} <= set(_v5_budget), _v5_budget
    # hook -> (deadline constant, headroom the deadline must leave for the
    #          hook's non-LLM work, how the NOMINAL per-leg sum is bounded)
    #
    # "nominal_fits": 2*_API_TIMEOUT + _FALLBACK_TIMEOUT (2 Anthropic credential
    #   candidates + the opt-in Ollama leg) also fits inside the host budget.
    # "deadline_binds": it deliberately does NOT, and the deadline is the only
    #   bound — SessionStart's budget is 15s while a healthy Haiku extraction
    #   wants ~10s, so shrinking the per-leg timeout to satisfy the arithmetic
    #   would leave a value that cannot complete (see llm/ccl_backend.call_llm
    #   docstring). Safe here ONLY because the injection — this hook's entire
    #   product — is printed and flushed before retroactive_save runs, so a kill
    #   costs the extraction and nothing else.
    _v5_llm_spec = {
        "stop":          ("_LLM_DEADLINE_S",   4.0, "nominal_fits"),
        "pre_compact":   ("_LLM_DEADLINE_S",  20.0, "nominal_fits"),
        "session_start": ("_RETRO_DEADLINE_S", 2.0, "deadline_binds"),
    }
    _v5_llm_report = []
    for _v5_name, (_v5_dl_c, _v5_head, _v5_rule) in _v5_llm_spec.items():
        _v5_src = (_REPO / "cc_memory" / "hooks" / f"{_v5_name}.py").read_text(
            encoding="utf-8")
        assert _re2.search(r"^_HOOK_T0 = time\.monotonic\(\)", _v5_src, _re2.M), \
            f"{_v5_name}.py must capture _HOOK_T0 at import time"
        assert _v5_src.index("_HOOK_T0 = time.monotonic()") \
            < _v5_src.index("sys.path.insert"), \
            f"{_v5_name}.py: _HOOK_T0 must be taken BEFORE the package imports"
        assert f"deadline=_HOOK_T0 + {_v5_dl_c}" in _v5_src, \
            f"{_v5_name}.py must pass an absolute deadline to call_llm"
        _v5_c = {}
        for _v5_const in ("_API_TIMEOUT", "_FALLBACK_TIMEOUT", _v5_dl_c):
            _v5_mm = _re2.search(rf"^{_v5_const} = ([\d.]+)$", _v5_src, _re2.M)
            assert _v5_mm, f"{_v5_name}.py missing {_v5_const}"
            _v5_c[_v5_const] = float(_v5_mm.group(1))
        _v5_api = _v5_c["_API_TIMEOUT"]
        _v5_dl = _v5_c[_v5_dl_c]
        _v5_nominal = 2 * _v5_api + _v5_c["_FALLBACK_TIMEOUT"]
        _v5_host = float(_v5_budget[_v5_name])
        assert _v5_api < _v5_dl, \
            f"{_v5_name}.py: one leg ({_v5_api}s) cannot finish inside " \
            f"{_v5_dl_c}={_v5_dl}s — the common case is dead on arrival"
        assert _v5_dl + _v5_head <= _v5_host, \
            f"{_v5_name}.py: {_v5_dl_c}={_v5_dl}s leaves under {_v5_head}s of " \
            f"the {_v5_host}s hooks.json budget for this hook's non-LLM work"
        if _v5_rule == "nominal_fits":
            assert _v5_nominal <= _v5_host, \
                f"{_v5_name}.py: 2*{_v5_api} + {_v5_c['_FALLBACK_TIMEOUT']} = " \
                f"{_v5_nominal}s exceeds the {_v5_host}s hooks.json budget"
        else:
            assert _v5_dl < _v5_nominal, \
                f"{_v5_name}.py is marked deadline-bound but its nominal sum " \
                f"({_v5_nominal}s) already fits under {_v5_dl_c}={_v5_dl}s — " \
                f"retag it 'nominal_fits' so the tighter check applies"
        _v5_llm_report.append(f"{_v5_name} {_v5_dl}/{_v5_nominal} in {_v5_host:.0f}s")
    print("[OK] v2.5.0 hook LLM budgets fit hooks.json (deadline/nominal in "
          "budget): " + ", ".join(_v5_llm_report))

    # === v2.5.0 (3b): call_llm honours an absolute deadline ==================
    # A deadline already in the past must skip EVERY leg — no socket is opened,
    # so the caller cannot overrun its host timeout on a stalled DNS/TLS phase.
    import llm.ccl_backend as _v5_ccl
    import core.auth as _v5_auth
    assert "deadline" in _inspect.signature(_v5_ccl.call_llm).parameters, \
        "call_llm must accept an absolute `deadline`"

    def _v5_no_network(*_a, **_kw):
        raise AssertionError("a network leg started AFTER the deadline passed")

    # fixture credentials: obviously-fake placeholders. Stubbing
    # get_api_candidates keeps this hermetic — no ANTHROPIC_API_KEY and no
    # ~/.claude/.credentials.json is ever read, so the real machine's
    # credentials are neither required nor touched.
    _v5_fake_keys = ("sk-ant-api03-EXAMPLE-caller-placeholder",
                     "sk-ant-api03-EXAMPLE-second-placeholder")
    _v5_saved_llm = (_v5_auth.get_api_candidates,
                     _v5_ccl._call_haiku, _v5_ccl._call_ollama)
    try:
        _v5_auth.get_api_candidates = lambda: [(_v5_fake_keys[1], "env",
                                                "api_key")]
        _v5_ccl._call_haiku = _v5_no_network
        _v5_ccl._call_ollama = _v5_no_network
        _v5_llm_err = None
        try:
            _v5_ccl.call_llm("sys", "user", api_key=_v5_fake_keys[0],
                             timeout=30, fallback_timeout=30,
                             deadline=_time.monotonic() - 5.0)
        except RuntimeError as _v5_e:
            _v5_llm_err = str(_v5_e)
        assert _v5_llm_err is not None, \
            "call_llm ran a leg past its deadline instead of raising"
        assert _v5_llm_err.count("skipped (deadline reached)") == 2, \
            f"both Anthropic candidates must be skipped: {_v5_llm_err}"
    finally:
        (_v5_auth.get_api_candidates,
         _v5_ccl._call_haiku, _v5_ccl._call_ollama) = _v5_saved_llm
    print("[OK] v2.5.0 call_llm: `deadline` kwarg accepted, a past deadline "
          "skips every leg without opening a socket")

    # === v2.5.0 (4): version single-source ===================================
    # core/version.py is THE runtime version; four manifests cannot import it
    # and must be bumped in lockstep. This is the check that catches a partial
    # bump (and it is why core/version.py exists at all — see its docstring).
    from core.version import __version__ as _v5_ver
    _v5_init_src = (_REPO / "cc_memory" / "__init__.py").read_text(encoding="utf-8")
    assert "from .core.version import __version__" in _v5_init_src, \
        "cc_memory/__init__.py must RE-EXPORT core/version.py, not restate it"
    assert not _re2.search(r'^__version__\s*=\s*["\']', _v5_init_src, _re2.M), \
        "cc_memory/__init__.py hardcodes a second version literal"
    _v5_pyproj_bytes = (_REPO / "pyproject.toml").read_bytes()
    assert not _v5_pyproj_bytes.startswith(b"\xef\xbb\xbf"), \
        "pyproject.toml has a UTF-8 BOM again — tomllib cannot parse it, so no " \
        "PEP 517 frontend can build or install the package"
    try:
        import tomllib as _v5_toml
        _v5_pyproj_ver = _v5_toml.loads(
            _v5_pyproj_bytes.decode("utf-8"))["project"]["version"]
    except ImportError:
        # why: tomllib is 3.11+; this project supports 3.8+, so on an older
        # interpreter read the literal directly rather than skip the check
        _v5_pm = _re2.search(r'^version\s*=\s*"([^"]+)"',
                             _v5_pyproj_bytes.decode("utf-8"), _re2.M)
        assert _v5_pm, "pyproject.toml [project] version not found"
        _v5_pyproj_ver = _v5_pm.group(1)
    _v5_manifests = {
        "cc_memory/config.json": _json2.loads(
            (_REPO / "cc_memory" / "config.json").read_text(
                encoding="utf-8"))["version"],
        ".claude-plugin/plugin.json": _json2.loads(
            (_REPO / ".claude-plugin" / "plugin.json").read_text(
                encoding="utf-8"))["version"],
        ".claude-plugin/marketplace.json": _json2.loads(
            (_REPO / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"))["plugins"][0]["version"],
        "pyproject.toml": _v5_pyproj_ver,
    }
    _v5_drifted = {k: v for k, v in _v5_manifests.items() if v != _v5_ver}
    assert not _v5_drifted, \
        f"version drift: core/version.py says {_v5_ver}, but {_v5_drifted}"
    print(f"[OK] v2.5.0 version single-source: core/version.py {_v5_ver} == all "
          f"{len(_v5_manifests)} manifests")

    # === v2.5.0 (5): FLAT standalone install inspects clean ==================
    # ui/installer.py copies subpackages to TARGET_DIR/<subdir>/ — a FLAT tree
    # with NO cc_memory/ segment — while _inspect_layout resolved `root / rel`
    # with rel carrying a literal `cc_memory/` prefix. Every standalone install
    # therefore reported "22 of 22 files missing" and /cc-mem status skipped the
    # API-key check for want of a functional layout. The old fixture built a
    # NESTED tree and only ever passed hooks_via="plugin-manifest", so neither
    # half of the real standalone shape was ever exercised.
    import shutil as _v5_sh
    _v5_flat = Path(tempfile.mkdtemp(prefix="cc-memory-flatplugin-"))
    for _v5_sub, _v5_files in _inst.SUBPACKAGE_FILES.items():
        _v5_d = _v5_flat / _v5_sub if _v5_sub else _v5_flat
        _v5_d.mkdir(parents=True, exist_ok=True)
        for _v5_f in _v5_files:
            (_v5_d / _v5_f).write_text("# stub\n", encoding="utf-8")
    assert not (_v5_flat / "cc_memory").exists(), "fixture must be FLAT"
    _v5_settings = {"hooks": _inst._make_hooks_config(_v5_flat)}
    _v5_flat_verdict = _inspect_layout("legacy-install", _v5_flat,
                                       hooks_via="user-settings", enabled=True,
                                       settings_dict=_v5_settings)
    assert _v5_flat_verdict["plugin_files_ok"] is True, \
        f"a healthy FLAT standalone install still inspects broken, missing: " \
        f"{_v5_flat_verdict['missing_files']}"
    assert _v5_flat_verdict["pkg_dir"] == _v5_flat, \
        "pkg_dir must resolve to the root itself for a flat install"
    assert set(_v5_flat_verdict["hooks_registered"]) == {
        "PreCompact", "SessionStart", "Stop", "PostToolUse", "UserPromptSubmit"
    }, f"settings.json[hooks] not read: {_v5_flat_verdict['hooks_registered']}"
    assert _print_layout_report(_v5_flat_verdict) is True, \
        "flat + user-settings install must report FUNCTIONAL"
    # hooks/hooks.json is a REPO-level file the standalone installer never
    # copies; requiring it would re-break every settings.json install
    assert "hooks/hooks.json" not in _v5_flat_verdict["missing_files"]
    _v5_sh.rmtree(_v5_flat, ignore_errors=True)
    print("[OK] v2.5.0 layout inspector: FLAT standalone install + "
          "user-settings hooks reports healthy (5/5)")

    # === v2.5.0 (5b): the installer ships every runtime module + surface =====
    # core/version.py was ADDED without being registered in either manifest;
    # a standalone install would have shipped a package that cannot import.
    _v5_shipped = {f"{_s}/{_f}" if _s else _f
                   for _s, _fs in _inst.SUBPACKAGE_FILES.items() for _f in _fs}
    _v5_on_disk = {p.relative_to(_REPO / "cc_memory").as_posix()
                   for p in (_REPO / "cc_memory").rglob("*.py")
                   if "__pycache__" not in p.parts}
    assert _v5_on_disk <= _v5_shipped, \
        f"runtime modules the standalone installer never copies: " \
        f"{sorted(_v5_on_disk - _v5_shipped)}"
    for _v5_sub, _v5_files in _inst.SUBPACKAGE_FILES.items():
        for _v5_f in _v5_files:
            _v5_p = ((_REPO / "cc_memory" / _v5_sub / _v5_f) if _v5_sub
                     else (_REPO / "cc_memory" / _v5_f))
            assert _v5_p.is_file(), f"SUBPACKAGE_FILES lists a missing file: {_v5_p}"
    assert set(_inst.SURFACE_FILES) == {
        "commands/cc-mem.md", "agents/plan-refiner.md", "agents/plan-guardian.md",
        "skills/ccm-load/SKILL.md", "skills/save-memories/SKILL.md",
    }, f"surface copy set changed: {_inst.SURFACE_FILES}"
    for _v5_rel in _inst.SURFACE_FILES:
        assert (_REPO / _v5_rel).is_file(), f"shipped surface missing: {_v5_rel}"

    # build_exe.py restates both manifests; they must be byte-identical, not
    # merely equivalent — a reviewer diffing the two files must see nothing.
    def _v5_manifest_block(text, head, tail):
        _i = text.index(head)
        return text[_i:text.index(tail, _i) + len(tail)]

    _v5_inst_src = (_REPO / "cc_memory" / "ui" / "installer.py").read_text(
        encoding="utf-8")
    _v5_bx_src = (_REPO / "build_exe.py").read_text(encoding="utf-8")
    for _v5_head, _v5_tail in (("SUBPACKAGE_FILES = {", "\n}\n"),
                               ("SURFACE_FILES = [", "\n]\n")):
        assert _v5_manifest_block(_v5_inst_src, _v5_head, _v5_tail) == \
            _v5_manifest_block(_v5_bx_src, _v5_head, _v5_tail), \
            f"ui/installer.py and build_exe.py {_v5_head.split()[0]} have drifted"
    sys.path.append(str(_REPO))
    import build_exe as _v5_bx
    assert _inst.SUBPACKAGE_FILES == _v5_bx.SUBPACKAGE_FILES
    assert _inst.SURFACE_FILES == _v5_bx.SURFACE_FILES
    print(f"[OK] v2.5.0 installer manifest: ships all {len(_v5_on_disk)} runtime "
          f"modules + 5 surfaces; build_exe.py copy is byte-identical")

    # === v2.5.0 (6): installer hook timeouts in lockstep with hooks.json =====
    # hooks/hooks.json is the source of truth; HOOK_SCRIPTS / ASYNC_HOOK are the
    # fallback for a frozen/flat install where that file is absent. Both must
    # carry the SAME numbers, or a standalone install silently runs with
    # different budgets than the ones asserted in block (3) above.
    _v5_cfg = _inst._make_hooks_config(Path("X:/cc-mem-timeout-probe"))
    for _v5_ev, _v5_groups in hj["hooks"].items():
        for _v5_entry in _v5_groups[0]["hooks"]:
            _v5_got = [c["timeout"] for c in _v5_cfg[_v5_ev][0]["hooks"]
                       if bool(c.get("async")) == bool(_v5_entry.get("async"))]
            assert _v5_got and _v5_got[0] == _v5_entry["timeout"], \
                f"{_v5_ev} ({'async' if _v5_entry.get('async') else 'sync'}): " \
                f"hooks.json says {_v5_entry['timeout']}, installer emits {_v5_got}"
    for _v5_ev, (_v5_script, _v5_t) in _inst.HOOK_SCRIPTS.items():
        _v5_sync = [e["timeout"] for e in hj["hooks"][_v5_ev][0]["hooks"]
                    if not e.get("async")]
        assert _v5_t == _v5_sync[0], \
            f"{_v5_ev}: HOOK_SCRIPTS fallback {_v5_t} != hooks.json {_v5_sync[0]}"
    assert _inst.ASYNC_HOOK == ("PreCompact", "hooks/consolidate_async.py", 300)
    print("[OK] v2.5.0 installer hook timeouts == hooks.json (live read AND "
          "frozen-install fallback table)")

    # === v2.5.0 (7): an unrefined raw plan wins over the stale structured one =
    # plan_active is a single slot holding BOTH forms. capture_exit_plan_mode
    # (the primary auto-capture path) and `/cc-mem plan-set --raw` stored a
    # brand-new raw plan with needs_refine=1 while every renderer kept printing
    # the PREVIOUS refined plan's goal and steps — the newest plan was invisible.
    _v5_pp = Path(tempfile.mkdtemp(prefix="cc-mem-planprec-"))
    _v5_mem_pp = _v5_pp / "memory"; _v5_mem_pp.mkdir(parents=True, exist_ok=True)
    _v5_db_pp = MemoryDB(_v5_mem_pp / "memory.db")
    _v5_pid_pp = _v5_db_pp.upsert_project(str(_v5_pp))
    _v5_structured = {
        "goal": "SUPERSEDED STRUCTURED GOAL",
        "success_criteria": ["the old criterion"],
        "steps": [{"id": 1, "title": "old step one", "status": "pending",
                   "notes": ""}],
        "context": "",
    }
    plan_mod.apply_refined_plan(_v5_db_pp, _v5_pid_pp, _v5_structured,
                                memory_dir=_v5_mem_pp)
    _v5_md0 = (_v5_mem_pp / "PLAN.md").read_text(encoding="utf-8")
    assert "SUPERSEDED STRUCTURED GOAL" in _v5_md0 \
        and "Pending refinement" not in _v5_md0, \
        "a freshly refined plan must render as the structured form"
    assert plan_mod.capture_exit_plan_mode(
        _v5_db_pp, _v5_pid_pp, "BRAND NEW RAW PLAN: rewrite the exporter",
        memory_dir=_v5_mem_pp) is True
    assert _v5_db_pp.get_plan_active(_v5_pid_pp)["needs_refine"] == 1
    _v5_md1 = (_v5_mem_pp / "PLAN.md").read_text(encoding="utf-8")
    assert "BRAND NEW RAW PLAN" in _v5_md1, \
        "PLAN.md still renders the stale structured plan after a raw capture"
    assert "STALE, superseded by the raw text above" in _v5_md1, \
        "the superseded structured plan must be labelled STALE, not just dropped"
    assert _v5_md1.index("BRAND NEW RAW PLAN") \
        < _v5_md1.index("SUPERSEDED STRUCTURED GOAL"), \
        "the current raw plan must come BEFORE the superseded structured one"
    # the predicate itself, and the unchanged pre-v2.5 behaviour when a caller
    # passes no meta at all
    assert plan_mod.raw_pending_refinement(
        {"raw": "r", "structured": _v5_structured, "needs_refine": 1,
         "last_refined_at": "2026-01-01T00:00:00"}) is True
    assert plan_mod.raw_pending_refinement(
        {"raw": "r", "structured": _v5_structured, "needs_refine": 0,
         "last_refined_at": "2026-01-01T00:00:00"}) is False
    assert "Pending refinement" in plan_mod.render_plan_md(
        _v5_structured, active_step_id=1, meta={"raw": "r", "needs_refine": 1})
    assert "Pending refinement" not in plan_mod.render_plan_md(
        _v5_structured, active_step_id=1), "meta-less render must be unchanged"
    print("[OK] v2.5.0 plan precedence: an unrefined raw plan is what PLAN.md "
          "renders; the structured one is labelled STALE")

    # === v2.5.0 (8): PostToolUse classifies privacy on the RAW response ======
    # Both _truncate_* helpers are lossy — a Read body collapses to
    # "(file content)" — so classifying on their OUTPUT made <private>
    # unobservable exactly where it mattered: a Read of a file the user marked
    # private stored is_private=0, which is what keeps a row OUT of the Stop
    # observer and the PreCompact extraction prompt. A false 0 ships that path
    # to the Anthropic API and into progress.files_touched.
    import io as _io
    _v5_ptu_spec = _ilu.spec_from_file_location(
        "_ptu_sm", _REPO / "cc_memory" / "hooks" / "post_tool_use.py")
    _v5_ptu = _ilu.module_from_spec(_v5_ptu_spec)
    _v5_ptu_spec.loader.exec_module(_v5_ptu)
    _v5_body = "KEY=<private>hunter2</private> trailing"
    assert _v5_ptu._truncate_output("Read", _v5_body) == "(file content)"
    assert _v5_has_priv(_v5_ptu._truncate_output("Read", _v5_body)) is False, \
        "fixture invalid: the truncated form must have LOST the marker"
    _v5_op = Path(tempfile.mkdtemp(prefix="cc-mem-ptu-"))
    (_v5_op / "memory").mkdir(parents=True, exist_ok=True)
    _v5_db_o = MemoryDB(_v5_op / "memory" / "memory.db")
    _v5_pid_o = _v5_db_o.upsert_project(str(_v5_op))

    class _V5FakeStdin:
        """Minimal stand-in: the hook only ever touches sys.stdin.buffer."""

        def __init__(self, payload):
            self.buffer = _io.BytesIO(payload)

    def _v5_run_ptu(payload):
        _saved_stdin = sys.stdin
        sys.stdin = _V5FakeStdin(_json3.dumps(payload).encode("utf-8"))
        try:
            _v5_ptu.main()
        except SystemExit:
            # why: the hook contract ends EVERY run with sys.exit(0); an
            # in-process invocation must absorb that, not tear down the suite
            pass
        finally:
            sys.stdin = _saved_stdin

    _v5_run_ptu({"cwd": str(_v5_op), "tool_name": "Read", "session_id": "s1",
                 "tool_input": {"file_path": "notes.md"},
                 "tool_response": "an entirely harmless body"})
    _v5_run_ptu({"cwd": str(_v5_op), "tool_name": "Read", "session_id": "s1",
                 "tool_input": {"file_path": "secrets.env"},
                 "tool_response": _v5_body})
    with _v5_db_o._connect() as _v5_conn:
        _v5_obs = [dict(r) for r in _v5_conn.execute(
            "SELECT tool_input, tool_output, is_private FROM observations "
            "ORDER BY id")]
    assert len(_v5_obs) == 2, f"expected 2 observation rows, got {_v5_obs}"
    assert _v5_obs[0]["is_private"] == 0 and _v5_obs[1]["is_private"] == 1, \
        f"is_private computed AFTER truncation (marker already gone): {_v5_obs}"
    assert all(o["tool_output"] == "(file content)" for o in _v5_obs), \
        "fixture invalid: the stored body must be the truncated placeholder"
    assert [o["tool_input"] for o in _v5_db_o.get_recent_observations(_v5_pid_o)] \
        == ["notes.md"], "the private row reached the observer/extraction feed"
    print("[OK] v2.5.0 PostToolUse: is_private classified on the RAW response, "
          "so a private Read stays out of the extraction feed")

    # === v2.5.0 (9): LIKE metacharacters escaped + LIMIT clamped both ends ===
    # Unescaped, a search for "%" matched every row and "_" matched every row
    # with at least one character — a full table dump from a one-character
    # query. And SQLite reads a NEGATIVE limit as "no limit", so the FLOOR is as
    # load-bearing as the ceiling (search_fts(pid, q, limit=10**6) used to fetch
    # every active row, driven straight from an MCP tool argument).
    assert MemoryDB._like_escape("100%") == "100\\%"
    assert MemoryDB._like_escape("a_b") == "a\\_b"
    assert MemoryDB._like_escape("a\\b") == "a\\\\b", \
        "the backslash must be doubled FIRST or the % / _ escapes get re-escaped"
    assert MemoryDB._like_escape("a\\%b") == "a\\\\\\%b"
    _v5_sp = Path(tempfile.mkdtemp(prefix="cc-mem-search-"))
    (_v5_sp / "memory").mkdir(parents=True, exist_ok=True)
    _v5_db_s = MemoryDB(_v5_sp / "memory" / "memory.db")
    _v5_pid_s = _v5_db_s.upsert_project(str(_v5_sp))
    for _v5_i in range(6):
        _v5_db_s.insert_memory(_v5_pid_s, None, "note",
                               f"widget number {_v5_i} does a thing",
                               importance=3)
    assert len(_v5_db_s.search_fts(_v5_pid_s, "widget")) == 6, "fixture check"
    # force the LIKE branch (an FTS5-less sqlite build takes it unconditionally)
    _v5_db_s._fts5_available = False
    assert _v5_db_s.search_fts(_v5_pid_s, "%") == [], \
        "a bare '%' still dumps the table — LIKE metacharacters unescaped"
    assert _v5_db_s.search_fts(_v5_pid_s, "_") == [], \
        "a bare '_' still matches every non-empty row"
    assert len(_v5_db_s.search_fts(_v5_pid_s, "widget")) == 6, \
        "escaping broke ordinary queries"
    for _v5_bad_limit in (-1, 0, -10 ** 6):
        assert len(_v5_db_s.search_fts(_v5_pid_s, "widget",
                                       limit=_v5_bad_limit)) == 1, \
            f"limit={_v5_bad_limit} was not clamped up to 1 (SQLite reads a " \
            f"negative LIMIT as UNLIMITED)"
    assert len(_v5_db_s.search_fts(_v5_pid_s, "widget", limit="abc")) == 6, \
        "a non-numeric limit must fall back to the documented default, not raise"
    assert MemoryDB._MAX_SEARCH_LIMIT == 1000
    _v5_db_s._MAX_SEARCH_LIMIT = 2  # instance-level, so 6 rows are enough
    assert len(_v5_db_s.search_fts(_v5_pid_s, "widget", limit=10 ** 6)) == 2, \
        "a caller-supplied limit is a hint, not a licence to materialise the table"
    del _v5_db_s._MAX_SEARCH_LIMIT
    print("[OK] v2.5.0 db search: LIKE metacharacters escaped, LIMIT clamped at "
          "both ends (floor 1, ceiling _MAX_SEARCH_LIMIT)")

    # === v2.5.0 (10): encoding_setup covers stdin and line-buffers stdout ====
    # stdin: the MCP server reads JSON-RPC frames from it; under the locale
    # codec a non-ASCII request raised UnicodeDecodeError INSIDE the line
    # iterator, outside the per-request try, killing the process silently.
    # line_buffering: without it reconfigure leaves stdout BLOCK-buffered on a
    # pipe (what every hook writes to), so a SessionStart killed at its 15s
    # timeout lost 100% of a 5069 B injection although every print() had run.
    class _V5RecStream:
        def __init__(self, boom=False):
            self.calls = []
            self._boom = boom

        def reconfigure(self, **kw):
            self.calls.append(kw)
            if self._boom:
                raise ValueError("underlying buffer already detached")

    _v5_rec = {n: _V5RecStream() for n in ("stdout", "stderr", "stdin")}
    _v5_saved_std = {n: getattr(sys, n) for n in _v5_rec}
    try:
        for _v5_n, _v5_s in _v5_rec.items():
            setattr(sys, _v5_n, _v5_s)
        enable_utf8_io()
    finally:
        for _v5_n, _v5_s in _v5_saved_std.items():
            setattr(sys, _v5_n, _v5_s)
    for _v5_n in ("stdout", "stderr"):
        assert _v5_rec[_v5_n].calls == [{"encoding": "utf-8",
                                         "errors": "replace",
                                         "line_buffering": True}], \
            f"sys.{_v5_n} must be UTF-8 AND line-buffered: {_v5_rec[_v5_n].calls}"
    assert _v5_rec["stdin"].calls == [{"encoding": "utf-8", "errors": "replace"}], \
        f"sys.stdin must be reconfigured, without line_buffering (a read " \
        f"stream rejects it): {_v5_rec['stdin'].calls}"
    # a stream that refuses to reconfigure must never take a hook down
    _v5_boom = {n: _V5RecStream(boom=True) for n in ("stdout", "stderr", "stdin")}
    _v5_saved_std = {n: getattr(sys, n) for n in _v5_boom}
    try:
        for _v5_n, _v5_s in _v5_boom.items():
            setattr(sys, _v5_n, _v5_s)
        enable_utf8_io()
    finally:
        for _v5_n, _v5_s in _v5_saved_std.items():
            setattr(sys, _v5_n, _v5_s)
    # ...and EVERY suite in tests/ must actually CALL it before its first line
    # of output, or section headers ship as locale-codec bytes (gbk on this
    # host) and read as mojibake in every UTF-8 terminal, log and CI capture.
    # SELF-APPLYING: this file is on the list, and so is test_surfaces.py,
    # which was UTF-8 only BY ACCIDENT until v2.5.1 — it never called
    # enable_utf8_io() and inherited the reconfigure from `from mcp import
    # server` at cc_memory/mcp/server.py, so an import reorder would have
    # silently turned its § headers into mojibake.
    # Compare CODE lines only. A naive src.index(...) probe also matches the
    # bare word inside the comment that explains this very ordering, which
    # sits above the call and made the assertion fail on a correct file.
    for _v5_suite in ("test_plan_carryover.py", "smoke_test.py",
                      "test_surfaces.py"):
        _v5_lines = (_REPO / "tests" / _v5_suite).read_text(
            encoding="utf-8").splitlines()
        _v5_call_at = next(
            (i for i, ln in enumerate(_v5_lines)
             if ln.split("#", 1)[0].strip() == "enable_utf8_io()"), None)
        _v5_print_at = next((i for i, ln in enumerate(_v5_lines)
                             if "print(" in ln.split("#", 1)[0]), None)
        assert _v5_call_at is not None, \
            f"tests/{_v5_suite} must CALL enable_utf8_io() — its section " \
            f"headers contain non-ASCII and ship as locale bytes without it"
        assert _v5_print_at is None or _v5_call_at < _v5_print_at, \
            (f"tests/{_v5_suite} calls enable_utf8_io() at line "
             f"{_v5_call_at + 1}, after its first output line at line "
             f"{_v5_print_at + 1}")
    print("[OK] v2.5.0 encoding_setup: stdin covered, stdout/stderr "
          "line-buffered, reconfigure failure survivable, and all 3 test "
          "suites call it before their first output line")

    print("\nProduced files:")
    for f in sorted(mem_dir.rglob("*")):
        if f.is_file():
            rel = f.relative_to(mem_dir).as_posix()
            print(f"  memory/{rel}  ({f.stat().st_size} bytes)")
    print(f"\nTest project was: {tmp}")

    # Teardown is a GATE, not a courtesy: every artifact of this run lives
    # under the sandbox, and a handle we cannot release would otherwise leak a
    # memory.db into the real %TEMP% on every single run.
    _cleanup_sandbox()
    print(f"Sandbox removed: {_SANDBOX}")

    print("\n===== ALL SMOKE TESTS PASSED =====")


if __name__ == "__main__":
    main()
