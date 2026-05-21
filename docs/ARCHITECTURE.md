# cc-memory — Architecture (v2.1)

## TL;DR

cc-memory is a Claude Code plugin that gives Claude **persistent, structured
memory across compactions and sessions**. Three design constraints:

1. **Anti-patch writes.** Every memory save goes through one entry
   (`llm.memory_writer.upsert_smart`) which either **merges** the new content
   into an existing similar memory, **supersedes** an older version (preserving
   a chain), or **inserts** as a new fact — chosen by similarity, not by the
   caller. There is no "append + dedup later" path.

2. **Forced handoff.** At every `SessionStart`, the plugin emits a
   `<system-reminder>` block instructing the next Claude to `Read
   memory/PROGRESS.md` before responding. PROGRESS.md is a single SOT,
   always full-rewritten from the `progress` SQLite table — never appended
   to. The previous v2.0 `SESSION_HANDOFF.md` (which drifted into patch-style
   pollution) is migrated aside.

3. **Single source of truth, no stacking.** Skills, commands, configs, and
   docs each live in exactly ONE place. No `.claude/skills/` AND `skills/`.
   No three copies of `save-memories`. No 6 files claiming different version
   numbers.

## Repository layout

```
cc-memory/
├── .claude-plugin/
│   ├── plugin.json              ← Plugin manifest (v2.1.0)
│   └── marketplace.json         ← /plugin marketplace add entry
├── hooks/hooks.json             ← Hook declarations (5 hooks)
├── skills/                      ← THE canonical skills location
│   ├── save-memories/SKILL.md
│   ├── mem-init/SKILL.md
│   └── mem-status/SKILL.md
├── commands/
│   └── cc-mem.md                ← /cc-mem slash command
├── docs/
│   ├── ARCHITECTURE.md          ← This file
│   ├── MEMORY_RULES.md          ← Anti-patch contract
│   └── HANDOFF_PROTOCOL.md      ← PROGRESS.md spec
├── cc_memory/                   ← Python package (subpackaged)
│   ├── __init__.py
│   ├── config.json
│   ├── core/                    ← Domain: db, extractor, consolidate, idle,
│   │                              progress, privacy, modes, auth, logger
│   ├── hooks/                   ← Hook entry points
│   ├── llm/                     ← ccl_backend (Haiku/Ollama) + memory_writer
│   ├── cli/                     ← mem.py, plan.py
│   ├── mcp/                     ← server.py (MCP stdio)
│   └── ui/                      ← installer, dashboard, web_viewer
├── build_exe.py                 ← PyInstaller build
├── pyproject.toml
├── README.md
├── CLAUDE.md                    ← Project instructions for Claude Code
├── CHANGELOG.md
└── LICENSE
```

## Lifecycle hooks

5 Claude Code hooks are registered (`hooks/hooks.json`):

| Hook | Entry | Timeout | Job |
|------|-------|---------|-----|
| `PreCompact` | [`cc_memory/hooks/pre_compact.py`](../cc_memory/hooks/pre_compact.py) | 45s | LLM extract memories via Haiku; route through `memory_writer.upsert_batch`; FULL-REWRITE `memory/PROGRESS.md`; archive session; maybe trigger LLM consolidation. |
| `SessionStart` | [`cc_memory/hooks/session_start.py`](../cc_memory/hooks/session_start.py) | 15s | Inject layered context (topics / critical / timeline / PROGRESS preview); emit the FORCED `<system-reminder>` to Read `PROGRESS.md`+`MEMORY.md`; retroactive save of unsaved JSONLs. |
| `Stop` | [`cc_memory/hooks/stop.py`](../cc_memory/hooks/stop.py) | 22s | Observer: extract from last turn's observations via Haiku; per-turn `patch_progress(files_touched, ...)`; every 5 turns run `idle.maybe_run_idle` (cleanup + MEMORY.md regen). |
| `PostToolUse` | [`cc_memory/hooks/post_tool_use.py`](../cc_memory/hooks/post_tool_use.py) | 8s | Insert one row into `observations` per tool call (no LLM). |
| `UserPromptSubmit` | [`cc_memory/hooks/user_prompt.py`](../cc_memory/hooks/user_prompt.py) | 8s | Auto-init `memory/` on first contact; track turn count; save prompt for Stop observer; on turn 1, seed `progress.current_request`. |

## Memory model

SQLite tables (defined in [`cc_memory/core/db.py`](../cc_memory/core/db.py)):

