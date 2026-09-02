"""Drive cc_memory/ui/installer.py as a subprocess against sandboxed HOMEs."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

REPO = Path("/home/user/cc-memory")
INST = REPO / "cc_memory" / "ui" / "installer.py"
SANDBOX = Path(tempfile.mkdtemp(prefix="E_inst_"))

def env_for(home):
    e = dict(os.environ)
    for k in ("HOME", "USERPROFILE"):
        e[k] = str(home)
    tmp = home / "tmp"; tmp.mkdir(exist_ok=True)
    for k in ("TMPDIR", "TEMP", "TMP"):
        e[k] = str(tmp)
    return e

def run(home, *args):
    r = subprocess.run([sys.executable, str(INST), *args], env=env_for(home),
                       capture_output=True, encoding="utf-8", cwd=str(SANDBOX))
    return r.returncode, r.stdout + r.stderr

def fresh(name):
    h = SANDBOX / name; h.mkdir(); return h

def settings(home): return home / ".claude" / "settings.json"

def main():
    # ---- S1: mixed matcher group + BOM + user hooks + hooks-list shape -----
    h = fresh("s1")
    (h / ".claude").mkdir()
    user_hook = {"type": "command", "command": "python3 /home/me/audit.py --repo /x/cc-memory"}
    s = {"permissions": {"allow": ["Bash(ls)"], "additionalDirectories": ["/x/cc-memory"]},
         "hooks": {"Stop": [{"matcher": "", "hooks": [
                        user_hook,
                        {"type": "command", "command": f'python3 "{h}/.claude/hooks/cc-memory/hooks/stop.py"', "timeout": 22}]}],
                   "Notification": [{"matcher": "", "hooks": [{"type": "command", "command": "notify-send hi"}]}]}}
    settings(h).write_bytes(b"\xef\xbb\xbf" + json.dumps(s, indent=4).encode())
    rc, out = run(h, "--cli")
    print(f"S1 install rc={rc}")
    d = json.loads(settings(h).read_text(encoding="utf-8-sig"))
    stop_cmds = [e["command"] for g in d["hooks"]["Stop"] for e in g["hooks"]]
    print("  Stop cmds after install:", [c[:40] for c in stop_cmds])
    print("  user audit hook kept:", any("audit.py" in c for c in stop_cmds))
    print("  Notification kept:", "Notification" in d["hooks"])
    print("  our stop entries:", sum("cc-memory/hooks/stop.py" in c for c in stop_cmds))
    print("  events:", sorted(d["hooks"]))
    print("  bak exists:", (h / ".claude" / "settings.json.cc-memory.bak").exists())
    print("  manifest:", json.loads((h/".claude/hooks/cc-memory/installed_surfaces.json").read_text())["files"])
    # reinstall -> no duplicate entries
    rc, out = run(h, "--cli")
    d = json.loads(settings(h).read_text(encoding="utf-8-sig"))
    stop_cmds = [e["command"] for g in d["hooks"]["Stop"] for e in g["hooks"]]
    print(f"S1 reinstall rc={rc} our stop entries:", sum("cc-memory/hooks/stop.py" in c for c in stop_cmds),
          "user kept:", any("audit.py" in c for c in stop_cmds))
    # user's own python under TARGET_DIR (non-managed subdir) + stale module in managed dir
    (h/".claude/hooks/cc-memory/mystuff").mkdir()
    (h/".claude/hooks/cc-memory/mystuff/tool.py").write_text("print(1)\n")
    (h/".claude/hooks/cc-memory/core/old_removed_module.py").write_text("x=1\n")
    (h/".claude/hooks/cc-memory/logs").mkdir(exist_ok=True)
    (h/".claude/hooks/cc-memory/logs/x.log").write_text("log\n")
    rc, out = run(h, "--cli")
    print("  after upgrade: user mystuff/tool.py exists:", (h/".claude/hooks/cc-memory/mystuff/tool.py").exists(),
          "| stale core/old_removed_module.py exists:", (h/".claude/hooks/cc-memory/core/old_removed_module.py").exists())
    print("  prune lines:", [l.strip() for l in out.splitlines() if "Pruned" in l])
    # uninstall
    rc, out = run(h, "--cli", "--uninstall")
    print(f"S1 --cli --uninstall rc={rc}")
    print("  printed lines mentioning 'memory/':", [l for l in out.splitlines() if "memory/" in l])
    d = json.loads(settings(h).read_text(encoding="utf-8-sig"))
    print("  hooks after uninstall:", {k: [[e['command'][:30] for e in g['hooks']] for g in v] for k, v in d.get("hooks", {}).items()})
    print("  additionalDirectories:", d["permissions"]["additionalDirectories"])
    print("  logs preserved:", (h/".claude/hooks/cc-memory/logs/x.log").exists(),
          "| package gone:", not (h/".claude/hooks/cc-memory/core").exists())
    print("  surfaces gone:", not (h/".claude/commands/cc-mem.md").exists(), not (h/".claude/skills/ccm-load").exists())

    # ---- S2: shapes: settings is a list / hooks is a string / hooks null ----
    for label, content in (("array", "[1,2]"), ("hooks-string", '{"hooks": "nope"}'),
                           ("hooks-null", '{"hooks": null}'), ("empty", ""), ("garbage", "{not json")):
        h = fresh("s2_" + label); (h/".claude").mkdir(); settings(h).write_text(content)
        rc, out = run(h, "--cli")
        d = None
        try: d = json.loads(settings(h).read_text(encoding="utf-8-sig"))
        except Exception as e: d = f"<{e}>"
        print(f"S2 {label}: rc={rc} events={sorted(d['hooks']) if isinstance(d, dict) and isinstance(d.get('hooks'), dict) else d if not isinstance(d, dict) else 'no-hooks'}",
              "| warn:", [l.strip() for l in out.splitlines() if "WARN" in l or "FAIL" in l][:2])

    # ---- S3: flags -----------------------------------------------------------
    h = fresh("s3")
    for args in (("--unistall",), ("--project", "/tmp/x"), ("--cli", "--bogus"), ("--help",), ("--cli", "--uninstall", "--force")):
        rc, out = run(h, *args)
        print(f"S3 {args}: rc={rc} first={out.splitlines()[0][:70] if out else ''!r}")

    # ---- S4: symlinked settings.json (dotfiles manager) ---------------------
    h = fresh("s4"); (h/".claude").mkdir(); (h/"dotfiles").mkdir()
    target = h/"dotfiles"/"claude-settings.json"
    target.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    os.symlink(target, settings(h))
    rc, out = run(h, "--cli")
    print(f"S4 symlinked settings.json: rc={rc}")
    print("  settings.json still a symlink:", os.path.islink(settings(h)))
    print("  dotfiles target has hooks:", "hooks" in json.loads(target.read_text()))
    print("  ~/.claude/settings.json has hooks:", "hooks" in json.loads(settings(h).read_text()))
    print("  warn lines:", [l.strip() for l in out.splitlines() if "WARN" in l or "ERR" in l])

    # ---- S5: ~/.claude missing entirely -------------------------------------
    h = fresh("s5")
    rc, out = run(h, "--cli")
    print(f"S5 no ~/.claude: rc={rc} settings created:", settings(h).exists())

    # ---- S6: corrupt manifest + user-owned same-named surface ---------------
    h = fresh("s6"); (h/".claude"/"commands").mkdir(parents=True)
    (h/".claude/hooks/cc-memory").mkdir(parents=True)
    (h/".claude/hooks/cc-memory/installed_surfaces.json").write_text("{corrupt")
    (h/".claude/commands/cc-mem.md").write_text("# user's own file\n")
    rc, out = run(h, "--uninstall")
    print(f"S6 uninstall with corrupt manifest: rc={rc} user's commands/cc-mem.md survived:",
          (h/".claude/commands/cc-mem.md").exists())

if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
        print("sandbox removed:", not SANDBOX.exists())
