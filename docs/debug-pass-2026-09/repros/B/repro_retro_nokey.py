"""session_start.retroactive_save loads up to 3 transcript windows (32 MiB JSON decode + full raw scan
each) BEFORE _retroactive_extract discovers there is no API key -- every SessionStart, forever,
because a file that yields no memories writes no sessions row and is never marked saved."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox, add_pkg_path
sb = Sandbox()
try:
    add_pkg_path()
    from core.db import MemoryDB
    from core.extractor import mangle_project_path
    proj = sb.proj; (proj / ".ccm").mkdir()
    db = MemoryDB(proj / ".ccm" / "memory.db"); pid = db.upsert_project(str(proj))
    # fully populated progress row so tier 3 does NOT load anything (isolates retroactive_save)
    db.upsert_progress(pid, current_request="r", status_done="d", status_in_flight="i", open_todos=[{"content":"t"}],
                       plan="p", critical_context=[{"id":1}], files_touched=[{"path":"f"}], transcript_ptr="/x", trigger_type="precompact")
    resolved = str(proj.resolve())
    tdir = sb.home / ".claude" / "projects" / mangle_project_path(resolved); tdir.mkdir(parents=True)
    rec = json.dumps({"cwd": resolved, "type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 2000}]}}) + "\n"
    per_file = 40 * 1024 * 1024
    for i in range(3):
        with open(tdir / f"old-{i}.jsonl", "w") as fh:
            n = 0
            while n < per_file:
                fh.write(rec); n += len(rec)
    print("transcripts:", [f"{p.name}={p.stat().st_size//2**20}MiB" for p in sorted(tdir.iterdir())])
    r = sb.run_hook("session_start", {"session_id": "cur", "cwd": str(proj)})
    print(f"no API key: rc={r['rc']} stderr={r['err']!r} wall={r['secs']:.2f}s  (hooks.json budget 15s)")
    # second start: identical cost -- nothing was recorded that would make the files skippable
    r2 = sb.run_hook("session_start", {"session_id": "cur", "cwd": str(proj)})
    print(f"second start:  wall={r2['secs']:.2f}s")
    # control: same hook with no transcripts on disk
    for p in tdir.iterdir(): p.unlink()
    r3 = sb.run_hook("session_start", {"session_id": "cur", "cwd": str(proj)})
    print(f"control (no transcripts): wall={r3['secs']:.2f}s")
    logs = "".join(p.read_text() for p in (sb.home/".claude/hooks/cc-memory/logs").glob("*.log"))
    print([l for l in logs.splitlines() if "retroactive" in l][:6])
finally:
    sb.cleanup()
