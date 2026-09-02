"""In-process: inject a concurrent settings.json writer between the installer's
read and its rename; the compare-and-swap must retry and keep BOTH edits."""
import json, os, shutil, sys, tempfile
from pathlib import Path
SB = Path(tempfile.mkdtemp(prefix="E_cas_"))
for k in ("HOME", "USERPROFILE"): os.environ[k] = str(SB)
(SB / "tmp").mkdir()
for k in ("TMPDIR", "TEMP", "TMP"): os.environ[k] = str(SB / "tmp")
try:
    assert Path.home() == SB, Path.home()
    sys.path.insert(0, "/home/user/cc-memory/cc_memory")
    from ui import installer
    assert installer.CLAUDE_DIR == SB / ".claude"
    installer.CLAUDE_DIR.mkdir()
    S = installer.SETTINGS_PATH
    S.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))

    orig = installer._read_settings
    state = {"n": 0}
    def hijacked():
        res = orig()
        state["n"] += 1
        if state["n"] == 1:
            d = json.loads(S.read_text())
            d["foreignKey"] = "landed between read and rename"
            S.write_text(json.dumps(d))
        return res
    installer._read_settings = hijacked
    logs = []
    ok = installer._merge_into_settings(installer._make_hooks_config(installer.TARGET_DIR), log_fn=logs.append)
    final = json.loads(S.read_text())
    print("install: ok=%s reads=%d foreignKey kept=%s hooks registered=%s" % (
        ok, state["n"], "foreignKey" in final, sorted(final.get("hooks", {}))))
    print("  log:", [l for l in logs if "underneath" in l])

    # uninstall path, same injection
    state["n"] = 0
    def hijacked2():
        res = orig()
        state["n"] += 1
        if state["n"] == 1:
            d = json.loads(S.read_text()); d["foreignKey2"] = "x"; S.write_text(json.dumps(d))
        return res
    installer._read_settings = hijacked2
    logs = []
    ok = installer._uninstall_settings(log_fn=logs.append)
    final = json.loads(S.read_text())
    print("uninstall: ok=%s reads=%d foreignKey2 kept=%s hooks left=%s" % (
        ok, state["n"], "foreignKey2" in final, final.get("hooks")))

    # absent-file sentinel: settings absent at read, created by someone in the window
    installer._read_settings = orig
    S.unlink()
    fp = installer._settings_fingerprint()
    print("absent fingerprint sentinel:", fp)
    state["n"] = 0
    def hijacked3():
        res = orig()
        state["n"] += 1
        if state["n"] == 1:
            S.write_text(json.dumps({"createdByClaudeCode": True}))
        return res
    installer._read_settings = hijacked3
    logs = []
    ok = installer._merge_into_settings(installer._make_hooks_config(installer.TARGET_DIR), log_fn=logs.append)
    final = json.loads(S.read_text())
    print("absent->created in window: ok=%s reads=%d createdByClaudeCode kept=%s hooks=%s" % (
        ok, state["n"], "createdByClaudeCode" in final, sorted(final.get("hooks", {}))))
    # hooks.json timeouts vs literal table
    declared, src = installer._declared_hook_timeouts()
    lit = {ev: {False: t} for ev, (_s, t) in installer.HOOK_SCRIPTS.items()}
    lit["PreCompact"][True] = installer.ASYNC_HOOK[2]
    print("declared==literal:", declared == lit, "src:", src.name if src else None)
finally:
    shutil.rmtree(SB, ignore_errors=True)
    print("sandbox removed:", not SB.exists())
