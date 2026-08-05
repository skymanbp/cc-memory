# CLAUDE.md — Project Instructions for Claude Code

## Project: cc-memory

**Claude Code persistent memory plugin (v2.4.3)** — anti-patch reconcile-on-write
+ LLM-judged semantic de-duplication, forced PROGRESS.md handoff with
per-session annotation, live PLAN.md anchor with plan-refiner / plan-guardian
subagents + mandatory carryover gate, bounded transcript reads, injection
observability, FTS5 search, AI-judged extraction with Haiku (optional local
Ollama fallback).

- **Language**: Python 3.8+ (pure stdlib, zero pip dependencies at runtime)
- **Version**: 2.4.3
- **License**: MIT
- **Platform**: Windows-primary, cross-platform compatible (Tkinter required for GUI)

## What changed in v2.4.3 (over v2.4.2)

**Shipped surfaces were broken, and the docs were lying.** A fact-check of every
documentation file against the code turned up three dead entry points:

1. **`/cc-mem` did not work at all.** `commands/cc-mem.md` used `$ARGS`; the
   placeholder Claude Code substitutes is `$ARGUMENTS`. Unsubstituted it expands
   to empty, and `mem.py`'s subparser is `required=True`, so every invocation
   aborted.
2. **`save-memories` was dead on any non-legacy install.** It hardcoded
   `~/.claude/hooks/cc-memory/cc_memory`, which on a marketplace install holds
   only `logs/` → `ModuleNotFoundError`.
3. **Install-layout probes were inverted repo-wide.** `ui/installer.py` copies
   subpackages to `TARGET_DIR/<subdir>/` — a **flat** tree with no `cc_memory/`
   segment — but `ccm-load`, `commands/cc-mem.md`, `cli/mem.py`'s legacy
   detection and the README all probed for the **nested** form. Every standalone
   install was therefore invisible to all of them. All four now probe both.

**Docs consolidated 5 → 2.** `docs/MEMORY_RULES.md`, `HANDOFF_PROTOCOL.md` and
`PLAN_PROTOCOL.md` merged into **`docs/CONTRACTS.md`**; `I18N.md` merged into
**`docs/ARCHITECTURE.md`** as §9. 79 citations across 18 files were repointed to
the new files + anchors. `docs/ARCHITECTURE.zh.md` is the Chinese translation
(the old `I18N.md` switcher pointed at a file that never existed).

The v2.4.0 carryover gate is now **documented in prose for the first time** — it
shipped with zero documentation outside its commit message.

Skills stay **two, and orthogonal**: `/ccm-load` owns activation + bootstrap
(the global plugin-enablement check exists nowhere else), `/save-memories` owns
the manual write path. `ccm-load` no longer claims to run the `/cc-mem status`
health check — it never did; it only printed DB counts.

## What changed in v2.4.2 (over v2.4.1)

**Hook survivability. Transcript reads are now bounded.** The `PreCompact` sync
leg was being killed mid-write on long-lived projects, and its LLM extraction
had been reading the wrong end of the transcript. Same root cause: the hook
loaded the ENTIRE `.jsonl` before using ~12 KB of it.

1. **`core.extractor.load_transcript_window`** — bounded head+tail read (40
   records + 32 MiB). A 2.11 GiB transcript parsed at ~25 MiB/s = ~88s of a
   120s budget before any work; now 1.66s, full hook 14.33s. `msg_count` stays
   exact via a raw record scan (~1 GiB/s). The old unbounded `load_transcript`
   survives for `ui/dashboard.py` only — **never call it from a hook.**
2. **Summaries fill from the NEWEST record backwards.** Filling from the oldest
   exhausted the 12k budget after 329 of ~585,000 records, pinning extraction
   to a session's opening hours. Fixed in both `pre_compact` and
   `session_start._summarize_transcript`.
3. **Killed runs are visible.** `memory/.pre_compact_attempt.json` is written
   at entry and removed only on completion, so a surviving marker proves the
   last attempt died (a timeout kill runs no `except` block, which is why the
   failure used to leave no trace at all). `.last_save.json` gained `trigger`,
   making AUTO compactions distinguishable from "never ran".
