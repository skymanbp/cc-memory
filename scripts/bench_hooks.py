#!/usr/bin/env python3
"""Measure what each hook costs, and what SessionStart puts in front of Claude.

    python scripts/bench_hooks.py            # prints a markdown table
    python scripts/bench_hooks.py --json     # the same numbers as JSON

Not a gate (tests/run_gates.py does not run it; `tools/` would make it one,
which is why it lives here beside build_exe.py). It exists so a claim like
"the injection got 30% smaller" or "Stop opens fewer connections" is a
number measured the same way before and after a change, on the same
fixture — the v2.16.0 CHANGELOG entry quotes its table.

Hermetic the way the test suites are: HOME / USERPROFILE / TEMP / TMP /
TMPDIR are redirected into one sandbox BEFORE cc_memory is imported (the
logger resolves `~/.claude` at import time), every credential variable is
dropped so no hook can POST fixture text to the Anthropic API, and the
sandbox is removed at the end — on the failure path too.

What is measured, per hook, on a project seeded with 600 memories, 6
directives and (for one Stop run) a raw plan awaiting refinement:

  wall     one real `python hook.py` process, stdin to exit, as Claude Code
           pays it — interpreter start + package import + the hook's work
  inproc   the hook's own main() inside that process (runpy, excludes the
           interpreter's own start-up)
  connects `sqlite3.connect` calls the hook made, and their open+close cost
  stdout   bytes the hook wrote; for SessionStart also an analysis of the
           injection (see `_analyse_injection`)

The driver (`--drive`) runs in a FRESH process per hook so the import cost
is paid exactly as in production; the counting patch on `sqlite3.connect`
is the one from the v2.16.0 plan's appendix.
"""
import io
import json
import os
import re
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "cc_memory"
HOOKS = PKG / "hooks"

N_MEMORIES = 600
N_STOP_RUNS = 3          # steady state is the LAST of these
_CRED_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
              "CLAUDE_CODE_OAUTH_TOKEN")


# ── driver: one hook, in THIS process, counting sqlite opens ────────────────

def _drive(hook_path, payload_path, show=False):
    payload = Path(payload_path).read_text(encoding="utf-8")
    stats = {"n": 0, "t_open": 0.0, "t_close": 0.0}

    class TimedConn(sqlite3.Connection):
        def close(self):
            t = time.perf_counter()
            r = super().close()
            stats["t_close"] += time.perf_counter() - t
            return r

    orig = sqlite3.connect

    def counting(*a, **k):
        k.setdefault("factory", TimedConn)
        t = time.perf_counter()
        c = orig(*a, **k)
        stats["t_open"] += time.perf_counter() - t
        stats["n"] += 1
        return c

    sqlite3.connect = counting
    sys.stdin = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")),
                                 encoding="utf-8")
    real_out = sys.stdout
    buf = io.BytesIO()
    sys.stdout = io.TextIOWrapper(buf, encoding="utf-8", write_through=True)
    t = time.perf_counter()
    try:
        runpy.run_path(str(hook_path), run_name="__main__")
    except SystemExit:
        pass  # why: every hook ends with sys.exit(0) by contract; that exit IS its normal return
    dt = time.perf_counter() - t
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass  # why: a hook that closed sys.stdout leaves nothing to flush; `buf` already holds the bytes
    out = buf.getvalue()
    sys.stdout = real_out
    result = {"connects": stats["n"],
              "open_close_ms": round(1000 * (stats["t_open"] + stats["t_close"]), 1),
              "inproc_ms": round(1000 * dt, 1),
              "stdout_bytes": len(out)}
    if show:
        result["stdout"] = out.decode("utf-8", "replace")
    print("@@RESULT " + json.dumps(result, ensure_ascii=False))


# ── orchestrator ────────────────────────────────────────────────────────────

def _sandbox():
    root = Path(tempfile.mkdtemp(prefix="cc-memory-bench-"))
    home = root / "home"
    tmp = root / "tmp"
    home.mkdir()
    tmp.mkdir()
    drive, rest = os.path.splitdrive(str(home))
    os.environ.update({
        "USERPROFILE": str(home), "HOME": str(home),
        "HOMEDRIVE": drive or "", "HOMEPATH": rest or str(home),
        "TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp),
        "PYTHONIOENCODING": "utf-8",
    })
    for var in _CRED_VARS:
        os.environ.pop(var, None)
    tempfile.tempdir = str(tmp)
    assert Path.home() == home, f"sandbox home not in effect: {Path.home()}"
    return root, home


