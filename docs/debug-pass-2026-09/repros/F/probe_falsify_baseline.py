"""F probe: falsify_fixes.py never checks the gate is GREEN on the pristine tree, so a gate that is red for an
unrelated reason makes every case against it report 'RED (detected)'."""
import os, sys, shutil, tempfile, subprocess
REPO = "/home/user/cc-memory"
SB = tempfile.mkdtemp(prefix="F-falsbase-")
for k in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[k] = SB
tempfile.tempdir = None
try:
    dst = os.path.join(SB, "cc-memory")
    shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".ccm", "memory", "*.db-shm", "*.db-wal"))
    # an UNRELATED breakage that makes the gate red before it checks anything
    for rel in ("tools/doc_claims.py", "tools/citation_check.py", "tools/doc_coverage.py"):
        p = os.path.join(dst, rel); t = open(p, encoding="utf-8").read()
        open(p, "w", encoding="utf-8", newline="").write(t.replace("\nimport argparse", "\nimport sys as _s; _s.exit(1)  # UNRELATED red: the gate is broken\nimport argparse", 1))
    for rel in ("tools/doc_claims.py", "tools/citation_check.py", "tools/doc_coverage.py"):
        r = subprocess.run([sys.executable, rel], cwd=dst, capture_output=True, encoding="utf-8", errors="replace")
        print(f"pristine-copy gate {rel}: rc={r.returncode} (red for an unrelated reason)")
    r = subprocess.run([sys.executable, "tools/falsify_fixes.py", "--case", "r8claimpy", "--case", "r12verbatim", "--case", "r11doccoverage"],
                       cwd=dst, capture_output=True, encoding="utf-8", errors="replace", timeout=600)
    print(r.stdout[-1200:]); print(r.stderr[-400:])
finally:
    shutil.rmtree(SB, ignore_errors=True)