4. **`RuntimeError` added to the extraction `except` tuple** — a total LLM
   outage no longer skips the PROGRESS.md rewrite.
5. **`memory/.gitignore` migrates existing installs** via
   `core.progress.ensure_memory_gitignore`. Three call sites import it
   (`hooks/pre_compact.py`, `hooks/user_prompt.py`, `ui/dashboard.py`); two
   more keep DELIBERATE literal copies because they cannot import the package
   (`ui/installer.py` is a stdlib-only bootstrap, `skills/ccm-load/SKILL.md`
   is an inline script) — those two must be hand-synced. Previously every new
   runtime artifact leaked forever.
6. **`pyproject.toml` BOM stripped** — `tomllib` could not parse it, so no PEP
   517 frontend could build or install the package since v2.4.0.

## What changed in v2.4.1 (over v2.4.0)

Carryover auto-carry matches **bare titles** too. A step whose title was
unchanged but whose `notes` grew long fell below the trigram-Jaccard threshold,
so the v2.4.0 gate refused a legitimate in-place plan update.

## What changed in v2.4.0 (over v2.3.4)

**Mandatory plan carryover gate.** `plan_active` is a single-row slot, so every
`plan-set --from-refiner` replaced the plan wholesale and unfinished steps
vanished unaccounted. Replacement now requires each unfinished step to be
auto-carried (trigram-Jaccard ≥ 0.5) or explicitly dispositioned
(`{old_title, action, reason}`); `plan-clear` refuses without `--reason`; every
outgoing plan is archived to `memory/.plan_history/`. **No force flag, by
design.** See `tests/test_plan_carryover.py`.

## What changed in v2.3.4 (over v2.3.3)

**Anthropic auth fall-through + opt-in local fallback — no schema change.**

1. **`core.auth.get_api_candidates()`** returns (key, source, wire) candidates
   in order (env key → Claude Code OAuth token). `get_api_key()` keeps its
   single-key back-compat surface (incl. the `oauth_expired` signal).
2. **`llm.ccl_backend.call_llm`** iterates candidates (bounded at 2) with the
   correct wire per credential: `sk-ant-oat…` → `Authorization: Bearer` +
   `anthropic-beta: oauth-2025-04-20`; `sk-ant-api…` → `x-api-key`. A dead env
   key no longer blackholes the healthy subscription token.
3. **Ollama fallback opt-in**: `config.json` `ccl.enabled` (default false).
   The error raised when everything fails now aggregates per-leg reasons.
4. `core.consolidate._worst_call_cost` = `2*haiku + fallback` (BudgetGate
   deadline guarantee stays honest with fall-through).

## What changed in v2.3.3 (over v2.3.2)

**Documentation multilingual version-control — docs + version metadata only, no
runtime behavior change.** English is the canonical skeleton; Chinese docs are
drift-tracked `*.zh.md` siblings (`README.zh.md` first). Each `.zh.md` carries a
line-1 HTML-comment marker binding it to a *normalized-sha256* of its English
source (CRLF/BOM/trailing-whitespace-immune, so the digest is stable across
platforms). Drift is decided **solely** by that hash; `version`/`translated`
fields are informational, so a version bump never mass-flags translations.

1. **`tools/i18n_check.py`** — pure-stdlib checker (dev/CI tool, deliberately
   NOT packaged into the installer or exes). States → labels/exit:
   IN-SYNC `[OK]` 0 · MISSING-TRANSLATION `[WARN]` 0 · STALE `[STALE]` nonzero ·
   ORPHAN `[FAIL]` nonzero · NO-MARKER `[FAIL]` nonzero. `--emit-marker <doc>`
   regenerates a marker after an English source changes.
2. **Smoke-test drift gate.** `tests/smoke_test.py` asserts no STALE/ORPHAN/
   NO-MARKER docs and that `README.zh.md`'s marker digest matches the live
   `hash_source(README.md)` — so editing an English doc without refreshing its
   translation turns the suite red.
