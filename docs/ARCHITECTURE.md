> **English** · [简体中文](ARCHITECTURE.zh.md)

# cc-memory — Architecture

<!-- No version in this heading, on purpose. It read "(v2.9.0)" through two
     releases: a version stamped into a title rots every time and nothing
     gates it, because a heading is not a countable claim. The canonical
     version is `cc_memory/core/version.py`, and `tests/run_gates.py` asserts
     every version SITE agrees with it. History lives in CHANGELOG.md. -->


cc-memory is a Claude Code plugin that gives Claude **persistent, structured
memory across compactions and sessions**. This document is the overview: what
the plugin is for, how the repository is laid out, which hooks fire when, what
the database holds, how data moves, how LLM calls are authenticated, what the
plugin writes into a project, where installed code physically lives, and the
convention that keeps the documentation translated without silent drift.

The three hard contracts — anti-patch writes, forced handoff, and the live plan
anchor — are specified in [docs/CONTRACTS.md](CONTRACTS.md). This file
describes the machinery; that file describes the rules the machinery must obey.

**On `file:line` citations.** They are now enforced, by
`tools/citation_check.py` (v2.5.2), which runs inside `tests/smoke_test.py`: for
each citation it resolves the symbols named in the surrounding prose with `ast`
and asserts the cited range covers that symbol's definition, or at least
mentions it. Its first run found **163 of 594 citations stale** — every one of
them written correctly and then left behind by a later edit above it — and
repaired them mechanically.

That is a gate against rot, not a proof of correctness. A citation whose
sentence names no uniquely resolvable function, class or ALL_CAPS constant is
reported SKIP and is **not** checked (370 of 594 today). Treat a line number as
a hint and the **symbol name** as the fact: `grep -n "def <symbol>" <file>` is
authoritative, and `python tools/citation_check.py --fix` is how you repair a
number rather than hand-counting.

## Contents

