# CLAUDE.md — Operating manual for Claude Code working on cc-memory

## Project: cc-memory

**Claude Code persistent memory plugin (v2.16.0)** — anti-patch reconcile-on-write
with LLM-judged semantic de-duplication and backpressure-triggered consolidation,
a forced PROGRESS.md handoff, a live PLAN.md anchor with plan-refiner /
plan-guardian subagents and a mandatory carryover gate, an enforced directive
ledger, layered SessionStart injection shaped by the start reason, query-time
recall, CJK-capable FTS5 search, and Haiku extraction from detached workers
(optional local Ollama fallback).

- **Language**: Python 3.8+ (pure stdlib, zero pip dependencies at runtime)
- **Version**: 2.16.0 — what changed in this version is `CHANGELOG.md` § [2.16.0]
- **License**: MIT
- **Platform**: Windows-primary; CI runs every gate on Windows and Linux
  (Tkinter required for the GUI)

This file is the manual: what runs, where state lives, what each channel
costs, and how a change is verified and released. The rules a change must not
break are numbered in **`INVARIANTS.md`** (symbols, gates and falsification
cases, no line numbers). The narrative of every release — what was measured,
what the first fix got wrong — is **`CHANGELOG.md`**; the rules each release
wrote into this file before v2.16.0 sit under that version's entry as *Rules
recorded in CLAUDE.md at release*.

## Repository layout

```
cc-memory/
├── .claude-plugin/plugin.json   ← plugin manifest (+ inline mcpServers entry)
├── .claude-plugin/marketplace.json
├── hooks/hooks.json             ← 6 hook commands across 5 events
├── skills/                      ← ccm-load (activation + init), save-memories
├── agents/                      ← plan-refiner.md, plan-guardian.md
├── commands/cc-mem.md           ← the /cc-mem slash command
├── docs/                        ← ARCHITECTURE.md, CONTRACTS.md (+ .zh.md siblings),
│   │                              debug-pass-2026-09.md (evidence record),
│   └── plans/                     the v2.16.0 plan (.txt, unscanned)
├── demo/                        ← run_demo.py + tally/ fixture + captures/
├── cc_memory/
│   ├── config.json              ← consolidation cadence, Ollama fallback, opt-out
│   ├── core/                    db, extractor, consolidate, idle, progress, plan,
│   │                            prompts, privacy, recall, modes, roots, layout,
│   │                            auth, logger, encoding_setup, version, atomic,
│   │                            markers, textsim
│   ├── hooks/                   _entry, post_tool_use, pre_compact,
│   │                            consolidate_async, session_start, stop, user_prompt
│   ├── llm/                     ccl_backend, memory_writer, parse, usage_judge
│   ├── cli/                     mem
│   ├── mcp/                     server
│   └── ui/                      installer, dashboard, web_viewer
├── tests/                       ← run_gates.py + five suites (see § Tests)
├── tools/                       ← dev/CI checkers, never packaged
├── scripts/                     ← build_exe.py, release_notes.py, bench_hooks.py
├── .github/workflows/           ← gates.yml, release.yml
├── INVARIANTS.md                ← the numbered rules
├── CHANGELOG.md · CONTRIBUTING.md · SECURITY.md · README.md · README.zh.md
└── CLAUDE.md                    ← this file
```

## Hooks (6) <!--ce:hooks-->

Declared in `hooks/hooks.json`. A marketplace / dev-checkout install is
discovered via `enabledPlugins` + `extraKnownMarketplaces` in
`~/.claude/settings.json`; only the standalone installer
(`ui/installer.py:_merge_into_settings`) writes hook entries into
`settings.json`. `PreCompact` fires TWO command hooks <!--ce:hooks:subset-->: a
blocking sync leg and a background `async` leg.

