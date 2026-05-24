"""
Force stdout/stderr to UTF-8 — call this BEFORE any print().

Why this exists
---------------
Hook scripts spawned by Claude Code on Windows inherit a gbk-encoded stdio
(cp936 locale on zh-CN installs). When SessionStart prints the injected
context — which includes memory content that can contain ANY unicode glyph
(emoji from project READMEs, the ↻ supersede marker we emit ourselves, math
symbols in scientific projects, etc.) — Python raises UnicodeEncodeError on
characters that don't fit in gbk, and the hook crashes.

A crashed hook is a Claude Code lifecycle bomb: PreCompact / SessionStart /
Stop must always exit 0 or context handoff gets skipped. Per the hook
contract in CLAUDE.md, that is unacceptable.

The fix is `sys.stdout.reconfigure(encoding='utf-8', errors='replace')`,
which Python 3.7+ exposes on TextIOWrapper. `errors='replace'` is the
critical clause: if reconfigure somehow ends up on a stream that still
can't encode a particular byte, the offending character becomes `?`
instead of crashing the hook.

Usage
-----
Each hook entrypoint imports and invokes this BEFORE any other code that
might write to stdout/stderr:

    from core.encoding_setup import enable_utf8_io
    enable_utf8_io()

It's an explicit function call (not a side-effect import) so the intent is
obvious to anyone reading the file.
"""
import sys


def enable_utf8_io() -> None:
    """Reconfigure stdout + stderr to UTF-8 with replacement on error.

    Idempotent and safe to call from any entry point. Failures are silent
    by design — falling back to the default encoding is strictly better
    than raising during hook startup.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            # why: reconfigure() is a TextIOWrapper method added in Python
            # 3.7; on older interpreters or non-TextIOWrapper streams (e.g.
            # captured by a test harness), there is nothing to reconfigure
            # and the calling site must accept the host encoding
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # why: reconfigure may fail when the stream is already wrapped
            # by an unflushable buffer or is detached; we silently keep the
            # current encoding rather than crash the hook process at start
            pass
