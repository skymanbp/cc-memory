# cc-memory

**Claude Code persistent memory plugin (v2.1)** — anti-patch reconcile-on-write,
forced PROGRESS.md handoff, FTS5 search, AI-judged extraction with Haiku +
local Ollama fallback.

## What it solves

Claude Code compresses (compacts) conversations when the context window fills up,
causing information loss: decisions, results, todos, and project knowledge
disappear. Conversations that end normally (terminal closed) also lose context.

cc-memory captures structured memories at every conversation boundary AND
**forces the next session to read a handoff document** before it starts work.

## What's new in v2.1

- **Anti-patch writes.** Every save goes through `llm.memory_writer.upsert_smart`,
  which MERGES (overwrites a similar memory in place), SUPERSEDES (archives the
  old, links the new via `supersedes_id`), or INSERTS — chosen by trigram-Jaccard
  similarity. No more stacked duplicates. See [docs/MEMORY_RULES.md](docs/MEMORY_RULES.md).
- **Forced handoff via PROGRESS.md.** `memory/PROGRESS.md` is the single source
  of truth for session handoff, always full-rewritten from a SQL row, never
  appended. SessionStart emits a `<system-reminder>` block that requires the
  next Claude to Read it before responding. See [docs/HANDOFF_PROTOCOL.md](docs/HANDOFF_PROTOCOL.md).
- **Auto-fresh MEMORY.md.** Regenerated after every write — no more 50-day-stale
  index files.
- **Idle reorg.** Stop hook runs lightweight cleanup every 5 turns (no LLM).
- **Clean subpackage layout.** `cc_memory/{core,hooks,llm,cli,mcp,ui}/`.
- **One installer, one skills location, one version number.** Removed `.claude/skills/`
  duplicate, removed the third copy of `save-memories`, removed dual installers.

## Installation

### Via marketplace (recommended once published)

```bash
claude /plugin marketplace add skymanbp/cc-memory
claude /plugin install cc-memory
```

### Local marketplace from this repo

```bash
claude /plugin marketplace add /path/to/cc-memory
claude /plugin install cc-memory
```

### Standalone exe (Windows)

