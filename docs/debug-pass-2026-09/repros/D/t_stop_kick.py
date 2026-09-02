"""D: end-to-end — after `/cc-mem consolidate` (as commands/cc-mem.md invokes it), does the
Stop hook spawn a redundant background consolidation?"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    proj = sb / "projA"
    db, pid, _ = _h.seed_project(proj, [
        {"content": f"Decision number {i}: use component {i} for subsystem {i} because it scales"} for i in range(12)])
    mdir = proj / ".ccm"
    marker = mdir / ".last_consolidation.json"
    kick = mdir / ".consolidation.kick"
    stop = _h.PKG / "hooks" / "stop.py"
    payload = json.dumps({"cwd": str(proj), "session_id": "sess-1", "hook_event_name": "Stop",
                          "transcript_path": str(sb / "t.jsonl"), "stop_hook_active": False})

    rc, out, err = _h.mem(["consolidate", "--no-llm"], ".", cwd=proj)   # exactly what /cc-mem runs
    print("CLI consolidate rc", rc, "| marker.project_path =", repr(json.loads(marker.read_text())["project_path"]))
    rc, out, err = _h.run([sys.executable, str(stop)], cwd=proj, stdin=payload, timeout=60)
    print(f"Stop hook rc={rc} stderr={err.strip()[:100]!r}")
    print("  .consolidation.kick written (= background worker spawned):", kick.exists())
    time.sleep(6)
    print("  marker after the redundant worker ran:", repr(json.loads(marker.read_text())["project_path"])
          if marker.exists() else "absent")
    # control: a marker whose path is the absolute cwd is honoured
    kick.unlink(missing_ok=True)
    rc, out, err = _h.mem(["consolidate", "--no-llm"], str(proj), cwd=sb)
    rc, out, err = _h.run([sys.executable, str(stop)], cwd=proj, stdin=payload, timeout=60)
    print("control (absolute --project): kick written:", kick.exists())
finally:
    _h.destroy_sandbox()
