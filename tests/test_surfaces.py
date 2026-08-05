# -*- coding: utf-8 -*-
"""Process-level surface tests: MCP stdio server, web viewer, installer, hooks.

Run:  python tests/test_surfaces.py

These surfaces are the ones the other release gates miss entirely:
`tests/smoke_test.py` never spawns `cc_memory/mcp/server.py`, never binds a
socket, only ever inspects the installer's hooks-config *dict* -- it never
parses a settings.json -- and never runs a hook as a process. So none of the
~30 v2.5.0 fixes to those files is defended by a test today. This file is
that defence.

  §1 MCP        real subprocess, framed JSON-RPC over binary stdio pipes
  §2 web viewer real ephemeral-port server, RAW sockets (http.client cannot
                express a malformed or drip-fed request)
  §3 installer  settings.json shape matrix, install AND uninstall, in-process
                with every module path constant redirected into a sandbox
  §4 hooks      `excluded_projects`, the only opt-out, driven through all SIX
                hook entry points as real subprocesses against a COPY of the
                package whose config.json names the fixture

Hermetic by construction: HOME / USERPROFILE / HOMEDRIVE / HOMEPATH / TEMP /
TMP / TMPDIR *and* `tempfile.tempdir` are redirected into one sandbox root
BEFORE cc_memory is imported, because `core.logger` and `ui.installer` both
resolve `Path.home()` at import time. The real ~/.claude is unreachable for the
whole run (asserted below), and the uninstall temp sweep can only ever see the
sandbox. The sandbox is REMOVED at the end and a leak it cannot clean is a
test failure -- see _cleanup_sandbox.

Framing note (MCP): ONE reader thread over ONE buffer, frames split only on
b"\\n". A fresh reader per readline() splits frames across readers and
manufactures phantom "torn stdout" that has fooled an auditor before.

DELIBERATELY NOT COVERED -- stated rather than silently dropped:
  * Thread-count during the drip attacks. `(Get-Process).Threads.Count` is
    Windows-only and its baseline is noisy (the fix report measured delta 0..2
    where 2 was sampling noise). A liveness GET fired *during* the drip is
    asserted instead: it proves the worker pool is not exhausted without
    depending on an OS-specific counter.
  * Real DNS rebinding and real browser rendering. Host/Origin are forged at
    the socket -- byte-for-byte what a rebound browser sends -- but no DNS is
    controlled and no DOM is rendered, so the SPA's XSS story is out of scope.
  * `ui/dashboard.py` (Tk) has no coverage here; it needs a display server.
  * The frozen-exe installer layout (`sys._MEIPASS`) is not exercised -- every
    run is as-script.
  * `installer._detect_python_cmd()` is stubbed for the matrix: it shells out
    per install, and the interpreter name is orthogonal to every shape being
    tested (`_CCM_COMMAND_RE` matches on the script path, not the interpreter).
  * MCP `MemoryError` on a huge frame is not reproduced -- only the 1 MiB
    frame CAP is asserted. Trying to OOM the box is not a test.
  * §4 runs each hook DIRECTLY. Whether Claude Code would have invoked it at
    all (hooks/hooks.json wiring, `enabledPlugins`) is smoke_test's job; §4
    asserts only what a hook does once it is handed a cwd.
"""
from __future__ import annotations

import contextlib
import gc
import hashlib
import io
import json
import os
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ── sandbox: must be installed BEFORE importing anything from cc_memory ─────
_SANDBOX = Path(tempfile.mkdtemp(prefix="cc-memory-surfaces-"))
_HOME = _SANDBOX / "home"
_TMP = _SANDBOX / "tmp"
_HOME.mkdir(parents=True, exist_ok=True)
_TMP.mkdir(parents=True, exist_ok=True)
_drive, _rest = os.path.splitdrive(str(_HOME))
os.environ.update({
    "USERPROFILE": str(_HOME),      # ntpath.expanduser checks this first
    "HOME": str(_HOME),             # posixpath.expanduser
    "HOMEDRIVE": _drive or "",
    "HOMEPATH": _rest or str(_HOME),
    "TEMP": str(_TMP),
    "TMP": str(_TMP),
    "TMPDIR": str(_TMP),
})
# env alone is not enough in-process: tempfile caches gettempdir() on first use,
# and mkdtemp() above already primed it with the REAL temp dir.
tempfile.tempdir = str(_TMP)
assert Path.home() == _HOME, (
    f"sandbox home not in effect (Path.home()={Path.home()}); refusing to run "
    f"against the real ~/.claude")


def _cleanup_sandbox():
    """Close every sqlite handle this process opened, then REMOVE the sandbox.

    Every connection alive in this process was opened by this suite, so closing
    them all is exactly "close your own handles". It is needed because
    `MemoryDB._connect()` is consumed as `with self._connect() as conn:`
    throughout the package and sqlite3's context manager COMMITS BUT DOES NOT
    CLOSE; the connection then survives inside its own statement-cache
    reference cycle and keeps memory.db open, which on Windows is a hard
    PermissionError [WinError 32] on rmtree.

    Measured before this existed: every SUCCESSFUL run left one
    `%TEMP%\\cc-memory-surfaces-*\\tmp\\ccm-web-served-*\\memory\\memory.db`
    of 475,136 B in the real %TEMP%, forever, because the teardown was
    `shutil.rmtree(_SANDBOX, ignore_errors=True)`. A leak this cannot clean is
    now REPORTED as a failure rather than swallowed.
    """
    for _conn in [o for o in gc.get_objects()
                  if isinstance(o, sqlite3.Connection)]:
        try:
            _conn.close()
        except sqlite3.Error:
            # why: an already-closed or mid-statement handle. The only goal is
            # releasing the OS file handle before rmtree, and one we cannot
            # release is caught by the rmtree check below anyway.
            pass
    gc.collect()          # breaks the statement-cache cycles described above
    # core.logger caches Logger objects in a module-level dict and each keeps an
    # OPEN append handle on <home>/.claude/hooks/cc-memory/logs/cc-memory-*.log
    # for the life of the process. That path is inside the sandbox home, so it
    # blocks rmtree before it ever reaches the databases.
    try:
        from core import logger as _logger_mod
        for _lg in list(getattr(_logger_mod, "_loggers", {}).values()):
            _lg.close()
    except ImportError:
        # why: teardown must work even if the package never became importable
        # (a failure during bootstrap) -- there is then nothing to close
        pass
    tempfile.tempdir = None
    try:
        shutil.rmtree(_SANDBOX)
    except OSError as exc:
        left = sorted(str(p) for p in _SANDBOX.rglob("*") if p.is_file())
        raise AssertionError(
            f"sandbox {_SANDBOX} survived cleanup ({exc}); {len(left)} file(s) "
            f"leaked into the real %TEMP%: {left[:10]}")


sys.path.insert(0, str(REPO / "cc_memory"))

from core.encoding_setup import enable_utf8_io     # noqa: E402  -- why: imports must follow the sandbox + sys.path bootstrap above; repo tests run as plain scripts
# Explicitly, BEFORE the first output line. This file's own section headers are
# non-ASCII (§) and used to survive only because `from mcp import server` below
# calls enable_utf8_io() at ITS module scope -- an import reorder would have
# silently turned every header into locale-codec mojibake. smoke_test.py pins
# this ordering for all three suites.
enable_utf8_io()

from core.db import MemoryDB                       # noqa: E402  -- why: same bootstrap ordering
from llm.memory_writer import (                    # noqa: E402  -- why: same bootstrap ordering
    MIN_CONTENT_LEN, regenerate_memory_index)
from mcp import server as mcp_server               # noqa: E402  -- why: same bootstrap ordering
from ui import installer as inst                   # noqa: E402  -- why: same bootstrap ordering

MCP_SERVER_PY = REPO / "cc_memory" / "mcp" / "server.py"
WEB_VIEWER_PY = REPO / "cc_memory" / "ui" / "web_viewer.py"