def _seed(proj):
    """600 distinct memories, 6 directives. Deterministic (seeded RNG)."""
    import random
    sys.path.insert(0, str(PKG))
    from core.db import MemoryDB
    from core.layout import memory_dir
    from llm.memory_writer import upsert_batch
    md = memory_dir(proj)
    db = MemoryDB(md / "memory.db")
    pid = db.upsert_project(str(proj))
    random.seed(7)
    nouns = ("vault client timeout retry backoff scheduler queue worker cache "
             "token session cookie header router handler middleware parser "
             "lexer tokenizer index shard replica leader follower quorum ledger "
             "journal snapshot checkpoint migration schema column table view "
             "trigger cursor batch stream buffer socket listener proxy gateway "
             "certificate key secret rotation audit metric tracer logger "
             "sampler exporter dashboard alert pager runbook playbook pipeline "
             "stage artifact bundle package module plugin adapter driver kernel "
             "thread process container image registry cluster node pod service "
             "ingress egress firewall policy quota budget invoice receipt "
             "customer account profile avatar locale timezone calendar reminder "
             "digest newsletter template layout theme palette icon sprite font "
             "glyph").split()
    verbs = ("must never always should prefers rejected adopted deprecated "
             "renamed moved pinned capped throttled retried disabled enabled "
             "documented measured verified refused defaulted").split()
    zh = ["用户要求把超时设为三十秒", "缓存策略改为写穿", "登录接口必须限流",
          "数据库迁移需要备份", "部署脚本禁止直接推主干", "日志级别默认改为警告",
          "配置文件采用只读挂载", "接口返回值必须带版本号", "测试必须在临时目录运行",
          "发布前必须跑全部门禁"]
    mems = []
    for i in range(N_MEMORIES):
        if i % 6 == 0:
            c = zh[i % len(zh)] + "，编号" + str(i)
        else:
            c = (f"{random.choice(nouns)} {random.choice(verbs)} "
                 f"{random.choice(nouns)} {random.choice(nouns)} #{i}: "
                 + " ".join(random.sample(nouns, 6)))
        mems.append({"category": random.choice(["decision", "fact", "note",
                                                 "preference"]),
                     "content": c, "importance": random.randint(1, 5),
                     "topic": random.choice(nouns[:12]), "tags": ["seed"]})
    t0 = time.perf_counter()
    r = upsert_batch(db, pid, None, mems, memory_dir=md)
    seed_s = time.perf_counter() - t0
    for i in range(6):
        db.upsert_directive(pid, f"bench-directive-{i}",
                            demand=f"benchmark directive number {i} stays active",
                            quote=f"the user said directive {i}",
                            kind="constraint" if i % 3 == 0 else "standing")
    n_active = db.get_stats(pid).get("n_memories")
    return db, pid, md, {"seed_counts": {k: v for k, v in r.items()
                                        if k != "results"},
                         "seed_s": round(seed_s, 1), "active": n_active}


def _transcript(home, proj, session_id):
    from core.extractor import mangle_project_path
    tdir = home / ".claude" / "projects" / mangle_project_path(str(proj))
    tdir.mkdir(parents=True)
    path = tdir / f"{session_id}.jsonl"
    recs = [{"cwd": str(proj), "type": "user",
             "message": {"role": "user", "content": "please add the vault client timeout flag"}},
            {"cwd": str(proj), "type": "assistant",
             "message": {"role": "assistant", "content": "decided to bound the retry at 30 s"}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return path


def _payload(hook, proj, sid, transcript, **extra):
    data = {"cwd": str(proj), "session_id": sid}
    if hook == "user_prompt":
        data["prompt"] = "what did we decide about the vault client timeout?"
    elif hook == "post_tool_use":
        data.update(tool_name="Read", tool_input={"file_path": "notes.md"},
                    tool_response="an entirely harmless tool response body")
    elif hook == "session_start":
        data.update(transcript_path=str(transcript),
                    hook_event_name="SessionStart", source="startup")
    elif hook == "pre_compact":
        data.update(transcript_path=str(transcript), trigger="manual")
    data.update(extra)
    return data


def _run(hook, payload, work, label=None, show=False):
    """One fresh process through the driver. Returns the driver's result."""
    pfile = work / f"{label or hook}.json"
    pfile.write_text(json.dumps(payload), encoding="utf-8")
    env = dict(os.environ)
    argv = [sys.executable, str(Path(__file__).resolve()), "--drive",
            str(HOOKS / f"{hook}.py"), str(pfile)]
    if show:
        argv.append("--show")
    t0 = time.perf_counter()
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env,
                          timeout=300, cwd=str(work))
    wall = time.perf_counter() - t0
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("@@RESULT ")]
    if proc.returncode != 0 or not line:
        raise RuntimeError(f"{hook}: rc={proc.returncode} stderr={proc.stderr[-500:]!r} "
                           f"stdout={proc.stdout[-300:]!r}")
    res = json.loads(line[-1][len("@@RESULT "):])
    res["wall_ms"] = round(1000 * wall, 1)
    res["stderr_bytes"] = len(proc.stderr)
    res["hook"] = label or hook
    return res


def _matcher_starts(tool):
    """Would hooks.json's PostToolUse matcher start the hook for `tool`?"""
    cfg = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    m = cfg["hooks"]["PostToolUse"][0].get("matcher", "")
    return True if m == "" else bool(re.search(m, tool))


