---
name: mem-status
description: Check if cc-memory is working. Shows hook status, database stats, observations, observer activity, API key, and recent logs.
disable-model-invocation: false
argument-hint: ""
---

## cc-memory Health Check

Run the status diagnostic and report the results to the user.

### Instructions

Run this exact command:

```bash
python3 ~/.claude/hooks/cc-memory/mem.py --project . status
```

Then briefly summarize the results, highlighting any `[FAIL]` or `[WARN]` items that need attention.
