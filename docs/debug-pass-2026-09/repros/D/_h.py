"""Reviewer-D harness. Call make_sandbox() FIRST, before importing the package."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path("/home/user/cc-memory")
PKG = REPO / "cc_memory"
MEM = PKG / "cli" / "mem.py"
PLAN = PKG / "cli" / "plan.py"
MCP = PKG / "mcp" / "server.py"
SCRATCH = Path(__file__).resolve().parent

_SB = None


def make_sandbox():
    """Fresh sandbox; redirects HOME/USERPROFILE/TMPDIR/TEMP/TMP into it."""
    global _SB
    sb = Path(tempfile.mkdtemp(prefix="revD_", dir=str(SCRATCH)))
    home = sb / "home"
    tmp = sb / "tmp"
    home.mkdir()
    tmp.mkdir()
    for k in ("HOME", "USERPROFILE"):
        os.environ[k] = str(home)
    for k in ("TMPDIR", "TEMP", "TMP"):
        os.environ[k] = str(tmp)
    os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    assert Path.home() == home, Path.home()
    _SB = sb
    return sb


def destroy_sandbox():
    if _SB is not None and _SB.exists():
        shutil.rmtree(_SB, ignore_errors=True)


def env():
    e = dict(os.environ)
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    return e


def run(cmd, cwd=None, stdin=None, timeout=120, extra_env=None):
    e = env()
    if extra_env:
        e.update(extra_env)
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, input=stdin,
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=e)
    return p.returncode, p.stdout, p.stderr


def mem(args, project, cwd=None, stdin=None, extra_env=None):
    return run([sys.executable, str(MEM), "--project", str(project)] + list(args),
               cwd=cwd, stdin=stdin, extra_env=extra_env)


def plan(args, project, cwd=None, stdin=None):
    return run([sys.executable, str(PLAN), "--project", str(project)] + list(args),
               cwd=cwd, stdin=stdin)


def pkg_imports():
    """Import the package in-process (after make_sandbox)."""
    if str(PKG) not in sys.path:
        sys.path.insert(0, str(PKG))
    from core.db import MemoryDB          # noqa
    from llm.memory_writer import upsert_smart, upsert_batch  # noqa
    from core.progress import ensure_memory_dir  # noqa
    return MemoryDB, upsert_smart, upsert_batch, ensure_memory_dir


def seed_project(path, memories=(), name_hint=None):
    """Create <path>/.ccm/memory.db with a project row + memories. Returns (db, pid)."""
    MemoryDB, upsert_smart, _, ensure_memory_dir = pkg_imports()
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    mdir = path / ".ccm"
    ensure_memory_dir(mdir)
    db = MemoryDB(mdir / "memory.db")
    pid = db.upsert_project(str(path.resolve()))
    ids = []
    for m in memories:
        r = upsert_smart(db, pid, None, category=m.get("category", "decision"),
                         content=m["content"], importance=m.get("importance", 3),
                         tags=m.get("tags", ["manual"]), topic=m.get("topic", ""))
        ids.append(r.get("id"))
    return db, pid, ids


def show(label, rc, out, err, maxlen=1500):
    print(f"--- {label}: rc={rc}")
    if out.strip():
        print("  stdout: " + out.strip()[:maxlen].replace("\n", "\n          "))
    if err.strip():
        print("  STDERR: " + err.strip()[:maxlen].replace("\n", "\n          "))


def is_traceback(err):
    return "Traceback (most recent call last)" in err
