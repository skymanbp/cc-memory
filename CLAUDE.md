# CLAUDE.md — Project Instructions for Claude Code

## Project: cc-memory

**Claude Code persistent memory plugin (v2.5.3)** — anti-patch reconcile-on-write
+ LLM-judged semantic de-duplication, forced PROGRESS.md handoff with
per-session annotation, live PLAN.md anchor with plan-refiner / plan-guardian
subagents + mandatory carryover gate, bounded transcript reads, injection
observability, FTS5 search, AI-judged extraction with Haiku (optional local
Ollama fallback).

- **Language**: Python 3.8+ (pure stdlib, zero pip dependencies at runtime)
- **Version**: 2.5.3
- **License**: MIT
- **Platform**: Windows-primary, cross-platform compatible (Tkinter required for GUI)

## What changed in v2.5.3 (over v2.5.2)

**v2.5.2's "Known limits" section, cleared.** Detail in `CHANGELOG.md`. The
invariants a future change must not break:

1. **`core/atomic.py:write_atomic` is THE artifact writer.** One
   implementation, consumed by `core/progress.py`, `core/plan.py` and
   `llm/memory_writer.py` under their old private names. Its contract:
   **replace completely, or raise — never truncate, never fall back.** v2.5.2's
   three copies were documented as "deliberate literal twins" and were not:
   two had no retry and fell back to a truncating `write_text`, which was the
   whole residual. Do not re-add a private copy; `smoke_test.py` asserts all
   three names ARE that function and that no module defines `_atomic_write*`.
   PROGRESS.md's writer propagates the raise (it is the handoff contract);
   PLAN.md's and MEMORY.md's catch it and keep the previous COMPLETE file (they
   are projections of already-committed state).

2. **`project_id` is REQUIRED and keyword-only** on `update_plan_status`,
   `delete_plan` and `update_plan_content`. `plans.id` is global to the DB
   file. Do not restore the `=None` default "for compatibility" — all 11 call
   sites already pass it, and the default was the entire hole.

3. **A fail-closed `config.json` must stay VISIBLE.** `core.modes.config_fault()`
   reports why the plugin suspended itself and `hooks/session_start.py` prints
   one line. A project the user genuinely LISTED must stay completely silent —
   §5 of `tests/test_surfaces.py` asserts both halves, and conflating them
   destroys either the opt-out's silence or the accident's diagnosability.

4. **The installer's `settings.json` write is a compare-and-swap.** Read takes a
   digest, write refuses to rename if the file changed, `_merge_into_settings`
   retries the whole merge (bounded by `_MERGE_ATTEMPTS`). Install and uninstall
   both. Do not "simplify" it back to read-then-write.

5. **The installer refuses unknown arguments** (`_KNOWN_FLAGS`). It used to
   ignore them, so `--unistall` performed an install and exited 0.

6. **Verify the exes by RUNNING them.** `scratchpad/verify_exe.py`-style
   install/uninstall against a sandboxed HOME. A PE-header check tells you how
   the binary was linked, not whether it works — and reading the header is what
   let the argument-handling defect above ship three times.

## What changed in v2.5.2 (over v2.5.1)

**A third audit, on angles the first two never used** — time, concurrency,
cross-surface agreement, hostile input — followed by seven fix agents on
disjoint files and an independent maintainer re-verification against each
finding's own repro (41/41). Full detail in `CHANGELOG.md`. The invariants a
future change must not break:

1. **Stored content is NEVER interpolated raw into anything Claude reads.**
   A memory row could forge a complete `<system-reminder>` block into the
   SessionStart injection (**8 blocks where the plugin emits 1**) and into
   PROGRESS.md, and `memory_add` is a model-invokable MCP tool — so one indirect
   injection became a *permanent* memory re-injected as authoritative context
   every session. `core.privacy.neutralize_markers` (escape, never delete) runs
   on the write path via `clean_for_storage` **and again on every render path**,
   because rows written by v2.5.1 and earlier are already armed in users' DBs.
   `neutralize_inline` for single-line slots, `neutralize_block` for slots whose
   newlines are real structure. Four renderers are covered: `core/progress.py`,
   `hooks/session_start.py`, `core/plan.py`, `llm/memory_writer.py`. If you add
   a fifth, neutralise there too. `core/consolidate.py`'s
   `^</?(ide_opened_file|system-reminder|antml)` list is garbage cleanup, **not**
   this defence — it is anchored at position 0 and one leading word evades it.

