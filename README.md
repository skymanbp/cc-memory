# cc-memory

**Claude Code persistent memory plugin** — automatic save/restore of conversation context across compactions via SQLite + lifecycle hooks.

## Problem

Claude Code compresses (compacts) conversations when the context window fills up, causing information loss: decisions, experiment results, task lists, and project knowledge disappear.

## Solution

cc-memory hooks into Claude Code's lifecycle events:

1. **PreCompact** — Before compaction, reads the full conversation transcript (JSONL), extracts structured information (decisions, metrics, configs, bugs, tasks), and saves to a per-project SQLite database + markdown files.
2. **SessionStart(compact)** — After compaction, reads the saved memory and injects it into Claude's new context window automatically.

## Features

- **Zero-dependency** — Pure Python stdlib (sqlite3, json, pathlib)
- **Automatic** — No manual intervention; hooks fire on compaction events
- **Per-project** — Each project gets its own `memory/` directory with SQLite DB
- **Structured extraction** — Categorizes memories: decision, result, config, bug, task, architecture, note
- **Importance scoring** — 1-5 scale; critical (5) memories always survive compactions
- **Auto keyword detection** — Builds project-specific vocabulary over sessions
- **CLI query tool** — `mem.py` with full SQL access for learning and debugging
- **Cross-project** — Global hooks; works with any project after init

## Installation

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/cc-memory.git

# Run the installer
python cc-memory/cc_memory/setup.py

# Initialize a project
python cc-memory/cc_memory/setup.py --init /path/to/your/project
```

The installer will:
1. Copy hook scripts to `~/.claude/hooks/cc-memory/`
2. Add PreCompact + SessionStart hooks to `~/.claude/settings.json`
3. Create `memory/` directory in the specified project

## Project Memory Layout

After initialization, each project gets:

```
<project>/memory/
├── memory.db              ← SQLite (primary storage, auto-updated)
├── MEMORY.md              ← Auto-generated index
├── SESSION_HANDOFF.md     ← Current session state (overwritten each compaction)
├── .gitignore             ← Excludes DB + session archives from git
├── sessions/YYYY/MM/      ← Archived session summaries
└── topics/                ← Long-term topic files
```

## CLI Usage (mem.py)

```bash
MEM="python ~/.claude/hooks/cc-memory/mem.py"

# Database statistics
$MEM --project /path/to/project stats

# List memories by category
$MEM --project /path/to/project list decisions
$MEM --project /path/to/project list result --limit 10

# Full-text search
$MEM --project /path/to/project search "F1=0.741"

# Run raw SQL queries
$MEM --project /path/to/project sql "SELECT category, COUNT(*) FROM memories GROUP BY category"
$MEM --project /path/to/project sql "SELECT * FROM memories WHERE importance >= 4 ORDER BY created_at DESC"

# Manually add a memory
$MEM --project /path/to/project add decision "Chose architecture X" --importance 5

# Show project keyword vocabulary
$MEM --project /path/to/project keywords

# View session history
$MEM --project /path/to/project sessions

# Print full database schema (educational)
$MEM --project /path/to/project schema
```

## SQLite Schema

5 tables with proper normalization, foreign keys, and indexes:

- **projects** — One row per project path
- **sessions** — One row per compaction event (timestamp, message count, archive path)
- **memories** — Extracted facts with category, importance (1-5), tags (JSON), active/archived
- **topics** — Long-form knowledge blobs per topic name (versioned)
- **keywords** — Auto-detected project vocabulary with frequency counters

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

## Requirements

- Python 3.8+ (stdlib only, no pip install needed)
- Claude Code with hooks support

## License

MIT