3. **`docs/ARCHITECTURE.md#9-documentation-language-convention-i18n`** — the convention (3-tier language model, naming, switcher,
   marker + normalization recipe, add/update workflows). Tier 3 = memory content
   is language-agnostic; the bilingual detectors in `extractor.py` /
   `user_prompt.py` / `session_start.py` are intentional and carry `# i18n Tier 3`
   guard comments so a future refactor can't silently reduce them to English.

## What changed in v2.3.2 (over v2.3.1)

**Consolidation moved off the blocking compaction path.** v2.3.1 raised the
PreCompact timeout 45→120s, but the every-Nth-session LLM consolidation could
still overrun on large DBs (one ungated LLM stage + a dishonest budget cost
model), so `Compacted PreCompact ... failed: Hook cancelled` still surfaced.
v2.3.2 fixes the root cause:

1. **`PreCompact` is now two hooks.** The sync leg (`pre_compact.py`) keeps only
   the fast, handoff-critical work (extraction + PROGRESS.md, ~1-5s). A new
   sibling `async` leg (`hooks/consolidate_async.py`, `"async": true`,
   timeout 300s) runs consolidation in the background so Claude Code never
   waits on it — a slow run can no longer surface as a compaction failure.
2. **Consolidation cadence is now an interval marker + lock**, not a fragile
   `session_count % N` check. `memory/.last_consolidation.json` records the
   count at the last run; a lock file prevents overlapping workers. Race-immune
   against the concurrent sync leg (WAL + busy_timeout make it safe).
3. **Honest budget cost model.** `consolidate_topics` is now budget-gated (it
   was the one ungated LLM loop), and `call_llm` takes a bounded
   `fallback_timeout` so a budgeted call's worst-case wall-clock is known up
   front. The BudgetGate therefore GUARANTEES the run finishes by
   `total_s - safety_s` (232s) < the 300s async timeout — never killed mid-write.

## What's new in v2.2 (over v2.1)

1. **Live PLAN.md anchor.** `memory/PLAN.md` is a new generated artifact that
   captures the project's current goal + step status. ExitPlanMode output
   (or user-supplied `/cc-mem plan-set` text) lands in the `plan_active`
   SQL table; TodoWrite events sync step statuses mechanically; sensitive
   Bash patterns (`git push`, `rm -rf`, deploys) flag drift.
2. **Two plugin-shipped subagents.** `agents/plan-refiner.md` normalises a
   raw plan into a structured JSON schema; `agents/plan-guardian.md` does a
   read-only ≤150-word drift check on demand. Stop hook emits an advisory
   status line when guardian thresholds trip (default: 8 turns OR 12 edits).
3. **`/cc-mem dashboard` + 7 new `/cc-mem plan-*` subcommands.** The GUI
   launcher auto-resolves its path under both marketplace and standalone
   installs. Plan CLI:  `plan-status`, `plan-show`, `plan-set
   (--raw|--raw-file|--from-refiner)`, `plan-check`, `plan-replan`,
   `plan-clear`.
4. **Skill consolidation.** `skills/mem-init` and `skills/mem-status` removed
   (subsets of `/ccm-load` + `/cc-mem status`). `skills/ccm-load` rewritten
   to auto-resolve plugin root instead of the maintainer's hardcoded path.

See `docs/CONTRACTS.md#plan-contract` for the full v2.2 contract.

## What changed in v2.1 (over v2.0)

1. **Subpackage layout.** Source is split into
   `cc_memory/{core,hooks,llm,cli,mcp,ui}/`. No more 22-file flat directory.
2. **Anti-patch writes.** `llm.memory_writer.upsert_smart` is the single
   entry for any save path. It MERGES / SUPERSEDES / INSERTS based on
   similarity — no stacking of duplicates. See `docs/CONTRACTS.md#anti-patch-contract`.
