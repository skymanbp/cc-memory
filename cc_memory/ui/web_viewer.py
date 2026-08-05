#!/usr/bin/env python3
"""
cc-memory/web_viewer.py -- Browser-based memory dashboard
=========================================================
Serves a single-page web app on localhost for viewing, searching,
and managing memories. Uses only Python stdlib (http.server).

Launch: python web_viewer.py [--port 9377] [--project PATH] [--no-open]

Threading / liveness
--------------------
The server is a ThreadingHTTPServer with daemon threads and a per-connection
socket timeout. A single-threaded HTTPServer with no handler timeout is wedged
forever by ONE idle TCP connection (browsers speculatively pre-connect, and
`webbrowser.open` below triggers exactly that), which made the viewer answer
zero requests in practice.

Trust boundary
--------------
Everything written through POST /api/memory is later injected into the next
Claude session at SessionStart, so this endpoint is a prompt-injection channel
into the user's own context. READS are sensitive too: /api/sessions selects
whole session rows — `archive_path` included — so leaking a response hands out
filesystem paths on top of memory text. Accordingly the server:
  * binds to 127.0.0.1 only,
  * sends NO Access-Control-Allow-Origin header (so a page on another origin
    cannot read a response),
  * rejects any request whose Host header is not this loopback origin. The
    Origin check below does NOT cover reads: under DNS rebinding
    (attacker.tld -> 127.0.0.1) the attacking page is *same-origin* with this
    server, so its GETs carry no Origin header at all and would otherwise be
    served in full. Host is the only header a browser will not let that page
    forge away.
  * rejects any request whose Origin header is not this exact origin, and
  * requires Content-Type: application/json on POST (an HTML form cannot send
    that content type without a preflight, which the missing CORS headers deny).

Body liveness
-------------
Both request-body paths (the POST read and the courtesy drain that precedes an
error response) run under an ABSOLUTE wall-clock deadline. `timeout` below is a
per-recv socket timeout that every arriving byte resets, so a size bound alone
let a client dripping one byte per 9 s lease a worker thread for hours —
ThreadingHTTPServer caps neither threads nor connections.
"""

import argparse
import json
import socket
import sys
import time
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

_HERE = Path(__file__).resolve().parent     # cc_memory/ui/
_PKG_ROOT = _HERE.parent                     # cc_memory/
sys.path.insert(0, str(_PKG_ROOT))

from core.db import MemoryDB
from core.encoding_setup import enable_utf8_io
from core.logger import get_logger
from llm.memory_writer import upsert_smart, regenerate_memory_index

_log = get_logger("web")

# Mirrors the allow-list inside llm.memory_writer.upsert_smart.
_CATEGORIES = ("decision", "result", "config", "bug", "task", "arch", "note")
_MAX_BODY = 1 << 20          # 1 MiB — a memory is text, not an upload
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# Wall-clock budgets for reading a request body. See "Body liveness" above.
_BODY_STALL_S = 5.0          # max silence inside one body read
_BODY_DEADLINE_S = 15.0      # max TOTAL time to receive a declared body
_DRAIN_DEADLINE_S = 3.0      # max TOTAL time spent draining a rejected body


class _BadRequest(ValueError):
    """Client-side input error -> HTTP 400.

    Raised instead of letting a ValueError escape the handler: an unhandled
    exception in do_GET/do_POST closes the socket with no HTTP response at
    all, which every client reports as RemoteDisconnected.
    """


