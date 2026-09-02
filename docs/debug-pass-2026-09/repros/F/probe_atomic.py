"""F probe: core.atomic.write_atomic contract (replace completely or raise; never truncate)."""
import os, sys, io, time, shutil, tempfile, threading, subprocess, textwrap
SB = tempfile.mkdtemp(prefix="F-atomic-")
for k in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[k] = SB
tempfile.tempdir = None
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
surprises = []
def say(label, got, want=None):
    flag = "" if want is None or got == want else "   <-- UNEXPECTED (wanted %r)" % (want,)
    if flag: surprises.append(label)
    print(f"  {label:<64} -> {got!r}{flag}")
def leftovers(d): return sorted(n for n in os.listdir(d) if n.endswith(".tmp"))
try:
    from core import atomic
    from pathlib import Path
    W = Path(SB) / "w"; W.mkdir()
    print("== happy path")
    atomic.write_atomic(W / "a.md", "one\ntwo\n"); say("content", (W / "a.md").read_bytes(), b"one\ntwo\n")
    say("no tmp leftovers", leftovers(W), [])
    print("== destination is a directory")
    (W / "isdir").mkdir()
    try: atomic.write_atomic(W / "isdir", "x"); say("raised", False, True)
    except OSError as e: say("raised OSError", type(e).__name__)
    say("no tmp leftovers", leftovers(W), [])
    print("== parent missing")
    try: atomic.write_atomic(W / "nope" / "f.md", "x"); say("raised", False, True)
    except OSError as e: say("raised OSError", type(e).__name__)
    print("== parent not writable (as uid 54321)")
    ro = Path(SB) / "ro"; ro.mkdir(); os.chmod(ro, 0o755); (ro / "f.md").write_text("OLD")
    os.chmod(SB, 0o755)
    child = textwrap.dedent(f"""
        import os, sys; os.setgid(54321); os.setuid(54321)
        sys.path.insert(0, "/home/user/cc-memory/cc_memory")
        from core.atomic import write_atomic
        try:
            write_atomic({str(ro / 'f.md')!r}, "NEW"); print("no raise")
        except OSError as e: print("raised", type(e).__name__)
    """)
    r = subprocess.run([sys.executable, "-c", child], capture_output=True, encoding="utf-8", errors="replace")
    say("child says", (r.stdout + r.stderr).strip()[:80]); say("old content intact", (ro / "f.md").read_text(), "OLD")
    print("== retry loop terminates and raises (os.replace always PermissionError)")
    real = os.replace
    os.replace = lambda a, b: (_ for _ in ()).throw(PermissionError("sharing violation"))
    try:
        t0 = time.monotonic()
        try: atomic.write_atomic(W / "b.md", "x"); say("fixed-count: raised", False, True)
        except PermissionError: say("fixed-count: raised PermissionError after %.3fs" % (time.monotonic() - t0), True)
        t0 = time.monotonic()
        try: atomic.write_atomic(W / "b.md", "x", budget_s=0.3); say("budget: raised", False, True)
        except PermissionError: el = time.monotonic() - t0; say("budget 0.3s: raised after ~0.3s", 0.28 < el < 0.6, True)
        say("no tmp leftovers after raise", leftovers(W), [])
    finally:
        os.replace = real
    print("== concurrency: 16 writers, one path")
    def w(i): atomic.write_atomic(W / "c.md", f"writer-{i}\n" * 2000)
    th = [threading.Thread(target=w, args=(i,)) for i in range(16)]; [t.start() for t in th]; [t.join() for t in th]
    txt = (W / "c.md").read_text(); first = txt.splitlines()[0]
    say("final file is ONE complete writer's text", set(txt.splitlines()) == {first} and txt.count("\n") == 2000, True)
    say("no tmp leftovers", leftovers(W), [])
    print("== reader holding the file open (POSIX)")
    fh = open(W / "a.md"); atomic.write_atomic(W / "a.md", "replaced\n"); say("replaced under an open reader", (W / "a.md").read_text(), "replaced\n"); fh.close()
    print("== fsync present")
    say("fsync called in source", "os.fsync" in Path(atomic.__file__).read_text(), True)
    print("== newline handling: the writer opens text mode with newline=None (platform translation)")
    src = Path(atomic.__file__).read_text()
    say("newline= passed to os.fdopen", "newline" in src.split("def write_atomic")[1].split("fsync")[0], False)
    # Emulate CPython's Windows write-side rule (newline=None => writenl "\\r\\n", translate on) with newline="\\r\\n"
    buf = io.BytesIO(); tw = io.TextIOWrapper(buf, encoding="utf-8", newline="\r\n"); tw.write("a\r\nb\n"); tw.flush()
    say("Windows emulation: 'a\\r\\nb\\n' lands on disk as", buf.getvalue(), b"a\r\nb\n")
    print("\nSURPRISES:", surprises or "none")
finally:
    shutil.rmtree(SB, ignore_errors=True)
