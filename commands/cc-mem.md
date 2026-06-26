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
| `serve [--port N]` | Launch the browser-based web viewer (stdlib http.server) |
| `dashboard` | Launch the Tkinter GUI dashboard for this project |
| `plan-status` | Live-plan counters + freshness summary (no LLM) |
| `plan-show` | Regenerate + print `memory/PLAN.md` |
| `plan-set --raw '<text>'` | Capture a raw plan, mark `needs_refine=1` |
| `plan-set --raw-file FILE` | Same, but read raw from a file |
| `plan-set --from-refiner` | Read structured JSON from stdin (refiner output) |
| `plan-check` | Reset guardian counters + emit plan-guardian invocation hint |
| `plan-replan` | Re-arm `needs_refine` on the current raw |
| `plan-clear` | Drop the active plan + delete PLAN.md |
| `inject-show` | Show exactly what the last SessionStart injected (ground truth) |
| `inject-usage` | Deterministic signals: did Claude actually Read PROGRESS.md/MEMORY.md |
| `encoding-check [--apply]` | Scan for U+FFFD corruption (read-only; `--apply` quarantines) |

> **Memory quality (v2.3).** `consolidate` now also runs LLM-judged **semantic
> de-duplication** (same fact reworded across sessions → merged, recoverable via
> `is_active=0` + `supersedes_id`) and **obsolescence detection** (a newer fact
> that directly contradicts an older one archives the stale one; a temporal
> guard + anti-event prompt prevent historical actions from wrongly obsoleting
> live facts). A reference-aware staleness net only archives very old +
> low-importance + never-injected rows. All archival is recoverable.

### How to invoke

Resolve the CLI path against the plugin root (works for both marketplace
and standalone-exe installs), then run the subcommand:

```bash
# Resolve plugin root: env var (marketplace/plugin context) → standalone install
if [ -n "${CLAUDE_PLUGIN_ROOT}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/cc_memory/cli/mem.py" ]; then
    CCMEM="${CLAUDE_PLUGIN_ROOT}/cc_memory/cli/mem.py"
elif [ -f "$HOME/.claude/hooks/cc-memory/cc_memory/cli/mem.py" ]; then
    CCMEM="$HOME/.claude/hooks/cc-memory/cc_memory/cli/mem.py"
else
    echo "cc-memory plugin not found"; exit 1
fi
python3 "$CCMEM" --project . $ARGS
```

Then summarize the output to the user. For `progress` and `stats`, give a 1-2
sentence highlight (what's happening, what's stuck). For `supersedes`, show the
chain length and any active head. For `dashboard`/`serve`, just confirm the
launch and stop — the GUI/web viewer lives in its own window/browser tab.

### Anti-patch reminder

When adding a memory via `/cc-mem add <category> "<content>"`, the CLI routes
through `llm.memory_writer.upsert_smart` automatically — so it will merge or
supersede if a similar memory exists rather than stacking. See
`docs/MEMORY_RULES.md` for the contract.
