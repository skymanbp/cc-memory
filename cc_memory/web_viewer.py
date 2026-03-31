#!/usr/bin/env python3
"""
cc-memory/web_viewer.py -- Browser-based memory dashboard
=========================================================
Serves a single-page web app on localhost for viewing, searching,
and managing memories. Uses only Python stdlib (http.server).

Launch: python web_viewer.py [--port 9377] [--project PATH]
"""

import argparse
import json
import os
import sys
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))

from db import MemoryDB
from logger import get_logger

_log = get_logger("web")

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
  input, select, button { background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
    padding: 6px 12px; border-radius: 6px; font-size: 14px; }
  input:focus, select:focus { border-color: #58a6ff; outline: none; }
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
  .card .content { margin-top: 4px; }
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

async function api(path) {
  const r = await fetch(API + path);
  return r.json();
}

function impClass(n) { return 'imp imp-' + n; }
function impStars(n) { return '<span class="' + impClass(n) + '">' + '★'.repeat(n) + '☆'.repeat(5-n) + '</span>'; }
function catBadge(c) { return '<span class="cat cat-' + c + '">' + c + '</span>'; }
function esc(s) { return s ? String(s).replace(/</g,'&lt;').replace(/>/g,'&gt;') : ''; }

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

async function doSearch() {
  const q = document.getElementById('search').value;
  const cat = document.getElementById('cat-filter').value;
  let url = '/api/memories?';
  if (q) url += 'q=' + encodeURIComponent(q) + '&';
  if (cat) url += 'category=' + encodeURIComponent(cat) + '&';
  const data = await api(url);
  const panel = document.getElementById('panel');
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

    def log_message(self, format, *args):
        _log.debug(format % args)

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        def p(key, default=None):
            return params.get(key, [default])[0]

        if path == "/" or path == "":
            self._html_response(_HTML)
            return

        db, pid = self.__class__.db, self.__class__.pid
        if not db:
            self._json_response({"error": "No database loaded"}, 500)
            return

        if path == "/api/memories":
            q = p("q", "")
            cat = p("category", "")
            if q:
                results = db.search_fts(pid, q, limit=30)
            else:
                cats = [cat] if cat else None
                results = db.get_recent_memories(
                    pid, sessions_back=5, categories=cats,
                    min_importance=int(p("importance", "1")), limit=30
                )
            self._json_response({"results": results})

        elif path == "/api/topics":
            self._json_response({"topics": db.get_topics(pid)})

        elif path == "/api/observations":
            limit = int(p("limit", "50"))
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
            self.send_error(404)

    def do_POST(self):
        db, pid = self.__class__.db, self.__class__.pid
        if not db:
            self._json_response({"error": "No database loaded"}, 500)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        if self.path == "/api/memory":
            mid = db.insert_memory(
                pid, None,
                body.get("category", "note"),
                body.get("content", ""),
                body.get("importance", 3),
                ["web"],
                topic=body.get("topic", ""),
            )
            self._json_response({"id": mid, "status": "saved"})
        else:
            self.send_error(404)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="cc-memory web dashboard")
    parser.add_argument("--port", type=int, default=9377)
    parser.add_argument("--project", default=".")
    parser.add_argument("--no-open", action="store_true", help="Don't open browser")
    args = parser.parse_args()

    project = str(Path(args.project).resolve())
    db_path = Path(project) / "memory" / "memory.db"
    if not db_path.exists():
        print(f"Error: no memory database at {db_path}")
        sys.exit(1)

    db = MemoryDB(db_path)
    pid = db.upsert_project(project)

    MemoryHandler.db = db
    MemoryHandler.pid = pid

    server = HTTPServer(("127.0.0.1", args.port), MemoryHandler)
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
        server.shutdown()


if __name__ == "__main__":
    main()
