---
description: Query and manage cc-memory state for the current project. Subcommands run the CLI in the installed plugin's cc_memory/cli/mem.py.
argument-hint: "<subcommand> [args]    e.g. stats | search <q> | progress | consolidate | supersedes <id>"
---

## /cc-mem — cc-memory CLI front-end

Run cc-memory CLI commands against the current project. The wrapper resolves
`--project .` for you.

### Common subcommands

| Subcommand | Effect |
|------------|--------|
| `stats` | Database statistics + supersede-chain count |
| `status` | Full health check (hooks, DB, API key, PROGRESS state) |
| `search <q>` | FTS5 search across memories |
| `list [category]` | List memories (filter by `decision`/`result`/`bug`/...) |
| `topics` | Show topic summaries |
| `progress` | Force-regenerate `memory/PROGRESS.md` from DB and print it |
| `supersedes <id>` | Walk the supersede chain for a memory ID (anti-patch history) |
| `consolidate` | Run full LLM-backed consolidation pipeline |
| `cleanup` | Lightweight no-LLM cleanup + MEMORY.md regen |
| `summary` | Latest session summary (request/done/next_steps) |
| `mode [name]` | Show/set project mode (code/research/writing) |

### How to invoke

Run this exact command, substituting `$ARGS`:

```bash
python3 ~/.claude/hooks/cc-memory/cc_memory/cli/mem.py --project . $ARGS
```

Then summarize the output to the user. For `progress` and `stats`, give a 1-2
sentence highlight (what's happening, what's stuck). For `supersedes`, show the
chain length and any active head.

### Anti-patch reminder

When adding a memory via `/cc-mem add <category> "<content>"`, the CLI routes
through `llm.memory_writer.upsert_smart` automatically — so it will merge or
supersede if a similar memory exists rather than stacking. See
`docs/MEMORY_RULES.md` for the contract.
