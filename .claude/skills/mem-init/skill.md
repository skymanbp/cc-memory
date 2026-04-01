---
name: mem-init
description: Initialize cc-memory for the current project. Creates memory/ directory, SQLite database, and deploys skills. Run once per project.
disable-model-invocation: false
argument-hint: ""
---

## Initialize cc-memory for This Project

Run this command to set up persistent memory for the current project:

```bash
python3 ~/.claude/hooks/cc-memory/setup.py --init .
```

After initialization, cc-memory will automatically:
- Capture tool observations (PostToolUse)
- Extract memories via AI after each response (Stop observer)
- Save structured memories before compaction (PreCompact)
- Restore context on new sessions (SessionStart)

Report the output to the user.
