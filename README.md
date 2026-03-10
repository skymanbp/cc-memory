# cc-memory

**Claude Code persistent memory plugin** — automatic LLM-powered save/restore of conversation context across compactions and sessions via SQLite + lifecycle hooks.

## Problem

Claude Code compresses (compacts) conversations when the context window fills up, causing information loss: decisions, experiment results, task lists, and project knowledge disappear. Conversations that end normally (user closes terminal) also lose context.

## Solution

cc-memory provides **three layers of automatic memory capture** so no important information is ever lost:

1. **PreCompact** (hook) — Before compaction, extracts structured memories from the full transcript using Haiku LLM API (with regex fallback), saves to SQLite.
2. **SessionStart** (hook) — On any new session, (a) injects saved context into Claude's prompt, and (b) retroactively saves any previous unsaved transcripts it detects.
3. **/save-memories** (skill) — Claude reviews the conversation with its own judgment and saves structured memories. Can be called manually or triggered by CLAUDE.md rules.

## Features

- **LLM-powered extraction** — Uses Claude Haiku API for high-quality, structured memory extraction (regex fallback when API key unavailable)
- **Retroactive save** — SessionStart detects unsaved previous transcripts and extracts from them automatically
- **Zero information loss** — Every conversation boundary (start/end/compaction) is covered
- **Per-project** — Each project gets its own `memory/` directory with SQLite DB
- **Structured memories** — Categorized: decision, result, config, bug, task, architecture, note
- **Importance scoring** — 1-5 scale; critical (5) memories always survive
- **Deduplication** — All save paths check against existing memories before inserting
- **Visual Dashboard** — Tkinter GUI with Save Session, Tidy Memories (LLM-powered cleanup), and full management
- **Plan Queue** — Task planning system with status tracking
- **CLI query tool** — `mem.py` with full SQL access
- **Standalone exe** — PyInstaller-built executables for one-click install and dashboard
- **Zero-dependency runtime** — Pure Python stdlib (sqlite3, json, pathlib, tkinter, urllib)

## Installation

### Option 1: Standalone exe (recommended for new machines)

1. Download `cc-memory-installer.exe` from releases
2. Double-click to run
3. Click "Install Plugin" -> "Configure Hooks" -> select project -> "Initialize Project"

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

### Optional: LLM-powered extraction

Set the `ANTHROPIC_API_KEY` environment variable to enable Haiku API extraction (recommended). Without it, cc-memory falls back to regex-based extraction which is lower quality.

```bash
# Windows
setx ANTHROPIC_API_KEY "sk-ant-..."

# Linux/macOS
export ANTHROPIC_API_KEY="sk-ant-..."
```

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
├── ~/.claude/hooks/cc-memory/     <- Plugin code (10 .py files)
└── ~/.claude/settings.json        <- Hook trigger configuration

Per-project (initialized per project)
└── <project>/
    ├── .claude/skills/save-memories/SKILL.md  <- Optional skill
    └── memory/
        ├── memory.db                  <- SQLite database (auto-updated)
        ├── MEMORY.md                  <- Auto-generated index
        ├── SESSION_HANDOFF.md         <- Latest session state
        ├── .gitignore                 <- Excludes DB + sessions from git
        ├── sessions/YYYY/MM/          <- Archived session summaries
        └── topics/                    <- Long-term topic files
