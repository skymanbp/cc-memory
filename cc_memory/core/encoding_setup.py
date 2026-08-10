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

stdin is covered too (v2.5.0). The MCP server reads JSON-RPC frames from
stdin; under the locale codec (gbk here) a non-ASCII request raised
UnicodeDecodeError *inside the line iterator*, outside the per-request try,
and killed the process with no response.

Line buffering is part of the contract as well (v2.5.0). Without
`line_buffering=True`, reconfigure leaves stdout block-buffered on a pipe —
which is what every hook writes to. A SessionStart killed at its 15 s timeout
lost 100% of a 5069 B injection even though every print() had already run,
because nothing had been flushed. stdin is a READ stream and must NOT get
line_buffering (reconfigure rejects it there).

Usage
-----
Each entrypoint imports and invokes this BEFORE any other code that reads
stdin or writes to stdout/stderr:

    from core.encoding_setup import enable_utf8_io
    enable_utf8_io()

It's an explicit function call (not a side-effect import) so the intent is
obvious to anyone reading the file.
"""
import sys


def enable_utf8_io() -> None:
    """Reconfigure stdout + stderr + stdin to UTF-8 with replacement on error.

    stdout/stderr additionally become line-buffered so a killed hook still
    delivers what it already printed.

    Idempotent and safe to call from any entry point. Failures are silent
    by design — falling back to the default encoding is strictly better
    than raising during hook startup.
    """
    for stream_name in ("stdout", "stderr", "stdin"):
        stream = getattr(sys, stream_name, None)
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            # why: reconfigure() is a TextIOWrapper method added in Python
            # 3.7; on older interpreters or non-TextIOWrapper streams (e.g.
            # captured by a test harness, or None under a --windowed exe),
            # there is nothing to reconfigure and the calling site must
            # accept the host encoding
            continue
        kwargs = {"encoding": "utf-8", "errors": "replace"}
        if stream_name != "stdin":
            # why: line_buffering is a write-side option; passing it to a read
            # stream raises, and stdin needs no flushing
            kwargs["line_buffering"] = True
        try:
            reconf(**kwargs)
        except (ValueError, OSError, TypeError):
            # why: reconfigure may fail when the stream is already wrapped
            # by an unflushable buffer, is detached, or (for stdin) has
            # already had data read from it; we silently keep the current
            # encoding rather than crash the hook process at start.
            # TypeError (register Y8): a stream whose reconfigure() takes
            # no keyword arguments — test doubles and exotic wrappers — used
            # to raise straight through the docstring's "safe to call from
            # any entry point" promise. Each stream is tried independently,
            # so one narrow stream cannot cost the other two their UTF-8.
            pass