# ═══════════════════════════════════════════════════════════════════════════
# shared helpers
# ═══════════════════════════════════════════════════════════════════════════

def _mk_project(tag):
    """Throwaway project with memory/memory.db. Returns (root, mem_dir, db, pid)."""
    root = Path(tempfile.mkdtemp(prefix=f"ccm-{tag}-"))
    mem = root / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    db = MemoryDB(mem / "memory.db")
    pid = db.upsert_project(str(root))
    return root, mem, db, pid


def _strict_loads(text):
    """RFC 8259 parser. json's own defaults accept NaN/Infinity; a conforming
    client (JS JSON.parse, Go, serde) does not, so anything this rejects is a
    frame a real MCP client would refuse."""
    def _reject_constant(name):
        raise AssertionError(f"non-RFC-8259 constant {name!r} on the wire")

    def _reject_nonfinite(raw):
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            raise AssertionError(f"non-finite float {raw!r} on the wire")
        return value

    return json.loads(text, parse_constant=_reject_constant,
                      parse_float=_reject_nonfinite)


# ═══════════════════════════════════════════════════════════════════════════
# §1  MCP stdio server
# ═══════════════════════════════════════════════════════════════════════════

class McpProc:
    """MCP server subprocess with ONE reader thread over ONE buffer.

    Binary pipes, bufsize=0, frames split ONLY on b"\\n". os.read() (not
    BufferedReader.read) so a partial frame is returned as soon as it arrives
    instead of blocking for a full buffer.
    """

    def __init__(self, cwd, env_extra=None):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        env.pop("PYTHONUTF8", None)
        if env_extra:
            env.update(env_extra)
        self.proc = subprocess.Popen(
            [sys.executable, str(MCP_SERVER_PY)], cwd=str(cwd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, bufsize=0)
        self.frames = []                 # list[bytes], one per LF-terminated line
        self.stderr_bytes = b""
        self._lock = threading.Lock()
        self._t_out = threading.Thread(target=self._pump_stdout, daemon=True)
        self._t_err = threading.Thread(target=self._pump_stderr, daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _pump_stdout(self):
        buf = b""
        fd = self.proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                # why: pipe torn down by process exit; treat exactly like EOF
                data = b""
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                with self._lock:
                    self.frames.append(line)
        if buf.strip():
            with self._lock:
                self.frames.append(buf)

    def _pump_stderr(self):
        fd = self.proc.stderr.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                # why: pipe torn down by process exit; treat exactly like EOF
                data = b""
            if not data:
                break
            with self._lock:
                self.stderr_bytes += data

    # ── wire ────────────────────────────────────────────────────────────────

    def send_raw(self, payload: bytes):
        data = payload + b"\n"
        while data:
            n = self.proc.stdin.write(data)
            if not n:
                raise AssertionError("MCP stdin write made no progress")
            data = data[n:]
        self.proc.stdin.flush()

    def send(self, obj):
        self.send_raw(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def n_frames(self):
        with self._lock:
            return len(self.frames)

    def wait_frames(self, n, timeout=25.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if len(self.frames) >= n:
                    return list(self.frames)
            time.sleep(0.02)
        with self._lock:
            raise AssertionError(
                f"timed out waiting for {n} MCP frames, got {len(self.frames)}"
                f" (last: {self.frames[-2:]})")

    def settle(self, quiet=0.8, timeout=30.0):
        """All frames once none has arrived for `quiet` seconds."""
        end = time.monotonic() + timeout
        last, stable_since = -1, time.monotonic()
        while time.monotonic() < end:
            n = self.n_frames()
            if n != last:
                last, stable_since = n, time.monotonic()
            elif time.monotonic() - stable_since >= quiet:
                break
            time.sleep(0.05)
        with self._lock:
            return list(self.frames)

    def finish(self, timeout=20.0):
        """Close stdin, reap, and return (rc, stderr_bytes, decoded_frames)."""
        try:
            self.proc.stdin.close()
        except OSError:
            # why: peer already gone; there is nothing left to close politely
            pass
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
            raise AssertionError("MCP server did not exit on stdin EOF")
        self._t_out.join(5)
        self._t_err.join(5)
        with self._lock:
            raw_frames = list(self.frames)
            err = self.stderr_bytes
        decoded = []
        for i, line in enumerate(raw_frames):
            assert not line.endswith(b"\r"), \
                f"frame {i} ends with CR -- stdout newline translation is back"
            # strict utf-8: a decode error here IS the gbk-mangling regression
            decoded.append(line.decode("utf-8"))
        for text in decoded:
            _strict_loads(text)
        return rc, err, decoded


def _by_id(frames):
    """{id: message} for frames carrying a non-null id; asserts no id twice."""
    out = {}
    for text in frames:
        msg = _strict_loads(text)
        rid = msg.get("id")
        if rid is None:
            continue
        assert rid not in out, f"id {rid!r} answered twice: {out[rid]} then {msg}"
        out[rid] = msg
    return out


def _call(mcp, rid, tool, args):
    mcp.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
              "params": {"name": tool, "arguments": args}})


def test_mcp():
    print("\n--- §1 MCP stdio server -------------------------------------")
    root, mem, db, pid = _mk_project("mcp")

    # A supersede chain + an archived row: both must be invisible to reads.
    m_old = db.insert_memory(pid, None, "note",
                             "alpha original note about the widget pipeline",
                             3, [], "alpha")
    m_arch = db.insert_memory(pid, None, "note",
                              "bravo note that gets archived by hand",
                              3, [], "bravo")
    m_live = db.insert_memory(pid, None, "note",
                              "charlie note that stays active throughout",
                              3, [], "charlie")
    m_new = db.supersede_memory(m_old, "alpha revised note about the widget pipeline",
                                pid, None, "note", 3, [], "alpha")
    db.archive_memory(m_arch)

    # Non-ASCII row seeded through the DB so the OUT direction is byte-exact by
    # construction (no writer normalisation in the way).
    unicode_out = "回归测试 ↻ supersede marker 🔥 ∀x∈ℝ ő Привет"
    m_uni = db.insert_memory(pid, None, "note",
                             "OUTBOUND " + unicode_out, 3, [], "unicode")

    # ── 1a. handshake + tools/list == the handler table ────────────────────
    mcp = McpProc(root)
    mcp.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18"}})
    mcp.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    got = _by_id(mcp.wait_frames(2))
    assert got[1]["result"]["serverInfo"]["name"] == "cc-memory", got[1]
    assert got[1]["result"]["protocolVersion"] == "2025-06-18", \
        f"a SUPPORTED protocolVersion was not honoured: {got[1]}"
    listed = sorted(t["name"] for t in got[2]["result"]["tools"])
    assert listed == sorted(mcp_server._HANDLERS), \
        f"tools/list {listed} != handler table {sorted(mcp_server._HANDLERS)}"
    assert listed == sorted(t["name"] for t in mcp_server.TOOLS)

    # Echoing a SUPPORTED version cannot tell negotiating from parroting: both
    # return the string just sent. `_negotiate_protocol` exists to REFUSE what
    # this server does not implement, so probe that branch directly -- an
    # unsupported string AND a non-string must both come back as
    # _PROTOCOL_DEFAULT. Without these two, mutating the guard to
    # `if isinstance(requested, str): return requested` still passed, i.e. a
    # client asking for a version the server cannot speak was told "yes".
    assert "1999-01-01" not in mcp_server._PROTOCOL_SUPPORTED, "fixture check"
    assert mcp_server._PROTOCOL_DEFAULT != "2025-06-18", \
        ("the supported-version probe above must ask for something OTHER than "
         "the fallback, or the two branches are indistinguishable again")
    mcp.send({"jsonrpc": "2.0", "id": 3, "method": "initialize",
              "params": {"protocolVersion": "1999-01-01"}})
    mcp.send({"jsonrpc": "2.0", "id": 4, "method": "initialize",
              "params": {"protocolVersion": 20250618}})
    got = _by_id(mcp.wait_frames(4))
    for _rid, _asked in ((3, "1999-01-01"), (4, 20250618)):
        assert got[_rid]["result"]["protocolVersion"] \
            == mcp_server._PROTOCOL_DEFAULT, \
            (f"protocolVersion {_asked!r} is not supported but was parroted "
             f"back instead of falling back to "
             f"{mcp_server._PROTOCOL_DEFAULT!r}: {got[_rid]}")
    print(f"[OK] MCP handshake: supported version honoured, unsupported "
          f"string + non-string negotiated down to "
          f"{mcp_server._PROTOCOL_DEFAULT}, tools/list == {len(listed)} "
          f"handlers")

    # ── 1b. every id answered exactly once, on every method, for junk params ─
    methods = ["initialize", "tools/list", "tools/call", "ping",
               "notifications/initialized", "notifications/cancelled",
               "resources/list"]
    junk = [None, [], "str", 7]
    sent_ids, rid = [], 100
    # Read the handshake's frame count instead of hardcoding it: a literal here
    # silently mis-slices the moment §1a gains or loses a probe.
    before = mcp.n_frames()
    for method in methods:
        for params in junk:
            msg = {"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": params}
            mcp.send(msg)
            sent_ids.append(rid)
            rid += 1
        mcp.send({"jsonrpc": "2.0", "id": rid, "method": method})  # params omitted
        sent_ids.append(rid)
        rid += 1
    frames = mcp.settle()
    got = _by_id(frames[before:])
    missing = [i for i in sent_ids if i not in got]
    assert not missing, \
        f"{len(missing)} of {len(sent_ids)} ids consumed without a reply: {missing}"
    print(f"[OK] MCP ids: {len(sent_ids)} junk-params requests "
          f"({len(methods)} methods x null/[]/str/num/omitted) answered exactly once")

    # a real Notification (no id) still gets silence
    n0 = mcp.n_frames()
    mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    mcp.send({"jsonrpc": "2.0", "method": "tools/list", "params": []})
    mcp.send({"jsonrpc": "2.0", "id": "sentinel", "method": "ping"})
    frames = mcp.wait_frames(n0 + 1)
    tail = frames[n0:]
    assert len(tail) == 1 and _strict_loads(tail[0])["id"] == "sentinel", \
        f"a Notification (no id) must get silence, got {tail}"
    print("[OK] MCP notifications: id-less messages stay silent, sentinel answered")

    # ── 1c. RFC-8259: NaN / 1e400 ids are refused, never echoed ─────────────
    for literal in (b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
                    b'{"jsonrpc":"2.0","id":1e400,"method":"ping"}'):
        n0 = mcp.n_frames()
        mcp.send_raw(literal)
        msg = _strict_loads(mcp.wait_frames(n0 + 1)[n0])
        assert msg["id"] is None and msg["error"]["code"] == -32700, \
            f"{literal!r} -> {msg}"
    print("[OK] MCP strict JSON: NaN / 1e400 ids -> -32700 (never echoed back)")

    # ── 1d. superseded + archived rows are excluded from reads ─────────────
    _call(mcp, 300, "memory_get_details", {"ids": [m_old, m_arch, m_live, m_new]})
    got = _by_id(mcp.settle())
    payload = json.loads(got[300]["result"]["content"][0]["text"])
    assert sorted(r["id"] for r in payload["results"]) == sorted([m_live, m_new]), \
        f"superseded/archived rows leaked: {payload}"
    assert sorted(payload["missing"]) == sorted([m_old, m_arch]), payload
    assert "isError" not in got[300]["result"], \
        "a dead id is a normal result, not a call failure"
    print("[OK] MCP memory_get_details: superseded + archived excluded, "
          "reported in 'missing'")

    # ── 1e. schema validation rejects instead of coercing ──────────────────
    bad = {
        401: ("memory_add", {"category": "note", "importance": 99,
                             "content": "importance ninety nine must be refused"}),
        402: ("memory_add", {"category": "bogus-category", "importance": 3,
                             "content": "a bogus category must be refused"}),
        403: ("memory_search", {"query": ""}),
        404: ("memory_search", {"query": "   "}),
        405: ("memory_search", {"query": "x", "limit": 10 ** 6}),
        406: ("memory_get_details", {"ids": []}),
        407: ("memory_add", {"category": "note", "importance": 3,
                             "content": "tiny"}),
    }
    for rid_, (tool, args) in bad.items():
        _call(mcp, rid_, tool, args)
    got = _by_id(mcp.settle())
    for rid_ in bad:
        assert got[rid_].get("error", {}).get("code") == -32602, \
            f"{bad[rid_]} was not rejected: {got[rid_]}"
    assert str(MIN_CONTENT_LEN) in got[407]["error"]["message"], got[407]
    print(f"[OK] MCP schema: {len(bad)} hostile arguments -> -32602 "
          f"(importance 99, bogus category, empty query, over-max limit, "
          f"empty ids, sub-minLength content)")

    rc, err, frames = mcp.finish()
    assert rc == 0, f"MCP server exited {rc}"
    assert err == b"", f"MCP server wrote to stderr: {err[:300]!r}"
    print(f"[OK] MCP session 1: rc=0, stderr empty, {len(frames)} frames all "
          f"strict-JSON parseable")

    # nothing hostile was stored
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT category, importance FROM memories WHERE project_id = ?",
            (pid,)).fetchall()
    assert all(r["category"] in mcp_server._CATEGORIES for r in rows), rows
    assert all(1 <= r["importance"] <= 5 for r in rows), rows

    # ── 1f. parse-level killers: a single frame must not kill the server ────
    mcp = McpProc(root)
    hostile = [
        (b'{"jsonrpc":"2.0","id":501,"method":"tools/call","params":'
         b'{"name":"memory_search","arguments":{"query":"x","limit":'
         + b"9" * 4301 + b'}}}'),                       # ValueError (int digits)
        (b'{"jsonrpc":"2.0","id":502,"method":"tools/call","params":'
         b'{"name":"memory_search","arguments":{"query":'
         + b"[" * 3125 + b'"x"' + b"]" * 3125 + b'}}}'),  # RecursionError
        (b'{"jsonrpc":"2.0","id":503,"method":"tools/call","params":'
         b'{"name":"memory_search","arguments":{"query":"'
         + b"x" * (1 << 21) + b'"}}}'),                  # over the 1 MiB frame cap
    ]
    for frame in hostile:
        mcp.send_raw(frame)
    mcp.send({"jsonrpc": "2.0", "id": 599, "method": "ping"})
    frames = mcp.wait_frames(4, timeout=40)
    got = _by_id(mcp.settle())
    assert 599 in got and "result" in got[599], \
        f"server stopped serving after the hostile frames: {frames}"
    assert mcp_server._MAX_LINE_CHARS == 1 << 20, \
        "frame cap moved; the >cap probe above is no longer over the cap"
    rc, err, frames = mcp.finish()
    assert rc == 0, f"a single hostile frame killed the server (rc={rc})"
    assert err == b"", f"hostile frames produced stderr: {err[-300:]!r}"
    print("[OK] MCP survivability: 4301-digit int, 3125-deep nesting and a "
          "2 MiB frame -> rc=0, stderr empty, next frame still served")

    # ── 1g. non-ASCII round-trips under PYTHONIOENCODING=gbk, both ways ─────
    gbk = {"PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0"}
    mcp = McpProc(root, env_extra=gbk)
    unicode_in = "入库测试 ↻ 🔥 ∀x∈ℝ ő Привет inbound payload"
    _call(mcp, 601, "memory_get_details", {"ids": [m_uni]})
    _call(mcp, 602, "memory_add", {"category": "note", "importance": 4,
                                   "content": unicode_in, "topic": "unicode"})
    got = _by_id(mcp.settle())
    out_payload = json.loads(got[601]["result"]["content"][0]["text"])
    assert out_payload["results"][0]["content"] == "OUTBOUND " + unicode_out, \
        "OUT direction mangled under gbk"
    add_result = json.loads(got[602]["result"]["content"][0]["text"])
    assert add_result["action"] == "inserted", add_result
    rc, err, _ = mcp.finish()
    assert rc == 0 and err == b"", f"gbk locale killed the server: rc={rc} {err[:300]!r}"
    raw_conn = sqlite3.connect(str(mem / "memory.db"))
    raw_conn.text_factory = bytes
    try:
        stored = raw_conn.execute("SELECT content FROM memories WHERE id = ?",
                                  (add_result["id"],)).fetchone()[0]
    finally:
        raw_conn.close()
    assert stored == unicode_in.encode("utf-8"), \
        f"IN direction not byte-identical under gbk: {stored[:80]!r}"
    print("[OK] MCP unicode: CJK/emoji/math/Cyrillic byte-identical in BOTH "
          "directions under PYTHONIOENCODING=gbk + PYTHONUTF8=0")

    # ── 1h. the missing-DB path is a FAILED call, not a silent success ──────
    empty = Path(tempfile.mkdtemp(prefix="ccm-nodb-"))
    mcp = McpProc(empty)
    for i, tool in enumerate(sorted(mcp_server._HANDLERS)):
        _call(mcp, 700 + i, tool, {"query": "x", "ids": [1], "category": "note",
                                   "content": "content long enough to pass",
                                   "importance": 3})
    got = _by_id(mcp.settle())
    for i, tool in enumerate(sorted(mcp_server._HANDLERS)):
        res = got[700 + i]["result"]
        assert res.get("isError") is True, f"{tool} on a missing DB: {res}"
    rc, err, _ = mcp.finish()
    assert rc == 0 and err == b""
    assert not (empty / "memory" / "memory.db").exists(), \
        "a read against a project with no DB must not create one"
    print(f"[OK] MCP missing DB: all {len(mcp_server._HANDLERS)} tools set "
          f"isError, no memory.db conjured")

    # ── 1i. hand-mirrored constants must not drift ─────────────────────────
    assert mcp_server._MIN_CONTENT_LEN == MIN_CONTENT_LEN, \
        (f"mcp/server.py _MIN_CONTENT_LEN={mcp_server._MIN_CONTENT_LEN} has "
         f"drifted from llm.memory_writer.MIN_CONTENT_LEN={MIN_CONTENT_LEN}; "
         f"memory_add would report success for a write that never happened")
    assert mcp_server._MAX_LIMIT <= MemoryDB._MAX_SEARCH_LIMIT, \
        "the advertised schema must stay the binding cap, not the DB clamp"
    print("[OK] MCP constants: _MIN_CONTENT_LEN mirrors the writer, "
          "_MAX_LIMIT <= MemoryDB._MAX_SEARCH_LIMIT")


# ═══════════════════════════════════════════════════════════════════════════
# §2  web viewer (raw sockets)
# ═══════════════════════════════════════════════════════════════════════════

def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _build(method, path, port, host="auto", origin=None, ctype=None,
           body=b"", ver="1.1", content_length=None):
    lines = [f"{method} {path} HTTP/{ver}"]
    if host == "auto":
        host = f"127.0.0.1:{port}"
    if host is not None:
        lines.append(f"Host: {host}")
    if origin is not None:
        lines.append(f"Origin: {origin}")
    if ctype is not None:
        lines.append(f"Content-Type: {ctype}")
    if content_length is not None:
        lines.append(f"Content-Length: {content_length}")
    elif body:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body


def _parse_http(raw):
    """(status:int|None, headers:{lower:str}, body:bytes)."""
    if not raw:
        return None, {}, b""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ")
    try:
        status = int(parts[1])
    except (IndexError, ValueError):
        return None, {}, body
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(b":")
        headers[k.strip().decode("latin-1").lower()] = v.strip().decode("latin-1")
    return status, headers, body


def _recv_all(sock):
    chunks = []
    while True:
        try:
            data = sock.recv(65536)
        except (socket.timeout, OSError):
            # why: peer reset after answering (Windows RST-on-close) or the
            # read deadline expired; whatever already arrived is the response
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def _send(port, request, timeout=20.0):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(request)
        return _parse_http(_recv_all(s))
    finally:
        s.close()


def _drip(port, head, interval, max_s, liveness_at=None):
    """Feed one body byte per `interval` until the server answers.

    Returns (status, headers, body, elapsed, liveness_status). `select` is used
    instead of sleep so the response is picked up the instant it lands -- on
    Windows the server's close-with-unread-body sends an RST that would discard
    it if we kept writing.
    """
    s = socket.create_connection(("127.0.0.1", port), timeout=max_s + 10)
    live = None
    t0 = time.monotonic()
    try:
        s.sendall(head)
        while time.monotonic() - t0 < max_s:
            readable, _, _ = select.select([s], [], [], interval)
            if readable:
                break
            if liveness_at is not None and live is None \
                    and time.monotonic() - t0 >= liveness_at:
                live = _send(port, _build("GET", "/api/stats", port),
                             timeout=5.0)[0]
            try:
                s.sendall(b"x")
            except OSError:
                # why: answered and closed under us -- stop feeding, go read
                break
        raw = _recv_all(s)
    finally:
        s.close()
    status, headers, body = _parse_http(raw)
    return status, headers, body, time.monotonic() - t0, live


class WebServer:
    def __init__(self, served_project, cwd):
        self.out_path = Path(tempfile.mkdtemp(prefix="ccm-weblog-")) / "out.txt"
        self.err_path = self.out_path.with_name("err.txt")
        self.port = None
        self.proc = None
        last = None
        for _ in range(3):
            port = _free_port()
            self._fh_out = open(self.out_path, "wb")
            self._fh_err = open(self.err_path, "wb")
            self.proc = subprocess.Popen(
                [sys.executable, str(WEB_VIEWER_PY), "--project",
                 str(served_project), "--port", str(port), "--no-open"],
                cwd=str(cwd), stdout=self._fh_out, stderr=self._fh_err,
                env=dict(os.environ))
            if self._wait_ready(port):
                self.port = port
                return
            last = port
            self.stop()
        raise AssertionError(
            f"web viewer never answered on 127.0.0.1:{last}\n"
            f"stdout: {self.out_path.read_text(errors='replace')}\n"
            f"stderr: {self.err_path.read_text(errors='replace')}")

    def _wait_ready(self, port, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                return False
            try:
                status = _send(port, _build("GET", "/api/stats", port),
                               timeout=3.0)[0]
            except OSError:
                status = None
            if status == 200:
                return True
            time.sleep(0.15)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=8)
        for fh in (getattr(self, "_fh_out", None), getattr(self, "_fh_err", None)):
            try:
                if fh:
                    fh.close()
            except OSError:
                # why: already closed by a previous stop(); nothing to do
                pass


def test_web():
    print("\n--- §2 web viewer (raw sockets) ------------------------------")
    served, served_mem, sdb, spid = _mk_project("web-served")
    bystander, by_mem, bdb, bpid = _mk_project("web-bystander")

    for imp, text in ((1, "widget alpha subsystem calibration notes"),
                      (3, "widget bravo telemetry export pipeline"),
                      (4, "widget charlie deployment rollback ledger"),
                      (5, "widget delta incident postmortem summary")):
        sdb.insert_memory(spid, None, "note", text, imp, [], "widgets")
    bdb.insert_memory(bpid, None, "note",
                      "bystander project must never be rewritten", 3, [], "bystander")
    regenerate_memory_index(sdb, spid, served_mem)
    regenerate_memory_index(bdb, bpid, by_mem)

    # constants first -- a regression here is invisible to every probe below
    from ui import web_viewer as wv                # noqa: E402  -- why: imported here so a syntax error in the viewer surfaces inside §2, not at test import
    for name in ("_BODY_STALL_S", "_BODY_DEADLINE_S", "_DRAIN_DEADLINE_S"):
        value = getattr(wv, name)
        assert 0 < value < 120, f"{name}={value} is not a usable wall-clock bound"
    assert wv._DRAIN_DEADLINE_S < wv._BODY_DEADLINE_S, \
        "draining a REJECTED body must be cheaper than reading an accepted one"
    print(f"[OK] web deadlines finite: stall={wv._BODY_STALL_S:g}s "
          f"drain={wv._DRAIN_DEADLINE_S:g}s total={wv._BODY_DEADLINE_S:g}s")

    server = WebServer(served, cwd=bystander)
    port = server.port
    try:
        # ── 2a. an idle pre-connect must NOT wedge the server ──────────────
        idle = []
        for _ in range(3):
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            idle.append(s)                      # connected, never sends a byte
        half = socket.create_connection(("127.0.0.1", port), timeout=5)
        half.sendall(b"GET /api/stats HTTP/1.1\r\n")   # partial header, no CRLFCRLF
        idle.append(half)
        t0 = time.monotonic()
        status, _, _ = _send(port, _build("GET", "/api/stats", port), timeout=10)
        elapsed = time.monotonic() - t0
        for s in idle:
            s.close()
        assert status == 200 and elapsed < 5.0, \
            (f"4 idle/partial connections wedged the server: status={status} "
             f"after {elapsed:.2f}s (this is the defect that made /cc-mem serve "
             f"answer zero requests)")
        print(f"[OK] web liveness: 3 idle + 1 half-header pre-connect, "
              f"GET /api/stats -> 200 in {elapsed:.2f}s")

        # ── 2b. garbage query params answer 400, never drop the socket ─────
        garbage = ["/api/memories?limit=abc", "/api/memories?limit=1e3",
                   "/api/memories?limit=0x1f", "/api/memories?limit=-4",
                   "/api/memories?limit=" + "9" * 400,
                   "/api/memories?importance=zzz", "/api/memories?importance=9",
                   "/api/memories?category=%3Cscript%3Ealert(1)%3C/script%3E",
                   "/api/observations?limit=abc"]
        for path in garbage:
            status, _, body = _send(port, _build("GET", path, port), timeout=10)
            assert status == 400, f"{path} -> {status} (body {body[:120]!r})"
            assert json.loads(body.decode("utf-8")).get("error"), body
        status, _, _ = _send(port, _build("GET", "/api/nope", port))
        assert status == 404
        print(f"[OK] web input: {len(garbage)} garbage query params -> 400 JSON, "
              f"unknown route -> 404, nothing dropped")

        # ── 2c. cross-origin rejected, and no ACAO header anywhere ─────────
        probes = [
            (_build("GET", "/api/stats", port, origin="http://evil.example"), 403),
            (_build("GET", "/api/stats", port, origin="null"), 403),
            (_build("GET", "/api/stats", port,
                    origin="http://127.0.0.1.evil.example"), 403),
            (_build("GET", "/api/stats", port, origin="http://127.0.0.1:1"), 403),
            (_build("GET", "/api/stats", port,
                    origin=f"http://127.0.0.1:{port}"), 200),
            (_build("GET", "/api/stats", port,
                    origin=f"http://localhost:{port}"), 200),
        ]
        for request, want in probes:
            status, headers, _ = _send(port, request)
            assert status == want, f"{request.splitlines()[:3]} -> {status}"
            assert "access-control-allow-origin" not in headers, \
                f"ACAO header re-opened the cross-origin read channel: {headers}"
        cross_post = _build("POST", "/api/memory", port,
                            origin="http://evil.example",
                            ctype="application/json",
                            body=b'{"content":"cross origin write attempt"}')
        status, headers, _ = _send(port, cross_post)
        assert status == 403, f"cross-origin POST -> {status}"
        assert "access-control-allow-origin" not in headers
        status, headers, _ = _send(port, _build("OPTIONS", "/api/memory", port))
        assert status == 405 and "allow" in headers, (status, headers)
        assert "access-control-allow-origin" not in headers
        print("[OK] web CORS: 4 cross-origin variants + POST -> 403, OPTIONS -> "
              "405+Allow, zero Access-Control-Allow-Origin headers")

        # ── 2d. Host validation (DNS rebinding) ────────────────────────────
        for host in (f"evil.rebind.example:{port}", "evil.rebind.example",
                     f"attacker.tld:{port}", "127.0.0.1:1"):
            status, _, _ = _send(port, _build("GET", "/api/sessions", port,
                                              host=host))
            assert status == 403, f"Host {host!r} -> {status} (archive_path leak)"
        status, _, _ = _send(port, _build("POST", "/api/memory", port,
                                          host=f"evil.rebind.example:{port}",
                                          ctype="application/json",
                                          body=b'{"content":"rebound write"}'))
        assert status == 403
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}",
                     "127.0.0.1"):
            status, _, _ = _send(port, _build("GET", "/api/stats", port, host=host))
            assert status == 200, f"legitimate Host {host!r} -> {status}"
        status, _, _ = _send(port, _build("GET", "/api/stats", port, host=None,
                                          ver="1.0"))
        assert status == 200, "a hand-written HTTP/1.0 client with no Host broke"
        print("[OK] web Host: 4 forged hosts + rebound POST -> 403; "
              "127.0.0.1/localhost/[::1]/bare/no-Host still 200")

        # ── 2e. importance survives a q= search ────────────────────────────
        status, _, body = _send(port, _build(
            "GET", "/api/memories?q=widget&importance=5", port))
        assert status == 200, status
        rows = json.loads(body.decode("utf-8"))["results"]
        assert rows and all(r["importance"] >= 5 for r in rows), \
            f"importance filter dropped when q is present: {[r['importance'] for r in rows]}"
        status, _, body = _send(port, _build(
            "GET", "/api/memories?q=widget&importance=1", port))
        assert len(json.loads(body.decode("utf-8"))["results"]) >= 4, \
            "over-filtered: importance=1 must not shrink the result set"
        print("[OK] web filters: importance honoured together with q, "
              "importance=1 not over-filtered")

        # ── 2f. POST writes the SERVED project, not the process cwd ────────
        served_before = (served_mem / "MEMORY.md").read_bytes()
        by_before = (by_mem / "MEMORY.md").read_bytes()
        marker = "targeting-probe-alpha"
        status, _, body = _send(port, _build(
            "POST", "/api/memory", port, ctype="application/json",
            origin=f"http://127.0.0.1:{port}",
            body=json.dumps({"category": "note", "importance": 3,
                             "topic": marker,
                             "content": "served project MEMORY.md targeting probe"
                             }).encode("utf-8")))
        assert status == 200, (status, body[:200])
        assert json.loads(body.decode("utf-8"))["action"] == "inserted", body
        served_after = (served_mem / "MEMORY.md").read_bytes()
        by_after = (by_mem / "MEMORY.md").read_bytes()
        assert served_after != served_before, "served MEMORY.md was not refreshed"
        assert marker.encode() in served_after, "probe topic missing from served index"
        assert by_after == by_before, \
            "the process cwd's MEMORY.md was rewritten (os.getcwd() targeting is back)"
        assert marker.encode() not in by_after
        print("[OK] web POST targeting: served MEMORY.md refreshed with the probe "
              "topic, bystander cwd project byte-identical")

        # ── 2g. a large legitimate body still assembles byte-exact ─────────
        big = "bulk-body-probe " + ("abcdefghij" * 30000)
        status, _, body = _send(port, _build(
            "POST", "/api/memory", port, ctype="application/json",
            body=json.dumps({"category": "note", "importance": 2,
                             "content": big}).encode("utf-8")), timeout=30)
        assert status == 200, (status, body[:200])
        result = json.loads(body.decode("utf-8"))
        assert result["action"] == "inserted", result
        with sdb._connect() as conn:
            stored = conn.execute("SELECT content FROM memories WHERE id = ?",
                                  (result["id"],)).fetchone()["content"]
        assert stored == big, \
            f"read1() loop mis-assembled the body: {len(stored)} vs {len(big)} chars"
        print(f"[OK] web large body: {len(big)} chars round-trip byte-exact "
              f"through the bounded read1 loop")

        # ── 2h. a truncated body is a prompt 4xx, not a 10s 500 ────────────
        s = socket.create_connection(("127.0.0.1", port), timeout=30)
        t0 = time.monotonic()
        try:
            s.sendall(_build("POST", "/api/memory", port,
                             ctype="application/json", content_length=500)
                      + b"12345")
            status, _, body = _parse_http(_recv_all(s))
        finally:
            s.close()
        elapsed = time.monotonic() - t0
        assert status == 400, f"truncated body -> {status} after {elapsed:.2f}s"
        assert elapsed < 12.0, f"truncated body answered only after {elapsed:.2f}s"
        print(f"[OK] web truncated body: 400 after {elapsed:.2f}s "
              f"(idle bound {wv._BODY_STALL_S:g}s), not a 500 on socket.timeout")

        # ── 2i. a slow drip is cut off by wall clock, not by per-recv idle ──
        status, _, _, elapsed, _ = _drip(
            port,
            _build("POST", "/api/memory", port, origin="http://evil.example",
                   ctype="application/json", content_length=1 << 20),
            interval=0.4, max_s=25)
        assert status == 403, f"drip-fed rejected POST -> {status} after {elapsed:.2f}s"
        assert elapsed < 10.0, \
            (f"the drain held a worker thread {elapsed:.2f}s: the bound is on "
             f"SIZE only again, and every drip byte resets the per-recv timeout")
        print(f"[OK] web drain deadline: rejected POST drip-fed 1 byte/0.4s "
              f"answered 403 in {elapsed:.2f}s (deadline "
              f"{wv._DRAIN_DEADLINE_S:g}s), was 52.09s pre-fix")

        status, _, _, elapsed, live = _drip(
            port,
            _build("POST", "/api/memory", port,
                   origin=f"http://127.0.0.1:{port}",
                   ctype="application/json", content_length=1 << 20),
            interval=0.5, max_s=30, liveness_at=4.0)
        assert status == 400, f"drip-fed accepted POST -> {status} after {elapsed:.2f}s"
        assert elapsed < 22.0, \
            f"primary body read has no wall-clock deadline ({elapsed:.2f}s)"
        assert live == 200, \
            f"the server stopped answering while a drip was in flight (live={live})"
        print(f"[OK] web body deadline: accepted-path drip answered 400 in "
              f"{elapsed:.2f}s (deadline {wv._BODY_DEADLINE_S:g}s) and "
              f"GET /api/stats stayed 200 mid-attack")

        status, _, _ = _send(port, _build("GET", "/api/stats", port))
        assert status == 200, "server did not survive the drip probes"
    finally:
        server.stop()

    err_text = server.err_path.read_text(encoding="utf-8", errors="replace")
    assert "Traceback" not in err_text, f"web viewer wrote a traceback:\n{err_text}"
    out_text = server.out_path.read_text(encoding="utf-8", errors="replace")
    assert "cc-memory dashboard:" in out_text, out_text
    print("[OK] web teardown: server stopped, stderr traceback-free")


