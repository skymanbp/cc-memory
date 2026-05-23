# CLAUDE.md — Project Instructions for Claude Code

## Project: cc-memory

**Claude Code persistent memory plugin (v2.1)** — anti-patch reconcile-on-write,
forced PROGRESS.md handoff, FTS5 search, AI-judged extraction with Haiku +
local Ollama fallback.

- **Language**: Python 3.8+ (pure stdlib, zero pip dependencies at runtime)
- **Version**: 2.1.0
- **License**: MIT
- **Platform**: Windows-primary, cross-platform compatible (Tkinter required for GUI)

## What changed in v2.1 (over v2.0)

1. **Subpackage layout.** Source is split into
   `cc_memory/{core,hooks,llm,cli,mcp,ui}/`. No more 22-file flat directory.
2. **Anti-patch writes.** `llm.memory_writer.upsert_smart` is the single
   entry for any save path. It MERGES / SUPERSEDES / INSERTS based on
   similarity — no stacking of duplicates. See `docs/MEMORY_RULES.md`.
3. **Forced handoff.** `memory/PROGRESS.md` (new in v2.1) replaces
   `SESSION_HANDOFF.md`. SessionStart emits a `<system-reminder>` block that
   directs the next Claude to `Read memory/PROGRESS.md` BEFORE responding.
   See `docs/HANDOFF_PROTOCOL.md`.
4. **Auto-fresh MEMORY.md.** Regenerated after every batch upsert.
5. **Idle reorg.** Stop hook runs lightweight cleanup every 5 turns (no LLM).
6. **One installer, one skills location, one version number** across all files.

## Repository layout

```
cc-memory/
├── .claude-plugin/
│   ├── plugin.json              ← Plugin manifest (v2.1.0)
│   └── marketplace.json         ← /plugin marketplace add entry
├── hooks/hooks.json             ← 5 hook declarations
├── skills/                      ← THE canonical skills location
│   ├── save-memories/SKILL.md   (routes through memory_writer)
│   ├── mem-init/SKILL.md
│   └── mem-status/SKILL.md
├── commands/cc-mem.md           ← /cc-mem slash command
├── docs/
│   ├── ARCHITECTURE.md
│   ├── MEMORY_RULES.md          ← Anti-patch contract
│   └── HANDOFF_PROTOCOL.md      ← PROGRESS.md spec
├── cc_memory/
│   ├── __init__.py              (version 2.1.0)
│   ├── config.json
│   ├── core/                    db, extractor, consolidate, idle, progress,
│   │                            privacy, modes, auth, logger
│   ├── hooks/                   post_tool_use, pre_compact, session_start,
│   │                            stop, user_prompt
│   ├── llm/                     ccl_backend, memory_writer
│   ├── cli/                     mem, plan
│   ├── mcp/                     server
│   └── ui/                      installer, dashboard, web_viewer
├── build_exe.py
├── pyproject.toml
├── README.md
├── CLAUDE.md                    ← This file
├── CHANGELOG.md
└── LICENSE
```

## Hooks (5)

Registered in `~/.claude/settings.json` and declared in `hooks/hooks.json`:

| Hook | Entry | Timeout | Purpose |
|------|-------|---------|---------|
| `PreCompact` | `cc_memory/hooks/pre_compact.py` | 45s | LLM extract → memory_writer.upsert_batch → FULL-REWRITE PROGRESS.md → archive |
| `SessionStart` | `cc_memory/hooks/session_start.py` | 15s | Inject layered context + FORCED `<system-reminder>` to Read PROGRESS.md |
| `Stop` | `cc_memory/hooks/stop.py` | 22s | Observer (Haiku) + per-turn PROGRESS.md patch + idle reorg every 5 turns |
| `PostToolUse` | `cc_memory/hooks/post_tool_use.py` | 8s | Insert observation row (no LLM) |
| `UserPromptSubmit` | `cc_memory/hooks/user_prompt.py` | 8s | Auto-init memory/ + turn count + seed `progress.current_request` on turn 1 |

Hook contract (NEVER violate):
- Hooks must NEVER write to stderr (Claude Code shows stderr as error UI).
  Use `core.logger.get_logger(...)`; it writes to `~/.claude/hooks/cc-memory/logs/`.
