> **English** · [简体中文](README.zh.md)

# cc-memory

**Claude Code persistent memory plugin (v2.3.3)** — anti-patch reconcile-on-write
with LLM-judged semantic de-duplication, forced PROGRESS.md handoff, live PLAN.md
anchor with plan-refiner / plan-guardian subagents, injection observability, FTS5
search, AI-judged extraction with Haiku + local Ollama fallback.

## What it solves

Claude Code compresses (compacts) conversations when the context window fills up,
causing information loss: decisions, results, todos, and project knowledge
disappear. Conversations that end normally (terminal closed) also lose context.

cc-memory captures structured memories at every conversation boundary AND
**forces the next session to read a handoff document** before it starts work.

## What's new in v2.3.3

- **Documentation multilingual version-control.** English is the canonical
  skeleton; Chinese docs are drift-tracked `*.zh.md` siblings (starting with
  [README.zh.md](README.zh.md)), each tied to a normalized-sha256 of its English
  source recorded in a line-1 marker. A pure-stdlib checker
  ([tools/i18n_check.py](tools/i18n_check.py)) plus a [tests/smoke_test.py](tests/smoke_test.py)
  gate turn red the moment an English doc changes without its translation being
  refreshed. Memory *content* stays language-agnostic — only docs are tracked.
  See [docs/I18N.md](docs/I18N.md).

This is a docs + version-metadata release — no runtime behavior changed.

## What's new in v2.3

- **LLM-judged semantic de-duplication.** The anti-patch writer's char-trigram
  similarity only catches near-verbatim restatement, so the same fact reworded
  each session used to stack up (unbounded DB growth). `consolidate.semantic_dedup`
  nominates small same-category candidate groups by word-Jaccard, Haiku confirms
  same-fact, and the survivor is refreshed to a merged canonical while losers are
  archived (`is_active=0`) with a forward `supersedes_id` link.
- **Obsolescence detection + reference-aware staleness net.** `detect_obsolete_llm`
  names `{stale, current}` pairs with a temporal guard (the superseder must be
  newer) + an anti-event prompt; `decay_and_archive` archives only rows that are
  simultaneously very old, low-importance, AND never injected. All archival is
  recoverable (`is_active=0`, never `DELETE`).
- **Injection observability.** SessionStart writes `memory/.last_inject.json`
  recording exactly which memories/topics were injected and prints a one-line
  recap; `/cc-mem inject-show` dumps ground truth, `/cc-mem inject-usage` reports
  whether Claude actually Read PROGRESS.md / MEMORY.md.
- **`/cc-mem encoding-check [--apply]`** — read-only U+FFFD corruption scan across
  the text tables (valid CJK preserved).

### v2.3.1 / v2.3.2 — "Hook cancelled" permanently fixed

The intermittent `Compacted PreCompact [...] failed: Hook cancelled` is gone.
v2.3.1 raised the PreCompact timeout 45s → 120s, but that only moved the goalpost
on large DBs. **v2.3.2 removes the failure mode**: `PreCompact` now declares two
command hooks — a fast **sync** leg (`hooks/pre_compact.py`, extraction +
PROGRESS.md, ~1-5s) and a background **`async`** leg (`hooks/consolidate_async.py`,
timeout 300s) that runs the every-Nth-session consolidation off the blocking
compaction path. A budget gate with an honest worst-case cost model guarantees the
async worker finishes before its timeout, so it can never be killed mid-write.
See [CHANGELOG.md](CHANGELOG.md).

## What's new in v2.2

- **Live plan anchor (`memory/PLAN.md`).** Captures `ExitPlanMode` output
  (or user-supplied raw plans) into a structured, step-tracked document
  that survives session boundaries. `TodoWrite` syncs step statuses
  mechanically; sensitive Bash calls (`git push`, deploys, ...) flag
  drift. See [docs/PLAN_PROTOCOL.md](docs/PLAN_PROTOCOL.md).