def _int(raw, default, minimum=None, maximum=None, name="parameter"):
    """Parse an int query/body value, or raise _BadRequest (never ValueError)."""
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise _BadRequest(f"{name} must be an integer, got {raw!r}")
    if minimum is not None and value < minimum:
        raise _BadRequest(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise _BadRequest(f"{name} must be <= {maximum}, got {value}")
    return value


# ---------------------------------------------------------------------------
# HTML SPA (embedded)
# ---------------------------------------------------------------------------
_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cc-memory Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 1200px; margin: auto; }
  h1 { color: #58a6ff; margin-bottom: 16px; font-size: 1.5em; }
  h2 { color: #8b949e; margin: 16px 0 8px; font-size: 1.1em; border-bottom: 1px solid #21262d; padding-bottom: 4px; }
  .top-bar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
  input, select, button, textarea { background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
    padding: 6px 12px; border-radius: 6px; font-size: 14px; font-family: inherit; }
  input:focus, select:focus, textarea:focus { border-color: #58a6ff; outline: none; }
  button { cursor: pointer; background: #21262d; }
  button:hover { background: #30363d; }
  button.primary { background: #238636; border-color: #238636; }
  button.primary:hover { background: #2ea043; }
  #search { width: 300px; }
  .tabs { display: flex; gap: 2px; margin-bottom: 16px; }
  .tabs button { border-radius: 6px 6px 0 0; border-bottom: none; }
  .tabs button.active { background: #161b22; color: #58a6ff; border-bottom: 2px solid #58a6ff; }
  .card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 12px; margin-bottom: 8px; }
  .card .meta { color: #8b949e; font-size: 12px; }
  .card .content { margin-top: 4px; white-space: pre-wrap; }
  .imp { display: inline-block; width: 14px; text-align: center; }
  .imp-5 { color: #f85149; font-weight: bold; }
  .imp-4 { color: #d29922; }
  .imp-3 { color: #58a6ff; }
  .imp-2 { color: #8b949e; }
  .imp-1 { color: #484f58; }
  .cat { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; }
  .cat-decision { background: #1f3d2c; color: #3fb950; }
  .cat-result { background: #1c2d3e; color: #58a6ff; }
  .cat-bug { background: #3d1f1f; color: #f85149; }
  .cat-config { background: #3d351f; color: #d29922; }
  .cat-task { background: #2d1f3d; color: #bc8cff; }
  .cat-arch { background: #1f2d3d; color: #79c0ff; }
  .cat-note { background: #21262d; color: #8b949e; }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
  .stat-card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 12px; text-align: center; }
  .stat-card .num { font-size: 1.8em; color: #58a6ff; font-weight: bold; }
  .stat-card .label { font-size: 12px; color: #8b949e; }
  .topic-card { background: #161b22; border-left: 3px solid #58a6ff; padding: 10px 12px; margin-bottom: 8px; border-radius: 0 6px 6px 0; }
  .topic-card .name { color: #58a6ff; font-weight: 600; }
  .obs-row { font-family: monospace; font-size: 13px; padding: 4px 8px; border-bottom: 1px solid #21262d; }
  .obs-tool { color: #d29922; }
  .obs-input { color: #8b949e; }
  .hidden { display: none; }
  #panel { min-height: 400px; }
  .empty { color: #484f58; padding: 40px; text-align: center; }
  #add-form { margin-bottom: 16px; }
  #add-form .row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
  #add-content { width: 100%; min-height: 90px; resize: vertical; }
  #add-topic { flex: 1; min-width: 160px; }
  #add-status { font-size: 13px; color: #8b949e; }
  #add-status.err { color: #f85149; }
  #add-status.ok { color: #3fb950; }
</style>
</head>
<body>
<h1>cc-memory Dashboard</h1>
<div class="top-bar">
  <input type="text" id="search" placeholder="Search memories..." onkeyup="if(event.key==='Enter')doSearch()">
  <button onclick="doSearch()">Search</button>
  <select id="cat-filter" onchange="doSearch()">
    <option value="">All categories</option>
    <option value="decision">decision</option>
    <option value="result">result</option>
    <option value="config">config</option>
    <option value="bug">bug</option>
    <option value="task">task</option>
    <option value="arch">arch</option>
    <option value="note">note</option>
  </select>
  <button class="primary" id="add-toggle" onclick="toggleAdd()">+ Add memory</button>
</div>

<div id="add-form" class="card hidden">
  <div class="row">
    <select id="add-cat" title="category">
      <option value="note">note</option>
      <option value="decision">decision</option>
      <option value="result">result</option>
      <option value="config">config</option>
      <option value="bug">bug</option>
      <option value="task">task</option>
      <option value="arch">arch</option>
    </select>
    <select id="add-imp" title="importance">
      <option value="1">1 - trivia</option>
      <option value="2">2 - minor</option>
      <option value="3" selected>3 - normal</option>
      <option value="4">4 - important</option>
      <option value="5">5 - critical</option>
    </select>
    <input type="text" id="add-topic" placeholder="topic (optional)">
  </div>
  <div class="row">
    <textarea id="add-content" placeholder="Memory content (10 characters minimum). Saved through upsert_smart, so a near-duplicate MERGES or SUPERSEDES instead of stacking."></textarea>
  </div>
  <div class="row">
    <button class="primary" onclick="saveMemory()">Save memory</button>
    <button onclick="toggleAdd()">Cancel</button>
    <span id="add-status"></span>
  </div>
</div>

<div class="tabs">
  <button class="active" onclick="showTab('memories')">Memories</button>
  <button onclick="showTab('topics')">Topics</button>
  <button onclick="showTab('observations')">Observations</button>
  <button onclick="showTab('sessions')">Sessions</button>
  <button onclick="showTab('stats')">Stats</button>
</div>
<div id="panel"><div class="empty">Loading...</div></div>

<script>
const API = '';
let currentTab = 'memories';

async function api(path, options) {
  const r = await fetch(API + path, options);
  return r.json();
}

const CATS = ['decision','result','config','bug','task','arch','note'];
function impClass(n) { return 'imp imp-' + n; }
function impStars(n) { return '<span class="' + impClass(n) + '">' + '★'.repeat(n) + '☆'.repeat(5-n) + '</span>'; }
// catBadge was the one sink that fed a DB column straight into an ATTRIBUTE.
// The class suffix now comes from a fixed allow-list instead of the row, and
// the visible label goes through esc() like every other sink.
function catBadge(c) { const s = String(c == null ? '' : c); return '<span class="cat' + (CATS.indexOf(s) >= 0 ? ' cat-' + s : '') + '">' + esc(s) + '</span>'; }
function esc(s) { return s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;') : ''; }

function showTab(name) {
  currentTab = name;
  document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tabs button').forEach(b => { if(b.textContent.toLowerCase()===name) b.classList.add('active'); });
  if (name === 'memories') doSearch();
  else if (name === 'topics') loadTopics();
  else if (name === 'observations') loadObservations();
  else if (name === 'sessions') loadSessions();
  else if (name === 'stats') loadStats();
}

function toggleAdd() {
  const f = document.getElementById('add-form');
  f.classList.toggle('hidden');
  if (!f.classList.contains('hidden')) document.getElementById('add-content').focus();
}

function setAddStatus(msg, kind) {
  const el = document.getElementById('add-status');
  el.className = kind || '';
  el.textContent = msg;          // textContent, never innerHTML
}

async function saveMemory() {
  const content = document.getElementById('add-content').value.trim();
  if (content.length < 10) { setAddStatus('Content must be at least 10 characters.', 'err'); return; }
  setAddStatus('Saving...', '');
  let res;
  try {
    res = await api('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        category: document.getElementById('add-cat').value,
        importance: parseInt(document.getElementById('add-imp').value, 10),
        topic: document.getElementById('add-topic').value,
        content: content
      })
    });
  } catch (e) {
    setAddStatus('Request failed: ' + e, 'err');
    return;
  }
  if (res.error) { setAddStatus('Error: ' + res.error, 'err'); return; }
  if (res.action === 'skipped') {
    setAddStatus('Skipped (' + (res.reason || 'duplicate') + ')' + (res.id ? ' — existing #' + res.id : ''), 'err');
    return;
  }
  const sim = (typeof res.similarity === 'number' && res.action !== 'inserted')
    ? ' sim=' + res.similarity.toFixed(2) : '';
  setAddStatus(res.action + ' #' + res.id + sim, 'ok');
  document.getElementById('add-content').value = '';
  document.getElementById('add-topic').value = '';
  if (currentTab === 'memories') doSearch();
}

async function doSearch() {
  const q = document.getElementById('search').value;
  const cat = document.getElementById('cat-filter').value;
  let url = '/api/memories?';
  if (q) url += 'q=' + encodeURIComponent(q) + '&';
  if (cat) url += 'category=' + encodeURIComponent(cat) + '&';
  const data = await api(url);
  const panel = document.getElementById('panel');
  if (data.error) { panel.innerHTML = '<div class="empty">' + esc(data.error) + '</div>'; return; }
  if (!data.results || !data.results.length) { panel.innerHTML = '<div class="empty">No memories found</div>'; return; }
  panel.innerHTML = data.results.map(m =>
    '<div class="card">' +
    '<div class="meta">#' + m.id + ' ' + catBadge(m.category) + ' ' + impStars(m.importance) +
    (m.topic ? ' <span style="color:#484f58">[' + esc(m.topic) + ']</span>' : '') +
    ' <span style="color:#484f58">' + (m.created_at||'').slice(0,16) + '</span></div>' +
    '<div class="content">' + esc(m.content) + '</div></div>'
  ).join('');
}

async function loadTopics() {
  const data = await api('/api/topics');
  const panel = document.getElementById('panel');
  if (!data.topics || !data.topics.length) { panel.innerHTML = '<div class="empty">No topics yet</div>'; return; }
  panel.innerHTML = data.topics.map(t =>
    '<div class="topic-card"><div class="name">' + esc(t.name) + ' <span style="color:#484f58">v' + t.version + '</span></div>' +
    '<div style="margin-top:4px">' + esc(t.content) + '</div></div>'
  ).join('');
}

async function loadObservations() {
  const data = await api('/api/observations?limit=50');
  const panel = document.getElementById('panel');
  if (!data.results || !data.results.length) { panel.innerHTML = '<div class="empty">No observations yet</div>'; return; }
  panel.innerHTML = '<h2>Recent Observations</h2>' + data.results.map(o =>
    '<div class="obs-row"><span class="obs-tool">[' + esc(o.tool_name) + ']</span> ' +
    '<span class="obs-input">' + esc(o.tool_input).slice(0,120) + '</span> ' +
    '<span style="color:#484f58">' + (o.timestamp||'').slice(11,19) + '</span></div>'
  ).join('');
}

async function loadSessions() {
  const data = await api('/api/sessions');
  const panel = document.getElementById('panel');
  if (!data.results || !data.results.length) { panel.innerHTML = '<div class="empty">No sessions yet</div>'; return; }
  panel.innerHTML = '<h2>Sessions</h2>' + data.results.map(s =>
    '<div class="card"><div class="meta">#' + s.id + ' ' + esc(s.trigger_type) + ' | ' +
    (s.compacted_at||'').slice(0,16) + ' | ' + s.msg_count + ' msgs</div>' +
    '<div class="content">' + esc((s.brief_summary||'').slice(0,200)) + '</div></div>'
  ).join('');
}

async function loadStats() {
  const data = await api('/api/stats');
  const panel = document.getElementById('panel');
  if (data.error) { panel.innerHTML = '<div class="empty">' + esc(data.error) + '</div>'; return; }
  let html = '<div class="stats-grid">';
  html += '<div class="stat-card"><div class="num">' + (data.n_sessions||0) + '</div><div class="label">Sessions</div></div>';
  html += '<div class="stat-card"><div class="num">' + (data.n_memories||0) + '</div><div class="label">Memories</div></div>';
  html += '<div class="stat-card"><div class="num">' + (data.n_topics||0) + '</div><div class="label">Topics</div></div>';
  html += '<div class="stat-card"><div class="num">' + (data.n_observations||0) + '</div><div class="label">Observations</div></div>';
  html += '</div>';
  if (data.by_category && data.by_category.length) {
    html += '<h2>By Category</h2>';
    data.by_category.forEach(r => {
      html += '<div class="card"><div class="meta">' + catBadge(r.category) + ' ' + r.n + ' entries (avg imp ' + (r.avg_imp||0).toFixed(1) + ')</div></div>';
    });
  }
  panel.innerHTML = html;
}

// Initial load
showTab('memories');
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
class MemoryHandler(BaseHTTPRequestHandler):
    db = None
    pid = None
    memory_dir = None      # set by main(); NEVER derived from os.getcwd()

    # why: BaseHTTPRequestHandler.timeout defaults to None, so handle_one_request
    # blocks forever on a connection that never sends a byte. Browsers
    # speculatively pre-connect, and one such socket used to wedge the whole
    # server. socket.timeout is caught by handle_one_request -> connection closed.
    timeout = 10

    def log_message(self, format, *args):
        _log.debug(format % args)

    # ── response helpers ────────────────────────────────────────────────────

    def _json_response(self, data, status=200, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # NOTE: no Access-Control-Allow-Origin, deliberately. See module docstring.
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html):
        body = html.encode("utf-8")
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _safe_error(self, message, status):
        """Answer a failed request — but never send a second response."""
        if getattr(self, "_responded", False):
            return
        try:
            self._drain_body()
            self._json_response({"error": message}, status)
        except Exception:
            # why: the peer is already gone (broken pipe / reset); there is
            # nothing left to answer and raising here would only kill the
            # worker thread with a traceback on stderr
            pass

    # ── bounded body reads ──────────────────────────────────────────────────

    def _restore_timeout(self):
        """Put the connection back on the normal per-request socket timeout."""
        try:
            self.connection.settimeout(self.timeout)
        except (AttributeError, OSError):
            # why: the peer already closed the socket, so there is no timeout
            # left to restore and raising would kill the worker thread
            pass

    def _timed_read(self, want, deadline):
        """One read of at most `want` bytes that cannot outlive `deadline`.

        `self.timeout` is a PER-recv timeout: every byte the peer sends resets
        it, so a size bound alone is no bound at all — a client dripping one
        byte every 9 s held a worker thread for hours. `deadline` is absolute
        wall clock: total time spent on this body, not time since the last
        byte. Returns b"" at EOF; raises socket.timeout past the deadline or
        after _BODY_STALL_S of silence.
        """
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("request body wall-clock deadline exceeded")
        try:
            self.connection.settimeout(min(left, _BODY_STALL_S))
        except (AttributeError, OSError):
            # why: nothing to set a timeout on (closed socket); the deadline
            # check above still bounds the loop, just less tightly
            pass
        # why read1: it returns after ONE underlying recv, so the deadline is
        # re-checked between drips. read() would block for the whole `want`.
        reader = getattr(self.rfile, "read1", None) or self.rfile.read
        return reader(min(want, 65536))

    def _read_body_bounded(self, length):
        """Read exactly `length` body bytes, or raise _BadRequest.

        A short or stalled body is a CLIENT error: raising _BadRequest answers
        it promptly with a 400 instead of letting socket.timeout escape into
        the generic 500 handler ten seconds later.
        """
        chunks, got = [], 0
        deadline = time.monotonic() + _BODY_DEADLINE_S
        try:
            while got < length:
                chunk = self._timed_read(length - got, deadline)
                if not chunk:
                    raise _BadRequest(
                        f"truncated request body: declared {length} bytes, "
                        f"received {got}")
                chunks.append(chunk)
                got += len(chunk)
        except socket.timeout:
            # Naming BOTH budgets, because either one can be what tripped:
            # a stalled client hits the idle bound, a drip-feeder the total.
            raise _BadRequest(
                f"incomplete request body: declared {length} bytes, received "
                f"{got} (limits: {_BODY_STALL_S:g}s idle, "
                f"{_BODY_DEADLINE_S:g}s total)")
        finally:
            self._restore_timeout()
        return b"".join(chunks)

    def _drain_body(self):
        """Consume an unread request body before answering with an error.

        Writing a response and closing while the peer is still sending its body
        makes Windows answer with a TCP reset, so the client reports
        ConnectionAbortedError instead of reading the 4xx we just wrote.
        Bounded TWO ways: an oversized or unparseable Content-Length is not
        drained at all, AND the drain gives up after _DRAIN_DEADLINE_S of wall
        clock. Courtesy to a well-behaved client must not double as a free
        worker-thread lease for a hostile one — the request being drained here
        has already been rejected.
        """
        if getattr(self, "_body_read", True):
            return
        self._body_read = True
        deadline = time.monotonic() + _DRAIN_DEADLINE_S
        try:
            declared = int(self.headers.get("Content-Length", "0") or 0)
            if declared <= 0 or declared > _MAX_BODY:
                return
            while declared > 0:
                chunk = self._timed_read(declared, deadline)
                if not chunk:
                    break
                declared -= len(chunk)
        except (ValueError, OSError):
            # why: malformed Content-Length, a peer that vanished mid-body, or
            # the drain deadline (socket.timeout IS an OSError) — nothing left
            # to drain, and the error response we are about to write stands
            pass
        finally:
            self._restore_timeout()

    def _reject(self, message, status):
        """Early POST rejection: drain first, then answer."""
        self._drain_body()
        self._json_response({"error": message}, status)

    # ── request guards ──────────────────────────────────────────────────────

    def _host_ok(self):
        """True unless the Host header names something other than this server.

        Without this the viewer is readable by any web page via DNS rebinding:
        attacker.tld resolves to 127.0.0.1, the page is then SAME-origin with
        us, and a same-origin GET carries no Origin header at all — so
        _origin_ok() waves it straight through and /api/sessions hands over
        archive_path. Every browser sends Host, and sends the name the page was
        actually loaded from, which is what makes the rebound name visible here.
        """
        host = self.headers.get("Host")
        if not host:
            # HTTP/1.1 mandates Host and no browser omits it; only a
            # hand-written HTTP/1.0 client can, and that is not a rebind.
            return True
        try:
            u = urlparse("//" + host.strip())
            hostname = (u.hostname or "").lower()
            port = u.port          # raises ValueError on a malformed port
        except ValueError:
            return False
        if hostname not in _LOOPBACK_HOSTS:
            return False
        if port is None:
            # A browser omits the port only when it is the scheme default, and
            # this server never listens on 80/443 by chance of that name.
            return True
        try:
            return int(port) == int(self.server.server_port)
        except (TypeError, ValueError):
            return False

    def _origin_ok(self):
        """True unless a browser told us this request came from another origin.

        Same-origin GETs carry no Origin header at all; a same-origin fetch()
        POST carries exactly our own origin. Anything else is cross-origin.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True            # non-browser client, or a same-origin GET
        if origin == "null":
            return False           # sandboxed iframe / file:// page
        try:
            u = urlparse(origin)
            host = (u.hostname or "").lower()
            port = u.port or (443 if u.scheme == "https" else 80)
        except ValueError:
            return False
        if host not in _LOOPBACK_HOSTS:
            return False
        try:
            return int(port) == int(self.server.server_port)
        except (TypeError, ValueError):
            return False

    def _require_db(self):
        db, pid = self.__class__.db, self.__class__.pid
        if not db:
            self._reject("No database loaded", 500)
            return None, None
        return db, pid

    # ── GET ─────────────────────────────────────────────────────────────────

    def do_GET(self):
        self._responded = False
        try:
            self._handle_get()
        except _BadRequest as e:
            self._safe_error(str(e), 400)
        except Exception as e:
            # why: an escaping exception closes the socket with no response,
            # which every client reports as RemoteDisconnected
            _log.error(f"GET {self.path} failed: {type(e).__name__}: {e}")
            self._safe_error(f"{type(e).__name__}: {e}", 500)

    def _handle_get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        def p(key, default=None):
            return params.get(key, [default])[0]

        if not self._host_ok():
            self._json_response({"error": "invalid Host header"}, 403)
            return

        if not self._origin_ok():
            self._json_response({"error": "cross-origin request rejected"}, 403)
            return

        if path == "/" or path == "":
            self._html_response(_HTML)
            return

        db, pid = self._require_db()
        if not db:
            return

        if path == "/api/memories":
            q = (p("q", "") or "").strip()
            cat = (p("category", "") or "").strip()
            if cat and cat not in _CATEGORIES:
                raise _BadRequest(f"unknown category {cat!r}")
            min_imp = _int(p("importance", "1"), 1, 1, 5, "importance")
            limit = _int(p("limit", "30"), 30, 1, 500, "limit")
            if q:
                # why: FTS5 MATCH expresses NEITHER the category nor the
                # importance predicate, and silently dropping either returned
                # rows the caller had explicitly filtered out. Over-fetch so
                # the post-filter still has `limit` rows to work with.
                post_filtered = bool(cat) or min_imp > 1
                results = db.search_fts(
                    pid, q, limit=min(limit * 5, 500) if post_filtered else limit)
                if cat:
                    results = [r for r in results if r.get("category") == cat]
                if min_imp > 1:
                    results = [r for r in results
                               if (r.get("importance") or 0) >= min_imp]
                if post_filtered:
                    results = results[:limit]
            else:
                # get_recent_memories returns recent-session rows PLUS every
                # session_id IS NULL row (v2.5) — which is what this viewer's own
                # POST writes. Do NOT inflate sessions_back to compensate; that
                # only widens the session window and still misses NULLs.
                results = db.get_recent_memories(
                    pid, sessions_back=5,
                    categories=[cat] if cat else None,
                    min_importance=min_imp, limit=limit,
                )
            self._json_response({"results": results})

        elif path == "/api/topics":
            self._json_response({"topics": db.get_topics(pid)})

        elif path == "/api/observations":
            limit = _int(p("limit", "50"), 50, 1, 1000, "limit")
            self._json_response({"results": db.get_recent_observations(pid, limit=limit)})

        elif path == "/api/sessions":
            with db._connect() as conn:
                rows = conn.execute(
                    """SELECT s.*, COUNT(m.id) AS n_mem
                       FROM sessions s LEFT JOIN memories m ON m.session_id=s.id AND m.is_active=1
                       WHERE s.project_id = ?
                       GROUP BY s.id ORDER BY s.compacted_at DESC LIMIT 20""",
                    (pid,)
                ).fetchall()
            self._json_response({"results": [dict(r) for r in rows]})

        elif path == "/api/stats":
            stats = db.get_stats(pid)
            stats["n_observations"] = db.get_observation_count(pid)
            self._json_response(stats)

        else:
            self._json_response({"error": f"not found: {path}"}, 404)

    # ── POST ────────────────────────────────────────────────────────────────

    def do_POST(self):
        self._responded = False
        self._body_read = False
        try:
            self._handle_post()
        except _BadRequest as e:
            self._safe_error(str(e), 400)
        except Exception as e:
            # why: same contract as do_GET — always answer, never drop the socket
            _log.error(f"POST {self.path} failed: {type(e).__name__}: {e}")
            self._safe_error(f"{type(e).__name__}: {e}", 500)

    def _handle_post(self):
        if not self._host_ok():
            self._reject("invalid Host header", 403)
            return

        if not self._origin_ok():
            self._reject("cross-origin request rejected", 403)
            return

        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            # why: an HTML form can only send urlencoded/multipart/text-plain, so
            # requiring JSON blocks the classic no-preflight cross-site POST
            self._reject("Content-Type must be application/json", 415)
            return

        db, pid = self._require_db()
        if not db:
            return

        length = _int(self.headers.get("Content-Length", "0"), 0, 0, _MAX_BODY,
                      "Content-Length")
        # Claim the stream BEFORE reading: _read_body_bounded raises
        # _BadRequest on a truncated/stalled body, and _safe_error must not
        # then re-enter _drain_body to wait on the same missing bytes again.
        self._body_read = True
        raw = self._read_body_bounded(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise _BadRequest(f"malformed JSON body: {e}")
        if not isinstance(body, dict):
            raise _BadRequest("JSON body must be an object")

        path = urlparse(self.path).path
        if path != "/api/memory":
            self._json_response({"error": f"not found: {path}"}, 404)
            return

        # Validate here rather than leaning on upsert_smart's coercion: a
        # silently down-graded category or clamped importance is indis-
        # tinguishable from an accepted one in the HTTP response.
        content = body.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise _BadRequest("content must be a non-empty string")
        category = body.get("category", "note")
        if category is None or category == "":
            category = "note"
        if not isinstance(category, str):
            raise _BadRequest("category must be a string")
        if category not in _CATEGORIES:
            raise _BadRequest(
                f"unknown category {category!r}; expected one of "
                f"{', '.join(_CATEGORIES)}")
        topic = body.get("topic", "")
        if topic is None:
            topic = ""
        if not isinstance(topic, str):
            raise _BadRequest("topic must be a string")
        importance = _int(body.get("importance", 3), 3, 1, 5, "importance")

        # Anti-patch: route through upsert_smart so similar memories
        # MERGE or SUPERSEDE instead of stacking. See docs/CONTRACTS.md#anti-patch-contract.
        result = upsert_smart(
            db, pid, None,
            category=category,
            content=content,
            importance=importance,
            tags=["web"],
            topic=topic,
        )
        # Refresh MEMORY.md of the SERVED project. This used to resolve
        # os.getcwd(), which is whatever directory the CLI was invoked from —
        # it rewrote a bystander project's MEMORY.md with the served project's
        # contents.
        memory_dir = self.__class__.memory_dir
        try:
            if memory_dir is not None and Path(memory_dir).exists():
                regenerate_memory_index(db, pid, Path(memory_dir))
        except Exception as e:
            # why: best-effort MEMORY.md refresh; the row is already saved,
            # so a regen failure shouldn't fail the HTTP response
            _log.error(f"MEMORY.md regen after POST /api/memory failed: {e}")
        self._json_response(result)

    # ── OPTIONS ─────────────────────────────────────────────────────────────

    def do_OPTIONS(self):
        # why: answering a CORS preflight would re-open the cross-origin write
        # channel; 405 + Allow is the honest answer for a same-origin-only API
        self._responded = False
        try:
            self._json_response({"error": "method not allowed"}, 405,
                                {"Allow": "GET, POST"})
        except Exception as e:
            # why: hook-style contract — a handler must never let an exception
            # escape and drop the socket without a response
            _log.error(f"OPTIONS {self.path} failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    enable_utf8_io()
    parser = argparse.ArgumentParser(description="cc-memory web dashboard")
    parser.add_argument("--port", type=int, default=9377)
    parser.add_argument("--project", default=".")
    parser.add_argument("--no-open", action="store_true", help="Don't open browser")
    args = parser.parse_args()

    project = str(Path(args.project).resolve())
    memory_dir = Path(project) / "memory"
    db_path = memory_dir / "memory.db"
    if not db_path.exists():
        print(f"Error: no memory database at {db_path}")
        sys.exit(1)

    db = MemoryDB(db_path)
    pid = db.upsert_project(project)

    MemoryHandler.db = db
    MemoryHandler.pid = pid
    MemoryHandler.memory_dir = memory_dir

    # 127.0.0.1, never 0.0.0.0 — this API can write memories that are injected
    # into the next Claude session.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), MemoryHandler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}"
    print(f"cc-memory dashboard: {url}")
    print(f"Project: {project}")
    print("Press Ctrl+C to stop")

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
