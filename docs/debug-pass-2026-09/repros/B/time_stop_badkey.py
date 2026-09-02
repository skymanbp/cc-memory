"""Stop hook with a dead API key and >=3 observations: does it stay inside hooks.json's 22 s, and what
does it log per turn?"""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core.db import MemoryDB
    proj = sb.proj; (proj / ".ccm").mkdir()
    db = MemoryDB(proj / ".ccm" / "memory.db"); pid = db.upsert_project(str(proj))
    for i in range(5): db.insert_observation(pid, "S", "Bash", f"cmd {i}", "out")
    r = sb.run_hook("stop", {"session_id": "S", "cwd": str(proj)}, env_extra={"ANTHROPIC_API_KEY": "sk-ant-api03-deadbeef"}, timeout=60)
    print(f"rc={r['rc']} wall={r['secs']:.2f}s stderr={r['err']!r} stdout={r['out'].strip()[:80]!r}")
    logs = "".join(p.read_text() for p in (sb.home/".claude/hooks/cc-memory/logs").glob("*.log"))
    print("\n".join(l[:160] for l in logs.splitlines() if "STOP" in l)[:1200])
finally:
    sb.cleanup()