```

### Memory Save Flow

```
Conversation in progress
│
├── [PreCompact hook fires]
│   ├── Try Haiku API extraction (structured, high quality)
│   ├── Fallback to regex extraction if no API key
│   ├── Dedup against existing memories
│   └── Save to SQLite + update MEMORY.md
│
├── [User calls /save-memories]
│   ├── Claude reviews conversation with its own judgment
│   ├── Extracts 5-15 structured memories
│   ├── Dedup against existing
│   └── Save via Python command
│
├── [Conversation ends without compaction]
│   └── (no hook fires — handled retroactively)
│
└── [Next session starts — SessionStart hook]
    ├── Job 1: Inject saved context into Claude's prompt
    └── Job 2: Retroactive save
        ├── Scan ~/.claude/projects/<hash>/*.jsonl
        ├── Find transcripts not yet in sessions table
        ├── Extract via Haiku API (or regex fallback)
        ├── Dedup + save to SQLite
        └── Update MEMORY.md
```

## Visual Dashboard

Launch the dashboard to manage any initialized project:

```bash
# As script
python ~/.claude/hooks/cc-memory/dashboard.py

# Or standalone exe
cc-memory-dashboard.exe
```

8 tabs: **Memories** (search/filter/add) | **Plans** (add/approve/execute) | **Sessions** | **Keywords** | **SQL Console** | **Stats**

### Dashboard Actions

- **Save Session** — Manually trigger memory extraction from the latest transcript (uses Haiku API with regex fallback)
- **Tidy Memories** — LLM-powered cleanup: sends all memories to Haiku API for analysis, identifies garbage/duplicates/mergeable entries, shows confirmation dialog before deletion
- **Add Memory** — Manually add a structured memory with category and importance
- **Search** — Full-text search across all memories with category/importance filters

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

Status flow: `draft` -> `evaluating` -> `ready` -> `executing` -> `done`/`failed`/`skipped`

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

## How Extraction Works

### Primary: LLM extraction (Haiku API)

When `ANTHROPIC_API_KEY` is set, cc-memory calls `claude-haiku-4-5-20251001` to analyze a condensed transcript (~12K chars) and extract structured memories. The LLM returns a JSON array of `{category, content, importance}` objects.

This produces high-quality, self-contained memories with accurate importance scoring.

### Fallback: Regex extraction

When no API key is available, cc-memory uses pattern-based extraction:

1. **Structured tool-use parsing**:
   - `TodoWrite` inputs -> task lists
   - `Edit`/`Write` calls -> files changed
   - `Bash` commands -> notable operations

2. **Text heuristics**:
   - Pattern matching for metric=value pairs
   - Category detection via regex (decision keywords, config patterns, etc.)
   - Importance boosting for words like CRITICAL, FIXED, NEVER

### Deduplication

All extraction paths (LLM, regex, manual) check `content.strip().lower()` against existing active memories before inserting. Memories are tagged with their extraction method (`["llm", "auto"]`, `["regex", "auto"]`, `["claude-judged"]`, `["retroactive"]`).

## Memory Categories

| Category | What gets extracted | Default importance |
|----------|--------------------|--------------------|
| decision | Explicit choices, confirmations, changes | 3 |
| result   | Numerical metrics (F1, AUC, loss, etc.) | 3 |
| config   | UPPER_CASE constant assignments | 2 |
| bug      | Identified and fixed problems | 4 |
| task     | Pending/completed work items | 2 |
| arch     | Model architecture, pipeline design | 3 |
| note     | Everything else above noise threshold | 1 |

## SQLite Schema

6 tables with proper normalization, foreign keys, and indexes:

- **projects** — One row per project path
- **sessions** — One row per save event (timestamp, message count, trigger type, claude_session_id)
- **memories** — Extracted facts with category, importance (1-5), tags (JSON), active/archived
- **topics** — Long-form knowledge blobs per topic name (versioned)
- **keywords** — Auto-detected project vocabulary with frequency counters
- **plans** — Task queue with status, execution order, feasibility notes, results

## Building exe files

```bash
pip install pyinstaller
python build_exe.py
```

Produces `dist/cc-memory-installer.exe` and `dist/cc-memory-dashboard.exe`.

## Requirements

- Python 3.8+ (stdlib only, no pip install needed)
- Claude Code with hooks support
- `ANTHROPIC_API_KEY` environment variable (optional, enables LLM extraction)
- PyInstaller (only for building exe, not for running)

## License

MIT
