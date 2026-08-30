---
description: Query and manage cc-memory state for the current project. Subcommands run the installed plugin's CLI (cc_memory/cli/mem.py on a marketplace/dev checkout, cli/mem.py on a flat standalone install — the resolver below probes both).
argument-hint: "<subcommand> [args]    e.g. stats | search <q> | progress | consolidate | supersedes <id>"
---

## /cc-mem — cc-memory CLI front-end

Run cc-memory CLI commands against the current project. The wrapper resolves
`--project .` for you.

### Subcommands

Every subcommand `cc_memory/cli/mem.py` defines is listed here, and
`tests/smoke_test.py` fails if one is missing — an undocumented subcommand is
one nobody uses, and `sql`'s read-only guard is a security fix that only helps
someone who knows the command exists.

| Subcommand | Effect |
|------------|--------|
| `stats` | Database statistics + supersede-chain count |
| `status` | Full health check (hooks, DB, API key, PROGRESS state) |
| `search <q>` | FTS5 search across memories |
| `list [category]` | List memories (filter by `decision`/`result`/`bug`/...) |
| `add <category> "<text>"` | Add one memory through the anti-patch writer |
| `topics` | Show topic summaries |
| `keywords` | Top project vocabulary by frequency |
| `sessions` | Compaction history with archive paths |
| `observations` | Raw PostToolUse rows still awaiting extraction |
| `schema` | Print the live SQLite schema (tables, indexes, migrations) |
| `paths [--json]` | Print the resolved project artifact paths — database, `PROGRESS.md`, `PLAN.md`, `MEMORY.md` — each with an exists/absent verdict. Read-only: an absent artifact is an answer, not an error. Exists because `status` reports counts with no locations, and hunting for the DB with an rglob found another project's file first |
| `sql "<SELECT ...>" [--json\|--full]` | Run a **read-only** query. Write statements are refused — only plain `SELECT`, `WITH … SELECT`, `EXPLAIN` and read-only `PRAGMA` run, and the `PRAGMA name(value)` setter form is refused too (an `=`-only test used to let it through). The default table truncates cells at 60 chars; `--full` prints untruncated `column: value` blocks, and `--json` emits pure-ASCII JSON (`\uXXXX` escapes), which no capturing shell's decode codec can garble — use it whenever CJK text comes back as `�` |
| `progress` | Force-regenerate `.ccm/PROGRESS.md` from DB and print it |
| `supersedes <id>` | Walk the supersede chain for a memory ID (anti-patch history) |
| `archive <id>... [--supersedes ID]` | Retire memories found to be WRONG: `is_active=0`, recoverable, never `DELETE`. The supported exit from "this stored fact is false" — `sql` is read-only and `add` reconciles only when the new text scores similar enough to the old. `--supersedes` records which memory replaced them, keeping the chain walkable. Refuses ids from another project (`memories.id` is global to the DB file) |
| `consolidate [--deep] [--no-llm]` | Run the full LLM-backed consolidation pipeline, then stamp the cadence marker (so the Stop hook's backpressure probe doesn't re-run what you just ran). `--deep` first loops the semantic-dedup judge until a round confirms nothing new — the way to pay a months-old backlog down in one sitting; already-refused groups are never re-judged within the run |
| `cleanup` | Lightweight no-LLM cleanup + MEMORY.md regen |
| `summary` | Latest session summary (request/done/next_steps) |
| `mode [name]` | Show/set project mode (code/research/writing) |
| `serve [--port N]` | Launch the browser-based web viewer (stdlib http.server) |
| `dashboard` | Launch the Tkinter GUI dashboard for this project |
| `plan-status` | Live-plan counters + freshness summary (no LLM) |
| `plan-show` | Regenerate + print `.ccm/PLAN.md` |
| `plan-set --raw '<text>'` | Capture a raw plan, mark `needs_refine=1` |
| `plan-set --raw-file FILE` | Same, but read raw from a file |
| `plan-set --from-refiner` | Read structured JSON from stdin (refiner output) |
| `plan-check` | Reset guardian counters + emit plan-guardian invocation hint |
| `plan-replan` | Re-arm `needs_refine` on the current raw |
| `plan-clear` | Drop the active plan + delete PLAN.md. Archived to `.ccm/.plan_history/` first; **`--reason "<why>"` is required when unfinished steps exist** (refuses and exits 1 otherwise — v2.4.0 carryover gate) |
| `directive-list [--status active\|blocked\|done\|superseded\|dropped\|all] [--json\|--full]` | Standing user directives, **most-repeated first**. A directive is a unit of user INTENT and outlives every plan; a plan step is a unit of execution and dies with its plan. Default filter is `active`; `blocked` rows show as `[b]`. `--full` lifts the per-field truncation, `--json` emits full rows as pure-ASCII JSON |
| `directive-add <slug> [--quote "..."] [--demand "..."] [--kind standing\|feature\|process\|oneoff\|constraint] [--times N]` | Record a directive. **Re-adding the same slug bumps `times_stated` on the ONE row** rather than creating a second — repetition is the importance signal a plan cannot express. `--times` sets the count outright, for backfilling from a transcript audit. **Reference plan steps by TITLE, never by number** — step ids are re-assigned on every replacement, and the command warns when the text contains `step #N` / `步骤 N` |
| `directive-edit <slug> [--demand ...] [--quote ...] [--kind ...] [--status active\|blocked]` | Correct a directive's record **without bumping its count** — an edit is maintenance, not a re-statement, and `directive-list` sorts by the count. Never creates (exits 1 on an unknown slug). `--status blocked` parks a directive that is waiting on the *user* — idle enforcement skips it — and `--status active` un-parks it; closure stays with `directive-close`, whose evidence gate this door must not bypass |
| `directive-close <slug> --evidence "<checkable>" [--status done\|superseded\|dropped]` | Close a directive. **`--evidence` is mandatory** and refuses an empty value (exits 1): a commit sha, `file:line`, or a gate name. A directive closed on an assertion is the exact failure the ledger exists to prevent |
| `inject-show` | Show exactly what the last SessionStart injected (ground truth) |
| `inject-usage` | Deterministic signals: did Claude actually Read PROGRESS.md/MEMORY.md |
| `encoding-check [--apply]` | Scan for U+FFFD corruption (read-only; `--apply` quarantines) |

