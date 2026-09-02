"""D: (a) does search_fts rebuild the index on malformed/empty queries (in-process counter);
(b) --raw-file with a UTF-8 BOM; (c) consolidate in LLM mode with no API key."""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    proj = sb / "projA"
    db, pid, ids = _h.seed_project(proj, [
        {"content": f"Decision {i}: subsystem {i} uses strategy {i} because of constraint {i}"} for i in range(5)])
    calls = []
    orig = db._rebuild_fts5
    db._rebuild_fts5 = lambda *a, **k: (calls.append(1), orig(*a, **k))
    for q in ("", '"', "NEAR(", "alpha OR", "x*\"y", "%", "Decision", "\x00", "   "):
        n0 = len(calls)
        rows = db.search_fts(pid, q, limit=30)
        print(f"(a) search_fts({q!r:<8}) -> {len(rows)} rows, rebuilds={len(calls) - n0}")

    fbom = sb / "plan_bom.md"
    fbom.write_bytes("﻿# Plan\n- step one\n".encode("utf-8"))
    rc, out, err = _h.mem(["plan-set", "--raw-file", str(fbom)], proj)
    raw_md_bom = (proj / '.ccm' / '.plan_raw.md').read_bytes()[:3] == bytes([0xef, 0xbb, 0xbf])
    raw = (db.get_plan_active(pid) or {}).get("raw") or ""
    print(f"(b) --raw-file with BOM: rc={rc} stored raw starts with U+FEFF: {raw.startswith(chr(0xfeff))} "
          f"| .plan_raw.md starts with BOM: {raw_md_bom}")

    t0 = time.time()
    rc, out, err = _h.mem(["consolidate"], proj, cwd=proj)
    print(f"(c) consolidate (LLM mode, no key) rc={rc} {time.time()-t0:.1f}s tb={_h.is_traceback(err)} | "
          + " | ".join(l.strip()[:70] for l in out.splitlines() if "LLM" in l or "key" in l.lower() or "FAIL" in l)[:300])
    t0 = time.time()
    rc, out, err = _h.mem(["consolidate", "--deep"], proj, cwd=proj)
    print(f"    consolidate --deep (no key) rc={rc} {time.time()-t0:.1f}s tb={_h.is_traceback(err)} | "
          + " | ".join(l.strip()[:70] for l in out.splitlines() if "deep" in l.lower() or "FAIL" in l or "judge" in l.lower())[:300])
finally:
    _h.destroy_sandbox()