# ═══════════════════════════════════════════════════════════════════════════
# §3  installer settings.json shape matrix
# ═══════════════════════════════════════════════════════════════════════════

# (tag, settings.json bytes-as-text, install must succeed)
SETTINGS_SHAPES = [
    ("well-formed",      '{"model": "opus"}', True),
    ("jsonc-comments",   '{\n  // a preference\n  "model": "opus"\n}', False),
    ("trailing-comma",   '{\n  "model": "opus",\n}', False),
    ("empty-file",       "", True),
    ("whitespace-only",  "   \n\t\n", True),
    ("bom-prefixed",     '﻿{"model": "opus"}', True),
    ("hooks-list",       '{"hooks": []}', True),
    ("hooks-string",     '{"hooks": "none"}', True),
    ("toplevel-list",    "[]", False),
    ("toplevel-number",  "42", False),
    ("permissions-str",  '{"permissions": "all"}', True),
    ("group-hooks-null", '{"hooks": {"Stop": [{"matcher": "", "hooks": null}]}}', True),
    ("group-hooks-5",    '{"hooks": {"Stop": [{"matcher": "", "hooks": 5}]}}', True),
    ("group-command-123",
     '{"hooks": {"Stop": [{"matcher": "", "hooks": '
     '[{"type": "command", "command": 123}]}]}}', True),
    ("event-not-list",   '{"hooks": {"Stop": "nope"}}', True),
]
_MALFORMED_GROUP_TAGS = ("group-hooks-null", "group-hooks-5", "group-command-123")


