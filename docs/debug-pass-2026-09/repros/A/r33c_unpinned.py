"""R33 variant: a project living UNDER a folder whose name is in _DEPENDENCY_DIRS
(~/work/external/<client>, D:\\Projects\\vendor\\<lib>) — initialised, NOT pinned —
still resolves every subdirectory cwd to itself."""
import os, sys, tempfile, shutil
from pathlib import Path
SB = tempfile.mkdtemp(prefix="A_r33b_")
for v in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[v] = SB
tempfile.tempdir = SB
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
try:
    from core import roots
    home = Path(SB) / "home" / "alice"; home.mkdir(parents=True)
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
    for folder in ("external", "vendor", "deps", "third_party"):
        proj = home / "work" / folder / "clientproj"
        (proj / ".git").mkdir(parents=True); pass
        (proj / ".ccm").mkdir(); (proj / ".ccm" / "memory.db").write_bytes(b"")
        cwd = proj / "src"; cwd.mkdir()
        root = Path(roots.project_root(str(cwd)))
        print(f"~/work/{folder:12}/clientproj/src -> {root.relative_to(home)}   "
              f"{'STRAY: cwd itself (pin + own db ignored)' if root == cwd else 'ok'}")
    print("chain seen by the rungs for the last one:",
          [p.name for p in roots._candidates(roots._chain(cwd.resolve()))])
finally:
    shutil.rmtree(SB, ignore_errors=True)
