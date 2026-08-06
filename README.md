> **English** · [简体中文](README.zh.md)

# cc-memory

**Claude Code persistent memory plugin (v2.5.6)** — anti-patch reconcile-on-write
with LLM-judged semantic de-duplication, forced PROGRESS.md handoff, live PLAN.md
anchor with plan-refiner / plan-guardian subagents and a mandatory carryover
gate, bounded transcript reads, injection observability, FTS5 search, AI-judged
extraction with Haiku (optional local Ollama fallback).

## What it solves

Claude Code compresses (compacts) conversations when the context window fills up,
causing information loss: decisions, results, todos, and project knowledge
disappear. Conversations that end normally (terminal closed) also lose context.

cc-memory captures structured memories at every conversation boundary AND
**forces the next session to read a handoff document** before it starts work.

## What's new in v2.5.6

**The plan-replacement gate guards `steps`. It always has — and on 2026-08-05
that cost a live plan two of its ten success criteria.** The replacement passed
cleanly, nothing was printed, and one of the vanished criteria was an
achieved-but-never-recorded release gate. A gate that covers part of an
artifact says nothing about the rest; the silence looked identical to approval.

- **`success_criteria` carryover advisory.** `plan-set --from-refiner` now
  snapshots the outgoing plan *before* the replacement and prints every
  criterion whose best trigram-Jaccard against the new plan's criteria — plus
  its `goal` and `context` — falls below the same `0.5` threshold the steps
  gate uses. A criterion folded into the new context counts as carried: lossy
  survival is still survival, and crying wolf trains the reader to skip it.
- **Deliberately an advisory, not a second refusal.** Criteria get reworded,
  merged, translated and retired-because-achieved. An English plan replaced by
  a Chinese one auto-carries nothing at all, so a hard gate here would make
  ordinary plan evolution impossible. What it buys is that "it vanished" and "I
  retired it on purpose" stop looking the same.
- **The advisory names its own blind spot.** Its last line says `context` is
  free text and is never compared — read it yourself. A gate that hides its
  scope is how this failure happened in the first place.
- `unmatched_criteria()` is appended at the **end** of `core/plan.py` on
  purpose: this repo documents contracts with ~600 `file:line` citations, and
  inserting beside `check_carryover` would have rotted ~60 of them across four
  docs. Topic cohesion lost to not breaking the citation graph.
- Pinned by `tests/test_plan_carryover.py` §7 — the core result, the
  context-fold suppression, and that the CLI actually *prints* it. A core
  function nobody surfaces is the same silence with extra steps.

## Previously — What's new in v2.5.5

**The doc gates covered 7 of this repository's 13 markdown files.** Asked
whether every document was aligned, the answer turned out to be that the *gate
scope* was the stale thing.

- `tools/citation_check.py` now tracks **all 13** markdown files — `CHANGELOG.md`,
  both agent prompts, `commands/cc-mem.md` and both skills were covered by
  nothing. `smoke_test.py` asserts the tracked list equals `git ls-files "*.md"`.
- **The docs' countable claims are gated too**, and three had drifted:
  `CLAUDE.md` told the next Claude to run 7 of the 8 gates; `commands/cc-mem.md`
  named 23 of 28 CLI subcommands (the missing five included `sql`, whose
  read-only guard is a security fix nobody can use without knowing it exists);
  and this README still said doc citations were unenforced, three releases after
  they stopped being.

## What's new in v2.5.4

**Zero known limits.** v2.5.3 closed five of six residuals and recorded four new
ones; this release closes all four, and adds a gate for each.

- **Every citation is checked — 0 unchecked, down from 253.** A citation naming
  no symbol is now bounds-checked (inside the file, not blank), and an ambiguous
  bare filename is disambiguated by symbol. The bounds check alone found **34
  stale citations** pointing past EOF or at blank lines, which every previous
  release shipped.
- **The `settings.json` lost update is closed in both directions.** v2.5.3
  checked the digest *before* the rename; there is now a post-write
  verification, so a peer write landing *after* it is detected too.
- **PLAN.md and MEMORY.md no longer go stale.** A retry *count* was the wrong
  shape — the file is unavailable for a *duration*. With a 3 s wall-clock
  budget: 0 stale renders in 150 rounds against three 100 %-duty readers, where
  12 fixed tries lost 2.
- **The dashboard exe is executed too**, not just PE-header inspected.

## What's new in v2.5.3

**v2.5.2's "Known limits" section, cleared.** No new audit — this release takes
the six residuals that release recorded rather than fixed. Two of them turned
out to be worse than they were written up as:

- **The three "deliberate literal twins" were not twins.** `core/progress.py`
  retried and re-raised; `core/plan.py` and `llm/memory_writer.py` had no retry
  and fell back to a plain **truncating** write — reintroducing the torn-read
  defect they existed to remove. That fallback *was* the residual. One
  implementation now (`core/atomic.py`), with an explicit contract: replace
  completely, or raise. Never truncate.
- **The plan mutators still accepted an unscoped call.** `plans.id` is global to
  the DB file, so an unscoped `UPDATE`/`DELETE` reaches another project's row.
  All 11 call sites already passed `project_id` by keyword, so requiring it cost
  nothing — it is now mandatory and keyword-only.

The rest: a fail-closed `config.json` now says so on SessionStart instead of
only in a log file; the installer **detects** a concurrent `settings.json` write
and re-merges instead of clobbering it; the installer exe is now actually **run**
(12/12) rather than PE-header-inspected — which immediately found that unknown
arguments were silently ignored, so a typo'd `--unistall` performed an *install*
and exited 0; and doc citation coverage went from 224 to 341 of 594 checked.

## What's new in v2.5.2

A third audit, on angles the first two never used — time, concurrency,
cross-surface agreement, hostile input. Full detail in
[CHANGELOG.md](CHANGELOG.md); the four that matter most:

- **Stored memory content could forge a complete `<system-reminder>` block** into
  the SessionStart injection (**8 blocks in a stdout where the plugin emits 1**)
  and into PROGRESS.md. `memory_add` is a model-invokable MCP tool, so one
  indirect injection — a malicious README, a fetched page, a dependency's source
  — became a *permanent* memory re-injected as authoritative context at the
  start of every later session. Markers are now **escaped, not deleted**, on the
  write path *and* on every render path (PROGRESS.md, the injection, PLAN.md,
  MEMORY.md), so text stays readable and stops carrying authority.
- **Two ways the privacy opt-out silently switched itself off**: a UTF-8 BOM on
  `config.json` (PowerShell's `Out-File` default) made `json.load` raise into a
  swallow-and-continue, and one unexpandable `~user` entry voided every entry
  after it. The parser now reads `utf-8-sig`, cannot be aborted by one bad
  entry, and **fails closed** on a config it cannot use.
- **The MCP server ignored `excluded_projects` entirely** — the seventh caller
  of a control v2.5.1 had just wired into the six hooks, and the one loaded by
  default with every call chosen by the model.
- **Concurrent and same-second writes destroyed data**: 12 compactions left 3
  session archives; four *sequential* plan replacements left 1 history file;
  PROGRESS.md / MEMORY.md / PLAN.md could be read as 0 bytes. All are now
  claimed atomically and written through `os.replace`.

Also: `MemoryDB._connect` no longer leaks a sqlite handle per operation (25 live
after 20 inserts → 0), and `tools/citation_check.py` now gates doc `file:line`
citations — its first run found **163 of 594 stale**.

## What's new in v2.5.0

The largest correctness release so far, and not a feature release: roughly 134
defects closed across 26 files, then re-attacked by four read-only adversarial
verifiers whose findings were closed as well. The headline items below were all
**silently wrong in shipped code**.

### Cross-project data contamination is closed

Claude Code's `~/.claude/projects/` directory names replace **every** character
outside `[A-Za-z0-9]` with `-`; cc-memory replaced only three of them
(`:` `\` `/`). Any project path containing `_` or `.` therefore mangled to a
slug that does not exist — and the miss fell through to a **fuzzy substring
search** across every slug directory on the machine. On the reference machine
(179 slug directories) the substring `core` matched 131 of them, and `app` and
`data` matched 141 each. A project could ingest another project's transcript,
send it to the extraction LLM, and store the result as its own memories.

- `core.extractor.mangle_project_path` is now the single source of truth for the
  slug convention, and the fuzzy fallback is **deleted** — a miss means "no
  transcript", never "guess". Four probes that previously resolved to a real but
  wrong directory now return `None`.
- Retroactive save demands **positive proof** that a transcript belongs to this
  project (the `cwd` its records carry) and fails closed when there is none.
  With two planted transcripts, one foreign: 2 LLM legs ingesting
  `['aaaa-foreign', 'bbbb-mine']` → 1 leg, `['bbbb-mine']`; a transcript with no
  `cwd` at all → 0 legs, 0 memories.
- The tier-3 PROGRESS.md mine is gated too, with a deliberately weaker
  disagreement rule (absent `cwd` allowed, a *different* `cwd` refused) so
  cwd-less transcripts keep working. A foreign transcript that used to yield
  `open_todos=['FOREIGN TODO leak']` and `files=['FOREIGN_SECRET_FILE.py']` now
  yields nothing and logs the refusal.
- The dashboard carried a verbatim copy of the same fuzzy resolver. It is gone
  there too.

### The v2.2 live plan anchor works through its hook — for the first time

`PostToolUse` returned early when `core.modes.should_observe()` was false, and
the whole live-plan block sat *below* that gate. `TodoWrite` is in every mode's
`skip_tools` and `ExitPlanMode` is in no mode's allow-list, so `should_observe`
was `False` for both plan-control tools in all three modes: `plan_active` was
never written by the hook, `memory/.plan_raw.md` and `memory/PLAN.md` never
appeared, and the guardian drift counters silently varied by mode. Plan control
is not observation — the block now runs above the gate, in every mode.

| via `PostToolUse`, per mode (code / research / writing) | before | after |
|---|---|---|
| `ExitPlanMode` → `plan_active` rows | 0 / 0 / 0 | 1 / 1 / 1 |
| `TodoWrite` → PLAN.md step statuses | untouched | `[x] [x] [~] ← ACTIVE` |
| `Edit` → `edits_since_last_guardian` | 1 / 0 / 1 | 1 / 1 / 1 |
| Bash `git push` → counter (1 edit + 20) | 21 / 20 / 1 | 21 / 21 / 21 |

A raw plan awaiting refinement is also visible now: `PLAN.md` and
`/cc-mem plan-status` lead with a **PENDING REFINEMENT** banner and the raw
text, with any older structured plan clearly labelled stale, instead of showing
only the superseded plan.

### The privacy filter failed OPEN, and is now linear-time and fails CLOSED

`strip_private` was `re.sub(r"<private>.*?</private>", "", text)` behind a
`text.count("<private>") > 100` ReDoS guard — and that guard **returned the text
unchanged**. Above the cap, `<private>` content reached both the Anthropic API
call and the `memories` table: 100 tags stripped correctly, 101 leaked.

The cap was also calibrated on the wrong signal. Well-formed tags are cheap;
an **unterminated** `<private>` is the quadratic case — 16,000 unterminated tags
(140.6 KiB) took `re.sub` **9,517.4 ms** and leaked the tail. A single
left-to-right `str.find` scan replaces it: 0.0 ms on that input, 5.1 ms vs
6.0 ms on 20,000 well-formed tags, no cap at all, and a dangling open tag now
drops the rest of the text rather than emitting it. Equivalence was proved on
20,000 random tag-soup inputs — zero behavioural differences on all 13,328
well-formed ones; the 6,672 unterminated ones differ intentionally.

### MCP: correct on the wire, and the schema is actually enforced

- **UTF-8 both directions, before the handles are captured.** Under the box's
  default gbk codec only 1 of 7 non-ASCII payloads round-tripped byte-identical;
  under strict gbk five got no response at all and the server exited 1. Now 7/7
  in all four locale regimes tested, stderr 0 B, exit 0. No `PYTHONUTF8` env
  block is needed any more.
- **Every parsed message with an id gets exactly one frame.** `params: null` —
  which many clients send for "no params" — produced **no response**, hanging any
  client without a timeout. So did a non-object `params` and a non-string tool
  `name`. All now answer `-32602`; 13/13 ids answered where 8 were orphaned.
- **Nothing escapes `main()`.** A 4301-digit integer (`ValueError`) and ~3000
  levels of nesting (`RecursionError`) were both reachable through an advertised
  tool argument *before* validation ran, and killed the server with a traceback
  on stderr. Frames are also length-capped and strict RFC 8259 in both
  directions (`NaN` / `Infinity` rejected instead of echoed).
- **`tools/call` arguments are validated against the advertised `inputSchema`**
  (required / type / enum / bounds / lengths) and rejected with `-32602` instead
  of being coerced. `memory_search` with no `query` used to dump the whole table
  and rebuild the FTS index; six malformed queries triggered six index rebuilds
  (27.3 ms) and now trigger none (6.1 ms). `memory_get_details` no longer serves
  retracted rows, and a failed write is no longer reported as success.

### The web viewer was unusable, and was a prompt-injection channel

- **One idle TCP connection wedged the whole server.** A single-threaded
  `HTTPServer` with no handler timeout meant a browser's speculative pre-connect
  — which `webbrowser.open` triggers by design — blocked every subsequent
  request. It now runs threaded with daemon threads and a per-connection
  timeout: 8 idle pre-connects → 200 in 0.02 s, 30 concurrent GETs → 30/30.
- **`Access-Control-Allow-Origin: *` on every response.** Anything written
  through `POST /api/memory` is injected into your next session, so any web page
  you had open could write into your own context — and read `/api/sessions`,
  which returns `archive_path` filesystem paths. The header is gone; `Origin`,
  `Host` (against DNS rebinding, where the attacking page is same-origin and
  sends no `Origin` at all) and `Content-Type: application/json` are all
  enforced.
- **`POST` rewrote the wrong project's `MEMORY.md`** — it derived the project
  root from `os.getcwd()` instead of the served project.
- Four routes answered malformed queries with **no HTTP response at all**; body
  reads had no wall-clock deadline, so one client dripping a byte every few
  seconds leased a worker thread for hours (measured: 403 after 52.09 s → 3.02 s;
  10 concurrent drips, thread delta 10 → 0-2).
- The `Add memory` form the docs already described now actually exists in the
  page.

### The standalone install shipped none of the user-facing surfaces

`~/.claude` after a standalone install contained `hooks/` and `settings.json`
and nothing else — no `/cc-mem` command, no `plan-refiner` / `plan-guardian`
agents, no skills. The installer now copies all five, records them in
`installed_surfaces.json`, and removes exactly those files **by name** on
uninstall (your own `commands/`, `agents/`, `skills/` entries survive).

It also **crashed after copying files** on any `settings.json` it could not
parse — a JSONC comment, a trailing comma, a UTF-8 BOM (which is what
PowerShell's `>` writes), or a hook group of an unexpected shape — leaving a
half-installed tree with no hooks registered. Settings are now validated at step
[0/3] before anything is copied, malformed shapes are preserved verbatim rather
than shredded, and a hook that merely *mentions* cc-memory is kept and warned
about instead of deleted. Across 19 settings shapes × 6 operations: **18 crashes
→ 0**.

### Hooks with hard host timeouts now bound their LLM wall-clock

`call_llm`'s own docstring required a time-budgeted caller to bound its
worst case; only `core/consolidate.py` did. `hooks/session_start.py` overran its
**15 s** budget with the shipped default config (2 credential candidates × 20 s
= 40 s), and `hooks/stop.py` measured **25.45 s** against a 22 s budget with
stalled legs. Every LLM-calling hook now passes an **absolute deadline** — not
just a per-leg timeout — which clamps each leg to the time actually remaining
and skips a leg that cannot finish. Stop: 25.45 s → 15.99 s. PreCompact: ~144 s
→ 74.39 s of its 120 s. Normal-path latency is unchanged (0.29 s → 0.30 s).

### Also in this release

- **One version string.** `cc_memory/core/version.py` is the canonical runtime
  source, importable under both the nested and the flat install layout;
  `cc_memory/__init__.py` re-exports it and the CLI, MCP and installer banners
  resolve it instead of carrying literals (two of which were stale).
- **`/cc-mem status` sees standalone installs.** Every layout probe assumed the
  nested `cc_memory/` segment, so a healthy flat install reported 22 of 22 files
  missing and the API-key check was skipped entirely.
- **`config.json` no longer lies.** Two audits found 34 of 51 leaf keys with no
  reader; every inert key is deleted, and the surviving ones cite their reader
  in-file. `excluded_projects` is **not** a new key — it shipped in v2.4.3 with
  an empty default and no reader anywhere — but it is now a real opt-out: a
  listed directory and everything beneath it gets no `memory/`, no DB, no
  observations, no extraction and no PROGRESS.md, because **all six hooks**
  check it before doing anything else.
- **`/cc-mem sql` is read-only for real.** `DROP TABLE topics` used to exit 0
  and drop the table. The guard also refuses the `PRAGMA name(value)` setter
  form, which SQLite accepts as an equivalent of `PRAGMA name = value` and an
  `=`-only test let through. The dashboard's SQL console requires a confirmation
  naming the statement before any write, and reports the rowcount.
- **The dashboard stopped destroying data**: bulk delete became bulk *archive*
  (no more dangling `supersedes_id` rows), `MEMORY.md` is regenerated after a
  tidy, a corrupt project registry is backed up before it is overwritten, and no
  callback can raise into a windowed build. It also gained a read-only
  **Progress / Plan** tab.
- **A third test suite.** `tests/test_surfaces.py` joins `smoke_test.py` and
  `test_plan_carryover.py` as a release gate, covering the surfaces neither of
  them touched: the standalone installer (surface install/uninstall by name,
  malformed-`settings.json` handling, hook-timeout lockstep with
  `hooks/hooks.json`), the MCP stdio server, the web viewer's request guards,
  and the rule that every LLM-calling hook passes an absolute deadline.

### What is *not* fixed

Recorded honestly, because each was measured rather than assumed:

- The web viewer is threaded with **no worker cap** — body reads are now
  deadline-bounded, but `ThreadingHTTPServer` still spawns a thread per
  connection. It is loopback-only. HTTP/1.0 pipelining is unchanged (browsers
  do not pipeline). The DNS-rebinding fix was verified with forged `Host`
  headers, not with real DNS control, and the SPA escaping hardening is
  defence-in-depth: no XSS was executed.
- MCP still echoes array/object `id`s (non-conforming but valid JSON; answering
  beats orphaning), and an unparsable or over-length frame is answered with
  `"id": null` because its id is genuinely unknowable. `MemoryError` from a huge
  line was never reproduced — the 1 MiB frame cap is justified by the escape
  class, not by a measured crash.
- `core/db.py`'s three plan mutators (`update_plan_status`, `delete_plan`,
  `update_plan_content`) all accept `project_id` and every shipped caller now
  passes it, but none of them *requires* it — an unscoped raw call from new code
  would still cross projects, because `plans.id` is global to the DB file.
- Searching for a bare `%` or `_` now returns 0 rows instead of the whole table.
  That is the fix, but it is a visible result change.
- **Doc `file:line` citations are enforced since v2.5.2** by
  `tools/citation_check.py`, which runs inside `tests/smoke_test.py`. Since
  v2.5.5 it covers **all 13** markdown files in the repository and leaves
  nothing unchecked: a citation is anchored to its symbol's `ast` definition or
  to a line that references it, and one whose sentence names no symbol is
  bounds-checked instead (inside the file, not blank). Repair a stale number
  with `python tools/citation_check.py --fix` rather than by hand.
- `tools/i18n_check.py` compares content hashes only. It cannot see a
  translation whose *body* has drifted from its English source — including a
  dead in-document anchor, which is how 22 of them survived in the Chinese docs
  until v2.5.1.

## What's new in v2.4.2

Hook survivability. On a long-lived project the `PreCompact` hook was being
**killed mid-write** — losing that compaction's memories — and its extraction
had quietly been reading the wrong end of the transcript. One root cause: the
hook loaded the *entire* transcript before using ~12 KB of it.

- **Bounded transcript reads.** `extractor.load_transcript_window` reads a
  head + tail window (40 records + 32 MiB) instead of the whole file. Measured
  on a real **2.11 GiB** transcript: loading went **88s → 1.66s**, and a full
  hook run finishes in **14.33s** against its 120s budget. `msg_count` stays
  exact via a raw record scan (~40× cheaper than parsing).
- **Extraction now reads the *recent* end.** The LLM summary filled its 12,000
  character budget starting from the oldest record — on that transcript it was
  exhausted after **329 of ~585,000 records**, so every extraction saw only the
  session's opening hours. It now fills from the newest backwards.
- **Killed runs are no longer invisible.** A timeout kill runs no `except`
  block, so a failed compaction used to leave `.last_save.json` describing the
  *previous* success. `PreCompact` now writes a start marker it clears only on
  completion, and SessionStart reports a survivor.
- **Automatic compactions are now visible.** `.last_save.json` records whether
  the trigger was `auto` or `manual` — Claude Code only surfaces hook execution
  in its UI for manual `/compact`, which made auto runs look like they never
  happened. (They always did.)
- **`memory/.gitignore` migrates existing installs** instead of only being
  created once, so generated state — including verbatim plan prose in
  `.plan_history/` — stops leaking into user repos.
- **Fixed: the package could not be built or installed.** A UTF-8 BOM in
  `pyproject.toml` (since v2.4.0) made `tomllib` fail, breaking every PEP 517
  frontend.

## What's new in v2.4.0 / v2.4.1

- **Mandatory plan carryover gate.** `plan_active` is a single-row slot, so
  replacing a plan used to silently sink unfinished steps. Replacement now
  requires every unfinished step to be auto-carried (by title similarity) or
  explicitly dispositioned with a reason; `/cc-mem plan-clear` refuses without
  `--reason`; every outgoing plan is archived to `memory/.plan_history/`.
  **There is no force flag, by design.** See
  [docs/CONTRACTS.md#plan-contract](docs/CONTRACTS.md#plan-contract).
- v2.4.1 fixed a false refusal where a long `notes` field diluted the
  title match and blocked a legitimate in-place plan update.

## What's new in v2.3.4

- **Anthropic auth fall-through.** A dead `ANTHROPIC_API_KEY` no longer
  blackholes a healthy Claude subscription — candidates are tried in order with
  the correct wire format each (`x-api-key` vs OAuth `Bearer`).
- **Local Ollama fallback is now opt-in** (`ccl.enabled`, default `false`).

## What's new in v2.3.3

- **Documentation multilingual version-control.** English is the canonical
  skeleton; Chinese docs are drift-tracked `*.zh.md` siblings (starting with
  [README.zh.md](README.zh.md)), each tied to a normalized-sha256 of its English
  source recorded in a line-1 marker. A pure-stdlib checker
  ([tools/i18n_check.py](tools/i18n_check.py)) plus a [tests/smoke_test.py](tests/smoke_test.py)
  gate turn red the moment an English doc changes without its translation being
  refreshed. Memory *content* stays language-agnostic — only docs are tracked.
  See [docs/ARCHITECTURE.md#9-documentation-language-convention-i18n](docs/ARCHITECTURE.md#9-documentation-language-convention-i18n).

This is a docs + version-metadata release — no runtime behavior changed.

## What's new in v2.3

- **LLM-judged semantic de-duplication.** The anti-patch writer's char-trigram
  similarity only catches near-verbatim restatement, so the same fact reworded
  each session used to stack up (unbounded DB growth). `consolidate.semantic_dedup`
  nominates small same-category candidate groups by word-Jaccard, Haiku confirms
  same-fact, and the survivor is refreshed to a merged canonical while losers are
  archived (`is_active=0`) with a forward `supersedes_id` link.
- **Obsolescence detection + reference-aware staleness net.** `detect_obsolete_llm`
  names `{stale, current}` pairs with a temporal guard (the superseder must be
  newer) + an anti-event prompt; `decay_and_archive` archives only rows that are
  simultaneously very old, low-importance, AND never injected. All archival is
  recoverable (`is_active=0`, never `DELETE`).
- **Injection observability.** SessionStart writes `memory/.last_inject.json`
  recording exactly which memories/topics were injected and prints a one-line
  recap; `/cc-mem inject-show` dumps ground truth, `/cc-mem inject-usage` reports
  whether Claude actually Read PROGRESS.md / MEMORY.md.
- **`/cc-mem encoding-check [--apply]`** — read-only U+FFFD corruption scan across
  the text tables (valid CJK preserved).

### v2.3.1 / v2.3.2 — "Hook cancelled" permanently fixed

The intermittent `Compacted PreCompact [...] failed: Hook cancelled` is gone.
v2.3.1 raised the PreCompact timeout 45s → 120s, but that only moved the goalpost
on large DBs. **v2.3.2 removes the failure mode**: `PreCompact` now declares two
command hooks — a fast **sync** leg (`hooks/pre_compact.py`, extraction +
PROGRESS.md, ~1-5s) and a background **`async`** leg (`hooks/consolidate_async.py`,
timeout 300s) that runs the every-Nth-session consolidation off the blocking
compaction path. A budget gate with an honest worst-case cost model guarantees the
async worker finishes before its timeout, so it can never be killed mid-write.
See [CHANGELOG.md](CHANGELOG.md).

## What's new in v2.2

- **Live plan anchor (`memory/PLAN.md`).** Captures `ExitPlanMode` output
  (or user-supplied raw plans) into a structured, step-tracked document
  that survives session boundaries. `TodoWrite` syncs step statuses
  mechanically; sensitive Bash calls (`git push`, deploys, ...) flag
  drift. See [docs/CONTRACTS.md#plan-contract](docs/CONTRACTS.md#plan-contract).
- **Plugin-shipped subagents.** `plan-refiner` normalises raw plans into
  JSON; `plan-guardian` checks alignment when drift counters trip.
  Definitions live in `agents/` and are auto-discovered after install.
- **`/cc-mem dashboard`** subcommand: launches the Tkinter GUI without
  needing to know the plugin install path.

## What's new in v2.1

- **Anti-patch writes.** Every save goes through `llm.memory_writer.upsert_smart`,
  which MERGES (overwrites a similar memory in place), SUPERSEDES (archives the
  old, links the new via `supersedes_id`), or INSERTS — chosen by trigram-Jaccard
  similarity. No more stacked duplicates. See [docs/CONTRACTS.md#anti-patch-contract](docs/CONTRACTS.md#anti-patch-contract).
- **Forced handoff via PROGRESS.md.** `memory/PROGRESS.md` is the single source
  of truth for session handoff, always full-rewritten from a SQL row, never
  appended. SessionStart emits a `<system-reminder>` block that requires the
  next Claude to Read it before responding. See [docs/CONTRACTS.md#handoff-contract](docs/CONTRACTS.md#handoff-contract).
- **Auto-fresh MEMORY.md.** Regenerated after every write — no more 50-day-stale
  index files.
- **Idle reorg.** Stop hook runs lightweight cleanup every 5 turns (no LLM).
- **Clean subpackage layout.** `cc_memory/{core,hooks,llm,cli,mcp,ui}/`.
- **One installer, one skills location, one version number.** Removed `.claude/skills/`
  duplicate, removed the third copy of `save-memories`, removed dual installers.

## Installation

### Via marketplace (recommended once published)

```bash
claude /plugin marketplace add skymanbp/cc-memory
claude /plugin install cc-memory
```

### Local marketplace from this repo

```bash
claude /plugin marketplace add /path/to/cc-memory
claude /plugin install cc-memory
```

### Standalone exe (Windows)

1. Download `cc-memory-installer.exe` from [Releases](https://github.com/skymanbp/cc-memory/releases)
2. Double-click → Install Plugin → Configure Hooks → done.

### From source

```bash
git clone https://github.com/skymanbp/cc-memory.git
python cc-memory/cc_memory/ui/installer.py        # GUI
# or
python cc-memory/cc_memory/ui/installer.py --cli  # CLI
```

The installer:
0. **Validates `~/.claude/settings.json` before copying anything.** If it cannot
   be parsed (JSONC comments, a trailing comma, a top-level array), it refuses
   with `Nothing has been installed.` rather than leaving a half-installed tree.
   A UTF-8 BOM — what PowerShell's `>` writes — is tolerated.
1. Copies the subpackage tree to `~/.claude/hooks/cc-memory/`. This tree is
   **FLAT**: `core/`, `hooks/`, `llm/`, `cli/`, `mcp/`, `ui/` sit directly under
   that directory, with **no `cc_memory/` path segment**. (The marketplace / dev
   checkout keeps the nested `<plugin root>/cc_memory/…` shape.)
2. Installs the five user-facing surfaces into `~/.claude/`:
   `commands/cc-mem.md`, `agents/plan-refiner.md`, `agents/plan-guardian.md`,
   `skills/ccm-load/SKILL.md`, `skills/save-memories/SKILL.md`. They are
   recorded in `~/.claude/hooks/cc-memory/installed_surfaces.json` and removed
   **by name** on uninstall, so your own files in those directories survive.
3. Adds the hook entries to `~/.claude/settings.json` (6 commands across 5
   events — `PreCompact` declares a sync + an `async` leg), with the same
   timeouts `hooks/hooks.json` declares. A hook of yours that merely mentions
   cc-memory is kept and reported, never deleted.
4. Prunes stale modules from a previous version and auto-detects + upgrades any
   v2.0 flat-layout install. `logs/` is preserved on uninstall.

Per-project initialization is **automatic** — the first user message creates
`<project>/memory/` and the SQLite DB. To opt a directory out entirely, list it
in `config.json`'s `excluded_projects` (see [Configuration](#configuration)).

## Architecture at a glance

```
Hooks (declared in hooks/hooks.json; discovered via the plugin manifest):

  UserPromptSubmit ─► turn count + first-prompt seeding of PROGRESS.md
                      auto-init memory/ on first contact

  PostToolUse     ─► live plan anchor, in EVERY mode: ExitPlanMode → plan_active.raw,
                     TodoWrite → mechanical step sync, edits/sensitive Bash → drift counters
                     insert one observation row per OBSERVED tool call (no LLM)

  Stop            ─► Haiku observer extracts memories from this turn
                     patch_progress(files_touched=...)
                     idle reorg every 5 turns

  PreCompact      ─► fires TWO hooks:
                     • sync  (pre_compact.py, 120s): Haiku extracts memories from
                       a bounded head+tail transcript window (40 records + 32 MiB) → memory_writer.upsert_smart →
                       FULL-REWRITE memory/PROGRESS.md → archive → regen MEMORY.md
                     • async (consolidate_async.py, 300s, off the blocking path):
                       every-Nth-session LLM consolidation under a time budget

  SessionStart    ─► inject context (topics + critical + timeline + PROGRESS preview)
                     record memory/.last_inject.json
                     emit FORCED <system-reminder>: "Read PROGRESS.md FIRST"
                     retroactive save of unsaved prior JSONLs
```

Per-project state lives at `<project>/memory/`:

```
memory/
├── memory.db                SQLite WAL, see core/db.py for schema
├── MEMORY.md                auto-generated index, refreshed every write
├── PROGRESS.md              full-rewrite from `progress` row at every Stop+PreCompact
├── PLAN.md                  full-rewrite from `plan_active` row (live plan anchor)
├── .last_save.json          status from last PreCompact
├── .last_inject.json        what SessionStart injected (observability)
├── .last_consolidation.json interval marker for the async consolidation leg
├── .consolidation.lock      prevents overlapping async workers
├── .pre_compact_attempt.json start marker; survives => last run was killed
├── .plan_raw.md             last raw ExitPlanMode capture
├── .plan_history/           append-only archive of replaced/cleared plans
├── .gitignore               excludes the DB, sessions, and all generated
│                            state above (migrates on every compaction)
├── sessions/YYYY/MM/        per-session archives
└── topics/                  reserved for future per-topic exports
```

## Memory model

| Category | What gets extracted | Default importance |
|----------|--------------------|--------------------|
| `decision` | Explicit choices, design changes | 3 |
| `result`   | Measured outcomes (numbers + units) | 3 |
| `config`   | Hyperparameters, env vars, constants | 2 |
| `bug`      | Identified+fixed problems, "NEVER do X" | 4 |
| `task`     | Pending/blocked work items | 2 |
| `arch`     | Module/pipeline structure, data flow | 3 |
| `note`     | Everything else above noise | 1 |

Importance scale: `1`=noise, `2`=low, `3`=normal, `4`=important, `5`=critical (never forget).

Memory **content** is language-agnostic — the extractor and resume-signal
detectors recognise both English and Chinese by design, and stored memories may be
in any language. Only the project's own docs follow the English-skeleton +
translation convention. See [docs/ARCHITECTURE.md#9-documentation-language-convention-i18n](docs/ARCHITECTURE.md#9-documentation-language-convention-i18n).

## CLI

**Inside Claude Code** (recommended, path-agnostic):

```
/cc-mem status                                    # Full health check
/cc-mem stats                                     # Memory + supersede-chain counts
/cc-mem list decision                             # Recent memories by category
                                                  # (all|decision|result|config|bug|task|arch|note)
/cc-mem search "auth flow"                        # FTS5 search
/cc-mem topics                                    # Topic summaries
/cc-mem progress                                  # Regenerate memory/PROGRESS.md and print
/cc-mem supersedes 42                             # Walk the supersede chain for memory #42
/cc-mem consolidate                               # Full LLM-backed consolidation
/cc-mem cleanup                                   # Lightweight no-LLM cleanup
/cc-mem add decision "Chose X" --importance 4     # Anti-patch upsert
/cc-mem inject-show                               # What SessionStart injected last (ground truth)
/cc-mem inject-usage                              # Whether Claude read PROGRESS.md / MEMORY.md
/cc-mem encoding-check                            # Scan text tables for U+FFFD corruption
/cc-mem dashboard                                 # Launch the Tkinter GUI
/cc-mem serve                                     # Launch the browser-based web viewer

# Live plan anchor (v2.2):
/cc-mem plan-status                               # Counters + freshness summary
/cc-mem plan-show                                 # Regenerate + print memory/PLAN.md
/cc-mem plan-set --raw "Build feature X by ..."   # Capture raw plan, mark needs_refine
/cc-mem plan-set --from-refiner                   # Store structured JSON (stdin)
/cc-mem plan-check                                # Reset counters + emit guardian hint
/cc-mem plan-replan                               # Re-arm needs_refine on stored raw
/cc-mem plan-clear                                # Drop the active plan
```

**Outside Claude Code** (shell, standalone-install path shown — adjust for
marketplace install):

```bash
# NOTE: $HOME, not ~. Bash expands a tilde BEFORE parameter expansion and does
# not rescan the result, so a ~ stored inside a variable stays a literal
# character and `$M status` dies with `can't open file '.../~/.claude/...'`.
M="python $HOME/.claude/hooks/cc-memory/cli/mem.py --project ."
$M status
$M search "auth flow"
# ... same subcommands as above
```

## MCP tools

8 tools exposed via `cc_memory/mcp/server.py`:

| Tool | Purpose |
|------|---------|
| `memory_search` | FTS5 search (compact results) |
| `memory_get_details` | Batch fetch full details by IDs |
| `memory_add` | Add via anti-patch upsert |
| `memory_stats` | Project statistics |
| `memory_topics` | List topic summaries |
| `memory_recent` | Recent memories with filters |
| `progress_get` | Read PROGRESS.md state (structured fields) |
| `progress_regenerate` | Force-rewrite memory/PROGRESS.md from SQL state |

The server speaks JSON-RPC 2.0 over stdio, and forces UTF-8 with LF-only
newlines on both stdin and stdout itself — **no `PYTHONUTF8` /
`PYTHONIOENCODING` env block is needed**.

**Marketplace / dev checkout — nothing to do.** `.claude-plugin/plugin.json`
ships the registration inline:

```jsonc
"mcpServers": {
  "cc-memory": {
    "command": "python3",
    "args": ["${CLAUDE_PLUGIN_ROOT}/cc_memory/mcp/server.py"]
  }
}
```

**Standalone install — register it by hand.** The installer copies the package
and the five surfaces only; it never writes a client config, and the flat tree
has no `.claude-plugin/` to be read. Note the **absent `cc_memory/` segment**:

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

`tools/call` arguments are validated against each tool's advertised
`inputSchema` and rejected with `-32602` rather than coerced, so a malformed
call fails loudly instead of writing something you did not ask for.

## Visual Dashboard

```bash
# Marketplace install or standalone — auto-resolves the plugin path:
/cc-mem dashboard

# Or invoke the CLI directly. Marketplace / dev checkout:
python <plugin-root>/cc_memory/cli/mem.py --project . dashboard
# Standalone install (FLAT — no cc_memory/ segment):
python ~/.claude/hooks/cc-memory/cli/mem.py --project . dashboard

# Or the standalone exe (Windows):
cc-memory-dashboard.exe
```

7 tabs: Memories · Plans · Sessions · Keywords · SQL Console · Stats ·
Progress / Plan (read-only view of the `progress` and `plan_active` rows behind
PROGRESS.md and PLAN.md).

Launching with no `--project` opens nothing: it lists the projects it knows and
waits for you to pick one. Deletions are **archives** (`is_active=0`), and any
non-`SELECT` statement in the SQL console requires a confirmation naming the
statement.

## Web viewer

```bash
/cc-mem serve
# opens http://127.0.0.1:9377 in your browser
```

Browse, search, and **add** memories: the `+ Add memory` form POSTs JSON to
`/api/memory`, which routes through `upsert_smart` like every other save path,
so a near-duplicate merges or supersedes instead of stacking.

Everything written through that endpoint is injected into your next Claude
session, and `/api/sessions` returns whole session rows including
`archive_path`. The server is guarded accordingly:

- binds to `127.0.0.1` only;
- sends **no** `Access-Control-Allow-Origin` header, and rejects any request
  whose `Origin` is not this exact origin (`OPTIONS` answers 405 — no preflight
  is ever granted);
- rejects any request whose `Host` is not this loopback origin. Origin alone
  cannot stop a DNS-rebound page, whose GETs are *same-origin* and carry no
  `Origin` header at all;
- requires `Content-Type: application/json` on POST, which an HTML form cannot
  send without a preflight.

## Plan Queue

Task planning system using the same SQLite DB. This is the plan **queue**
(`plans` table) — distinct from the live plan **anchor** (`plan_active` /
`memory/PLAN.md`, driven by `/cc-mem plan-*`).

```bash
# Console script, if you installed the package with pip:
P="cc-memory-plan --project ."
# Or run the module from a standalone install (FLAT — no cc_memory/ segment).
# Use $HOME, not ~: bash expands a tilde before parameter expansion and does not
# rescan the result, so a ~ inside a variable is never expanded on use.
P="python $HOME/.claude/hooks/cc-memory/cli/plan.py --project ."

$P add "Task A" "Task B" "Task C"
$P list
$P evaluate           # mark draft → evaluating; Claude evaluates feasibility
$P approve --all      # evaluating → ready
$P exec --next        # ready → executing, and print the plan for Claude to run
$P done 1 "Result"    # mark complete
$P status             # queue summary
$P clear              # drop done/failed/skipped
```

`exec` does **not** spawn anything — it flips status and prints the plan text
plus the `done` command to run afterwards. Every subcommand that names plan IDs
resolves them within `--project` first and exits 1 on an unknown or foreign ID.

Status flow: `draft` → `evaluating` → `ready` → `executing` → `done`/`failed`/`skipped`.

## Configuration

Edit `config.json` in your install root — standalone (flat layout):
`~/.claude/hooks/cc-memory/config.json`; marketplace / dev checkout:
`<plugin-root>/cc_memory/config.json`.

**Every key in this file is read by code.** Keys that nothing read were deleted
in v2.5 rather than left in place — an inert tunable is worse than no tunable,
because editing it looks like it does something. What remains:

- `version` — last-resort version fallback for a flat install that has no
  `core/version.py`. `cc_memory/core/version.py` is canonical.
- `consolidation.auto_interval_sessions` — sessions between async consolidation
  runs (default 5).
- `ccl.enabled` / `ccl.ollama_url` / `ccl.local_model` — the local Ollama
  fallback. **Opt-in; `enabled` defaults to `false`**, in which case the
  Anthropic legs are the only backends.
- `excluded_projects` — absolute paths that opt OUT of cc-memory entirely. A
  listed directory *and everything beneath it* gets no `memory/` directory, no
  DB, no observations, no extraction and no PROGRESS.md: **all six hooks, plus
  the MCP server**, call `core.modes.is_excluded(cwd)` as their first act after
  resolving `cwd` and exit 0 (MCP answers `isError`). That is one shared
  implementation, not a copy per caller — v2.5.0 shipped it as two private
  copies in the only two hooks that *create* `memory/`, which left a project
  that was initialised BEFORE it was listed fully instrumented: observations
  kept accumulating, PROGRESS.md kept naming its files, and with a live
  credential the Stop observer kept POSTing them to the Anthropic API. v2.5.1
  fixed the six hooks and missed the seventh caller: **the MCP server had no
  check at all** through v2.5.1 — it is loaded by default from the shipped
  manifest and every call is model-initiated, so a listed project stayed fully
  readable and writable by the model. v2.5.2 gates it in `_get_db`, the single
  choke point all eight tools reach. Matching is on the resolved absolute path,
  case-insensitive on Windows. This is the only opt-out.
  **Since v2.5.2 the parser fails CLOSED**: a `config.json` that exists and
  cannot be used — invalid JSON, not an object, not UTF-8 — excludes *every*
  project and logs the reason, rather than guessing "not excluded" and storing
  data irreversibly. An **absent or empty** config is not that case (no list,
  nothing excluded). The file is read as `utf-8-sig` and a `~user` entry that
  cannot be expanded now degrades to a literal comparison — a BOM (PowerShell's
  `Out-File` default) and one bad `~` entry each used to switch the whole
  opt-out off silently.
- `notes` — in-file documentation, including which module owns each value that
  used to live here.

The removed tunables are module constants; change them there:
anti-patch thresholds in `llm/memory_writer.py`, SessionStart injection budgets
in `hooks/session_start.py`, the idle-reorg interval in `core/idle.py`, the
per-mode observation skip-list in `core/modes.py`, the web viewer's default port
in `ui/web_viewer.py`. MCP registration is the `mcpServers` block in
`.claude-plugin/plugin.json`, not a config key.

## API key

cc-memory auto-detects your Claude OAuth token from `~/.claude/.credentials.json`.
No manual API key setup is needed if you're logged into Claude Code.

Resolution order: `ANTHROPIC_API_KEY` env var → Claude OAuth token.

## Tests

Five stdlib scripts, no pytest and no pip dependencies. **Eight release gates —
run all of them.**

```bash
python -m compileall -q cc_memory tests tools
python -c "import tomllib,pathlib;tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))"

python tests/smoke_test.py
# expect a series of [OK] lines ending with "===== ALL SMOKE TESTS PASSED ====="

python tests/test_plan_carryover.py
# expect "RESULT: 14 passed, 0 failed"

python tests/test_surfaces.py
# expect "===== ALL SURFACE TESTS PASSED ====="   (§1-§6)

python tools/i18n_check.py       # translation drift; nonzero exit on drift
python tools/citation_check.py   # doc file:line citations; "0 unchecked, 0 stale"
```

The eighth gate is version-site agreement: `pyproject.toml`, both
`.claude-plugin/*.json`, `cc_memory/config.json` and `cc_memory/core/version.py`
must all carry the same string, which `smoke_test.py` asserts.

- `tests/smoke_test.py` — the canonical end-to-end check: anti-patch writer
  decisions, PROGRESS.md full-rewrite, the fill-only-empty refresh contract,
  last-wins TodoWrite extraction, the tier-3 transcript fallback, legacy
  `SESSION_HANDOFF.md` migration, the layout inspector, the two-hook PreCompact
  shape, the bounded transcript window, the i18n drift gate, and — since v2.5.2
  — `.gitignore` three-copy parity, the sqlite handle-count regression, PLAN.md
  / MEMORY.md forgery resistance, the single-atomic-writer rule, the
  keyword-only `project_id` on the plan mutators, and the two doc gates.
- `tests/test_plan_carryover.py` — the v2.4.0 carryover gate (20 checks); the
  only coverage of that feature.
- `tests/test_surfaces.py` — new in v2.5, six sections, for the surfaces
  neither of the others touches: §1 the MCP stdio server, §2 the web viewer's
  request guards, §3 the standalone installer (surface install/uninstall by
  name, malformed-`settings.json` handling, hook-timeout lockstep with
  `hooks/hooks.json`), §4 `excluded_projects` across all six hooks, §5 the
  config.json parser shapes plus the MCP half of the same opt-out, §6 the
  `settings.json` compare-and-swap. Plus the rule that every LLM-calling hook
  passes an absolute deadline.
- `tools/i18n_check.py` — translation drift, by normalized content hash.
- `tools/citation_check.py` — every `file.py:LINE` citation in all 13 markdown
  files. `--fix` repairs; `--list` shows every verdict.

Both dev checkers also run *inside* `smoke_test.py`, so a green suite already
implies a green doc state:

```bash
python tools/i18n_check.py --list       # every English/翻译 pair + recorded vs current hash
python tools/citation_check.py --fix    # rewrite stale line numbers in place
```

## Build executables

```bash
pip install pyinstaller
python build_exe.py
# produces:
#   dist/cc-memory-installer.exe
#   dist/cc-memory-dashboard.exe
```

## Requirements

- Python 3.8+ (stdlib only — no pip dependencies at runtime)
- Claude Code with hooks support
- PyInstaller (only for building the exe, not for running)
- On Windows: ensure `python3` resolves to a Python 3 interpreter, since
  `hooks/hooks.json` invokes `python3` and the python.org installer does
  not provide `python3.exe` by default. The simplest fix is to symlink or
  shim `python3` to `python` on PATH.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — full architecture overview
  ([简体中文](docs/ARCHITECTURE.zh.md))
- [docs/CONTRACTS.md](docs/CONTRACTS.md) — the three hard contracts
  ([简体中文](docs/CONTRACTS.zh.md))
- [docs/CONTRACTS.md#anti-patch-contract](docs/CONTRACTS.md#anti-patch-contract) — anti-patch write contract
- [docs/CONTRACTS.md#handoff-contract](docs/CONTRACTS.md#handoff-contract) — PROGRESS.md spec
- [docs/CONTRACTS.md#plan-contract](docs/CONTRACTS.md#plan-contract) — PLAN.md + subagent spec
- [docs/ARCHITECTURE.md#9-documentation-language-convention-i18n](docs/ARCHITECTURE.md#9-documentation-language-convention-i18n) — documentation multilingual (English / 中文) version-control
- [CHANGELOG.md](CHANGELOG.md) — version history

## License

MIT