def _quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


def _point_installer_at(tag):
    home = Path(tempfile.mkdtemp(prefix=f"ccm-inst-{tag}-"))
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    inst.CLAUDE_DIR = claude
    inst.TARGET_DIR = claude / "hooks" / "cc-memory"
    inst.SETTINGS_PATH = claude / "settings.json"
    inst.SURFACE_MANIFEST = inst.TARGET_DIR / "installed_surfaces.json"
    return claude


def _ccm_groups(settings):
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    return [(event, g) for event, lst in hooks.items()
            if isinstance(lst, list) for g in lst if inst._is_ccm_group(g)]


def test_installer():
    print("\n--- §3 installer settings.json shape matrix ------------------")
    saved = (inst.CLAUDE_DIR, inst.TARGET_DIR, inst.SETTINGS_PATH,
             inst.SURFACE_MANIFEST, inst._detect_python_cmd)
    # why: _detect_python_cmd shells out on every install; the interpreter name
    # is orthogonal to every shape under test (_CCM_COMMAND_RE matches on the
    # SCRIPT path), and 15 installs x 2 probes is pure wall clock.
    inst._detect_python_cmd = lambda: "python3"
    try:
        assert Path.home() == _HOME, "sandbox home lost before installer tests"
        n_ok = n_bad = 0
        for tag, raw, must_install in SETTINGS_SHAPES:
            claude = _point_installer_at(tag)
            inst.SETTINGS_PATH.write_text(raw, encoding="utf-8")
            rc, out = _quiet(inst.cli_install)

            if not must_install:
                n_bad += 1
                assert rc == 1, f"{tag}: cli_install rc={rc}, expected 1\n{out}"
                assert not inst.TARGET_DIR.exists(), \
                    f"{tag}: files were copied before settings.json was validated"
                assert "[FAIL]" in out, f"{tag}: no [FAIL] line\n{out}"
                rc2, out2 = _quiet(inst.cli_uninstall)
                assert rc2 == 1, f"{tag}: cli_uninstall rc={rc2}\n{out2}"
                continue

            n_ok += 1
            assert rc == 0, f"{tag}: cli_install rc={rc}\n{out}"
            assert (inst.TARGET_DIR / "core" / "db.py").is_file(), tag
            for rel in inst.SURFACE_FILES:
                assert (claude / rel).is_file(), f"{tag}: surface missing {rel}"
            settings = json.loads(inst.SETTINGS_PATH.read_text(encoding="utf-8"))
            hooks = settings.get("hooks")
            assert isinstance(hooks, dict) and len(hooks) == 5, \
                f"{tag}: expected 5 registered events, got {hooks}"
            assert all(isinstance(g, dict) for lst in hooks.values() for g in lst), \
                f"{tag}: a string hook value was iterated into characters"
            ours = _ccm_groups(settings)
            assert len(ours) == 5, f"{tag}: {len(ours)} cc-memory groups, expected 5"
            precompact = [g for ev, g in ours if ev == "PreCompact"][0]
            assert len(precompact["hooks"]) == 2, f"{tag}: {precompact}"
            assert sum(1 for h in precompact["hooks"] if h.get("async")) == 1, \
                f"{tag}: the async consolidation leg is missing"
            if tag in _MALFORMED_GROUP_TAGS:
                original = json.loads(raw)["hooks"]["Stop"][0]
                assert hooks["Stop"][0] == original, \
                    f"{tag}: the user's malformed group was mangled: {hooks['Stop']}"
            if tag in ("well-formed", "bom-prefixed"):
                assert settings["model"] == "opus", f"{tag}: sibling key lost"
            if tag == "bom-prefixed":
                assert not inst.SETTINGS_PATH.read_bytes().startswith(
                    b"\xef\xbb\xbf"), "installer rewrote settings.json WITH a BOM"

            rc2, out2 = _quiet(inst.cli_uninstall)
            assert rc2 == 0, f"{tag}: cli_uninstall rc={rc2}\n{out2}"
            settings = json.loads(inst.SETTINGS_PATH.read_text(encoding="utf-8"))
            assert not _ccm_groups(settings), \
                f"{tag}: cc-memory hooks survived uninstall: {settings.get('hooks')}"
            assert "hooks" not in settings or settings["hooks"], \
                f'{tag}: uninstall left an empty "hooks": {{}}'
            # By VALUE, not by type. Uninstall clobbering "all" -> "" keeps the
            # type and passed the old isinstance() check. The installer never
            # CREATES `permissions` (_uninstall_settings only prunes an
            # already-present additionalDirectories list), so anything other
            # than an exact round-trip -- including inventing the key -- is a
            # bug. Presence is compared too, hence the one-key dict.
            perms_want = {"permissions": "all"} if tag == "permissions-str" else {}
            perms_got = {k: settings[k] for k in ("permissions",) if k in settings}
            assert perms_got == perms_want, \
                (f"{tag}: the user's `permissions` value must round-trip "
                 f"through install+uninstall: got {perms_got!r}, "
                 f"expected {perms_want!r}")
            if tag in _MALFORMED_GROUP_TAGS:
                assert settings["hooks"]["Stop"] == [json.loads(raw)["hooks"]["Stop"][0]], \
                    f"{tag}: uninstall did not restore the user's own group"
            for rel in inst.SURFACE_FILES:
                assert not (claude / rel).exists(), f"{tag}: surface survived: {rel}"
        print(f"[OK] installer matrix: {len(SETTINGS_SHAPES)} settings.json shapes "
              f"({n_ok} install+uninstall clean, {n_bad} refused before any copy), "
              f"zero tracebacks")

        # ── 3b. the user's own data survives install AND uninstall ──────────
        claude = _point_installer_at("userdata")
        user_cmd = "python audit.py --repo /work/cc-memory"
        user_group = {"matcher": "", "hooks": [
            {"type": "command", "command": user_cmd, "timeout": 5}]}
        inst.SETTINGS_PATH.write_text(json.dumps(
            {"hooks": {"Stop": [user_group]},
             "permissions": {"additionalDirectories": ["/work/cc-memory-notes"]}},
            indent=2), encoding="utf-8")
        for rel in ("commands/not-ours.md", "agents/my-agent.md",
                    "skills/my-skill/SKILL.md"):
            p = claude / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("mine\n", encoding="utf-8")

        rc, out = _quiet(inst.cli_install)
        assert rc == 0, out
        settings = json.loads(inst.SETTINGS_PATH.read_text(encoding="utf-8"))
        assert user_group in settings["hooks"]["Stop"], \
            f'a user hook merely MENTIONING "cc-memory" was deleted by install: ' \
            f'{settings["hooks"]["Stop"]}'
        assert "[WARN]" in out and user_cmd in out, \
            "a kept-but-ambiguous hook must be reported, not silently retained"

        (inst.TARGET_DIR / "logs").mkdir(parents=True, exist_ok=True)
        (inst.TARGET_DIR / "logs" / "cc-memory-2026-01-01.log").write_text(
            "x\n", encoding="utf-8")
        marker = Path(tempfile.gettempdir()) / "cc_mem_turns_surfacetest01"
        marker.write_text("7\n", encoding="utf-8")
        keepme = Path(tempfile.gettempdir()) / "cc_mem_not_ours.txt"
        keepme.write_text("keep\n", encoding="utf-8")

        rc, out = _quiet(inst.cli_uninstall)
        assert rc == 0, out
        settings = json.loads(inst.SETTINGS_PATH.read_text(encoding="utf-8"))
        assert settings["hooks"]["Stop"] == [user_group], \
            f"uninstall ate the user's own hook: {settings.get('hooks')}"
        assert settings["permissions"]["additionalDirectories"] == \
            ["/work/cc-memory-notes"], \
            "uninstall ate an additionalDirectories entry that only mentions the name"
        for rel in ("commands/not-ours.md", "agents/my-agent.md",
                    "skills/my-skill/SKILL.md"):
            assert (claude / rel).is_file(), f"uninstall deleted a user file: {rel}"
        for rel in inst.SURFACE_FILES:
            assert not (claude / rel).exists(), f"our surface survived: {rel}"
        assert not (claude / "skills" / "ccm-load").exists(), \
            "an emptied skills/ccm-load/ directory must be removed"
        assert (inst.TARGET_DIR / "logs" / "cc-memory-2026-01-01.log").is_file(), \
            "uninstall deleted the log history"
        assert not (inst.TARGET_DIR / "core").exists(), "package files survived"
        assert not marker.exists(), "the temp session marker was not swept"
        assert keepme.exists(), "the temp sweep deleted a file that is not ours"
        assert str(Path(tempfile.gettempdir())).startswith(str(_SANDBOX)), \
            "the temp sweep ran against the REAL %TEMP%"
        print("[OK] installer user data: cc-memory-mentioning hook + "
              "commands/agents/skills files + logs/ + foreign temp files all "
              "survive; our surfaces and markers are gone")
    finally:
        (inst.CLAUDE_DIR, inst.TARGET_DIR, inst.SETTINGS_PATH,
         inst.SURFACE_MANIFEST, inst._detect_python_cmd) = saved


