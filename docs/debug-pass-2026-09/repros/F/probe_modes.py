"""F probe: core.modes read_config / is_excluded / config_fault / should_observe."""
import os, sys, json, shutil, tempfile
SB = tempfile.mkdtemp(prefix="F-modes-")
for k in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[k] = SB
tempfile.tempdir = None
PKG = os.path.join(SB, "pkg", "cc_memory")
shutil.copytree("/home/user/cc-memory/cc_memory", PKG,
                ignore=shutil.ignore_patterns("__pycache__"))
sys.path.insert(0, PKG)
CFG = os.path.join(PKG, "config.json")
surprises = []
def say(label, got, want=None):
    flag = "" if want is None or got == want else "   <-- UNEXPECTED (wanted %r)" % (want,)
    if flag:
        surprises.append(label)
    print(f"  {label:<58} -> {got!r}{flag}")
def setcfg(b):
    if os.path.isdir(CFG):
        os.rmdir(CFG)
    if b is None:
        if os.path.exists(CFG):
            os.remove(CFG)
    else:
        with open(CFG, "wb") as fh:
            fh.write(b)
try:
    from core import modes
    print("== read_config shapes")
    setcfg(None);                      say("absent", modes.read_config(), ({}, None))
    setcfg(b"");                       say("empty", modes.read_config(), ({}, None))
    setcfg(b"  \n\t ");                say("whitespace", modes.read_config(), ({}, None))
    setcfg(b"\xef\xbb\xbf{\"a\":1}");  say("BOM+valid", modes.read_config(), ({"a": 1}, None))
    setcfg("{\"a\":1}".encode("utf-16"));      r = modes.read_config(); say("UTF-16 with BOM -> cfg None", r[0], None); print("     note:", r[1][:70])
    setcfg("{\"a\":1}".encode("utf-16-le"));   r = modes.read_config(); say("UTF-16-LE no BOM -> cfg None", r[0], None); print("     note:", r[1][:70])
    setcfg(b"{\"a\":1} trailing");     say("trailing garbage -> cfg None", modes.read_config()[0], None)
    setcfg(b"[1,2]");                  say("JSON array -> cfg None", modes.read_config()[0], None)
    setcfg(b"null");                   say("JSON null -> cfg None", modes.read_config()[0], None)
    setcfg(b"\"s\"");                  say("JSON string -> cfg None", modes.read_config()[0], None)
    setcfg(None); os.mkdir(CFG);       r = modes.read_config(); say("config.json is a DIRECTORY -> cfg None", r[0], None); print("     note:", (r[1] or "")[:70]); os.rmdir(CFG)
    setcfg(b"\xff\xfe\x00\x00 {}");    say("invalid utf-8 bytes -> cfg None", modes.read_config()[0], None)

    print("== is_excluded / config_fault with excluded_projects shapes")
    proj = os.path.join(SB, "project"); os.makedirs(os.path.join(proj, "sub", "deep"))
    other = os.path.join(SB, "projectile"); os.makedirs(other)
    link = os.path.join(SB, "lnk"); os.symlink(proj, link)
    def cfg(entries):
        setcfg(json.dumps({"excluded_projects": entries}).encode())
    cfg([proj]);          say("listed dir itself", modes.is_excluded(proj), True)
    say("  subdir of listed", modes.is_excluded(os.path.join(proj, "sub", "deep")), True)
    say("  prefix-sharing sibling /projectile", modes.is_excluded(other), False)
    say("  symlink cwd -> listed", modes.is_excluded(link), True)
    say("  cwd with trailing slash", modes.is_excluded(proj + "/"), True)
    say("  cwd with /./ and /../", modes.is_excluded(proj + "/sub/../sub/./deep"), True)
    say("  cwd different CASE (POSIX must be False)", modes.is_excluded(proj.upper()), False)
    say("  config_fault (valid listing) is None", modes.config_fault(), None)
    cfg([link]);          say("listed entry is a symlink; real cwd", modes.is_excluded(proj), True)
    cfg([proj + "/"]);    say("listed with trailing slash", modes.is_excluded(os.path.join(proj, "sub")), True)
    cfg(proj);            say("bare string entry", modes.is_excluded(proj), True)
    cfg([]);              say("empty list", modes.is_excluded(proj), False); say("  config_fault", modes.config_fault(), None)
    cfg("");              say("empty string", modes.is_excluded(proj), False)
    cfg({});              say("dict -> fail closed", modes.is_excluded(proj), True); say("  config_fault non-None", bool(modes.config_fault()), True)
    cfg(0);               say("0 -> fail closed", modes.is_excluded(proj), True)
    cfg(False);           say("false -> fail closed", modes.is_excluded(proj), True)
    cfg([1, None, {}]);   say("list of non-strings -> not excluded, no raise", modes.is_excluded(proj), False); say("  config_fault", modes.config_fault(), None)
    cfg(["~nosuchuser_zz/x", proj]); say("~user entry before a real one: real one still matches", modes.is_excluded(proj), True)
    cfg(["~/project"]);   say("~ entry expands against HOME", modes.is_excluded(proj), True)
    cfg(["/"]);           say("root listed excludes everything", modes.is_excluded(proj), True)
    cfg(["project"]);     cwd0 = os.getcwd(); os.chdir(SB); say("relative entry resolves against process cwd", modes.is_excluded(proj), True); os.chdir(cwd0)
    say("  same relative entry from a different cwd", modes.is_excluded(proj), False)
    cfg(["a\x00b"]);      say("NUL in entry: no raise", modes.is_excluded(proj), False)
    cfg([proj])
    print("== is_excluded cwd shapes (listed=%s)" % proj)
    for label, cwd, want in [("None", None, False), ("int", 123, False), ("bytes", proj.encode(), False),
                             ("empty", "", False), ("blank", "   ", False),
                             ("NUL in cwd", proj + "\x00", False), ("5000-char path", proj + "/" + "x" * 5000, False),
                             ("'.' (resolves to process cwd)", ".", False), ("list", [proj], False)]:
        try:
            say(label, modes.is_excluded(cwd), want)
        except Exception as e:
            say(label, f"RAISED {type(e).__name__}: {e}", want)
    print("== _norm_path never raises")
    for v in ["~nosuchuser/x", "a\x00b", "", ".", "/", "x" * 10000, "\\\\server\\share"]:
        try:
            r = modes._norm_path(v); print(f"  {v[:30]!r:<34} -> {r[:60]!r}")
        except Exception as e:
            say(f"_norm_path({v[:20]!r})", f"RAISED {type(e).__name__}", "no raise")
    print("== should_observe")
    say("code: Edit", modes.should_observe("code", "Edit"), True)
    say("code: TodoWrite (skip)", modes.should_observe("code", "TodoWrite"), False)
    say("code: ExitPlanMode (not in observe)", modes.should_observe("code", "ExitPlanMode"), False)
    say("research: Edit (in skip)", modes.should_observe("research", "Edit"), False)
    say("writing: Bash (in skip)", modes.should_observe("writing", "Bash"), False)
    say("unknown mode falls back to code", modes.should_observe("nosuchmode", "Edit"), True)
    say("None mode falls back to code", modes.should_observe(None, "Edit"), True)
    for m, spec in modes.MODES.items():
        both = set(spec["observe_tools"]) & set(spec["skip_tools"])
        say(f"{m}: tools in BOTH lists", sorted(both), [])
    print("== cli_opt_out_notice")
    cfg([proj]); say("cli_opt_out_notice(proj) non-None", bool(modes.cli_opt_out_notice(proj)), True)
    say("cli_opt_out_notice(other) None", modes.cli_opt_out_notice(other), None)
    say("cli_opt_out_notice(123)", modes.cli_opt_out_notice(123) is None or "str", True)
    print("\nSURPRISES:", surprises or "none")
finally:
    shutil.rmtree(SB, ignore_errors=True)
