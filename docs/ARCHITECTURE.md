> **English** · [简体中文](ARCHITECTURE.zh.md)

# cc-memory — Architecture (v2.4.3)

cc-memory is a Claude Code plugin that gives Claude **persistent, structured
memory across compactions and sessions**. This document is the overview: what
the plugin is for, how the repository is laid out, which hooks fire when, what
the database holds, how data moves, how LLM calls are authenticated, what the
plugin writes into a project, where installed code physically lives, and the
convention that keeps the documentation translated without silent drift.

The three hard contracts — anti-patch writes, forced handoff, and the live plan
anchor — are specified in [docs/CONTRACTS.md](CONTRACTS.md). This file
describes the machinery; that file describes the rules the machinery must obey.

## Contents

- [1. Overview / what it solves](#1-overview--what-it-solves)
- [2. Repository layout](#2-repository-layout)
- [3. Hooks](#3-hooks)
- [4. Database schema](#4-database-schema)
- [5. Data flow](#5-data-flow)
- [6. LLM backends and auth](#6-llm-backends-and-auth)
- [7. Per-project state (memory/)](#7-per-project-state-memory)
- [8. Install layouts](#8-install-layouts)
- [9. Documentation language convention (i18n)](#9-documentation-language-convention-i18n)

---

## 1. Overview / what it solves

Three design constraints drive everything else:

1. **Anti-patch writes.** Every memory save goes through one entry
   (`llm.memory_writer.upsert_smart`) which either **merges** the new content
   into an existing similar memory, **supersedes** an older version (preserving
   a chain), or **inserts** as a new fact — chosen by similarity, not by the
   caller. There is no "append + dedup later" path.

2. **Forced handoff.** At every `SessionStart`, the plugin emits a
   `<system-reminder>` block instructing the next Claude to `Read
   memory/PROGRESS.md` before responding. PROGRESS.md is a single SOT,
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
(`core/extractor.py` `_PATTERNS` at `extractor.py:35-69` / `_IMPORTANCE_BOOST`
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
│   ├── plugin.json              ← Plugin manifest (v2.4.3)
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
│   └── CONTRACTS.md             ← anti-patch + forced handoff + live plan
├── cc_memory/                   ← Python package (subpackaged)
│   ├── __init__.py              (__version__ = "2.4.3", cc_memory/__init__.py:64)
│   ├── config.json
│   ├── core/                    ← Domain: db, extractor, consolidate, idle,
│   │                              progress, plan, privacy, modes, auth,
│   │                              logger, encoding_setup
│   ├── hooks/                   ← Hook entry points (6 modules)
│   ├── llm/                     ← ccl_backend (Haiku/Ollama) + memory_writer
│   ├── cli/                     ← mem.py, plan.py
│   ├── mcp/                     ← server.py (MCP stdio)
│   └── ui/                      ← installer, dashboard, web_viewer
├── tests/                       ← smoke_test.py (canonical end-to-end) +
│                                  test_plan_carryover.py
├── tools/i18n_check.py          ← doc-translation drift checker (dev/CI only)
├── build_exe.py                 ← PyInstaller build
├── pyproject.toml
├── README.md
├── README.zh.md                 ← drift-tracked translation (see §9)
├── CLAUDE.md                    ← Project instructions for Claude Code
├── CHANGELOG.md
└── LICENSE
```

`agents/`, `tests/`, and `tools/` are load-bearing, not incidental:
`agents/plan-refiner.md` is nudged from `cc_memory/hooks/stop.py:279-284` and
`agents/plan-guardian.md` from `stop.py:286-296`; `tools/i18n_check.py` is what
[§9](#9-documentation-language-convention-i18n) specifies and what
`tests/smoke_test.py:878-895` imports as a drift gate.

The version string is declared in six canonical places and must stay in
lockstep: `.claude-plugin/plugin.json:3`, `pyproject.toml:3`,
`cc_memory/__init__.py:64`, `cc_memory/config.json:2`, plus the CLI banners
(`cli/mem.py:276, 984`) and the MCP-server banners (`mcp/server.py:274, 316`).
(They had drifted to three different values — 2.4.1 / 2.3.4 / 2.3.3 — before
v2.4.2; see `CHANGELOG.md` under 2.4.2 → Changed.)

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

**6 hook commands across 5 Claude Code events** are declared in
`hooks/hooks.json` — `PreCompact` declares two, a blocking sync leg
(`hooks.json:9`, 120s) and a background `async` leg (`hooks.json:14`, 300s,
`"async": true`):

| Hook | Entry | Timeout | Job |
|------|-------|---------|-----|
| `PreCompact` (sync) | [`cc_memory/hooks/pre_compact.py`](../cc_memory/hooks/pre_compact.py) | 120s | Read a BOUNDED head+tail transcript window (`extractor.load_transcript_window`); LLM extract memories via Haiku; route through `memory_writer.upsert_batch`; FULL-REWRITE `memory/PROGRESS.md`; archive session. Writes a start marker so a killed run is detectable. |
| `PreCompact` (async) | [`cc_memory/hooks/consolidate_async.py`](../cc_memory/hooks/consolidate_async.py) | 300s, `async: true` | Every-Nth-session LLM consolidation, moved OFF the blocking compaction path in v2.3.2 (interval marker + lock, budget-gated). |
| `SessionStart` | [`cc_memory/hooks/session_start.py`](../cc_memory/hooks/session_start.py) | 15s | Inject layered context (topics / critical / timeline / PROGRESS preview / footer); emit the FORCED `<system-reminder>` to Read `PROGRESS.md` + `MEMORY.md`; retroactive save of unsaved JSONLs. |
| `Stop` | [`cc_memory/hooks/stop.py`](../cc_memory/hooks/stop.py) | 22s | Observer: extract from last turn's observations via Haiku; per-turn `patch_progress(files_touched, ...)`; every 5 turns run `idle.maybe_run_idle` (cleanup + MEMORY.md regen); when a plan is active, bump its turn counter and emit ONE advisory line — the plan-refiner nudge if the plan is still unrefined, else a guardian-check nudge once drift thresholds trip. |
| `PostToolUse` | [`cc_memory/hooks/post_tool_use.py`](../cc_memory/hooks/post_tool_use.py) | 8s | Insert one row into `observations` per OBSERVED tool call (mode allowlist / skip list — `core.modes.should_observe`); plus live-plan capture: `ExitPlanMode` → `plan_active.raw`, `TodoWrite` → mechanical step sync, `Edit`/`Write`/`MultiEdit`/`NotebookEdit` → +1 drift counter, sensitive Bash call → +20. No LLM. |
| `UserPromptSubmit` | [`cc_memory/hooks/user_prompt.py`](../cc_memory/hooks/user_prompt.py) | 8s | Auto-init `memory/` on first contact; track turn count; save prompt for the Stop observer; on turn 1, tag the session and seed `progress.current_request` (typing the trigger `resume_request` vs `user_prompt` from the bilingual resume-signal whitelist). |

### Hook stdout contract

Each hook's stdout has a specific role, and violating it is a user-visible bug:

- `SessionStart` stdout → injected context (read by Claude).
- `Stop` stdout → status line(s): one `[cc-memory] …` line every turn
  (`stop.py:260-265`), plus at most one `[cc-memory.plan] …` advisory line.
- `PreCompact` (sync) stdout → ONE status line (shows in the next session's
  compacted context).
- `PreCompact` (async) / `PostToolUse` / `UserPromptSubmit` stdout → empty. The
  async leg's stdout is not shown inline at all (`consolidate_async.py:31`).

### PreCompact: why two legs

v2.3.1 raised the sync timeout 45→120s, but the every-Nth-session LLM
consolidation could still overrun on large DBs, so
`Compacted PreCompact … failed: Hook cancelled` still surfaced. v2.3.2 split
the event:

- The **sync leg** keeps only fast, handoff-critical work (extraction +
  PROGRESS.md, ~1-5s; `pre_compact.py:5-20`).
- The **async leg** runs `core.consolidate.run_consolidation` under a
  `BudgetGate` with `_BUDGET_TOTAL_S = 240.0` and `_BUDGET_SAFETY_S = 8.0`
  (`consolidate_async.py:59-60`), so the last LLM call it starts finishes by
  `total_s - safety_s` = 232s < the hook's own 300s timeout — the worker is
  never killed mid-write.
- Cadence is an **interval marker + lock**, not a fragile
  `session_count % N` check: `memory/.last_consolidation.json` records the
  session count at the last successful run and
  `memory/.consolidation.lock` prevents overlapping workers (a lock older than
  `_STALE_LOCK_S = 360.0`, `consolidate_async.py:64`, is reclaimed). This is
  race-immune against the concurrent sync leg — a ±1 drift in the count can
  cause neither a double-run nor a miss (`consolidate_async.py:19-28`).

### Timeouts are declared twice and must stay in lockstep

`hooks/hooks.json` is the marketplace/dev declaration.
`cc_memory/ui/installer.py` `HOOK_SCRIPTS` (`installer.py:55-61`) is the
standalone-install declaration, expressed as a **base** timeout multiplied by
1.5 on Windows (`installer.py:101`): PreCompact `80 × 1.5 = 120`, SessionStart
`10 × 1.5 = 15`, Stop 22, PostToolUse 8, UserPromptSubmit 8. The async leg is
appended separately at a **flat** 300s (`apply_mult=False`,
`installer.py:122-123`) because it is a background deadline, not a blocking-UI
budget.

### Known gap: the observation gate shadows the plan branches

`post_tool_use.py:86-87` returns early when
`core.modes.should_observe(mode, tool_name)` is false, and the live-plan
integration block sits **after** that gate at `post_tool_use.py:113-141`.
`should_observe` (`core/modes.py:65-71`) is a skip-list check followed by an
allowlist check against `observe_tools`, and in all three shipped modes
(`modes.py:9-56`):

- `TodoWrite` is in every mode's `skip_tools` (`modes.py:18, 30, 45`) →
  `should_observe` is False;
- `ExitPlanMode` is in no mode's `observe_tools` → `should_observe` is False.

Verified by running `core.modes.should_observe` directly for all three modes:
both tools return `False` in each. Consequently the `ExitPlanMode` capture
(`post_tool_use.py:117-122`) and the `TodoWrite` step sync
(`post_tool_use.py:124-129`) are unreachable through this hook as written;
today the live plan is fed by `cli/mem.py:772` (`/cc-mem plan-set`), and
`tests/smoke_test.py:419, 466` exercises `core.plan.capture_exit_plan_mode` /
`apply_todowrite_sync` directly rather than through the hook, so the suite does
not catch it. The edit counter (`post_tool_use.py:131-133`) *does* fire in
`code` and `writing` modes (where `Edit`/`Write`/`MultiEdit` are observed) but
not in `research` mode, where those tools are skipped (`modes.py:31`); the
sensitive-call bump (`post_tool_use.py:139-141`) requires `Bash`, observed in
`code` and `research` but skipped in `writing` (`modes.py:46`). This is a code
defect recorded here rather than a documentation error — it was not in the
audit's action list and is reported as a new finding.

Note also that `config.json`'s `observation.skip_tools` key
(`cc_memory/config.json:35-40`) is **dead**: no Python reads it (`skip_tools`
appears only in `core/modes.py:17, 29, 44, 67`). The live filter is
`core/modes.py`'s per-mode `skip_tools` + `observe_tools`.

---

## 4. Database schema

SQLite tables (defined in [`cc_memory/core/db.py`](../cc_memory/core/db.py)),
project-local at `<project>/memory/memory.db`, WAL mode:

| Table | Purpose |
|-------|---------|
| `projects` | One row per project path (`db.py:36`); carries `mode` since migration `v2_project_mode` (`db.py:158`) |
| `sessions` | One row per compaction event (`db.py:44`) |
| `memories` | Extracted facts (category, importance, topic, content_hash, **supersedes_id**, last_referenced_at) (`db.py:55`) |
| `topics` | Consolidated summaries per topic name (versioned) (`db.py:69`) |
| `keywords` | Auto-detected project vocabulary (`db.py:79`) |
| `plans` | Plan queue (draft → ready → done) (`db.py:88`) |
| `observations` | Raw PostToolUse events, cleaned up after extraction (`db.py:129`) |
| `session_summaries` | 6-field structured summary per session (request / investigated / learned / completed / next_steps / notes) + files_read/files_modified (`db.py:143`) |
| **`progress`** | NEW in v2.1 — single row per project. SOT for `memory/PROGRESS.md` (`db.py:177`). |
| **`plan_active`** | NEW in v2.2 — single row per project. SOT for `memory/PLAN.md` (`db.py:199`). |
| `_migrations` | Tracks applied migrations (`db.py:278`) |

Eleven tables, matching `CLAUDE.md` § "Database schema (11 tables)".

Plus `memories_fts` — an FTS5 virtual table over `memories`, kept in sync by three
triggers (`core/db.py:317-341`, migration `v2_fts5`). It is created only when the
local SQLite build has FTS5; otherwise `db.search_fts` falls back to `LIKE`
(`core/db.py:306-313`, `:1106`). FTS5 is advertised in
`.claude-plugin/plugin.json:4` and `:12`, and `/cc-mem status` reports which
path is live (`cli/mem.py:307-308`).

The `supersedes_id` column on `memories` (migration `v3_supersedes`,
`db.py:169`) makes the anti-patch chain explicit: when `upsert_smart` decides a
new memory supersedes an old one, the new row links back to the old row's ID
(and the old row is archived). Walking the chain via
`db.get_supersede_chain(memory_id)` (`db.py:513`) shows the full update
history. `content_hash` (migration `v2_content_hash`, `db.py:124`) is
`sha256[:16]` of the normalized content, used for the cheap exact-duplicate
check (`db.compute_content_hash` at `db.py:722`, `db.find_by_hash` at
`db.py:735`).

Migrations are applied in order from the `_MIGRATIONS` list (`db.py:119`) and
recorded in `_migrations`. Levels shipped so far: **v1** (topic column +
index), **v2** (content_hash, observations, session_summaries, project mode,
FTS5, hash backfill), **v3** (anti-patch + forced handoff: `supersedes_id`,
`progress`), **v4** (`plan_active`), **v5** (session annotation:
`progress.current_session_id`, `progress.session_started_at` — so a
multi-session workflow can tell from PROGRESS.md whether it is reading its own
write, `db.py:219-222`), **v6** (reference-aware aging:
`memories.last_referenced_at`, set on injection, so effective age is
`now - COALESCE(last_referenced_at, created_at)` and a referenced fact stays
"young", `db.py:226-237`).

The `progress` row's user-facing fields are `current_request`, `status_done`,
`status_in_flight`, `status_blocked`, `open_todos`, `plan`, `critical_context`,
`files_touched`, `transcript_ptr`, `updated_at`, `trigger_type` (11, plus the
two v5 session-annotation columns). The `plan_active` row holds `raw`,
`structured`, `active_step`, `edits_since_last_guardian`,
`turns_since_last_guardian`, `last_guardian_at`, `last_refined_at`,
`needs_refine`, `created_at`, `updated_at` (`db.py:199-211`).

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
  ├─ 1. compute_content_hash → find_by_hash → SKIP if exact match
  │
  ├─ 2. find the most similar ACTIVE memory (Jaccard on character trigrams).
  │      Scope: memories in the same topic when a topic is set AND that scan
  │      yields candidates; otherwise a category-scoped scan of the 50 most
  │      recently updated (memory_writer._find_similar, memory_writer.py:63-92)
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

- `upsert_batch` (`memory_writer.py:161-196`) loops `upsert_smart` per item and
  regenerates ONCE at the end, but only when a `memory_dir` is passed
  (`memory_writer.py:190-194`). All hook callers pass it
  (`pre_compact.py:435`, `stop.py:166`, `session_start.py:722`); the sync
  PreCompact leg additionally touches it again after the rest of its state
  changes (`pre_compact.py:509`).
- Single-shot callers call `regenerate_memory_index` explicitly:
  `cli/mem.py:524` and `:584`, `mcp/server.py:192`, `ui/dashboard.py:956`,
  `ui/web_viewer.py:325`, plus the `skills/ccm-load` inline script
  (`SKILL.md:127, 137`). `core/idle.py:94` and
  `hooks/consolidate_async.py:188` also refresh it after maintenance.

(The pre-merge diagram showed regeneration as an unconditional step of
`upsert_smart` and elided the `db` argument; both are corrected above against
`memory_writer.py:95, 190, 199`. The caller list is likewise the full set found
by grepping `upsert_smart|upsert_batch` across `cc_memory/`.)

Thresholds live in one place — `memory_writer.HIGH_SIM = 0.80`,
`MID_SIM = 0.50`, `MIN_CONTENT_LEN = 10`, `MAX_CANDIDATES_TO_SCAN = 50`
(`memory_writer.py:44-47`), mirrored informationally in `config.json`'s
`writer` block. See [docs/CONTRACTS.md](CONTRACTS.md#anti-patch-contract) for
the full contract.

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
  write_progress_md(db, project_id, memory_dir)   ← FULL REWRITE of memory/PROGRESS.md
    ↓
  .last_save.json (incl. trigger: auto|manual) + _clear_attempt(memory_dir)

Stop (every turn):
  db.tag_progress_session(project_id, session_id)   ← v5, before the patch
    ↓
  db.patch_progress(files_touched=..., trigger_type="stop")
    ↓
  write_progress_md(db, project_id, memory_dir)   ← FULL REWRITE again (idempotent)

UserPromptSubmit (turn 1 only):
  db.tag_progress_session(project_id, session_id)
    ↓
  db.patch_progress(current_request=<user msg>,
                    trigger_type="resume_request" | "user_prompt")
    ↓
  write_progress_md(db, project_id, memory_dir)

SessionStart:
  inject context blob (topics 30% + critical 15% + timeline 20% +
                       PROGRESS preview 25% + footer 10% of a ~16000-char
                       budget — session_start.py:48-56)
  footer may carry: killed-PreCompact warning (surviving .pre_compact_attempt.json,
                    after a 10-minute grace window), OAuth/api-key warnings, counts
  emit: <system-reminder>
          You MUST Read memory/PROGRESS.md and memory/MEMORY.md before
          responding to any user request. Explicitly state in your reply:
          "Read PROGRESS.md — prior progress: <summary>."
          … plus the RESUME PROTOCOL (bilingual token whitelist → auto-execute
          open_todos[0]).
        </system-reminder>
```

Call signatures above are the real ones: `write_progress_md(db, project_id,
memory_dir)` (`core/progress.py:239`; call sites `pre_compact.py:501`,
`stop.py:213`, `user_prompt.py:133`, `session_start.py:680`, `mcp/server.py:243`,
`cli/mem.py:648`). See
[docs/CONTRACTS.md](CONTRACTS.md#handoff-contract) for the PROGRESS.md
schema.

### Killed-run detection (v2.4.2)

A `PreCompact` killed by the host timeout dies on `TerminateProcess`: no
`except`, no `finally`, so `.last_save.json` still describes the *previous*
successful run and the failure is invisible. The sync leg therefore writes
`memory/.pre_compact_attempt.json` **before** the transcript load
(`pre_compact.py:359-368`) and removes it only on a completed run
(`pre_compact.py:536`) — including on its own error path (`pre_compact.py:571`),
so an *errored* run is never reported as a *killed* one. `SessionStart` reports
a surviving marker, but only once it is at least 10 minutes old, so a run still
in flight is never mislabelled (`session_start.py:187-206`).

### Live plan flow (v2.2)

`ExitPlanMode` output (or user-supplied `/cc-mem plan-set` text) lands in
`plan_active.raw` with `needs_refine = 1`; the `plan-refiner` subagent
normalises it to JSON, written back via `/cc-mem plan-set --from-refiner`;
`TodoWrite` events sync step statuses mechanically by trigram-Jaccard match (no
LLM); `Edit`/`Write`/`MultiEdit`/`NotebookEdit` bump
`edits_since_last_guardian`, and sensitive Bash calls (`git push`, `rm -rf`,
`DROP TABLE`, `npm publish`, `kubectl apply`, `terraform apply`, … —
`core/plan.py:596-613`) bump it by 20. The Stop hook emits the guardian
advisory once `turns_since_last_guardian >= 8` OR
`edits_since_last_guardian >= 12` (`core/plan.py:569-585`). Hooks never spawn
subagents themselves — they only nudge. Full spec:
[docs/CONTRACTS.md](CONTRACTS.md#plan-contract). See the
[known gap](#known-gap-the-observation-gate-shadows-the-plan-branches) above
for which of these branches the observation gate currently shadows.

---

## 6. LLM backends and auth

`llm.ccl_backend.call_llm` calls Anthropic Haiku (model
`claude-haiku-4-5-20251001`, `ccl_backend.py:27`). Callers resolve one
credential up front with `core.auth.get_api_key()` and pass it in; `call_llm`
tries that one FIRST, then FALLS THROUGH to the remaining
`core.auth.get_api_candidates()` entries when a leg fails — bounded to 2
Anthropic legs total (`ccl_backend.py:149`), so the worst-case wall-clock stays
a known quantity for the consolidation BudgetGate. Candidate order and wire
format (`core/auth.py:20-57`, `_wire_for` at `core/auth.py:8-17`,
`_call_haiku` headers at `ccl_backend.py:62-72`):

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
disabled]" footer (`session_start.py:214-219`). Hook callers use it to *supply*
the credential passed into `call_llm`: `pre_compact.py:146 → :166`,
`stop.py:93`, `session_start.py:490`, `core/consolidate.py:355, 549, 724`.

Fall-through was added in v2.3.4 for a concrete failure: a dead env key (e.g.
zero credit → HTTP 400) used to blackhole the healthy subscription token behind
it and silently push every LLM call onto Ollama, cold-loading a 5.9 GB local
model per consolidation batch (`core/auth.py:30-33`, `ccl_backend.py:10-12`).

The local Ollama fallback is **opt-in and OFF by default**
(`cc_memory/config.json:63` `ccl.enabled: false`;
`ccl_backend.py:33` `_DEFAULT_OLLAMA_ENABLED = False`, so a missing key also
reads as False), alongside `ccl.ollama_url` / `ccl.local_model`. When disabled
the leg is skipped and only recorded as the reason string
`"ollama: disabled (config ccl.enabled=false)"` (`ccl_backend.py:169-170`), so
a default install has no local fallback at all.

`call_llm`'s `fallback_timeout` bounds the Ollama leg. When `None` it defaults
to `min(timeout*3, 120)`; a time-budgeted caller MUST pass an explicit value so
the worst-case in-flight wall-clock is known: at most `timeout` per Anthropic
candidate (bounded at 2) + `fallback_timeout` when Ollama is enabled. That
bound is what lets the consolidation `BudgetGate` guarantee completion before
its deadline — see `core.consolidate._worst_call_cost`
(`ccl_backend.py:127-134`).

If every enabled leg fails, `call_llm` raises `RuntimeError` carrying the
aggregated per-leg reasons (`ccl_backend.py:172-174`) and hooks degrade
gracefully — extraction is skipped, but archives/handoff/observations still
save. Hooks NEVER raise into Claude Code. (That last sentence only became true
in v2.4.2: `_extract_via_llm`'s `except` tuple did not include `RuntimeError`,
so a total LLM outage escaped to the hook's outer handler and skipped the
`PROGRESS.md` rewrite along with extraction — see `CHANGELOG.md` under 2.4.2.)

---

## 7. Per-project state (memory/)

Per-project state lives at `<project>/memory/`:

```
<project>/memory/
├── memory.db                    SQLite (WAL mode, all tables)
├── MEMORY.md                    auto-generated, refreshed after every batch write
├── PROGRESS.md                  full-rewrite from `progress` row, every Stop+PreCompact
├── PLAN.md                      full-rewrite from `plan_active` row (v2.2)
├── .last_save.json              status from last PreCompact (incl. auto/manual trigger)
├── .last_inject.json            what SessionStart actually injected (v2.3)
├── .last_consolidation.json     session count at last consolidation (v2.3.2)
├── .consolidation.lock          prevents overlapping async workers (v2.3.2)
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
(`memory_writer.py:199`); `PROGRESS.md` ← `core.progress.write_progress_md`
(`progress.py:239, 366`); `PLAN.md` ← `core.plan.write_plan_md`
(`plan.py:310`); `.plan_history/` ← `plan.py:437`; `.last_save.json` ←
`pre_compact.py:526, 556`; `.last_inject.json` ← `session_start.py:291-309`
(tempfile + `os.replace`, genuinely atomic, unlike the plain write used for
`.last_save.json`); `.last_consolidation.json` / `.consolidation.lock` ←
`consolidate_async.py:155-156`; `.pre_compact_attempt.json` ←
`pre_compact.py:284-311`. `sessions/` and `topics/` are created by whichever
path touches the project first — `user_prompt.py:44-45` on auto-init, or
`pre_compact.py:342-343`.

`memory/PROGRESS.md`, `memory/MEMORY.md`, and `memory/PLAN.md` are **generated
artifacts**. Edit the SQL source of truth instead (`progress` for PROGRESS.md,
`plan_active` for PLAN.md, `memories`/`topics`/`keywords` for MEMORY.md).

### .gitignore migrates, not just creates (v2.4.2)

`core.progress.MEMORY_GITIGNORE_LINES` (`progress.py:42-56`) is the canonical
ignore set, and `ensure_memory_gitignore` (`progress.py:59-80`) **appends only
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
`core.progress.migrate_legacy_handoff`, `progress.py:383`).

---

## 8. Install layouts

Three layouts are recognised by `cli/mem.py` `_detect_install_layouts`
(`cc_memory/cli/mem.py:103-188`). A machine can have more than one at once
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
  (`mem.py:176-187`), written by the PyInstaller installer
  (`ui/installer.py:33` `TARGET_DIR`). Hooks here are registered directly in
  `~/.claude/settings.json` by `_merge_into_settings` (`installer.py:127+`),
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
(`installer.py:74-92`) writes each `SUBPACKAGE_FILES` key
(`installer.py:37-48`) directly under `TARGET_DIR`, with **no `cc_memory/`
segment**, and `_make_hooks_config` builds commands as
`python "<TARGET_DIR>/hooks/<name>.py"` (`installer.py:104-115`):

```
~/.claude/hooks/cc-memory/           ← ui/installer.py:33 TARGET_DIR
├── __init__.py
├── config.json
├── core/    auth.py consolidate.py db.py encoding_setup.py extractor.py
│            idle.py logger.py modes.py plan.py privacy.py progress.py
├── hooks/   consolidate_async.py post_tool_use.py pre_compact.py
│            session_start.py stop.py user_prompt.py
├── llm/     ccl_backend.py memory_writer.py
├── cli/     mem.py plan.py
├── mcp/     server.py
├── ui/      dashboard.py installer.py web_viewer.py
└── logs/    ← core.logger output target
```

Note what is *absent* from the flat tree: there is no `hooks/hooks.json`
(`SUBPACKAGE_FILES` does not include it — registration goes into
`~/.claude/settings.json` instead), no `skills/`, `agents/`, `commands/`,
`docs/`, `tests/`, or `tools/`. `tools/i18n_check.py` in particular is
deliberately excluded from `SUBPACKAGE_FILES` and `build_exe.py`; the packaged
plugin is unchanged by it (see [§9.5](#95-the-checker)).

Detection accepts both shapes: `mem.py:181` tests
`(legacy / "cc_memory").exists() or (legacy / "core" / "db.py").exists()`,
with the comment recording why — probing only for `cc_memory/` made every
install this installer produces invisible to `/cc-mem status`
(`mem.py:176-179`).

### Two flat-layout inconsistencies, verified

Both are stated here because a consumer probing for the standalone layout will
hit them:

1. **`/cc-mem status` marks a healthy flat install as broken.**
   `_inspect_layout` resolves `_REQUIRED_PLUGIN_FILES` against the layout root
   (`mem.py:195`), and every entry in that list is `cc_memory/…`-prefixed
   (`mem.py:77-100`), plus `hooks/hooks.json`. A flat install has none of those
   paths, so all 22 entries report missing and the layout prints `[FAIL]` even
   when the hooks work. Detection (which accepts flat) and inspection (which
   assumes nested) disagree.
2. **The installer's own post-install instructions print a path it never
   creates.** `installer.py:479` and `:481` print
   `TARGET_DIR / 'cc_memory' / 'cli' / 'mem.py'` and
   `TARGET_DIR / 'cc_memory' / 'ui' / 'dashboard.py'`; the files land at
   `TARGET_DIR/cli/mem.py` and `TARGET_DIR/ui/dashboard.py`. The GUI path in
   the same file is correct (`installer.py:396`
   `TARGET_DIR / "ui" / "dashboard.py"`), as is the already-installed probe
   (`installer.py:311` `TARGET_DIR / "core" / "db.py"`). The module docstring's
   claim that "hooks/settings paths point to `cc_memory/hooks/<name>.py` (not
   flat)" (`installer.py:13`) is likewise stale relative to
   `_make_hooks_config`.

Neither is a documentation error being corrected — both are code defects found
while verifying this chapter, recorded so the doc does not repeat them.

### Interpreter requirement

`hooks/hooks.json` invokes `python3`. On Linux/macOS this is the standard
Python 3 binary. On Windows the python.org installer ships `python.exe` plus
the `py.exe` launcher but NOT `python3.exe` by default — install "Add Python to
PATH" + tick "py launcher", or alias `python3 -> python`, before installing the
plugin. Otherwise hooks fail silently (logged to
`~/.claude/hooks/cc-memory/logs/`, but Claude Code shows no error UI for a
missing command). The standalone installer sidesteps this by probing:
`_detect_python_cmd` uses `python3` only if `shutil.which("python3")` finds it,
else `python` (`installer.py:95-96`).

---

## 9. Documentation language convention (i18n)

This chapter defines how cc-memory keeps human-facing documentation in more than
one language **without silent drift**. English is the canonical skeleton; other
languages are drift-tracked siblings tied to a hash of their English source.

It is the English source-of-truth for the convention. The checker that enforces it
is `tools/i18n_check.py` (pure stdlib, dev/CI only — not shipped in the plugin).

> Merge note: this chapter was `docs/I18N.md` through v2.4.1. The Tier-3 guard
> comments in `core/extractor.py:34, 72`, `hooks/session_start.py:271`,
> `hooks/user_prompt.py:125` and the pointer in `cc_memory/__init__.py:37` still
> cite `docs/I18N.md §1`; read that as §9.1 below until those strings are
> refreshed.

### 9.1 The three-tier language model

The whole system rests on separating three different things people mean by
"language", each with its own rule:

| Tier | What | Rule | Where it lives |
|------|------|------|----------------|
| 1 — Skeleton | English canonical docs + all LLM-facing strings | English is authoritative; every translation needs an English source | `README.md`, `docs/*.md`; hook / CLI instruction strings |
| 2 — Translation | Human-read docs in another language | `NAME.<lang>.md` sibling, drift-tracked, produced on demand | `README.zh.md` (the only translation that exists today; `docs/*.zh.md` on demand) |
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
  `core/extractor.py:35-69` (`_PATTERNS`), `core/extractor.py:73-77`
  (`_IMPORTANCE_BOOST`), `hooks/session_start.py:269-273` (RESUME PROTOCOL
  tokens) and `hooks/user_prompt.py:127-130` (`resume_signals`); the last two
  must stay in sync with each other, since the forced reminder promises the
  behavior that `user_prompt` types as `resume_request`.

Only **Tier 2** — the human-facing docs — is what this convention version-controls.

### 9.2 File naming

- `NAME.md` — the canonical **English** source (the skeleton).
- `NAME.<lang>.md` — a translation sibling. Today `<lang>` is `zh` (Simplified
  Chinese); the only translation that currently exists is `README.zh.md`.
- Every translation MUST have a matching English source. A `NAME.zh.md` with no
  `NAME.md` is an **ORPHAN** (checker fails). There are no translation-only docs.

Tracked set (what the checker looks at): `README.md` at the repo root plus
`docs/*.md`, excluding `*.zh.md` (`tools/i18n_check.py:146-157`). Translations
are `README.zh.md` and `docs/*.zh.md`, non-recursive
(`tools/i18n_check.py:160-166`). After the v2.4.2 doc consolidation the tracked
English set is exactly three files — `README.md`, `docs/ARCHITECTURE.md`,
`docs/CONTRACTS.md` — not the five that existed before the merge. Both
`docs/ARCHITECTURE.md` and `docs/CONTRACTS.md` currently have no `.zh.md`
sibling, i.e. both are MISSING-TRANSLATION, a soft warning that never fails the
build.

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
MISSING-TRANSLATION is not in `FAIL_STATES` (`tools/i18n_check.py:68`). Adding
the switcher is step 2 of the 5-step sequence in §9.6; steps 3-5 must follow in
the same change.

**This file carries a switcher** (line 1) because `docs/ARCHITECTURE.zh.md` was
authored in the same release (v2.4.3) that added it — steps 2-5 of §9.6 were
completed together, which is exactly what the `I18N.md` precedent above says to
do. `docs/CONTRACTS.md` has **no** switcher, correctly: it has no translation
yet, and adding one would recreate the dead link this merge removed.

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

Drift is decided **solely** by the `sha256`. `version` and `translated` are
informational, so a future version bump never mass-flags every translation as stale —
only an actual change to the English *content* does.

Marker parsing is **fail-closed** (`tools/i18n_check.py:107-124`): it is
BOM-tolerant, but any read/decode error, or a first line that does not match the
grammar, yields `None` and the caller reports NO-MARKER (a FAIL state) rather
than silently treating the translation as valid.

#### Hash normalization (the cross-platform-critical part)

The digest is taken over a **normalized** form of the English source, and the exact
same normalizer runs at emit time and at check time. This is what makes the hash
stable across Windows/Unix: CRLF vs LF, a UTF-8 BOM, or trailing-whitespace churn
cannot move the digest.

Recipe (`normalize_markdown` in `tools/i18n_check.py:85-97`):

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
`SUBPACKAGE_FILES` (`installer.py:37-48`), `build_exe.py`, and `cli/mem.py`
`_REQUIRED_PLUGIN_FILES` (`mem.py:77-100`), so the packaged plugin is unchanged
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
(`FAIL_STATES`, `tools/i18n_check.py:68`; `main` returns `1` on failure,
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

If instead you delete or rename an English doc, delete or rename its translation too,
or the checker will report `[FAIL] ORPHAN`.

### 9.8 Scope and exclusions

**In scope (translated on demand):** `README.md` and `docs/*.md`.

**Explicitly excluded from translation:**

- `CLAUDE.md`, `commands/`, `skills/`, `agents/` — Claude-facing, and their YAML
  front-matter is owned by the loader; adding unknown keys risks loader rejection.
- `CHANGELOG.md` — append-only release churn; not a document you read top-to-bottom.
- `memory/**` — generated artifacts.
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
  `normalize_markdown` rstrips every line (`tools/i18n_check.py:96`), so trailing
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
