"""F probe: core.markers safe_id / marker_dir / write_marker / read_marker / _is_link."""
import os, sys, stat, shutil, tempfile, subprocess, textwrap
SB = tempfile.mkdtemp(prefix="F-markers-")
for k in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[k] = SB
tempfile.tempdir = None
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
surprises = []
def say(label, got, want=None):
    flag = "" if want is None or got == want else "   <-- UNEXPECTED (wanted %r)" % (want,)
    if flag: surprises.append(label)
    print(f"  {label:<60} -> {got!r}{flag}")
try:
    from core import markers
    from pathlib import Path
    print("== safe_id")
    a, b = "0123456789abcdef-AAAA", "0123456789abcdef-BBBB"
    say("two ids sharing a 16-char prefix differ", markers.safe_id(a) != markers.safe_id(b), True)
    say("len", len(markers.safe_id(a)), 16)
    for v in (None, b"bytes", "", 123, "x" * 100000, "\udcff"):
        try: say(f"safe_id({str(v)[:12]!r}) ok", isinstance(markers.safe_id(v), str), True)
        except Exception as e: say(f"safe_id({str(v)[:12]!r})", f"RAISED {type(e).__name__}", "no raise")
    print("== marker_dir")
    d = markers.marker_dir(); st = os.lstat(d)
    say("marker_dir under sandbox TMPDIR", str(d).startswith(SB), True)
    say("mode 0700", oct(stat.S_IMODE(st.st_mode)), "0o700")
    say("_dir_is_private(marker_dir)", markers._dir_is_private(d), True)
    say("_dir_is_private(/tmp) (1777 shared root)", markers._dir_is_private(Path("/tmp")), False)
    print("== write/read")
    p = markers.marker_path("cc_mem_probe_", markers.safe_id("s"))
    say("write ok", markers.write_marker(p, "hello 中文"), True)
    say("read back", markers.read_marker(p), "hello 中文")
    say("file mode 0600", oct(stat.S_IMODE(os.lstat(p).st_mode)), "0o600")
    with open(p, "wb") as fh: fh.write(b"\xff\xfe bad")
    say("non-utf8 marker decodes with replacement", "�" in markers.read_marker(p), True)
    os.remove(p); os.mkdir(p)
    say("read_marker on a DIRECTORY -> default", markers.read_marker(p, "DEF"), "DEF")
    say("write_marker on a DIRECTORY -> False", markers.write_marker(p, "x"), False)
    os.rmdir(p)
    victim = Path(SB) / "victim.txt"; victim.write_text("precious")
    os.symlink(victim, p)
    say("write_marker through planted symlink refused", markers.write_marker(p, "x"), False)
    say("  victim untouched", victim.read_text(), "precious")
    say("read_marker through planted symlink -> default", markers.read_marker(p, "DEF"), "DEF")
    say("_is_link(symlink)", markers._is_link(p), True)
    os.remove(p)
    say("_is_link(missing)", markers._is_link(p), False)
    say("_is_link(dir)", markers._is_link(d), False)
    say("read_marker(missing) -> default", markers.read_marker(p, "DEF"), "DEF")
    say("write_marker parent missing -> False", markers.write_marker(Path(SB) / "nope" / "m", "x"), False)
    for bad in (123, None, b"bytes"):
        try: say(f"write_marker(text={bad!r}) never raises", markers.write_marker(p, bad), None)
        except Exception as e: say(f"write_marker(text={bad!r}) never raises", f"RAISED {type(e).__name__}", "no raise")
        if os.path.exists(p): os.remove(p)
    print("== pre-planted directory owned by another uid (co-tenant attack) -> degrade, never trust")
    d2 = Path(SB) / "t2"; d2.mkdir(); planted = d2 / f"cc-memory-{os.getuid()}"; planted.mkdir(mode=0o777)
    os.chown(planted, 54321, 54321)
    os.environ["TMPDIR"] = str(d2); tempfile.tempdir = None
    md = markers.marker_dir()
    say("marker_dir() with attacker-owned dir returns", str(md.relative_to(SB)))
    say("  write refused / not persisted there", markers.write_marker(md / "cc_mem_x", "secret"), None)
    say("  attacker dir empty afterwards", sorted(os.listdir(planted)), [])
    os.environ["TMPDIR"] = SB; tempfile.tempdir = None
    print("== when no system temp dir is usable, tempfile.gettempdir() falls back to CWD")
    # faithful stdlib mechanism: only unusable candidates, then os.getcwd()
    proj = Path(SB) / "user-repo"; proj.mkdir()
    child = textwrap.dedent(f"""
        import os, sys, tempfile
        os.chdir({str(proj)!r})
        sys.path.insert(0, "/home/user/cc-memory/cc_memory")
        # emulate a locked-down box: TMPDIR/TEMP/TMP and the platform dirs are all unusable
        tempfile._candidate_tempdir_list = lambda: ["/nonexistent-a", "/nonexistent-b", os.getcwd()]
        tempfile.tempdir = None
        print("gettempdir ->", tempfile.gettempdir())
        from core import markers
        d = markers.marker_dir(); print("marker_dir ->", d)
        p = markers.marker_path("cc_mem_prompt_", markers.safe_id("sess"))
        print("write_marker ->", markers.write_marker(p, "USER PROMPT TEXT"))
        print("repo listing ->", sorted(os.listdir(os.getcwd())))
        print("marker content in repo ->", markers.read_marker(p))
    """)
    r = subprocess.run([sys.executable, "-c", child], capture_output=True, encoding="utf-8", errors="replace")
    print(textwrap.indent(r.stdout + r.stderr, "    "))
    print("\nSURPRISES:", surprises or "none")
finally:
    shutil.rmtree(SB, ignore_errors=True)