1. Download `cc-memory-installer.exe` from [Releases](https://github.com/skymanbp/cc-memory/releases)
2. Double-click → Install Plugin → Configure Hooks → done.

### From source

```bash
git clone https://github.com/skymanbp/cc-memory.git
python cc-memory/cc_memory/ui/installer.py        # GUI
# or
python cc-memory/cc_memory/ui/installer.py --cli  # CLI
```

The installer:
1. Copies the subpackage tree to `~/.claude/hooks/cc-memory/`.
2. Adds the 5 hook entries to `~/.claude/settings.json`.
3. Auto-detects + upgrades any v2.0 flat-layout install.

Per-project initialization is **automatic** — the first user message creates
`<project>/memory/` and the SQLite DB.

## Architecture at a glance

```
Hooks (registered in ~/.claude/settings.json):

  UserPromptSubmit ─► turn count + first-prompt seeding of PROGRESS.md
                      auto-init memory/ on first contact

  PostToolUse     ─► insert one observation row per tool call (no LLM)

  Stop            ─► Haiku observer extracts memories from this turn
                     patch_progress(files_touched=...)
                     idle reorg every 5 turns

  PreCompact      ─► Haiku extracts memories from the full transcript
                     ALL writes go through llm.memory_writer.upsert_smart
                     FULL-REWRITE memory/PROGRESS.md from SQL row
                     archive session, regen MEMORY.md, maybe LLM-consolidate

  SessionStart    ─► inject context (topics + critical + timeline + PROGRESS preview)
                     emit FORCED <system-reminder>: "Read PROGRESS.md FIRST"
                     retroactive save of unsaved prior JSONLs
```

Per-project state lives at `<project>/memory/`:

```
memory/
├── memory.db                SQLite WAL, see core/db.py for schema
├── MEMORY.md                auto-generated index, refreshed every write
├── PROGRESS.md              full-rewrite from `progress` row at every Stop+PreCompact
├── .last_save.json          status from last PreCompact
├── .gitignore               excludes DB + sessions
├── sessions/YYYY/MM/        per-session archives
└── topics/                  reserved for future per-topic exports
```

## Memory model

| Category | What gets extracted | Default importance |
|----------|--------------------|--------------------|
| `decision` | Explicit choices, design changes | 3 |
| `result`   | Measured outcomes (numbers + units) | 3 |
| `config`   | Hyperparameters, env vars, constants | 2 |
| `bug`      | Identified+fixed problems, "NEVER do X" | 4 |
| `task`     | Pending/blocked work items | 2 |
| `arch`     | Module/pipeline structure, data flow | 3 |
| `note`     | Everything else above noise | 1 |

Importance scale: `1`=noise, `2`=low, `3`=normal, `4`=important, `5`=critical (never forget).

## CLI

**Inside Claude Code** (recommended, path-agnostic):

```
/cc-mem status                                    # Full health check
/cc-mem stats                                     # Memory + supersede-chain counts
/cc-mem list decisions                            # Recent memories by category
/cc-mem search "auth flow"                        # FTS5 search
/cc-mem topics                                    # Topic summaries
/cc-mem progress                                  # Regenerate memory/PROGRESS.md and print
/cc-mem supersedes 42                             # Walk the supersede chain for memory #42
/cc-mem consolidate                               # Full LLM-backed consolidation
/cc-mem cleanup                                   # Lightweight no-LLM cleanup
/cc-mem add decision "Chose X" --importance 4     # Anti-patch upsert
/cc-mem dashboard                                 # Launch the Tkinter GUI
/cc-mem serve                                     # Launch the browser-based web viewer
```

**Outside Claude Code** (shell, standalone-install path shown — adjust for
marketplace install):

```bash
M="python ~/.claude/hooks/cc-memory/cc_memory/cli/mem.py --project ."
$M status
$M search "auth flow"
# ... same subcommands as above
```

## MCP tools

8 tools exposed via `cc_memory/mcp/server.py`:

| Tool | Purpose |
|------|---------|
| `memory_search` | FTS5 search (compact results) |
| `memory_get_details` | Batch fetch full details by IDs |
| `memory_add` | Add via anti-patch upsert |
| `memory_stats` | Project statistics |
| `memory_topics` | List topic summaries |
| `memory_recent` | Recent memories with filters |
| `progress_get` | Read PROGRESS.md state (structured fields) |
| `progress_regenerate` | Force-rewrite memory/PROGRESS.md from SQL state |

Enable via `~/.claude/mcp.json` (set `cc_memory.mcp.auto_register=true` in
`cc_memory/config.json` and re-install).

## Visual Dashboard

```bash
# Marketplace install or standalone — auto-resolves the plugin path:
/cc-mem dashboard

# Or invoke the CLI directly (replace <plugin-root> with your install path):
python <plugin-root>/cc_memory/cli/mem.py --project . dashboard

# Or the standalone exe (Windows):
cc-memory-dashboard.exe
```

6 tabs: Memories · Plans · Sessions · Keywords · SQL Console · Stats.

## Web viewer

```bash
/cc-mem serve
# opens http://127.0.0.1:9377 in your browser
```

## Plan Queue

Task planning system using the same SQLite DB:

```bash
P="python ~/.claude/hooks/cc-memory/cc_memory/cli/plan.py --project ."

$P add "Task A" "Task B" "Task C"
$P list
$P evaluate           # mark draft → evaluating; Claude evaluates feasibility
$P approve --all      # evaluating → ready
$P exec --next        # ready → executing (launches Claude Code CLI)
$P done 1 "Result"    # mark complete
$P status             # queue summary
$P clear              # drop done/failed/skipped
```

Status flow: `draft` → `evaluating` → `ready` → `executing` → `done`/`failed`/`skipped`.

## Configuration

Edit `~/.claude/hooks/cc-memory/cc_memory/config.json`:

- `extraction.*` — extraction caps (sentences, metrics, todos, file changes)
- `writer.*` — anti-patch thresholds (`high_similarity_threshold`,
  `mid_similarity_threshold`)
- `injection.*` — SessionStart token budget and per-layer fractions
- `observation.*` — PostToolUse truncation limits, skip lists
- `idle_reorg.interval_turns` — N turns between idle reorg runs (default 5)
- `consolidation.*` — full LLM consolidation schedule
- `ccl.*` — Ollama fallback URL + model
- `modes.default` — default project mode (code/research/writing)

## API key

cc-memory auto-detects your Claude OAuth token from `~/.claude/.credentials.json`.
No manual API key setup is needed if you're logged into Claude Code.

Resolution order: `ANTHROPIC_API_KEY` env var → Claude OAuth token.

## Tests

`tests/smoke_test.py` is an end-to-end stdlib script (no pytest needed)
that verifies the anti-patch writer decisions, PROGRESS.md full-rewrite,
the fill-only-empty refresh contract, last-wins TodoWrite extraction, the
tier-3 transcript fallback, legacy `SESSION_HANDOFF.md` migration, and
the layout inspector.

```bash
python tests/smoke_test.py
# expect a series of [OK] lines ending with "===== ALL SMOKE TESTS PASSED ====="
```

## Build executables

```bash
pip install pyinstaller
python build_exe.py
# produces:
#   dist/cc-memory-installer.exe
#   dist/cc-memory-dashboard.exe
```

## Requirements

- Python 3.8+ (stdlib only — no pip dependencies at runtime)
- Claude Code with hooks support
- PyInstaller (only for building the exe, not for running)
- On Windows: ensure `python3` resolves to a Python 3 interpreter, since
  `hooks/hooks.json` invokes `python3` and the python.org installer does
  not provide `python3.exe` by default. The simplest fix is to symlink or
  shim `python3` to `python` on PATH.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — full architecture overview
- [docs/MEMORY_RULES.md](docs/MEMORY_RULES.md) — anti-patch write contract
- [docs/HANDOFF_PROTOCOL.md](docs/HANDOFF_PROTOCOL.md) — PROGRESS.md spec
- [CHANGELOG.md](CHANGELOG.md) — version history

## License

MIT
