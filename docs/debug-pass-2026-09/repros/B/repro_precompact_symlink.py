"""pre_compact: the last-resort handler writes .last_save.json THROUGH a linked .ccm/
that ensure_memory_dir just refused (privacy fail-closed)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from harness import Sandbox
sb = Sandbox()
try:
    victim = sb.root / "victim"; victim.mkdir()
    (victim / ".pre_compact_attempt.json").write_text("{}")   # a file at the link target
    os.symlink(victim, sb.proj / ".ccm")                       # planted link where the state dir goes
    tp = sb.root / "t.jsonl"
    tp.write_text(json.dumps({"type":"user","cwd":str(sb.proj),"message":{"role":"user","content":"hi"}}) + "\n")
    r = sb.run_hook("pre_compact", {"session_id":"s","cwd":str(sb.proj),"transcript_path":str(tp),"trigger":"manual"})
    print("rc", r["rc"], "stderr", repr(r["err"][:100]), "stdout", repr(r["out"][:100]))
    print("victim dir now holds:", sorted(p.name for p in victim.iterdir()))
    ls = victim / ".last_save.json"
    print(".last_save.json written through the link:", ls.exists(), ls.read_text() if ls.exists() else "")
    print(".pre_compact_attempt.json at target deleted through the link:", not (victim / ".pre_compact_attempt.json").exists())
finally:
    sb.cleanup()
