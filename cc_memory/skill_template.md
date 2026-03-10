---
name: save-memories
description: Save important memories from this conversation to the cc-memory database. Claude reviews the conversation, extracts structured memories with its own judgment, and persists them. Use when the conversation contains decisions, results, bugs, or architecture insights worth preserving.
disable-model-invocation: false
argument-hint: "[optional: focus area or specific things to remember]"
---

## Save Memories to cc-memory Database

Review the current conversation and extract **structured memories** worth preserving across sessions.

### Instructions

1. **Review the conversation** from the beginning (or since the last save). Identify:
   - **Decisions** made (architecture choices, parameter selections, design tradeoffs)
   - **Results** (experiment metrics, benchmarks, performance numbers with context)
   - **Bugs** found and fixed (root cause + fix, especially "NEVER do X" warnings)
   - **Config** changes (hyperparameters, constants, settings that were tuned)
   - **Architecture** insights (model structure, pipeline design, data flow)
   - **Tasks** still pending or blocked

2. **For each memory**, determine:
   - `category`: one of `decision`, `result`, `config`, `bug`, `task`, `arch`, `note`
   - `importance`: 1-5 scale:
     - **5** = Critical, must never forget (data safety rules, fundamental architecture)
     - **4** = Important decision or significant result
     - **3** = Useful context (experiment outcome, config value)
     - **2** = Minor note (might be useful later)
     - **1** = Trivial (skip these, don't save)
   - `content`: One concise sentence. Include specific numbers, file names, or parameter values. No vague descriptions.

3. **Quality rules**:
   - Only save **conclusions**, not the discussion process
   - Each memory should be **self-contained** (understandable without context)
   - Include **specific values**: "GNN D1 F1=0.741" not "GNN performed well"
   - **Deduplicate**: check existing memories first, don't repeat what's already saved
   - Aim for **5-15 memories** per session (quality over quantity)
   - Skip: conversation logistics, tool errors, debugging steps, meta-discussion about memory itself

4. **Save using this exact Python command** (one call per batch):

```bash
python3 -c "
import sys; sys.path.insert(0, str(__import__('pathlib').Path.home() / '.claude/hooks/cc-memory'))
from db import MemoryDB
from pathlib import Path

project = str(Path('.').resolve())
db = MemoryDB(Path(project) / 'memory' / 'memory.db')
pid = db.upsert_project(project)

# Check existing to avoid duplicates
existing = set()
with db._connect() as conn:
    for r in conn.execute('SELECT content FROM memories WHERE project_id=? AND is_active=1', (pid,)):
        existing.add(r['content'].strip().lower())

memories = [
    # ('category', 'content', importance),
    # ADD MEMORIES HERE
]

saved = 0
for cat, content, imp in memories:
    if content.strip().lower() not in existing:
        db.insert_memory(pid, None, cat, content, imp, ['claude-judged'])
        saved += 1
print(f'Saved {saved} new memories ({len(memories)-saved} duplicates skipped)')
"
```

5. **After saving**, report what was saved in a brief summary to the user.

### Example Output Format

Before running the save command, list what you plan to save:

| # | Category | Imp | Content |
|---|----------|-----|---------|
| 1 | decision | 4   | Switched CNN optimizer from Adam to AdamW with weight_decay=1e-4 |
| 2 | result   | 3   | CNN LOCO after optimizer change: bacc=0.523 (+3.4% vs baseline) |
| 3 | bug      | 5   | NEVER run Cell 11 with n_jobs>1 on Windows (HDF5 lock contention) |

Then execute the Python command with these memories.