2. **`core.modes.is_excluded` has SEVEN callers, not six.** The six hooks plus
   `mcp/server.py:_get_db`, the single choke point every MCP tool reaches. MCP
   is loaded by default from the shipped manifest and every call is
   model-initiated, which makes it the *least* optional of the seven. Do not add
   an MCP handler that opens a DB path itself.

3. **`core.modes.read_config` is THE runtime reader of `config.json`.** It reads
   `utf-8-sig` (a BOM — PowerShell's `Out-File` default — used to switch the
   whole opt-out off silently) and **fails CLOSED**: a file that exists and
   cannot be used excludes every project and logs why. Absent/empty is not that
   case. `_norm_path` cannot raise, so no single entry can abort the loop
   (`~user` raises `RuntimeError`, which is neither `OSError` nor `ValueError`).
   Do not re-add a private config read; `cli/mem.py` and `mcp/server.py` keep
   version-only readers on purpose, both `utf-8-sig`.

4. **Generated artifacts are written atomically and named uniquely.**
   PROGRESS.md / MEMORY.md / PLAN.md go through tmp + `os.replace` (0-byte reads
   were routine: 4,867 in 16,071 samples). Session archives carry millisecond
   stems **and** an `O_CREAT|O_EXCL` claim of the exact path; `.plan_history`
   likewise (4 sequential replacements used to leave 1 file).
   `write_session_archive` must derive `YYYY/MM` from the stem it is given, not
   from its own clock.

5. **`MemoryDB._connect` is a context manager that CLOSES.** It commits /
   rolls back exactly as `sqlite3.Connection.__exit__` does; the `close()` in
   the `finally` is the only new behaviour. All 81 call sites keep
   `with self._connect() as conn:`. Cost is real and measured: +340 % per
   operation (WAL checkpoint on last-close), +0.6 s on a 120 s PreCompact
   budget. Do not "optimise" it back into a factory.

6. **`<private>` is honoured on BOTH progress ingresses** —
   `hooks/user_prompt.py` and `hooks/pre_compact.py:_first_user_request` — and
   cleaning happens **before** the 500-char cut so a span straddling the cut
   stays a matched pair. PROGRESS.md is not in `memory/.gitignore`, so a leak
   there is a leak into the user's repository.

7. **`tools/citation_check.py` gates doc `file:line` citations** (see § Tests).

## What changed in v2.5.0 (over v2.4.3)

**A correctness release, not a feature release.** ~134 defects closed across 26
files, then re-attacked by four read-only adversarial verifiers whose findings
were closed too. Nine things that were silently wrong in shipped code:

