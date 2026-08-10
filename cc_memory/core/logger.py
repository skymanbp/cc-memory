"""
Structured file logger.

Hooks must NOT write to stderr (Claude Code shows stderr as error UI).
All diagnostic output goes to ~/.claude/hooks/cc-memory/logs/cc-memory-YYYY-MM-DD.log.

Suppression policy: every except-pass below is intentional because logger
failures must never propagate — a broken logger that crashes a hook would
block Claude Code itself (hook contract: never raise, never block).
"""
import atexit
import os
from datetime import datetime, timedelta
from pathlib import Path

_LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "SILENT": 4}
_DEFAULT_LEVEL = "INFO"
_MAX_LOG_DAYS = 7


def _log_dir():
    """Resolve the log directory lazily; None when there is no home to use.

    Path.home() RAISES RuntimeError when no home can be determined — on
    Windows with USERPROFILE and HOMEDRIVE+HOMEPATH all unset, on POSIX with
    HOME unset and no passwd entry. Binding it to a module constant therefore
    made `from core.logger import get_logger` itself a raising statement, and
    four hooks <!--ce:hooks:subset--> import it at module top level: stop,
    pre_compact, session_start and consolidate_async each exited rc=1 with a
    RuntimeError traceback on stderr — the two things the hook contract
    forbids (see the
    module docstring). user_prompt and post_tool_use survived only because
    they happen to import it lazily inside try/except.

    Returning None instead of raising lets every caller no-op, which is the
    same degradation this module already applies to an unwritable log dir.
    """
    try:
        return Path.home() / ".claude" / "hooks" / "cc-memory" / "logs"
    except Exception:
        # why: a hook with no resolvable home must still run; it just cannot
        # write a log file, which is exactly the no-op path below
        return None


def _ensure_log_dir():
    log_dir = _log_dir()
    if log_dir is None:
        return None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # why: logger init must never crash a hook; if log dir can't be created
        # (perm error / read-only fs / disk full), we silently degrade to no-op
        return None
    return log_dir


def _cleanup_old_logs(log_dir):
    try:
        cutoff = datetime.now() - timedelta(days=_MAX_LOG_DAYS)
        for f in log_dir.glob("cc-memory-*.log"):
            try:
                date_str = f.stem.replace("cc-memory-", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
            except (ValueError, OSError):
                # why: malformed filename or unlink permission denied — skip
                # this file rather than abort the cleanup sweep
                continue
    except Exception:
        # why: cleanup is housekeeping; never block actual logging on its failure
        pass


class Logger:
    def __init__(self, component: str, level: str = None):
        self.component = component.upper()
        level_name = (level or os.environ.get("CC_MEMORY_LOG_LEVEL", _DEFAULT_LEVEL)).upper()
        self._level = _LOG_LEVELS.get(level_name, 1)
        self._file_handle = None
        self._today = None

    def _get_file(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._today != today:
            if self._file_handle:
                try:
                    self._file_handle.close()
                except Exception:
                    # why: old handle close failure shouldn't stop us rotating to a new one
                    pass
            log_dir = _ensure_log_dir()
            if log_dir is None:
                # why: no resolvable home, or an uncreatable log dir — the same
                # no-op every other failure path here takes, never a raise
                return None
            log_path = log_dir / f"cc-memory-{today}.log"
            try:
                self._file_handle = open(str(log_path), "a", encoding="utf-8", errors="replace")
                self._today = today
            except Exception:
                # why: open() failure (perm/disk) — return None so callers no-op
                # instead of raising into the hook entry point
                return None
            _cleanup_old_logs(log_dir)
        return self._file_handle

    def _write(self, level: str, msg: str):
        if _LOG_LEVELS.get(level, 0) < self._level:
            return
        ts = datetime.now().strftime("%H:%M:%S.%f")[:12]
        line = f"[{ts}] [{level}] [{self.component}] {msg}\n"
        fh = self._get_file()
        if fh:
            try:
                fh.write(line)
                fh.flush()
            except Exception:
                # why: write failure (disk full / handle invalidated by external
                # rotation) — drop the line silently per hook-safety contract
                pass

    def debug(self, msg):  self._write("DEBUG", msg)
    def info(self, msg):   self._write("INFO", msg)
    def warn(self, msg):   self._write("WARN", msg)
    def error(self, msg):  self._write("ERROR", msg)

    def error_tb(self, msg: str, exc: Exception = None):
        import traceback
        tb = traceback.format_exc() if exc is None else str(exc)
        self._write("ERROR", f"{msg}\n{tb}")

    def tool(self, tool_name: str, detail: str = ""):
        self._write("DEBUG", f"{tool_name}({detail})" if detail else tool_name)

    def close(self):
        """Release the day-log handle. The logger stays USABLE afterwards.

        Resetting `_today` is load-bearing, not tidiness. `_get_file()` reopens
        only when `self._today != today`; close() used to clear `_file_handle`
        while LEAVING `_today` set, so the very next `_write()` took the
        "same day, nothing to do" branch, got `None` back, and silently dropped
        that line — and every later line — for the rest of the process. That
        made close() a one-way kill switch, which is why it was never safe to
        call from the package and stayed dead code. With `_today = None` the
        next write reopens the file, so close() is now a release, not a kill.
        """
        if self._file_handle:
            try:
                self._file_handle.close()
            except Exception:
                # why: close on already-broken handle; nothing we can do, drop it
                pass
            self._file_handle = None
        self._today = None


_loggers = {}


def get_logger(component: str, level: str = None) -> Logger:
    key = component.upper()
    if key not in _loggers:
        _loggers[key] = Logger(component, level)
    return _loggers[key]


def close_all_loggers():
    """Close every cached Logger's file handle. Safe to call at any time.

    `_loggers` caches one Logger per component for the life of the process and
    each keeps an OPEN append handle on today's log file. That is bounded (one
    handle per component, rotated on date change), NOT the per-operation leak
    `core.db._connect` had — but it is still a handle held for the whole run,
    and on Windows an open handle blocks deletion of the file. The short-lived
    hooks release theirs at process exit; the long-lived surfaces
    (`ui/dashboard.py`, `ui/web_viewer.py`, `mcp/server.py`) should call this on
    shutdown. Both test suites already sweep `_loggers` by hand for exactly this
    reason — this is that sweep, owned by the module that owns the cache.

    Idempotent, and NOT terminal: after this call the next log line reopens the
    file (see Logger.close).
    """
    for lg in list(_loggers.values()):
        try:
            lg.close()
        except Exception:
            # why: teardown must never raise — this runs from atexit below, and
            # an escaping exception there is printed to STDERR, which Claude
            # Code renders as hook error UI (hook contract: never stderr)
            pass


# why: makes the release above actually happen on every exit path a hook can
# take (`sys.exit(0)` runs atexit handlers), instead of leaving it to
# interpreter teardown. Fully swallowed inside close_all_loggers, so it can
# never emit a traceback on stderr.
atexit.register(close_all_loggers)
