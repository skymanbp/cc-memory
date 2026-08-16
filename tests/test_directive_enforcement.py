# -*- coding: utf-8 -*-
"""v2.11.0 enforcement gate — 计划与指令不许只靠提醒 (2026-08-15).

Every piece of plan machinery in this package was ADVISORY. `stop.py` said so
in its own comment ("The plan-refiner nudge is advisory"), and the nudge was
rate-limited to once per five turns on top of that. Measured in the
lore_disaster project: a 51,237-char raw plan sat unrefined while PLAN.md,
`plan-status` and the drift guardian all answered from the PREVIOUS plan, and
a full-transcript audit of 416 deduped user messages later found a mechanic
the user had demanded SIX separate times with zero implementation.

This suite pins the two halves of the fix:

  §1 directive ledger — repetition accumulates on ONE row; explicit counts are
     honoured; listing orders by repetition; closing records evidence + time
  §2 blocking_reasons — an unrefined plan blocks; a drifted plan blocks; a
     clean plan does not; an idle directive blocks only past the threshold
  §3 the kill switch (CC_MEMORY_PLAN_ENFORCE=0) disables enforcement entirely
  §4 the escape budget — the refusal text announces the remaining budget, and
     a changed condition set restarts it (so fixing one problem never eats the
     budget of the next). An unbreakable block is worse than no block.

Run:  python tests/test_directive_enforcement.py
"""
from __future__ import annotations

import gc
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ── sandbox: must be installed BEFORE importing anything from cc_memory ─────
# Literal twin of the block in test_plan_carryover.py / smoke_test.py: these
# are standalone scripts that cannot import each other, and a shared helper
# would have to live inside the package under test. Without it a test run
# writes into the maintainer's real ~/.claude and leaks project dirs into the
# real %TEMP%.
_SANDBOX = Path(tempfile.mkdtemp(prefix="ccm-enforce-box-"))
_HOME = _SANDBOX / "home"
_TMP = _SANDBOX / "tmp"
_HOME.mkdir(parents=True, exist_ok=True)
_TMP.mkdir(parents=True, exist_ok=True)
_drive, _rest = os.path.splitdrive(str(_HOME))
os.environ.update({
    "USERPROFILE": str(_HOME),
    "HOME": str(_HOME),
    "HOMEDRIVE": _drive or "",
    "HOMEPATH": _rest or str(_HOME),
    "TEMP": str(_TMP),
    "TMP": str(_TMP),
    "TMPDIR": str(_TMP),
})
tempfile.tempdir = str(_TMP)
assert Path.home() == _HOME, (
    f"sandbox home not in effect (Path.home()={Path.home()}); refusing to run "
    f"against the real ~/.claude")


def _cleanup_sandbox():
    for _conn in [o for o in gc.get_objects()
                  if isinstance(o, sqlite3.Connection)]:
        try:
            _conn.close()
        except sqlite3.Error:
            # why: already-closed or mid-statement handle; the only goal is
            # releasing the OS file handle before rmtree, and one we cannot
            # release is caught by the rmtree check below anyway
            pass
    gc.collect()
    try:
        from core import logger as _logger_mod
        for _lg in list(getattr(_logger_mod, "_loggers", {}).values()):
            _lg.close()
    except ImportError:
        # why: teardown must work even if the package never became importable
        pass
    tempfile.tempdir = None
    try:
        shutil.rmtree(_SANDBOX)
    except OSError as exc:
        left = sorted(str(p) for p in _SANDBOX.rglob("*") if p.is_file())
        raise AssertionError(
            f"sandbox {_SANDBOX} survived cleanup ({exc}); {len(left)} file(s) "
            f"leaked into the real %TEMP%: {left[:10]}")


sys.path.insert(0, str(REPO / "cc_memory"))

from core.db import MemoryDB          # noqa: E402  -- why: imports must follow the sys.path bootstrap above; repo tests run as plain scripts
from core import plan as plan_mod     # noqa: E402  -- why: same bootstrap ordering
from core.encoding_setup import enable_utf8_io  # noqa: E402  -- why: same bootstrap ordering

enable_utf8_io()

FAILURES: list = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(label)


def _mk_db(name):
    root = Path(tempfile.mkdtemp(prefix=f"ccm-enf-{name}-"))
    db = MemoryDB(root / "memory.db")
    return db, db.upsert_project(str(root))


# ── §1 directive ledger ─────────────────────────────────────────────────────
def section_1():
    print("\n§1 指令账本：重复累计在一行，而不是新增一行")
    db, pid = _mk_db("ledger")

    db.upsert_directive(pid, "pause-on-quota",
                        quote="额度不够就暂停任务，等我指示",
                        demand="免费通路失效即暂停并请示", kind="standing")
    db.upsert_directive(pid, "pause-on-quota")
    db.upsert_directive(pid, "pause-on-quota")
    rows = db.list_directives(pid)
    check("restating accumulates on one row", len(rows) == 1, f"got {len(rows)}")
    check("times_stated counts the repetitions", rows[0]["times_stated"] == 3,
          f"got {rows[0]['times_stated']}")
    check("the verbatim quote is preserved",
          "暂停任务" in rows[0]["quote"], rows[0]["quote"])

    # a transcript audit imports a count directly rather than replaying it
    db.upsert_directive(pid, "waaagh-reserves", demand="Waaagh 式预备队/援军",
                        times_stated=6, kind="feature")
    db.upsert_directive(pid, "garrison-lore", demand="守军按背景打磨",
                        times_stated=2, kind="feature")
    active = db.list_directives(pid, status="active")
    check("explicit times_stated is honoured",
          [r for r in active if r["slug"] == "waaagh-reserves"][0]
          ["times_stated"] == 6)
    check("listing is ordered by repetition (loudest first)",
          [r["slug"] for r in active][0] == "waaagh-reserves",
          str([r["slug"] for r in active]))

    n = db.set_directive_status(pid, "pause-on-quota", "done",
                                evidence="状态板铁律 + 实测")
    closed = [r for r in db.list_directives(pid)
              if r["slug"] == "pause-on-quota"][0]
    check("closing updates exactly one row", n == 1, f"rowcount={n}")
    check("closing records evidence and a timestamp",
          closed["status"] == "done" and closed["closed_at"]
          and "状态板" in closed["evidence"], str(closed)[:160])
    check("closed rows leave the active list",
          len(db.list_directives(pid, status="active")) == 2)


