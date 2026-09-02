"""R1: the home boundary is env-spelled/structural only; a resolved chain that
spells home differently (symlinked /home parent, WSL /mnt/c/Users/x) walks INTO
the home directory and adopts a home database."""
import os, sys, tempfile, shutil
from pathlib import Path
SB = tempfile.mkdtemp(prefix="A_r1_")
for v in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
    os.environ[v] = SB
tempfile.tempdir = SB
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
try:
    sb = Path(SB)
    # --- case 1: /home is a symlink to another volume (Linux/macOS) ---------
    real_home_parent = sb / "vol" / "home"          # the real directory
    (real_home_parent / "alice").mkdir(parents=True)
    (sb / "home").symlink_to(real_home_parent, target_is_directory=True)
    home = sb / "home" / "alice"                    # spelled THROUGH the link
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
    (home / ".ccm").mkdir()
    (home / ".ccm" / "memory.db").write_bytes(b"")  # a home database
    cwd = home / "proj" / "src"                     # uninitialised project
    cwd.mkdir(parents=True)
    from core import roots
    print("Path.home()      :", Path.home())
    print("cwd (as given)   :", cwd)
    print("cwd.resolve()    :", cwd.resolve())
    root = roots.project_root(str(cwd))
    print("project_root     :", root)
    print("-> is the HOME directory:", Path(root).resolve() == home.resolve())

    # --- case 2: WSL-shaped mount of a Windows profile ----------------------
    win_home = sb / "mnt" / "c" / "Users" / "bob"
    (win_home / "memory").mkdir(parents=True)
    (win_home / "memory" / "memory.db").write_bytes(b"")   # the documented C:\Users\<user>\memory\memory.db
    (win_home / "Documents").mkdir(); (win_home / "Desktop").mkdir()
    cwd2 = win_home / "Projects" / "foo" / "src"
    cwd2.mkdir(parents=True)
    print()
    print("_is_profile_dir(/mnt/c/Users/bob):", roots._is_profile_dir(win_home))
    root2 = roots.project_root(str(cwd2))
    print("project_root(", cwd2.relative_to(sb), ") ->", Path(root2).relative_to(sb))
    print("-> is the Windows profile dir:", Path(root2) == win_home)
finally:
    shutil.rmtree(SB, ignore_errors=True)