| Table | Purpose |
|-------|---------|
| `projects` | One row per project path |
| `sessions` | One row per compaction event |
| `memories` | Extracted facts (category, importance, topic, content_hash, **supersedes_id**) |
| `topics` | Consolidated summaries per topic name (versioned) |
| `keywords` | Auto-detected project vocabulary |
| `plans` | Plan queue (draft → ready → done) |
| `observations` | Raw PostToolUse events (cleaned up after extraction) |
| `session_summaries` | 6-field structured summary per session |
| **`progress`** | NEW in v2.1 — single row per project. SOT for `memory/PROGRESS.md`. |
| `_migrations` | Tracks applied migrations |

The new `supersedes_id` column on `memories` makes the anti-patch chain
explicit: when `upsert_smart` decides a new memory supersedes an old one, the
new row links back to the old row's ID (and the old row is archived). Walking
the chain via `db.get_supersede_chain(memory_id)` shows the full update
history.

## Memory write flow (anti-patch)

```
caller (PreCompact / Stop observer / /save-memories skill / MCP add / mem.py add)
  │
  ▼
llm.memory_writer.upsert_smart(content, topic, category, ...)
  │
  ├─ 1. compute_content_hash → find_by_hash → SKIP if exact match
  │
  ├─ 2. find similar in same topic (Jaccard on trigrams)
  │      │
  │      ├─ sim >= 0.80 → MERGE_IN_PLACE (db.update_memory)
  │      │                  no new row, no stacking
  │      │
  │      ├─ sim >= 0.50 → SUPERSEDE (db.supersede_memory)
  │      │                  archive old, insert new with supersedes_id link
  │      │
  │      └─ sim <  0.50 → fall through to insert
  │
  └─ 3. INSERT NEW (independent fact)
  │
  ▼
regenerate_memory_index(project_id, memory_dir)   ← MEMORY.md always fresh
```

See [docs/MEMORY_RULES.md](MEMORY_RULES.md) for the full contract.

## Handoff flow (forced)

```
PreCompact:
  collect_progress_state(...)
    ↓
  db.upsert_progress(...)         ← full overwrite of progress row
    ↓
  write_progress_md(memory_dir)   ← FULL REWRITE of memory/PROGRESS.md

Stop (every turn):
  db.patch_progress(files_touched=..., trigger_type="stop")
    ↓
  write_progress_md(memory_dir)   ← FULL REWRITE again (idempotent)

UserPromptSubmit (turn 1 only):
  db.patch_progress(current_request=<user msg>, trigger_type="user_prompt")
    ↓
  write_progress_md(memory_dir)

SessionStart:
  inject context blob (topics + critical + timeline + PROGRESS preview)
  emit: <system-reminder>
          You MUST Read memory/PROGRESS.md and memory/MEMORY.md before
          responding to any user request. Explicitly state in your reply:
          "Read PROGRESS.md — prior progress: <summary>."
        </system-reminder>
```

See [docs/HANDOFF_PROTOCOL.md](HANDOFF_PROTOCOL.md) for the PROGRESS.md schema.

## LLM backends

`llm.ccl_backend.call_llm` tries Anthropic Haiku (model
`claude-haiku-4-5-20251001`) first, with a local Ollama fallback configured in
`cc_memory/config.json` (`ccl.ollama_url` + `ccl.local_model`). API key
resolution (`core.auth.get_api_key`) order:

1. `ANTHROPIC_API_KEY` env var
2. OAuth token in `~/.claude/.credentials.json` (auto-detected, with
   `expiresAt` validation)

If both backends fail, hooks degrade gracefully — extraction is skipped, but
archives/handoff/observations still save. Hooks NEVER raise into Claude Code.

## Project mirror

Installed plugin code lives at `~/.claude/hooks/cc-memory/` and mirrors the
`cc_memory/` subpackage layout. The installer copies subdirectories
faithfully (`core/`, `hooks/`, `llm/`, ...) so absolute import paths work in
both environments.

Per-project state lives at `<project>/memory/`:

```
<project>/memory/
├── memory.db                    SQLite (WAL mode, all tables)
├── MEMORY.md                    auto-generated, refreshed every write
├── PROGRESS.md                  full-rewrite from `progress` row, every Stop+PreCompact
├── .last_save.json              status from last PreCompact
├── .gitignore                   excludes DB + sessions
├── sessions/YYYY/MM/            archived per-session summaries
└── topics/                      reserved for future per-topic md exports
```

Old v2.0 `SESSION_HANDOFF.md` files are renamed to `SESSION_HANDOFF.md.v2.bak`
on first PreCompact under v2.1 (one-shot migration in `core.progress`).