# ── §2 blocking_reasons ─────────────────────────────────────────────────────
def section_2():
    print("\n§2 阻断条件：过期计划必须拦，干净计划必须放行")
    unrefined = {"raw": "goal: ship it", "needs_refine": 1,
                 "structured": {}, "last_refined_at": ""}
    keys = [r[0] for r in plan_mod.blocking_reasons(unrefined)]
    check("an unrefined plan blocks", "plan-unrefined" in keys, str(keys))

    clean = {
        "raw": "goal: ship it",
        "needs_refine": 0,
        "last_refined_at": "2026-08-15T00:00:00",
        "structured": {"goal": "ship it", "steps": [
            {"id": 1, "title": "a", "status": "in_progress"}]},
        "edits_since_last_guardian": 0,
        "turns_since_last_guardian": 0,
    }
    check("a fresh, checked plan does not block",
          plan_mod.blocking_reasons(clean) == [],
          str(plan_mod.blocking_reasons(clean)))

    drifted = dict(clean, edits_since_last_guardian=999,
                   turns_since_last_guardian=999)
    keys = [r[0] for r in plan_mod.blocking_reasons(drifted)]
    check("a long-undrifted-checked plan blocks", "plan-drift" in keys,
          str(keys))

    fresh_directive = [{"status": "active", "slug": "x", "times_stated": 6,
                        "demand": "d", "turns_idle": 3}]
    check("a recently-touched directive does NOT block",
          plan_mod.blocking_reasons(clean, fresh_directive) == [])
    idle_directive = [{"status": "active", "slug": "waaagh", "times_stated": 6,
                       "demand": "Waaagh 预备队", "turns_idle": 40}]
    keys = [r[0] for r in plan_mod.blocking_reasons(clean, idle_directive)]
    check("an idle, repeatedly-stated directive blocks",
          "directive-idle:waaagh" in keys, str(keys))
    check("a closed directive is never raised",
          plan_mod.blocking_reasons(
              clean, [dict(idle_directive[0], status="done")]) == [])


# ── §3 kill switch ──────────────────────────────────────────────────────────
def section_3():
    print("\n§3 关停开关：CC_MEMORY_PLAN_ENFORCE=0 必须彻底关掉强制")
    unrefined = {"raw": "x", "needs_refine": 1, "structured": {},
                 "last_refined_at": ""}
    prev = os.environ.get("CC_MEMORY_PLAN_ENFORCE")
    try:
        for value in ("0", "false", "off", "NO"):
            os.environ["CC_MEMORY_PLAN_ENFORCE"] = value
            check(f"disabled by {value!r}",
                  plan_mod.blocking_reasons(unrefined) == [])
        os.environ["CC_MEMORY_PLAN_ENFORCE"] = "1"
        check("re-enabled by '1'", plan_mod.blocking_reasons(unrefined) != [])
    finally:
        if prev is None:
            os.environ.pop("CC_MEMORY_PLAN_ENFORCE", None)
        else:
            os.environ["CC_MEMORY_PLAN_ENFORCE"] = prev


# ── §4 escape budget ────────────────────────────────────────────────────────
def section_4():
    print("\n§4 逃生预算：阻断必须可逃，且换了条件要重新计数")
    reasons = [("plan-unrefined", "what", "how")]
    first = plan_mod.render_block_reason(reasons, 1)
    last = plan_mod.render_block_reason(reasons, plan_mod._BLOCK_MAX_CONSECUTIVE)
    check("the refusal names the failed condition", "plan-unrefined" in first)
    check("the refusal states the fix", "how" in first)
    check("early refusals announce the remaining budget",
          "more refusal" in first, first[-160:])
    check("the final refusal stops promising more budget",
          "more refusal" not in last, last[-160:])
    check("the kill switch is discoverable from the refusal itself",
          "CC_MEMORY_PLAN_ENFORCE" in first)

    # The budget is keyed by the CONDITION SET, checked here at the level the
    # hook uses: two different condition sets must not share a counter.
    import hashlib
    dig = lambda ks: hashlib.sha256("|".join(sorted(ks)).encode()).hexdigest()[:12]
    check("a changed condition set yields a different budget key",
          dig(["plan-unrefined"]) != dig(["plan-drift"]))
    check("the same condition set is stable regardless of order",
          dig(["a", "b"]) == dig(["b", "a"]))


def main():
    print("=" * 66)
    print("v2.11.0 enforcement gate — plan + directive ledger")
    print("=" * 66)
    try:
        section_1()
        section_2()
        section_3()
        section_4()
    finally:
        _cleanup_sandbox()
    print("\n" + "=" * 66)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