3. **Forced handoff.** `memory/PROGRESS.md` (new in v2.1) replaces
   `SESSION_HANDOFF.md`. SessionStart emits a `<system-reminder>` block that
   directs the next Claude to `Read memory/PROGRESS.md` BEFORE responding.
   See `docs/CONTRACTS.md#handoff-contract`.
4. **Auto-fresh MEMORY.md.** Regenerated after every batch upsert.
5. **Idle reorg.** Stop hook runs lightweight cleanup every 5 turns (no LLM).
6. **One installer, one skills location, one version number** across all files.

## Repository layout

```
cc-memory/
├── .claude-plugin/
│   ├── plugin.json              ← Plugin manifest (v2.4.2)
│   └── marketplace.json         ← /plugin marketplace add entry
├── hooks/hooks.json             ← 6 hook commands across 5 events
├── skills/                      ← THE canonical skills location
│   ├── ccm-load/SKILL.md        (one-shot end-to-end activation + init + status)
│   └── save-memories/SKILL.md   (routes through memory_writer)
├── agents/                      ← Plugin-shipped subagents (v2.2+)
│   ├── plan-refiner.md          (raw plan → structured JSON, one-shot)
│   └── plan-guardian.md         (drift check, read-only, ≤150 words)
├── commands/cc-mem.md           ← /cc-mem slash command
├── docs/
│   ├── ARCHITECTURE.md
│   ├── MEMORY_RULES.md          ← Anti-patch contract
│   ├── HANDOFF_PROTOCOL.md      ← PROGRESS.md spec
│   ├── PLAN_PROTOCOL.md         ← PLAN.md spec (live plan anchor, v2.2)
│   └── I18N.md                  ← docs multilingual + drift contract (v2.3.3)
├── README.zh.md                 ← drift-tracked Chinese translation
├── tools/i18n_check.py          ← translation drift checker (dev/CI only)
├── cc_memory/
│   ├── __init__.py              (version 2.4.2)
│   ├── config.json
│   ├── core/                    db, extractor, consolidate, idle, progress,
│   │                            plan, privacy, modes, auth, logger,
│   │                            encoding_setup
│   ├── hooks/                   post_tool_use, pre_compact, consolidate_async,
│   │                            session_start, stop, user_prompt
│   ├── llm/                     ccl_backend, memory_writer
│   ├── cli/                     mem, plan
│   ├── mcp/                     server
│   └── ui/                      installer, dashboard, web_viewer
├── tests/
│   ├── smoke_test.py            end-to-end anti-patch + PROGRESS.md +
│   │                            tier-3 transcript + layout-inspector +
│   │                            live-plan + i18n gate + bounded-window tests
│   └── test_plan_carryover.py   carryover gate (v2.4.0+), 14 checks
├── build_exe.py
├── pyproject.toml
├── README.md
├── CLAUDE.md                    ← This file
├── CHANGELOG.md
└── LICENSE
```

## Hooks (6)

Declared in `hooks/hooks.json`. A **marketplace / dev-checkout** install is
discovered via `enabledPlugins` + `extraKnownMarketplaces` in
`~/.claude/settings.json` → the plugin manifest → `hooks/hooks.json`; the
`hooks` key of `settings.json` stays untouched. Only the **standalone**
installer (`ui/installer.py:_merge_into_settings`) writes hook entries there.
`PreCompact` fires TWO command hooks (v2.3.2): a blocking sync leg + a
background `async` leg.

| Hook | Entry | Timeout | Purpose |
|------|-------|---------|---------|
| `PreCompact` (sync) | `cc_memory/hooks/pre_compact.py` | 120s | LLM extract → memory_writer.upsert_batch → FULL-REWRITE PROGRESS.md → archive (fast, ~1-5s) |
| `PreCompact` (async) | `cc_memory/hooks/consolidate_async.py` | 300s, `async:true` | Background consolidation every N sessions (interval marker + lock, budget-gated) — off the blocking path |
| `SessionStart` | `cc_memory/hooks/session_start.py` | 15s | Inject layered context + FORCED `<system-reminder>` to Read PROGRESS.md |
| `Stop` | `cc_memory/hooks/stop.py` | 22s | Observer (Haiku) + per-turn PROGRESS.md patch + idle reorg every 5 turns |
| `PostToolUse` | `cc_memory/hooks/post_tool_use.py` | 8s | Insert observation row (no LLM) |
| `UserPromptSubmit` | `cc_memory/hooks/user_prompt.py` | 8s | Auto-init memory/ + turn count + seed `progress.current_request` on turn 1 |