- **Plugin-shipped subagents.** `plan-refiner` normalises raw plans into
  JSON; `plan-guardian` checks alignment when drift counters trip.
  Definitions live in `agents/` and are auto-discovered after install.
- **`/cc-mem dashboard`** subcommand: launches the Tkinter GUI without
  needing to know the plugin install path.

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
2. Adds the hook entries to `~/.claude/settings.json` (6 commands across 5
   events — `PreCompact` declares a sync + an `async` leg).
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

  PreCompact      ─► fires TWO hooks:
                     • sync  (pre_compact.py, 120s): Haiku extracts memories from
                       the full transcript → memory_writer.upsert_smart →
                       FULL-REWRITE memory/PROGRESS.md → archive → regen MEMORY.md
                     • async (consolidate_async.py, 300s, off the blocking path):
                       every-Nth-session LLM consolidation under a time budget

  SessionStart    ─► inject context (topics + critical + timeline + PROGRESS preview)
                     record memory/.last_inject.json
                     emit FORCED <system-reminder>: "Read PROGRESS.md FIRST"
                     retroactive save of unsaved prior JSONLs
```

Per-project state lives at `<project>/memory/`:

```
memory/
├── memory.db                SQLite WAL, see core/db.py for schema
├── MEMORY.md                auto-generated index, refreshed every write
├── PROGRESS.md              full-rewrite from `progress` row at every Stop+PreCompact
├── PLAN.md                  full-rewrite from `plan_active` row (live plan anchor)
├── .last_save.json          status from last PreCompact
├── .last_inject.json        what SessionStart injected (observability)
├── .last_consolidation.json interval marker for the async consolidation leg
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

Memory **content** is language-agnostic — the extractor and resume-signal
detectors recognise both English and Chinese by design, and stored memories may be
in any language. Only the project's own docs follow the English-skeleton +
translation convention. See [docs/I18N.md](docs/I18N.md).

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
/cc-mem inject-show                               # What SessionStart injected last (ground truth)
/cc-mem inject-usage                              # Whether Claude read PROGRESS.md / MEMORY.md
/cc-mem encoding-check                            # Scan text tables for U+FFFD corruption
/cc-mem dashboard                                 # Launch the Tkinter GUI
/cc-mem serve                                     # Launch the browser-based web viewer

# Live plan anchor (v2.2):
/cc-mem plan-status                               # Counters + freshness summary
/cc-mem plan-show                                 # Regenerate + print memory/PLAN.md
/cc-mem plan-set --raw "Build feature X by ..."   # Capture raw plan, mark needs_refine
/cc-mem plan-set --from-refiner                   # Store structured JSON (stdin)
/cc-mem plan-check                                # Reset counters + emit guardian hint
/cc-mem plan-replan                               # Re-arm needs_refine on stored raw
/cc-mem plan-clear                                # Drop the active plan
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
- `consolidation.*` — full LLM consolidation schedule (incl.
  `auto_interval_sessions` for the async leg)
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
tier-3 transcript fallback, legacy `SESSION_HANDOFF.md` migration, the
layout inspector, the two-hook PreCompact shape, and the i18n drift gate.

```bash
python tests/smoke_test.py
# expect a series of [OK] lines ending with "===== ALL SMOKE TESTS PASSED ====="
```

Documentation translations are drift-checked separately:

```bash
python tools/i18n_check.py          # [OK]/[STALE]/[FAIL] per doc; nonzero exit on drift
python tools/i18n_check.py --list   # show every English/翻译 pair + recorded vs current hash
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
- [docs/PLAN_PROTOCOL.md](docs/PLAN_PROTOCOL.md) — PLAN.md + subagent spec
- [docs/I18N.md](docs/I18N.md) — documentation multilingual (English / 中文) version-control
- [CHANGELOG.md](CHANGELOG.md) — version history

## License

MIT