def _analyse_injection(text, db, pid, md):
    """How much of SessionStart's stdout is the same memory said twice."""
    manifest = {}
    try:
        manifest = json.loads((md / ".last_inject.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass  # why: no manifest = SessionStart tracked no ids; report 0 ids rather than fail the bench
    ids = []
    for key in ("critical_ids", "timeline_ids", "shown_ids"):
        ids += [i for i in (manifest.get(key) or []) if isinstance(i, int)]
    ids = sorted(set(ids))
    dup = 0
    max_occ = 0
    for mid in ids:
        row = db.get_memory(mid)
        if not row:
            continue
        needle = (row["content"] or "")[:48]
        if not needle:
            continue
        occ = text.count(needle)
        max_occ = max(max_occ, occ)
        if occ > 1:
            dup += 1
    return {"bytes": len(text.encode("utf-8")), "approx_tokens": len(text) // 4,
            "injected_ids": len(ids), "ids_repeated": dup,
            "max_occurrences": max_occ,
            "system_reminders": text.count("<system-reminder>"),
            "progress_full_file": "SINGLE SOURCE OF TRUTH" in text}


def _bench():
    root, home = _sandbox()
    results = []
    info = {}
    try:
        work = root / "work"
        work.mkdir()
        proj = root / "proj"
        proj.mkdir()
        db, pid, md, info = _seed(proj)
        sid = "bench-session-0001"
        transcript = _transcript(home, proj, sid)

        results.append(_run("user_prompt", _payload("user_prompt", proj, sid, transcript), work))
        for tool in ("Read", "Edit", "Agent", "mcp__github__get_issue"):
            r = _run("post_tool_use",
                     _payload("post_tool_use", proj, sid, transcript,
                              tool_name=tool,
                              tool_input={"file_path": "notes.md"} if tool in ("Read", "Edit")
                              else {"prompt": "x"}),
                     work, label=f"post_tool_use[{tool}]")
            r["matcher_starts_hook"] = _matcher_starts(tool)
            results.append(r)
        for i in range(N_STOP_RUNS):
            # a user turn precedes every Stop in a real session
            _run("user_prompt", _payload("user_prompt", proj, sid, transcript),
                 work, label=f"user_prompt[turn{i}]")
            r = _run("stop", _payload("stop", proj, sid, transcript), work,
                     label=f"stop[turn{i}]")
            results.append(r)
        sys.path.insert(0, str(PKG))
        from core.plan import capture_exit_plan_mode
        capture_exit_plan_mode(db, pid, "1. Add the vault client timeout flag\n"
                               "2. Write the migration\n3. Run the gates\n"
                               "4. Update README", md)
        results.append(_run("stop", _payload("stop", proj, sid, transcript), work,
                            label="stop[raw plan pending]"))
        ss = _run("session_start", _payload("session_start", proj, sid, transcript),
                  work, show=True)
        ss["injection"] = _analyse_injection(ss.pop("stdout", ""), db, pid, md)
        results.append(ss)
        rs = _run("session_start",
                  _payload("session_start", proj, sid, transcript, source="resume"),
                  work, label="session_start[resume]", show=True)
        rs["injection"] = _analyse_injection(rs.pop("stdout", ""), db, pid, md)
        results.append(rs)
        results.append(_run("pre_compact", _payload("pre_compact", proj, sid, transcript), work))
    finally:
        try:
            from core import logger as _logger_mod
            for lg in list(getattr(_logger_mod, "_loggers", {}).values()):
                lg.close()
        except ImportError:
            pass  # why: a failure before the package imported leaves no log handle to close
        tempfile.tempdir = None
        shutil.rmtree(root, ignore_errors=True)
    return info, results


def _table(info, results):
    out = [f"seeded {N_MEMORIES} memories in {info.get('seed_s')} s "
           f"({info.get('active')} active after reconcile), 6 directives; "
           f"python {sys.version.split()[0]} on {sys.platform}", "",
           "| hook | wall ms | in-process ms | sqlite opens | open+close ms | stdout B | note |",
           "|---|---|---|---|---|---|---|"]
    for r in results:
        note = ""
        if "matcher_starts_hook" in r:
            note = ("matcher starts hook" if r["matcher_starts_hook"]
                    else "matcher would NOT start hook")
        if "injection" in r:
            inj = r["injection"]
            note = (f"~{inj['approx_tokens']} tok, {inj['injected_ids']} ids, "
                    f"{inj['ids_repeated']} said twice (max x{inj['max_occurrences']}), "
                    f"{inj['system_reminders']} system-reminder, "
                    f"PROGRESS.md {'inlined whole' if inj['progress_full_file'] else 'digest/absent'}")
        if r["stderr_bytes"]:
            note = (note + "; " if note else "") + f"STDERR {r['stderr_bytes']} B"
        out.append(f"| {r['hook']} | {r['wall_ms']} | {r['inproc_ms']} | "
                   f"{r['connects']} | {r['open_close_ms']} | {r['stdout_bytes']} | {note} |")
    return "\n".join(out)


def main(argv):
    if argv[:1] == ["--drive"]:
        _drive(argv[1], argv[2], show="--show" in argv[3:])
        return 0
    info, results = _bench()
    if "--json" in argv:
        print(json.dumps({"info": info, "results": results}, ensure_ascii=False, indent=1))
    else:
        print(_table(info, results))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