Hook contract (NEVER violate):
- Hooks must NEVER write to stderr (Claude Code shows stderr as error UI).
  Use `core.logger.get_logger(...)`; it writes to `~/.claude/hooks/cc-memory/logs/`.
- Hooks must NEVER raise an unhandled exception. Always `sys.exit(0)`.
- Each hook's stdout has a specific role:
  - `SessionStart` stdout → injected context (read by Claude)
  - `Stop` stdout → status line (read by Claude)
  - `PreCompact` (sync) stdout → ONE status line (shows in next session's compacted context)
  - `PreCompact` (async) / `PostToolUse` / `UserPromptSubmit` stdout → empty

## Database schema (11 tables)

Defined in `cc_memory/core/db.py`. See `docs/ARCHITECTURE.md` for full diagram.

- `projects`, `sessions`, `memories`, `topics`, `keywords`, `plans`
- `observations` (PostToolUse events, cleaned after extraction)
- `session_summaries` (6-field structured summary per session)
- `progress` (v2.1: single row per project, SOT for PROGRESS.md)
- `plan_active` (NEW in v2.2: single row per project, SOT for PLAN.md)
- `_migrations` (tracks applied migrations)

Key columns added in v2.1:
- `memories.supersedes_id` — forms the update chain (anti-patch contract)
- `memories.content_hash` — sha256[:16] of normalized content for cheap dedup

## Anti-patch contract

> Every memory save path routes through `llm.memory_writer.upsert_smart`,
> which MERGES in place, SUPERSEDES with a chain link, or INSERTS based on
> trigram-Jaccard similarity. Never call `db.insert_memory` directly from a
> caller path. See `docs/CONTRACTS.md#anti-patch-contract` for the full spec.

All save paths route through the writer (no remaining direct callers of
`db.insert_memory` outside the writer itself):
- `hooks/pre_compact.py` ✓
- `hooks/stop.py` (observer) ✓
- `cli/mem.py add` ✓
- `mcp/server.py memory_add` ✓
- `skills/save-memories/SKILL.md` ✓ (calls `upsert_batch`)
- `ui/dashboard.py` "Add Memory" dialog ✓ (`upsert_smart`)
- `ui/dashboard.py` "Save Session" ✓ (`upsert_batch`)
- `ui/dashboard.py` new-project init ✓ (`upsert_batch`)
- `ui/web_viewer.py` POST /api/memory ✓ (`upsert_smart`)
- `hooks/session_start.py` retroactive save ✓ (`upsert_batch`)

The single `db.insert_memory` call outside the writer is inside
`MemoryDB.supersede_memory` (`core/db.py`) — that is the writer's own
SUPERSEDE implementation, not a caller path.

## Forced handoff contract

> `memory/PROGRESS.md` is the single source of truth for session handoff.
> It is ALWAYS full-rewritten from the `progress` SQL row, never appended.
> SessionStart emits a `<system-reminder>` requiring the next Claude to Read
> it BEFORE responding. See `docs/CONTRACTS.md#handoff-contract`.

The `progress` row has 11 user-facing fields (`current_request`, `status_*`,
`open_todos`, `plan`, `critical_context`, `files_touched`, `transcript_ptr`,
`updated_at`, `trigger_type`). It is updated by four paths:
- `PreCompact` does a full overwrite (`upsert_progress`).
- `Stop` patches `files_touched` per turn (`patch_progress`).
- `UserPromptSubmit` patches `current_request` on turn 1 (`patch_progress`).
- `SessionStart` fills ONLY still-empty fields via
  `_refresh_progress_row` (`patch_progress`, `trigger_type="session_start_refresh"`).
  Fill-only-empty by contract — it must never overwrite a populated field.

`SESSION_HANDOFF.md` from v2.0 is renamed to `SESSION_HANDOFF.md.v2.bak` on
first PreCompact under v2.1 (one-shot migration in `core/progress.py`).

## Live plan anchor (v2.2)

> `memory/PLAN.md` is the single source of truth for the current goal +
> step status. Distinct from PROGRESS.md (session handoff) — PLAN.md
> outlives sessions. See `docs/CONTRACTS.md#plan-contract` for the full spec.

The `plan_active` table (one row per project) backs PLAN.md. Lifecycle:

- `PostToolUse` captures `ExitPlanMode` → `plan_active.raw`, sets
  `needs_refine = 1`.
- A **`plan-refiner`** subagent (shipped in `agents/`) is invoked by the
  main Claude on the Stop-hook nudge; it outputs structured JSON which is
  written back via `/cc-mem plan-set --from-refiner`.
- `PostToolUse` on `TodoWrite` mechanically syncs todos → step statuses
  via trigram-Jaccard match (no LLM). On `Edit`/`Write`/`MultiEdit`, it
  bumps `edits_since_last_guardian`.
- `Stop` hook emits a single status line when guardian thresholds are
  crossed (default: 8 turns OR 12 edits). Main Claude responds by
  invoking the **`plan-guardian`** subagent (also in `agents/`), then
  `/cc-mem plan-check` to reset counters.

Hooks never spawn subagents themselves — they only nudge. The plugin's
two subagents (`agents/plan-refiner.md`, `agents/plan-guardian.md`) live
in the plugin so they're discoverable under both marketplace and
standalone installs.

## Development guidelines

- **Pure stdlib only at runtime.** No pip dependencies of any kind;
  PyInstaller is build-time only. The rule is *stdlib-only*, not a closed
  whitelist — beyond the obvious `sqlite3, json, pathlib, urllib, datetime,
  subprocess, tkinter, time, hashlib, re, http.server`, live hook paths also
  use `os, sys, tempfile, argparse, collections, typing, threading,
  traceback, shutil, platform, textwrap, unicodedata, webbrowser,
  __future__`. All stdlib, all fine.
- **Hook safety > anything else.** A broken hook can hang or break Claude
  Code itself. `try: ... except Exception: pass` with a `# why: ...` comment
  is appropriate in hook code. Log to file via `core.logger`.
- **SQL safety.** All queries use parameterized statements. Never use string
  formatting for SQL.
- **OAuth auto-detection.** Always use `core.auth.get_api_key()` for API key
  resolution. Never hardcode key reading.
- **Anti-patch.** Never call `db.insert_memory` directly from a caller path
  — use `llm.memory_writer.upsert_smart` or `upsert_batch`. See
  `docs/CONTRACTS.md#anti-patch-contract`.
- **Plugin-agnostic.** Don't add project-specific keywords (e.g. ML/astro
  vocab) to `extractor.py` or `consolidate.py`. Those were removed in v2.1
  for a reason.