| Hook | Entry | Timeout | Matcher | Does | Detached worker | stdout reaches the model? |
|---|---|---|---|---|---|---|
| `UserPromptSubmit` | `hooks/user_prompt.py` | 8 s | all | auto-init `.ccm/` (migrating a pre-v2.13.0 `memory/`), count the turn, seed `progress.current_request` once per session on the first non-scaffolding prompt, **query-time recall**, print the plan advisory a Stop parked | — | **yes** — the recall block, the parked advisory, or nothing |
| `PostToolUse` | `hooks/post_tool_use.py` | 8 s | `core.modes.HOOK_TOOL_MATCHER` (the tools any mode observes + the plan legs; spelled in `hooks.json` and `ui/installer.py:HOOK_MATCHERS`) | live plan anchor in EVERY mode (ExitPlanMode capture, TodoWrite step sync, drift counters), THEN one observation row for observed tools | — | no (empty) |
| `Stop` | `hooks/stop.py` | 22 s | all | decide whether to spawn the observer, idle reorg every `IDLE_INTERVAL_TURNS`, per-turn `patch_progress(files_touched)`, backpressure probe, plan enforcement | `stop.py --observe` (Haiku extraction from this turn's observations, `.observer.lock`) | **no** — empty on a turn that may close; the `{"decision": "block"}` document on a refusal |
| `PreCompact` (sync) | `hooks/pre_compact.py` | 120 s | all | bounded transcript window → Haiku extract → `upsert_batch` → FULL-REWRITE PROGRESS.md → archive | — | no (empty; status goes to the log) |
| `PreCompact` (async) | `hooks/consolidate_async.py` | 300 s, `async: true` | all | budget-gated consolidation every N sessions or on backlog | itself; also spawned standalone (`--cwd`) by the Stop probe | no |
| `SessionStart` | `hooks/session_start.py` | 15 s | all | layered injection shaped by `source` + the forced `<system-reminder>` | `session_start.py --retro` (retroactive save of unsaved transcripts, `.retro.lock`) | **yes** — the injection |

Hook contract (never violate):

- A hook NEVER writes to stderr (Claude Code shows it as error UI) and NEVER
  raises: `try: ... except Exception: pass` with a `# why: ...` comment is the
  right shape in hook code, logging through `core.logger.get_logger(...)`
  (`~/.claude/hooks/cc-memory/logs/`). Always `sys.exit(0)`.
- Every hook enters through `hooks/_entry.py`: `parse_payload` (stdin read to
  EOF) and `resolve_project` (`core.modes.is_excluded` on the RAW cwd, THEN
  `core.roots.project_root`). No hook imports those two directly.
- Every LLM-calling path passes an absolute `deadline` into `call_llm`
  (`stop.py:_LLM_DEADLINE_S`, `session_start.py:_RETRO_DEADLINE_S`, the
  PreCompact envelope); a hook never reads a transcript except through
  `core.extractor.load_transcript_window`.
- Workers are spawned only through `hooks/_entry.py:spawn_detached`, take
  their lock only through `consolidate_async._acquire_lock`, and never call
  the model while `core.auth.llm_backoff` says a call recently failed.

## Database schema (12 tables)

Defined in `cc_memory/core/db.py`; the diagram is `docs/ARCHITECTURE.md` §4.

- `projects`, `sessions`, `memories` (+ the `memories_fts` trigram index),
  `topics`, `keywords`, `plans` (the v2.0 queue table — its CLI, dashboard tab
  and `MemoryDB` methods were deleted in v2.16.0; the table stays)
- `observations` (PostToolUse events; fed to the observer above
  `projects.obs_watermark`, then to PreCompact only above
  `MemoryDB.observer_cursor`, and deleted once fed)
- `session_summaries`, `progress` (single row per project, SOT for PROGRESS.md),
  `plan_active` (single row per project, SOT for PLAN.md), `directives` (the
  user-INTENT ledger), `_migrations`

`PRAGMA user_version` carries `_bootstrap_stamp`, derived from the schema text
and the migration ledger's names, so a settled open runs one pragma instead of
the whole bootstrap; the two self-healing probes (`_detect_fts5`,
`_ensure_active_hash_unique`) stay outside the stamp on purpose. Key columns:
`memories.supersedes_id` (the update chain), `memories.content_hash`
(sha256[:16] of normalised content), `memories.recall_count`,
`plan_active.turns_total` / `guardian_checked_at_turn`,
`directives.turns_at_touch`.

## The three contracts

Specified in `docs/CONTRACTS.md`; the sentences below are the operating summary.

**Anti-patch** (`docs/CONTRACTS.md#anti-patch-contract`). Every memory save path
routes through `llm.memory_writer.upsert_smart` / `upsert_batch`, which MERGES
in place, SUPERSEDES with a chain link, REINFORCES an exact-hash duplicate
(importance max + tags union, no new row) or INSERTS, on `core.textsim`
similarity (`HIGH_SIM` / `MID_SIM`; CJK bigrams, ASCII trigrams). Never call
`db.insert_memory` from a caller path — its only callers are the writer's own
`supersede_memory` and the tests. The save paths: `hooks/pre_compact.py`,
the `stop.py --observe` worker, `cli/mem.py add`, `mcp/server.py memory_add`,
`skills/save-memories/SKILL.md`, `ui/dashboard.py` (Add Memory, Save Session,
new-project init), `ui/web_viewer.py` POST `/api/memory`, and the
`session_start.py --retro` worker. MEMORY.md is regenerated only after a batch
that wrote something.

**Forced handoff** (`docs/CONTRACTS.md#handoff-contract`). `.ccm/PROGRESS.md` is
ALWAYS full-rewritten from the `progress` row, never appended. Writers:
PreCompact overwrites the row (`upsert_progress`); Stop patches
`files_touched` per turn (`patch_progress`, one transaction); UserPromptSubmit
seeds `current_request` once per session (`strip_scaffolding` is the predicate
both ingresses share, the `cc_mem_seeded_` marker records the seed);
SessionStart fills ONLY still-empty fields (`fill_empty_progress`, emptiness
tested inside the UPDATE). §4 renders `plan_active`, §5 renders the memories
store at `core.db.CRITICAL_IMPORTANCE`; the `critical_context` column is
retired. The SessionStart `<system-reminder>` demands a Read of PROGRESS.md and
nothing else, with the first-reply ack (`core.progress.ACK_TEMPLATE`) except
after a compaction.

**Live plan anchor** (`docs/CONTRACTS.md#plan-contract`). `plan_active` backs
`.ccm/PLAN.md`. `PostToolUse` captures `ExitPlanMode` into `plan_active.raw`
(`needs_refine = 1`) and syncs `TodoWrite` to step statuses mechanically; these
legs run in EVERY mode, above the `should_observe` gate. The main Claude
refines through the `plan-refiner` subagent and `/cc-mem plan-set
--from-refiner`, which passes the carryover gate (each unfinished step
auto-carried or dispositioned; no force flag; every outgoing plan archived to
`.ccm/.plan_history/`). `Stop` REFUSES the turn (`core.plan.blocking_reasons`
→ `{"decision": "block"}`) when a raw plan is unrefined, when the guardian
thresholds are crossed (`GUARDIAN_TURN_THRESHOLD` turns or
`GUARDIAN_EDIT_THRESHOLD` edits; a sensitive Bash call counts 20), or when an
active directive has been idle past `DIRECTIVE_IDLE_TURNS`; the remedy is the
`plan-guardian` subagent FIRST, then `/cc-mem plan-check` to record it. The
escape budget (`_BLOCK_MAX_CONSECUTIVE`, per episode) degrades a persistent
refusal to an advisory that the next UserPromptSubmit prints;
`CC_MEMORY_PLAN_ENFORCE=0` is the kill switch. Directives outlive plans, are
closed only with `--evidence`, and reference steps by title. Hooks never spawn
subagents.

## Injection channels and budgets

The model reads the stdout of SessionStart and UserPromptSubmit and nothing else. Every
Claude-visible sentence is spelled once in `core/prompts.py` (`/cc-mem
inject-show --templates` lists them), and every slot that carries stored text is
a render path — `neutralize_inline` for one-line slots, `neutralize_block` where
newlines are structure; `python tools/contracts.py` lists the modules.

| Channel | When | Renderer | Budget | De-duplication |
|---|---|---|---|---|
| Directive ledger | SessionStart, first layer; also `PLAN.md` even with no plan | `core.plan.render_directive_lines` via `session_start._build_directives_layer` | 0.10 of `_DEFAULT_BUDGET` (16 000 chars) | constraints first, then most-repeated; an over-budget ROW is skipped, never the layer |
| Knowledge base (topics) | SessionStart | `_build_topics_layer` | 0.25 | a topic with only a fallback summary (`FALLBACK_SUMMARY_PREFIX`) does not cover its critical rows |
| Critical (unmerged) | SessionStart | `_build_critical_layer` | 0.20 | rows at `CRITICAL_IMPORTANCE`; a topic-covered row is dropped here and recorded as `topic_covered_ids` |
| Recent (timeline) | SessionStart | `_build_timeline_layer` | 0.20 | ordered by id; never re-lists a row in `shown_ids ∪ covered` |
| PROGRESS digest | SessionStart | `core.progress.render_progress_digest` via `_build_progress_digest` (§1-§4 of the row, with the file's own slot renderers; file preview only when there is a file and no row) | 0.15 | — |
| Footer + forced reminder | SessionStart | `_build_footer`, `_build_forced_reminder(demand_ack=)` | 0.10 | one Read demanded: PROGRESS.md |
| Query-time recall | UserPromptSubmit, when `core.recall.is_query_like` | `core.recall.select_recalls` → `render_recall_block` | `RECALL_MAX_ROWS` 3, `RECALL_BUDGET_CHARS` 1 200, floor `RECALL_MIN_RELEVANCE` 0.45 (overlap coefficient), prompt ≥ `RECALL_MIN_PROMPT_CHARS` 12 | session-scoped: skips `shown_ids` of this session's inject manifest and the last 200 ids in `.last_recall.json` (`core.recall.RECALL_MANIFEST`); zero bytes when nothing clears the floor |
| Parked plan advisory | UserPromptSubmit, once | `user_prompt` reads the `core.plan.BLOCK_MARKER_PREFIX` marker Stop wrote | one line | cleared on print |
| Refusal | Stop, on `blocking_reasons` | `core.plan.render_block_reason` | one JSON document | — |

`source` shapes the SessionStart injection (`session_start._injection_mode`):
`startup` and `clear` build every layer; `resume` and `fork` restate the
directive ledger and nothing else (no manifest write, no injection count — the
startup context is still in the conversation); `compact` rebuilds every layer
and demands no ack. The inject manifest `.ccm/.last_inject.json` records
`source`, `ack_demanded`, `progress_layer`, `shown_ids`, `topic_covered_ids` and
`directive_slugs`; `/cc-mem inject-show` prints it and `/cc-mem inject-usage`
measures delivery (free) and, with `--judge`, use (billed, tri-state — `unknown`
is never rendered as `unused`).

## Consolidation, cleanup and the workers

| Trigger | Where | Fires when | Guarded by |
|---|---|---|---|
| Backpressure | Stop probe → `consolidate_async.py --cwd` detached | `core.consolidate.consolidation_backlog`: `BACKLOG_ROWS` (50) new rows past the marker's `last_memory_id`, or `BACKLOG_DAYS` (7) with at least 10 new rows | `.consolidation.kick` cooldown `_CONSOLIDATE_KICK_COOLDOWN_S` (600 s, fails CLOSED), a live `.consolidation.lock` younger than `STALE_LOCK_S` (360 s) |
| Interval | PreCompact async leg | every `consolidation.auto_interval_sessions` sessions (`config.json`, default 5) | the same lock; `BudgetGate` finishes by `total_s - safety_s` |
| Idle reorg | Stop, in-process, no LLM | every `IDLE_INTERVAL_TURNS` (5) turns | defers while a consolidation lock is live |
| Manual | `/cc-mem consolidate` | on demand | writes the same marker (`write_consolidation_marker`, the one writer) |
| Observer | Stop → `stop.py --observe` detached | a credential, ≥ `_MIN_OBS_FOR_EVAL` (3) observations above the cursor, no backoff | `.observer.lock` (`_OBSERVER_STALE_LOCK_S` 60 s); feeds `_OBS_FED_PER_STOP` (20) rows; advances the cursor only after the call returned |
| Retroactive save | SessionStart → `session_start.py --retro` detached | `_retro_candidates` by `stat()` alone (never on resume/fork), a credential, no backoff | `.retro.lock` (`_RETRO_STALE_LOCK_S` 60 s); `_RETRO_DEADLINE_S` 13 s |

A failed model call writes `.ccm/.llm_backoff.json` through
`core.auth.note_llm_failure` alone (60 s, doubling to 1 800 s; cleared by the
first success); every worker reads `core.auth.llm_backoff` before calling. A
run WITHOUT a credential records which LLM stages it skipped (`llm_stages` in
`.last_consolidation.json`): the SessionStart footer names them and `/cc-mem
status` prints the last run, the backlog and whether a run is due.
`deep_dedup` remembers judged groups (`skip_signatures`) so it converges; MERGE
and nomination cross categories at `HIGH_SIM`; every snapshot verdict writes
through `archive_if_unchanged`.

Per-project state lives in `.ccm/` (`core/layout.MEMORY_DIRNAME`, resolved by
`core.roots.project_root`; `/cc-mem paths` prints every file). `.ccm/.gitignore`
hides everything but PROGRESS.md, PLAN.md and MEMORY.md; a pre-v2.13.0
`memory/` is renamed once, only when positively identified as ours
(`core.layout.is_ccm_dir`), never through a link.

## Development guidelines

- **Pure stdlib only at runtime.** No pip dependencies of any kind; PyInstaller
  is build-time only. The rule is stdlib-only, not a closed whitelist.
- **Hook safety > anything else.** A broken hook can hang Claude Code itself.
- **SQL safety.** Parameterised statements only; never format SQL.
- **One resolver each.** Credentials through `core.auth.get_api_key()`; paths
  compared through `core.layout.canonical_path` / `same_path`; the state
  directory through `core.layout.memory_dir` (write) / `find_memory_dir`
  (read); markers through `core.markers`; similarity through `core.textsim`;
  Claude-visible text through `core.prompts`; extraction through
  `llm.parse.build_extraction_prompt` + `normalize_memories`; the guardian
  through `core.plan.guardian_verdict`; the lock through
  `consolidate_async._acquire_lock`. A second spelling of any of these is a
  defect, and `tests/smoke_test.py` greps for several of them.
- **Plugin-agnostic.** No project-specific vocabulary in `extractor.py` or
  `consolidate.py`.
- **Stored content is never interpolated raw** into anything Claude reads:
  `clean_for_storage` on the write path, `neutralize_*` on every render path.
- **Read files before modifying them**; a `try/except: pass` needs a `# why:`.
- **Do not enumerate a set in prose.** Bind the count (`six hooks
  <!--ce:hooks-->`, `:subset`, `:asof`); `tools/contracts.py` computes the sets.
- **A new rule gets an `INVARIANTS.md` entry** in the commit that adds its gate,
  and a contract change goes into `docs/CONTRACTS.md` (both languages).
- **CHANGELOG entries are dated records**: never swept to a new name, and a
  dated measurement keeps its number.

## Data & safety rules

- Never delete or overwrite `memory.db` or archived sessions without asking.
- Never fabricate extraction results or memory content.
- Hooks must never block Claude Code — always exit cleanly.
- `<private>…</private>` is stripped on every ingress (`core.privacy`, a
  left-to-right scan that fails CLOSED on a dangling tag; tags match
  case-insensitively), and a private row never reaches an Anthropic request.
- Tag memories with their extraction method. The complete set any code emits,
  verified by grepping every `"tags"` literal under `cc_memory/`:
  `["observer","realtime"]` (the observer worker), `["mcp"]`, `["manual"]`
  (`cli/mem.py`), `["manual","dashboard"]` (Add Memory), `[method,"manual"]`
  with `method` `"llm"` or `"regex"`, `["regex","manual"]` and
  `["metric","manual"]` (Save Session), `["auto-detected","init"]`
  (new-project init), `["web"]`, `["llm-dedup","merged"]`
  (`core/consolidate.py`). The writer appends `"merged"` / `"supersedes"`.
  The PreCompact LLM path sets no `tags` key, so those rows store `[]`.
- `.ccm/PROGRESS.md`, `.ccm/PLAN.md` and `.ccm/MEMORY.md` are generated
  artifacts written through `core.atomic.write_atomic`. Edit the SQL source of
  truth, never the file.

## Tests

**TWELVE release gates, and there is ONE command that runs them:**

```bash
python tests/run_gates.py          # all 12; prints a table, exits nonzero on any red
python tests/run_gates.py --list   # what each gate checks
python tests/run_gates.py --fast   # skips the 2 slow suites — NOT a release run
```

Twelve = five suites, four dev checkers, `compileall`, a `tomllib` parse of
`pyproject.toml`, and version-site agreement. The count is derived:
`run_gates.py:GATES` is the single list, `tests/smoke_test.py` asserts this
section opens with the derived count word, that every suite and checker on
disk is on the list, that the list runs each one, and that every other
reader-facing gate count (`README.md`, `README.zh.md`, `CONTRIBUTING.md`, the
PR template) agrees in both languages. **Run the runner, not the list.**

| Gate | Asks |
|---|---|
| `tests/smoke_test.py` | end-to-end: migrations, `upsert_smart` decisions, PROGRESS.md full rewrite and fill-only-empty, the plan anchor, identity and the state directory, the workers with their spawns stubbed, injection layers by `source`, consolidation C1-C4, and the three doc gates below |
| `tests/test_plan_carryover.py` | the carryover gate and PROGRESS.md §4 reading the store |
| `tests/test_surfaces.py` | installer, MCP stdio, web viewer, the opt-out across all six hooks <!--ce:hooks-->, project-root anchoring from a subdirectory, the CLI boundary, the dashboard's pure cores |
| `tests/test_directive_enforcement.py` | the directive ledger, Stop refusals and the escape budget, continuation Stops, the backoff, the observer spawn, the parked advisory |
| `tests/test_recall.py` | a stored fact is findable by a substring in either language; hostile queries; the recall channel with a control that EMITS beside every zero-bytes assertion |
| `tools/i18n_check.py` | every `.zh.md` is bound to the normalised hash of its English source and its own body |
| `tools/citation_check.py` | every `file.py:LINE` citation in `TRACKED` resolves to its symbol or at least its bounds; verbatim regions are never scanned or fixed |
| `tools/doc_claims.py` | every bound count in the tracked markdown, `cc_memory/config.json` and the package's docstrings matches `tools/contracts.py` |
| `tools/doc_coverage.py` | every schema table, `ALTER`-added column, MCP tool and config key is NAMED by its owning document in both languages |

Individually, when you need one gate's own output:

```bash
python tests/smoke_test.py                  # "===== ALL SMOKE TESTS PASSED ====="
python tests/test_plan_carryover.py         # "RESULT: N passed, 0 failed"
python tests/test_surfaces.py               # "===== ALL SURFACE TESTS PASSED ====="
python tests/test_directive_enforcement.py  # "ALL CHECKS PASSED"
python tests/test_recall.py                 # "===== ALL RECALL TESTS PASSED ====="
python tools/i18n_check.py                  # "OK (no drift)", exit 0
python tools/citation_check.py [--fix]      # "0 unchecked, 0 stale", exit 0
python tools/doc_claims.py                  # "0 problem(s)", exit 0
python tools/doc_coverage.py                # "0 gap(s)", exit 0
python tools/contracts.py                   # not a gate: what each set contains today
python tools/falsify_fixes.py --anchors     # not in run_gates: a CI step, run before tagging
python tools/falsify_fixes.py --case <id>   # revert one fix on a COPY; the gate must go RED
```

Rules of evidence: a new check is kept only after its `--case` was driven RED
(a GREEN case indicts the check, not the case); `--anchors` proves the
register itself has not rotted and is run before every tag; `gate_baseline`
runs each gate once on an untouched copy and reports UNSOUND rather than RED
when that baseline is red. Every suite redirects `USERPROFILE`/`HOME` and
`TMPDIR`/`TEMP`/`TMP` into a `tempfile` sandbox before importing the package,
asserts `Path.home()` moved, and tears the sandbox down in a `finally`; every
subprocess capture passes `encoding="utf-8"`. When a behaviour is added to
`memory_writer`, `progress`, `extractor.load_transcript_window`,
`session_start` or the workers, add the assertion block and the case in the
same commit, and the `INVARIANTS.md` entry beside them.

Documentation is gated the same way. A `file.py:LINE` citation is repaired
with `python tools/citation_check.py --fix` when a cited file grows; an English
edit to a doc with a `.zh.md` sibling is translated and then re-stamped
(`python tools/i18n_check.py --emit-marker <doc>`, which refuses an
untranslated re-stamp); a count of hooks, renderers or render paths is bound
or not written; `CLAUDE.md` stays under 45 KB.

## Interpreter requirement

`hooks/hooks.json` invokes `python3`. On Windows the python.org installer ships
`python.exe` and the `py.exe` launcher but no `python3.exe` by default — install
with "Add Python to PATH", or alias `python3 -> python`, before installing the
plugin. Otherwise hooks fail silently (logged, no error UI).

## Build, release, sync

```bash
pip install pyinstaller
python scripts/build_exe.py        # dist/cc-memory-installer.exe + dist/cc-memory-dashboard.exe
python scripts/bench_hooks.py      # per-hook cost and injection analysis (not a gate)
```

**Release binaries come from CI, never from this machine.**
`.github/workflows/release.yml` runs on a `v*` tag push: refuses a tag that
disagrees with `core/version.py`; runs `python tests/run_gates.py` on the tagged
commit; builds both exes and RUNS them (a real `--cli` install and `--uninstall`
against a sandboxed home, exit 2 on an unknown flag, `--help` exits 0);
publishes the Release with the CHANGELOG section as body
(`scripts/release_notes.py`, whose title is the section's first `###`).
Procedure: bump the five version sites (`core/version.py`, `pyproject.toml`,
`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
`cc_memory/config.json` — `run_gates.py` asserts they agree) plus this file's
version line, the README badges and `SECURITY.md`'s supported minor → write the
CHANGELOG entry → gates green → `python tools/falsify_fixes.py --anchors` green →
commit → `git tag vX.Y.Z` → `git push origin main vX.Y.Z` → watch `gates.yml`
and `release.yml` → verify the attached assets by downloading them. A moved tag
is a rewritten history.

**Sync.** On this machine Claude Code runs cc-memory from the git working tree
(directory marketplace in `~/.claude/settings.json`; `hooks/hooks.json` uses
`${CLAUDE_PLUGIN_ROOT}`), so editing `cc_memory/**.py` updates the live hooks on
the next session — no copy step; `~/.claude/hooks/cc-memory/` holds only
`logs/`. Another machine without a checkout uses `cc-memory-installer.exe` from
the GitHub Release, which lays the package FLAT under
`~/.claude/hooks/cc-memory/` (no `cc_memory/` segment), copies the surfaces into
`~/.claude/{commands,agents,skills}` and registers the hooks in
`settings.json`. Every probe for an install accepts both shapes.

## See also

- `INVARIANTS.md` — the numbered rules, with their gates and falsification cases
- `CHANGELOG.md` — version history; § [2.16.0] is this version
- `docs/ARCHITECTURE.md` — module map, data flow, install layouts, the i18n convention
- `docs/CONTRACTS.md` — the three contracts in specification form
- `commands/cc-mem.md` — every `/cc-mem` subcommand