# ═══════════════════════════════════════════════════════════════════════════
# §4  excluded_projects -- the only opt-out, across all six hooks
# ═══════════════════════════════════════════════════════════════════════════

# The six hook entry points, in the order a real session fires them.
# user_prompt and pre_compact are the two that CREATE memory/; the other four
# gate on memory/memory.db merely EXISTING, which is why the fixture below is
# a project that was initialised BEFORE it was excluded -- the case where
# "gates on the DB existing" and "opts out" are not the same behaviour.
_HOOK_ORDER = ["user_prompt", "post_tool_use", "stop", "pre_compact",
               "session_start", "consolidate_async"]


def _hook_payload(hook, cwd, session_id, transcript):
    """The stdin object Claude Code hands this hook."""
    data = {"cwd": str(cwd), "session_id": session_id}
    if hook == "user_prompt":
        data["prompt"] = "please write the exporter"
    elif hook == "post_tool_use":
        data.update(tool_name="Read", tool_input={"file_path": "notes.md"},
                    tool_response="an entirely harmless tool response body")
    elif hook == "pre_compact":
        data.update(transcript_path=str(transcript), trigger="manual")
    return data


def _run_hook(pkg, hook, cwd, session_id, transcript):
    """One hook, one real subprocess. Returns (rc, stdout, stderr)."""
    env = dict(os.environ)
    # No live credential may reach a hook here: the Stop observer and the
    # SessionStart retroactive save would otherwise POST fixture text to the
    # Anthropic API from a test run. Their LLM legs are expected to fail; the
    # hook contract says that must still be rc=0 with an empty stderr.
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(var, None)
    proc = subprocess.run(
        [sys.executable, str(pkg / "hooks" / f"{hook}.py")], cwd=str(cwd),
        input=json.dumps(_hook_payload(hook, cwd, session_id, transcript)),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=180)
    return proc.returncode, proc.stdout, proc.stderr


