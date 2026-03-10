# cc-memory

**Claude Code persistent memory plugin** — automatic save/restore of conversation context across compactions and new sessions via SQLite + lifecycle hooks.

## Problem

Claude Code compresses (compacts) conversations when the context window fills up, causing information loss: decisions, experiment results, task lists, and project knowledge disappear.

## Solution

cc-memory hooks into Claude Code's lifecycle events:

1. **PreCompact** — Before compaction, reads the full conversation transcript (JSONL), extracts structured information (decisions, metrics, configs, bugs, tasks), and saves to a per-project SQLite database + markdown files.
2. **SessionStart** — On any new session (startup, resume, or post-compaction), reads saved memory and injects it into Claude's context automatically.

## Features

- **Zero-dependency** — Pure Python stdlib (sqlite3, json, pathlib, tkinter)
- **Automatic** — No manual intervention; hooks fire on lifecycle events
- **Per-project** — Each project gets its own `memory/` directory with SQLite DB
- **Structured extraction** — Categorizes memories: decision, result, config, bug, task, architecture, note
- **Importance scoring** — 1-5 scale; critical (5) memories always survive compactions
- **Auto keyword detection** — Builds project-specific vocabulary over sessions
- **Plan Queue** — Task planning system with status tracking (draft → ready → executing → done)
- **CLI query tool** — `mem.py` with full SQL access for learning and debugging
- **Visual Dashboard** — Tkinter GUI for browsing, searching, and managing memories
- **Standalone exe** — PyInstaller-built executables for one-click install and dashboard
- **Cross-project** — Global hooks; works with any project after init

## Installation

### Option 1: Standalone exe (recommended for new machines)

1. Download `cc-memory-installer.exe` from releases
2. Double-click to run
3. Click "Install Plugin" → "Configure Hooks" → select project → "Initialize Project"

### Option 2: From source

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/cc-memory.git

# Run the installer GUI
python cc-memory/cc_memory/installer.py

# Or CLI setup
python cc-memory/cc_memory/setup.py

# Initialize a project
python cc-memory/cc_memory/setup.py --init /path/to/your/project
```

The installer will:
1. Copy hook scripts to `~/.claude/hooks/cc-memory/`
2. Add PreCompact + SessionStart hooks to `~/.claude/settings.json`
3. Create `memory/` directory in the specified project

## Architecture

```
Global (installed once, shared by all projects)
├── ~/.claude/hooks/cc-memory/     ← Plugin code (10 .py files)
└── ~/.claude/settings.json        ← Hook trigger configuration

Per-project (initialized per project)
└── <project>/memory/
    ├── memory.db                  ← SQLite database (auto-updated)
    ├── MEMORY.md                  ← Auto-generated index
    ├── SESSION_HANDOFF.md         ← Latest session state
    ├── .gitignore                 ← Excludes DB + sessions from git
    ├── sessions/YYYY/MM/          ← Archived session summaries
    └── topics/                    ← Long-term topic files
```

## Visual Dashboard

Launch the dashboard to manage any initialized project:

```bash
# As script
python ~/.claude/hooks/cc-memory/dashboard.py

# Or standalone exe
cc-memory-dashboard.exe
```

6 tabs: **Memories** (search/filter/add) | **Plans** (add/approve/execute) | **Sessions** | **Keywords** | **SQL Console** | **Stats**

## Plan Queue

Task planning system integrated into the memory database:

```bash
PLAN="python ~/.claude/hooks/cc-memory/plan.py --project /path/to/project"

# Add tasks
$PLAN add "Implement feature X" "Write tests for Y" "Deploy Z"

# View active plans
$PLAN list

# Workflow: evaluate → approve → execute → mark done
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

Plans can also be managed via the Dashboard GUI (Plans tab).

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

# Print full database schema (educational)
$MEM schema
```

## SQLite Schema

6 tables with proper normalization, foreign keys, and indexes:

- **projects** — One row per project path
- **sessions** — One row per compaction event (timestamp, message count, archive path)
- **memories** — Extracted facts with category, importance (1-5), tags (JSON), active/archived
- **topics** — Long-form knowledge blobs per topic name (versioned)
- **keywords** — Auto-detected project vocabulary with frequency counters
- **plans** — Task queue with status, execution order, feasibility notes, results

## Memory Categories

| Category | What gets extracted | Default importance |
|----------|--------------------|--------------------|
| decision | Explicit choices, confirmations, changes | 3 |
| result   | Numerical metrics (F1, AUC, loss, etc.) | 3 |
| config   | UPPER_CASE constant assignments | 2 |
| bug      | Identified and fixed problems | 4 |
| task     | Pending/completed work items, TodoWrite entries | 2 |
| arch     | Model architecture, pipeline design facts | 3 |
| note     | Everything else above noise threshold | 1 |

## How Extraction Works

The PreCompact hook reads Claude Code's transcript JSONL file and uses two strategies:

1. **Structured tool-use parsing** (high quality):
   - `TodoWrite` inputs → perfect task lists
   - `Edit`/`Write` calls → files changed this session
   - `Bash` commands → notable operations run

2. **Text heuristics** (supplementary):
   - Pattern matching for metric=value pairs
   - Category detection via regex (decision keywords, config patterns, etc.)
   - Importance boosting for words like CRITICAL, FIXED, NEVER

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
