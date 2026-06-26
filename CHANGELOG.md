# Changelog

All notable changes to cc-memory are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.3.0] — 2026-06-26

The "memory quality + observability" release. Fixes two long-standing problems:
(1) the database accumulated unboundedly because the anti-patch writer's
char-level trigram-Jaccard only catches near-VERBATIM restatement, so the same
fact reworded each session always took the INSERT branch; (2) there was no way
to tell whether injected memory was actually read or used. Designed and
adversarially verified against the live DB (a 21-node false-merge cluster and
~15 wrongly-archived durable facts in the naive approaches were caught and
designed out before implementation).

### Added

- **LLM-judged semantic de-duplication** (`consolidate.semantic_dedup`). Word-
  Jaccard nominates small SAME-CATEGORY candidate groups (≤4, no transitive
  union-find — that produced a giant cross-fact blob on the live DB), Haiku
  confirms same-fact, the survivor's content is refreshed to a merged canonical
  and losers are archived (`is_active=0`) with a forward `supersedes_id` link.
  Validated on the live DB: 4/4 correct merges, distinct facts left alone.
- **Obsolescence detection** (`consolidate.detect_obsolete_llm`). Per category,
  oldest+newest rows are shown together so old-vs-new contradictions co-occur;
  Haiku names `{stale_id, current_id}` pairs. A **temporal guard** (the
  superseding memory must be NEWER) + an **anti-event prompt** (a one-time
  action like "uninstalled X" never obsoletes descriptive facts) prevent the
  false archives the live-DB dry-run exposed (15 → 3, 0 dangerous).
- **Reference-aware staleness net** (`consolidate.decay_and_archive`). Archives
  ONLY rows that are simultaneously very old (`effective_age > 180d` via
  `created_at`/`last_referenced_at`, immune to `updated_at` churn), low
  importance (≤2), AND never injected — a zero-false-archive safety net.
- **Conservative topic canonicalization** (`consolidate.canonicalize_topics`).
  Merges fragmented labels ('cc-memory','cc-memory backend','cc-memory-fixes' →
  'cc-memory') with token-Jaccard≥0.6, but REFUSES single-bare-token hub merges
  (so distinct 'memory-bloat'/'memory-injection' stay separate). Relabel-only,
  fully decoupled from archiving.
- **Injection observability**: SessionStart writes `memory/.last_inject.json`
  (atomic) recording exactly which memories/topics were injected; SessionStart
  prints a one-line recap; new `/cc-mem inject-show` (ground-truth dump) and
  `/cc-mem inject-usage` (deterministic signals: did Claude Read
  PROGRESS.md/MEMORY.md). No unreliable `#id`-guessing.
- **`/cc-mem encoding-check [--apply]`** — read-only U+FFFD corruption scan
  across text tables (confirmed live: 0 in memories/topics/progress).
- **`v6` migration**: `memories.last_referenced_at` + index. Reference bumping
  on every SessionStart injection keeps surfaced facts "young".
- **Shared substrate** in `consolidate.py`: `is_decodable` (mojibake guard,
  preserves valid CJK), `effective_age_days` (created_at-based), and a
  `BudgetGate` that bounds in-hook LLM calls against the 45s PreCompact budget.
- New DB methods: `bump_last_referenced`, `archive_obsolete` (forward-linked,
  no new row), `get_referenced_id_set`.

### Changed

- `run_consolidation` stage order is now load-bearing: garbage → lexical dedup
  → **semantic dedup** → topic assign → **canonicalize** → summarize →
  **decay+staleness net** → **obsolescence** → archive_consolidated (content-
  near-dup guarded). All in-hook LLM stages are budget-gated; `_maybe_consolidate`
  passes a residual-budget gate seeded with the PreCompact hook start time.
- `archive_consolidated` now only archives over-cap members that are CONTENT
  near-duplicates (trigram≥0.65) of a kept member — so topic label merging can
  never cause a distinct fact to be archived.
- `build_context` (SessionStart) returns/records injected memory ids and bumps
  their `last_referenced_at`.

### Fixed

