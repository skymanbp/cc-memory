> **English** · [简体中文](README.zh.md)

<div align="center">

# cc-memory

**Persistent memory for Claude Code.**
Your project's decisions, results, bugs and plans survive compaction, session
boundaries, and closed terminals — and the next session is *forced* to read
them before it does anything.

[![version](https://img.shields.io/badge/version-2.11.3-blue.svg)](CHANGELOG.md)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](pyproject.toml)
[![dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)](#requirements)
[![release gates](https://img.shields.io/badge/release%20gates-10-orange.svg)](#release-gates)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#requirements)

</div>

---

## Table of contents

- [The problem](#the-problem)
- [How it works in 60 seconds](#how-it-works-in-60-seconds)
- [Quick start](#quick-start)
- [Feature index](#feature-index)
  - [1 · Memory capture](#1--memory-capture)
  - [2 · Memory quality — the anti-patch contract](#2--memory-quality--the-anti-patch-contract)
  - [3 · Session handoff — PROGRESS.md](#3--session-handoff--progressmd)
  - [4 · Plan and intent — PLAN.md and the directive ledger](#4--plan-and-intent--planmd-and-the-directive-ledger)
  - [5 · Search and retrieval](#5--search-and-retrieval)
  - [6 · Interfaces](#6--interfaces)
  - [7 · Privacy and safety](#7--privacy-and-safety)
  - [8 · Reliability engineering](#8--reliability-engineering)
- [Reference](#reference)
  - [`/cc-mem` — 32 subcommands](#cc-mem--32-subcommands)
  - [`cc-memory-plan` — the plan queue](#cc-memory-plan--the-plan-queue)
  - [MCP tools](#mcp-tools)
  - [Configuration](#configuration)
  - [Per-project files](#per-project-files)
  - [Database schema](#database-schema)
  - [Hook table](#hook-table)
- [Architecture](#architecture)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [What's new in v2.11.3](#whats-new-in-v2113)
- [Documentation map](#documentation-map)
- [License](#license)

---

## The problem

Claude Code **compacts** a conversation when the context window fills. Whatever
was in the discarded turns is gone: the decision you made three hours ago, the
benchmark number you measured, the bug you already fixed once, the constraint
you stated and had to state again. Sessions that simply end — terminal closed,
laptop shut — lose the same thing, silently.

The usual workarounds do not survive contact with a long project:

| Workaround | Why it fails |
|---|---|
| A hand-written `NOTES.md` | Nobody updates it under deadline; it goes stale and starts lying |
| Pasting context back in each session | Manual, lossy, and costs the tokens you were trying to save |
| Bigger context windows | Delays the compaction; does not remove it |
| Append-only memory files | The same fact gets restated every session and stacks up; the file becomes noise |

cc-memory attacks all four. It captures structured memories at every
conversation boundary, **reconciles** each new fact against what is already
stored instead of appending, and emits a `<system-reminder>` at session start
that requires the next Claude to read the handoff document **before** it
responds.

---

## How it works in 60 seconds

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

---

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
[Configuration](#configuration).

---

## Feature index

### 1 · Memory capture

| Feature | What it does |
|---|---|
| **AI-judged extraction** | Haiku reads the conversation and returns structured `{category, content, importance}` records — not keyword scraping |
| **Regex fallback** | A project-neutral pattern layer runs when no credential is available, so capture never depends on the network |
| **Optional local fallback** | Ollama backend, **opt-in** via `ccl.enabled` (default `false`) |
| **Two capture points** | `Stop` captures per turn from that turn's tool observations; `PreCompact` captures from the transcript before context is destroyed |
| **Bounded transcript reads** | A head+tail window (40 records + 32 MiB) instead of the whole file — a 2.11 GiB transcript loads in 1.66 s instead of 88 s, so the hook is never killed mid-write |
| **Newest-first summarisation** | The extraction budget fills from the most recent record backwards; filling from the oldest pinned every extraction to a session's opening hours |
| **Seven categories** | `decision` · `result` · `config` · `bug` · `task` · `arch` · `note` |
| **Five importance levels** | `1` noise → `5` critical / never forget |
| **Three project modes** | `code` · `research` · `writing` — each with its own observed-tool set and injection priority (`/cc-mem mode`) |
| **Language-agnostic content** | Extraction and resume-signal detection recognise English *and* Chinese by design; stored memories may be in any language |

### 2 · Memory quality — the anti-patch contract

The differentiator. Most memory tools append; appending is what turns a memory
store into noise.

| Feature | What it does |
|---|---|
| **Reconcile-on-write** | Every save path routes through one writer, which decides **MERGE** (overwrite a near-identical row in place), **SUPERSEDE** (archive the old, link the new via `supersedes_id`), or **INSERT** |
| **Similarity substrate** | Trigram-Jaccard, with **character bigrams inside CJK runs** — plain trigrams collapse on Chinese, scoring a one-character correction at 0.4545 and filing every correction as a new contradictory fact |
| **LLM-judged semantic de-dup** | The same fact *reworded* each session scores low on trigrams; a second pass nominates candidate groups by word-Jaccard and has Haiku confirm same-fact before merging |
| **Obsolescence detection** | Names `{stale, current}` pairs with a temporal guard, so a historical action cannot obsolete a live fact |
| **Walkable history** | `supersedes_id` forms a DAG (cycles refused, first lineage fact preserved with `COALESCE`); `/cc-mem supersedes <id>` walks it |
| **Nothing is ever deleted** | Archival is `is_active=0`, always recoverable. `/cc-mem archive` is the supported way to retire a fact discovered to be **wrong** |
| **Provenance preserved** | Tags are unioned with the surviving row's, never replaced, and capped |

### 3 · Session handoff — PROGRESS.md

| Feature | What it does |
|---|---|
| **Single source of truth** | `memory/PROGRESS.md` is always **full-rewritten** from one SQL row, never appended — it cannot go stale or self-contradict |
| **Forced read** | `SessionStart` emits a `<system-reminder>` requiring the next Claude to read it *before* responding |
| **Eleven user-facing fields** | current request · done · in-flight · blocked · open todos · plan · critical context · files touched · transcript pointer · updated-at · trigger type |
| **Four writers, one contract** | `PreCompact` full-overwrites; `Stop` patches files-touched per turn; `UserPromptSubmit` seeds the request on turn 1; `SessionStart` fills **only still-empty** fields |
| **Per-session annotation** | The row records which session is speaking and when it started |
| **Killed-run visibility** | A start marker survives a timeout kill, so a compaction that died mid-write is provable rather than invisible |
| **Injection observability** | `.last_inject.json` records exactly what was injected; `/cc-mem inject-show` dumps ground truth and `/cc-mem inject-usage` reports whether Claude actually read it |

### 4 · Plan and intent — PLAN.md and the directive ledger

Two different things, deliberately kept apart: a **plan step** is a unit of
execution and dies when the plan is replaced; a **directive** is a unit of user
intent and outlives every plan.

| Feature | What it does |
|---|---|
| **Live plan anchor** | `memory/PLAN.md` is full-rewritten from the `plan_active` row; `ExitPlanMode` output is captured automatically |
| **Two shipped subagents** | `plan-refiner` normalises a raw plan into structured JSON; `plan-guardian` does a read-only ≤150-word drift check |
| **Mechanical step sync** | `TodoWrite` events sync step statuses by title similarity — no LLM, no drift |
| **Drift counters** | Edits bump a counter; a sensitive Bash call (`git push`, `rm -rf`, deploys) bumps it by 20 |
| **Mandatory carryover gate** | Replacing a plan requires every unfinished step to be auto-carried or explicitly dispositioned with a reason. **There is no force flag, by design** |
| **Success-criteria advisory** | Criteria that vanish in a replacement are named — a gate that covers only `steps` says nothing about the rest |
| **Append-only plan history** | Every outgoing plan is archived to `memory/.plan_history/` |
| **Directive ledger** | `/cc-mem directive-add` records what the *user* demanded. Re-stating the same slug bumps `times_stated` on **one** row — repetition is an importance signal a plan cannot express |
| **Evidence-gated closure** | `/cc-mem directive-close` **refuses without `--evidence`**: a commit, a `file:line`, or a gate name. A directive closed on an assertion is the failure the ledger exists to prevent |
| **Stop enforcement** | The `Stop` hook can refuse to end a turn while a plan sits unrefined, a live plan is undrift-checked, or an active directive has gone idle — with a guaranteed escape budget and the kill switch `CC_MEMORY_PLAN_ENFORCE=0` |

### 5 · Search and retrieval

| Feature | What it does |
|---|---|
| **FTS5 full-text search** | `/cc-mem search "auth flow"`, with `LIKE ? ESCAPE` fallback and clamped limits |
| **Topic summaries** | Memories roll up into topics, refreshed by consolidation |
| **Keyword vocabulary** | Project vocabulary by frequency, grown across sessions |
| **Layered injection** | `SessionStart` injects topics + critical memories + a recent timeline + a PROGRESS preview, under per-layer budgets |
| **Auto-fresh index** | `memory/MEMORY.md` is regenerated after every batch write |
| **Corruption scan** | `/cc-mem encoding-check` finds U+FFFD damage across the text tables; `--apply` quarantines, recoverably |

### 6 · Interfaces

| Interface | Entry point | What you get |
|---|---|---|
| **Slash command** | `/cc-mem <sub>` | 32 subcommands, path-agnostic, resolves `--project .` for you |
| **Shell CLI** | `cc-memory` / `cli/mem.py` | The same 32 subcommands outside Claude Code |
| **Plan queue CLI** | `cc-memory-plan` / `cli/plan.py` | 12 subcommands for a `draft → ready → executing → done` task queue |
| **MCP server** | `cc_memory/mcp/server.py` | 8 tools over JSON-RPC 2.0 stdio, registered inline in the plugin manifest |
| **Desktop dashboard** | `/cc-mem dashboard` | Tkinter GUI, 7 tabs: Memories · Plans · Sessions · Keywords · SQL Console · Stats · Progress/Plan |
| **Web viewer** | `/cc-mem serve` | Loopback-only browser UI: browse, search, and add memories |
| **Skills** | `/ccm-load`, `/save-memories` | One-shot activation + bootstrap; manual save through the anti-patch writer |
| **Subagents** | `plan-refiner`, `plan-guardian` | Shipped in the plugin, discoverable under both install layouts |

### 7 · Privacy and safety

| Feature | What it does |
|---|---|
| **Project opt-out** | `excluded_projects` — a listed directory *and everything beneath it* gets no `memory/`, no database, no observations, no extraction, no injection. Enforced by every hook and by the MCP server, on the **raw** cwd, *before* the project-root anchor |
| **Fails closed** | A `config.json` that exists but cannot be used excludes **every** project and logs why, rather than guessing "not excluded" and storing data irreversibly |
| **`<private>` spans** | Text between `<private>` tags is stripped before both the Anthropic call and the database — linear-time, no cap, and a dangling open tag drops the remainder rather than leaking it |
| **Authority-marker neutralisation** | Stored content is **escaped, never interpolated raw**, on the write path and on every render path — a memory cannot forge a `<system-reminder>` into your next session |
| **Read-only SQL** | `/cc-mem sql` refuses every write statement, including the `PRAGMA name(value)` setter form |
| **Loopback-only web viewer** | No CORS header, `Origin` and `Host` both enforced (DNS-rebinding), JSON content-type required on POST, bounded header *and* body phases, capped concurrency |
| **MCP schema enforcement** | `tools/call` arguments are validated against the advertised `inputSchema` and refused with `-32602` rather than coerced |
| **No telemetry** | Nothing is sent anywhere except the extraction/consolidation calls to Anthropic (or your local Ollama), using your existing Claude Code credential |

### 8 · Reliability engineering

| Feature | What it does |
|---|---|
| **Zero runtime dependencies** | Pure Python standard library. PyInstaller is build-time only |
| **Atomic artifact writes** | One writer — tmp + `os.replace`, with a wall-clock retry budget. Contract: *replace completely, or raise; never truncate* |
| **Project-root anchoring** | `cwd` follows the agent's `cd`; a resolver walks an ancestor chain (database → `CLAUDE_PROJECT_DIR` → project markers) so a stray database is never born in a subdirectory. Containers of projects and dependency trees are never candidates |
| **One shared hook entry ladder** | stdin parse → opt-out → root anchor, implemented once; per-hook policies stay per-hook |
| **Bounded LLM wall-clock** | Every LLM-calling hook passes an absolute deadline, not just a per-leg timeout, so it cannot overrun the host's hard hook timeout |
| **Off the blocking path** | Consolidation runs as an `async` PreCompact leg under a budget gate, so it can never surface as `Hook cancelled` |
| **Scoped to the project** | Every command that touches a table scopes it by `project_id` — one database file legitimately holds several projects |
| **10 release gates** | Three doc gates, four test suites, `compileall`, a `pyproject` parse, and version-site agreement. See [Release gates](#release-gates) |
| **A falsification register** | Every registered fix is reverted on a temporary copy to prove its gate goes **red**. A gate that cannot fail is a gate that is lying |

---

## Reference

### `/cc-mem` — 32 subcommands

Inside Claude Code (path-agnostic — the wrapper resolves the plugin root):

```
# ── state and health ───────────────────────────────────────────────────────
/cc-mem status                      Full health check (hooks, DB, API key, PROGRESS)
/cc-mem stats                       Memory counts + supersede-chain count
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
/cc-mem sql "<SELECT ...>"          Read-only query (writes refused)

# ── writing memory ─────────────────────────────────────────────────────────
/cc-mem add <category> "<text>" [--importance N]   Anti-patch upsert
/cc-mem archive <id>... [--supersedes ID]          Retire a WRONG fact (recoverable)
/cc-mem consolidate                 Full LLM-backed consolidation
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
/cc-mem plan-set --from-refiner     Store structured JSON from stdin
/cc-mem plan-check                  Reset drift counters + emit guardian hint
/cc-mem plan-replan                 Re-arm needs_refine on the stored raw
/cc-mem plan-clear --reason "<why>" Drop the active plan (reason required if unfinished)

# ── directive ledger ───────────────────────────────────────────────────────
/cc-mem directive-list [--status active|done|superseded|dropped|all]
/cc-mem directive-add <slug> --demand "..." [--quote "..."] [--kind ...] [--times N]
/cc-mem directive-close <slug> --evidence "<commit|file:line|gate>"

# ── interfaces ─────────────────────────────────────────────────────────────
/cc-mem dashboard                   Launch the Tkinter GUI
/cc-mem serve [--port N]            Launch the loopback web viewer
```

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
| `version` | `2.11.3` | Last-resort fallback for a flat install predating `core/version.py`, which is canonical |
| `consolidation.auto_interval_sessions` | `5` | Sessions between async consolidation runs |
| `ccl.enabled` | `false` | Local Ollama fallback — **opt-in** |
| `ccl.ollama_url` | `http://localhost:11434` | Ollama endpoint |
| `ccl.local_model` | `ccl-9b` | Local model name |
| `excluded_projects` | `[]` | Absolute paths that opt OUT entirely. The only opt-out; there is no per-project override file |
| `notes` | — | In-file documentation, including which module reads each value |

Everything else is a module constant, documented in `notes.removed_keys`:
writer thresholds in `llm/memory_writer.py`, injection budgets in
`hooks/session_start.py`, the idle-reorg interval in `core/idle.py`, the
per-mode observation skip-list in `core/modes.py`, the web viewer's default
port in `ui/web_viewer.py`. MCP registration is the `mcpServers` block in
`.claude-plugin/plugin.json`, not a config key.

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
├── .last_consolidation.json    interval marker for the async leg
├── .consolidation.lock         prevents overlapping async workers
├── .pre_compact_attempt.json   start marker; survives ⇒ the last run was killed
├── .plan_raw.md                last raw ExitPlanMode capture
├── .plan_history/              append-only archive of replaced / cleared plans
├── sessions/YYYY/MM/           per-session archives
└── topics/                     reserved for per-topic exports
```

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
| `Stop` | `hooks/stop.py` | 22 s | Haiku observer, per-turn PROGRESS patch, idle reorg every 5 turns, plan enforcement |
| `PreCompact` (sync) | `hooks/pre_compact.py` | 120 s | Extract → reconcile → full-rewrite PROGRESS.md → archive |
| `PreCompact` (async) | `hooks/consolidate_async.py` | 300 s | Budget-gated consolidation, off the blocking path |
| `SessionStart` | `hooks/session_start.py` | 15 s | Inject layered context + the forced `<system-reminder>` |

Hook contract, never violated: hooks never write to stderr (Claude Code renders
stderr as error UI), never raise, and always exit 0.

---

## Architecture

Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
([简体中文](docs/ARCHITECTURE.zh.md)). The three hard contracts are specified in
[docs/CONTRACTS.md](docs/CONTRACTS.md) ([简体中文](docs/CONTRACTS.zh.md)):

- [Anti-patch contract](docs/CONTRACTS.md#anti-patch-contract) — how a write reconciles
- [Handoff contract](docs/CONTRACTS.md#handoff-contract) — the PROGRESS.md spec
- [Plan contract](docs/CONTRACTS.md#plan-contract) — PLAN.md, the carryover gate, the subagents

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
├── .github/                 CI running every release gate · issue + PR templates
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
├── scripts/                 build_exe.py — PyInstaller build
├── tests/                   4 suites + run_gates.py (one command, every gate)
├── tools/                   citation_check · doc_claims · contracts · falsify_fixes · i18n_check
├── CLAUDE.md                project instructions for Claude Code
├── CHANGELOG.md · README.md · README.zh.md · LICENSE · pyproject.toml
```

### Release gates

Ten gates, all pure stdlib — no pytest, no pip dependencies. Run them all with
one command:

```bash
python tests/run_gates.py           # runs all 10, prints a table, exits nonzero on any red
python tests/run_gates.py --list    # show what each gate is
```

Or individually:

```bash
python -m compileall -q cc_memory tests tools
python -c "import tomllib,pathlib;tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))"
python tests/smoke_test.py                    # end-to-end + the three doc gates + version agreement
python tests/test_plan_carryover.py           # the carryover gate
python tests/test_surfaces.py                 # installer · MCP · web viewer · opt-out · anchoring
python tests/test_directive_enforcement.py    # the directive ledger + Stop enforcement
python tools/i18n_check.py                    # translation drift
python tools/citation_check.py                # every file.py:LINE citation in the tracked docs
python tools/doc_claims.py                    # prose counts vs the sets computed from the tree
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

### Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Hooks never fire on Windows | `hooks/hooks.json` invokes `python3`, and the python.org installer ships no `python3.exe`. Tick "Add Python to PATH" + "py launcher", or shim `python3 → python` |
| `/cc-mem` says the plugin is not found | Both install layouts must be probed. Run `/cc-mem status` — it inspects the layout and reports which files are missing |
| Nothing is being extracted | No credential. `/cc-mem status` checks it. Log in to Claude Code, or set `ANTHROPIC_API_KEY` |
| A `memory/` appeared in a subdirectory | A stray from before root anchoring. `/cc-mem status` lists every separate database below the project root with its memory count. A stray is **reported, never merged or deleted** — pin a genuinely nested project with a `.ccm-root` file |
| The plugin went completely silent | A `config.json` that exists but cannot be parsed **fails closed** and excludes every project. `SessionStart` prints one line saying so; fix the JSON |
| Claude cannot end a turn | Plan enforcement is blocking. Read the refusal — it names the condition and the fix. It always degrades to an advisory after its escape budget; `CC_MEMORY_PLAN_ENFORCE=0` switches it off |
| `Hook cancelled` on compaction | Fixed in v2.3.2 by moving consolidation to an `async` leg. If you still see it, file an issue with `memory/.last_save.json` |
| A memory is simply wrong | `/cc-mem archive <id>` — reconciliation handles a *restatement*, `archive` handles a *repudiation* |

---

## What's new in v2.11.3

**The documentation caught up with the code.** v2.11.2 changed how directive
idleness is measured — a schema change with a load-bearing rule attached — and
the ten gates went green anyway, because they check citation line numbers,
bound counts and translation hashes. **None of them asks whether a new design
was written down.** The specification documents had zero mentions of it.

- `docs/CONTRACTS.md` § Plan contract gains the rule as a fourth load-bearing
  property: idleness is `turns_total - turns_at_touch`, and must never be
  measured against `turns_since_last_guardian`, which every guardian check
  resets.
- `docs/ARCHITECTURE.md` § Database schema documents both v9 columns in the
  same "carries X since migration Y" form the other rows already use.
- `commands/cc-mem.md` says what "idle" counts for a user: turns since *that
  directive* was last written — re-stating or closing it restarts the clock,
  running `/cc-mem plan-check` does not.
- Both Chinese siblings updated to match.

Also corrected: this README claimed cross-platform support "by construction",
which is not evidence. All ten gates now run on Windows **and** Linux (3.11,
3.13) in CI; macOS remains unmeasured and is now described that way.

## What's new in v2.11.2

**The three things v2.11.1 recorded as still open, closed — including the one
it had papered over.**

- **Directive idleness now has a real baseline.** v2.11.1 measured it against
  `turns_since_last_guardian`, and *that counter resets* — `/cc-mem plan-check`
  and every plan replacement zero it. So a directive genuinely untouched for
  thirty turns looked freshly attended to the moment anyone ran a guardian
  check: the ledger forgiving exactly the neglect it exists to surface. Schema
  **v9** adds a monotonic `turns_total` that nothing resets, and each directive
  records the turn it was last touched at. Idleness became subtraction between
  two numbers that only increase, which no reset can distort.
- **Linux runs all ten gates.** It used to run the platform-independent subset
  with a comment asserting the other two were Windows-specific — an assumption,
  never a measurement, and it left the largest unknown in the project: whether
  cc-memory works on Linux at all. The full suite now runs on Linux for 3.11
  and 3.13.
- **A stray `.pytest_cache/`** in a project that documents "no pytest" is gone.

v2.11.1 itself closed six defects in the code path that can refuse your turn —
an escape budget that could never release, a stored directive reaching Claude as
a live authority marker, a refusal whose stdout was not JSON, and a cleared plan
that enforced forever.

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

Developed Windows-first. **All ten release gates run on both Windows and Linux
(Python 3.11 and 3.13) in CI** — that used to be a claim resting on "written to
be portable", which is not evidence; Linux ran only a subset until v2.11.3, and
the assumption that the other two suites were Windows-specific turned out to be
wrong. macOS is not covered by CI: it is expected to work (the same POSIX paths
Linux exercises) but has not been measured, and this document will not say it
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
