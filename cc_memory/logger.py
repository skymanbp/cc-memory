"""
cc-memory/logger.py -- Structured file logger
Replaces stderr output in all hooks. Claude Code shows stderr as error UI,
so all diagnostic output must go to log files instead.

Log files: ~/.claude/hooks/cc-memory/logs/cc-memory-YYYY-MM-DD.log
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_LOG_DIR = Path.home() / ".claude" / "hooks" / "cc-memory" / "logs"
_LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "SILENT": 4}
_DEFAULT_LEVEL = "INFO"
_MAX_LOG_DAYS = 7


def _ensure_log_dir():
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _cleanup_old_logs():
    """Remove log files older than _MAX_LOG_DAYS."""
    try:
        cutoff = datetime.now() - timedelta(days=_MAX_LOG_DAYS)
        for f in _LOG_DIR.glob("cc-memory-*.log"):
            try:
                date_str = f.stem.replace("cc-memory-", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
            except (ValueError, OSError):
                pass
    except Exception:
        pass


class Logger:
    """Structured logger that writes to daily log files, never to stderr."""

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
                    pass
            _ensure_log_dir()
            log_path = _LOG_DIR / f"cc-memory-{today}.log"
            try:
                self._file_handle = open(str(log_path), "a", encoding="utf-8")
                self._today = today
            except Exception:
                return None
            # Cleanup old logs on new day
            _cleanup_old_logs()
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
                pass

    def debug(self, msg: str):
        self._write("DEBUG", msg)

    def info(self, msg: str):
        self._write("INFO", msg)

    def warn(self, msg: str):
        self._write("WARN", msg)

    def error(self, msg: str):
        self._write("ERROR", msg)

    def error_tb(self, msg: str, exc: Exception = None):
        """Log error with optional traceback."""
        import traceback
        tb = traceback.format_exc() if exc is None else str(exc)
        self._write("ERROR", f"{msg}\n{tb}")

    def timing(self, label: str, ms: float):
        self._write("INFO", f"{label}: {ms:.1f}ms")

    def tool(self, tool_name: str, detail: str = ""):
        """Compact tool log: Edit(src/main.py), Bash(git status)"""
        self._write("DEBUG", f"{tool_name}({detail})" if detail else tool_name)

    def close(self):
        if self._file_handle:
            try:
                self._file_handle.close()
            except Exception:
                pass
            self._file_handle = None


# Module-level convenience
_loggers = {}


def get_logger(component: str, level: str = None) -> Logger:
    """Get or create a logger for the given component."""
    key = component.upper()
    if key not in _loggers:
        _loggers[key] = Logger(component, level)
    return _loggers[key]
