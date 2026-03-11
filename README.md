# cc-memory

**Claude Code persistent memory plugin** — automatic LLM-powered save/restore of conversation context across compactions and sessions via SQLite + lifecycle hooks.

## Problem

Claude Code compresses (compacts) conversations when the context window fills up, causing information loss: decisions, experiment results, task lists, and project knowledge disappear. Conversations that end normally (user closes terminal) also lose context.

## Solution

cc-memory provides **three layers of automatic memory capture** so no important information is ever lost:

1. **PreCompact** (hook) — Before compaction, extracts structured memories from the full transcript using Haiku LLM API, saves to SQLite.
2. **SessionStart** (hook) — On any new session, (a) injects saved context into Claude's prompt, and (b) retroactively saves any previous unsaved transcripts.
3. **/save-memories** (skill) — Claude reviews the conversation with its own judgment and saves structured memories. Triggered manually or by CLAUDE.md rules.

Plus a **Stop hook** that reminds Claude to call `/save-memories` before the conversation ends.

## Features

- **LLM-powered extraction** — Uses Claude Haiku API for high-quality, structured memory extraction
- **OAuth auto-detection** — Automatically reads Claude's own OAuth token from `~/.claude/.credentials.json` (no separate API key needed)
- **Token expiry checking** — Validates OAuth token before use; warns when expired
- **Retroactive save** — SessionStart detects unsaved previous transcripts and extracts from them
- **Save status notifications** — SessionStart reports last auto-save result and API key status
- **Zero information loss** — Every conversation boundary (start/end/compaction) is covered
- **Per-project** — Each project gets its own `memory/` directory with SQLite DB
- **Structured memories** — Categorized: decision, result, config, bug, task, architecture, note
- **Importance scoring** — 1-5 scale; critical (5) memories always survive
- **Deduplication** — All save paths check against existing memories before inserting
- **Visual Dashboard** — Tkinter GUI with memory management, plan execution, LLM-powered cleanup
- **Project Registry** — Persistent project list with add/remove/reorder/scan
- **Plan Queue** — Task planning system with Claude Code CLI execution
- **CLI query tool** — `mem.py` with full SQL access
- **Standalone exe** — PyInstaller-built executables for one-click install and dashboard
- **Zero-dependency runtime** — Pure Python stdlib (sqlite3, json, pathlib, tkinter, urllib)

## Installation

### Option 1: Standalone exe (recommended for Windows)

