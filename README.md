> **English** · [简体中文](README.zh.md)

<div align="center">

# cc-memory

**Persistent memory for Claude Code.**
Your project's decisions, results, bugs and plans survive compaction, session
boundaries, and closed terminals — the next session is *forced* to read them
before it does anything, and what is stored is *reconciled*, never stacked.

[![version](https://img.shields.io/badge/version-2.12.1-blue.svg)](CHANGELOG.md)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](pyproject.toml)
[![dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)](#requirements)
[![release gates](https://img.shields.io/badge/release%20gates-11-orange.svg)](#release-gates)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#requirements)

</div>

---

## Table of contents

- [What this is](#what-this-is)
- [The problem it attacks](#the-problem-it-attacks)
- [What it does — six capabilities](#what-it-does--six-capabilities)
- [How it works](#how-it-works)
- [Why this one is different](#why-this-one-is-different)
- [Quick start](#quick-start)
- [Seen in the field](#seen-in-the-field)
- [Measured numbers](#measured-numbers)
- [Reference](#reference)
  - [`/cc-mem` — 34 subcommands](#cc-mem--34-subcommands)
  - [`cc-memory-plan` — the plan queue](#cc-memory-plan--the-plan-queue)
  - [MCP tools](#mcp-tools)
  - [Configuration](#configuration)
  - [Per-project files](#per-project-files)
  - [Database schema](#database-schema)
  - [Hook table](#hook-table)
- [Design philosophy](#design-philosophy)
- [Architecture](#architecture)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Roadmap and known limits](#roadmap-and-known-limits)
- [What's new in v2.12.0](#whats-new-in-v2120)
- [Documentation map](#documentation-map)
- [License](#license)

---

## What this is

A Claude Code plugin — six hooks <!--ce:hooks-->, a CLI, an MCP server, a desktop dashboard
and a web viewer over one project-local SQLite database — that captures
structured memories at every conversation boundary, **reconciles** each new
fact against what is already stored instead of appending, hands the next
session a forced-read handoff document, keeps a live plan anchor with an
**enforced** user-intent ledger, and consolidates the store in the background
when the write backlog says it is due. Pure Python standard library, zero
runtime dependencies, everything on your machine.

## The problem it attacks

Claude Code **compacts** a conversation when the context window fills.
Whatever was in the discarded turns is gone: the decision you made three hours
ago, the benchmark number you measured, the bug you already fixed once, the
constraint you stated and had to state again. Sessions that simply end —
terminal closed, laptop shut — lose the same thing, silently.

The usual workarounds do not survive contact with a long project:

| Workaround | Why it fails |
|---|---|
| A hand-written `NOTES.md` | Nobody updates it under deadline; it goes stale and starts lying |
| Pasting context back in each session | Manual, lossy, and costs the tokens you were trying to save |
| Bigger context windows | Delays the compaction; does not remove it |
| Append-only memory files | The same fact gets restated every session and stacks up; the file becomes noise |
| A memory store nobody is forced to read | Injection that the next session may ignore is a suggestion, and suggestions lose to deadlines |

The last two rows are the ones most memory tools stop at, and they are where
this project spends most of its engineering: **writes reconcile** (merge /
supersede / insert, chosen by similarity — never append-and-hope), and **reads
are forced** (a `<system-reminder>` at session start that requires the next
Claude to read the handoff before responding — and a Stop hook that can refuse
to end a turn while plan state is stale).

## What it does — six capabilities

**Capability 1 — capture.** Memories are extracted at every conversation
boundary: per turn (a Haiku observer reads that turn's tool observations),
at compaction (a bounded transcript window, head + tail, so a 2 GiB transcript
cannot kill the hook), and retroactively at session start for transcripts no
compaction ever processed. AI-judged `{category, content, importance, topic}`
records, with a project-neutral regex fallback when no credential is
available. English and Chinese are both first-class.

**Capability 2 — reconcile-on-write (the anti-patch contract).** Every save
path routes through one writer that decides **SKIP** (exact duplicate),
**MERGE** (near-identical restatement, rewritten in place), **SUPERSEDE**
(the fact changed — archive the old row, link the new one to it), or
**INSERT** (genuinely new). Nothing is ever deleted; superseded history stays
walkable. Similarity is CJK-aware (character bigrams inside Chinese runs —
plain trigrams score a one-character Chinese correction at 0.45 and would file
every correction as a new contradictory fact).

**Capability 3 — forced handoff.** `memory/PROGRESS.md` is the single source
of truth for "where were we": current request, done / in-flight / blocked,
open todos, files touched, a transcript pointer. Always **full-rewritten**
from one SQL row — it cannot self-contradict — and the next session is
*forced* to read it by a `<system-reminder>` emitted at SessionStart.
`/cc-mem inject-usage` tells you whether that actually happened.

**Capability 4 — plan anchor + directive ledger, enforced.** `memory/PLAN.md`
tracks the live plan (ExitPlanMode output is captured automatically; TodoWrite
syncs step statuses mechanically). Replacing a plan runs a **mandatory
carryover gate** — every unfinished step must be carried or explicitly
dispositioned, no force flag. Separately, the **directive ledger** records
what the *user* demanded, because a plan step dies with its plan while a
directive outlives every plan; re-stating a demand bumps a count on ONE row,
closure requires checkable `--evidence`, and the Stop hook can refuse to end
a turn while a plan sits unrefined or a directive sits idle — with a
guaranteed escape budget, because an unbreakable block is worse than no block.

**Capability 5 — retrieval and injection.** FTS5 full-text search, topic
summaries, a keyword vocabulary, and a layered SessionStart injection (topics
+ critical memories + recent timeline + PROGRESS preview) under per-layer
budgets. `.last_inject.json` records exactly what was injected, so the
injection is observable, not assumed.

**Capability 6 — consolidation with backpressure (v2.12.0).** Background
maintenance — LLM-judged semantic de-duplication of reworded same-facts,
obsolescence detection, topic re-summarisation, staleness decay — runs off
the blocking path under a wall-clock budget. It triggers on compaction
cadence **or on write backlog** (50 unconsolidated rows, or 7 stale days with
new rows), because the cadence-only trigger starved projects that never
compact: this repository measured 349 memories accumulated in one month
against a 17-day-old consolidation marker. `/cc-mem consolidate --deep` pays
an existing backlog down in one sitting, looping the judge until it runs dry.

## How it works

```
┌──────────────────────── your Claude Code session ────────────────────────┐
│                                                                          │
│  UserPromptSubmit ──▶ create memory/ on first contact, count the turn,    │
│                       seed "what the user asked for" on turn 1           │
│                                                                          │
│  PostToolUse     ──▶ live plan anchor (ExitPlanMode → captured plan,      │
│                       TodoWrite → step sync, edits → drift counters)      │
│                       + one observation row per observed tool call        │
│                                                                          │
│  Stop            ──▶ Haiku reads this turn's observations and writes      │
│                       memories · patches PROGRESS.md · enforces the plan  │
│                       · spawns background consolidation on write backlog  │
│                                                                          │
│  PreCompact      ──▶ sync leg  : extract from a bounded transcript window │
│                       → reconcile → FULL-REWRITE PROGRESS.md → archive    │
│                       async leg: LLM consolidation, off the blocking path │
│                                                                          │
│  SessionStart    ──▶ inject topics + critical memories + timeline, then   │
│                       FORCE: "Read memory/PROGRESS.md before responding"  │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                     <project>/memory/memory.db   (SQLite + FTS5)
                     <project>/memory/PROGRESS.md (handoff, full-rewrite)
                     <project>/memory/PLAN.md     (live plan anchor)
                     <project>/memory/MEMORY.md   (browsable index)
```

Everything is **project-local**. `memory/` lives inside your repository, is
git-ignored by a `.gitignore` cc-memory writes itself, and never leaves your
machine except for the extraction call to Anthropic (which you can scope with
`<private>` tags, or switch off per-project entirely).

## Why this one is different

Where most memory plugins are a vector store plus a prompt, cc-memory is
built as **a data-integrity system that happens to store memories**. The
specifics, each of which cost a measured defect to learn:

- **Writes reconcile at write time, in one transaction.** The whole decision
  tree — hash check, similarity scan, branch write — runs under a single
  `BEGIN IMMEDIATE`, so two concurrent savers of the same sentence
  cannot both insert it. "Append now, dedup later" is the design this project
  exists to reject; later never comes under deadline.
- **Reads are forced, and enforcement is real.** The handoff reminder is not
  a hint — and since v2.11.0 the Stop hook can refuse to close a turn over
  stale plan state, after a real project measured a 51,237-character plan
  sitting unrefined while every plan reader answered from its predecessor,
  and a user demand stated six separate times reached zero implementation.
  Every refusal carries a bounded escape budget and a kill switch
  (`CC_MEMORY_PLAN_ENFORCE=0`): an advisory that never fires and a block that
  never releases are both failure modes, and both have tests.
- **Nothing is interpolated raw into anything Claude reads.** Stored content
  is escaped on the write path and again on every render path, because a
  memory row that can forge a `<system-reminder>` into your next session is a
  permanent prompt injection. The privacy filter fails **closed** (a dangling
  `<private>` tag drops the remainder rather than leaking it), and so does an
  unreadable `config.json` (it excludes every project rather than guessing).
- **The gates can go red, and that is checked.** Eleven release gates run on
  every change — four test suites, four documentation gates, plus build
  checks. A falsification register (`tools/falsify_fixes.py`) reverts each
  registered fix on a temporary copy and asserts its gate actually FAILS
  there: a check that cannot go red is a comment that costs CI time. 166
  registered breakage cases as of v2.12.0, every one driven red individually
  before being kept.
- **Documentation is under the same gates as code.** Every `file.py:LINE`
  citation in the docs is mechanically verified against the tree; every
  counted claim ("all six hooks…" <!--ce:hooks-->) is bound to a set computed from the code;
  the Chinese documentation is hash-bound to its English source and drift
  fails the build; and a gate asks whether each public surface is documented
  at all — in both languages.

## Quick start

### Install — marketplace (recommended)

```bash
claude /plugin marketplace add skymanbp/cc-memory
claude /plugin install cc-memory
```

### Install — from a local checkout

```bash
git clone https://github.com/skymanbp/cc-memory.git
claude /plugin marketplace add ./cc-memory
claude /plugin install cc-memory
```

### Install — standalone, no marketplace

```bash
python cc-memory/cc_memory/ui/installer.py          # GUI
python cc-memory/cc_memory/ui/installer.py --cli    # headless
```

Or download `cc-memory-installer.exe` from
[Releases](https://github.com/skymanbp/cc-memory/releases) on Windows.

### Then

Nothing. Per-project initialization is automatic: the first message you send in
a project creates `<project>/memory/` and its database. Verify with:

```
/cc-mem status
```

To opt a directory out completely, list it in `excluded_projects` — see
[Configuration](#configuration). To find where everything lives:
`/cc-mem paths`.

## Seen in the field

Real output, not mockups. Below: the anti-patch writer refusing to stack, then
a deep consolidation run converging — captured verbatim from a v2.12.0 demo
project on 2026-08-26.

**Write-time reconciliation.** The same fact restated is skipped; a changed
value supersedes its predecessor and the history stays walkable:

```text
$ cc-mem add decision "Use SQLite WAL mode for the memory store"
[inserted] #1 sim=0.00

$ cc-mem add decision "Use SQLite WAL mode for the memory store"     # verbatim restatement
[skipped] #1 (hash_match) sim=1.00

$ cc-mem add config "PreCompact hook timeout is 45 seconds"
[inserted] #5 sim=0.00

$ cc-mem add config "PreCompact hook timeout is 120 seconds"         # the fact CHANGED
[superseded] #5 -> #6 sim=0.77

$ cc-mem supersedes 6
Supersede chain for #6 (2 versions, newest first):
  v2  #6  [ACTIVE]    config   PreCompact hook timeout is 120 seconds
  v1  #5  [archived]  config   PreCompact hook timeout is 45 seconds
```

**Deep consolidation.** Rewordings that score below the lexical thresholds
(a paraphrase measured at sim 0.45 in the same demo) are exactly what the
LLM judge exists for — `--deep` loops it until a round confirms nothing new:

```text
$ cc-mem consolidate --deep
deep dedup round 1: 1 group(s) judged, 1 archived
deep dedup round 2: 1 group(s) judged, 1 archived
deep dedup round 3: 0 group(s) judged, 0 archived
consolidation done: 2 active memories
```

Six writes, two surviving facts, zero data destroyed — every archived row is
`is_active=0` and recoverable.

**What a session sees at start** (the injection is real context, layered under
a token budget, and logged to `.last_inject.json`):

```text
=== CC-MEMORY: Context Restored ===
Project: cc-memory  |  2026-08-26 15:06
### Knowledge Base (by topic)
**[testing]** Testing infrastructure spans five core suites: …
**[release]** The cc-memory project has completed release cycles through v2.11 …
### Critical memories
- #506 [release] v2.9.0 released with commit 0313339, tag v2.9.0 …
### <system-reminder>
You MUST Read memory/PROGRESS.md before responding …
```

**And the failures it caught in real projects** — the measurements that drove
the last three releases, kept here because they are the honest pitch:

| Incident (project, date) | What cc-memory's machinery did |
|---|---|
| A 51,237-char raw plan sat unrefined while PLAN.md, `plan-status` and the drift guardian all answered from the *previous* plan; a demand stated 6× reached zero implementation (`lore_disaster`, 2026-08-15) | Forced the v2.11.0 redesign: Stop-hook **enforcement** with an escape budget, and the directive ledger — intent that outlives plans |
| Two plan renumberings left 11 dead "step #N" references in directive text and 4 that *silently pointed at the wrong step* (`Autoshop`, 2026-08-25) | v2.12.0: `plan-set` audits every active directive on replacement and names each reference `DEAD` or `SILENTLY RETARGETED`; the carryover gate itself had already refused two malformed replacements in the same session |
| 349 memories accumulated in one month with consolidation last run 17 days earlier; injected topic summaries were three minor versions stale (this repository, 2026-08-26) | v2.12.0: the backpressure trigger — consolidation now runs when the *write backlog* says so, not only when compactions happen |

## Measured numbers

Not a synthetic benchmark suite — these are the before/after measurements the
fixes were justified with, reproduced from [CHANGELOG.md](CHANGELOG.md), each
tagged with the release that measured it.

| What | Before | After | Where measured |
|---|---|---|---|
| Loading a 2.11 GiB transcript in the PreCompact hook | ~88 s (hook killed) | 1.66 s (full hook 14.33 s) | v2.4.2 |
| Privacy filter on 16,000 unterminated `<private>` tags | 9,517.4 ms, tail **leaked** | 0.0 ms, tail dropped (fail-closed) | v2.5.0 |
| One-character correction to a 10-char Chinese fact, similarity score | 0.45 → filed as a *new contradictory fact* | CJK bigrams → reconciled | v2.8.0 |
| Same-fact scan on a 51-memory topic (cap was 50) | 0.95-similar row never compared → duplicate inserted | scanned, reconciled (cap 500, truncation logged) | v2.5.5 |
| Two concurrent savers of one sentence | both inserted (2 rows, 1 hash) | one transaction → 1 row | v2.8.0 |
| Session-recency query at 2,000 sessions | 557.68 ms (quadratic) | 4.31 ms (indexed) | v2.8.0 |
| MEMORY.md 0-byte reads under concurrent writers | 4,867 of 16,071 samples | 0 (atomic tmp + `os.replace`) | v2.5.2 |
| MCP stdio, non-ASCII payload round-trip on a GBK box | 1 of 7 | 7 of 7 (forced UTF-8) | v2.5.0 |
| Web viewer under one idle TCP connection | wedged permanently | 200 in 0.02 s (threaded + deadlines) | v2.5.0 |
| Stop hook worst case with stalled LLM legs (22 s budget) | 25.45 s (killed mid-write) | 15.99 s (absolute deadline) | v2.5.0 |
| Consolidation on a no-compaction workflow | never ran (349 rows / 17 days) | due at 50 rows or 7 stale days | v2.12.0 |
| Doc citations verified mechanically, first run | 163 of 594 stale | 0 stale, gated on every change | v2.5.2 |

Costs are measured too, not only wins: closing every DB connection per
operation costs +340 % per op (+0.6 s on a 120 s compaction budget) and was
kept anyway, because a leaked WAL handle is worse — the reasoning is in
[CHANGELOG.md](CHANGELOG.md) under v2.5.2.

---

## Reference

### `/cc-mem` — 34 subcommands

Inside Claude Code (path-agnostic — the wrapper resolves the plugin root):

```
# ── state and health ───────────────────────────────────────────────────────
/cc-mem status                      Full health check (hooks, DB, API key, PROGRESS)
/cc-mem stats                       Memory counts + supersede-chain count
/cc-mem paths [--json]              Resolved DB / PROGRESS.md / PLAN.md / MEMORY.md paths
/cc-mem schema                      Live SQLite schema (tables, indexes, migrations)
/cc-mem mode [code|research|writing] Show or set the project mode
/cc-mem summary                     Latest session summary
/cc-mem sessions                    Compaction history with archive paths
/cc-mem observations                Raw PostToolUse rows awaiting extraction

# ── reading memory ─────────────────────────────────────────────────────────
/cc-mem search "<query>"            FTS5 search
/cc-mem list [category]             Recent memories, optionally by category
/cc-mem topics                      Topic summaries
/cc-mem keywords                    Project vocabulary by frequency
/cc-mem supersedes <id>             Walk a memory's supersede chain
/cc-mem sql "<SELECT ...>" [--json|--full]   Read-only query (writes refused)

# ── writing memory ─────────────────────────────────────────────────────────
/cc-mem add <category> "<text>" [--importance N]   Anti-patch upsert
/cc-mem archive <id>... [--supersedes ID]          Retire a WRONG fact (recoverable)
/cc-mem consolidate [--deep]        Full LLM-backed consolidation; --deep loops
                                    the dedup judge until it runs dry
/cc-mem cleanup                     Lightweight no-LLM cleanup + MEMORY.md regen
/cc-mem encoding-check [--apply]    U+FFFD corruption scan

# ── handoff ────────────────────────────────────────────────────────────────
/cc-mem progress                    Regenerate memory/PROGRESS.md and print it
/cc-mem inject-show                 What the last SessionStart injected
/cc-mem inject-usage                Did Claude actually read PROGRESS.md / MEMORY.md

# ── live plan anchor ───────────────────────────────────────────────────────
/cc-mem plan-status                 Counters + freshness summary
/cc-mem plan-show                   Regenerate + print memory/PLAN.md
/cc-mem plan-set --raw "<text>"     Capture a raw plan, mark needs_refine
/cc-mem plan-set --raw-file FILE    Same, from a file
/cc-mem plan-set --from-refiner     Store structured JSON from stdin (audits
                                    directive step references on replacement)
/cc-mem plan-check                  Reset drift counters + emit guardian hint
/cc-mem plan-replan                 Re-arm needs_refine on the stored raw
/cc-mem plan-clear --reason "<why>" Drop the active plan (reason required if unfinished)

# ── directive ledger ───────────────────────────────────────────────────────
/cc-mem directive-list [--status active|blocked|done|superseded|dropped|all] [--json|--full]
/cc-mem directive-add <slug> --demand "..." [--quote "..."] [--kind ...] [--times N]
/cc-mem directive-edit <slug> [--demand ...] [--quote ...] [--kind ...] [--status active|blocked]
/cc-mem directive-close <slug> --evidence "<commit|file:line|gate>"

# ── interfaces ─────────────────────────────────────────────────────────────
/cc-mem dashboard                   Launch the Tkinter GUI
/cc-mem serve [--port N]            Launch the loopback web viewer
```

Three output conventions worth knowing: `--full` lifts the 60-char table
truncation; `--json` emits **pure-ASCII** JSON (`\uXXXX` escapes), which no
capturing shell's decode codec can garble — use it whenever CJK text comes
back as `�`; and `directive-edit` corrects a record **without** bumping the
repetition count (`directive-add` is the only path that counts).

Full per-subcommand semantics: [commands/cc-mem.md](commands/cc-mem.md).

Outside Claude Code:

```bash
# NOTE: $HOME, not ~. Bash expands a tilde BEFORE parameter expansion and does
# not rescan the result, so a ~ stored inside a variable stays a literal.
M="python $HOME/.claude/hooks/cc-memory/cli/mem.py --project ."   # flat install
$M status
$M search "auth flow"
```

### `cc-memory-plan` — the plan queue

A task queue in the same database, distinct from the live plan **anchor**.

```bash
P="cc-memory-plan --project ."       # or python .../cli/plan.py --project .

$P add "Task A" "Task B" "Task C"    # append drafts
$P list                              # show the queue
$P reorder <id> <position>           # move a task
$P evaluate                          # draft → evaluating
$P set-eval <id> "<verdict>"         # record a feasibility verdict
$P approve --all                     # evaluating → ready
$P exec --next                       # ready → executing, print the plan text
$P done <id> "<result>"              # → done
$P fail <id> "<why>"                 # → failed
$P skip <id> "<why>"                 # → skipped
$P status                            # queue summary
$P clear                             # drop done/failed/skipped
```

Flow: `draft → evaluating → ready → executing → done | failed | skipped`.
`exec` spawns nothing — it flips status and prints the plan text plus the
`done` command to run afterwards. Every subcommand that names an id resolves it
within `--project` first and exits 1 on an unknown or foreign id.

### MCP tools

8 tools over JSON-RPC 2.0 stdio. The server forces UTF-8 with LF newlines on
both stdin and stdout itself — **no `PYTHONUTF8` / `PYTHONIOENCODING` env block
is required**.

| Tool | Purpose |
|---|---|
| `memory_search` | FTS5 search, compact results |
| `memory_get_details` | Batch fetch full details by id |
| `memory_add` | Add through the anti-patch writer |
| `memory_stats` | Project statistics |
| `memory_topics` | Topic summaries (bounded) |
| `memory_recent` | Recent memories with filters |
| `progress_get` | Read the PROGRESS state as structured fields |
| `progress_regenerate` | Force-rewrite `memory/PROGRESS.md` from SQL |

**Marketplace / dev checkout — nothing to do.** `.claude-plugin/plugin.json`
ships the registration inline.

**Standalone install — register by hand.** Note the absent `cc_memory/` path
segment in the flat layout:

```jsonc
// <project>/.mcp.json, or the user-scoped equivalent
{
  "mcpServers": {
    "cc-memory": {
      "command": "python3",
      "args": ["<HOME>/.claude/hooks/cc-memory/mcp/server.py"]
    }
  }
}
```

### Configuration

`config.json` in your install root — flat: `~/.claude/hooks/cc-memory/config.json`;
marketplace / dev checkout: `<plugin-root>/cc_memory/config.json`.

**Every key in this file is read by code.** Inert tunables were deleted rather
than left in place, because editing one that does nothing looks like it does
something.

| Key | Default | Meaning |
|---|---|---|
| `version` | `2.12.1` | Last-resort fallback for a flat install predating `core/version.py`, which is canonical |
| `consolidation.auto_interval_sessions` | `5` | Sessions between async consolidation runs (the backlog trigger is independent of this — its thresholds are module constants in `core/consolidate.py`) |
| `ccl.enabled` | `false` | Local Ollama fallback — **opt-in** |
| `ccl.ollama_url` | `http://localhost:11434` | Ollama endpoint |
| `ccl.local_model` | `ccl-9b` | Local model name |
| `excluded_projects` | `[]` | Absolute paths that opt OUT entirely. The only opt-out; there is no per-project override file |
| `notes` | — | In-file documentation, including which module reads each value |

Everything else is a module constant, documented in `notes.removed_keys`:
writer thresholds in `llm/memory_writer.py`, backlog thresholds in
`core/consolidate.py`, injection budgets in `hooks/session_start.py`, the
idle-reorg interval in `core/idle.py`, the per-mode observation skip-list in
`core/modes.py`, the web viewer's default port in `ui/web_viewer.py`. MCP
registration is the `mcpServers` block in `.claude-plugin/plugin.json`, not a
config key.

**Environment variables**

| Variable | Effect |
|---|---|
| `ANTHROPIC_API_KEY` | Preferred credential; falls through to the Claude Code OAuth token when absent or dead |
| `CLAUDE_PROJECT_DIR` | Consulted by the project-root resolver when it names a directory in the ancestor chain |
| `CC_MEMORY_PLAN_ENFORCE=0` | Kill switch for Stop-hook plan enforcement |

### Per-project files

```
<project>/memory/
├── memory.db                   SQLite (WAL) — the source of truth
├── MEMORY.md                   browsable index, refreshed after every write
├── PROGRESS.md                 handoff; full-rewritten from the `progress` row
├── PLAN.md                     live plan anchor; from the `plan_active` row
├── .gitignore                  written by cc-memory; migrates on existing installs
├── .last_save.json             status + trigger of the last PreCompact
├── .last_inject.json           what SessionStart injected (observability)
├── .last_consolidation.json    cadence marker + row-id watermark for the backlog trigger
├── .consolidation.lock         prevents overlapping async workers
├── .consolidation.kick         backpressure spawn cooldown (v2.12.0)
├── .pre_compact_attempt.json   start marker; survives ⇒ the last run was killed
├── .plan_raw.md                last raw ExitPlanMode capture
├── .plan_history/              append-only archive of replaced / cleared plans
├── sessions/YYYY/MM/           per-session archives
└── topics/                     reserved for per-topic exports
```

Note: this `memory/` lives in **your project directory** — it is unrelated to
`~/.claude/projects/<slug>/memory/`, which some Claude Code setups use for
their own per-project notes. `/cc-mem paths` prints exactly which files this
plugin reads and writes for the current project.

### Database schema

12 tables in one project-local SQLite file:

| Table | Holds |
|---|---|
| `projects` | One row per project root |
| `sessions` | Compaction / session history with archive paths |
| `memories` | The facts, with `supersedes_id`, `content_hash`, `is_active` |
| `topics` | Rolled-up topic summaries |
| `keywords` | Project vocabulary by frequency |
| `observations` | PostToolUse events, cleaned after extraction |
| `session_summaries` | A structured summary per session |
| `progress` | **One row per project** — the source of truth for PROGRESS.md |
| `plan_active` | **One row per project** — the source of truth for PLAN.md |
| `plans` | The plan **queue** (`cc-memory-plan`) |
| `directives` | The user-intent ledger (`/cc-mem directive-*`) |
| `_migrations` | Applied schema migrations |

### Hook table

Six hook commands <!--ce:hooks--> across five Claude Code events, declared in
[hooks/hooks.json](hooks/hooks.json):

| Event | Script | Timeout | Job |
|---|---|---|---|
| `UserPromptSubmit` | `hooks/user_prompt.py` | 8 s | Auto-init `memory/`, count the turn, seed the request on turn 1 |
| `PostToolUse` | `hooks/post_tool_use.py` | 8 s | Live plan anchor **in every mode**, then one observation row per observed tool |
| `Stop` | `hooks/stop.py` | 22 s | Haiku observer, per-turn PROGRESS patch, idle reorg every 5 turns, backpressure probe, plan enforcement |
| `PreCompact` (sync) | `hooks/pre_compact.py` | 120 s | Extract → reconcile → full-rewrite PROGRESS.md → archive |
| `PreCompact` (async) | `hooks/consolidate_async.py` | 300 s | Budget-gated consolidation, off the blocking path; also the standalone backpressure worker |
| `SessionStart` | `hooks/session_start.py` | 15 s | Inject layered context + the forced `<system-reminder>` |

Hook contract, never violated: hooks never write to stderr (Claude Code renders
stderr as error UI), never raise, and always exit 0.

---

## Design philosophy

The technical stack is deliberately boring: **pure Python standard library**
(`sqlite3`, `json`, `pathlib`, `urllib`, `tkinter`, `http.server`), zero pip
dependencies at runtime, PyInstaller only to build the optional Windows
executables. A plugin that runs inside your editor's hook budget has no
business shipping a dependency tree.

The engineering culture is less boring, and it is the actual product:

- **Measurements over assumptions.** Features and fixes enter this project
  attached to a number: a defect reproduced, a latency measured, a blast
  radius counted. When an assumption is kept (macOS support, say), the docs
  say "unmeasured" rather than implying evidence.
- **Fail closed, escape open.** Privacy filters, config parsing and ownership
  checks fail toward *not leaking* and *not guessing*. Enforcement — the one
  place where failing closed would trap the user — carries a bounded escape
  budget and a kill switch instead.
- **A check that cannot go red is not a check.** Every registered fix has a
  falsification case that reverts it on a copy and proves the gate fails.
  Several cases have caught the *checks* being vacuous rather than the fixes
  being wrong; those were fixed by strengthening the check, never by deleting
  the case.
- **Prose that counts things is bound to the code that defines them.** "All
  six hooks" <!--ce:hooks--> in these docs is machine-checked against the hook manifest;
  enumerating a set by hand in prose is treated as a defect class, because
  it rotted three separate times before the gate existed.
- **History is append-only.** CHANGELOG entries are never rewritten to match
  the present; memory rows are archived, never deleted; superseded facts stay
  on a walkable chain. A system whose job is remembering should not itself
  forget by overwriting.

## Architecture

Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
([简体中文](docs/ARCHITECTURE.zh.md)). The three hard contracts are specified in
[docs/CONTRACTS.md](docs/CONTRACTS.md) ([简体中文](docs/CONTRACTS.zh.md)):

- [Anti-patch contract](docs/CONTRACTS.md#anti-patch-contract) — how a write reconciles
- [Handoff contract](docs/CONTRACTS.md#handoff-contract) — the PROGRESS.md spec
- [Plan contract](docs/CONTRACTS.md#plan-contract) — PLAN.md, the carryover gate, the directive ledger, the subagents

**Two install layouts, both supported.** A marketplace / dev checkout keeps the
nested `<plugin-root>/cc_memory/…` shape. The standalone installer lays the
package **FLAT** under `~/.claude/hooks/cc-memory/` — `core/`, `hooks/`, `llm/`,
`cli/`, `mcp/`, `ui/` directly under it, with **no `cc_memory/` path segment**.
Any code or documentation that probes for an install must accept both.

---

## Development

### Repository layout

```
cc-memory/
├── .claude-plugin/          plugin.json (+ inline mcpServers) · marketplace.json
├── .github/                 CI: gates.yml (every gate, on push) · release.yml
│                            (tag → gates → exes → run them → Release) · templates
├── agents/                  plan-refiner.md · plan-guardian.md
├── commands/                cc-mem.md — the /cc-mem slash command
├── hooks/hooks.json         hook declarations (6 commands / 5 events)
├── skills/                  ccm-load/ · save-memories/
├── cc_memory/               the Python package
│   ├── core/                db · extractor · consolidate · plan · progress · privacy
│   │                        modes · roots · atomic · markers · textsim · auth · …
│   ├── hooks/               _entry (shared ladder) + the six hook entry points
│   ├── llm/                 ccl_backend · memory_writer · parse
│   ├── cli/                 mem.py · plan.py
│   ├── mcp/                 server.py
│   └── ui/                  installer · dashboard · web_viewer
├── docs/                    ARCHITECTURE.md · CONTRACTS.md (+ .zh.md siblings)
├── scripts/                 build_exe.py (PyInstaller) · release_notes.py
│                            (CHANGELOG section → release body)
├── tests/                   4 suites + run_gates.py (one command, every gate)
├── tools/                   citation_check · doc_claims · doc_coverage · contracts ·
│                            falsify_fixes · i18n_check
├── CLAUDE.md                project instructions for Claude Code
├── CHANGELOG.md · README.md · README.zh.md · LICENSE · pyproject.toml
```

### Release gates

Eleven gates, all pure stdlib — no pytest, no pip dependencies. Run them all with
one command:

```bash
python tests/run_gates.py           # runs all 11, prints a table, exits nonzero on any red
python tests/run_gates.py --list    # show what each gate is
```

Or individually:

```bash
python -m compileall -q cc_memory tests tools
python -c "import tomllib,pathlib;tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))"
python tests/smoke_test.py                    # end-to-end + the doc gates it hosts + version agreement
python tests/test_plan_carryover.py           # the carryover gate
python tests/test_surfaces.py                 # installer · MCP · web viewer · opt-out · anchoring
python tests/test_directive_enforcement.py    # the directive ledger + Stop enforcement
python tools/i18n_check.py                    # translation drift
python tools/citation_check.py                # every file.py:LINE citation in the tracked docs
python tools/doc_claims.py                    # prose counts vs the sets computed from the tree
python tools/doc_coverage.py                  # every public surface is named by the doc that owns it
```

Two more scripts are **not** gates, and are the ones to reach for when you
doubt a gate:

```bash
python tools/contracts.py       # print what the code currently says each set contains
python tools/falsify_fixes.py   # revert each registered fix on a COPY, assert its gate goes RED
```

Tests must use `tempfile` directories only and must remove them: all four
suites redirect `HOME`/`USERPROFILE` **and** `TMPDIR`/`TEMP`/`TMP` into a
sandbox before importing the package, assert `Path.home()` really moved, and
tear the sandbox down in a `finally`. An uncleanable leak is a test failure.

### Build the executables

```bash
pip install pyinstaller
python scripts/build_exe.py
# → dist/cc-memory-installer.exe
#   dist/cc-memory-dashboard.exe
```

The executables on the [Releases](https://github.com/skymanbp/cc-memory/releases)
page are **not** built on a maintainer's machine. `.github/workflows/release.yml`
builds them on a `v*` tag push, after re-running every release gate on the
tagged commit, and then **runs** them before publishing: the installer performs
a real `--cli` install and `--uninstall` against a sandboxed home directory and
must refuse an unknown flag; the dashboard is launched with `--help` and must
exit cleanly. Only then does it create the GitHub Release, with both exes
attached and the matching CHANGELOG section as the body. A tag that disagrees
with `core/version.py`, or has no CHANGELOG entry, cannot publish.

### Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Hooks never fire on Windows | `hooks/hooks.json` invokes `python3`, and the python.org installer ships no `python3.exe`. Tick "Add Python to PATH" + "py launcher", or shim `python3 → python` |
| `/cc-mem` says the plugin is not found | Both install layouts must be probed. Run `/cc-mem status` — it inspects the layout and reports which files are missing |
| Nothing is being extracted | No credential. `/cc-mem status` checks it. Log in to Claude Code, or set `ANTHROPIC_API_KEY` |
| Where is the database, actually? | `/cc-mem paths` prints the resolved DB / PROGRESS.md / PLAN.md / MEMORY.md with exists/absent verdicts — do not hunt with a recursive glob; the first `*.db` it finds may belong to another tool |
| CJK output shows as `�` | The *capturing shell* decoded UTF-8 with its own codepage (PowerShell 5.1 uses the console codepage). Use `--json` — pure-ASCII output that no capture codec can garble |
| A `memory/` appeared in a subdirectory | A stray from before root anchoring. `/cc-mem status` lists every separate database below the project root with its memory count. A stray is **reported, never merged or deleted** — pin a genuinely nested project with a `.ccm-root` file |
| The plugin went completely silent | A `config.json` that exists but cannot be parsed **fails closed** and excludes every project. `SessionStart` prints one line saying so; fix the JSON |
| Claude cannot end a turn | Plan enforcement is blocking. Read the refusal — it names the condition and the fix. It always degrades to an advisory after its escape budget; `CC_MEMORY_PLAN_ENFORCE=0` switches it off |
| A directive keeps blocking but it's waiting on *me* | `/cc-mem directive-edit <slug> --status blocked` parks it (idle enforcement skips it); `--status active` un-parks. A "never do X" rule should be `--kind constraint` — it is never idle-checked at all |
| `Hook cancelled` on compaction | Fixed in v2.3.2 by moving consolidation to an `async` leg. If you still see it, file an issue with `memory/.last_save.json` |
| A memory is simply wrong | `/cc-mem archive <id>` — reconciliation handles a *restatement*, `archive` handles a *repudiation* |

---

## Roadmap and known limits

Recorded rather than papered over — an unlisted limit is a limit someone else
has to rediscover:

- **macOS is unmeasured.** All eleven gates run on Windows and Linux (3.11,
  3.13) in CI; macOS is expected to work (the same POSIX paths Linux
  exercises) but has not been measured, and this document will not say it has.
- **The step-reference audit is lexical.** It catches `步骤 N` / `step #N` /
  `#N` shapes; a directive that references a step by a paraphrased number
  ("the twelfth step") is not matched. The durable rule is to reference steps
  by title — the audit exists for when the rule was broken anyway.
- **Backlog thresholds are module constants** (50 rows / 7 days), not config
  keys — deliberately, until real usage shows they need per-project tuning.
  Raising a threshold means editing `core/consolidate.py` and knowing why.
- **The Tkinter dashboard's shells have no executable coverage.** Their logic
  cores were extracted into pure functions and tested headlessly (v2.10.1);
  refactoring the remaining 2.9k-line GUI without tests was deliberately
  deferred.
- **Candidate future work:** surfacing `inject-usage` signals in the Stop
  status line; a `directive-*` surface in the dashboard; richer `paths`-style
  diagnostics for multi-database machines.

---

## What's new in v2.12.0

**Consolidation that actually runs, and a ledger you can maintain.** Driven by
two measurements: this repository's own database (349 memories in one month
against a 17-day-old consolidation marker) and a seven-finding field report
from a real consuming project.

- **Backpressure-triggered consolidation** — due at 50 unconsolidated rows or
  7 stale days, probed by the Stop hook every turn, executed by the same
  budget-gated background worker; plus `consolidate --deep`, which loops the
  semantic-dedup judge until it runs dry. The manual path now stamps the same
  cadence marker as the hook path, through one shared writer.
- **`directive-edit`** — correct a directive without bumping its repetition
  count (the count is the ledger's importance signal; repairs were inflating
  it). `--status blocked` parks work that waits on the user; `--kind
  constraint` marks prohibitions that are never idle-checked.
- **Plan replacements audit directive step references** — every `step #N` in
  active directive text is checked against the outgoing and incoming step
  tables and reported as `DEAD` or `SILENTLY RETARGETED`. The documented rule:
  reference steps by title, never by number.
- **`paths`, `--json`, `--full`** — find your artifacts, read them
  untruncated, and get output no capture codec can garble.

v2.12.1 (same day) is release engineering only — the first CI-built release,
after its first run failed on the exe-verification step and the Linux gate
lanes caught a Windows-only test assumption; the plugin is unchanged.

Every earlier release is in **[CHANGELOG.md](CHANGELOG.md)**, which is the
single history of this project — this README documents what the software *is*,
not what it used to be.

---

## Requirements

- **Python 3.8+** — standard library only, zero runtime dependencies
- **Claude Code** with hooks support
- **Tkinter** only for the desktop dashboard (the CLI, MCP server and web
  viewer do not need it)
- **PyInstaller** only for building the executables
- **Windows**: `python3` must resolve to a Python 3 interpreter (see
  [Troubleshooting](#troubleshooting))

Developed Windows-first. **All eleven release gates run on both Windows and
Linux (Python 3.11 and 3.13) in CI**; macOS is not covered by CI — it is
expected to work but has not been measured, and this document will not say it
has.

---

## Documentation map

| Document | What it is for |
|---|---|
| [README.md](README.md) · [README.zh.md](README.zh.md) | What this is, what it does, how to use it |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [.zh](docs/ARCHITECTURE.zh.md) | Module map, data flow, install layouts, the i18n convention |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) · [.zh](docs/CONTRACTS.zh.md) | The three hard contracts, in specification form |
| [commands/cc-mem.md](commands/cc-mem.md) | Every `/cc-mem` subcommand, with semantics |
| [CLAUDE.md](CLAUDE.md) | Instructions for Claude Code working *on* this repository |
| [CHANGELOG.md](CHANGELOG.md) | The complete version history |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) | How to contribute; how to report a vulnerability |

English is the canonical skeleton; each `*.zh.md` is a drift-tracked sibling
bound to a normalised hash of its English source, and `tools/i18n_check.py`
turns the suite red when one drifts. Memory **content** is language-agnostic —
only the project's own documentation follows this convention.

---

## License

[MIT](LICENSE) © skymanbp

---

<sub>**Keywords** — Claude Code plugin · Claude Code memory · persistent memory
for LLM agents · agent long-term memory · context window management ·
conversation compaction recovery · session handoff · AI coding assistant memory
· Anthropic Claude · MCP server · Model Context Protocol · SQLite FTS5 memory
store · retrieval for coding agents · prompt-injection hardening · pure-stdlib
Python plugin.</sub>