- Read files before modifying them; respect the cc-enslaver-style discipline.

## Data & safety rules

- Never delete or overwrite `memory.db` or archived sessions without asking.
- Never fabricate extraction results or memory content.
- Hooks must never block Claude Code — always exit cleanly (`sys.exit(0)`).
- Tag memories with their extraction method for traceability. Tags actually
  emitted today: `["observer","realtime"]` (`hooks/stop.py`), `["mcp"]`
  (`mcp/server.py`), `["manual"]` (`cli/mem.py`), `["manual","dashboard"]`
  and `["auto-detected","init"]` (`ui/dashboard.py`), `["web"]`
  (`ui/web_viewer.py`), `["llm-dedup","merged"]` (`core/consolidate.py`);
  the writer appends `"merged"` / `"supersedes"` on those paths. NOTE: the
  PreCompact LLM path sets no `tags` key, so those rows store `[]` — do not
  document a `["llm","auto"]` tag that no code emits.
- `memory/PROGRESS.md` and `memory/MEMORY.md` are generated artifacts. Edit
  the SQL source of truth (`progress` table for PROGRESS.md, `memories`/
  `topics`/`keywords` for MEMORY.md) instead.

## Tests

**Two suites. BOTH are release gates — run both.**

`tests/smoke_test.py` is the canonical end-to-end check. In a throwaway temp
project it exercises: v3/v6 migrations, `upsert_smart` decisions
(INSERT/MERGE/SUPERSEDE/SKIP), the `progress` row + `PROGRESS.md`
full-rewrite, the fill-only-empty refresh contract, last-wins TodoWrite
extraction, the tier-3 transcript fallback, the legacy `SESSION_HANDOFF.md`
migration, the layout inspector, the v2.3.2 async consolidation lock/marker,
the v2.3.3 i18n drift gate, and the v2.4.2 bounded-window / summary-direction
/ killed-run-visibility contracts.