1. **Cross-project data contamination — the worst of the set.**
   `~/.claude/projects/` slugs replace **every** character outside
   `[A-Za-z0-9]` with `-`; the old mangler replaced three (`:` `\` `/`), so any
   project path containing `_` or `.` produced a non-existent slug — and the
   miss fell through to a **fuzzy substring search** across every slug
   directory (179 on the reference box; substring `core` matched 131, `app` and
   `data` 141 each). `core.extractor.mangle_project_path` is now the single
   source of truth for the convention and the fuzzy branch is **deleted** (a
   miss is "no transcript", never "guess"). `hooks/session_start.py` gained
   `_transcript_belongs_to` — fail-closed, demands positive `cwd` proof — for
   `retroactive_save`, and the deliberately weaker `_transcript_is_foreign`
   (absent `cwd` allowed, different `cwd` refused) for the tier-3 mine, so the
   cwd-less `smoke_test.py:266-278` fixture still works. `ui/dashboard.py`
   carried a verbatim copy of the same resolver; that is gone too.
   Measured: retroactive save 2 LLM legs / `['aaaa-foreign','bbbb-mine']` → 1 leg
   / `['bbbb-mine']`; tier-3 `open_todos=['FOREIGN TODO leak']` → `[]`.

2. **The v2.2 live plan anchor had never run through its own hook.**
   `hooks/post_tool_use.py` early-returned on `not should_observe(mode, tool)`
   and the whole plan block sat below that gate. `TodoWrite` is in every mode's
   `skip_tools` and `ExitPlanMode` is in no mode's `observe_tools`, so both
   plan-control tools were `False` in all three modes: `plan_active` was never
   written, `.plan_raw.md` / `PLAN.md` never appeared, and the drift counters
   varied by mode. `_apply_plan_integration` (`post_tool_use.py:82`) now runs
   **above** the gate; the gate wraps only the `insert_observation` block.
   Plan control is not observation — `core/modes.py`'s `should_observe`
   docstring now forbids re-inverting this. Per mode: ExitPlanMode → plan rows
   0/0/0 → 1/1/1; Edit counter 1/0/1 → 1/1/1; `git push` 21/20/1 → 21/21/21.
   A raw plan awaiting refinement is also no longer invisible:
   `core.plan.raw_pending_refinement` (`plan.py:262`) makes PLAN.md and
   `plan-status` lead with a PENDING REFINEMENT banner + the raw text.

3. **`core/privacy.py` failed OPEN.** `strip_private` was a non-greedy `re.sub`
   behind a `count("<private>") > 100` ReDoS guard that **returned the text
   unchanged** — 100 tags stripped, 101 leaked, into both the Anthropic call and
   the `memories` table. The cap was calibrated on the wrong signal too: 20,000
   well-formed tags cost `re.sub` 6.0 ms, but 16,000 **unterminated** ones
   (140.6 KiB) cost 9,517.4 ms. Replaced by a single left-to-right `str.find`
   scan (`_strip_tagged_spans`): no cap, 0.0 ms on that input, and a dangling
   open tag now fails **CLOSED** (remainder dropped). Equivalence proved on
   20,000 random inputs — 0 differences on all 13,328 well-formed ones.
   Relatedly, `hooks/post_tool_use.py` classified `is_private` **after**
   `_truncate_output`, which turns a `Read` body into the literal
   `"(file content)"` — so a Read of a file the user marked private stored
   `is_private=0` and shipped its path to the API. Classification now runs on the
   raw input/response.

4. **MCP was wrong on the wire and unenforced at the schema.**
   `mcp/server.py` now forces UTF-8 + LF on stdin **and** stdout before the
   handles are captured (default gbk: 1/7 non-ASCII payloads round-tripped →
   7/7; strict gbk: 5 of 7 got no response at all and the server exited 1).
   Every parsed message carrying an id gets exactly one frame — `params: null`,
   a non-object `params` and a non-string tool `name` all used to produce
   **silence**, hanging a client with no timeout. Nothing escapes `main()`
   (a 4301-digit int → `ValueError`, ~3000-deep nesting → `RecursionError`, both
   reachable through `memory_search.limit` before validation); frames are
   length-capped and strict RFC 8259 both ways. `tools/call` arguments are
   validated against the advertised `inputSchema` and refused with `-32602`
   instead of coerced — `memory_search` with no `query` used to dump the table
   and rebuild the FTS index (6 malformed queries → 6 rebuilds, 27.3 ms → 0
   rebuilds, 6.1 ms). `core/db.py` gained `_MAX_SEARCH_LIMIT = 1000`, clamped at
   both ends (SQLite reads `LIMIT -1` as no limit), and `LIKE ? ESCAPE '\'`.

5. **`ui/web_viewer.py` was unusable and was a prompt-injection channel.**
   A single-threaded `HTTPServer` with no handler timeout meant **one** idle TCP
   connection — exactly what `webbrowser.open` provokes — wedged the server
   permanently; now `ThreadingHTTPServer` + daemon threads + per-connection
   timeout (8 idle pre-connects → 200 in 0.02 s). It sent
   `Access-Control-Allow-Origin: *`, so any page could write into the user's own
   next session and read `/api/sessions`' `archive_path`; the header is gone and
   `Origin`, `Host` (DNS rebinding: a rebound page is same-origin and sends no
   `Origin`) and `Content-Type: application/json` are enforced. POST rewrote the
   **wrong project's** `MEMORY.md` (`os.getcwd()` instead of the served project).
   Four routes answered malformed queries with no HTTP response at all, and body
   reads had no wall-clock deadline (a 1-byte-per-3-s drip held a worker
   52.09 s → 3.02 s). The Add-Memory form the docs already claimed now exists.

6. **The standalone installer shipped zero user-facing surfaces, and crashed
   then hung on an unparseable `settings.json`.** `~/.claude` after an install
   held `hooks/` and `settings.json` only — no `/cc-mem`, no agents, no skills.
   `SURFACE_FILES` (5 entries) is now copied into `~/.claude/{commands,agents,
   skills}`, recorded in `installed_surfaces.json`, and removed **by name** on
   uninstall. `_read_settings` validates at step [0/3] before anything is
   copied (19 shapes × 6 operations: **18 crashes → 0**), tolerates a BOM
   (PowerShell's `>`), preserves malformed hook groups verbatim instead of
   shredding them, and keeps-and-warns about a hook that merely *mentions*
   cc-memory instead of deleting it. The `× 1.5` Windows timeout multiplier is
   deleted; `_declared_hook_timeouts()` reads `hooks/hooks.json` when present
   and the literal table is a numerically identical fallback. `logs/` survives
   uninstall; stale modules from a previous version are pruned.

7. **Hooks with hard host timeouts now bound their LLM wall-clock.**
   `llm.ccl_backend.call_llm` gained an absolute `deadline` (clamps each leg to
   the time remaining, skips a leg that cannot finish). Only `core/consolidate.py`
   had ever honoured the docstring's requirement to bound the envelope:
   `session_start` overran its 15 s budget **with the shipped default config**
   (2 candidates × 20 s = 40 s) and `stop` measured 25.45 s against 22 s with
   stalled legs. Now `stop.py` `_LLM_DEADLINE_S = 14.0` (25.45 s → 15.99 s),
   `pre_compact.py` `75.0` (~144 s → 74.39 s of 120 s), `session_start.py`
   `_RETRO_DEADLINE_S = 13.0` with `_API_TIMEOUT` 20 → 10. Normal-path latency
   is unchanged (0.29 s → 0.30 s).

8. **One version string.** `cc_memory/core/version.py` is the canonical runtime
   source — importable under both layouts, unlike `cc_memory/__init__.py`, which
   now re-exports it. `cli/mem.py`, `mcp/server.py`, `ui/installer.py` and
   `build_exe.py` all resolve it instead of carrying literals (two of `mem.py`'s
   were stale). It is in `SUBPACKAGE_FILES["core"]` and
   `_REQUIRED_PLUGIN_FILES`, so a flat install ships it.

9. **`config.json` no longer lies, and `/cc-mem status` sees flat installs.**
   Two audits measured 34 of 51 leaf keys with no Python reader; every inert key
   is deleted (86 → 29 lines) and the survivors cite their reader in-file.
   `excluded_projects` is **not** a new key — it shipped in v2.4.3 with an empty
   default and zero readers repo-wide; v2.5.0 gave it readers, and it is now a
   real opt-out enforced by **all six hooks** through the single implementation
   `core.modes.is_excluded(cwd)`, called as each hook's first act after
   resolving `cwd`. Do not re-copy that function into a hook: v2.5.0 shipped it
   as two private copies in `user_prompt.py` + `pre_compact.py` (the only two
   hooks that CREATE `memory/`), which left a project initialised BEFORE it was
   listed fully instrumented — the other four gate only on `memory/memory.db`
   existing, so observations, PROGRESS.md and the Stop observer's API calls all
   kept running. `tests/test_surfaces.py` §4 now drives all six.
   Separately, `_inspect_layout` resolved `cc_memory/…`-prefixed paths against
   the layout root, so a healthy **flat** install reported 22 of 22 files
   missing and the API-key check was skipped; it now resolves `pkg_dir` once and
   only requires `hooks/hooks.json` for plugin-manifest installs.

**Also**: `/cc-mem sql` is genuinely read-only (`DROP TABLE topics` used to exit
0 and drop the table); the dashboard's SQL console requires a confirmation
naming any non-`SELECT` statement, bulk delete became bulk **archive**, a corrupt
project registry is backed up before being overwritten, launching with no
`--project` opens nothing, and a new read-only **Progress / Plan** tab renders
the `progress` + `plan_active` rows (7 tabs now). `.claude-plugin/plugin.json`
ships an inline `mcpServers` entry. `cc-memory-plan` (the console script) could
not be imported at all — `cli/plan.py` now has a `main()`.

**Residual limits, recorded rather than papered over:**

- `core/db.py`'s three plan mutators — `update_plan_status` (`db.py:1417`),
  `delete_plan` (`:1410`) and `update_plan_content` (`:1427`) — all accept
  `project_id`, and `cli/plan.py` + `ui/dashboard.py` pass it at every call
  site, but none of them *requires* it (it defaults to `None`). An unscoped raw
  call from new code would therefore still cross projects, because `plans.id` is
  global to the DB file. This is the wording `README.md` § "What is *not* fixed"
  uses; the pre-v2.5.1 text here claimed `delete_plan` / `update_plan_content`
  "take no `project_id`", which contradicted both the code and the README.
- `ThreadingHTTPServer` has no worker cap — body reads are deadline-bounded, the
  thread count is not. Loopback-only. DNS rebinding was verified with forged
  `Host` headers, not real DNS; the SPA escaping hardening is defence-in-depth
  (no XSS was executed).
- MCP still echoes array/object `id`s, and an unparsable/over-length frame is
  answered with `"id": null` because its id is unknowable. The 1 MiB frame cap
  is justified by the escape class — `MemoryError` was never reproduced.
- `mcp/server.py`'s `_MIN_CONTENT_LEN = 10` is a hand-mirrored literal, not an
  import, to keep server boot lazy.
- The installer's `--console` switch is asserted from the PyInstaller flag; the
  exes were not rebuilt, so the PE subsystem is unverified.
- Searching for a bare `%` or `_` now returns 0 rows instead of the whole table.
- Doc `file:line` citations are hand-maintained and rot on every refactor;
  `docs/ARCHITECTURE.md` and `docs/CONTRACTS.md` still carry stale ones (a
  definition-site checker finds them mechanically — see the note under
  § Tests). Nothing enforces them.

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
│   ├── plugin.json              ← Plugin manifest (+ inline mcpServers entry)
│   └── marketplace.json         ← /plugin marketplace add entry
├── hooks/hooks.json             ← 6 hook commands across 5 events
├── skills/                      ← THE canonical skills location
│   ├── ccm-load/SKILL.md        (one-shot end-to-end activation + init + status)
│   └── save-memories/SKILL.md   (routes through memory_writer)
├── agents/                      ← Plugin-shipped subagents (v2.2+)
│   ├── plan-refiner.md          (raw plan → structured JSON, one-shot)
│   └── plan-guardian.md         (drift check, read-only, ≤150 words)
├── commands/cc-mem.md           ← /cc-mem slash command
├── docs/                        ← TWO English docs since v2.4.3, each with a
│   │                              drift-tracked .zh.md sibling
│   ├── ARCHITECTURE.md          ← overview + install layouts + i18n convention (§9)
│   ├── ARCHITECTURE.zh.md
│   ├── CONTRACTS.md             ← anti-patch + forced handoff + live plan anchor
│   └── CONTRACTS.zh.md
├── README.md / README.zh.md     ← drift-tracked pair
├── tools/i18n_check.py          ← translation drift checker (dev/CI only)
├── cc_memory/
│   ├── __init__.py              (re-exports core/version.py)
│   ├── config.json
│   ├── core/                    db, extractor, consolidate, idle, progress,
│   │                            plan, privacy, modes, auth, logger,
│   │                            encoding_setup, version
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
│   ├── test_plan_carryover.py   carryover gate (v2.4.0+), 14 checks
│   └── test_surfaces.py         installer surfaces + settings shapes + timeout
│                                lockstep, MCP stdio, web-viewer guards, hook
│                                LLM deadline (v2.5.0)
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
| `PostToolUse` | `cc_memory/hooks/post_tool_use.py` | 8s | Live plan anchor in EVERY mode (ExitPlanMode capture / TodoWrite step sync / drift counters), THEN an observation row for observed tools only (no LLM) |
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

The `progress` row has 11 user-facing fields (`current_request`, `status_done`,
`status_in_flight`, `status_blocked`, `open_todos`, `plan`, `critical_context`,
`files_touched`, `transcript_ptr`, `updated_at`, `trigger_type` —
`core/db.py:188-201`), **plus** the two v5 session-annotation columns
`current_session_id` / `session_started_at` (`core/db.py:230-233`). That is 13
non-PK columns in the schema; "11" counts only the user-facing ones, and
`docs/ARCHITECTURE.md` §4 states it the same way. It is updated by four paths:
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
  bumps `edits_since_last_guardian`; a sensitive Bash call bumps it by 20.
- `Stop` hook emits a single status line when guardian thresholds are
  crossed (default: 8 turns OR 12 edits). Main Claude responds by
  invoking the **`plan-guardian`** subagent (also in `agents/`), then
  `/cc-mem plan-check` to reset counters. The refiner nudge is rate-limited
  to once every 5 turns per session (`hooks/stop.py:_claim_refine_nudge`);
  only `plan.apply_refined_plan` may clear `needs_refine`.

**All of the `PostToolUse` legs above run in EVERY mode, above the
`should_observe` gate** (`hooks/post_tool_use.py:165`). They shipped below it
from v2.2 through v2.4.3, which made the entire anchor dead through its own
hook — `TodoWrite` is in every mode's `skip_tools` and `ExitPlanMode` is in no
mode's `observe_tools`. Plan control is not observation: mode selects what is
worth *remembering*, never whether the plan anchor tracks reality. Do not move
this block back under the gate, and do not "fix" it by adding those two tools to
the mode allow-lists.

A raw plan that has not been refined yet is rendered verbatim under a
**PENDING REFINEMENT** banner by both `PLAN.md` and `/cc-mem plan-status`, with
any older structured plan labelled stale — `core.plan.raw_pending_refinement` is
the shared predicate.

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
- Tag memories with their extraction method for traceability. The COMPLETE set
  of tags any code emits today — verified by grepping every `"tags"` literal
  under `cc_memory/`:
  - `["observer","realtime"]` — `hooks/stop.py`
  - `["mcp"]` — `mcp/server.py`
  - `["manual"]` — `cli/mem.py`
  - `["manual","dashboard"]` — `ui/dashboard.py:1599` (Add-Memory dialog)
  - `[method, "manual"]` where `method` is `"llm"` or `"regex"` —
    `ui/dashboard.py:2125` (Save Session)
  - `["regex","manual"]` — `ui/dashboard.py:2151` (Save Session, regex leg)
  - `["metric","manual"]` — `ui/dashboard.py:2156` (Save Session, metric leg)
  - `["auto-detected","init"]` — `ui/dashboard.py:2278` (new-project init)
  - `["web"]` — `ui/web_viewer.py`
  - `["llm-dedup","merged"]` — `core/consolidate.py`

  The writer appends `"merged"` / `"supersedes"` on top of whatever the caller
  passed. NOTE: the PreCompact LLM path sets no `tags` key at all, so those rows
  store `[]` — do not document a `["llm","auto"]` tag that no code emits. If you
  add an emitter, add it to this list; the four `ui/dashboard.py` Save-Session /
  init shapes were missing from it through v2.5.0.
- `memory/PROGRESS.md` and `memory/MEMORY.md` are generated artifacts. Edit
  the SQL source of truth (`progress` table for PROGRESS.md, `memories`/
  `topics`/`keywords` for MEMORY.md) instead.

## Tests

**Three suites. ALL are release gates — run all three, plus `tools/i18n_check.py`
and a `tomllib` parse of `pyproject.toml`.**

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

`tests/test_surfaces.py` (v2.5.0) covers the surfaces neither of the others
touched: the standalone installer (surfaces installed and removed by name,
malformed-`settings.json` shapes, hook-timeout lockstep against
`hooks/hooks.json`, manifest parity so a new runtime module cannot ship
unpackaged), the MCP stdio server, the web viewer's request guards, and the
source-level rule that every LLM-calling hook passes an absolute deadline.

```bash
python tests/smoke_test.py
# expect: [OK] lines ending with "===== ALL SMOKE TESTS PASSED ====="
python tests/test_plan_carryover.py
# expect: "RESULT: 14 passed, 0 failed"
python tests/test_surfaces.py
# expect: "===== ALL SURFACE TESTS PASSED ====="  (§1-§5)
python tools/i18n_check.py
# expect: "3 in-sync", exit 0
python tools/citation_check.py
# expect: "0 stale, 0 missing", exit 0  (also asserted inside smoke_test.py)
```

No pytest / pip dependencies — all three are stdlib scripts and reflect the
runtime contract (pure stdlib, see Development guidelines below). When you add a
behavior to `memory_writer`, `progress`, `extractor.load_transcript_window`, or
`session_start._refresh_progress_row`, add a corresponding assertion block.
Tests MUST use `tempfile` directories only; installer work must redirect
`USERPROFILE`/`HOME` **and** `TMPDIR`/`TEMP`/`TMP`. Every subprocess capture
needs an explicit `encoding="utf-8"` — the default codec on this box is gbk and
the CLI emits real UTF-8.

**`excluded_projects` is covered by `tests/test_surfaces.py` §4** — it drives
all six hooks against a fresh excluded directory, a subdirectory of one, and a
project that was initialised BEFORE it was listed (the case the two-copy v2.5.0
implementation got wrong). Keep that block in step with any hook you add: a hook
that does not call `core.modes.is_excluded` is a privacy regression, not a style
nit.

**Doc `file:line` citations ARE gated now — `tools/citation_check.py` (v2.5.2).**
For each `path:lines` citation in the tracked docs it resolves the symbols named
in the surrounding prose with `ast` and asserts the cited range covers the
definition **or** mentions the symbol (the docs cite call sites at least as often
as definitions). It runs inside `smoke_test.py`, so rot turns the suite red.
`python tools/citation_check.py --fix` repairs what it can; `--list` shows every
verdict. First run measured **163 of 594 citations stale** — the cost of three
releases with no gate.

Two limits to know before trusting a green result: a citation whose sentence
names no resolvable symbol at all is reported **SKIP**, not OK (253 of 594
today, down from 370 once v2.5.3 taught it to anchor CROSS-FILE citations on the
text of the cited range — the `` `db.tag_progress_session(...)`
(`user_prompt.py:181`) `` shape, which is the commonest in these docs). `--fix`
rewrites a same-file citation to the **definition** site and a cross-file one to
the occurrence NEAREST the stale number — a stated assumption (it was right when
written; the file grew above it), not a proof. Ordinary variable
assignments are deliberately not indexed — indexing them made the checker anchor
prose words like `db`, `pid` and `plan` onto unrelated locals and report ~40
correct citations as rot, which is the failure mode that makes a gate worthless.

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
`cc-memory-installer.exe` (see `build_exe.py`). That installer lays the package
**FLAT** under `~/.claude/hooks/cc-memory/` — `core/`, `hooks/`, `llm/`, `cli/`,
`mcp/`, `ui/` directly under that directory, with **no `cc_memory/` path
segment** — copies the 5 surfaces into `~/.claude/{commands,agents,skills}`, and
registers hooks in `settings.json[hooks]` the v2.0 way. Same package, different
on-disk shape: any code or doc that probes for an install must accept both the
nested and the flat form. `~/.claude/hooks/cc-memory/` under a marketplace
install holds only `logs/`.

## See also

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — full architecture overview
- [docs/CONTRACTS.md#anti-patch-contract](docs/CONTRACTS.md#anti-patch-contract) — anti-patch contract
- [docs/CONTRACTS.md#handoff-contract](docs/CONTRACTS.md#handoff-contract) — PROGRESS.md spec
- [docs/CONTRACTS.md#plan-contract](docs/CONTRACTS.md#plan-contract) — PLAN.md + subagent spec (v2.2)
- [CHANGELOG.md](CHANGELOG.md) — version history