- [1. Overview / what it solves](#1-overview--what-it-solves)
- [2. Repository layout](#2-repository-layout)
- [3. Hooks](#3-hooks)
- [4. Database schema](#4-database-schema)
- [5. Data flow](#5-data-flow)
- [6. LLM backends and auth](#6-llm-backends-and-auth)
- [7. Per-project state (.ccm/)](#7-per-project-state-ccm)
- [8. Install layouts](#8-install-layouts)
- [9. Documentation language convention (i18n)](#9-documentation-language-convention-i18n)

---

## 1. Overview / what it solves

Three design constraints drive everything else:

1. **Anti-patch writes.** Every memory save goes through one entry
   (`llm.memory_writer.upsert_smart`) which either **merges** the new content
   into an existing similar memory, **supersedes** an older version (preserving
   a chain), **reinforces** an exact duplicate (no new row — only its higher
   importance and any new tags are folded into the row it matched), or
   **inserts** as a new fact — chosen by similarity, not by the
   caller. There is no "append + dedup later" path.

2. **Forced handoff.** At every `SessionStart`, the plugin emits a
   `<system-reminder>` block instructing the next Claude to `Read
   .ccm/PROGRESS.md` before responding. PROGRESS.md is a single SOT,
   always full-rewritten from the `progress` SQLite table — never appended
   to. The previous v2.0 `SESSION_HANDOFF.md` (which drifted into patch-style
   pollution) is migrated aside.

3. **Single source of truth, no stacking.** Skills, commands, configs, and
   docs each live in exactly ONE place. No `.claude/skills/` AND `skills/`.
   No three copies of `save-memories`. No 6 files claiming different version
   numbers.

Two operational constraints follow from the fact that this code runs inside
Claude Code's own hook budget:

4. **Hooks must never block and never raise.** Every hook entry point ends in
   `sys.exit(0)`, writes diagnostics to `~/.claude/hooks/cc-memory/logs/` via
   `core.logger`, and never writes to stderr (Claude Code renders stderr as an
   error). Work whose latency is unbounded is moved off the blocking path
   entirely — that is why consolidation became a second, `async` PreCompact
   hook in v2.3.2 (see [§3](#3-hooks)).

5. **Pure stdlib at runtime.** `sqlite3`, `json`, `pathlib`, `urllib`,
   `datetime`, `subprocess`, `tkinter`, `time`, `hashlib`, `re`,
   `http.server`. No pip dependencies. PyInstaller is build-time only.

### Bilingual by design — memory content is language-agnostic

Memory **content** is deliberately language-neutral. The category detectors
(`core/extractor.py` `_PATTERNS` at `extractor.py:73-77` / `_IMPORTANCE_BOOST`
at `extractor.py:73-77`) and the resume-signal sets (`hooks/user_prompt.py:127-130`,
`hooks/session_start.py:269-273` RESUME PROTOCOL) match both Chinese and English
on purpose, and stored memories may be in any language. This is **Tier 3** of
the documentation language model — separate from the English-skeleton *docs*
convention (Tier 1). Those detectors carry inline `i18n Tier 3` comments and
must NOT be reduced to English-only. See
[§9.1](#91-the-three-tier-language-model) for the full three-tier model.

---

## 2. Repository layout

```
cc-memory/
├── .claude-plugin/
│   ├── plugin.json              ← Plugin manifest (version = the release)
│   └── marketplace.json         ← /plugin marketplace add entry
├── hooks/hooks.json             ← Hook declarations (6 commands / 5 events)
├── skills/                      ← THE canonical skills location
│   ├── ccm-load/SKILL.md        (one-shot activation + init + status)
│   └── save-memories/SKILL.md   (routes through memory_writer)
├── agents/                      ← Plugin-shipped subagents (v2.2+)
│   ├── plan-refiner.md          (raw plan → structured JSON)
│   └── plan-guardian.md         (read-only drift check, ≤150 words)
├── commands/
│   └── cc-mem.md                ← /cc-mem slash command
├── docs/
│   ├── ARCHITECTURE.md          ← This file (overview + i18n convention)
│   ├── ARCHITECTURE.zh.md       ← drift-tracked translation (see §9)
│   ├── CONTRACTS.md             ← anti-patch + forced handoff + live plan
│   └── CONTRACTS.zh.md          ← drift-tracked translation (see §9)
├── cc_memory/                   ← Python package (subpackaged)
│   ├── __init__.py              (re-exports core/version.py)
│   ├── config.json
│   ├── core/                    ← Domain: db, extractor, consolidate, idle,
│   │                              progress, plan, privacy, modes, roots,
│   │                              auth, logger, encoding_setup, version,
│   │                              atomic, markers, textsim
│   ├── hooks/                   ← 6 hook entry points + _entry.py (the
│   │                              shared entry ladder: stdin parse + the
│   │                              opt-out→anchor gate, v2.10.0)
│   ├── llm/                     ← ccl_backend (Haiku/Ollama) + memory_writer
│   │                              + parse (tolerant LLM-JSON reader)
│   ├── cli/                     ← mem.py, plan.py
│   ├── mcp/                     ← server.py (MCP stdio)
│   └── ui/                      ← installer, dashboard, web_viewer
├── .github/workflows/           ← gates.yml (every gate, on push/PR) +
│                                  release.yml (tag → gates → exes → RUN
│                                  them → GitHub Release, v2.12.0)
├── tests/                       ← run_gates.py (THE gate runner) + the four
│                                  suites: smoke_test, test_plan_carryover,
│                                  test_surfaces, test_directive_enforcement
├── tools/                       ← dev/CI checkers, never packaged: i18n_check,
│                                  citation_check, doc_claims, doc_coverage,
│                                  contracts, falsify_fixes
├── scripts/                     ← build_exe.py (PyInstaller) +
│                                  release_notes.py (CHANGELOG → release body)
├── pyproject.toml
├── README.md
├── README.zh.md                 ← drift-tracked translation (see §9)
├── CLAUDE.md                    ← Project instructions for Claude Code
├── CHANGELOG.md
└── LICENSE
```

`agents/`, `tests/`, and `tools/` are load-bearing, not incidental:
`agents/plan-refiner.md` is nudged from `cc_memory/hooks/stop.py` and
`agents/plan-guardian.md` from the same hook; `tools/i18n_check.py` is what
[§9](#9-documentation-language-convention-i18n) specifies and what
`tests/smoke_test.py:878-895` imports as a drift gate.

### One version string (v2.5)

`cc_memory/core/version.py` holds `__version__` and is **the** canonical runtime
source. It lives in `core/` rather than in `cc_memory/__init__.py` because every
entry point bootstraps by putting the *package directory* on `sys.path` and then
importing flat (`from core.db import MemoryDB`) — the standalone installer lays
the tree out FLAT, so under that layout `import cc_memory` raises
`ModuleNotFoundError` and `from cc_memory import __version__` cannot be used by
any module that must run from both layouts. `from core.version import
__version__` resolves under both, and `cc_memory/__init__.py:64` re-exports it so
the wheel-importable form keeps working.

Four **non-importable** manifests cannot read it and must be bumped alongside:
`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
`cc_memory/config.json` (`version`, the last-resort fallback for a flat install
that somehow lacks `core/version.py`) and `pyproject.toml`.
`tests/smoke_test.py` asserts they all agree.

Before v2.5 the string was retyped in the CLI banners (`cli/mem.py`), the MCP
server banners (`mcp/server.py`), the installer banner/GUI title
(`ui/installer.py`) and `build_exe.py`; two of those literals were stale at
v2.4.3. All of them now resolve the value at runtime, with a documented
fallback chain (`core.version` → package `__init__` → text scan →
`config.json` → `"unknown"`) so a partial install degrades instead of crashing.

### Where the pre-merge documents went

The five-document `docs/` tree was consolidated into two files in v2.4.3. All
79 in-repo citations were repointed in the same change, so nothing in the tree
still resolves to a deleted filename. This table is the redirect map for anyone
following an old link from a released CHANGELOG entry, a GitHub permalink, or a
prior session's notes:

| Pre-merge document | Now |
|---|---|
| `docs/MEMORY_RULES.md` | [CONTRACTS.md § Anti-patch contract](CONTRACTS.md#anti-patch-contract) |
| `docs/HANDOFF_PROTOCOL.md` | [CONTRACTS.md § Handoff contract](CONTRACTS.md#handoff-contract) |
| `docs/PLAN_PROTOCOL.md` | [CONTRACTS.md § Plan contract](CONTRACTS.md#plan-contract) |
| `docs/I18N.md` | [§9 of this file](#9-documentation-language-convention-i18n) |
| `docs/ARCHITECTURE.md §3` (schema) | [§4 of this file](#4-database-schema) — the pre-merge file had no numbered sections, so the number moved even though the filename did not |

`CHANGELOG.md` deliberately keeps the old filenames in its historical entries:
those entries describe what the tree looked like at the time, and rewriting them
would falsify the record.

---

## 3. Hooks

**6 hook commands <!--ce:hooks--> across 5 Claude Code events** are declared in
`hooks/hooks.json` — `PreCompact` declares two, a blocking sync leg
(`hooks.json:9`, 120s) and a background `async` leg (`hooks.json:14`, 300s,
`"async": true`):

| Hook | Entry | Timeout | Job |
|------|-------|---------|-----|
| `PreCompact` (sync) | [`cc_memory/hooks/pre_compact.py`](../cc_memory/hooks/pre_compact.py) | 120s | Read a BOUNDED head+tail transcript window (`extractor.load_transcript_window`); LLM extract memories via Haiku; route through `memory_writer.upsert_batch`; FULL-REWRITE `.ccm/PROGRESS.md`; archive session. Writes a start marker so a killed run is detectable. |
| `PreCompact` (async) | [`cc_memory/hooks/consolidate_async.py`](../cc_memory/hooks/consolidate_async.py) | 300s, `async: true` | LLM consolidation, moved OFF the blocking compaction path in v2.3.2 (interval marker + lock, budget-gated). Due every Nth session OR when the write backlog says so (v2.12.0); also spawnable standalone (`--cwd <root>`) by the Stop hook's backpressure probe. |
| `SessionStart` | [`cc_memory/hooks/session_start.py`](../cc_memory/hooks/session_start.py) | 15s | Inject layered context (standing directives / topics / critical / timeline / PROGRESS preview / footer — the directive ledger is the first layer since v2.12.2; before that nothing injected it); emit the FORCED `<system-reminder>` to Read `PROGRESS.md` + `MEMORY.md`; retroactive save of unsaved JSONLs. |
| `Stop` | [`cc_memory/hooks/stop.py`](../cc_memory/hooks/stop.py) | 22s | Observer: extract from last turn's observations via Haiku; per-turn `patch_progress(files_touched, ...)`; every 5 turns run `idle.maybe_run_idle` (cleanup + MEMORY.md regen); probe the consolidation backlog and spawn the detached async worker when it is due (v2.12.0); when a plan is LIVE, bump its turn counter and **enforce** — refuse the turn (`{"decision": "block"}`) over an unrefined plan, an undrift-checked plan, or an idle directive, with the escape budget CONTRACTS.md specifies (v2.11.0; the advisory nudge this row used to describe is gone). |
| `PostToolUse` | [`cc_memory/hooks/post_tool_use.py`](../cc_memory/hooks/post_tool_use.py) | 8s | Live-plan integration FIRST, in every mode: `ExitPlanMode` → `plan_active.raw`, `TodoWrite` → mechanical step sync, `Edit`/`Write`/`MultiEdit`/`NotebookEdit` → +1 drift counter, sensitive Bash call → +20. THEN one row into `observations`, for OBSERVED tool calls only (mode allowlist / skip list — `core.modes.should_observe`). No LLM. Measured ~180-290 ms end to end, of which ~75-120 ms is interpreter start-up. |
| `UserPromptSubmit` | [`cc_memory/hooks/user_prompt.py`](../cc_memory/hooks/user_prompt.py) | 8s | Auto-init `.ccm/` on first contact; track turn count; save prompt for the Stop observer; on the first non-scaffolding prompt (once per session — `strip_scaffolding`, shared with `pre_compact._first_user_request`, and the `cc_mem_seeded_` marker; v2.14.0), tag the session and seed `progress.current_request` (typing the trigger `resume_request` vs `user_prompt` from the bilingual resume-signal whitelist). |

### Hook stdout contract

Each hook's stdout has a specific role, and violating it is a user-visible bug:

- `SessionStart` stdout → injected context (read by Claude).
- `Stop` stdout → status line(s): one `[cc-memory] …` line every turn
  (`stop.py:582-587`), plus at most one `[cc-memory.plan] …` advisory line.
- `PreCompact` (sync) stdout → ONE status line (shows in the next session's
  compacted context).
- `PreCompact` (async) / `PostToolUse` / `UserPromptSubmit` stdout → empty. The
  async leg's stdout is not shown inline at all (`consolidate_async.py:37`).

### PreCompact: why two legs

v2.3.1 raised the sync timeout 45→120s, but the every-Nth-session LLM
consolidation could still overrun on large DBs, so
`Compacted PreCompact … failed: Hook cancelled` still surfaced. v2.3.2 split
the event:

- The **sync leg** keeps only fast, handoff-critical work (extraction +
  PROGRESS.md, ~1-5s; `pre_compact.py:5-20`).
- The **async leg** runs `core.consolidate.run_consolidation` under a
  `BudgetGate` with `_BUDGET_TOTAL_S = 240.0` and `_BUDGET_SAFETY_S = 8.0`
  (`consolidate_async.py:71`), so the last LLM call it starts finishes by
  `total_s - safety_s` = 232s < the hook's own 300s timeout — the worker is
  never killed mid-write.
- Cadence is an **interval marker + lock**, not a fragile
  `session_count % N` check: `.ccm/.last_consolidation.json` records the
  session count at the last successful run and
  `.ccm/.consolidation.lock` prevents overlapping workers (a lock older than
  `_STALE_LOCK_S = 360.0`, `consolidate_async.py:75`, is reclaimed). This is
  race-immune against the concurrent sync leg — a ±1 drift in the count can
  cause neither a double-run nor a miss (`consolidate_async.py:19-28`).
- **Backpressure is the third trigger (v2.12.0).** The sessions interval
  assumes compactions happen; a project worked in short sessions never
  compacts, and starved — measured on this repository, 349 rows written in
  one month against a 17-day-old marker, with SessionStart injecting topic
  summaries three minor versions stale.
  `core.consolidate.consolidation_backlog` reads the marker's
  `last_memory_id` row-id watermark and declares a run due at 50
  unconsolidated rows, or 7 days with ≥ 10 new rows. The Stop hook probes it
  every turn (one COUNT query, `stop.py:_maybe_kick_consolidation`) and
  spawns the SAME async worker detached (`consolidate_async.py --cwd`); the
  worker re-checks under the lock, so a racing spawn is a no-op, and a
  `.consolidation.kick` cooldown (10 min) bounds respawn of a failing
  worker. Marker I/O is shared with the manual CLI path
  (`core.consolidate.read_consolidation_marker` /
  `write_consolidation_marker`), which now stamps the marker too — and
  `/cc-mem consolidate --deep` loops the semantic-dedup judge until dry
  (`core.consolidate.deep_dedup`) to pay a backlog down in one sitting. Full
  cadence contract:
  [CONTRACTS.md § When consolidation actually runs](CONTRACTS.md#when-consolidation-actually-runs-v2120--backpressure).

### Timeouts are declared twice and must stay in lockstep

`hooks/hooks.json` is the marketplace/dev declaration and is the source of
truth. `cc_memory/ui/installer.py` `HOOK_SCRIPTS` / `ASYNC_HOOK`
(`installer.py:108-114`) is the standalone-install declaration. Since v2.5 those
entries carry the **final wire values** — PreCompact 120 (sync) / 300 (async),
SessionStart 15, Stop 22, PostToolUse 8, UserPromptSubmit 8 — and
`_declared_hook_timeouts()` (`installer.py:701-732`) *reads* `hooks/hooks.json`
whenever it is available (dev checkout, or `cc_memory_meta/hooks.json` inside a
frozen build), falling back to the literal table only for a flat/frozen install
where that file is absent.

The `platform`-based `× 1.5` Windows multiplier that used to express these as
base timeouts is **deleted**. It made the standalone install disagree with the
marketplace install on three of five events (Stop 33 vs 22, PostToolUse 12 vs 8,
UserPromptSubmit 12 vs 8). Raising a timeout now means editing `hooks/hooks.json`
**and** the fallback table together; `tests/test_surfaces.py` asserts they agree
numerically.

### The observation gate no longer shadows the plan branches (fixed in v2.5)

Through v2.4.3, `post_tool_use.py` returned early when
`core.modes.should_observe(mode, tool_name)` was false, and the live-plan
integration block sat **after** that gate. `should_observe` is a skip-list check
followed by an allowlist check against `observe_tools`, and in all three shipped
modes:

- `TodoWrite` is in every mode's `skip_tools` → `should_observe` is False;
- `ExitPlanMode` is in no mode's `observe_tools` → `should_observe` is False.

So the entire v2.2 live-plan anchor was **dead through its own hook**:
`plan_active` was never written by `PostToolUse`, `.ccm/.plan_raw.md` and
`.ccm/PLAN.md` never appeared from a plan-mode exit, and the drift counters
silently varied by mode (the edit bump fired in `code` and `writing` but not
`research`; the sensitive-Bash bump fired in `code` and `research` but not
`writing`). The live plan was reachable only through `/cc-mem plan-set`, and
`tests/smoke_test.py` exercised `core.plan.capture_exit_plan_mode` /
`apply_todowrite_sync` directly rather than through the hook, so the suite never
caught it.

`_apply_plan_integration` (`post_tool_use.py:88-120`) now runs **above** the gate
(called at `post_tool_use.py:88-120`), and `should_observe` wraps only the
`insert_observation` block. Measured per mode (code / research / writing):
`ExitPlanMode` → `plan_active` rows `0/0/0` → `1/1/1`; `Edit` →
`edits_since_last_guardian` `1/0/1` → `1/1/1`; Bash `git push` (1 edit + 20)
`21/20/1` → `21/21/21`.

**The invariant, stated so it is not re-broken:** mode selects what is worth
*remembering*; it must never decide whether the plan anchor tracks reality. Plan
control is not observation. Do not move the block back under the gate, and do
not "fix" it by adding `TodoWrite` / `ExitPlanMode` to the mode allow-lists —
`core/modes.py`'s `should_observe` docstring records both prohibitions.

Note also that `config.json` no longer carries an `observation.skip_tools` key.
It was one of ~34 leaf keys with no reader, all deleted in v2.5; the live filter
has always been `core/modes.py`'s per-mode `skip_tools` + `observe_tools`.

---

## 4. Database schema

SQLite tables (defined in [`cc_memory/core/db.py`](../cc_memory/core/db.py)),
project-local at `<project>/.ccm/memory.db`, WAL mode:

| Table | Purpose |
|-------|---------|
| `projects` | One row per project (`db.py:37`) — identified by the database it sits in, not by the `path` string it records: a moved or renamed directory re-attaches its own row instead of minting a second one (§7); carries `mode` since migration `v2_project_mode` (`db.py:144`) and the durable observer cursor `obs_watermark` since `v7_projects_obs_watermark` |
| `sessions` | One row per compaction event (`db.py:46`); carries `complete` since `v7_sessions_complete` (backfilled, so pre-v7 rows read as complete) |
| `memories` | Extracted facts (category, importance, topic, content_hash, **supersedes_id**, last_referenced_at) (`db.py:57`) |
| `topics` | Consolidated summaries per topic name (versioned) (`db.py:71`) |
| `keywords` | Auto-detected project vocabulary (`db.py:81`) |
| `plans` | Plan queue (draft → ready → done) (`db.py:90`) |
| `observations` | Raw PostToolUse events, cleaned up after extraction (`db.py:131`) |
| `session_summaries` | 6-field structured summary per session (request / investigated / learned / completed / next_steps / notes) + files_read/files_modified (`db.py:144`) |
| **`progress`** | NEW in v2.1 — single row per project. SOT for `.ccm/PROGRESS.md` (`db.py:188`). |
| **`plan_active`** | NEW in v2.2 — single row per project. SOT for `.ccm/PLAN.md` (`db.py:212`). Carries `turns_total` since `v9_plan_turns_total`: a MONOTONIC turn count that nothing resets, distinct from `turns_since_last_guardian`, which every guardian check and plan replacement zeroes |
| **`directives`** | NEW in v2.11.0 — the user-INTENT ledger. `times_stated` accumulates on ONE row per `slug`; a directive outlives every plan, which is why it is not plan steps. Carries `turns_at_touch` since `v9_directives_turns_at_touch` — the value of `turns_total` when it was last written, so idleness is subtraction between two monotonic numbers. Since v2.12.0 `status` may also be `blocked` (parked on the user, idle-exempt) and `kind` may be `constraint` (a standing prohibition, idle-exempt) — vocabulary additions, no schema change; only `directive-add` may bump the count (`directive-edit` corrects fields without touching it) |
| `_migrations` | Tracks applied migrations (`db.py:651`) |

Twelve tables, matching `CLAUDE.md` § "Database schema (12 tables)".

Plus `memories_fts` — an FTS5 virtual table over `memories` (`core/db.py:455-458`),
kept in sync by three triggers (`core/db.py:459-478`, migration `v2_fts5` at
`db.py:3178-3212`). It is created only when the local SQLite build has FTS5; otherwise
`db.search_fts` (`core/db.py:3178-3212`) falls back to `LIKE ? ESCAPE '\'`
(`core/db.py:3178-3212`). FTS5 is advertised in `.claude-plugin/plugin.json:4`
and `:12`, and `/cc-mem status` reports which path is live (`cli/mem.py`,
`cmd_status`).

The `supersedes_id` column on `memories` (migration `v3_supersedes`,
`db.py:168`) makes the anti-patch chain explicit: when `upsert_smart` decides a
new memory supersedes an old one, the new row links back to the old row's ID
(and the old row is archived). Walking the chain via
`db.get_supersede_chain(memory_id)` (`db.py:1667-1682`) shows the full update
history. `content_hash` (migration `v2_content_hash`, `db.py:1667-1682`) is
`sha256[:16]` of the normalized content, used for the cheap exact-duplicate
check (`db.compute_content_hash` at `db.py:2222-2224`, `db.find_by_hash` at
`db.py:2222-2224`).

Migrations are applied in order from the `_MIGRATIONS` list (`db.py:121-284`) and
recorded in `_migrations`. Levels shipped so far: **v1** (`topic` column +
index), **v2** (content_hash, observations, session_summaries, project mode,
FTS5, hash backfill), **v3** (anti-patch + forced handoff: `supersedes_id`,
`progress`), **v4** (`plan_active`), **v5** (session annotation:
`progress.current_session_id`, `progress.session_started_at` — so a
multi-session workflow can tell from PROGRESS.md whether it is reading its own
write, `db.py:230-233`), **v6** (reference-aware aging:
`memories.last_referenced_at`, set on injection, so effective age is
`now - COALESCE(last_referenced_at, created_at)` and a referenced fact stays
"young", `db.py:244-248`), **v7** (round-7/8 hardening: `plan_active.revision`
for optimistic plan concurrency, `sessions.complete` + backfill,
`projects.obs_watermark` for the durable observer cursor, and the two
recency indexes `idx_memories_session` / `idx_sessions_sid` that turned two
measured quadratics linear — 557.68 ms → 4.31 ms at 2 000 sessions).

The `progress` row's user-facing fields are `current_request`, `status_done`,
`status_in_flight`, `status_blocked`, `open_todos`, `plan`, `critical_context`,
`files_touched`, `transcript_ptr`, `updated_at`, `trigger_type` (11 —
`db.py:188-201` — plus the two v5 session-annotation columns, 13 non-PK columns
in all). The `plan_active` row holds `raw`, `structured`, `active_step`,
`edits_since_last_guardian`, `turns_since_last_guardian`, `last_guardian_at`,
`last_refined_at`, `needs_refine`, `created_at`, `updated_at`
(`db.py:210-222`), plus `revision` since `v7_plan_revision` — every UPDATE
bumps it, and a writer that computed its state from a read passes the
revision it read (`update_plan_if_revision`), so a plan changed underneath
is a refused write, not a silent overwrite.

All queries use parameterized statements; string-formatted SQL is prohibited.

---

## 5. Data flow

### Memory write flow (anti-patch)

```
caller (PreCompact / Stop observer / SessionStart retroactive save /
        /save-memories skill / MCP add / mem.py add /
        dashboard Add-Memory + Save-Session / web_viewer add)
  │
  ▼
llm.memory_writer.upsert_smart(db, project_id, session_id, category, content,
                               importance, tags, topic)
  │
  ├─ 0. clean_for_storage + reject content < 10 chars ("too_short"); coerce an
  │      unknown category to "note"; clamp importance to 1..5
  │
  ├─ 1. compute_content_hash → exact match (SQL, inside the transaction)
  │      → no new row and no content rewrite; importance = max(new, old)
  │        and tags are unioned, since neither is part of the hash
  │      → "reinforced" when that changed the row, "skipped" when it did not
  │
  ├─ 2. find the most similar ACTIVE memory (Jaccard on character trigrams).
  │      Scope: memories in the same topic when a topic is set AND that scan
  │      yields candidates; otherwise a category-scoped scan of the 50 most
  │      recently updated (memory_writer._find_similar, memory_writer.py:102-131)
  │      │
  │      ├─ sim >= 0.80 → MERGE_IN_PLACE (db.update_memory)
  │      │                  no new row, no stacking; importance = max(new, old);
  │      │                  tags gain "merged"
  │      │
  │      ├─ sim >= 0.50 → SUPERSEDE (db.supersede_memory)
  │      │                  archive old, insert new with supersedes_id link;
  │      │                  importance = max(new, old); tags gain "supersedes"
  │      │
  │      └─ sim <  0.50 → fall through to insert
  │
  └─ 3. INSERT NEW (independent fact)

regenerate_memory_index(db, project_id, memory_dir)   ← MEMORY.md refresh
```

`upsert_smart` itself does **not** regenerate `MEMORY.md`. The refresh is the
caller's responsibility, and there are exactly two shapes:

- `upsert_batch` (`memory_writer.py:318-360`) loops `upsert_smart` per item and
  regenerates ONCE at the end, but only when a `memory_dir` is passed
  (`memory_writer.py:322`). All hook callers pass it
  (`pre_compact.py:435`, `stop.py:166`, `session_start.py:1144`); the sync
  PreCompact leg additionally touches it again after the rest of its state
  changes (`pre_compact.py:806`).
- Single-shot callers call `regenerate_memory_index` explicitly:
  `cli/mem.py:1207` and `:584`, `mcp/server.py:647`, `ui/dashboard.py:1715`,
  `ui/web_viewer.py:1034`, plus the `skills/ccm-load` inline script
  (`skills/ccm-load/SKILL.md:308, 318`). `core/idle.py:96` and
  `hooks/consolidate_async.py:276` also refresh it after maintenance.

(The pre-merge diagram showed regeneration as an unconditional step of
`upsert_smart` and elided the `db` argument; both are corrected above against
`memory_writer.py:318-360, 190, 199`. The caller list is likewise the full set found
by grepping `upsert_smart|upsert_batch` across `cc_memory/`.)

Thresholds live in ONE place — `memory_writer.HIGH_SIM = 0.80`,
`MID_SIM = 0.50`, `MIN_CONTENT_LEN = 10`, `MAX_CANDIDATES_TO_SCAN = 50`
(`memory_writer.py:75`). They are no longer mirrored in `config.json`: that
`writer` block was read by nothing and was deleted in v2.5, because an inert
tunable is worse than no tunable. See
[docs/CONTRACTS.md](CONTRACTS.md#anti-patch-contract) for the full contract.

### The privacy filter (step 0) — linear, uncapped, fail-closed (v2.5)

`clean_for_storage` (`core/privacy.py`) guards both the LLM-facing path
(`core/extractor.py`) and the memory write path, so its failure mode is a leak
in two directions at once. Through v2.4.3 it was
`re.sub(r"<private>.*?</private>", "", text)` behind a
`text.count("<private>") > 100` ReDoS guard — and that guard **returned the text
unchanged**, i.e. it failed OPEN exactly when the payload looked adversarial:
100 tags stripped correctly, 101 leaked into the Anthropic request *and* the
`memories` table.

The cap was calibrated on the wrong signal as well. Well-formed tags are cheap
for the regex engine; an **unterminated** `<private>` is the quadratic case,
because every open-tag position rescans the remainder for a close tag that is
not there (measured, CPython 3.13):

| input | `re.sub` | linear scan |
|---|---|---|
| 20,000 well-formed tags (996.1 KiB) | 6.0 ms | 5.1 ms |
| 16,000 unterminated tags (140.6 KiB) | **9,517.4 ms**, tail leaked | 0.0 ms, tail dropped |

`_strip_tagged_spans` is a single left-to-right `str.find` pass with no
backtracking, so no cap is needed at all — and a dangling open tag now fails
**CLOSED**: everything from it to the end of the text is dropped rather than
emitted. Pairing semantics are unchanged (each open tag binds to the first
following close tag), verified on 20,000 random tag-soup inputs with zero
behavioural differences across all 13,328 well-formed ones.

The same class of bug lived in `hooks/post_tool_use.py`, which computed
`is_private` **after** `_truncate_output` — and that helper replaces a `Read`
body with the literal `"(file content)"`. A `Read` of a file the user had marked
private was therefore stored with `is_private=0`, and since `is_private` is the
only filter in `db.get_recent_observations` / `get_observations_since`, that
row's path reached the Stop observer, the PreCompact extraction prompt, and
`progress.files_touched`. Classification now runs on the raw input/response,
before either lossy truncation helper.

### Handoff flow (forced)

```
PreCompact (sync):
  _write_attempt(memory_dir, trigger, claude_sid, transcript_bytes)
    ↓                              ← start marker BEFORE the transcript load
  load_transcript_window(transcript_path)   ← bounded head+tail read
    ↓
  collect_progress_state(db, project_id, memory_dir, ...)
    ↓
  db.upsert_progress(...)                   ← full overwrite of progress row
    ↓
  write_progress_md(db, project_id, memory_dir)   ← FULL REWRITE of .ccm/PROGRESS.md
    ↓
  .last_save.json (incl. trigger: auto|manual) + _clear_attempt(memory_dir)

Stop (every turn):
  db.tag_progress_session(project_id, session_id)   ← v5, before the patch
    ↓
  db.patch_progress(files_touched=..., trigger_type="stop")
    ↓
  write_progress_md(db, project_id, memory_dir)   ← FULL REWRITE again (idempotent)

UserPromptSubmit (first non-scaffolding prompt, once per session):
  db.tag_progress_session(project_id, session_id)
    ↓
  db.patch_progress(current_request=<user msg>,
                    trigger_type="resume_request" | "user_prompt")
    ↓
  write_progress_md(db, project_id, memory_dir)

SessionStart:
  inject context blob (standing directives 10% + topics 25% + critical 15% +
                       timeline 15% + PROGRESS preview 25% + footer 10% of a
                       ~16000-char budget — session_start.py:48-56)
  footer may carry: killed-PreCompact warning (surviving .pre_compact_attempt.json,
                    after a 10-minute grace window), OAuth/api-key warnings, counts
  emit: <system-reminder>
          You MUST Read .ccm/PROGRESS.md and .ccm/MEMORY.md before
          responding to any user request. Explicitly state in your reply:
          "Read PROGRESS.md — prior progress: <summary>."
          … plus the RESUME PROTOCOL (bilingual token whitelist → auto-execute
          open_todos[0]).
        </system-reminder>
```

Call signatures above are the real ones: `write_progress_md(db, project_id,
memory_dir)` (`core/progress.py:331-490`; call sites `pre_compact.py:775`,
`stop.py:473`, `user_prompt.py:52`, `session_start.py:946`, `mcp/server.py:243`,
`cli/mem.py:1298`). See
[docs/CONTRACTS.md](CONTRACTS.md#handoff-contract) for the PROGRESS.md
schema.

### Killed-run detection (v2.4.2)

A `PreCompact` killed by the host timeout dies on `TerminateProcess`: no
`except`, no `finally`, so `.last_save.json` still describes the *previous*
successful run and the failure is invisible. The sync leg therefore writes
`.ccm/.pre_compact_attempt.json` **before** the transcript load
(`pre_compact.py:359-368`) and removes it only on a completed run
(`pre_compact.py:795`) — including on its own error path (`pre_compact.py:731`),
so an *errored* run is never reported as a *killed* one. `SessionStart` reports
a surviving marker, but only once it is at least 10 minutes old, so a run still
in flight is never mislabelled (`session_start.py:187-206`).

### Transcript ownership: no fuzzy matching, ever (v2.5)

Two SessionStart paths read a `.jsonl` transcript that PreCompact never
processed — `retroactive_save` (LLM extraction of unsaved prior sessions) and
the tier-3 `_refresh_progress_row` mine (`open_todos`, `files_touched`,
`transcript_ptr`). Both resolve the project's directory under
`~/.claude/projects/`, and through v2.4.3 both could resolve to **another
project's** directory.

The slug convention is: replace **every** character outside `[A-Za-z0-9]` with
`-`. cc-memory replaced three of them (`:` `\` `/`), so any project path
containing `_` or `.` mangled to a slug that does not exist — and the miss fell
through to a fuzzy substring search over every slug directory on the machine.
Blast radius on the reference box (179 slug directories): the substring `core`
matched 131 of them, `app` and `data` 141 each, `proj` 33. A project could
ingest a foreign transcript, send it to Haiku, and store the extracted facts as
its own memories.

Three changes close it:

1. `core.extractor.mangle_project_path` is the single source of truth for the
   convention (`extractor.py:535-571`), used by `find_latest_transcript`,
   `hooks/session_start.py` and `ui/dashboard.py` — which had carried a verbatim
   copy of the old resolver, fuzzy branch included.
2. The fuzzy fallback is **deleted**. A miss returns `None`. Callers must treat
   that as "no transcript", never as licence to guess.
3. Ownership is checked positively. `_transcript_belongs_to`
   (`session_start.py:739-756`) reads the `cwd` the transcript's own records carry
   and is **fail-closed** — no `cwd`, no ingest — and gates `retroactive_save`
   after the bounded window load. The tier-3 mine uses the deliberately weaker
   `_transcript_is_foreign` (`session_start.py:759-786`): absent `cwd` is allowed,
   a *different* `cwd` is refused. The two differ on purpose — retroactive save
   persists LLM-extracted memories forever and should demand proof, while
   tier-3 must still work for the cwd-less transcript shape
   `tests/smoke_test.py:266-278` builds.

Measured: with two planted transcripts, one foreign, retroactive save went from
2 LLM legs ingesting `['aaaa-foreign', 'bbbb-mine']` to 1 leg ingesting
`['bbbb-mine']`; a transcript with no `cwd` at all yields 0 legs and 0 memories.
Tier-3 went from `open_todos=[{'content': 'FOREIGN TODO leak'}]` +
`files=['FOREIGN_SECRET_FILE.py']` + a foreign `transcript_ptr` to empty, with
the refusal logged. The window is loaded **before** `transcript_ptr` is written,
because a pointer to another project's transcript is itself contamination.

### Live plan flow (v2.2)

`ExitPlanMode` output (or user-supplied `/cc-mem plan-set` text) lands in
`plan_active.raw` with `needs_refine = 1`; the `plan-refiner` subagent
normalises it to JSON, written back via `/cc-mem plan-set --from-refiner`;
`TodoWrite` events sync step statuses mechanically by trigram-Jaccard match (no
LLM); `Edit`/`Write`/`MultiEdit`/`NotebookEdit` bump
`edits_since_last_guardian`, and sensitive Bash calls (`git push`, `rm -rf`,
`DROP TABLE`, `npm publish`, `kubectl apply`, `terraform apply`, … —
`core.plan.is_sensitive_tool_call`, `plan.py:1375-1398`) bump it by 20. Once
`turns_since_last_guardian >= 8` OR `edits_since_last_guardian >= 12`
(`core.plan.should_nudge_guardian`, `plan.py:1339-1355`), the Stop hook
**refuses the turn** rather than advising (v2.11.0 — the rate-limited nudge
this sentence used to describe is deleted; see
[CONTRACTS.md](CONTRACTS.md#the-stop-hook-can-refuse-the-turn-v2110) for the
escape budget). Hooks never spawn plan subagents themselves — the refusal
tells the main Claude to. Full spec:
[docs/CONTRACTS.md](CONTRACTS.md#plan-contract). Every branch above runs in
every mode since v2.5 — see
[the observation gate](#the-observation-gate-no-longer-shadows-the-plan-branches-fixed-in-v25)
for what used to shadow them.

A raw plan that has not been refined yet is no longer invisible:
`core.plan.raw_pending_refinement` (`plan.py:402-431`) is the shared predicate, and
both `write_plan_md` and `/cc-mem plan-status` lead with a PENDING REFINEMENT
banner plus the verbatim raw text, labelling any older structured plan as
superseded. The verbatim block's fence widens past the longest backtick run in
the raw text, because plan-mode output routinely contains code fences.

---

## 6. LLM backends and auth

`llm.ccl_backend.call_llm` calls Anthropic Haiku (model
`claude-haiku-4-5-20251001`, `ccl_backend.py:222-326`). Callers resolve one
credential up front with `core.auth.get_api_key()` and pass it in; `call_llm`
tries that one FIRST, then FALLS THROUGH to the remaining
`core.auth.get_api_candidates()` entries when a leg fails — bounded to 2
Anthropic legs total (`ccl_backend.py:264`), so the worst-case wall-clock stays
a known quantity for the consolidation BudgetGate. Candidate order and wire
format (`core/auth.py:20-57`, `_wire_for` at `core/auth.py:8-17`,
`_call_haiku` headers at `ccl_backend.py:97-127`):

1. `ANTHROPIC_API_KEY` env var → `x-api-key` header
2. Claude Code OAuth token in `~/.claude/.credentials.json` (auto-detected,
   `expiresAt`-validated, ms epoch) → `Authorization: Bearer` +
   `anthropic-beta: oauth-2025-04-20`

The wire distinction is not cosmetic: verified live 2026-07-14, an
`sk-ant-oat…` token sent via `x-api-key` gets HTTP 401 "invalid x-api-key"
while the same token via Bearer + beta gets HTTP 200 (`core/auth.py:14-15`).

`get_api_key()` is the single-credential back-compat view of that same list (it
does not retry, `core/auth.py:60-93`); it also carries the `oauth_expired`
signal behind SessionStart's "[WARNING: OAuth expired — LLM extraction
disabled]" footer (`session_start.py:665`). Hook callers use it to *supply*
the credential passed into `call_llm`: `pre_compact.py:94 → :166`,
`stop.py:86`, `session_start.py:665`, `core/consolidate.py:426, 549, 724`.

Fall-through was added in v2.3.4 for a concrete failure: a dead env key (e.g.
zero credit → HTTP 400) used to blackhole the healthy subscription token behind
it and silently push every LLM call onto Ollama, cold-loading a 5.9 GB local
model per consolidation batch (`core/auth.py:30-33`, `ccl_backend.py:10-12`).

The local Ollama fallback is **opt-in and OFF by default**
(`cc_memory/config.json` `ccl.enabled: false`;
`ccl_backend.py:36` `_DEFAULT_OLLAMA_ENABLED = False`, so a missing key also
reads as False), alongside `ccl.ollama_url` / `ccl.local_model`. When disabled
the leg is skipped and only recorded as the reason string
`"ollama: disabled (config ccl.enabled=false)"`, so a default install has no
local fallback at all.

### Bounding wall-clock: `fallback_timeout` and `deadline`

`call_llm` (`ccl_backend.py:222-326`) offers two independent bounds.

`fallback_timeout` bounds the Ollama leg. When `None` it defaults to
`min(timeout*3, 120)`. The worst-case envelope of one call is then

```
2 * timeout  +  (fallback_timeout if ccl.enabled else 0)
```

because the Anthropic candidates are bounded at 2 (`ccl_backend.py:274`). That
arithmetic is what lets the consolidation `BudgetGate` guarantee completion —
see `core.consolidate._worst_call_cost`.

**`deadline` is the stronger bound, and the one a hook must use.** It is an
absolute `time.monotonic()` instant by which the call must be FINISHED: every
leg's effective timeout is clamped to the time actually remaining, and a leg
with less than the minimum left is skipped outright. Total wall-clock is
therefore bounded by the deadline no matter how many credential candidates
exist, which lets a caller keep a generous per-leg `timeout` for the common
single-candidate path instead of shrinking it to survive the pathological one.

Through v2.4.3, `core/consolidate.py` was the *only* module that honoured any of
this, and three hooks <!--ce:hooks:subset--> with hard host timeouts violated it — one of them with the
shipped default config:

| call site | host budget | pre-v2.5 envelope, `ccl` off | on |
|---|---|---|---|
| `hooks/stop.py` | 22 s | 16 s | 40 s ✗ |
| `hooks/pre_compact.py` | 120 s | 50 s + ~26 s transcript work | 125 s ✗ |
| `hooks/session_start.py` | 15 s | **40 s ✗** | 100 s ✗ |

`session_start`'s pre-v2.5 `_RETRO_DEADLINE_S` only decided whether to start
another *file*; it could not interrupt a leg already in flight, so one hung
socket was 40 s against a 15 s kill. All three hooks <!--ce:hooks:subset--> now capture `_HOOK_T0 =
time.monotonic()` **before** their package imports (so import cost is charged
against the budget) and pass `deadline=_HOOK_T0 + <budget>` into `call_llm`:
`stop.py` `_LLM_DEADLINE_S = 14.0`, `pre_compact.py` `75.0`,
`session_start.py` `_RETRO_DEADLINE_S = 13.0`. Measured with deliberately
stalled legs: Stop `25.45 s → 15.99 s` of 22 s; PreCompact `~144 s → 74.39 s`
of 120 s. Normal-path latency is unchanged (0.29 s → 0.30 s), because a leg with
plenty of budget still gets its full per-leg `timeout`.

The honest bound is `deadline + (k-1) * timeout`, where `k` is the observed
overrun ratio of a socket timeout (the deadline stops legs from *starting*; the
final leg can still be in flight). At the measured `k ≈ 1.48` that is 17.4 s of
Stop's 22 s and 87 s of PreCompact's 120 s.

If every enabled leg fails, `call_llm` raises `RuntimeError` carrying the
aggregated per-leg reasons (`ccl_backend.py:222-326`) and hooks degrade
gracefully — extraction is skipped, but archives/handoff/observations still
save. Hooks NEVER raise into Claude Code. (That last sentence only became true
in v2.4.2: `_extract_via_llm`'s `except` tuple did not include `RuntimeError`,
so a total LLM outage escaped to the hook's outer handler and skipped the
`PROGRESS.md` rewrite along with extraction — see `CHANGELOG.md` under 2.4.2.)

---

## 7. Per-project state (.ccm/)

Per-project state lives at `<project>/.ccm/`:

```
<project>/.ccm/
├── memory.db                    SQLite (WAL mode, all tables)
├── MEMORY.md                    auto-generated, refreshed after every batch write
├── PROGRESS.md                  full-rewrite from `progress` row, every Stop+PreCompact
├── PLAN.md                      full-rewrite from `plan_active` row (v2.2)
├── .last_save.json              status from last PreCompact (incl. auto/manual trigger)
├── .last_inject.json            what SessionStart actually injected (v2.3)
├── .last_consolidation.json     session count + row-id watermark at last
│                                consolidation (v2.3.2; watermark v2.12.0)
├── .consolidation.lock          prevents overlapping async workers (v2.3.2)
├── .consolidation.kick          backpressure spawn cooldown (v2.12.0)
├── .pre_compact_attempt.json    start marker; survives ⇒ last run was killed (v2.4.2)
├── .plan_raw.md                 last raw ExitPlanMode capture (v2.2)
├── .plan_history/               append-only archive of replaced/cleared plans (v2.4.0)
├── .gitignore                   ignores memory.db (+ -wal/-shm), sessions/, and every
│                                dot-prefixed runtime artifact above, plus *.tmp — the
│                                three generated .md files are deliberately NOT ignored
├── sessions/YYYY/MM/            archived per-session summaries
└── topics/                      reserved for future per-topic md exports
```

Writers, for traceability: `MEMORY.md` ← `memory_writer.regenerate_memory_index`
(`memory_writer.py:261-370`); `PROGRESS.md` ← `core.progress.write_progress_md`
(`progress.py:331-490, 366`); `PLAN.md` ← `core.plan.write_plan_md`
(`plan.py:733-782`); `.plan_history/` ← `plan.py:733-782`; `.last_save.json` ←
`pre_compact.py:737, 771`; `.last_inject.json` ← `session_start.py:291-309`
(tempfile + `os.replace`, genuinely atomic, unlike the plain write used for
`.last_save.json`); `.last_consolidation.json` ←
`core.consolidate.write_consolidation_marker` (one writer, async hook + CLI);
`.consolidation.lock` ← `_acquire_lock` (`consolidate_async.py:121-155`);
`.consolidation.kick` ← `stop.py:_maybe_kick_consolidation`;
`.pre_compact_attempt.json` ←
`pre_compact.py:284-311`. `sessions/` and `topics/` are created by whichever
path touches the project first — `user_prompt.py:57-63` on auto-init, or
`pre_compact.py:342-343`.

`.ccm/PROGRESS.md`, `.ccm/MEMORY.md`, and `.ccm/PLAN.md` are **generated
artifacts**. Edit the SQL source of truth instead (`progress` for PROGRESS.md,
`plan_active` for PLAN.md, `memories`/`topics`/`keywords` for MEMORY.md).

**Identification is tri-state, and a link is not a state directory
(v2.14.0).** `core.layout` decides where the state directory IS — `.ccm/`,
or a pre-v2.13.0 `memory/` awaiting its one-way rename — by identifying the
legacy directory's CONTENTS, never by its name (`CLAUDE.md` § v2.13.0).
Through v2.13.2 every probe returned a plain False when it could not run, so
a lock held for one second, an antivirus hold or a CANTOPEN took the same
branch as "not ours" — the irreversible one: `.ccm/` was created empty
beside a `memory/` holding every memory the project had, and from then on
the settled case answered `.ccm/`. The probes now answer `True` / `False` /
`layout.UNKNOWN`; `UNKNOWN` is falsy, so every write guard keeps failing
closed, and `migrate_legacy_dir` alone routes it to the refused-rename branch
(keep using `memory/`, retry next turn). Its settled case requires
`.ccm/memory.db` to hold bytes, and an empty `.ccm/` beside a positively-ours
`memory/` returns `memory/`. A symlinked or junctioned `.ccm` is not a state
directory at all (`_is_usable_state_dir`, through `core.markers._is_link`):
the resolver never follows it, never renames onto it, and `find_memory_dir`
refuses it on the read side too; `pre_compact`'s last-resort handler — the
one recovery path that re-derived the location after `ensure_memory_dir` had
refused the link — re-applies the same probe before it writes.

### Which `<project>` — root anchoring (v2.6.0)

`<project>` is **not** the `cwd` the hook payload carries. That cwd is the
session's CURRENT working directory and follows the agent's own `cd`, so a
session launched at a repo root that ran one command inside `cli/` began
reporting `<root>/cli` — and `_init_project_if_needed` (`user_prompt.py:106-137`)
mkdir'd a second, fully independent database there. Four of the six hooks <!--ce:hooks:subset--> gate
on `.ccm/memory.db` merely EXISTING, so once born the stray kept being
written: measured 27 memories and its own `projects` row in one such database,
against 161 in the real one two levels up. It also had no `.gitignore` (only
the directory the init path creates gets one), so a 184 KB binary `memory.db`
rode into three commits of the user's repository.

**Prevention, not migration — the load-bearing decision.** The first draft of
this resolver tried to *heal* an existing stray by taking the outermost end of
a contiguous run of database-bearing ancestors, on the theory that a stray is
always deeper than the real root. An adversarial design review killed it
against ground truth: enumerating every `.ccm/memory.db` on the reporting
machine found **20** databases, and **four** of them are legitimately nested
inside another one — `Claude-Code-Local/companion` alone holds 3725 memories
and carries its own `.git`. A stray sub-database and a deliberate nested
sub-project are **byte-for-byte indistinguishable on disk**: both have
`.ccm/memory.db` whose `projects` row names their own directory, because
`upsert_project` (`core/db.py:1172-1209`) records whatever cwd it was handed.
Outermost-wins resolves that ambiguity unconditionally in the direction that
destroys data, so the first post-upgrade session in `companion` would have
moved 3725 memories out of reach, silently.

An existing database is therefore a **declaration of identity** and is never
overridden. The reported bug is fixed by *prevention*: the marker rung runs
before any database exists, so the stray is never born. Adopting one that
already exists means merging two SQLite files — destructive and irreversible —
which belongs in an explicit, confirmed command, not in a hook that runs on
every prompt.

**The same rule holds INSIDE the database.** `upsert_project` records the
resolved cwd in `projects.path`, and through v2.13.2 that string was also the
project's identity: a moved or renamed project directory got a second row, and
every memory, session, progress row, plan and directive of the first went dark
on every surface — SessionStart injected 0 memories, `/cc-mem list` printed
`(none)`, `status` minted the second row itself and reported an empty
database — while the rows sat one `project_id` away. `cli/mem.py` documented
the symptom (register C4) and told the user to inspect the old rows by hand;
the consolidation marker grew a path check to survive it. The database's
location is the declaration now. `MemoryDB.upsert_project` matches a row by
exact path, then by `core.layout.canonical_path` — resolved, then
`normcase`d: the ONE comparable spelling every identity compare in the tree
uses (the consolidation marker, the root resolver's home boundary, the
dashboard's project registry, the `excluded_projects` opt-out) — and only
when nothing matches AND the database sits at `<cwd>/.ccm/memory.db`
(`core.layout.database_owner`) does it re-attach a row to `cwd`: the most
recently active row whose recorded directory no longer exists. A row whose
directory still exists elsewhere is another live directory's and is never
taken — not even when it is the only row in the file — and a database that is
not `cwd`'s own never re-attaches anything, so a sibling row deliberately
sharing one file keeps its identity. `MemoryDB.find_project_id` is the same lookup for
the surfaces that ask a question (`status`, `stats`, `list`, `sessions`,
`keywords`): it re-attaches, it never inserts. The consolidation marker
carries the row's `project_id`, so a rename no longer costs the "one early
consolidation" the path check used to charge, and its `project_path` is
stored resolved, so the CLI's documented `--project .` matches the absolute
cwd a hook reads with. A `MemoryDB` constructed on the legacy
`memory/memory.db` before the rename follows it: `_connect` retries once
through `MemoryDB._follow_state_dir`, from the legacy name to `.ccm/` only,
only when the new file exists and passes the constructor's link refusal, and
only after a connect actually failed — so a dashboard, web viewer or MCP
server that outlives the rename keeps answering, and nothing but the
migration ever joins the legacy name.

`project_root` (`core/roots.py:682-727`) resolves a root first. Every hook
rebinds `cwd` to it immediately **after** `is_excluded` and never before:
resolving first would widen a per-subdirectory exclusion away by climbing to
its unexcluded parent. Since v2.10.0 that ordering is not a per-hook
discipline but a mechanism: hooks call
`hooks/_entry.py:resolve_project`, the ONE shared gate that runs
`is_excluded` on the raw cwd and then anchors (stdin parsing is shared the
same way via `parse_payload`). `tests/test_surfaces.py` asserts the order
once inside the gate, refuses a direct import in any hook, and
`tools/falsify_fixes.py --case r10entryorder` proves the inversion goes red. The chain of candidate ancestors stops below any home
directory, below the filesystem root, at a `.ccm-root` pin, and after 25
levels (`_chain`, `core/roots.py:359-387`). First hit wins:

0. `cwd` itself has `.ccm/memory.db` → `cwd`. Terminal, before anything else
   is consulted. This single line is what discharges the "never orphan"
   constraint for every database that exists today.
1. The **nearest** ancestor with `.ccm/memory.db` (`_nearest`). No outward
   extension — see above. This is the rung that fixes the reported bug, since
   `CodeEraser/cli` has no database while `CodeEraser` does. It needs no VCS
   and no manifest, which matters for projects that are not repositories.
2. `CLAUDE_PROJECT_DIR`, when it names a directory in the chain (`_from_env`,
   `core/roots.py:661-679`). Ranked *below* the database rungs deliberately:
   it records where Claude Code was launched, which is not authority to orphan
   a database. Containment is likewise the point — a value left over from
   another project must not redirect this one.
3. Project markers — `.git`, `.hg`, `.svn`, `.ccm-root` and the usual
   manifests (`_MARKERS`) — nearest, then extended outward to the enclosing
   repository (`_marker_root`). The only rung that can fire before any
   database exists, i.e. the one doing the actual prevention.
4. The cwd as given — the pre-v2.6.0 answer, so nothing that worked before
   stops working. When the answer is cwd, the ORIGINAL string is returned
   unresolved, which keeps symlinked project directories byte-identical to
   their old behaviour.

**The guards belong to the CANDIDATE SET, not to any one rung (v2.7.0).**
v2.6.0 hung them off the marker rung's extension loop alone, and every rung
that did not inherit them became a separate data-integrity defect: the
database rung consulted nothing, so a `.ccm/` created by one session in a
projects folder captured every uninitialised project under it; the marker rung
never checked the FIRST marker it found, so one stray `package.json` there did
the same; and neither had any notion of a dependency tree. `_candidates`
(`core/roots.py:466-515`) now filters the chain once, before any rung reads it:

- **Containers of projects are removed** (`_is_container`). Two asymmetric
  triggers: two or more children that are VCS roots is always decisive (the
  reporting machine's projects folder has 27), while two or more children that
  merely own a database counts only when this directory owns none itself — a
  project whose own database sits alongside nested ones is a real, observed
  layout. A directory that is itself a VCS root is never a container, or a
  repository with two submodules would stop being resolvable. The read is
  bounded (`_CONTAINER_SCAN_CAP`, v2.12.2): proving the negative used to
  visit every subdirectory of every ancestor on every hook and MCP call,
  which under a 6,366-subdirectory `%TEMP%` cost 3.5-4.4 s per call.
- **Dependency trees are removed** (`_DEPENDENCY_DIRS`). Reading a file under
  `node_modules/`, `vendor/` or `site-packages/` anchors on the project that
  *depends* on the package. v2.6.0 anchored on the package — it has a
  `package.json`, so the marker rung accepted it — and planted a database
  inside the dependency tree, where the reporter did not look. Filtering, not
  truncating: the walk must continue *past* the dependency to reach its owner.
- **A `.ccm-root` pin short-circuits every rule, and a database survives the
  cut (v2.14.0).** The pin exemption used to be attached to `_is_container`
  and to the filesystem-root rule separately, and the dependency-name rule
  never had one — so a project CALLED `external` (or under
  `~/work/external/`) was cut from every chain, pinned and initialised alike,
  and every subdirectory cwd resolved to itself. `_is_pinned` is consulted
  once, here, before any rule; `_dependency_cut` spares a pinned or
  database-owning directory, and the verdict is deliberate: a directory that
  owns a database wins at every depth, dependency name or not — rung 0
  already says so for the directory itself, and `left-pad` → itself while
  `left-pad/lib` → repo was one `cd` flipping the target database. The
  volume-root rule uses `_is_volume_root`, so `/mnt/c` is refused on the same
  terms as the `C:\` it projects, and `_is_profile_dir` recognises
  `/mnt/c/Users/bob` as the profile it is.

The marker rung then has two remaining ceilings: `_MARKER_MAX_RISE` = 6 levels
above cwd (caught by test_surfaces §4 climbing *seven* levels out of a temp
fixture into the real user profile), and a **VCS root, which ends the walk
inclusively** — a repository is the outermost thing that can still be one
project, and using `.git` only as a *stop* signal never *requires* it.

The extension deliberately does **not** require a contiguous run of markers.
v2.6.0 broke the run at the first marker-less ancestor, which is exactly what
`packages/`, `apps/`, `crates/` and `libs/` are, so the standard monorepo
layout resolved to the package and re-created the very stray this module
exists to prevent — while two docstrings promised the workspace.

Two absences from the marker set are deliberate. `CLAUDE.md` is not a marker
because Claude Code supports per-subdirectory ones, and neither is `.claude/`
— the user's home has one, and Claude Code writes one into whatever directory
a session happens to approve a permission in. Both are per-cwd session
residue, not root evidence.

The home boundary is doubled: what the environment reports
(`HOME`/`USERPROFILE`/`Path.home()`, `_home_dirs`) **and** the
platform-conventional shape — a child of a directory named `Users` or `home`
**that itself sits at the filesystem root** (`_is_profile_dir`). Containers,
CI, `sudo` and this project's own test sandbox all redirect the former.
Measured with `HOME` pointed into a sandbox: the walk climbed seven levels out
of a temp fixture into the real profile and matched the `.ccm/memory.db`
that one session run in the home directory had left there. Structure survives
that redirection; environment does not. The filesystem-root qualifier is
v2.7.0 and is load-bearing in the other direction: without it any in-repo
directory named `users/` looked like a profile and truncated the chain, so a
session in `<repo>/users/alice/sub` could reach no rung at all and planted a
stray four levels down — the defect produced by the guard against it.

**Every surface anchors, not just the hooks (v2.8.0).** v2.7.0 claimed this
and delivered it for `cli/mem.py` alone; the audit that followed found seven
more surfaces that turn a supplied string into a database path, none of them
anchoring. They now share one implementation, `anchor_project`
(`core/roots.py:730-783`):

| Surface | Can it CREATE? | Announces via |
|---|---|---|
| six hooks <!--ce:hooks--> | yes (`user_prompt`, `pre_compact`) | `core.logger`, rare hooks only |
| `cli/mem.py` | no — reports "no memory database at X" | `print` |
| `cli/plan.py` | writing subcommands only (v2.8.0) | `print` |
| `mcp/server.py` | yes, the one model-facing write path | `_log` — **never** `print` |
| `ui/dashboard.py` | yes, via `_load_project` | UI dialog |
| `ui/installer.py` | yes, *Initialize Project* | install log |
| `ui/web_viewer.py` | no, read-only | `print` |
| `skills/ccm-load`, `skills/save-memories` | yes | `print` |

Two details that are easy to get wrong and were:

- **`announce` is a parameter, not a `print`.** The MCP server speaks JSON-RPC
  on stdout, so a redirection notice written there corrupts the framing and
  kills the session; it passes `_log.info` instead. Every human-facing surface
  passes a printer, because an explicit `--project` is an instruction and
  quietly substituting something else would be worse than the bug.
- **The opt-out is checked BEFORE anchoring, everywhere.** Anchoring is the
  step that may move *upward*; running it first would resolve an excluded
  subdirectory to its unexcluded parent and serve a project the user opted
  out of. `test_surfaces` §7 asserts this ordering at source level for all six
  hooks <!--ce:hooks-->.

`anchor_project` compares **both sides resolved**. `project_root` returns the
caller's own unresolved spelling whenever the answer is the input itself
(that is what keeps symlinked project directories working), so a one-sided
comparison could never match a relative path: `--project .` — exactly what the
`/cc-mem` wrapper passes — announced `. is inside a project rooted at .` on
every single call.

`cli/plan.py` additionally stopped conjuring databases from read-only
commands. `MemoryDB.__init__` mkdirs and creates, so `list` and `status` used
to fabricate a 140 KB empty database — *without* `.ccm/.gitignore`, the one
omission that let a stray ride into version control — merely for asking what
was in the queue. `cli/mem.py` had always refused instead; two halves of one
CLI pair must not disagree about that.

A pre-existing stray is therefore left exactly where it is — and *reported*,
so it is not invisible: `nested_databases` (`core/roots.py:797-862`) backs a
`[WARN] Separate database below this project` line in `cc-mem status`, which
names each one and its memory count. That is an explicit command rather than a
hook, because it walks the tree. `.ccm-root` — an empty file — pins a
directory as a root in its own right, which is the escape hatch for a project
deliberately nested inside another and for any layout these heuristics read
wrong. `tests/test_surfaces.py` §7 pins all of it: the ladder over a real
filesystem, all six hooks <!--ce:hooks--> run from a subdirectory, and the source-level rule
that every hook resolves *after* the opt-out.

### .gitignore migrates, not just creates (v2.4.2)

`core.progress.MEMORY_GITIGNORE_LINES` (`progress.py:42-56`) is the canonical
ignore set, and `ensure_memory_gitignore` (`progress.py:85-122`) **appends only
the missing lines**, preserving anything the user added. Every previous
generator was guarded by `if not gi.exists()`, so each time the plugin started
writing a new artifact, existing installs kept the stale ignore list forever and
silently began leaking it. Several of these artifacts embed verbatim
conversation or plan prose, which makes that a privacy problem rather than
noise. `pre_compact.py:353` runs it on EVERY compaction (not only at project
creation) precisely so old installs migrate. Two standalone copies of the list
exist because they cannot import this module and must be kept in sync:
`cc_memory/ui/installer.py` (stdlib-only bootstrap) and
`skills/ccm-load/SKILL.md` (inline script).

Old v2.0 `SESSION_HANDOFF.md` files are renamed to `SESSION_HANDOFF.md.v2.bak`
on first PreCompact under v2.1 (one-shot migration
`core.progress.migrate_legacy_handoff`, `progress.py:628-646`).

---

## 8. Install layouts

Three layouts are recognised by `cli/mem.py` `_detect_install_layouts`
(`cc_memory/cli/mem.py:487-565`). A machine can have more than one at once
(e.g. a dev checkout plus a stale marketplace-cache entry), so `/cc-mem status`
reports on each:

- **marketplace-directory** — `extraKnownMarketplaces["cc-memory"].source.path`
  points at a checkout (`mem.py:134-142`). `hooks/hooks.json`'s
  `${CLAUDE_PLUGIN_ROOT}` then resolves to the working tree itself, so editing
  `cc_memory/**.py` updates the live hooks with no copy step. This is the dev
  layout this repo uses, and it is why `CLAUDE.md` § "Sync protocol" says no
  copy into `~/.claude/hooks/` is needed for code changes.
- **marketplace-cache** — `installPath` from
  `~/.claude/plugins/installed_plugins.json` (`mem.py:144-174`). A recorded
  `installPath` that no longer exists is reported as a broken layout rather
  than skipped (`mem.py:158-170`).
- **legacy / standalone install** — `~/.claude/hooks/cc-memory/`
  (`mem.py:48`), written by the PyInstaller installer
  (`ui/installer.py:72` `TARGET_DIR`). Hooks here are registered directly in
  `~/.claude/settings.json` by `_merge_into_settings` (`installer.py:1116-1150+`),
  not via a plugin manifest.

Under the marketplace layouts `~/.claude/hooks/cc-memory/` holds only `logs/`
(the `core.logger` output target). `logs/` is present under every layout,
because the logger path is absolute and independent of where code lives.

### Nested vs flat: the standalone installer writes a FLAT tree

This distinction matters for anyone probing the filesystem, because the two
shapes do not share a `cc_memory/` path segment.

**Marketplace / dev checkout (NESTED)** — the package sits under a
`cc_memory/` directory inside the plugin root, and hook commands are
`${CLAUDE_PLUGIN_ROOT}/cc_memory/hooks/<name>.py` (`hooks/hooks.json:9, 14, 27,
39, 51, 63`):

```
<plugin root>/                       ← e.g. D:\Projects\cc-memory
├── hooks/hooks.json
└── cc_memory/
    ├── __init__.py, config.json
    ├── core/  hooks/  llm/  cli/  mcp/  ui/
```

**Standalone installer (FLAT)** — `_copy_subpackages(TARGET_DIR)`
(`installer.py:77-89`) writes each `SUBPACKAGE_FILES` key (`installer.py:77-89`)
directly under `TARGET_DIR` (`installer.py:72`), with **no `cc_memory/`
segment**, and `_make_hooks_config` (`installer.py:735-757`) builds commands as
`python "<TARGET_DIR>/hooks/<name>.py"`:

```
~/.claude/hooks/cc-memory/           ← ui/installer.py:72 TARGET_DIR
├── __init__.py
├── config.json
├── installed_surfaces.json  ← what was written into ~/.claude (v2.5)
├── core/    atomic.py auth.py consolidate.py db.py encoding_setup.py
│            extractor.py idle.py layout.py logger.py markers.py modes.py
│            plan.py privacy.py progress.py roots.py textsim.py version.py
├── hooks/   _entry.py consolidate_async.py post_tool_use.py pre_compact.py
│            session_start.py stop.py user_prompt.py
├── llm/     ccl_backend.py memory_writer.py parse.py
├── cli/     mem.py plan.py
├── mcp/     server.py
├── ui/      dashboard.py installer.py web_viewer.py
└── logs/    ← core.logger output target
```

Note what is *absent* from the flat tree: there is no `hooks/hooks.json`
(`SUBPACKAGE_FILES` does not include it — registration goes into
`~/.claude/settings.json` instead), no `.claude-plugin/`, no `docs/`, `tests/`,
or `tools/`. `tools/i18n_check.py` in particular is deliberately excluded from
`SUBPACKAGE_FILES` and `build_exe.py`; the packaged plugin is unchanged by it
(see [§9.5](#95-the-checker)). Two consequences follow: the flat layout can
never read `plugin.json`, so its **MCP registration must be written by hand**
against `<TARGET_DIR>/mcp/server.py`; and `core/version.py` had to be added to
`SUBPACKAGE_FILES["core"]`, or every module doing `from core.version import
__version__` would degrade on a flat install.

### The five surfaces are installed separately (v2.5)

`skills/`, `agents/` and `commands/` are **not** package files, and through
v2.4.3 the standalone installer did not ship them at all: `~/.claude` after an
install contained `hooks/` and `settings.json` and nothing else — no `/cc-mem`
command, no `plan-refiner` / `plan-guardian` agents, no skills. Everything the
user actually interacts with was missing.

`SURFACE_FILES` (`installer.py:95-101`) names exactly five paths —
`commands/cc-mem.md`, `agents/plan-refiner.md`, `agents/plan-guardian.md`,
`skills/ccm-load/SKILL.md`, `skills/save-memories/SKILL.md` — and `_copy_surfaces`
(`installer.py:467-501`) writes them into `~/.claude/` at install step [2/3],
recording what it wrote in `installed_surfaces.json` (`installer.py:58`).

Uninstall is **by name**, never `rmtree`: `~/.claude/{commands,agents,skills}`
hold the user's own files. `_remove_surfaces` (`installer.py:504-538`) deletes only
the recorded paths, removes an emptied `skills/<name>/` but never `commands/` or
`agents/` themselves, and distinguishes "no manifest" (fall back to this build's
`SURFACE_FILES`) from "a manifest recording nothing" (delete nothing, and say
so). A round trip with a user's own `commands/my-own.md` and `agents/my-agent.md`
seeded leaves exactly those two files behind.

### settings.json is validated before anything is copied (v2.5)

`_read_settings` (`installer.py:729-761`) returns `(dict, None)` or `(None, error)`
and never raises; `cli_install` calls it at step **[0/3]** and returns 1 with
`Nothing has been installed.` on a parse failure. Through v2.4.3 the parse
happened *after* the copy, so a `settings.json` the installer could not read
left 32 files on disk with **zero hooks registered** — and the uninstaller died
the same way, halfway through. Across 19 settings shapes × {pre-check, install,
install ×2, uninstall, uninstall ×2}: **18 crashes → 0**.

Judgement calls worth knowing: an empty/whitespace-only file is treated as `{}`
(there is no user data to lose, and refusing would strand a fresh machine); a
UTF-8 BOM is tolerated, because PowerShell's `>` writes one; the three shapes
that genuinely encode intent we cannot parse (JSONC comments, a trailing comma,
a top-level array) refuse with rc=1 and copy nothing; a malformed hook group is
**preserved verbatim** rather than shredded; and a hook command that merely
mentions "cc-memory" without running one of this build's six hook scripts <!--ce:hooks--> is
**kept and warned about** rather than deleted.

### The frozen exe spawns scripts through a real interpreter, and a settings write follows the link (v2.14.0)

In a PyInstaller onefile build `sys.executable` IS the installer binary, so
`[sys.executable, dashboard.py, …]` re-entered `main()` with the dashboard
path in argv: `_KNOWN_FLAGS` refused it and exited 2 into a console that
closes instantly, and before the v2.5.3 refusal the same click performed a
silent re-install. `_python_for_script` (`installer.py`) hands a `.py` to the
interpreter the hook commands already resolve (`_detect_python_cmd`),
absolute when PATH can supply it; `tests/test_surfaces.py` §3 asserts at
source level that nothing else reads `sys.executable`.

`~/.claude/settings.json` is a SYMLINK on a dotfiles-managed home (stow,
chezmoi), and `Path.replace` renames over the link: the versioned copy kept
its old content with no hooks, the path became an unversioned regular file,
and the next sync restored the link and un-registered cc-memory.
`_settings_write_target` resolves the link and writes the target, with the
temp file beside the target (a rename cannot cross filesystems); both halves
of the compare-and-swap still read `SETTINGS_PATH`.

### Layout detection and inspection agree (fixed in v2.5)

Detection accepts both shapes: `mem.py:522` tests
`(legacy / "cc_memory").exists() or (legacy / "core" / "db.py").exists()`.
Inspection used to disagree with it. `_inspect_layout` (`mem.py:493-562`) resolved
every `cc_memory/…`-prefixed entry of `_REQUIRED_PLUGIN_FILES` (`mem.py:304-363`)
against the layout **root**, so a healthy flat install reported all 22 files
missing, printed `[FAIL]`, and — because `/cc-mem status` only runs the API-key
check against a "fully-functional" layout — skipped that check entirely.

It now resolves `pkg_dir` once (`mem.py:539`:
`root/"cc_memory"` if that directory exists, else `root`), strips the prefix
accordingly, and requires `hooks/hooks.json` only for plugin-manifest installs —
the standalone installer never copies it, and it is meaningless when the hooks
come from `settings.json`. The report prints `(flat)` / `(nested)` so the shape
is visible, and `cmd_status` puts the returned `pkg_dir` on `sys.path` instead
of a hardcoded `root/"cc_memory"`.

The installer's own post-install instructions printed
`TARGET_DIR/cc_memory/cli/mem.py`, a path it never creates; they now print
`TARGET_DIR/cli/mem.py` (`installer.py:72`), which exists.

### Interpreter requirement

`hooks/hooks.json` invokes `python3`. On Linux/macOS this is the standard
Python 3 binary. On Windows the python.org installer ships `python.exe` plus
the `py.exe` launcher but NOT `python3.exe` by default — install "Add Python to
PATH" + tick "py launcher", or alias `python3 -> python`, before installing the
plugin. Otherwise hooks fail silently (logged to
`~/.claude/hooks/cc-memory/logs/`, but Claude Code shows no error UI for a
missing command).

The standalone installer sidesteps this by **running** each candidate rather
than probing for its existence: `_detect_python_cmd` (`installer.py:646-666`)
executes `<cand> -c "import sys;print(sys.version_info[0])"` with a 15 s timeout
and takes the first that answers `3`. `shutil.which("python3")` was not enough —
on Windows it resolves to a 0-byte App Execution Alias when Store Python is not
installed, and the resulting hook command fails silently. (The residual caveat:
on such a machine, *executing* that alias can pop the Microsoft Store; the
timeout bounds it.)

---

## 9. Documentation language convention (i18n)

This chapter defines how cc-memory keeps human-facing documentation in more than
one language **without silent drift**. English is the canonical skeleton; other
languages are drift-tracked siblings tied to a hash of their English source.

It is the English source-of-truth for the convention. The checker that enforces it
is `tools/i18n_check.py` (pure stdlib, dev/CI only — not shipped in the plugin).

> Merge note: this chapter was `docs/I18N.md` through v2.4.2, and was merged
> here in v2.4.3. Every in-code pointer was retargeted in the same change — the
> Tier-3 guard comments in `core/extractor.py` (`:32`, `:71`),
> `hooks/session_start.py` and `hooks/user_prompt.py`, plus the module docstring
> of `cc_memory/__init__.py`, all cite
> `docs/ARCHITECTURE.md#9-documentation-language-convention-i18n §1`, and
> `grep -rn I18N cc_memory/` returns nothing. (The three hook/package line
> numbers this note used to carry moved with unrelated edits; `grep -rn "i18n
> Tier 3" cc_memory/` is the durable locator.)

### 9.1 The three-tier language model

The whole system rests on separating three different things people mean by
"language", each with its own rule:

| Tier | What | Rule | Where it lives |
|------|------|------|----------------|
| 1 — Skeleton | English canonical docs + all LLM-facing strings | English is authoritative; every translation needs an English source | `README.md`, `docs/*.md`; hook / CLI instruction strings |
| 2 — Translation | Human-read docs in another language | `NAME.<lang>.md` sibling, drift-tracked, produced on demand | `README.zh.md`, `docs/ARCHITECTURE.zh.md`, `docs/CONTRACTS.zh.md` — since v2.5 all three tracked English docs have one |
| 3 — Content | Memory content the user stores | Any language; bilingual detection is intentional | `extractor.py`, `user_prompt.py`, `session_start.py` |

- **Tier 1 stays English on purpose.** Hook stdout and the `Claude:` CLI
  instruction prints are read by the model, not the end user — they are tuned for
  instruction-following, so translating them would degrade behavior. "English
  canonical" means these are never translated.
- **Tier 3 is language-agnostic by design.** The extractor patterns and
  resume-signal sets match both English and Chinese, and stored memories may be in
  any language. See
  [§1 "Bilingual by design"](#bilingual-by-design--memory-content-is-language-agnostic).
  Do **not** reduce those detectors to English-only — that would break clause 3 of
  the design ("内容可以是任意语言"). The concrete guarded sites are
  `core/extractor.py:35-69` (`_PATTERNS`), `core/extractor.py:35-69`
  (`_IMPORTANCE_BOOST`), the RESUME PROTOCOL token lines in
  `hooks/session_start.py` and `resume_signals` in `hooks/user_prompt.py`; the
  last two must stay in sync with each other, since the forced reminder promises
  the behavior that `user_prompt` types as `resume_request`. All four carry an
  `i18n Tier 3` comment — `grep -rn "i18n Tier 3" cc_memory/` locates them
  without depending on line numbers that move.

Only **Tier 2** — the human-facing docs — is what this convention version-controls.

### 9.2 File naming

- `NAME.md` — the canonical **English** source (the skeleton).
- `NAME.<lang>.md` — a translation sibling. Today `<lang>` is `zh` (Simplified
  Chinese); the translations that exist are `README.zh.md`,
  `docs/ARCHITECTURE.zh.md` and `docs/CONTRACTS.zh.md`.
- Every translation MUST have a matching English source. A `NAME.zh.md` with no
  `NAME.md` is an **ORPHAN** (checker fails). There are no translation-only docs.

Tracked set (what the checker looks at): `README.md` at the repo root plus
`docs/*.md`, excluding `*.zh.md` (`tools/i18n_check.py:146-157`). Translations
are `README.zh.md` and `docs/*.zh.md`, non-recursive
(`tools/i18n_check.py:160-166`). After the v2.4.3 doc consolidation the tracked
English set is exactly three files — `README.md`, `docs/ARCHITECTURE.md`,
`docs/CONTRACTS.md` — not the five that existed before the merge. Since v2.5
**all three have a translation**, so a healthy run reports `3 in-sync` and there
is no MISSING-TRANSLATION left: any edit to an English doc that is not followed
by steps 2-4 of [§9.7](#97-updating-after-the-english-source-changes) turns both
this checker and `tests/smoke_test.py` red.

### 9.3 The language switcher

Each doc carries a one-line language switcher as a **blockquote above the H1**. A
blockquote is its own markdown block, so it never collides with the `#` heading, and
it renders as an ordinary link line:

- English doc, line 1 (after the marker on translations — see §9.4):
  `> **English** · [简体中文](NAME.zh.md)`
- Translation, line 2 (line 1 is the marker):
  `> [English](NAME.md) · **简体中文**`

The current language is shown in **bold**; the others are links.

A switcher must only be added once its target will exist in the same release.
`docs/I18N.md` shipped a switcher pointing at `docs/I18N.zh.md`, which was never
written — a dead link on GitHub, and one CI cannot catch by design, because
MISSING-TRANSLATION is not in `FAIL_STATES` (`tools/i18n_check.py:85`). Adding
the switcher is step 2 of the 5-step sequence in §9.6; steps 3-5 must follow in
the same change.

**This file carries a switcher** (line 1) because `docs/ARCHITECTURE.zh.md` was
authored in the same release (v2.4.3) that added it — steps 2-5 of §9.6 were
completed together, which is exactly what the `I18N.md` precedent above says to
do. `docs/CONTRACTS.md` gained its switcher in v2.5, in the same change that
added `docs/CONTRACTS.zh.md`; it correctly had none before that, because a
switcher without its target is the dead link this convention exists to prevent.
All three tracked English docs now carry one, and all three have a translation.

### 9.4 The drift marker

Line 1 of every translation is a machine-readable marker — an HTML comment, so it is
invisible when rendered and inert to Claude Code's plugin/skill/agent loader (it is
**never** YAML front-matter, which the loader owns):

```
<!-- i18n-source: README.md | sha256: dc06fb064d615ae5 | version: 2.3.2 | translated: 2026-07-11 -->
```

That line illustrates the marker **format**; it is not a claim about any current
file's digest. Real markers are generated with `--emit-marker` (§9.6, §9.7).
Grammar: `tools/i18n_check.py:50-60` (`MARKER_FMT` / `MARKER_RE`, which requires
exactly 16 lowercase hex digits and an ISO date).

Fields:

| Field | Meaning | Used for drift? |
|-------|---------|-----------------|
| `i18n-source` | sibling filename of the English source | no (locates the source) |
| `sha256` | 16-hex-char sha256 prefix of the **normalized** English source | **yes — the only drift signal** |
| `version` | `cc_memory.__version__` at translation time | no (informational) |
| `translated` | `YYYY-MM-DD` the translation was refreshed | no (informational) |
| `translation` | 16-hex-char sha256 prefix of the **translation's own body** (every line but the marker), optional since v2.14.0 | no — read by `--emit-marker` only (§9.7) |

Drift is decided **solely** by the `sha256`. `version` and `translated` are
informational, so a future version bump never mass-flags every translation as stale —
only an actual change to the English *content* does. `translation` is not a
drift signal either: it is what lets `--emit-marker` refuse to certify a
translation nobody translated (§9.7).

Marker parsing is **fail-closed** (`tools/i18n_check.py:107-124`): it is
BOM-tolerant, but any read/decode error, or a first line that does not match the
grammar, yields `None` and the caller reports NO-MARKER (a FAIL state) rather
than silently treating the translation as valid.

#### Hash normalization (the cross-platform-critical part)

The digest is taken over a **normalized** form of the English source, and the exact
same normalizer runs at emit time and at check time. This is what makes the hash
stable across Windows/Unix: CRLF vs LF, a UTF-8 BOM, or trailing-whitespace churn
cannot move the digest.

Recipe (`normalize_markdown` in `tools/i18n_check.py:102-114`):

```
strip UTF-8 BOM  →  decode utf-8  →  CRLF/CR → LF  →  rstrip each line
                 →  exactly one trailing "\n"  →  sha256(...).hexdigest()[:16]
```

The marker hashes the **whole English file, including its switcher line**. So when
you add or change the switcher, the digest changes too — which is why the switcher
must be finalized *before* you emit the marker (see §9.6).

### 9.5 The checker

`tools/i18n_check.py` is pure stdlib and lives outside the `cc_memory` package on
purpose — it is a dev/CI tool and is deliberately absent from `ui/installer.py`
`SUBPACKAGE_FILES` (`installer.py:77-89`), `build_exe.py`, and `cli/mem.py`
`_REQUIRED_PLUGIN_FILES` (`mem.py:304-363`), so the packaged plugin is unchanged
by it.

```bash
python tools/i18n_check.py            # check every tracked doc
python tools/i18n_check.py --list     # every English/translation pair + recorded vs current hash
python tools/i18n_check.py --verbose  # show a detail line for every doc, not just failures
python tools/i18n_check.py --emit-marker README.md          # print a fresh marker line
python tools/i18n_check.py --root /path/to/repo             # override the repo root
python tools/i18n_check.py --emit-marker README.md --version-label 2.4.2  # override the marker's version field
python tools/i18n_check.py --emit-marker README.md --date 2026-08-04      # override the marker's translated: date
```

`--root` defaults to the repo containing the script, not the CWD
(`tools/i18n_check.py:305-307`), so the checker gives the same answer from any
directory.

States, labels, and exit codes:

| State | Label | Meaning | Exit |
|-------|-------|---------|------|
| IN-SYNC | `[OK]` | translation's marker hash == current English hash | 0 |
| MISSING-TRANSLATION | `[WARN]` | English doc has no `.zh.md` sibling yet | 0 |
| STALE | `[STALE]` | marker hash != current English hash (source changed) | nonzero |
| ORPHAN | `[FAIL]` | translation whose English source is gone/renamed | nonzero |
| NO-MARKER | `[FAIL]` | translation whose first line has no valid marker | nonzero |

The checker exits nonzero if **any** STALE / ORPHAN / NO-MARKER is present
(`FAIL_STATES`, `tools/i18n_check.py:85`; `main` returns `1` on failure,
`:351-353`). MISSING-TRANSLATION is a soft warning — a translation simply hasn't
been produced yet — and never fails the build. `tests/smoke_test.py:878-895`
imports the checker, asserts no STALE/ORPHAN/NO-MARKER across tracked docs, and
separately asserts that `README.zh.md`'s marker digest equals the live
`hash_source(README.md)`, so a stale translation turns the smoke test red.
`--emit-marker` is a separate mode: it prints one marker line and exits 0, or
exits **2** if the named English source does not exist (`tools/i18n_check.py:339-341`).

### 9.6 Adding a translation

Order matters: the marker hashes the whole English file, so the English switcher must
be in place *before* the marker is emitted.

1. Finalize the English doc's **content**.
2. Prepend the English switcher to the English doc, line 1:
   `> **English** · [简体中文](NAME.zh.md)`.
3. Emit the marker: `python tools/i18n_check.py --emit-marker docs/NAME.md` and copy
   the printed line.
4. Create `docs/NAME.zh.md`:
   - line 1 = the emitted marker,
   - line 2 = the translation switcher `> [English](NAME.md) · **简体中文**`,
   - blank line, then the full translation.
5. Verify: `python tools/i18n_check.py` shows `[OK] IN-SYNC` for the pair and exits 0.

Do not stop after step 2. A switcher without steps 3-5 is a dead link that the
checker will not flag (§9.3).

### 9.7 Updating after the English source changes

When you edit an English doc that already has a translation, the checker will report
`[STALE]` until you refresh the translation:

1. Edit the English doc.
2. Update the translation's body to match the new English content.
3. Re-emit the marker: `python tools/i18n_check.py --emit-marker docs/NAME.md`.
4. Replace **line 1** of `docs/NAME.zh.md` with the newly emitted marker.
5. Verify: `python tools/i18n_check.py` → `[OK]`, exit 0.

Step 2 is the one the digest can never see, so since v2.14.0 step 3 checks
it: `--emit-marker` records the translation body's hash in the marker
(`translation:`), and when the English digest has changed since the previous
marker but the translation's body has not, it **refuses** (exit 2) instead
of printing a marker that would read `[OK]`. Measured before the check
existed: README.md edited to claim macOS, marker re-emitted and pasted,
README.zh.md untouched, checker `IN-SYNC`. An English-only change that needs
no translation (a typo, a citation renumbered) passes with
`--translation-unchanged "<why>"`; a marker stamped before the field existed
is accepted once and gains the field.

If instead you delete or rename an English doc, delete or rename its translation too,
or the checker will report `[FAIL] ORPHAN`.

### 9.8 Scope and exclusions

**In scope (translated on demand):** `README.md` and `docs/*.md`.

**Explicitly excluded from translation:**

- `CLAUDE.md`, `commands/`, `skills/`, `agents/` — Claude-facing, and their YAML
  front-matter is owned by the loader; adding unknown keys risks loader rejection.
- `CHANGELOG.md` — append-only release churn; not a document you read top-to-bottom.
- `.ccm/**` — generated artifacts.
- Runtime UI strings (CLI / dashboard) — LLM-facing (Tier 1) and with no central
  output seam; deliberately deferred, not part of this convention.

### 9.9 Verification checklist

- `python tools/i18n_check.py` → tracked English docs are `[OK]` or `[WARN]`, exit 0.
- `python tools/i18n_check.py --list` → each pair's recorded hash `==` current hash.
- `python tests/smoke_test.py` → prints the `[OK] i18n: ...` line and ends with
  `===== ALL SMOKE TESTS PASSED =====`.
- Cross-platform hash stability: `normalize_markdown` yields the same digest under LF
  and CRLF (round-trip line endings and re-run `--list`).
- Negative test: make a **real content change** to an English doc that has a
  translation — e.g. insert a word mid-line — → checker reports `[STALE]` and the
  smoke test fails; refresh the marker → green. The pre-merge wording of this
  bullet ("append a space to any line") was wrong and is corrected here:
  `normalize_markdown` rstrips every line (`tools/i18n_check.py:102-114`), so trailing
  whitespace is normalized away and cannot move the digest.
- Loader safety: the first bytes of every doc are a blockquote (`>`) or HTML comment
  (`<!--`) or `#` — never `---`. No Claude-facing file is modified.

---

## See also

- [docs/CONTRACTS.md](CONTRACTS.md) — the three hard contracts: anti-patch
  writes, forced handoff, live plan anchor
- [CHANGELOG.md](../CHANGELOG.md) — version history
- [CLAUDE.md](../CLAUDE.md) — project instructions for Claude Code
- `tests/smoke_test.py` — the canonical end-to-end check; run it after any
  change to `memory_writer`, `progress`, `plan`, or
  `session_start._refresh_progress_row`