def _seed_project(root, n_sessions=6):
    """A project that already has memory/memory.db -- i.e. one the user
    initialised and listed in excluded_projects AFTERWARDS, which is the
    natural sequence (you reach for the control on realising a repo is
    sensitive). n_sessions is >= config.json consolidation.auto_interval_
    sessions so consolidate_async's interval gate is OPEN and the opt-out is
    the only thing that can stop it."""
    mem = root / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    db = MemoryDB(mem / "memory.db")
    pid = db.upsert_project(str(root))
    for i in range(n_sessions):
        db.insert_session(pid, f"pre-existing-{i}", "auto", 10, "", "")
    return db, pid


def _memory_names(mem_dir):
    """Everything under memory/, minus SQLite's transient journal siblings."""
    if not mem_dir.exists():
        return set()
    return {p.relative_to(mem_dir).as_posix()
            for p in mem_dir.rglob("*")} - {"memory.db-wal", "memory.db-shm"}


def test_excluded_projects():
    print("\n--- §4 excluded_projects across all six hooks ----------------")
    # A COPY of the package: the repo's own config.json must never be written
    # to (it is the live plugin on this machine), and the fixture paths have to
    # be listed literally because matching is on the resolved absolute path.
    pkg = Path(tempfile.mkdtemp(prefix="ccm-excl-pkg-")) / "cc_memory"
    shutil.copytree(REPO / "cc_memory", pkg,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                  "projects.json"))
    assert REPO not in pkg.parents, "the fixture package must be a COPY"

    work = Path(tempfile.mkdtemp(prefix="ccm-excl-work-"))
    excluded = work / "excluded-project"
    beneath = excluded / "vendor" / "nested-checkout"
    control = work / "control-project"
    fresh = work / "control-fresh"
    for d in (excluded, beneath, control, fresh):
        d.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((pkg / "config.json").read_text(encoding="utf-8"))
    cfg["excluded_projects"] = [str(excluded)]
    (pkg / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
    assert json.loads((pkg / "config.json").read_text(
        encoding="utf-8"))["excluded_projects"] == [str(excluded)]

    transcript = work / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in (
        {"type": "user",
         "message": {"role": "user", "content": "build the exporter"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit",
             "input": {"file_path": "src/exporter.py"}}]}},
    )) + "\n", encoding="utf-8")

    excl_db, excl_pid = _seed_project(excluded)
    ctrl_db, ctrl_pid = _seed_project(control)
    seeded = _memory_names(excluded / "memory")
    assert seeded == {"memory.db"}, seeded
    tmp_dir = Path(tempfile.gettempdir())

    # ── the contract, asserted on OUTCOMES rather than on any one hook's
    #    implementation: no memory/, no observations, no progress row, no
    #    injection -- for the excluded directory AND for a directory beneath it.
    for tag, cwd, sid in (
            ("excluded root (memory.db ALREADY exists)", excluded,
             "excl-root-000001"),
            ("a directory BENEATH the excluded root", beneath,
             "excl-deep-000001")):
        for hook in _HOOK_ORDER:
            rc, out, err = _run_hook(pkg, hook, cwd, sid, transcript)
            assert rc == 0 and err == "", \
                f"{tag}: {hook} rc={rc} stderr={err[:300]!r}"
            assert out == "", \
                (f"{tag}: {hook} wrote {len(out)} chars into the session "
                 f"instead of staying silent: {out[:300]!r}")
        assert not (tmp_dir / f"cc_mem_turns_{sid[:16]}").exists(), \
            f"{tag}: a turn marker was written for an excluded project"

    assert _memory_names(excluded / "memory") == seeded, \
        (f"the excluded project gained artifacts: "
         f"{sorted(_memory_names(excluded / 'memory') - seeded)}")
    assert not (beneath / "memory").exists(), \
        "memory/ was created in a directory BENEATH the excluded root"
    assert excl_db.get_observation_count(excl_pid) == 0, \
        "tool inputs/outputs were stored for an excluded project"
    assert excl_db.get_progress(excl_pid) is None, \
        "a progress row was created for an excluded project"
    assert len(excl_db.get_all_active_memories(excl_pid)) == 0, \
        "a memory was extracted for an excluded project"

    # ── control: the SAME fixture, one directory over, NOT listed. Without it
    #    every assertion above also passes for a hook that no-ops for an
    #    unrelated reason (missing DB, closed interval gate, empty transcript).
    ctrl_sid = "ctrl-seeded-0001"
    ctrl_out, ctrl_obs = {}, {}
    for hook in _HOOK_ORDER:
        rc, out, err = _run_hook(pkg, hook, control, ctrl_sid, transcript)
        assert rc == 0 and err == "", f"control: {hook} rc={rc} {err[:300]!r}"
        ctrl_out[hook] = out
        # sampled per hook: PreCompact CLEANS observations after extracting,
        # so a count taken at the end would read 0 for a healthy run.
        ctrl_obs[hook] = ctrl_db.get_observation_count(ctrl_pid)
    assert ctrl_obs["post_tool_use"] >= 1, \
        "control: PostToolUse stored no observation, so 'no observations' above proves nothing"
    assert "[cc-memory]" in ctrl_out["session_start"], \
        "control: SessionStart injected nothing, so 'no injection' above is vacuous"
    assert "[cc-memory]" in ctrl_out["stop"], \
        "control: Stop printed no status line"
    assert ctrl_db.get_progress(ctrl_pid) is not None, \
        "control: no progress row, so 'no progress row' above is vacuous"
    assert (control / "memory" / "PROGRESS.md").is_file(), \
        "control: PreCompact wrote no PROGRESS.md"
    assert (tmp_dir / f"cc_mem_turns_{ctrl_sid[:16]}").is_file(), \
        "control: UserPromptSubmit wrote no turn marker"

    # ...and a NOT-excluded project with nothing yet still gets memory/ built,
    # which is what makes the "directory beneath" assertion non-vacuous.
    for hook in ("user_prompt", "pre_compact"):
        rc, out, err = _run_hook(pkg, hook, fresh, "ctrl-fresh-00001",
                                 transcript)
        assert rc == 0 and err == "", \
            f"control-fresh: {hook} rc={rc} {err[:300]!r}"
    assert (fresh / "memory" / "memory.db").is_file(), \
        "control-fresh: a NOT-excluded fresh project got no memory/memory.db"

    print(f"[OK] excluded_projects: {len(_HOOK_ORDER)} hooks x (excluded root "
          f"with memory.db ALREADY present + a directory beneath it) -> no "
          f"memory/, no observations, no progress row, no injection, no turn "
          f"marker; the same hooks against an identical NOT-excluded project "
          f"produce all five")


# ═══════════════════════════════════════════════════════════════════════════

def main():
    print(f"Sandbox root: {_SANDBOX}")
    print(f"Sandbox home: {Path.home()}")
    started = time.monotonic()
    test_mcp()
    test_web()
    test_installer()
    test_excluded_projects()
    # Teardown is a GATE, not a courtesy: this suite creates ~475 KB of SQLite
    # per run under the real %TEMP% and used to hide its own failure to remove
    # it behind ignore_errors=True.
    _cleanup_sandbox()
    print("[OK] sandbox teardown: every sqlite handle closed, sandbox removed")
    print(f"\n===== ALL SURFACE TESTS PASSED ({time.monotonic() - started:.1f}s) =====")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        traceback.print_exc()
        print(f"\n===== SURFACE TESTS FAILED (sandbox kept at {_SANDBOX}) =====")
        sys.exit(1)
