"""D: opt-out gate before any DB open, on a COPY of the package (config.json is package-relative)."""
import sys, json, shutil, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    pkg = sb / "pkgcopy" / "cc_memory"
    shutil.copytree(_h.PKG, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    excluded = sb / "excluded"; excluded.mkdir()
    live = sb / "live"
    db, pid, _ = _h.seed_project(live, [{"content": "Live project decision: keep the cache warm at boot"}])
    narrow = live / "vendor"; narrow.mkdir()
    cfg = json.loads((pkg / "config.json").read_text(encoding="utf-8-sig"))
    cfg["excluded_projects"] = [str(excluded), str(narrow)]
    (pkg / "config.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    mem = pkg / "cli" / "mem.py"; plan = pkg / "cli" / "plan.py"; mcp = pkg / "mcp" / "server.py"

    def run(script, args, project, cwd):
        return _h.run([sys.executable, str(script), "--project", str(project)] + args, cwd=cwd)
    for label, project, cwd in (("excluded fresh dir", excluded, excluded), ("excluded via ''", "", excluded),
                                ("excluded via .", ".", excluded), ("NARROW: live/vendor", narrow, narrow),
                                ("NARROW via . from vendor", ".", narrow)):
        rc, out, err = run(mem, ["add", "decision", "Should never be stored anywhere at all"], project, cwd)
        rc2, out2, err2 = run(plan, ["add", "should not exist"], project, cwd)
        print(f"{label:<28} mem rc={rc} {out.strip()[:70]!r} | plan rc={rc2} {out2.strip()[:60]!r}")
    print("excluded dir contents:", sorted(str(p.relative_to(excluded)) for p in excluded.rglob("*")) or "NONE")
    print("live/vendor contents  :", sorted(str(p.relative_to(narrow)) for p in narrow.rglob("*")) or "NONE")
    print("live memories:", db.get_stats(pid)["n_memories"], "(1 expected)")
    # MCP: server launched in the excluded dir; and in live/, asked for vendor/
    for cwd, proj_arg in ((excluded, None), (live, str(narrow)), (narrow, None)):
        frames = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_add", "arguments":
                   {"category": "decision", "content": "MCP write into an opted-out project", "importance": 5,
                    **({"project": proj_arg} if proj_arg else {})}}}]
        p = subprocess.run([sys.executable, str(mcp)], cwd=str(cwd), input="\n".join(json.dumps(f) for f in frames) + "\n",
                           capture_output=True, encoding="utf-8", timeout=60, env=_h.env())
        r = json.loads(p.stdout.strip().splitlines()[0])
        print(f"MCP cwd={cwd.name:<9} project={proj_arg!r:<50} -> isError={r['result'].get('isError')} {r['result']['content'][0]['text'][:70]}")
    print("excluded dir contents after MCP:", sorted(str(p.relative_to(excluded)) for p in excluded.rglob("*")) or "NONE")
    print("live/vendor after MCP:", sorted(str(p.relative_to(narrow)) for p in narrow.rglob("*")) or "NONE")
    print("live memories:", db.get_stats(pid)["n_memories"])
finally:
    _h.destroy_sandbox()