`tests/test_plan_carryover.py` covers the v2.4.0 carryover gate (14 checks) —
the only coverage of that feature.

```bash
python tests/smoke_test.py
# expect: [OK] lines ending with "===== ALL SMOKE TESTS PASSED ====="
python tests/test_plan_carryover.py
# expect: "RESULT: 14 passed, 0 failed"
```

No pytest / pip dependencies — both are stdlib scripts and reflect the runtime
contract (pure stdlib, see Development guidelines below). When you add a
behavior to `memory_writer`, `progress`, `extractor.load_transcript_window`, or
`session_start._refresh_progress_row`, add a corresponding assertion block.

## Interpreter requirement

`hooks/hooks.json` invokes `python3`. On Linux/macOS this is the standard
Python 3 binary. On Windows the python.org installer ships `python.exe`
plus the `py.exe` launcher but NOT `python3.exe` by default — install
"Add Python to PATH" + tick "py launcher", or symlink/alias `python3 ->
python` before installing the plugin. Otherwise hooks will fail silently
(logged to `~/.claude/hooks/cc-memory/logs/`, but Claude Code shows no
error UI for missing-command hooks).

## Build

```bash
pip install pyinstaller
python build_exe.py
# produces dist/cc-memory-installer.exe + dist/cc-memory-dashboard.exe
```

## Sync protocol

**Since v2.1.1 (marketplace registration), no sync to `~/.claude/hooks/` is
needed for code changes on this machine.** Claude Code discovers cc-memory
via `~/.claude/settings.json`:

```jsonc
"enabledPlugins":       { "cc-memory@cc-memory": true },
"extraKnownMarketplaces": {
  "cc-memory": { "source": { "source": "directory",
                             "path": "D:\\Projects\\cc-memory" } }
}
```

`hooks/hooks.json` uses `${CLAUDE_PLUGIN_ROOT}/cc_memory/hooks/<name>.py`,
which resolves to **the git working tree itself**. Editing
`cc_memory/**.py` here updates the live hooks on the next Claude Code
session — no copy step.

`~/.claude/hooks/cc-memory/` only holds `logs/` now (logger output target).

To deploy to another machine without a git checkout, build
`cc-memory-installer.exe` (see `build_exe.py`). That installer lays code
under `~/.claude/hooks/cc-memory/cc_memory/` and registers hooks the v2.0
way — same package, alternate install path.

## See also

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — full architecture overview
- [docs/CONTRACTS.md#anti-patch-contract](docs/CONTRACTS.md#anti-patch-contract) — anti-patch contract
- [docs/CONTRACTS.md#handoff-contract](docs/CONTRACTS.md#handoff-contract) — PROGRESS.md spec
- [docs/CONTRACTS.md#plan-contract](docs/CONTRACTS.md#plan-contract) — PLAN.md + subagent spec (v2.2)
- [CHANGELOG.md](CHANGELOG.md) — version history
