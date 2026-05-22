---
name: mem-status
description: Check if cc-memory is working. Shows hook registration, database stats, observations, observer activity, API key, supersede chains, and recent logs.
---

## cc-memory Health Check

Run the v2.1 status diagnostic and report the results to the user.

### Step 1 — Run the diagnostic

```bash
python3 ~/.claude/hooks/cc-memory/cc_memory/cli/mem.py --project . status
```

### Step 2 — Summarize

Briefly summarize the output, highlighting:

- Any `[FAIL]` items — plugin files missing, no DB, no API key, etc.
- Any `[WARN]` items — OAuth expired, FTS5 unavailable
- The supersede-chain count from `stats` (means the anti-patch writer is active)
- Whether `PROGRESS:` shows a non-empty current_request (handoff is healthy)

If `[FAIL]` items exist, suggest the fix:
- Missing plugin files → run the installer (`cc-memory-installer.exe`) or
  re-install from source.
- No hooks registered → run installer again, or manually configure
  `~/.claude/settings.json`.
- No API key → set `ANTHROPIC_API_KEY` env var, or sign in via Claude Code so
  `~/.claude/.credentials.json` has an OAuth token.
