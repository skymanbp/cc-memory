"""R33: a project whose OWN directory name is in _DEPENDENCY_DIRS is filtered out
of the candidate chain — even when pinned with .ccm-root, even when it already
owns .ccm/memory.db — so every subdirectory cwd resolves to itself."""
import os, sys, tempfile, shutil
from pathlib import Path
SB = tempfile.mkdtemp(prefix="A_r33_")
for v in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[v] = SB
tempfile.tempdir = SB
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
try:
    from core import roots
    home = Path(SB) / "home" / "alice"; home.mkdir(parents=True)
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
    for name in ("external", "vendor", "deps"):
        proj = home / "Projects" / name
        (proj / ".git").mkdir(parents=True)                 # a real repository
        (proj / ".ccm-root").write_text("")                 # AND explicitly pinned
        (proj / ".ccm").mkdir(); (proj / ".ccm" / "memory.db").write_bytes(b"")  # AND already initialised
        cwd = proj / "src" / "pkg"; cwd.mkdir(parents=True)
        root = roots.project_root(str(cwd))
        print(f"project {name!r:11} cwd=.../{cwd.relative_to(home)}  ->  root=.../{Path(root).relative_to(home)}"
              f"   {'STRAY (resolved to cwd itself)' if Path(root) == cwd else 'ok'}")
    # control: same layout, ordinary name
    proj = home / "Projects" / "widgets"; (proj / ".git").mkdir(parents=True); (proj / ".ccm").mkdir(); (proj/".ccm"/"memory.db").write_bytes(b"")
    cwd = proj / "src" / "pkg"; cwd.mkdir(parents=True)
    print(f"project {'widgets'!r:11} cwd=.../{cwd.relative_to(home)}  ->  root=.../{Path(roots.project_root(str(cwd))).relative_to(home)}   ok")
finally:
    shutil.rmtree(SB, ignore_errors=True)