- **Unbounded memory accumulation** (the "shit mountain"): the root cause was
  lexical-only dedup. Confirmed on the live DB — 122 active memories but only
  2 pairs reached trigram-Jaccard ≥0.5 while many were the same fact reworded.
- **No read/use observability**: SessionStart injected context silently with no
  user-visible signal.
- **Corrected a misdiagnosis**: rows that looked like GBK mojibake (#98/#105/
  #107) are valid Chinese (`重构目标`, `marketplace清单`, `安装脚本`); the
  garble was a cp936 terminal rendering artifact. `memories`/`topics`/`progress`
  have 0 U+FFFD. No data-repair migration was warranted.

### Notes

- All consolidation archival is recoverable (`is_active=0`, never `DELETE`).
  `docs/MEMORY_RULES.md` documents the consolidation-backstop exception to the
  "route every write through memory_writer" rule.

---

## [2.2.0] — 2026-05-25

The "live plan anchor + subagent" release. Adds `memory/PLAN.md` as a
project-level task anchor backed by a new SQL table, two plugin-shipped
subagents (`plan-refiner`, `plan-guardian`) that the main Claude invokes
on Stop-hook nudges, and a polished CLI/Skill surface. Backwards-compatible
for stored data; the v4 migration applies to existing DBs on the next hook
that touches them.

### Added

- **`memory/PLAN.md`** — live plan document, full-rewritten from the
  `plan_active` SQL row on every relevant event. Distinct from
  `PROGRESS.md` (which remains the session-handoff doc). See
  [`docs/PLAN_PROTOCOL.md`](docs/PLAN_PROTOCOL.md).
- **`plan_active` SQL table (v4 migration)** — single row per project with
  `raw`, `structured` (JSON), `active_step`, `edits_since_last_guardian`,
  `turns_since_last_guardian`, `last_guardian_at`, `last_refined_at`,
  `needs_refine`, `created_at`, `updated_at`.
- **`cc_memory/core/plan.py`** — schema validation
  (`is_valid_structured`, `normalize_structured`), trigram-Jaccard
  TodoWrite→step matching (`match_todos_to_steps`, `sync_todos_to_steps`),
  PLAN.md renderer (`render_plan_md`, `write_plan_md`), capture/apply
  entry points (`capture_exit_plan_mode`, `apply_refined_plan`,
  `apply_todowrite_sync`), and drift-nudge logic
  (`should_nudge_guardian`, `is_sensitive_tool_call`).
- **`agents/plan-refiner.md`** — one-shot subagent that converts a raw
  plan document into the canonical JSON schema. Tools: Read, Grep, Bash.
  Model: haiku.
- **`agents/plan-guardian.md`** — read-only subagent that compares
  PLAN.md + PROGRESS.md against recent activity and reports alignment in
  ≤150 words. Tools: Read, Grep, Bash (read-only operations only).
- **Seven new `/cc-mem` subcommands**: `plan-status`, `plan-show`,
  `plan-set --raw / --raw-file / --from-refiner`, `plan-check`,
  `plan-replan`, `plan-clear`.
- **`/cc-mem dashboard`** subcommand — launches the Tkinter GUI by
  auto-resolving `dashboard.py` relative to `cli/mem.py`. Works under
  marketplace and standalone installs without hardcoded paths.
- **PostToolUse hook** now special-cases three tool types: `ExitPlanMode`
  (captures raw plan + marks needs_refine), `TodoWrite` (mechanical
  step-status sync, no LLM), and `Edit/Write/MultiEdit/NotebookEdit`
  (bumps the guardian drift counter).
- **Sensitive Bash patterns** (`git push`, `rm -rf`, `drop table`,
  `npm/cargo publish`, `kubectl/terraform/ansible apply`) bump the
  drift counter by 20 so the next Stop emits a guardian-recommendation
  status line.
- **Stop hook plan nudges** — single advisory status line (no
  `<system-reminder>` spam):
  - `[cc-memory.plan] NEW PLAN captured … invoke @plan-refiner` when
    `needs_refine = 1`,
  - `[cc-memory.plan] guardian check recommended (turn_threshold | edit_threshold)`
    when counters cross thresholds.
- **`docs/PLAN_PROTOCOL.md`** — full spec: lifecycle diagram, JSON
  schema, sync algorithm, nudge thresholds, sensitive-tool list.
- **`enable_utf8_io()` in `core/encoding_setup.py`** — idempotent stdio
  UTF-8 reconfigure called by every hook entry. Prevents `gbk`-crash on
  Windows when status lines contain glyphs (e.g. `↻`).
- **MEMORY.md auto-warning block** — every regen emits a strong
  "AUTO-GENERATED · DO NOT EDIT BY HAND" header pointing to the
  `/cc-mem add` workflow.
- **`_inspect_layout`** + `_print_layout_report` in `cli/mem.py` —
  marketplace-aware install-layout health check used by `/cc-mem status`.
- **RESUME PROTOCOL** in `session_start._build_forced_reminder` — the
  forced `<system-reminder>` now includes Chinese + English resume-signal
  whitelist tokens and a directive to read `open_todos[0]` first.
- **Tier-3 transcript fallback** in `session_start._refresh_progress_row`
  — when DB sources are empty, mine the prior session's JSONL transcript
  for TodoWrite snapshots and file edits to seed PROGRESS.md.
- **Last-wins TodoWrite extraction** in `core/extractor.extract_latest_todo_state`
  — replaces the previous "stack every TodoWrite" behaviour, eliminating
  duplicate todos in PROGRESS.md.

### Changed

- **Repository layout**: new `agents/` directory (plugin-shipped
  subagents) and `cc_memory/core/plan.py`.  `core/encoding_setup.py`
  promoted from incidental import to a first-class module listed in
  `_REQUIRED_PLUGIN_FILES`, packaging manifests, and CLAUDE.md.
- **`commands/cc-mem.md`** — the bash invocation block now resolves the
  plugin root via `CLAUDE_PLUGIN_ROOT` with a fallback to
  `~/.claude/hooks/cc-memory/`, fixing the v2.1 issue where the slash
  command only worked for standalone installs.
- **`skills/ccm-load/SKILL.md`** — replaced the hardcoded
  `D:/Projects/cc-memory/cc_memory` path with a 3-tier resolver
  (`CLAUDE_PLUGIN_ROOT` → settings.json marketplace path → standalone
  install). Skill now works on any host.
- **`ui/dashboard.py`** — "Add Memory" dialog and "Save Session"
  workflow both routed through `upsert_smart` / `upsert_batch`
  respectively. No more direct `db.insert_memory` callers in the
  dashboard (closes the v2.1 known gap).
- **Hooks**: `post_tool_use.py`, `stop.py`, and `session_start.py` all
  call `enable_utf8_io()` first thing on entry.
- **`installer.py`** + **`build_exe.py`**: `SUBPACKAGE_FILES` now lists
  `core/plan.py` and `core/encoding_setup.py` (the latter was missing
  from packaging in v2.1).
- Version bumped from `2.1.0` to `2.2.0` in all locations
  (`__init__.py`, `config.json`, `plugin.json`, `marketplace.json`,
  `pyproject.toml`, `mcp/server.py`).

### Removed

- **`skills/mem-init/SKILL.md`** — its only job (creating `memory/`) is
  auto-done by `UserPromptSubmit` and `/ccm-load` step 2 covers manual
  re-init.
- **`skills/mem-status/SKILL.md`** — duplicate of the more discoverable
  `/cc-mem status` slash command.

### Fixed

- **Plugin manifest schema** — non-standard fields in `plugin.json`
  that blocked Claude Code's plugin discovery have been stripped.
- **`ccm-load` skill** had a hardcoded Windows path (`D:/Projects/...`)
  that made it work only on the maintainer's machine.
- **`/cc-mem` slash command path** — `commands/cc-mem.md` used the
  v2.0 standalone install path (`~/.claude/hooks/cc-memory/...`) which
  doesn't exist under marketplace installs. Now uses
  `${CLAUDE_PLUGIN_ROOT}` with the standalone path as fallback.
- **Dashboard discoverability** — marketplace-installed users had no
  obvious entry point to the GUI. `/cc-mem dashboard` now resolves
  it under any install layout.
- **`session_start.py` fill-only-empty contract** — pre-set fields on
  the `progress` row (from a fresh PreCompact) are no longer
  overwritten by a stale `session_summary` during refresh.
- **TodoWrite stacking** in PROGRESS.md — was accumulating every
  TodoWrite snapshot ever made; now uses last-wins via
  `extract_latest_todo_state`.

### Migration notes

- **Existing v2.1 installations**: the v4 migration runs the first
  time any hook touches `memory.db`. No action needed.
- **Plan feature is opt-in**: until the user enters Claude's plan mode
  or invokes `/cc-mem plan-set --raw`, `plan_active` stays empty and
  no `PLAN.md` is generated. Existing projects are unaffected.
- **Subagents must be discoverable**: this release ships
  `agents/plan-refiner.md` and `agents/plan-guardian.md` inside the
  plugin tree. After upgrading, run `/ccm-load` and confirm the
  subagents appear (a future cc-memory CLI subcommand may verify
  discovery; for now check with `Task(...)`).

---

## [2.1.0] — 2026-05-21

The "anti-patch + forced handoff" release. Major restructure of save paths and
handoff mechanics. Backwards-compatible for stored data (existing DBs migrate
forward automatically); existing installations need `installer.py` re-run to
update settings.json paths to the new subpackage layout.

### Added

- **`llm.memory_writer.upsert_smart`** — unified anti-patch write entry. All
  save paths (PreCompact, Stop observer, `/save-memories` skill, MCP `memory_add`,
  CLI `mem.py add`) now route through one function that decides MERGE_IN_PLACE
  vs SUPERSEDE vs INSERT based on trigram-Jaccard similarity. See
  [`docs/MEMORY_RULES.md`](docs/MEMORY_RULES.md).
- **`memories.supersedes_id`** column + `db.get_supersede_chain(id)` — preserves
  update history. Walk a chain via `mem.py supersedes <id>`.
- **`progress` SQL table** + **`memory/PROGRESS.md`** — replaces v2.0
  `SESSION_HANDOFF.md`. Always full-rewritten from the SQL row, never appended.
  See [`docs/HANDOFF_PROTOCOL.md`](docs/HANDOFF_PROTOCOL.md).
- **Forced `<system-reminder>` at SessionStart** — instructs the next session
  to `Read memory/PROGRESS.md` before responding. Replaces the soft "remember
  to call /save-memories" text spam.
- **`core.idle.maybe_run_idle`** — every 5 user turns, run lightweight no-LLM
  reorg (garbage cleanup + topic assignment + MEMORY.md regen) from the Stop
  hook. Closes the "MEMORY.md goes 50 days stale between PreCompacts" gap.
- **`memory_writer.regenerate_memory_index`** — `memory/MEMORY.md` is now
  refreshed after every batch write, not just at PreCompact.
- **`core.progress`** — PROGRESS.md generator (`write_progress_md`),
  state collector (`collect_progress_state`), and one-shot migrator
  (`migrate_legacy_handoff`) that renames stale `SESSION_HANDOFF.md` to
  `SESSION_HANDOFF.md.v2.bak`.
- New CLI subcommands:
  - `mem.py progress` — force-regenerate `memory/PROGRESS.md`.
  - `mem.py supersedes <id>` — walk the supersede chain for a memory.
- New MCP tools: `progress_get`, `progress_regenerate`.
- `pyproject.toml`, `commands/cc-mem.md`, `docs/{ARCHITECTURE,MEMORY_RULES,HANDOFF_PROTOCOL}.md`,
  `CHANGELOG.md` — proper plugin packaging and documentation.

### Changed

- **Repository layout**: `cc_memory/` reorganized into subpackages
  `core/` (db, extractor, consolidate, idle, progress, privacy, modes, auth,
  logger), `hooks/` (5 hook entry points), `llm/` (ccl_backend, memory_writer),
  `cli/` (mem, plan), `mcp/` (server), `ui/` (installer, dashboard, web_viewer).
  Reduces the previous 22-file flat directory.
- `hooks/hooks.json` paths updated to `cc_memory/hooks/<name>.py`.
- `installer.py` (was `installer_standalone.py`) now mirrors the subpackage
  layout under `~/.claude/hooks/cc-memory/` and auto-detects/cleans v2.0
  flat-layout installs on upgrade.
- `build_exe.py` bundles the subpackage tree into `cc_memory_files/<subdir>/`.
- `extractor.py`: removed hard-coded astrophysics/ML keywords
  (`CNN, Swin, GNN, HOG, SBI, TDA, fusion, LOCO, ...`) that contaminated this
  generic plugin. Metric extraction is now project-neutral.
- `consolidate.py`: removed the same astro `_GROUPS` dict; topic clusters now
  derive purely from project keyword frequency.
- `session_start.py`: layered context injection rebalanced — `progress`
  preview now takes 25% of the budget (was 15% for `handoff`).
- `stop.py`: removed the "remember to call /save-memories" text reminder
  (replaced by the SessionStart forced reminder).
- Version bumped from `2.0.0` to `2.1.0` in all locations
  (`__init__.py`, `config.json`, `plugin.json`, `marketplace.json`,
  `mcp/server.py`).

### Removed

- `.claude/skills/` directory — was a duplicate of `skills/` ("stacking"
  violation). `skills/<name>/SKILL.md` is now the only canonical location.
- `cc_memory/skill_template.md` — was a third divergent copy of the
  `save-memories` skill. Deleted; installer deploys from `skills/`.
- `cc_memory/skill_status.md` — duplicate of `skills/mem-status/SKILL.md`.
- `cc_memory/installer.py` — superseded by `cc_memory/ui/installer.py`
  (renamed from `installer_standalone.py`, which is also removed).
- `cc_memory/setup.py` — redundant with auto-init in `UserPromptSubmit`.
- `MemoryDB.global_db()` cross-project registry — dead code, never wired up.
- Orphan `memory_timeline` mention in `mcp_server.py` docstring (the tool
  was declared but never implemented).

### Fixed

- `memory/MEMORY.md` going 50+ days stale because only PreCompact regenerated
  it. Now every write path (Stop observer, /save-memories, mem.py add, MCP
  add) calls `regenerate_memory_index` automatically.
- `memory/SESSION_HANDOFF.md` accumulating pollution (Bash output, log
  fragments, tool error text) because of append-style writes. Replaced
  entirely by PROGRESS.md, which never appends.
- Multiple version strings drifting out of sync (CLAUDE.md said 1.1.0;
  `__init__.py` said 2.0.0; README said 14 modules when there were 22).
  All metadata is now generated/validated from a single source.
- The `save-memories` skill bypassing `is_duplicate_hash` and using its own
  in-memory set-membership check (which missed punctuation variants).

### Migration notes

- **Existing installations**: re-run `installer.py` (or
  `cc-memory-installer.exe`). The installer detects v2.0 flat-layout files
  and removes them before laying down the v2.1 subpackage structure. Your
  per-project `memory.db` is migrated forward in place by `_MIGRATIONS:v3_*`
  (adds `supersedes_id` and the `progress` table).
- **Existing `SESSION_HANDOFF.md`**: on first PreCompact under v2.1, the
  file is renamed to `SESSION_HANDOFF.md.v2.bak`. PROGRESS.md takes over.
- **Hook commands in `~/.claude/settings.json`**: paths change from
  `…/cc-memory/pre_compact.py` to `…/cc-memory/cc_memory/hooks/pre_compact.py`.
  The installer rewrites these automatically.

---

## [2.0.0] — earlier

PostToolUse capture, FTS5 search, progressive disclosure context injection,
MCP server, web viewer, privacy tags, mode system. (Pre-2.1 history is
condensed; see git log for detail.)

## [1.1.0] — earlier

Initial public version: 3 hooks (PreCompact / SessionStart / Stop), SQLite
backend, LLM extraction via Haiku, /save-memories skill.
