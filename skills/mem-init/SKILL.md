---
name: mem-init
description: Verify that cc-memory is initialized for the current project. (Initialization is automatic on the first UserPromptSubmit — this skill is for explicit check / re-init.)
disable-model-invocation: false
argument-hint: ""
---

## Initialize cc-memory for This Project

As of v2.1, **initialization is automatic** — on the very first user message
of any session, the UserPromptSubmit hook creates `memory/`, the SQLite
database, and a `.gitignore`. You normally don't need to do anything.

This skill is for: (a) verifying that auto-init succeeded, (b) re-running
init after a manual `memory/` directory deletion.

### Step 1 — Check existing state

```bash
python3 -c "
from pathlib import Path
p = Path('.').resolve() / 'memory' / 'memory.db'
print(f'memory.db: {\"exists\" if p.exists() else \"MISSING\"}')
print(f'path: {p}')
"
```

### Step 2 — If missing, run the writer's bootstrap

```bash
python3 -c "
import sys
from pathlib import Path
PLUGIN = Path.home() / '.claude' / 'hooks' / 'cc-memory'
sys.path.insert(0, str(PLUGIN / 'cc_memory'))
from core.db import MemoryDB
project = str(Path('.').resolve())
mem_dir = Path(project) / 'memory'
mem_dir.mkdir(parents=True, exist_ok=True)
(mem_dir / 'sessions').mkdir(exist_ok=True)
(mem_dir / 'topics').mkdir(exist_ok=True)
db = MemoryDB(mem_dir / 'memory.db')
db.upsert_project(project)
gi = mem_dir / '.gitignore'
if not gi.exists():
    gi.write_text('memory.db\nmemory.db-wal\nmemory.db-shm\nsessions/\n.last_save.json\n', encoding='utf-8')
print(f'Initialized memory/ at {mem_dir}')
"
```

### Step 3 — Report

Confirm whether init succeeded and what was created.

After initialization (auto or manual), cc-memory's hooks take over:
- `PostToolUse` captures every tool call as an observation
- `Stop` extracts memories every turn via Haiku
- `PreCompact` runs full extraction before any context compression
- `SessionStart` injects context AND emits a forced reminder to read PROGRESS.md
- `UserPromptSubmit` tracks turns + seeds PROGRESS.md current_request on turn 1
