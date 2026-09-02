"""Sandbox bootstrap: redirect HOME/USERPROFILE/TMPDIR/TEMP/TMP to a fresh
temp dir BEFORE the package is imported, put cc_memory on sys.path, and give
scripts a leave() that removes the sandbox. Import this FIRST."""
import os, shutil, sys, tempfile
from pathlib import Path

_ROOT = None
PKG = "/home/user/cc-memory/cc_memory"


def enter():
    global _ROOT
    _ROOT = Path(tempfile.mkdtemp(prefix="ccm-revC-"))
    for var in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
        os.environ[var] = str(_ROOT)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    tempfile.tempdir = None  # force re-evaluation from the new TMPDIR
    if PKG not in sys.path:
        sys.path.insert(0, PKG)
    assert Path.home() == _ROOT, (Path.home(), _ROOT)
    assert Path(tempfile.gettempdir()) == _ROOT, tempfile.gettempdir()
    return _ROOT


def project(name="proj"):
    """A fresh project directory with an initialised .ccm/ state dir."""
    p = _ROOT / name
    (p / ".ccm").mkdir(parents=True)
    return p


def logfile_text():
    logs = _ROOT / ".claude" / "hooks" / "cc-memory" / "logs"
    if not logs.exists():
        return ""
    return "\n".join(f.read_text(encoding="utf-8", errors="replace")
                     for f in sorted(logs.glob("*.log")))


def leave():
    try:
        from core.logger import close_all_loggers
        close_all_loggers()
    except Exception:
        pass
    import gc
    gc.collect()
    shutil.rmtree(_ROOT, ignore_errors=False)
    assert not _ROOT.exists(), f"sandbox leak: {_ROOT}"