- Hooks must NEVER raise an unhandled exception. Always `sys.exit(0)`.
- Each hook's stdout has a specific role:
  - `SessionStart` stdout → injected context (read by Claude)
  - `Stop` stdout → status line (read by Claude)
  - `PreCompact` stdout → ONE status line (shows in next session's compacted context)
  - `PostToolUse`/`UserPromptSubmit` stdout → empty

## Database schema (10 tables)

Defined in `cc_memory/core/db.py`. See `docs/ARCHITECTURE.md` for full diagram.

- `projects`, `sessions`, `memories`, `topics`, `keywords`, `plans`
- `observations` (PostToolUse events, cleaned after extraction)
- `session_summaries` (6-field structured summary per session)
- `progress` (NEW in v2.1: single row per project, SOT for PROGRESS.md)
- `_migrations` (tracks applied migrations)

Key columns added in v2.1:
- `memories.supersedes_id` — forms the update chain (anti-patch contract)
- `memories.content_hash` — sha256[:16] of normalized content for cheap dedup

## Anti-patch contract

> Every memory save path routes through `llm.memory_writer.upsert_smart`,
> which MERGES in place, SUPERSEDES with a chain link, or INSERTS based on
> trigram-Jaccard similarity. Never call `db.insert_memory` directly from a
> caller path. See `docs/MEMORY_RULES.md` for the full spec.

Save paths converted to use the writer:
- `hooks/pre_compact.py` ✓
- `hooks/stop.py` (observer) ✓
- `cli/mem.py add` ✓
- `mcp/server.py memory_add` ✓
- `skills/save-memories/SKILL.md` ✓ (calls `upsert_batch`)

Not yet converted (still on direct `db.insert_memory` for legacy reasons):
- `ui/dashboard.py` "Add Memory" dialog — slated for v2.2.
- `ui/dashboard.py` "Save Session" — slated for v2.2.

## Forced handoff contract

> `memory/PROGRESS.md` is the single source of truth for session handoff.
> It is ALWAYS full-rewritten from the `progress` SQL row, never appended.
> SessionStart emits a `<system-reminder>` requiring the next Claude to Read
> it BEFORE responding. See `docs/HANDOFF_PROTOCOL.md`.

The `progress` row has 11 user-facing fields (`current_request`, `status_*`,
`open_todos`, `plan`, `critical_context`, `files_touched`, `transcript_ptr`,
`updated_at`, `trigger_type`). It is updated by three paths:
- `PreCompact` does a full overwrite (`upsert_progress`).
- `Stop` patches `files_touched` per turn (`patch_progress`).
- `UserPromptSubmit` patches `current_request` on turn 1 (`patch_progress`).

`SESSION_HANDOFF.md` from v2.0 is renamed to `SESSION_HANDOFF.md.v2.bak` on
first PreCompact under v2.1 (one-shot migration in `core/progress.py`).

## Development guidelines

- **Pure stdlib only at runtime.** Only `sqlite3, json, pathlib, urllib,
  datetime, subprocess, tkinter, time, hashlib, re, http.server`. No pip
  dependencies. PyInstaller is build-time only.
- **Hook safety > anything else.** A broken hook can hang or break Claude
  Code itself. `try: ... except Exception: pass` with a `# why: ...` comment
  is appropriate in hook code. Log to file via `core.logger`.
- **SQL safety.** All queries use parameterized statements. Never use string
  formatting for SQL.
- **OAuth auto-detection.** Always use `core.auth.get_api_key()` for API key
  resolution. Never hardcode key reading.
- **Anti-patch.** Never call `db.insert_memory` directly from a caller path
  — use `llm.memory_writer.upsert_smart` or `upsert_batch`. See
  `docs/MEMORY_RULES.md`.
- **Plugin-agnostic.** Don't add project-specific keywords (e.g. ML/astro
  vocab) to `extractor.py` or `consolidate.py`. Those were removed in v2.1
  for a reason.
- Read files before modifying them; respect the cc-enslaver-style discipline.

## Data & safety rules

- Never delete or overwrite `memory.db` or archived sessions without asking.
- Never fabricate extraction results or memory content.
- Hooks must never block Claude Code — always exit cleanly (`sys.exit(0)`).
- Tag memories with their extraction method (`["llm", "auto"]`,
  `["observer", "realtime"]`, `["manual"]`, `["mcp"]`, `["merged"]`,
  `["supersedes"]`, etc.) for traceability.
- `memory/PROGRESS.md` and `memory/MEMORY.md` are generated artifacts. Edit
  the SQL source of truth (`progress` table for PROGRESS.md, `memories`/
  `topics`/`keywords` for MEMORY.md) instead.

## Build

```bash
pip install pyinstaller
python build_exe.py
# produces dist/cc-memory-installer.exe + dist/cc-memory-dashboard.exe
```

## Sync protocol

**Since v2.1.1 (marketplace registration), no sync to `~/.claude/hooks/` is
needed for code changes on this machine.** Claude Code discovers cc-memory
via `~/.claude/settings.json`:

```jsonc
"enabledPlugins":       { "cc-memory@cc-memory": true },
"extraKnownMarketplaces": {
  "cc-memory": { "source": { "source": "directory",
                             "path": "D:\\Projects\\cc-memory" } }
}
```

`hooks/hooks.json` uses `${CLAUDE_PLUGIN_ROOT}/cc_memory/hooks/<name>.py`,
which resolves to **the git working tree itself**. Editing
`cc_memory/**.py` here updates the live hooks on the next Claude Code
session — no copy step.

`~/.claude/hooks/cc-memory/` only holds `logs/` now (logger output target).

To deploy to another machine without a git checkout, build
`cc-memory-installer.exe` (see `build_exe.py`). That installer lays code
under `~/.claude/hooks/cc-memory/cc_memory/` and registers hooks the v2.0
way — same package, alternate install path.

## See also

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — full architecture overview
- [docs/MEMORY_RULES.md](docs/MEMORY_RULES.md) — anti-patch contract
- [docs/HANDOFF_PROTOCOL.md](docs/HANDOFF_PROTOCOL.md) — PROGRESS.md spec
- [CHANGELOG.md](CHANGELOG.md) — version history
