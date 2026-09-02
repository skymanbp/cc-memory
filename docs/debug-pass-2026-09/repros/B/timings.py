"""Happy-path wall-clock of every hook vs its hooks.json timeout (no API key -> no network legs)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    proj = sb.proj
    tp = sb.root / "t.jsonl"
    recs = [json.dumps({"type":"user","cwd":str(proj),"message":{"role":"user","content":"Build the billing module with decimal math"}})]
    recs += [json.dumps({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":f"Decided: use Decimal for money, step {i} done, file src/bill_{i}.py edited"}]}}) for i in range(300)]
    recs.append(json.dumps({"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"TodoWrite","input":{"todos":[{"content":"wire the invoice PDF","status":"pending","priority":"high"}]}}]}}))
    tp.write_text("\n".join(recs) + "\n")
    base = {"session_id": "S", "cwd": str(proj), "transcript_path": str(tp), "trigger": "manual"}
    budget = {"user_prompt": 8, "post_tool_use": 8, "stop": 22, "pre_compact": 120, "consolidate_async": 300, "session_start": 15}
    runs = [
        ("user_prompt", "first-contact init", dict(base, prompt="Build the billing module")),
        ("post_tool_use", "ExitPlanMode 100KB", dict(base, tool_name="ExitPlanMode", tool_input={"plan": ("# Plan\n" + "1. step with detail\n" * 5000)})),
        ("post_tool_use", "TodoWrite", dict(base, tool_name="TodoWrite", tool_input={"todos": [{"content": "step with detail", "status": "in_progress"}]})),
        ("post_tool_use", "Read 600KB body", dict(base, tool_name="Read", tool_input={"file_path": "/x"}, tool_response="y" * 600_000)),
        ("post_tool_use", "Bash git push", dict(base, tool_name="Bash", tool_input={"command": "git push"}, tool_response="ok")),
        ("stop", "turn", base),
        ("pre_compact", "301-record transcript", base),
        ("consolidate_async", "hook leg", base),
        ("session_start", "startup", dict(base, source="startup")),
    ]
    for hook, label, pl in runs:
        r = sb.run_hook(hook, pl)
        flag = "" if r["rc"] == 0 and not r["err"] else f"  <-- rc={r['rc']} err={r['err'][:80]!r}"
        print(f"{hook:17s} {label:22s} {r['secs']:5.2f}s / {budget[hook]:3d}s  stdout_lines={len(r['out'].strip().splitlines())}{flag}")
finally:
    sb.cleanup()
