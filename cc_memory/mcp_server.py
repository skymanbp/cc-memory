#!/usr/bin/env python3
"""
cc-memory/mcp_server.py -- MCP stdio JSON-RPC server
=====================================================
Exposes cc-memory search and management tools via MCP protocol.
Registered as a Claude Code MCP server in ~/.claude/mcp.json.

Protocol: JSON-RPC 2.0 over stdio (stdin/stdout).
stdout is RESERVED for JSON-RPC. All logging goes to file.

Tools:
  memory_search       — FTS5 search across memories (compact results)
  memory_get_details  — Batch fetch full details by IDs
  memory_add          — Add a memory manually
  memory_stats        — Project statistics
  memory_topics       — List topic summaries
  memory_recent       — Recent memories with filters
  memory_timeline     — Unified chronological timeline
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

# Redirect stdout for safety — MCP uses it for JSON-RPC
_original_stdout = sys.stdout
_original_stdin = sys.stdin

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))

from logger import get_logger
_log = get_logger("mcp")

# ---------------------------------------------------------------------------
# Tool definitions for MCP
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "memory_search",
        "description": "Search project memories using full-text search. Returns compact results (id, category, importance, snippet). Use memory_get_details for full content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "project": {"type": "string", "description": "Project path (default: cwd)"},
                "limit": {"type": "integer", "description": "Max results (default: 20)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_get_details",
        "description": "Get full details of memories by IDs. Use after memory_search.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"}, "description": "Memory IDs to fetch"},
                "project": {"type": "string", "description": "Project path (default: cwd)"},
            },
            "required": ["ids"],
        },
    },
    {
        "name": "memory_add",
        "description": "Add a new memory to the project database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["decision", "result", "config", "bug", "task", "arch", "note"]},
                "content": {"type": "string", "description": "Memory content (one concise sentence)"},
                "importance": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1-5 scale"},
                "topic": {"type": "string", "description": "Topic tag (optional)"},
                "project": {"type": "string", "description": "Project path (default: cwd)"},
            },
            "required": ["category", "content", "importance"],
        },
    },
    {
        "name": "memory_stats",
        "description": "Get project memory statistics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project path (default: cwd)"},
            },
        },
    },
    {
        "name": "memory_topics",
        "description": "List all topic summaries for the project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project path (default: cwd)"},
            },
        },
    },
    {
        "name": "memory_recent",
        "description": "Get recent memories with optional filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project path (default: cwd)"},
                "sessions_back": {"type": "integer", "description": "Sessions to look back (default: 3)"},
                "min_importance": {"type": "integer", "description": "Minimum importance (default: 2)"},
                "category": {"type": "string", "description": "Filter by category"},
                "limit": {"type": "integer", "description": "Max results (default: 20)"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _get_db(project_path=None):
    from db import MemoryDB
    project = project_path or os.getcwd()
    db_path = Path(project) / "memory" / "memory.db"
    if not db_path.exists():
        return None, None, "No memory database found for this project"
    db = MemoryDB(db_path)
    pid = db.upsert_project(project)
    return db, pid, None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------
def handle_memory_search(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    query = args.get("query", "")
    limit = args.get("limit", 20)
    results = db.search_fts(pid, query, limit=limit)
    # Compact format: id, category, importance, snippet
    compact = []
    for m in results:
        snippet = m["content"][:80] + "..." if len(m["content"]) > 80 else m["content"]
        compact.append({
            "id": m["id"],
            "category": m["category"],
            "importance": m["importance"],
            "snippet": snippet,
            "topic": m.get("topic", ""),
        })
    return {"results": compact, "count": len(compact)}


def handle_memory_get_details(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    ids = args.get("ids", [])
    if not ids:
        return {"results": []}
    with db._connect() as conn:
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM memories WHERE id IN ({ph}) AND project_id = ?",
            ids + [pid]
        ).fetchall()
    return {"results": [dict(r) for r in rows]}


def handle_memory_add(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    mid = db.insert_memory(
        pid, None, args["category"], args["content"],
        args.get("importance", 3), ["mcp"],
        topic=args.get("topic", ""),
    )
    return {"id": mid, "status": "saved"}


def handle_memory_stats(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    stats = db.get_stats(pid)
    stats["n_observations"] = db.get_observation_count(pid)
    return stats


def handle_memory_topics(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    topics = db.get_topics(pid)
    return {"topics": topics}


def handle_memory_recent(args):
    db, pid, err = _get_db(args.get("project"))
    if err:
        return {"error": err}
    cats = [args["category"]] if args.get("category") else None
    results = db.get_recent_memories(
        pid,
        sessions_back=args.get("sessions_back", 3),
        categories=cats,
        min_importance=args.get("min_importance", 2),
        limit=args.get("limit", 20),
    )
    return {"results": results, "count": len(results)}


_HANDLERS = {
    "memory_search": handle_memory_search,
    "memory_get_details": handle_memory_get_details,
    "memory_add": handle_memory_add,
    "memory_stats": handle_memory_stats,
    "memory_topics": handle_memory_topics,
    "memory_recent": handle_memory_recent,
}


# ---------------------------------------------------------------------------
# JSON-RPC + MCP protocol
# ---------------------------------------------------------------------------
def _send(obj):
    """Write JSON-RPC response to stdout."""
    data = json.dumps(obj, ensure_ascii=False)
    _original_stdout.write(data + "\n")
    _original_stdout.flush()


def _handle_request(req):
    """Process a single JSON-RPC request."""
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        _send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cc-memory", "version": "2.0.0"},
            },
        })
    elif method == "notifications/initialized":
        pass  # No response needed for notifications
    elif method == "tools/list":
        _send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        })
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        handler = _HANDLERS.get(tool_name)
        if not handler:
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            })
            return
        try:
            result = handler(tool_args)
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                },
            })
        except Exception as e:
            _log.error(f"MCP tool error: {tool_name}: {e}")
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                    "isError": True,
                },
            })
    elif method == "ping":
        _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    else:
        if req_id is not None:
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })


def _parent_heartbeat(interval=30):
    """Background thread: exit if parent process dies."""
    ppid = os.getppid()
    while True:
        time.sleep(interval)
        if os.getppid() != ppid:
            _log.info("parent died, exiting MCP server")
            os._exit(0)


def main():
    _log.info("MCP server starting")

    # Redirect sys.stdout to prevent accidental prints breaking protocol
    sys.stdout = open(os.devnull, "w")

    # Start parent heartbeat to prevent orphan
    t = threading.Thread(target=_parent_heartbeat, daemon=True)
    t.start()

    # Main loop: read JSON-RPC from stdin
    for line in _original_stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            _handle_request(req)
        except json.JSONDecodeError:
            _log.error(f"invalid JSON: {line[:100]}")
        except Exception as e:
            _log.error(f"MCP error: {e}")

    _log.info("MCP server exiting (stdin closed)")


if __name__ == "__main__":
    main()
