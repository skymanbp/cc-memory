# CLAUDE.md — Project Instructions for Claude Code

## Project: cc-memory

**Claude Code persistent memory plugin** — automatic LLM-powered save/restore of conversation context across compactions and new sessions via SQLite + lifecycle hooks.

- **Language**: Python 3.8+ (pure stdlib, zero external dependencies)
- **Version**: 1.1.0
- **License**: MIT
- **Platform**: Windows-primary, cross-platform compatible

## Problem Solved

Claude Code compresses (compacts) conversations when the context window fills up, causing information loss. Conversations that end normally (user closes terminal) also lose context. cc-memory captures structured memories at every boundary so nothing important is ever lost.

## Architecture Overview

### Three-Layer Memory Capture + Stop Reminder

1. **PreCompact hook** (30s timeout) — Before compaction, extracts structured memories from the full JSONL transcript using Haiku LLM API. Writes to stderr only; stdout must stay empty. Always exits 0 to never block compaction. Writes `.last_save.json` status file.
2. **SessionStart hook** (10s timeout) — On new session, (a) injects saved context into Claude's prompt via stdout (including last save status and API key warnings), and (b) retroactively saves any unsaved previous transcripts.
3. **/save-memories skill** — Claude reviews conversation with its own judgment and saves 5-15 structured memories. Manual or auto-triggered via CLAUDE.md rules.
4. **Stop hook** (5s timeout) — After each Claude response, counts user turns. After 8+ turns, injects a one-time reminder to call `/save-memories` before ending.

### API Key Resolution (auth.py)

Shared module used by all components. Resolution order:
1. `ANTHROPIC_API_KEY` environment variable
2. Claude OAuth token from `~/.claude/.credentials.json` (auto-detected, with `expiresAt` validation)

Returns `(key, source)` where source is `"env"`, `"oauth"`, `"oauth_expired"`, or `""`.

### LLM Extraction

- Calls `claude-haiku-4-5-20251001` on ~12KB condensed transcript
- Returns structured JSON `[{category, content, importance}]`
- If no API key or token expired, extraction is skipped (archive/handoff still saved)
- Regex extraction is **disabled** (produced 78% garbage; only LLM or /save-memories)

### Storage Layout

```
Global (installed once):
  ~/.claude/hooks/cc-memory/     ← All .py files (14 modules + config.json)
  ~/.claude/hooks/cc-memory/projects.json  ← Persistent project registry
  ~/.claude/settings.json        ← Hook trigger configuration

Per-project (initialized per project):
  <project>/memory/
    ├── memory.db                ← SQLite (6 tables, WAL mode)
    ├── MEMORY.md                ← Auto-generated index (loaded into context)
    ├── SESSION_HANDOFF.md       ← Latest session state
    ├── .last_save.json          ← Last PreCompact save status
    ├── .gitignore
    ├── sessions/YYYY/MM/        ← Archived session summaries
    └── topics/                  ← Long-term topic files
```

## Project Structure

```
cc_memory/                      ← Main package (14 modules)
  __init__.py                   ← Version info (1.1.0)
  auth.py                       ← Shared API key resolution (env > OAuth > expiry check)
  config.json                   ← Extraction/injection/archive settings
  db.py                         ← SQLite abstraction (MemoryDB class, 6 tables)
  extractor.py                  ← Transcript parsing & structured extraction
  pre_compact.py                ← PreCompact hook entry point
  session_start.py              ← SessionStart hook + retroactive save
  stop.py                       ← Stop hook (save-memories reminder)
  mem.py                        ← CLI query tool (stats/list/search/sql/add)
  plan.py                       ← Plan queue CLI (add/list/approve/exec/done)
  dashboard.py                  ← Tkinter visual dashboard GUI (6 tabs)
  installer.py                  ← Tkinter GUI installer
  installer_standalone.py       ← Standalone exe entry point
  setup.py                      ← CLI setup script
  skill_template.md             ← /save-memories skill template
build_exe.py                    ← PyInstaller build script
dist/                           ← Built executables
```

## Database Schema (6 tables)

- **projects** — `id, path (UNIQUE), name, created_at, last_active`
- **sessions** — `id, project_id (FK), claude_session_id, trigger_type, compacted_at, msg_count, archive_path, brief_summary`
- **memories** — `id, project_id (FK), session_id (FK nullable), category, content, importance (1-5), tags (JSON), created_at, updated_at, is_active`
- **topics** — `id, project_id (FK), name, content, updated_at, version; UNIQUE(project_id, name)`
- **keywords** — `id, project_id (FK), keyword, frequency, last_seen; UNIQUE(project_id, keyword)`
- **plans** — `id, project_id (FK), content, exec_order, status, feasibility, result, created_at, updated_at`

**Memory categories**: decision, result, config, bug, task, arch, note
**Importance scale**: 1=noise, 2=low, 3=normal, 4=important, 5=critical
**Plan status flow**: draft → evaluating → ready → executing → done/failed/skipped
**Trigger types**: auto, manual_dashboard_llm, retroactive_llm, retroactive_none

## Key APIs

### MemoryDB (db.py)
- `upsert_project(cwd) -> int` — Get or create project ID
- `insert_session(...)` / `get_recent_session_ids(project_id, n)`
- `insert_memory(project_id, session_id, category, content, importance, tags)`
- `get_recent_memories(project_id, sessions_back, categories, min_importance)`
- `get_critical_memories(project_id, min_importance=4)`
- `archive_memory(memory_id)` — Soft-delete (sets is_active=0)
- `upsert_topic(project_id, name, content)` / `get_topics(project_id)`
- `upsert_keywords(project_id, freq_map)` / `get_top_keywords(project_id, n)`
- `add_plan(...)` / `get_plans(...)` / `update_plan_status(...)` / `get_next_plan(...)`
- `get_stats(project_id)` — Returns {n_sessions, n_memories, by_category, last_session}

### auth.py
- `get_api_key() -> (str, str)` — Returns `(key, source)`. Source is "env", "oauth", "oauth_expired", or "".

## Development Guidelines

- **Pure stdlib only** — No pip dependencies at runtime. Only sqlite3, json, pathlib, urllib, datetime, subprocess, tkinter, time.
- **Hook contracts** — PreCompact: stderr only, exit 0 always. SessionStart: stdout = injected context, must complete within timeout. Stop: stdout = reminder text (seen by Claude).
- **Deduplication** — All save paths must check `content.strip().lower()` against existing active memories before inserting.
- **SQL safety** — All queries use parameterized statements. Never use string formatting for SQL.
- **No regex extraction** — Regex fallback is disabled (produced garbage). Only LLM extraction or /save-memories skill.
- **OAuth auto-detection** — Always use `auth.get_api_key()` for API key resolution. Never hardcode key reading.
- Follow existing code conventions and patterns.
- Read files before modifying them.

## Data & Safety Rules

- Never delete or overwrite `memory.db` or archived sessions without asking.
- Never fabricate extraction results or memory content.
- Hooks must never block Claude Code operation — always exit cleanly.
- Tag all memories with their extraction method for traceability.

## Build

```bash
pip install pyinstaller
python build_exe.py
# Produces dist/cc-memory-installer.exe + dist/cc-memory-dashboard.exe
```

## Sync Protocol

Plugin code exists in three locations that must stay in sync:
1. `D:/Projects/cc-memory/cc_memory/` — Git source of truth
2. `C:/Users/skyma/.claude/hooks/cc-memory/` — Installed hooks (active)
3. `D:/Projects/cc-memory/dist/*.exe` — Built executables

After any code change: copy to installed location → git commit → rebuild exe.