1. Download `cc-memory-installer.exe` from [Releases](https://github.com/skymanbp/cc-memory/releases)
2. Double-click to run
3. Click "Install Plugin" → "Configure Hooks" → select project → "Initialize Project"

### Option 2: From source

```bash
# Clone the repo
git clone https://github.com/skymanbp/cc-memory.git

# Run the installer GUI
python cc-memory/cc_memory/installer.py

# Or CLI setup
python cc-memory/cc_memory/setup.py

# Initialize a project
python cc-memory/cc_memory/setup.py --init /path/to/your/project
```

The installer will:
1. Copy hook scripts to `~/.claude/hooks/cc-memory/`
2. Add PreCompact + SessionStart + Stop hooks to `~/.claude/settings.json`
3. Create `memory/` directory in the specified project

### API Key (automatic)

cc-memory **auto-detects** your Claude OAuth token from `~/.claude/.credentials.json`. No manual API key setup is needed if you're logged into Claude Code.

If auto-detection fails (e.g., running outside Claude Code), you can:

```bash
# Set env var manually
setx ANTHROPIC_API_KEY "sk-ant-..."   # Windows
export ANTHROPIC_API_KEY="sk-ant-..."  # Linux/macOS

# Or use the Dashboard Settings dialog
```

API key resolution order: **manual setting** → **ANTHROPIC_API_KEY env var** → **Claude OAuth token**

### Optional: /save-memories skill

Copy the skill template into any project for Claude-judged memory saves:

```bash
mkdir -p <project>/.claude/skills/save-memories
cp cc_memory/skill_template.md <project>/.claude/skills/save-memories/SKILL.md
```

Then invoke with `/save-memories` during any conversation, or add a CLAUDE.md rule to auto-trigger it.

## Architecture

```
Global (installed once, shared by all projects)
├── ~/.claude/hooks/cc-memory/     ← Plugin code (14 .py files + config.json)
├── ~/.claude/settings.json        ← Hook trigger configuration
└── ~/.claude/.credentials.json    ← OAuth token (read-only, managed by Claude Code)

Per-project (initialized per project)
└── <project>/
    ├── .claude/skills/save-memories/SKILL.md  ← Optional skill
    └── memory/
        ├── memory.db                  ← SQLite database (auto-updated)
        ├── MEMORY.md                  ← Auto-generated index
        ├── SESSION_HANDOFF.md         ← Latest session state
        ├── .last_save.json            ← Last auto-save status
        ├── .gitignore                 ← Excludes DB + sessions from git
        ├── sessions/YYYY/MM/          ← Archived session summaries
        └── topics/                    ← Long-term topic files
```

### Memory Save Flow

```
Conversation in progress
│
├── [Stop hook fires after each response]
│   └── After 8+ turns, reminds Claude to call /save-memories (once per session)
│
├── [User calls /save-memories]
│   ├── Claude reviews conversation with its own judgment
│   ├── Extracts 5-15 structured memories
│   ├── Dedup against existing
│   └── Save via Python command
│
├── [PreCompact hook fires before compaction]
│   ├── Auto-detect API key (env var or OAuth token)
│   ├── Call Haiku API for structured extraction
│   ├── Dedup against existing memories
│   ├── Save to SQLite + update MEMORY.md
│   └── Write .last_save.json status
│
├── [Conversation ends without compaction]
│   └── (no hook fires — handled retroactively)
│
└── [Next session starts — SessionStart hook]
    ├── Job 1: Inject saved context into Claude's prompt
    │   ├── Critical memories (importance=5)
    │   ├── Recent memories (last 3 sessions)
    │   ├── Last session handoff
    │   ├── Last auto-save status report
    │   └── API key status warning (if expired/missing)
    └── Job 2: Retroactive save
        ├── Scan ~/.claude/projects/<hash>/*.jsonl
        ├── Find transcripts not yet in sessions table
        ├── Extract via Haiku API
        ├── Dedup + save to SQLite
        └── Update MEMORY.md
```

## Hooks Configuration

Three hooks are registered in `~/.claude/settings.json`:

| Hook | Trigger | Timeout | Purpose |
|------|---------|---------|---------|
| PreCompact | Before compaction | 30s | Extract + save memories from full transcript |
| SessionStart | Every session start | 10s | Inject context + retroactive save |
| Stop | After each response | 5s | Remind to call /save-memories |

## Visual Dashboard

Launch the dashboard to manage any initialized project:

```bash
# As script
python ~/.claude/hooks/cc-memory/dashboard.py

# Or standalone exe
cc-memory-dashboard.exe
```

6 tabs: **Memories** (search/filter/add) | **Plans** (add/approve/execute) | **Sessions** | **Keywords** | **SQL Console** | **Stats**

### Dashboard Actions

- **Save Session** — Manually trigger memory extraction from the latest transcript (uses Haiku API)
- **Tidy Memories** — LLM-powered cleanup: sends all memories to Haiku API for analysis, identifies garbage/duplicates/mergeable entries, shows confirmation dialog
- **Add Memory** — Manually add a structured memory with category and importance
- **Search** — Full-text search across all memories with category/importance filters
- **Manage Projects** — Persistent project registry with add/remove/reorder/scan
- **Execute Plan** — Launch Claude Code CLI with selected plan content in a new console
- **Settings** — API key configuration with source display (manual/env/OAuth)

## Plan Queue

Task planning system integrated into the memory database:

```bash
PLAN="python ~/.claude/hooks/cc-memory/plan.py --project /path/to/project"

# Add tasks
$PLAN add "Implement feature X" "Write tests for Y" "Deploy Z"

# View active plans
$PLAN list

# Workflow: evaluate -> approve -> execute -> mark done
$PLAN evaluate           # Output plans for Claude to assess
$PLAN approve --all      # Approve all evaluated plans
$PLAN exec --next        # Execute next ready plan
$PLAN done 1 "Completed" # Mark plan as done

# Status overview
$PLAN status

# Cleanup
$PLAN clear
```

Status flow: `draft` → `evaluating` → `ready` → `executing` → `done`/`failed`/`skipped`

Plans can also be managed via the Dashboard GUI (Plans tab) with Execute button launching Claude Code CLI.

## CLI Usage (mem.py)

```bash
MEM="python ~/.claude/hooks/cc-memory/mem.py --project /path/to/project"

# Database statistics
$MEM stats

# List memories by category
$MEM list decisions
$MEM list result --limit 10

# Full-text search
$MEM search "F1=0.741"

# Run raw SQL queries
$MEM sql "SELECT category, COUNT(*) FROM memories GROUP BY category"
$MEM sql "SELECT * FROM memories WHERE importance >= 4 ORDER BY created_at DESC"

# Manually add a memory
$MEM add decision "Chose architecture X" --importance 5

# Show project keyword vocabulary
$MEM keywords

# View session history
$MEM sessions

# Print full database schema
$MEM schema
```

## How Extraction Works

### Primary: LLM extraction (Haiku API)

cc-memory calls `claude-haiku-4-5-20251001` to analyze a condensed transcript (~12K chars) and extract structured memories. The LLM returns a JSON array of `{category, content, importance}` objects.

API key is auto-detected from Claude's OAuth token (`~/.claude/.credentials.json`). If the token is expired or unavailable, extraction is skipped (archive and handoff are still saved).

### Deduplication

All extraction paths (LLM, manual, Claude-judged) check `content.strip().lower()` against existing active memories before inserting. Memories are tagged with their extraction method (`["llm", "auto"]`, `["claude-judged"]`, `["retroactive"]`, `["manual"]`).

## Memory Categories

| Category | What gets extracted | Default importance |
|----------|--------------------|--------------------|
| decision | Explicit choices, confirmations, design changes | 3 |
| result   | Numerical metrics (F1, AUC, loss, accuracy, etc.) | 3 |
| config   | Configuration values, hyperparameters, constants | 2 |
| bug      | Identified and fixed problems, "NEVER do X" warnings | 4 |
| task     | Pending/completed work items | 2 |
| arch     | Model architecture, pipeline design, data flow | 3 |
| note     | Everything else above noise threshold | 1 |

## SQLite Schema

6 tables with proper normalization, foreign keys, and indexes:

- **projects** — One row per project path
- **sessions** — One row per save event (timestamp, message count, trigger type, claude_session_id)
- **memories** — Extracted facts with category, importance (1-5), tags (JSON), active/archived
- **topics** — Long-form knowledge blobs per topic name (versioned)
- **keywords** — Auto-detected project vocabulary with frequency counters
- **plans** — Task queue with status, execution order, feasibility notes, results

## Project Structure

```
cc_memory/                    ← Main package (14 modules)
  __init__.py                 ← Version info
  auth.py                     ← Shared API key resolution (env > OAuth > expiry check)
  config.json                 ← Extraction/injection/archive settings
  db.py                       ← SQLite abstraction (MemoryDB class, 6 tables)
  extractor.py                ← Transcript parsing & structured extraction
  pre_compact.py              ← PreCompact hook entry point
  session_start.py            ← SessionStart hook + retroactive save
  stop.py                     ← Stop hook (save-memories reminder)
  mem.py                      ← CLI query tool (stats/list/search/sql/add)
  plan.py                     ← Plan queue CLI (add/list/approve/exec/done)
  dashboard.py                ← Tkinter visual dashboard GUI (6 tabs)
  installer.py                ← Tkinter GUI installer
  installer_standalone.py     ← Standalone exe entry point
  setup.py                    ← CLI setup script
  skill_template.md           ← /save-memories skill template
build_exe.py                  ← PyInstaller build script
dist/                         ← Built executables
  cc-memory-installer.exe     ← One-click install
  cc-memory-dashboard.exe     ← Visual dashboard
```

## Building exe files

```bash
pip install pyinstaller
python build_exe.py
```

Produces `dist/cc-memory-installer.exe` and `dist/cc-memory-dashboard.exe`.

## Requirements

- Python 3.8+ (stdlib only, no pip install needed)
- Claude Code with hooks support
- PyInstaller (only for building exe, not for running)

## License

MIT