> **Directive ledger + Stop enforcement (v2.11).** The `directive-*` commands
> back a ledger that is deliberately **not** plan steps: a plan step dies when
> the plan is replaced, a directive outlives every plan. The Stop hook reads
> both and can **refuse to end a turn** (`{"decision": "block"}`) while a plan
> sits unrefined, a live plan has gone undrift-checked, or an active directive
> has been idle past its threshold. The refusal always escapes: after
> `_BLOCK_MAX_CONSECUTIVE` refusals of the *same* condition set it degrades to
> a loud advisory, and the counter is keyed by a digest of the condition keys,
> so fixing one problem never spends the next one's budget. Kill switch:
> `CC_MEMORY_PLAN_ENFORCE=0`. A project with no `plan_active` row is never
> enforced, so opting in is what turns it on.
>
> **What "idle" counts (v2.11.2).** Turns since *that directive* was last
> written, not since the project was last drift-checked: `turns_total`
> (monotonic, reset by nothing) minus the directive's own `turns_at_touch`.
> Re-stating a directive, editing it, or changing its status restarts its
> clock — those are attention. Running `/cc-mem plan-check` does **not**: it
> resets the drift counter, and if idleness were measured against that, a
> guardian check would forgive a directive nobody had touched in thirty turns.
>
> **Two shapes idle enforcement deliberately skips (v2.12.0).** A directive
> with `--status blocked` is waiting on the *user* (material to deliver, a
> decision to make) — the assistant cannot progress it, so counting its idle
> turns only manufactured re-statements to silence the block, inflating the
> count. And a `--kind constraint` is a standing prohibition ("never commit
> the token") with no recordable positive action by construction: its success
> is that nothing happens, so it is enforced by being injected, never by
> being "worked".

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
# Resolve plugin root. TWO layouts exist and both must be probed:
#   nested — marketplace / dev checkout:  <root>/cc_memory/cli/mem.py
#   flat   — standalone installer output: <root>/cli/mem.py
# (ui/installer.py copies each subpackage to TARGET_DIR/<subdir>/ directly,
#  so a standalone install has NO cc_memory/ segment.)
CCMEM=""
for R in "${CLAUDE_PLUGIN_ROOT}" "$HOME/.claude/hooks/cc-memory"; do
    [ -n "$R" ] || continue
    if   [ -f "$R/cc_memory/cli/mem.py" ]; then CCMEM="$R/cc_memory/cli/mem.py"; break
    elif [ -f "$R/cli/mem.py" ];           then CCMEM="$R/cli/mem.py";           break
    fi
done
if [ -z "$CCMEM" ]; then
    echo "cc-memory plugin not found"; exit 1
fi
python3 "$CCMEM" --project . $ARGUMENTS
```

Then summarize the output to the user. For `progress` and `stats`, give a 1-2
sentence highlight (what's happening, what's stuck). For `supersedes`, show the
chain length and any active head. For `dashboard`/`serve`, just confirm the
launch and stop — the GUI/web viewer lives in its own window/browser tab.

### Anti-patch reminder

When adding a memory via `/cc-mem add <category> "<content>"`, the CLI routes
through `llm.memory_writer.upsert_smart` automatically — so it will merge or
supersede if a similar memory exists rather than stacking. See
`docs/CONTRACTS.md#anti-patch-contract` for the contract.

Reconciliation is similarity-driven, so it handles a **restatement** of a fact,
not a **repudiation** of one. When a stored memory is simply wrong and no new
wording is close enough to supersede it, `archive` is the supported exit —
using it is not a failure of the anti-patch contract, it is the other half.
