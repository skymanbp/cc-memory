---
name: save-memories
description: Save important memories from this conversation to the cc-memory database via the anti-patch upsert path (merge / supersede / insert based on similarity to existing memories).
disable-model-invocation: false
argument-hint: "[optional: focus area or specific things to remember]"
---

## Save Memories to cc-memory Database

Review the current conversation and persist **structured memories** through the
**anti-patch writer** (`llm.memory_writer.upsert_smart`). Never bypass this path:
it auto-decides whether to MERGE (overwrite an existing high-similarity memory),
SUPERSEDE (archive an old version and insert a refined one with a chain link),
or INSERT (genuinely new fact). This prevents stacked duplicates.

### Step 1 — Review the conversation

Identify, since the last save or session start:
- **Decisions** made (architecture choices, parameter selections, tradeoffs)
- **Results** (measurements, benchmarks with specific numbers + units)
- **Bugs** found and fixed (root cause + fix; especially "NEVER do X" warnings)
- **Config** changes (hyperparameters, env vars, settings that were tuned)
- **Architecture** insights (module structure, pipeline design, data flow)
- **Tasks** still pending or blocked

### Step 2 — Score each candidate

- `category`: one of `decision`, `result`, `config`, `bug`, `task`, `arch`, `note`
- `importance`: 1-5 (5=critical/never-forget, 4=important, 3=useful, 2=minor, 1=skip)
- `content`: one self-contained sentence with **specific values** (numbers, file
  paths, parameter names). Bad: "tuned the learning rate". Good: "lr=3e-4 picked
  over 1e-3 because val_loss flatlined after epoch 8."
- `topic`: a short lowercase keyword for grouping (e.g. `auth`, `pipeline`, `ui`)

### Step 3 — Quality bar

- Only **conclusions**, not the discussion process
- Each memory must be understandable WITHOUT the conversation
- 5-15 memories per call (quality > quantity)
- Skip: tool errors, navigation, meta-discussion, conversation logistics
- Do NOT save memories ABOUT the memory plugin itself unless it's a critical bug

### Step 4 — Run this exact command

The writer handles dedup, similarity-based reconcile, and MEMORY.md regen
automatically. Do not call `db.insert_memory` directly.

```bash
python3 -c "
import sys
from pathlib import Path

PLUGIN = Path.home() / '.claude' / 'hooks' / 'cc-memory'
sys.path.insert(0, str(PLUGIN / 'cc_memory'))

from core.db import MemoryDB
from llm.memory_writer import upsert_batch

project = str(Path('.').resolve())
db = MemoryDB(Path(project) / 'memory' / 'memory.db')
pid = db.upsert_project(project)

memories = [
    # {'category': 'decision', 'content': '...', 'importance': 4, 'topic': 'auth'},
    # ADD MEMORIES HERE — see Step 2 for fields
]

counts = upsert_batch(db, pid, None, memories, memory_dir=Path(project) / 'memory')
print(f\"inserted={counts.get('inserted',0)} \"
      f\"merged={counts.get('merged',0)} \"
      f\"superseded={counts.get('superseded',0)} \"
      f\"skipped={counts.get('skipped',0)}\")
"
```

### Step 5 — Report results

Tell the user the breakdown:
- **Inserted**: brand-new facts
- **Merged**: refined an existing high-similarity memory in place
- **Superseded**: replaced an older version of the same fact (preserved as chain)
- **Skipped**: exact duplicates already present

The merged/superseded counts are *good*: they mean the writer is preventing the
patch-style stacking the v2.1 anti-patch contract was designed to stop. See
`docs/MEMORY_RULES.md` for the full contract.
