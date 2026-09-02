"""Drive all six hooks as subprocesses through a hostile payload matrix.
Asserts rc==0, stderr empty, stdout shape, and no state dir planted in the hook's own cwd."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path, HOOKS

sb = Sandbox()
try:
    add_pkg_path()
    from core.db import MemoryDB
    proj = sb.proj
    (proj / ".ccm").mkdir()
    db = MemoryDB(proj / ".ccm" / "memory.db")
    pid = db.upsert_project(str(proj))
    # a JSONL transcript with non-record lines
    tp = sb.root / "t.jsonl"
    lines = [json.dumps({"type":"user","cwd":str(proj),"message":{"role":"user","content":"hello world do the thing"}}),
             "null", "42", '"s"', "[1,2]", "true", "not json at all",
             json.dumps({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"ok done"}]}})]
    tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    afile = sb.root / "afile.txt"; afile.write_text("x")
    huge = "x" * (3 * 1024 * 1024)

    base = {"session_id": "sess-1", "cwd": str(proj), "transcript_path": str(tp), "trigger": "manual",
            "prompt": "hi", "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": "out"}
    def mk(**over):
        d = dict(base); d.update(over); return d

    payloads = {
        "wellformed": mk(),
        "empty_stdin": None,
        "non_utf8": b"\xff\xfe{\"cwd\": 1}",
        "null": b"null", "int": b"42", "str": b'"s"', "list": b"[1,2]", "true": b"true",
        "no_keys": {},
        "cwd_int": mk(cwd=123), "cwd_list": mk(cwd=["a"]), "cwd_null": mk(cwd=None), "cwd_empty": mk(cwd=""),
        "cwd_missing_dir": mk(cwd=str(sb.root / "nope" / "deeper")),
        "cwd_file": mk(cwd=str(afile)),
        "cwd_nul": mk(cwd=str(proj) + "\x00x"),
        "cwd_dotdot": mk(cwd=str(proj / ".." / "proj")),
        "sid_int": mk(session_id=123), "sid_list": mk(session_id=[1]), "sid_null": mk(session_id=None),
        "sid_sep": mk(session_id="../../etc/passwd"), "sid_slash": mk(session_id="a/b\\c"), "sid_nul": mk(session_id="a\x00b"),
        "tp_missing": mk(transcript_path=str(sb.root / "missing.jsonl")),
        "tp_dir": mk(transcript_path=str(sb.root)),
        "tp_nonjsonl": mk(transcript_path=str(afile)),
        "tp_int": mk(transcript_path=5), "tp_nul": mk(transcript_path=str(tp) + "\x00"),
        "trigger_list": mk(trigger=[1]), "trigger_int": mk(trigger=7),
        "prompt_int": mk(prompt=5), "prompt_huge": mk(prompt=huge),
        "prompt_private": mk(prompt="<private>secret</private> rest"),
        "prompt_dangling_private": mk(prompt="ok <private>secret and more"),
        "tool_input_str": mk(tool_input="str"), "tool_input_null": mk(tool_input=None),
        "tool_input_bad_cmd": mk(tool_input={"command": 5}),
        "tool_name_int": mk(tool_name=5), "tool_name_empty": mk(tool_name=""),
        "todos_strings": mk(tool_name="TodoWrite", tool_input={"todos": ["fix bug", "x"]}),
        "todos_nondict": mk(tool_name="TodoWrite", tool_input={"todos": [1, None]}),
        "exitplan_int": mk(tool_name="ExitPlanMode", tool_input={"plan": 7}),
        "exitplan_ok": mk(tool_name="ExitPlanMode", tool_input={"plan": "# Plan\n1. do x\n2. do y"}),
        "huge_payload": mk(tool_response=huge),
    }

    problems = []
    for hook in ("user_prompt", "post_tool_use", "stop", "pre_compact", "consolidate_async", "session_start"):
        for name, pl in payloads.items():
            if pl is None:
                r = sb.run_hook(hook, None, stdin_bytes=b"")
            elif isinstance(pl, bytes):
                r = sb.run_hook(hook, None, stdin_bytes=pl)
            else:
                r = sb.run_hook(hook, pl)
            bad = []
            if r["rc"] != 0: bad.append(f"rc={r['rc']}")
            if r["err"].strip(): bad.append(f"stderr={r['err'][:300]!r}")
            out = r["out"]
            if hook in ("user_prompt", "post_tool_use", "consolidate_async") and out.strip():
                bad.append(f"stdout nonempty={out[:200]!r}")
            if hook == "pre_compact" and out.strip() and len(out.strip().splitlines()) != 1:
                bad.append(f"stdout lines={len(out.strip().splitlines())} {out[:200]!r}")
            if hook == "stop" and out.strip():
                s = out.strip()
                if s.startswith("{"):
                    try:
                        d = json.loads(s); assert d.get("decision") == "block"
                    except Exception as e: bad.append(f"bad block json {e} {s[:200]!r}")
                elif len(s.splitlines()) != 1:
                    bad.append(f"stop stdout lines={len(s.splitlines())} {s[:200]!r}")
            pl_ = sb.planted()
            if pl_: bad.append(f"planted={pl_}")
            if r["secs"] > 10: bad.append(f"slow={r['secs']:.1f}s")
            if bad:
                problems.append((hook, name, bad))
                print(f"[BAD] {hook:17s} {name:24s} {bad}")
    print(f"\n{len(problems)} problem(s) across {6*len(payloads)} runs")
    # show what the project looks like now
    print("proj .ccm:", sorted(p.name for p in (proj / ".ccm").iterdir()))
finally:
    sb.cleanup()
