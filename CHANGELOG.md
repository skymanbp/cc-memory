# Changelog

All notable changes to cc-memory are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.11.4] — 2026-08-17

### The eleventh gate: is it written down at all?

v2.11.3 fixed an undocumented design **by hand** and recorded the class as open:
*"no gate detects an undocumented design."* This release closes it.

### Added

- **`tools/doc_coverage.py`** — release gate #11. The other three doc gates all
  verify the documentation that **already exists**: a `file.py:LINE` citation
  still points at its symbol, a sentence that COUNTS something matches the tree,
  a translation is bound to a hash of its source. None of them asks whether a
  new public surface produced any documentation at all — which is why v2.11.2's
  two schema columns appeared **0 times** in the specification while all ten
  gates passed.

  It enumerates four surfaces **from the code** and requires the document that
  owns each one to name every member — in **both** language siblings, because a
  Chinese reader following the same specification must not be reading a shorter
  one:

  | surface | enumerated from | must appear in |
  |---|---|---|
  | schema tables | `CREATE TABLE` in `core/db.py` | `docs/ARCHITECTURE.md` (+`.zh`) |
  | schema columns | `ALTER TABLE … ADD COLUMN` | `docs/ARCHITECTURE.md` (+`.zh`) |
  | MCP tools | `mcp/server.py` advertised schemas | `README.md` (+`.zh`) |
  | config keys | `cc_memory/config.json` leaves | `README.md` (+`.zh`) |

  38 members, 76 document checks. Columns declared inside the original
  `CREATE TABLE` are covered by the table itself; an `ALTER` is the shape that
  arrives **later**, which is exactly when documentation is forgotten.

  Falsified against the real history rather than a constructed case:
  `falsify --case r11doccoverage` reverts the sentence v2.11.3 added by hand and
  the gate goes red. Registering it also caught that the first breakage was too
  small — `turns_total` appears twice in that document, so removing only its
  definition left the word present and the case ran GREEN. A substring check is
  falsified only by removing every occurrence.

- The gate-script list `tests/smoke_test.py` asserts is now **derived on both
  sides**: every `tools/*.py` is a gate except the two the docs explicitly call
  "not a gate" (`contracts.py`, `falsify_fixes.py`). It previously spelled out
  `tools/doc_claims.py` by hand — a hand-kept list of the scripts that check
  hand-kept lists — and `doc_coverage.py` would have had to be added beside it.

### Deliberately not checked, measured rather than assumed

- **Migration KEYS against `CHANGELOG.md`.** Measured before scoping the gate:
  **27 of 29** keys are absent from it. Requiring them would be a 27-item red
  gate whose only remedy is rewriting history entries, and this project holds
  that a history edited to stay current is not a history.
- **Whether the prose is CORRECT.** This gate answers "is this surface
  mentioned at all". `doc_claims` covers counted assertions and
  `citation_check` covers pointers; whether the described *behaviour* is right
  is not mechanical, and a green run should not be read as claiming it.

### Changed

- Every live "ten gates" claim became eleven — README badge and gate list,
  `CLAUDE.md`, `CONTRIBUTING.md`, the PR template, and both CI job names.
  Sentences describing what was true at an earlier release keep their original
  number; a history edited to stay current is not a history.
- `CONTRIBUTING.md` gains the rule the gate cannot enforce: **a new invariant
  goes in `docs/CONTRACTS.md`, not only in `CHANGELOG.md`** — the person about
  to break it is reading the specification, not the release history.

---

## [2.11.3] — 2026-08-17

### The gates were green and the specification was silent

v2.11.2 changed how directive idleness is measured — a schema migration with a
load-bearing rule attached — and all ten gates passed. They check that a
`file.py:LINE` citation still points at its symbol, that a sentence which
COUNTS something matches the tree, and that each translation is bound to a hash
of its source. **None of them asks whether a new design was written down at
all.** Measured after the fact: `turns_total` / `turns_at_touch` appeared 3× in
`CLAUDE.md` and 2× in this changelog, and **0×** in `docs/CONTRACTS.md`,
`docs/ARCHITECTURE.md`, `commands/cc-mem.md` or either Chinese sibling.

A contract that lives only in a changelog entry is a contract the next change
will break, because the person about to break it will be reading the
specification.

### Changed

- **`docs/CONTRACTS.md` § Plan contract** gains directive idleness as a fourth
  load-bearing property of a Stop refusal: it is
  `plan_active.turns_total - directives.turns_at_touch`, and must NEVER be
  measured against `turns_since_last_guardian`, which `/cc-mem plan-check` and
  every plan replacement zero. Both earlier shapes are recorded there with why
  each looked right, and the rule that the stamp is written inside
  `upsert_directive` / `set_directive_status` rather than supplied by callers.
- **`docs/ARCHITECTURE.md` § Database schema** documents both v9 columns in the
  same "carries X since migration Y" form the `projects` and `sessions` rows
  already use, naming `turns_total` as monotonic and distinguishing it from the
  resettable drift counter.
- **`commands/cc-mem.md`** states what "idle" counts for a *user*: turns since
  that directive was last written; re-stating or closing it restarts the clock,
  `/cc-mem plan-check` does not.
- Both `.zh.md` siblings updated to match, markers regenerated.
- **`README.md` no longer claims cross-platform support "by construction".**
  That phrasing described an intention, not a measurement. It now states what
  CI actually runs — all ten gates on Windows and on Linux (3.11, 3.13) — and
  says plainly that macOS is unmeasured rather than implying otherwise.

### Known limits

- macOS has no CI coverage. It is expected to work (the same POSIX paths the
  Linux job exercises) and that expectation is not evidence; the documentation
  now says so instead of rounding it up to "cross-platform".
- No gate detects an undocumented design. This release fixed the instance by
  hand; the class remains open, and the honest description of it is that
  documentation completeness is still a human responsibility here.

---

## [2.11.2] — 2026-08-17

### The debts v2.11.1 recorded, paid — including the one it had approximated

v2.11.1 closed six defects and then wrote down three things it had *not*
closed. This release closes all three. One of them was not merely deferred: it
was a fix that looked complete and was not.

### Fixed

- **Directive idleness was measured against a counter that RESETS.**
  v2.11.0 stamped every active directive with the project's
  `turns_since_last_guardian`, so one recorded ten seconds ago was announced as
  "no progress for 40 turns" and refused the user's turn. v2.11.1 replaced that
  with a "has it been touched since the guardian window opened?" guard, which
  killed the false positive — and inherited a worse one from the counter it
  still read. `/cc-mem plan-check` and every plan replacement zero
  `turns_since_last_guardian`, so a directive genuinely untouched for 30 turns
  looked freshly attended to the moment anybody ran a guardian check. **The
  ledger forgave exactly the neglect it exists to surface**, and it did so
  silently, because "no directive is idle" is indistinguishable from "the
  ledger is working".

  A resettable counter cannot measure elapsed neglect; the answer is a clock
  that never resets, not a cleverer comparison against one that does. Schema
  **v9** adds `plan_active.turns_total` — incremented by
  `bump_plan_turn_counter` alongside the drift counter, reset by nothing — and
  `directives.turns_at_touch`. Idleness is now `turns_total - turns_at_touch`:
  subtraction between two monotonic numbers. Both columns DEFAULT 0, so an
  upgraded database reads every existing directive as touched at turn 0 — as
  old as the project, the safe direction for a ledger whose job is to notice
  neglect.

  The stamp is read inside `upsert_directive`'s and `set_directive_status`'s own
  `BEGIN IMMEDIATE`, not passed in by callers: every caller would otherwise have
  to know that idleness is counted in plan turns, and the one that forgot would
  write a row that could never be seen as idle. A status change stamps too —
  reopening a closed directive used to produce a row instantly "idle" by however
  many turns had passed while it was closed.

- **`.pytest_cache/`** removed — a stray directory in a project whose
  contributing rules document "no pytest, no pip dependencies".

### Changed

- **Linux runs all ten gates.** The workflow ran `--fast` on Linux behind a
  comment asserting `smoke_test` / `test_surfaces` were Windows-specific: an
  assumption, never a measurement, and it left the largest unknown in the
  project unmeasured — whether cc-memory works on Linux at all. Both suites now
  run there on 3.11 and 3.13, with `python3-tk` (the one real dependency they
  need; everything else is standard library).
- `tests/test_directive_enforcement.py` is **53 checks**, up from the 27 the
  v2.11.0 entry records for that release.

### Added

- Falsification cases `r11resetforgives` (a guardian check must not forgive an
  idle directive) and a re-anchored `r11idle` (idleness is per-row, not the
  project clock), both driven RED individually. Register 160 → 161.

---

## [2.11.1] — 2026-08-16

### The release that shipped with a red gate, and the engine nobody drove

v2.11.0 was tagged while `tests/smoke_test.py` was **failing**: the directive
ledger added `directive-add` / `directive-close` / `directive-list` and
`commands/cc-mem.md` was never updated, which its own doc-facts assertion
checks. Worse, `main()` is one sequential function, so that first failing
assert also **hid** the assertion below it — `core/db.py` had created **12**
tables since `v8_directives` while three documents still said eleven. Two gate
failures, one visible, neither caught, because "run all ten gates" was prose in
`CLAUDE.md` rather than an executable.

A seven-scope disjoint audit with adversarial verification then found that the
v2.11.0 enforcement engine — the code that can **refuse to end a user's turn** —
had zero test coverage of its own. The `[True, True, True, False, False]`
evidence the entry below cites was real, and it exercised only the path where
the marker is writable.

### Fixed — the enforcement path

- **The escape budget could never release, trapping the session.**
  `core.markers.write_marker` **never raises** (first line of its docstring);
  all three failure paths `return False`. `hooks/stop.py:_block_attempt` guarded
  the "cannot persist the count, so advise instead of blocking" case with
  `except OSError` — dead code — and discarded the return value. On any marker
  directory `core.markers` refuses (a mode-1777 temp root, a planted reparse
  point, a read-only temp), nothing persisted, every read came back empty, `n`
  stayed 1, and the hook refused forever. Measured `[1,1,1,1,1,1,1,1]` over
  eight consecutive Stops; after the fix, `[None × 5]` (degrades to advisory)
  while the healthy path is unchanged at `[True, True, True, False, False]`.
- **A stored directive reached Claude as a LIVE authority marker.**
  `blocking_reasons` interpolates a directive's `slug` and `demand` into the
  block text, and `render_block_reason` was the only renderer in `core/plan.py`
  that did not neutralize — while `hooks/stop.py:_emit_block` hands the result
  to the harness as `{"decision": "block", "reason": ...}`, a higher-authority
  channel than PROGRESS.md. Reproduced: one forged `<system-reminder>` per
  rendered directive. Escaped on **both** sides now — `db.upsert_directive`
  routes `quote`/`demand`/`evidence` through `clean_for_storage`, and
  `render_block_reason` ends with `neutralize_document`.
- **A refusal's stdout was not a JSON document.** An unconditional per-turn
  status line printed to the same stream before the decision object. The status
  line is now built first and emitted only on the paths where the turn closes.
- **A cleared plan enforced forever.** `clear_plan_active` keeps a tombstone row
  on purpose (it is what keeps `revision` monotonic across clears and closes the
  CAS ABA window); the hook tested the row's truthiness, so a project whose plan
  the user had explicitly dropped kept accruing turns and being refused every 8.
  `core.plan.is_live_plan` is now the named predicate — **named deliberately**,
  because a test can only re-implement an inline condition and a
  re-implementation passes whatever the hook does (proved: `falsify --case
  r11tombstone` ran GREEN until the predicate existed).
- **A just-stated directive was reported idle.** `_idle_directives` stamped
  every active row with the PLAN's guardian counter, so a directive recorded
  seconds ago was announced as "no progress for 40 turns" and blocked the turn.
  It now requires the directive to be untouched since the guardian window
  opened, comparing with `>=` because `MemoryDB._now()` stamps **whole
  seconds** — a strict `>` reproduced the false block, caught by the new gate
  rather than by review.
- **Re-stating a directive erased it.** `cli/mem.py`'s argparse defaults are
  `''` / `'standing'`, not `None`, so a bare `directive-add <slug>` passed three
  non-None values and wiped `demand` and `quote` and reset `kind` — the single
  operation the ledger exists for. Only supplied flags are forwarded now.
- **Concurrent directive creates raced.** `sqlite3` takes no write lock for a
  SELECT, so two creators of one slug both saw no row and both INSERTed; the
  loser died on `idx_directives_slug` out of a hook, and `times_stated` is
  exactly the counter a lost write corrupts. `BEGIN IMMEDIATE`, the same idiom
  `reconcile_upsert` uses. Measured: 8 concurrent creators → 0 exceptions, 1
  row, `times_stated = 8`.

### Fixed — packaging and repository integrity

- **`cc_memory/hooks/_entry.py` was absent from `cli/mem.py`'s
  `_REQUIRED_PLUGIN_FILES`**, so `/cc-mem status` certified an install where all
  six hooks die at import as healthy. This was the **third** recurrence
  (`core/roots.py`, then `core/markers.py`), and the list's own comments
  predicted each next one without preventing it — so the requirement is now
  **derived**: `smoke_test.py` walks the hooks' module-level import graph with
  `ast` and asserts every reachable module is listed.
- **A `.gitignore` blanket `.*/` silently removed `.github/` from the
  repository** — zero tracked files, invisible to `git status`, so the
  release-gate CI would never have been committed — and matched
  `.claude-plugin/` too, where the two existing files survived only because they
  were already tracked. Re-included by negation and gated: eight shipped paths
  must stay tracked-able, two private paths must stay ignored.
- `MemoryDB.is_duplicate_hash` deleted — zero callers repo-wide, and its
  signature still advertised the check-then-write shape the anti-patch
  transaction removed.

### Added

- **`tests/run_gates.py`** — one command runs all ten gates, prints a table and
  exits nonzero on any red. `--list`, `--only`, `--fast`. The gate COUNT in
  `CLAUDE.md` is now asserted against `len(GATES)` instead of typed, and every
  suite/checker on disk must appear both in that list and in the runner.
- **`.github/`** — `workflows/gates.yml` (all ten gates on Windows, the
  platform-independent subset on Linux, plus `falsify_fixes --anchors`), issue
  templates and a PR template.
- **`CONTRIBUTING.md`** and **`SECURITY.md`**.
- **`tests/test_directive_enforcement.py` §5** — checks over `_block_attempt`,
  `_emit_block`, `_idle_directives`, `is_live_plan`, the write path and the
  CLI, all of which previously had **zero** executable coverage.
- **Nine falsification cases** (`r11budget`, `r11blockmarker`, `r11idle`,
  `r11tombstone`, `r11directiverace`, `r11restate`, `r11gitignore`,
  `r11flattree`, `r11entryreq`), each driven RED individually. Two ran GREEN
  first; the **checks** were fixed, not the cases. Register 151 → 160.
- A gate asserting the flat-install tree diagram names every shipped module in
  both language siblings — it had fallen six behind.

### Changed

- **`build_exe.py` moved to `scripts/build_exe.py`**; `ROOT` resolves to the
  checkout rather than the script's directory, and `smoke_test.py` asserts no
  copy remains at the repository root.
- **README rewritten** — 1228 → ~690 lines. The reverse-chronological release
  archaeology (640 lines, 52% of the file) moved out to this changelog, which
  already carried all of it; what replaces it is a table of contents, a
  quickstart, a feature index in eight categories, full CLI/MCP/config
  reference, and a troubleshooting table. `README.zh.md` rewritten to match.
- **Discoverability**: GitHub topics 0 → 20, a new repository description and
  homepage; `pyproject.toml` keywords 9 → 24 and classifiers 7 → 18; both
  plugin manifests carry 22 keywords.
- `docs/ARCHITECTURE.md` no longer stamps a version into its title — it read
  "(v2.9.0)" through two releases, and a heading is not a countable claim, so
  nothing gated it.
- `docs/CONTRACTS.md` § Plan contract said the Stop hook "NEVER" emits anything
  but an advisory status line, and cited a symbol the hook no longer calls. It
  now specifies the three load-bearing properties of a refusal.
- `LICENSE` names its copyright holder.
- `CLAUDE.md`'s citations to `memory/*.md` registers are marked
  maintainer-local: `/memory/` is git-ignored by design, so no clone has them.

---

## [2.11.0] — 2026-08-15

### Advisory became enforced, because advisory did not work

The whole plan subsystem was a suggestion. `hooks/stop.py` said so in its own
comment — *"The plan-refiner nudge is advisory"* — and rate-limited that
suggestion to once per five turns on top.

What that cost, measured in a real consuming project on 2026-08-15: a
**51,237-character raw plan sat unrefined** while `PLAN.md`, `plan-status`
**and the drift guardian** all answered from the PREVIOUS plan. The guardian
was faithfully drift-checking against a superseded baseline — the one job it
exists to do, performed against the wrong document. A full-transcript audit of
**416 deduped user messages** then found a feature demanded **six separate
times** with zero implementation, and a pause rule stated **three times** that
was violated the first time it mattered. Nothing detected any of it, because
nothing was ever forced.

### Added

- **`directives` table (schema v8) + `directive-list` / `directive-add` /
  `directive-close`.** A ledger of what the USER asked for, deliberately
  separate from plan steps: a step is a unit of EXECUTION and dies when the
  plan is replaced or the step is marked done, while a directive is a unit of
  INTENT that outlives every plan. Folding them together is precisely how the
  six-times-repeated demand vanished — it was never a step in whichever plan
  happened to be active. `times_stated` accumulates on ONE row, because
  repetition is the importance signal a plan cannot express.
- **`directive-close` refuses without `--evidence`.** A directive closed on an
  assertion is the exact failure the ledger exists to prevent.
- **Stop enforcement**: `core.plan.blocking_reasons` +
  `hooks/stop.py:_emit_block` emit `{"decision": "block", "reason": ...}` when
  a plan is unrefined, has gone undrift-checked, or an active directive has sat
  idle past the threshold.
- **A guaranteed escape from that enforcement.** After
  `_BLOCK_MAX_CONSECUTIVE` refusals of the *same condition set* it degrades to
  a loud advisory; `_block_attempt` keys the counter by a digest of the
  condition keys, so fixing one problem never spends the budget of the next.
  An unbreakable block is worse than no block. Kill switch:
  `CC_MEMORY_PLAN_ENFORCE=0`.
- **`tests/test_directive_enforcement.py`** — 27 checks across the ledger, the
  blocking predicate, the kill switch and the escape budget, plus a live hook
  drive proving the wire format reaches the harness and that the budget really
  releases (`[True, True, True, False, False]` over five consecutive Stops).

### Changed

- Projects with **no plan row are never enforced**, so opting into planning is
  what turns enforcement on; every other project on the machine is untouched.
- `cc_mem_block_` registered in `ui/installer.py`'s temp-marker sweep list —
  every prefix a hook writes must be listed there or those files leak forever.

### Removed

- `_claim_refine_nudge` and its two constants. A rate-limited advisory is what
  let a plan sit unrefined indefinitely; leaving the helper would keep a
  second, unreachable policy in the tree. Its `cc_mem_refine_` prefix stays in
  the uninstall sweep list so older installs still get cleaned.

---

## [2.10.1] — 2026-08-10

### The three items v2.10.0 recorded as open, closed

- **The dashboard's highest-complexity logic is now executed by a gate.**
  `_scan_project_deep` (cx 100) was already a pure module-level function and
  already driven by §8; the other two monsters' cores are now pure
  staticmethods — `DashboardApp._render_progress_plan` (the Progress/Plan
  tab's cx-54 text builder, register-E3 escaping included) and
  `DashboardApp._normalize_tidy_verdict` (the tidy callback's cx-heavy LLM
  verdict normaliser) — extracted behaviour-preserving, with the Tk callbacks
  keeping only widget plumbing and dialogs. §8 drives both: hostile stored
  markers must come out escaped, empty rows must render placeholders, and the
  three LLM shapes measured live pre-v2.9.0 (`[1,2,3]`, `{"id":"abc"}`,
  `delete_ids:[null]`) plus the keep==delete refusal and the unknown-id
  filter all hold. `falsify --case r10dashrender` un-escapes the renderer on
  a copy and §8 goes RED (the first draft of that breakage modelled the
  counterfactual backwards — `raise ImportError` lands in a fallback that
  ALSO escapes — and was rewritten per round 9's lesson 1). What remains
  uncovered is stated: the Tk event/dialog shells, which now hold no logic.
- **The contracts registries are fail-loud about their proxy.**
  `_verify_entry_gate` (same pattern as `_BACKSTOP_CREATORS`): if
  `hooks/_entry.py` stops consulting `is_excluded` before `project_root`,
  the opt-out and anchoring registries raise instead of keeping six hooks
  listed as protected. `falsify --case r10gateproxy` guts the gate and
  `doc_claims` goes RED (verified). The v2.10.0 ledger entry recording this
  as an accepted risk is retired.
- **The codex confirmation verdict is in: CONFIRMED-CLOSED.** The follow-up
  had been queued behind a zombie — the FIRST review run, wedged for over an
  hour after its file reads with no report. Killed it, re-dispatched the
  one-question confirmation fresh: the Q2 guard closes the finding, encloses
  only the diagnostic call, and changes no control flow or return value
  (three file:line citations).

Falsify registry: 149 → **151** cases, anchors 151/151 intact, both new
cases verified RED individually.

---

## [2.10.0] — 2026-08-10

### An anti-bloat architecture round: measure first, mechanise the one real duplication

The brief was to re-read everything since v2.5 and answer one question: had
five convergence rounds of fixes turned into patch-on-patch bloat? The answer
came from measurement, not impression. A stdlib-`ast` sweep of every function
in `cc_memory/` + `tools/` against the v2.5.0 baseline (487 → 818 functions,
12,514 → 20,836 function-LOC, per-function cyclomatic complexity ranked)
showed the growth is overwhelmingly *mechanism* — `core/atomic.py`,
`core/textsim.py`, `core/markers.py`, `core/roots.py`, the snapshot-verdict
guards in `core/db.py`, and the three gate tools — each line traceable to a
measured defect. One structural duplication survived the review:

- **The six hooks each hand-rolled the same entry ladder** — stdin read →
  JSON parse → object check → `is_excluded` on the RAW cwd → `project_root`
  anchor — ~350 lines of six-way copies with the guard comments pasted
  verbatim ("json.loads SUCCEEDS on well-formed non-object payloads" ×5,
  "Anchor AFTER the opt-out" ×6). Every drift between those copies has
  shipped as a defect: v2.7.0's release theme was rungs that missed a guard,
  and v2.9.0's junk-cwd database plant was a missing `isinstance` rung in
  exactly one hook. The ladder now lives ONCE in **`hooks/_entry.py`**:
  `parse_payload()` (with `replace_errors` carrying PostToolUse's deliberate
  lossy-decode) and `resolve_project()` (which owns the opt-out→anchor
  ORDER). Per-hook field policies stay per-hook — coerce vs abort,
  pre_compact's NUL check, SessionStart's `config_fault` visibility — the
  same mechanism/policy split `cli_opt_out_notice` gave the three CLI
  surfaces in v2.7.0.

What holds it in place:

- `tests/test_surfaces.py` §4 gained the **narrow-exclusion drive**: a listed
  subdirectory INSIDE a live project, driven through all six hooks, asserting
  its activity is recorded nowhere — not as a stray `memory/` and not in the
  parent's database. That is the direction an anchor-before-opt-out inversion
  widens away, and it was behaviourally untested before this round.
- §7's source rule now asserts the ORDER once, inside the gate itself, and
  refuses a direct `is_excluded` / `project_root` import in any hook.
- `tools/falsify_fixes.py --case r10entryorder` inverts the order inside the
  gate on a temporary copy and the suite goes RED (verified); `r9bigstdin`
  re-anchored onto the shared read, RED at its new anchor (verified). Anchors
  148/148 intact.
- `tools/contracts.py` counts `resolve_project` for the opt-out and anchoring
  registries, with `hooks/_entry.py` excluded as the implementing module —
  both registries report the SAME 12 members as before the refactor.

Reviewed and deliberately NOT refactored (dispositions in
CLAUDE.md § v2.10.0): `pre_compact.main`'s linear pipeline, the `db.py`
snapshot-verdict cluster, `_refresh_progress_row`'s three-tier fill, and the
dashboard's three cx-47..100 functions (zero executable coverage — refactoring
an untested 2.9k-line GUI is the failure mode this round exists to avoid).

Net: six hook entries shrank by the ladder; the one new module carries the
mechanism plus its documentation; behaviour pinned by the 48-pair junk-cwd
probe, §4 (now four exclusion shapes), §7, and the full falsify registry.

---

## [2.9.0] — 2026-08-09

### A dual-perspective review: two independent readers, 18 defects, 18 repros

No cc-tree this round. Two reviewers with **disjoint file sets and different
angles** read the shipped v2.8.0 tree at the same time: a six-scope fan-out of
my own (db/writer · hooks · mcp/cli · ui · core · tools/tests, severe findings
put through adversarial refutation) and an independent read-only pass by codex
over the whole runtime package. 12 candidates survived refutation, 17 more came
back below the severity bar, codex returned 3 — and **every single one was
reproduced here before it counted**. Two were refuted and dropped: a
"reconcile_upsert caps its candidate set" claim that misattributed a pre-existing
bound, and a "SessionStart tier-3 runs unbudgeted" claim whose structural half
was right and whose consequence did not follow.

The fixes, by what they protect:

**Data you would have lost.**

- **`archive_obsolete` DESTROYED an existing supersede link.** A loser produced
  by an earlier SUPERSEDE already points at the row it replaced; the write was
  an unconditional `supersedes_id = ?`, so chain `[2,1]` became `[2,3]` and the
  original wording was unreachable from every walk — while `/cc-mem supersedes`
  printed the result under the label "newest first". Now `COALESCE`: the slot
  keeps the FIRST lineage fact it learns and the second is logged rather than
  written over the first.
- **`patch_progress` bootstrapped across three transactions.** A read, a
  conditional `upsert_progress`, then the UPDATE — each on its own connection.
  Two hooks first-touching the same project interleaved and B's stale "row
  absent" verdict replayed the default row over A's landed patch (measured:
  `current_request` came back `''`). One `BEGIN IMMEDIATE` with
  `INSERT OR IGNORE` now; 200 concurrent first-touch pairs lost 0 fields.
- **MEMORY.md's ordering probe was blind inside one second.** The
  moved-under-us fingerprint was row counts + `MAX(id)` + `MAX(updated_at)`,
  and `_now()` stamps whole seconds, so an in-place UPDATE in the same second
  changed none of the three: a stale render was accepted as current and written
  over newer state. Replaced by `PRAGMA data_version` read twice on ONE held
  connection.
- **`merge_near_duplicates` archived on the authority of a row it was
  archiving.** Jaccard is not transitive; the inner loop kept comparing an
  anchor already condemned in the same pass, so a memory left the active set
  with no surviving near-duplicate (measured at 0.61 against the actual
  survivor, under the 0.65 threshold).
- **One malformed TodoWrite entry cost the entire compaction.** A `content` of
  `null` or a number raised `AttributeError` out of `build_extraction` — taking
  the session archive, the batch upsert AND the PROGRESS.md handoff with it.
  Same for a non-dict `message`. Both are typed gates now, matching the guard
  `_decode_records` already had one level up.

**Data that reached the wrong project, or the wrong person.**

- **Four `/cc-mem` commands ignored project scope**, in a database file that
  legitimately holds several projects. `encoding-check` counted every project's
  rows and `--apply` **archived** them by bare id; `supersedes` printed another
  project's full memory into the Claude session; `sessions` and `keywords`
  listed the other project's rows — archive filenames included — under this
  project's heading. `cmd_archive` had carried the guard the whole time.
- **A reinstall DELETED a user's own hook.** The install path judged ownership
  per matcher GROUP, so any user entry sharing a group with ours vanished with
  rc=0 and no warning. Register Y2 fixed exactly this for the uninstall path and
  left the install path on the old shape; both are per-ENTRY now.
- **The settings.json compare-and-swap was disarmed on a fresh machine.**
  `_settings_fingerprint` returned `None` for an absent file and both halves of
  the guard are gated on `expect is not None`, so "I expect no file" was
  conflated with "I have no expectation" — a settings.json Claude Code created
  inside the write window was destroyed with rc=0. Now an `_FP_ABSENT` sentinel.
- **PLAN.md forged whole document sections.** Two model-authored slots — a
  superseded plan's `goal` and `refined_by` — were interpolated raw, and an
  embedded newline produced a second complete "Pending refinement" block or a
  second `## Goal`: an attacker-chosen "current plan" inside the file Claude
  reads as the live anchor.
- **An empty prompt left the PREVIOUS turn's request** in the per-session
  marker that the Stop observer splices verbatim into its Anthropic request, so
  the memories it wrote were attributed to a different turn. The marker write
  now sits above every truthiness test, which is what the code's own comment
  had always claimed.

**Things that silently stopped working.**

- **PostToolUse discarded any tool event over 512 KiB.** It was the only hook
  reading a stdin PREFIX; a larger payload truncated mid-JSON, the parse raised,
  and the silent handler dropped the observation row **and** the
  mode-independent live-plan block — rc=0, empty stderr, no log line. A 600 KiB
  `Read` result reaches it; a `package-lock.json` is routinely that size.
- **The Windows junction defeated both fail-closed link guards.**
  `stat.S_ISLNK` is False for a `mklink /J` reparse point (no admin needed), so
  `ensure_memory_dir` accepted a junctioned `memory/` and created `.gitignore`,
  `sessions/` and `topics/` inside the junction target, and `roots._has_db`
  adopted the directory as a project root through it. Both now use
  `core.markers._is_link`, which this package already had.
- **The web viewer could be locked out indefinitely** by 16 connections
  dripping an unfinished header block: the deadlines covered only the request
  BODY, and the handler's own `timeout` is per-recv, so every byte reset it
  while the connection held one of the 16 admission permits. A 10 s absolute
  header budget closes it (measured: recovery at t+10.1 s, from "no recovery,
  ever"). The 503 shed reply was also being discarded by a TCP reset — the
  socket was closed with the peer's request unread — so it now half-closes and
  drains first (30/30 probes received the 503, from 26/30).
- **MCP answered frames with no `jsonrpc` member**, and frames claiming
  `"1.0"`, as ordinary Requests. JSON-RPC 2.0 §4 requires exactly `"2.0"`;
  they get `-32600` now.
- **`unmatched_criteria` judged CJK criteria on the ASCII bar.** Bigram
  shingles score a one-character Chinese substitution at 0.5556, so the flat
  0.5 threshold called a REPLACED criterion "carried" while the steps gate
  refused the identical pair at 2/3 — the two halves of one replacement
  disagreeing.
- **`_merged_tags` exploded a bare string** into one tag per character.

**The gates themselves — five holes, found by turning the review on them.**

- **The citation gate was `.py`-only**, silently exempting 25 citations in the
  tracked docs from the invariant "no citation may be UNCHECKED". Two were
  already rotten in exactly the way the bounds branch exists to catch. Now
  623/623 checked, 0 skip.
- **One modifier word between a number and its noun defeated every
  `doc_claims` trigger** — the commonest English shape. Two live claims about
  the hooks contract were unbound and unchecked; the new pattern (one word,
  plural noun, measured at 5 matches and 0 false positives across every scanned
  surface) caught them plus two more the moment it landed.
- **`tools/contracts.py` under-counted the marker defence by one**:
  `neutralize_document` was missing from the render-path probe, and an
  `import ... as` alias was invisible from both ends. `render_paths` was 6 with
  7 in the tree — the same N+1 disease its own comments record recurring
  "inside its own cure".
- **`verify_anchors` caught only `SystemExit`**, so a rotted anchor of the four
  hand-written kinds killed the whole scan with a traceback and no summary.
  Both handlers name `(Exception, SystemExit)` now — `SystemExit` is a
  `BaseException`, which is why naming one is not naming the other. It proved
  itself immediately: four anchors this round's fixes moved were reported
  together instead of one at a time.
- **`_HOOK_ORDER` was a hand list bound to nothing.** It is the sole
  enumeration behind the opt-out gate, the subdirectory test, the
  is_excluded-then-project_root rule and the junk-cwd probe, so a seventh hook
  would have been covered by none of them while the banner still said "all 6".
  It is asserted equal to the computed `hooks` contract now.
- **The third release gate ran outside a sandbox and never cleaned up** —
  `Path.home()` stayed the real one and each run leaked two project
  directories into the real `%TEMP%`. Found: 270 of them, 42 MB, removed.

### Verification

Nine gates green in one run. `tests/smoke_test.py` gained a §9 block and
`tests/test_surfaces.py` a §9 section (both named for this round); the
falsification register went **127 → 147 cases**, every new one driven RED
individually — including one that had to be rewritten after it ran GREEN,
because it modelled "the probe always fires" instead of "the probe is blind".
`--anchors` 147/147. Recorded coverage gaps, including the ones this round did
NOT close, are in `memory/falsify-coverage.md`.

Release assets, for the first time: both PyInstaller executables and a
`SHA256SUMS.txt`.

---

## [2.8.0] — 2026-08-09

### Round 8 — the radial audit turned on round 7's own fixes

A second cc-tree pass, prompted by one observation: this project's own
SessionStart banner rendered as `&#61;&#61;&#61; CC-MEMORY...` — round 7's
assembled-sweep fix was eating its own frame. 27 candidates, 18 survived
adversarial refutation, 13 fixed; every one reproduced here first.

- **The injection swept its own banner away.** The sweep ran over the whole
  joined document, header and terminator included. The header and tail are
  now emitted outside the swept body, and the gate asserts on
  `build_context()`'s OUTPUT — the previous assertion read `_build_footer()`'s
  return value, a string from *before* the sweep, and stayed green through
  the regression.
- **`neutralize_markers` peeled ONE nesting level** (`_MARKER_TAG_RE`'s body
  is `[^<>]*>`); depth 2 survived a full render. Now a bounded fixed point
  (`_MAX_MARKER_PASSES = 8`) that escapes the whole document wholesale if
  anything still matches past the bound — fail closed, never fail quiet.
- **The harness strip inherited `<private>`'s fail-closed tail** and cut an
  ordinary user question at the tag (77 chars stored as 34). Harness blocks
  are the OPPOSITE case: an unpaired open is emitted as literal text and the
  render side escapes it; `<private>` keeps failing closed, asserted in the
  same block so the two halves cannot drift.
- **A corrupt FTS index in ONE handle issued DDL that unindexed every other
  handle's writes.** `_disable_fts5` drops triggers only for "this sqlite has
  no fts5 module"; a per-connection failure now degrades that connection
  alone, and an EMPTY MATCH against a triggerless index is not trusted — it
  falls through to LIKE instead of reporting a just-written row missing.
- **`''` is not a session identity.** `get_recent_sessions` deduped on
  `IS NULL` only, and `pre_compact` writes `''` when the harness supplies no
  id — five independent compactions collapsed into one timeline entry.
- **The observer watermark moved into `projects.obs_watermark`** (v7
  migration): durable, per-project, seeded at the active end of the queue
  (cold start fed 40, not the whole backlog), advanced with a SQL-level
  `MAX` so a slow session cannot rewind it.
- **Two v7 indexes** turned measured quadratics linear:
  `get_recent_sessions` 557.68 ms → 4.31 ms at 2 000 sessions;
  `get_recent_session_ids` 47.41 ms → 2.75 ms at 150 claims, the `EXISTS`
  now planning as a covering-index SEARCH instead of a per-candidate SCAN.
- **The MCP scope gate refused `project: "."`** — the plugin's own canonical
  spelling, added by round 7 itself. `_same_root` (realpath + normcase)
  compares identities, not strings.
- **`ui/dashboard.py`'s generated CLAUDE.md is swept whole** — its
  description slot comes from a cloned repo's `package.json` *before*
  `clean_for_storage` ever runs; **`memory_topics` is bounded** (rows capped,
  bodies clipped with a visible marker, truncation reported — 272 KB /
  ~68 000 tokens measured unbounded); **MEMORY.md's topic list is capped**
  with a visible "newest N of M" line and its archive block walks only the
  newest months by stem instead of `rglob`+`stat` over the whole history.

Closing the round's last two findings generalised two gates:

- **`tools/doc_claims.py` scans THREE surfaces** with one grammar — tracked
  markdown, `cc_memory/config.json`, and the shipped package's docstrings +
  comment runs. The first sweep of the new surfaces found three counts
  already wrong: config.json still called the MCP server "the seventh
  caller" of the opt-out (twelve surfaces consult it), `_connect`'s
  docstring counted 66 call sites in a file holding 80, and a manifest
  comment said "three hooks" import `core/markers` (two hooks and
  `core/idle.py` do). The grammar gained four guards, each justified by a
  measured false positive: version digits (`v2.7 hook` parsed as "7 hook"),
  hyphenated compounds (`hook-contract` as "2 hook"), ALL-quantifiers
  (`every one of the six hooks` as "1 hooks"), and word-boundary guards on
  the Chinese pattern, whose latin noun spellings had re-matched every false
  positive the English patterns had just learned to decline.
- **`ui/dashboard.py` is EXECUTED by a gate** — headless import plus its
  module-level surface driven directly: the deep scan and CLAUDE.md
  generator against a hostile fixture (with an explicit assertion that the
  hostile text reaches the output, so the sweep assertion cannot be
  vacuously green), and the SQL console's read-only classifier in both
  directions. The Tk class itself remains undriven and is recorded as such.

The falsification register grew 41 → 127 across the two cc-tree rounds;
every case was verified RED individually before being kept, and `--anchors`
reports 127/127 intact.

### Round 7 — a radial cc-tree audit of the whole tree

Twelve framings expanded from the repository root, 21 candidates, 18
adjudicated after independent reproduction, 15 fixed (3 narrowed to their
measured extent, 1 refused by user ruling). The headline fixes: per-session
marker directories gained a privacy guard and junction-awareness on Windows;
PROGRESS.md / PLAN.md / MEMORY.md sweep their ASSEMBLED text rather than
slot-by-slot (two independently clean values could complete a marker across
a join the renderer wrote); archive filenames are escaped as values;
`core/textsim.py`'s word grammar covers non-Latin scripts beyond CJK; the
FTS layer probes before trusting, guards `_match_fts`, and repairs triggers;
two check-then-insert upserts became single `ON CONFLICT` statements; the
observation queue is served oldest-first with an explicit per-extraction
budget; PROGRESS.md §2 is filled from extraction results (user ruling); the
plan-refiner's input is written to disk before the nudge that consumes it;
hooks write nothing to either console stream; and the MCP server gained the
launch-project scope gate (user ruling: lock to the launch project).

### Round 4 — the state machines, the clock, and the injection contract

Round 3 swept what happens to memory content. Round 4 attacked three angles
none of the earlier rounds had used — lifecycle state transitions, schema
evolution over time, and output budgets — and found **20 more defects, every
one reproduced independently before it was accepted.** One of them was mine:

- **The round-3 CJK substrate LOOSENED the carryover gate.** Two consumers
  compare against these scores in OPPOSITE safety directions. The writer's
  `MID_SIM` wants a higher score (merge the duplicate); the plan gate's
  `CARRYOVER_MATCH_THRESHOLD` wants a lower one, because a false match
  silently DROPS an unfinished step. Raising CJK similarity helped the first
  and broke the second: a sweep of 325 one-character CJK substitutions moved
  **98 from FLAGGED to auto-carried and 0 the other way**, including
  `把超时设为三十秒` vs `把超时设为六十秒` — thirty seconds versus sixty,
  opposite facts — at 0.3333 → 0.5556. `core/plan.py` now derives its own
  bigram-calibrated bar (2/3, from the arithmetic that reproduces the
  trigram crossover); English verdicts are unchanged and the CJK gate is 36
  cases STRICTER than before, which is the safe direction for a gate that
  exists to refuse.
- **TodoWrite retired the steps of a plan every renderer refuses to show.**
  Between ExitPlanMode and refinement, `plan_active` holds a SUPERSEDED
  structured plan; PLAN.md and `plan-status` both render a PENDING banner
  instead of it and `plan-check` refuses to check it — but `apply_todowrite_sync`
  kept mutating it, from todos that belong to the NEW plan. Measured: three
  unfinished steps flipped to `done`, `unfinished_steps` emptied, and the
  replacement then passed the mandatory carryover gate with zero
  dispositions — one of the three would not even have auto-carried.
- **One disposition discharged every step whose title resembled it.**
  Entries were matched fuzzily and never consumed, so
  `{"old_title": "Add unit tests for the auth module", "reason": "landed in
  PR #412"}` licensed the drop of auth / authz / audit / admin at once. Three
  of those four drops carried a reason about a different step, which is "a
  drop without a recorded reason" wearing a costume.
- **A todo matching at 0.4474 could retire a step the gate would refuse to
  carry at 0.50.** `done` is the one status that removes a step from
  `unfinished_steps`, and the no-regress rule makes it a one-way door. A
  status that ESCAPES the gate now has to clear the gate's own bar.
- **A re-captured raw plan was destroyed unarchived**, contradicting "every
  outgoing plan is archived" — and re-entering plan mode is the likeliest
  double-fire in the whole lifecycle.
- **A wall-clock string was ordering and bounding everything.** `_now()` is
  naive LOCAL time; it repeats an hour at every DST fall-back and steps back
  on any NTP correction. It was the observation watermark AND the sort key
  for every "most recent" query. Measured with the clock stepped back one
  hour: **3 observations written, 0 of 3 visible to extraction, 3 of 3
  deleted** — destroyed without ever reaching the LLM; and the newest session
  sorted LAST, so `get_recent_memories(sessions_back=1)` returned nothing
  while an active memory existed and PROGRESS.md attributed the handoff to
  the wrong session. Both now key on the monotonic row id.
- **The FTS migration ledger recorded INTENT, not state.** `_setup_fts5`
  swallows its own `OperationalError` and returns, while `_run_migrations`
  writes the `v2_fts5` row unconditionally — so a database first opened on a
  sqlite without FTS5 was marked migrated with no index, and never rebuilt on
  any later run or version. The `LIKE` fallback needs a contiguous substring,
  so ordinary multi-word queries return nothing, and `mcp/server.py` counts an
  empty result set as a SUCCESS: the model is told the project has no such
  memory rather than that search is broken. `_detect_fts5` repairs now, and
  `_fts5_available` became per-instance (it was class state describing a
  per-database property).
- **The round-3 snapshot guard had a blind spot of its own.**
  `compute_content_hash` digests `content.strip().lower()` — a DEDUP identity
  — and using it as a VERSION identity let a concurrent case-only rewrite
  through: `'Deploy Key Is ROTATED Monthly'` → `'deploy key is rotated
  monthly'`, same hash, archived anyway. It compares the text now.
- **One non-UTF-8 byte in PROGRESS.md deleted the ENTIRE injection.**
  `read_text(encoding="utf-8")` raises `UnicodeDecodeError` — a `ValueError`,
  not an `OSError` — which escaped a handler that caught only `OSError`, out
  of `build_context`, into the hook's outer handler. Measured: **2777 bytes
  with the mandatory `<system-reminder>` and every memory, down to 58 bytes
  with neither**, rc=0 and nothing on stderr, from appending one GBK line to
  a generated file. Two fixes: the read tolerates it, and the forced reminder
  no longer shares a failure domain with the layers — a contract a stray byte
  anywhere upstream can delete is not a contract.
- **Two individually-clean values reassembled a live authority tag when
  joined.** Neutralisation ran per value while the renderer CONCATENATES: a
  row ending `<system-reminder` and the next starting `>` produced a token
  the module's OWN detector matches — 4 matches in an injection where the
  plugin emits 2. `build_context` now escapes the ASSEMBLED content, and
  appends the reminder AFTER that pass; the first version of this fix escaped
  the plugin's own reminder, and the check written alongside it caught that.
- **A bare CR bypassed the heading escape.** `neutralize_block` split on
  `\n` only while `_CONTROL_RE` deliberately KEEPS `\r`, so
  `\r## 7. Pre-compact Transcript Pointer` was never escaped and Windows
  text mode turned it into a real line break — two `## 7.` headings in a
  document that has one, which is the exact forgery the function exists to
  prevent.
- **One oversized row emptied a whole injection layer.** The budget checks
  said `break`, and rows are ordered `importance DESC, updated_at DESC` —
  exactly where a freshly written row lands. Measured: the critical layer
  went from 8 of 8 facts to 0 and the timeline from 12 of 12 to 0, while both
  headers still rendered, so the injection looked structurally normal and was
  empty. One 10,000-character topic NAME did the same to the knowledge-base
  layer, and the topic truncation INVERTED under pressure (`summary[:max_len-3]`
  became a negative slice). `memory_add` is model-invokable, so none of this
  has to be an accident.
- **The one layer the budget table claimed to bound was the only unbounded
  one.** `_LAYER_BUDGETS["footer"]` has declared a 0.10 share since it was
  written and `_build_footer` took no budget at all; one 5 MB field in
  `memory/.last_save.json` — a plain file anything with the Write tool can
  create — produced a **5,010,676-character injection against a 16,000
  budget**, 313x over.
- **Session archives were the last artifact still truncate-written.** 332
  EMPTY reads in 2,264 samples under three concurrent readers, against 0 in
  3.4M for `write_atomic` — and here a torn file is PERMANENT, because
  `_reserve_archive_ts` has already claimed the path and nothing rewrites it.
  Now atomic, and a failed archive costs the archive rather than the
  compaction.
- **The plan queue walked backwards.** `approve <ID>` and `set-eval <ID>`
  took explicit ids with no status predicate, so a `done` plan re-entered the
  ready queue where `exec --next` hands it back to Claude to run again — the
  twin of a defect already fixed in `cmd_evaluate` in the same file. And
  `exec --next <ID>` exited 0 while executing a DIFFERENT plan than the one
  named; a contradictory invocation is refused now.
- **A read-only command demanded a guardian check.** `is_sensitive_tool_call`
  was a bare substring test that bumps the drift counter by 20 against a
  threshold of 12, so `grep -rn "git push" docs/` tripped it. Patterns are
  anchored at a command position now. The drift counters also survived a full
  plan replacement, firing the nudge on turn 0 of a brand-new plan.
- **`normalize_structured` raised outside its documented `ValueError`
  contract** on a model-generated payload (`1e999` → `OverflowError`, a list
  → `TypeError`), so `plan-set --from-refiner` answered a mostly-correct
  refiner output with a raw traceback.

Falsification grew with the fixes: `tools/falsify_fixes.py` now carries **41
cases, 41 detected**. One of them was written GREEN — the observation-watermark
check passed against its own reverted fix, because the id-based cleanup had
already deleted the row that distinguished the two implementations. The check
was rewritten to assert the read BEFORE any cleanup. A counterfactual harness
that only ever confirms is worth nothing; this is the second release where it
caught a vacuous check that a green suite had not.

### Round 3 — the memory *content* paths, and a gate that could not see CJK

The rounds above swept **where** a project's data lives. This one swept what
happens to the data once it is there, and found that the anti-patch contract
— the plugin's oldest invariant — had been silently inoperative for
Chinese-language memories since it was written.

- **Character trigrams collapse on CJK, so every Chinese correction was
  filed as a NEW fact.** `_trigram_set` existed as three private English-only
  copies (`llm/memory_writer.py`, `core/consolidate.py`, `core/plan.py`).
  A one-character edit to a ten-character Chinese fact scores **0.4545**
  where the equivalent English edit scores 0.7317 — under `MID_SIM` (0.50),
  so neither MERGE nor SUPERSEDE could ever fire. Reproduced on a live
  database first, not constructed: a near-verbatim Chinese correction of
  memory #294 scored **0.23**, was inserted as #301, and both contradictory
  rows stayed active until they were archived by hand. The second layer was
  no better — `core/consolidate.py`'s `_word_set` tokenised with
  `[a-z0-9_]{3,}`, so a pure-CJK memory produced an EMPTY set, word-Jaccard
  returned 0.0, and the LLM judge was never even offered the duplicate.
  New `core/textsim.py` is the ONE substrate for all of it: character
  bigrams inside CJK runs (the same edit now scores 0.636), trigrams
  everywhere else, and ASCII output **byte-identical** to the retired copies
  so no tuned threshold in the tree moves. For a user whose project memory is
  mostly Chinese, stacked contradictions were the normal case, not an edge.
- **MERGE destroyed the surviving row's tags.** It wrote
  `set(incoming + ["merged"])`, so a memory born `["observer","realtime"]`
  came out `["merged"]` and its provenance — the thing `CLAUDE.md` keeps a
  table of emitters for — was gone. Tags are now an order-preserving union
  and capped at `MAX_TAGS` (32); nothing bounded them before, and a
  10,000-entry list supplied through the model-invokable `memory_add` was
  stored verbatim.
- **A 0.95-similar row ranked 51st was invisible.** `MAX_CANDIDATES_TO_SCAN`
  was 50 against a `(importance DESC, created_at DESC)` ordering, so the cap
  bounded CORRECTNESS, not cost: measured, a true similarity of 0.952 was
  reported as 0.036 and the "new" fact was inserted beside its twin. Now 500,
  and a truncated scan is logged rather than silent.
- **`supersede_memory` was two transactions.** Insert committed, then archive
  committed; a process killed between them left BOTH rows active — the new
  fact and the fact it replaces, contradicting each other in every render.
  One transaction now.
- **Five `id IN (...)` writers died past the SQLite variable cap**
  (`OperationalError: too many SQL variables`, measured at 32767 ids; the cap
  is 999 on builds before 3.32). All chunk now, inside one transaction each.
- **Snapshot verdicts archived repaired content.** `cleanup_garbage` runs
  unattended from the Stop hook CONCURRENT with the PreCompact writer, and
  its verdict is computed in a separate transaction — so a row whose garbage
  content had just been merged over was archived anyway (measured). The three
  snapshot stages now write through `archive_if_unchanged`, conditional on
  the `content_hash` the verdict was computed from.
- **`supersedes_id` could be made cyclic** (`A→B→A`, constructible through
  `archive_obsolete` after a killed supersede). The chain walker survived on
  its seen-guard while returning garbage lineage; links that would close a
  loop are now refused and logged.
- **One non-record JSONL line cost the whole compaction.** `json.loads`
  succeeds on `null`, `42`, `"s"`, `[1,2]`, `true`; every consumer then calls
  `msg.get(...)`, so `build_extraction` raised `AttributeError`, the hook's
  outer handler wrote `success:false`, and the PROGRESS.md handoff — the
  thing the plugin exists for — was skipped. Both loaders drop non-records.
- **There was no supported way to retire a WRONG memory.** `sql` is read-only,
  `add` reconciles only on similarity (which, per the first item, a Chinese
  correction never achieved), and the only route left was to bypass the CLI
  and call `db.bulk_archive` by hand — which is what this maintainer actually
  did. New `/cc-mem archive <id>... [--supersedes ID]`: archives, never
  deletes, records lineage, and refuses an id belonging to another project in
  the same database file.
- **`call_llm`'s `deadline` was an idle timeout, not a wall clock.**
  `urlopen(req, timeout=t)` is per-socket-operation and every arriving byte
  resets it, so a peer dripping one byte per interval held a leg open
  indefinitely: **11.07 s measured against a 3 s deadline**. Each leg now runs
  under a true wall-clock bound. The first fix for it did not work — closing
  the response DRAINS the remaining body, which blocked for 8.10 s of that
  11.10 s; aborting the socket returns in 0.00 s.
- **`pre_compact` dropped a whole compaction over an annotation field.** A
  list-valued `trigger` reached `db.insert_session`, raised
  `sqlite3.InterfaceError`, and the outer handler abandoned extraction, the
  archive and PROGRESS.md — for a field whose only job is to say "auto" or
  "manual". Both `trigger` and `session_id` are coerced; `cwd` and
  `transcript_path` remain load-bearing and still exit early.
- **The marker hardening was half a fix, twice.** `write_marker`'s
  `O_NOFOLLOW` guarded writes while all six readers used a bare `read_text`,
  which FOLLOWS a planted symlink — and the prompt marker's content is
  spliced into the Stop observer's Anthropic request. Then the read-side fix
  turned out not to work on Windows at all: `O_NOFOLLOW` is 0 there and an
  `fstat` taken after the open describes the TARGET, so a link to a regular
  file passed `S_ISREG` and the linked contents were read in full (measured).
  The portable guard is `os.lstat`, and it now runs on BOTH paths.
  Separately, three modules truncated the session id to 16 characters, so any
  two sessions sharing a prefix shared EVERY marker; one shared `safe_id`
  hashes the whole id.
- **`/ccm-load`'s opt-out gate shared a `try` with `core.roots`.** A package
  tree missing that module raised `ImportError` past the gate, the handler
  printed "root anchoring unavailable", and an EXCLUDED project was then
  fully initialised — database, PROGRESS.md, MEMORY.md, .gitignore.
  Reproduced both ways. The gate now has its own `try`, ahead of anchoring.
- **Both `.gitignore` literal copies strict-decoded.** `core/progress.py`
  gained `errors="replace"` for a GBK-appended line; the skill and the
  installer copies did not, so a UTF-16 `.gitignore` aborted `/ccm-load` with
  `rc=1` and a `UnicodeDecodeError` traceback.
- **The web viewer's admission shed closed the socket with no HTTP
  response.** The client sees `ConnectionResetError` (`[WinError 10054]`,
  measured on the 17th concurrent request) with no status and no
  `Retry-After`, so the SPA's `fetch()` rejects and the panel sits on Loading
  forever: a cap that exists to keep the viewer responsive under load
  presented as the viewer being broken under load. It answers `503` now.
- **Initialize Project reported "Success" for a refusal.** `_init_project`
  returned `None` whether it scaffolded or declined, so an opted-out project
  — where nothing at all was created — produced the same dialog as a real
  install, naming the raw pick even when anchoring had redirected elsewhere.
  It returns its outcome and the path it actually used.
- **`tools/doc_claims.py` had three coverage holes of its own**, each
  measured: an ASCII number word INSIDE another word bound a claim nobody
  wrote (`done` → 1, `often` → 10); `seven of the hooks` was not a trigger
  site at all; and the Chinese trigger knew only the measure word 个, so
  `六条钩子` and `6 个 hook` were invisible. Closing them turned up six real
  unbound claim sites in the shipped docs.
- **`tools/contracts.py` was itself enumerating.** `memory_dir_creators`
  counted `ensure_memory_dir` callers only, certifying SIX creators while the
  tree had EIGHT — `core/db.py`'s backstop mkdir and the installer's
  stdlib-only bootstrap create one each and neither goes through the choke
  point. The N+1 prose disease, recurring inside its own cure.

Documentation drift found by the widened gate and fixed: `CLAUDE.md`'s "36
pairs" (48), "18 ladder cases" (23), "four checks" (nine §7 functions by `git
diff`) and "EIGHT release gates / two dev checkers" (nine / three, while its
own closing paragraph already said "all six scripts"); `README.md` §Tests'
"Five stdlib scripts", "Eight release gates", `RESULT: 14 passed`, `§1-§6`,
"two doc gates" and "six sections"; and `docs/CONTRACTS.md`'s claim that
`upsert_smart`, unlike `semantic_dedup`, does not union tags — true when
written, false as of this release. Both Chinese translations were updated to
match rather than having their drift hashes refreshed over an untranslated
change.

Every fix above carries a counterfactual. `tools/falsify_fixes.py` reverts each
one on a TEMPORARY COPY of the tree and asserts the corresponding gate goes
RED — 21 cases, 21 detected — so no check in this release is known only to
pass. Two of them earned their place immediately: the marker symlink guard
went red on Windows the first time it ran (`O_NOFOLLOW` is 0 there and the
`fstat` describes the target), and the `call_llm` deadline fix did not work
at all until `resp.close()` was replaced, because closing DRAINS the body.

---

**v2.7.0 taught the six hooks where a project's root is, and left every other
surface behind.** Three further adversarial debug rounds — each finding
independently reproduced before being accepted, two rejected as
irreproducible — confirmed 22 defects. The shape repeats the one v2.7.0 was
released to fix, one level up: a guard was attached to *some* callers instead
of to the thing they all pass through.

Measured on the reporting machine, not hypothesised: a `memory/memory.db` was
sitting at the root of drive `D:`, created by this project's own test suite —
`test_surfaces`' pathological-cwd case fed `D:*b`, the resolver answered
`D:\`, and the hook initialised a database there on **every run**. While it
existed, every uninitialised project on that drive resolved to it.

### Fixed — surfaces that never anchored

- **`cli/plan.py`** never anchored `--project`, and `_get_db` mkdirs and
  creates, so even the read-only `list` planted `<subdir>/memory/memory.db`.
- **`mcp/server.py`** fed raw `os.getcwd()` to three tools — the one
  model-facing *write* surface. Its `memory_add` and `progress_regenerate`
  then re-derived the path a second time, *after* `_get_db` had anchored, so
  `MEMORY.md` and `PROGRESS.md` hit ENOENT against a directory the database
  did not live in (swallowed for the first, a hard tool error for the second).
  Both now derive from `db.db_path.parent`, which cannot disagree with the
  database actually opened.
- **`ui/dashboard.py`** anchored only `--project`; the other four routes into
  `_load_project` (combobox, Manage…/Save, the registry, Init New) planted a
  stray in whatever directory the user browsed to. Anchoring moved into
  `_load_project` itself — the one place a path becomes a database.
- **`ui/installer.py`**'s *Initialize Project* built the scaffold at the raw
  picked path; **`ui/web_viewer.py`** was the last unanchored `--project`,
  and its symptom was the inverse — it *refused* a fully initialised project
  whenever it was started from a subdirectory.
- **`skills/save-memories`** and **`skills/ccm-load`**: the latter's anchoring
  was dead code (`best['path']` is not a layout key, so the `KeyError` was
  swallowed by its own fallback), and the former never anchored at all.

### Fixed — the privacy opt-out was never enforced outside hooks and MCP

`is_excluded` appeared **zero** times in `cli/mem.py`, `cli/plan.py` and
`ui/dashboard.py`, while the MCP refusal promised memories were "neither
readable nor writable through **any** cc-memory tool". All hand-run surfaces
now enforce it through one shared gate, checked *before* anchoring so a
per-subdirectory exclusion is never widened to its unexcluded parent.

Three more surfaces had to be swept in before that was true, and each was
found only after the previous fix shipped: the dashboard's *Init New* (a
route that reaches `_ensure_memory_dir` without passing `_load_project`),
the installer's *Initialize Project*, and **both skills** — whose bodies are
shell-quoted `python3 -c` blobs that no import graph reaches.

The installer's gate then turned out to be **unreachable**. `_init_project`
imported `core.modes` at :1103 while the only `sys.path` setup in the file
sat at :1137 — 34 lines *below* it, inside the same function. On the first
Initialize Project click of a process the import raised
`ModuleNotFoundError` straight into `except ImportError: pass`, so an
opted-out project received the full scaffold; a *second* click in the same
process worked, because the late insert had leaked the path. `sys.path` is
now primed at module scope like every other surface.

An opt-out is also no longer reported as a failure: the dashboard routed it
through the missing-drive error dialog, which blamed an unplugged drive and
advised removing the entry — a false cause and a remedy that changes
nothing.

A blank `--project` then bypassed that new gate on all three: `is_excluded`
rejects an empty string by design, while `anchor_project("")` resolves it to
the real root — a fully working spelling that skipped the check. Measured, a
`plan.py --project "" add` wrote a row into an opted-out project's database
one command after `--project .` was refused. `core.modes.cli_opt_out_notice`
normalises a blank value to the current directory before the gate.

### Fixed — resolver and hook contracts

- **The filesystem root was a candidate.** `_chain`'s docstring promised it
  stopped "below the filesystem root" and the code never did. Now excluded —
  with two exemptions: `start` itself, and a root carrying `.ccm-root`,
  without which this rule silently overruled the pin exemption added in the
  same change.
- **`.ccm-root` lost to the container heuristic.** A pinned directory that
  looked container-shaped was dropped from the candidate set, so the
  documented escape hatch did nothing. `PIN_MARKER` now exempts, like a VCS
  root.
- **`anchor_project` compared an unresolved root against a resolved input**,
  so `--project .` — what the `/cc-mem` wrapper passes — announced
  `. is inside a project rooted at .` on every call.
- **`core/logger.py` bound `Path.home()` at module scope.** `Path.home()`
  raises when no home resolves, making `from core.logger import get_logger` a
  raising statement: `stop`, `pre_compact`, `session_start` and
  `consolidate_async` each exited **rc=1 with a stderr traceback** — the two
  things the hook contract forbids outright.
- **`hooks/pre_compact.py` was the only hook without an `isinstance(cwd, str)`
  guard** and the only one that mkdirs unconditionally, so `{"cwd": 123}`
  created a database in the *hook process's own* working directory.
- **Databases were created without `memory/.gitignore`** — the one omission
  that let a 184 KB `memory.db` ride into three commits of a sibling
  repository. Writing it was every caller's job, so every caller forgot:
  `cli/mem.py` alone has thirteen `MemoryDB(...)` sites and none of them did
  it, and a first `/cc-mem add` left the binary staged by `git add -A`. It
  now happens in `MemoryDB.__init__` — the line that brings a `memory/`
  directory into existence — where no caller can skip it. It stays idempotent
  and additive, so opening an existing database costs one read.
- **`/cc-mem cleanup` fabricated a database for a project that had none**,
  then reported "Final: 0 active memories, MEMORY.md regenerated" — a success
  line for work that could not have happened. Its sibling `consolidate` had
  always refused; two commands over the same memories must not disagree about
  whether there have to be any.
- **The refusal wording named a false cause under fail-closed config.** With
  an unparseable `config.json` every project refuses, including ones in no
  list; "remove it from that list" was both wrong and impossible. It now
  branches on `config_fault()`.

### Fixed — found by a full adversarial code audit

Twelve framings across security, concurrency, resource-exhaustion and
trust-boundary lenses, every finding reproduced before it was accepted and
two rejected as irreproducible.

- **The automatic janitor destroyed memories four surfaces had just
  accepted.** `core/consolidate.py` carried a *second* length floor — 20
  characters against the writer's 10 — and deleted, not archived. Measured:
  `/cc-mem add note "lr=3e-4 wins"` printed `[inserted] #1`, appeared in
  MEMORY.md, and five turns later the `memories` table held **zero rows**.
  `core/db.py` states that every delete path must archive because a hard
  DELETE strands `supersedes_id`, and reserves `delete_memories()` for
  user-driven purges — the unattended janitor was its only caller in the tree.
  It now imports the one floor and calls `bulk_archive`.
- **Two render paths did not escape authority markers.** `CLAUDE.md` says the
  defence "runs on the write path and again on **every** render path" and then
  names four renderers; `mcp/server.py` and `cli/mem.py` were not among them,
  with zero occurrences of `neutralize_*` between them. Measured on this
  repository's own database: 307 active rows, **2 already armed** — the same
  row rendering as `&lt;system-reminder&gt;` through SessionStart and as a
  live tag through the MCP server. MCP now defangs at `_send_tool_result` (one
  choke point, so its handlers cannot drift apart), and the CLI in `_trunc`.
  `topic` — the one model-controlled column with no write gate — and the
  LLM-authored topic summary now go through `clean_for_storage`.
- **A NUL byte turned a read into a full index rebuild.** fts5 takes the MATCH
  expression as a C string, so a NUL truncates it; both forms tried in
  `_match_fts` then fail and its double-failure branch concludes the *index*
  is broken. Reachable from the web viewer's `?q=%00` and from `memory_search`,
  whose `minLength: 1` a lone NUL satisfies. `search_fts` now strips C0
  controls, which tokenise to nothing anyway.
- **`upsert_progress`'s session-tag guarantee was a lost update.** It read the
  tag through `get_progress`, which opens and *closes* its own connection,
  then opened a second one to write — so a `tag_progress_session` landing in
  between was clobbered, and PROGRESS.md then told the next session that
  another session's todos were its own. Read and write are now one
  `BEGIN IMMEDIATE` transaction.
- **Per-session markers were world-readable and symlink-followable.** They
  hold 500 characters of the user's prompt and `hooks/stop.py` reads them back
  into an Anthropic request. On Linux with `TMPDIR` unset that is mode-1777
  `/tmp`, and `write_text` follows symlinks. New `core/markers.py` puts them
  in a per-uid 0700 directory and writes with `O_NOFOLLOW` at 0600; the
  uninstall sweep covers the new and the legacy location.
- **The viewer bounded one request but not how many.** Its own docstring says
  "ThreadingHTTPServer caps neither threads nor connections"; a connection that
  sends nothing never reaches the body deadlines and still leases a thread.
  Admission is now capped at 16 and sheds **non-blocking** — a bounded *wait*
  measured worse than no cap at all, because `process_request` runs on the
  accept loop. Idle timeout 10s → 3s.
- **The pairwise consolidation stages had no bound of any kind.** `BudgetGate`
  is consulted only by the three LLM stages, while `merge_near_duplicates` and
  `_nominate_groups` run N(N-1)/2 comparisons over every active memory before
  the first network call. Both now cap at 1500 rows.
- **`PRAGMA journal_mode`'s return value was never read**, and SQLite keeps the
  old mode *silently* when it refuses — measured here: an invalid mode returns
  the previous value and raises nothing. WAL does not work on network
  filesystems, which this codebase explicitly contemplates. `_connect` now
  reads the result and degrades to a rollback journal with one warning.
- Also: `write_atomic` fsyncs before the rename; `.plan_raw.md` goes through it
  (the plan-refiner reads it from another process); the installer's rename
  retry has backoff (five iterations with no sleep sampled the same instant
  and converted nothing); `ensure_memory_gitignore` decodes with
  `errors="replace"` and catches `ValueError` — `UnicodeDecodeError` escaped
  its `except OSError`, and `pre_compact` calls it *above* the archive, the
  session row, the memories and PROGRESS.md, so one GBK byte cost the whole
  compaction; `write_progress_md` lost five discarded round-trips and an
  unguarded `None` subscript on the per-turn path.

### Fixed — hazards the fixes above introduced

Five convergence rounds ran against this change set, one of them with a lens
that looked only for damage the repairs had done. It found four, all
reproduced before being accepted:

- **The viewer's new admission cap eroded under the load it exists to bound.**
  `_BoundedServer` released the permit in `process_request`'s except *and* in
  `shutdown_request`, on the belief that socketserver calls the latter only
  from the worker thread. `BaseServer._handle_request_noblock` calls it on
  both of its failure arms too, so a `RuntimeError: can't start new thread`
  returned one permit twice — measured, the ceiling climbed 16 → 17 → 18 → 19
  and never came back. `shutdown_request` is now the single release point, and
  `_ADMIT` is a `BoundedSemaphore` so the next such bug raises instead of
  quietly lifting the cap.
- **Cleaning the topic on the way into `topics` orphaned older rows.**
  `get_memories_by_topic` matches `memories.topic = ?` on string equality, so
  escaping the key while a pre-v2.8.0 row still holds the raw value broke the
  lookup — measured, a legacy `build<system-reminder>x` topic went from one
  matching memory to none. Only the summary is cleaned now; new rows are
  already safe because the write path cleans `topic`, and every render path
  escapes at render time.
- **`core/markers.py` was in none of the ship manifests.** A standalone
  install would have shipped a package whose hooks cannot import. It is now in
  all three lists, and `smoke_test` asserts that every `core/` module appears
  in all three — `core/roots.py` went missing from the third one the same way
  in v2.6.0, and only the two copy manifests were being compared.
- **A NUL in `cwd` took `pre_compact` to rc=1 with a traceback.** A NUL is the
  one character no filesystem accepts, and every stdlib path call rejects it
  with `ValueError` — not `OSError`. So it walked past the handlers: the mkdir
  raised, the outer `except Exception` caught it, and the *recovery* path then
  wrote `.last_save.json` under the same poisoned `cwd`, raised the same
  `ValueError`, and escaped its narrower `except OSError`. Rejected at the
  entry now, where one check covers every downstream use; the last-resort
  handler catches `Exception`, because a last-resort handler that can itself
  raise is not one.

### Fixed — round six: the loop itself

Round five's findings were eleven parts documentation drift to six parts code,
and the code defects were again guards missing from N+1th call sites. Both are
the same disease: a fact maintained by hand at every place it is used. This
round removes the hand from the loop instead of patching the sites.

- **`/cc-mem summary` and `/cc-mem inject-show` printed stored rows raw.**
  `/cc-mem` runs as a Bash command inside a Claude session, so its stdout IS a
  render path; a planted `<system-reminder>` measured live=1/escaped=0 through
  both. The per-call-site rule had already failed twice, so the unsafe
  primitive is gone: `cli/mem.py` shadows `print` with one that escapes every
  argument (idempotent, verified on already-escaped text). 194 sites, no list
  to maintain. Swept all 28 subcommands afterwards: rc=0, no tracebacks, no
  format changes.
- **Five surfaces resurrected a deleted project directory.**
  `mkdir(parents=True)` materialises the whole chain, so a project removed or
  renamed mid-session was recreated as an empty shell — memory.db, .gitignore,
  sessions/, topics/ — by the next hook to fire. `ui/dashboard.py` already
  refused correctly, but in a private method, which is why both hooks,
  `cli/mem.py`, `cli/plan.py` and `core/plan.py` (twice) each kept their own
  wrong copy. The refusal is now `core.progress.ensure_memory_dir` — one
  function, seven callers — and `MemoryDB.__init__` dropped `parents=True` as
  the backstop for anything that bypasses it. Falsified both ways: guard
  removed → both hooks recreate the gone directory; guard present → they
  don't, and a first run on an EXISTING directory still initialises fully.
- **`write_plan_md` could violate its own "never raises" docstring** — its
  directory creation sat above the try block that exists to absorb write
  failures. Moved inside.
- **`cleanup_garbage` said "deleted" everywhere while archiving.** The result
  key `garbage_deleted`, the CLI line and the module docstring all reported an
  irreversible purge for rows that are recoverable and still on the supersede
  chain. Renamed to `garbage_archived` across producer and consumers.
- **`merge_near_duplicates` logged "comparing the newest 1500".**
  `get_all_active_memories` orders by `(topic, importance DESC, created_at
  DESC)`, so the slice is the alphabetically-first topics and whole
  late-alphabet topics go uncompared. The log now says so.
- **A line-range citation inside source had already rotted.** `cmd_cleanup`
  cited `(:1001-1003)` for its sibling's refusal; those lines are an unrelated
  SELECT. `tools/citation_check.py` only scans the tracked docs, so citations
  in source comments are checked by nobody — this one now names the symbol
  instead of a number.

### Fixed — hazards round six introduced, and one it exposed

Three independent adversarial passes over the round-six changes, every finding
reproduced here before being accepted. Five of the six are defects the round's
own fixes created — the failure mode this release is named for, caught by
auditing the fix instead of the symptom.

- **The refusal to resurrect a project reached the user as a traceback.**
  `ensure_memory_dir` and `MemoryDB.__init__` now raise `FileNotFoundError`
  for a project directory that is gone — correct, and `/cc-mem add` printed
  nine lines of stack for it while `cli/plan.py` printed one clean sentence
  for the identical case. The boundary went on `main()`'s single
  `dispatch[args.command](args)` line, not on the subcommand that was
  noticed: thirteen `MemoryDB(...)` sites in that file can raise it and a
  fourteenth would have been missed.
- **`argparse` bypassed the escaping `print` entirely.** It writes to its own
  stream and ECHOES the offending argument, so an invalid subcommand spelled
  as an authority marker was measured printing that tag LIVE — into output
  `commands/cc-mem.md` hands straight back to Claude. The parser subclass
  overrides `_print_message`, argparse's one output funnel, so usage, `--help`
  and errors are all covered; subparsers inherit it automatically.
- **`capture_exit_plan_mode` committed before it validated.**
  `upsert_plan_active` commits, and the directory check ran after it, so a
  failure left `needs_refine=1` durable with no `.plan_raw.md` beside it and
  `hooks/stop.py` then reported the raw plan as captured. A precondition that
  runs after the commit is not a precondition.
- **Table columns were measured on unescaped text.** `_table` took widths from
  the raw cell while `_trunc` escaped on the way out, so a 38-character
  `<system-reminder>…` became 50 escaped and was cut back to 38 — twelve
  characters lost from a column that was never full. Pre-existing, exposed by
  looking at the render path as a whole.
- **The claim gate could be fooled four ways**, each fixed and re-falsified:
  a version-mentioning heading exempted `## Live plan anchor (v2.2)`, a
  live section (release-note phrases are matched now, not version numbers);
  `## Hooks (6)` was invisible to a number-before-noun grammar; two adjacent
  claims could swap bindings and both pass (each binding now takes the
  nearest unclaimed site before it); one unclosed fence silently exempted a
  document's remainder (odd parity is now an error). Chinese `这一/哪一/任意
  一/第六` parsed as counts, and tilde and indented fences were not
  recognised as fences.
- **`tools/contracts.py` counted any `.py` token in `hooks.json` as a hook**
  — a script named in a `description` would have inflated every bound count.
  It reads `command` values only. Its AST pass also now counts name LOADS,
  so an aliased guard (`f = neutralize_block`, which `core/progress.py` does
  today) registers; the five computed sets are byte-identical before and
  after, verified by set diff rather than by matching totals.

### Added — executable contracts, so prose cannot drift silently

- **`tools/contracts.py`** computes each asserted set from the tree itself:
  the registered hooks (parsed from `hooks/hooks.json`), the render paths, the
  opt-out surfaces, the `memory/` creators, the anchoring surfaces (AST
  call-site analysis — a module that merely *mentions* a guard in a comment
  does not count, which a grep cannot promise). Counts are `len()` of the
  membership, so "how many" and "which ones" cannot disagree.
- **`tools/doc_claims.py`** verifies the docs against that registry. A countable
  claim binds to a contract with an invisible HTML comment — an inline
  `ce:hooks` marker asserts equality, `ce:hooks:subset` strictly less,
  `ce:hooks:asof` a historical statement never compared — and every
  numeric hook/renderer claim outside a version-titled section or a fenced
  diagram MUST be bound, which is what stops a newly written sentence from
  drifting in unbound. 21 claims bound across the six current-state docs;
  CLAUDE.md's two standing-rule enumerations ("Four renderers are covered",
  "SEVEN callers, not six" — both false by three releases) now point at the
  generator instead of restating its output. Falsified three ways before
  landing: a seventh registered hook, a subset claim overtaking its whole set,
  and a new unbound sentence each fail the gate; the untouched tree passes.
  Wired into `smoke_test` as the third doc gate, beside citations and i18n.

### Changed

- `cli/plan.py`'s read-only `list` and `status` no longer conjure a database;
  they report "no memory database at X" like `cli/mem.py` always has.
- New public API `core.roots.anchor_project(raw, announce=None)` — the one
  implementation every non-hook surface shares. `announce` is a parameter
  rather than a `print` because the MCP server speaks JSON-RPC on stdout.
- New module `core/markers.py` — the one place per-session temp markers are
  resolved and written. Seven call sites across three files went through it,
  and `tempfile.gettempdir()` no longer appears in any of them.
- New `core.db.MemoryDB.get_memory(memory_id)` — a single row by id, active or
  archived. `core/consolidate.py` uses it to re-check its dedup survivor after
  the LLM judge call, a network round-trip the Stop hook can mutate underneath.
- `MemoryDB.__init__` writes `memory/.gitignore`, so no caller can omit it.
  Idempotent and additive: opening an existing database costs one read.

### Tests

`test_surfaces` gained seven checks and `smoke_test` one. Each was verified to
FAIL against the exact state it exists to catch before being kept:

- `_roots_skill_bootstrap` — every `best[...]` subscript in `/ccm-load` must
  be a key some layout actually defines. Red against 2.7.0 as shipped.
- `_skill_shell_metachars` — no backtick and no dollar anywhere in **either**
  skill's shell double-quoted body, comments included; bash expands them
  before python parses. `/ccm-load`'s body is static and can only gain one
  when a human edits it; `/save-memories` has a slot Claude writes into on
  every run, and Step 2 asks it for file paths and parameter names — exactly
  the prose an LLM renders with backticks. The recurring hazard was the file
  the check did not cover.
- `_roots_anchor_announce` — 5 cases; a redirection is announced exactly
  when one happened, never for `.`, an absolute root, or a trailing `/.`.
- `_cli_opt_out_gate` — 5 `--project` spellings including the blank ones,
  driven through the real CLIs as subprocesses.
- `_hooks_never_plant_on_junk_cwd` — 48 (hook, malformed-cwd) pairs
  asserting rc **and** stderr **and** that no database appears. Checking
  only rc is how the `pre_compact` side effect survived a review round: it
  exited 0, wrote nothing to stderr, and created a database anyway. Two of
  the values are well-formed **strings** carrying a NUL — every other one is
  a wrong type, which an isinstance guard catches, and that is why a string
  no filesystem accepts got through.
- `_every_creator_asks_the_opt_out` + `_every_creator_refuses_in_practice`
  — a source rule paired with a behavioural one. The source rule greps, and
  a grep cannot see reachability: on its own it green-lit `ui/installer.py`
  while that surface's gate could not execute at all. The behavioural half
  drives each creator in a **fresh** subprocess, because the installer bug
  only appeared on a process's first call.
- `_viewer_admission_balance` — the admission permit is returned exactly once
  per request across five failed thread starts, and `_ADMIT` rejects an
  over-release. Both halves are needed: the count check catches the leak, the
  type check keeps the next one loud.
- `smoke_test` now cross-checks the **third** manifest. Two lists were being
  compared (`ui/installer.py`, `build_exe.py`) while
  `cli/mem.py:_REQUIRED_PLUGIN_FILES` — the one `/cc-mem status` calls an
  install healthy by — was maintained by hand and drifted twice.

---

## [2.7.0] — 2026-08-08

**v2.6.0 attached its safety guards to one rung's inner loop instead of to the
candidate set, and every rung that did not inherit them became its own
data-integrity defect.** A convergent adversarial debug round — five
dimensions, every finding double-verified against the real source — confirmed
45 defects in the release. The three worst all share that one root cause, and
all three were reproduced before being fixed:

- the **database rung consulted no guard at all**, so a `memory/` created by a
  single session in a projects folder captured every uninitialised project
  under it (measured: five repository children, all swallowed);
- the **marker rung never container-checked the first marker it found**, only
  the ones it extended onto, so one stray `package.json` in a projects folder
  did the same to every marker-less directory below it;
- **neither had any notion of a dependency tree**, so a cwd inside
  `node_modules/left-pad` anchored on the package — it has a `package.json` —
  and planted a database where the reporter does not look.

### Fixed — resolution

- **`_candidates()` filters the chain once, before any rung reads it.**
  Containers and dependency internals are simply not candidates, for every
  rung, which is the structural fix rather than three separate patches.
- **`_is_container` rewritten with asymmetric triggers.** Two VCS-root
  children is always decisive; two merely database-owning children counts only
  when the directory owns none itself. A directory that is itself a VCS root
  is never a container — otherwise a repository with two submodules stops
  being resolvable. v2.6.0's version exempted any directory with a database,
  which is exactly what a polluted container has.
- **The marker extension no longer requires a contiguous run.** `packages/`,
  `apps/`, `crates/` and `libs/` carry no manifest, so v2.6.0 stopped at the
  package and re-created the stray — while two of its own docstrings promised
  the workspace. The VCS ceiling is what bounds the climb.
- **`_is_profile_dir` now requires the `Users`/`home` container to sit at the
  filesystem root.** Without that, any in-repo `users/` directory looked like
  a profile and truncated the chain, so a session in `<repo>/users/alice/sub`
  reached no rung and planted a stray four levels down — the defect produced
  by the guard against it.

### Fixed — hook contract

- **`project_root` now really never raises.** v2.6.0 claimed it and did not
  deliver: the handler's own `return Path(cwd)` re-raised the TypeError it was
  catching, so a `{"cwd": 123}` payload took the hook to rc=1 with a traceback
  on stderr — which Claude Code renders as an error UI.
- **`user_prompt.py` gained the field-type guard the other five hooks already
  had.** It was the one hook that would crash on a non-string `cwd` or
  `session_id` outside any try.

### Fixed — reporting and install

- **Every surface anchors, not just the hooks.** `cc-mem`'s `--project` goes
  through `_anchor_project` and `/ccm-load` resolves before building the
  scaffold. Until now the hooks refused to create a stray while `/cc-mem add`
  from a subdirectory made one — and rung 0, being terminal, then pinned all
  six hooks to it permanently. A redirection is always PRINTED: an explicit
  `--project` is an instruction.
- **`nested_databases` reached one level less than asked** (a directory's own
  `memory/` is found while scanning that directory), and **skipped nine
  directory names including `vendor` and `node_modules`** — i.e. the one tool
  meant to surface a stray was blind exactly where strays are most likely.
  Depth is now honoured and the skip set is down to `.git` and `__pycache__`.
- **The nested-database report now runs BEFORE the missing-database early
  return.** The stray-only shape — no database here, one in a subdirectory —
  is the most damaging layout there is, and v2.6.0 printed "No database" and
  returned without mentioning it.
- **The nested count is active-only and cannot write.** It counted every row
  while the root's own line counted active rows, so the same command reported
  two sizes for one database (3725 vs 2607 here); and plain `mode=ro` still
  lets SQLite create `-wal`/`-shm` siblings, so the "read-only" report wrote
  into the directory it only meant to name. `immutable=1` forbids that.
- **`core/roots.py` added to `_REQUIRED_PLUGIN_FILES`.** Every hook imports it
  at module level, so an install missing it does not degrade — all six die at
  import, while `status` reported the install healthy.

### Tests

`tests/test_surfaces.py` §7 grew to 23 ladder cases plus a contracts block:
every defect above has a fixture that reproduces it, `_CONTAINER_CHILDREN` is
pinned from both sides (it was completely unpinned — the suite passed with the
threshold at 1), and `project_root` is asserted to return a `Path` for `int`,
`None`, `list`, `dict` and `bytes`.

---

## [2.6.0] — 2026-08-07

**Every hook read the project out of `cwd`, and `cwd` follows the agent's own
`cd`.** A session launched at a repo root that ran one command inside `cli/`
started reporting `<root>/cli`, and `UserPromptSubmit` mkdir'd a second, fully
independent database there. Four of the six hooks gate on `memory/memory.db`
merely EXISTING, so once born the stray sustained itself: 27 memories and its
own `projects` row in one, against 161 in the real database two levels up —
observations, progress rows and `PROGRESS.md` all landing where no
`SessionStart` would ever read them. It also carried no `.gitignore`, because
only the directory the init path creates gets one, so a 184 KB binary
`memory.db` rode into three commits of the user's repository. There was no
notion of a project root anywhere in the plugin: `CLAUDE_PROJECT_DIR` and
`.git` had zero occurrences across `hooks/` and `core/`.

### Added

- **`core/roots.py`** — `project_root(cwd, log=None)` resolves a project root
  from the payload's cwd and never raises: any failure returns `Path(cwd)`,
  the pre-2.6.0 answer. Over an ancestor chain bounded below every home
  directory, below the filesystem root, at a `.ccm-root` pin and at 25 levels,
  first hit wins: (0) a `memory/memory.db` at cwd itself — terminal, before
  anything else is consulted; (1) the NEAREST ancestor with one, no outward
  extension; (2) `CLAUDE_PROJECT_DIR` when it names a directory *in the
  chain*, ranked below the database rungs because "where Claude Code was
  launched" is not authority to orphan a database; (3) project markers
  (`.git`, `.hg`, `.svn`, manifests), nearest then extended outward so a
  workspace member resolves to its workspace — the only rung that can fire
  before any database exists, i.e. the one that stops a stray being created at
  all; (4) cwd verbatim. Returning the ORIGINAL unresolved string when the
  answer is cwd keeps symlinked project directories byte-identical.
- **`.ccm-root`** — an empty file that pins a directory as a project root and
  truncates the walk there. The escape hatch for a project deliberately nested
  inside another, and for any layout the heuristics read wrong.
- **`nested_databases()` + a `cc-mem status` report** — every separate
  `memory/memory.db` below the project root is listed with its memory count
  and what it means. Resolution never merges or moves one, so this is how a
  stray born before v2.6.0 stops being invisible. On an explicit command, not
  in a hook, because it walks the tree; read-only by construction (a
  `mode=ro` connection, not `MemoryDB`, so reporting never writes a
  `projects` row into someone else's database).
- **`tests/test_surfaces.py` §7** — the twin of §4: 18 ladder cases over a real
  filesystem, all six hooks run from a SUBDIRECTORY of a seeded project
  (no second `memory/` appears; the root database gets the writes), and the
  source-level rule that every hook resolves *after* `is_excluded`.

### Fixed

- **All six hooks now anchor.** Each rebinds `cwd` to the resolved root
  immediately after its `is_excluded` gate — after, never before: resolving
  first would widen a per-subdirectory exclusion away by climbing to its
  unexcluded parent. One rebind per entry point rather than a fix at each use
  site, because `memory_dir`, `db_path` and `upsert_project` must agree on one
  directory and per-site fixes are how they drift apart.
- **`SessionStart` from a subdirectory no longer starts blind.** It used to
  log "no DB for `<subdir>`" and inject nothing while the project's real
  memory sat two levels up.

### Changed

- **Prevention replaced migration, after an adversarial review killed the
  first draft against ground truth.** That draft took the OUTERMOST end of a
  contiguous run of database-bearing ancestors, to heal an existing stray.
  Enumerating every `memory/memory.db` on the reporting machine found 20
  databases and **four legitimately nested inside another project** —
  `Claude-Code-Local/companion` alone holds 3725 memories and carries its own
  `.git`. A stray and a deliberate sub-project are byte-for-byte
  indistinguishable on disk (both have a `projects` row naming their own
  directory, because `upsert_project` records whatever cwd it was handed), so
  outermost-wins resolves that ambiguity in the direction that destroys data:
  the first post-upgrade session in `companion` would have moved 3725 memories
  out of reach, silently. An existing database is now terminal at distance 0
  and never extended past at distance ≥ 1, which discharges "never orphan" by
  construction for all 20.
- **The marker rung's outward walk gained two more ceilings**, since it is now
  the only rung that travels: it ends inclusively at a VCS root (a repository
  is the outermost thing that can still be one project — used as a *stop*
  signal, never as a requirement), and it refuses any directory with two or
  more project-shaped immediate children. The reporting machine's projects
  folder has 27, so without that one stray `package.json` dropped there would
  have collapsed every project under it into a single database.
- **`.claude/` is no longer treated as a project marker**, and `CLAUDE.md`
  never was. Both mark "a directory Claude Code reads from" rather than a
  project root: the user's HOME has a `.claude/`, and Claude Code writes one
  into whatever directory a session happens to approve a permission in — it is
  per-cwd session residue. Nothing is lost: every surveyed project carries
  `.git`, and any initialised project is found by the database rungs.
- **The home boundary is doubled: environment AND structure.** `_home_dirs()`
  reads `Path.home()` / `USERPROFILE` / `HOME`; `_is_profile_dir()` matches any
  direct child of a directory named `Users` or `home`. Containers, CI, `sudo`
  and this project's own test sandbox all redirect the environment. Measured
  with HOME pointed into a sandbox: the walk climbed seven levels out of a temp
  fixture into the real profile and matched the `memory/memory.db` that one
  session run in `~` had left there.
- **A stray database is reported, never merged or deleted** — by `cc-mem
  status`, see Added. PreCompact, SessionStart and the async consolidation
  additionally log the redirection when one happens; the per-turn hooks stay
  silent, because a line there is a line per turn.
- `ui/installer.py` **and `build_exe.py`** `SUBPACKAGE_FILES` ship
  `core/roots.py` — without it a standalone install would import a module that
  is not on disk. The two copies are asserted identical by `smoke_test.py`,
  which is what caught the second one being missed.

---

## [2.5.6] — 2026-08-05

**The plan-replacement gate guards `steps` — and that partial coverage cost a
live plan two of its ten success criteria on 2026-08-05.** The replacement
passed the R610 gate cleanly, nothing was printed, and one of the two vanished
criteria was an achieved-but-never-recorded release gate. Scope of evidence is
not scope of claim: a green gate says nothing about the parts it does not read.

### Added

- **`unmatched_criteria(old_structured, new_plan)`** in `core/plan.py` —
  returns every outgoing `success_criteria` entry whose best trigram-Jaccard
  against the replacement's criteria **plus its `goal` and `context`** is below
  the steps gate's own `CARRYOVER_MATCH_THRESHOLD = 0.5`. A criterion folded
  into the new context counts as carried; flagging lossy-but-real survival
  would train the reader to ignore the advisory.
- **Carryover advisory in `plan-set --from-refiner`** — `cmd_plan_set`
  snapshots the outgoing plan *before* `apply_refined_plan` (afterwards it
  exists only in `memory/.plan_history/`) and prints the unmatched criteria,
  what the gate does and does not cover, and — in its last line — that
  `context` is free text and is never compared at all. A gate that hides its
  own scope is how this failure happened.
- `tests/test_plan_carryover.py` **§7** — the core result, the context-fold
  suppression, and an end-to-end assertion that the CLI actually prints the
  advisory. 20 checks in that suite, all passing.

### Changed

- `docs/CONTRACTS.md` + `docs/CONTRACTS.zh.md` gain a
  "What the gate does NOT cover" subsection under Door 1, including the
  verbatim advisory output.
- Two stale `cli/mem.py:1248` citations rewritten to `:1268` by
  `tools/citation_check.py --fix` after the CLI insertion shifted them.

### Deliberately not done

- **No second refusal gate.** Criteria legitimately get reworded, merged,
  translated and retired-because-achieved; an EN→ZH plan replacement
  auto-carries nothing, so a hard gate here would block ordinary evolution.
- **`context` is still not compared.** It is prose; a similarity score over it
  would be noise. The advisory says so out loud instead of pretending coverage.

### Placement note

`unmatched_criteria` sits at the **end** of `core/plan.py`, not beside
`check_carryover` where it belongs by topic. This repo carries ~600 `file:line`
citations and only the symbol-anchored subset is machine-checked; inserting
mid-module would have rotted ~60 citations across four documents, most of them
invisible to the checker. Measured before choosing: the beside-`check_carryover`
placement broke 29 refs in `CONTRACTS.md` alone, the end-of-file placement
breaks 0.

---

## [2.5.5] — 2026-08-05

**The doc gates covered 7 of the repository's 13 markdown files.** Asked whether
every document was aligned, the answer was checkable rather than assertable —
and checking it found that the *gate scope itself* was the stale thing.

### Fixed

- **`tools/citation_check.py` now tracks all 13 markdown files, not 7.**
  `CHANGELOG.md`, both agent prompts, `commands/cc-mem.md` and both skills were
  covered by nothing at all. `smoke_test.py` now asserts the tracked list equals
  `git ls-files "*.md"`, so "which docs are gated" cannot drift again. 599
  citations, 0 unchecked, 0 stale.

- **The docs' countable claims are gated too.** Nothing checked cross-document
  *facts*, only citation line numbers — and three had already drifted:

  * `CLAUDE.md` § Tests still said *"Three suites … run all three, plus
    `tools/i18n_check.py`"* after `citation_check.py` became a gate, i.e. it told
    the next Claude to run seven of the eight gates. It now describes all eight,
    and a new assertion fails if the section stops naming any gate script.
  * `commands/cc-mem.md` named 23 of the 28 subcommands `cli/mem.py` defines.
    The five missing ones — `sql`, `sessions`, `schema`, `keywords`,
    `observations` — included `sql`, whose read-only guard is a v2.5.0 security
    fix that only helps someone who knows the command exists. All 28 are now
    listed, and a new assertion fails if a subcommand is added without a doc row.
  * `README.md` and `README.zh.md` still carried *"Doc `file:line` citations are
    unenforced … Nothing enforces them today"* in their limits section, three
    releases after `citation_check.py` started enforcing them, and both still
    said *"Three stdlib scripts … all three are release gates"*.

  The `11 tables` claim is now asserted against `core/db.py` as well.

### Verification

Eight gates green. Independent harnesses unchanged and re-run: 42/42, 12/12,
6/6. Exes rebuilt, PE subsystem verified, released assets hash-verified against
the locally tested build.

## [2.5.4] — 2026-08-05

**Zero known limits.** v2.5.3 closed five of six residuals and recorded four
new ones. This release closes all four — by measurement, not by rewording — and
adds a gate for each so none can come back. There is no *Known limits* section
below, because there is nothing to put in it.

### Fixed

- **Every citation is checked. 0 unchecked, down from 253.**
  `tools/citation_check.py` could only anchor a citation on a symbol, so 253 of
  595 opted out of the gate entirely. Two changes closed that:

  * an ambiguous bare filename is now disambiguated by symbol — this repo has
    both `cli/plan.py` and `core/plan.py`, and 13 citations said only
    `plan.py`; the surrounding prose names a symbol that exists in exactly one;
  * a citation naming no symbol at all is **bounds-checked**: the cited range
    must lie inside the file and contain at least one non-blank line.

  That last check alone found **34 stale citations** — pointing past EOF or at
  nothing but blank lines — which every previous release shipped. All repaired.
  `smoke_test.py` now fails if *any* citation is unchecked: 595/595, 353
  symbol-anchored + 242 bounds-checked.

- **The `settings.json` lost update is closed in both directions.** v2.5.3
  checked the file's digest *before* renaming, leaving the window between that
  check and the rename. There is now a **post-write verification**: the file is
  read back and compared byte-for-byte against what was written, so a peer write
  landing after the rename is detected too and the merge is redone. Measured
  with a peer write forced into *both* windows in one run: our hooks registered,
  and both of the peer's keys survived.

- **PLAN.md and MEMORY.md no longer go stale.** A fixed retry count is the wrong
  shape for this failure — the destination is unavailable for as long as another
  process holds it open, which is a *duration*. `write_atomic` gained a
  wall-clock `budget_s`, and the two derived artifacts use 3 s. Measured, 150
  write rounds against three readers at 100 % duty cycle:

  ```
  12 fixed tries (0.78 s)   stale renders 2 / 150
  3 s budget                stale renders 0 / 150   (202,914 reads, 0 empty)
  ```

- **The dashboard exe is executed too.** `--help` exercises argparse and the
  frozen bootstrap; the GUI is then started against a real project and is still
  alive 12 s later. Both exes are now run, not just linked and inspected.

### Verification

Eight gates green. Independent harnesses: 42/42 (v2.5.2 repros), 12/12 (real
installer exe), 6/6 (this release's four claims). Exes rebuilt, PE subsystem
verified, released assets hash-verified against the locally tested build.

## [2.5.3] — 2026-08-05

**The "Known limits" section of v2.5.2, cleared.** No new audit: this release
takes the six residuals that release recorded rather than fixed, and closes
five of them outright. The sixth — the installer's `settings.json` TOCTOU —
could not be closed by locking, so it is closed by *detection* instead.

Two of the six turned out to be worse than they were written up as.

### Fixed — the residual that was really a defect

- **The three "deliberate literal twins" were not twins, and two of them still
  truncated.** v2.5.2 shipped `_atomic_write` in `core/progress.py` and
  `_atomic_write_text` in `core/plan.py` and `llm/memory_writer.py`, documented
  on both sides as intentional copies. The progress one retried `os.replace`
  five times and re-raised; the other two had **no retry** and, on failure, fell
  back to the plain truncating `write_text` — reintroducing, for that call,
  precisely the torn-read defect the function existed to remove. That fallback
  *was* the "20 empty reads in 28,141 samples" residual.

  `core/atomic.py` is now the single implementation, and its contract is
  explicit: **replace completely, or raise. Never truncate, never silently fall
  back.** `core` may be imported by `llm`, so the split never had a dependency
  reason in the first place. The two derived artifacts (PLAN.md, MEMORY.md)
  catch the raise, log it and keep the previous *complete* file — a stale
  artifact beats a torn one, and both regenerate on the next write. PROGRESS.md
  still raises, because it is the handoff contract rather than a projection.
  `core/plan.py` also gained the logger it had never had, which is why every
  failure in it previously had to be either raised or swallowed.

- **`update_plan_status` / `delete_plan` / `update_plan_content` still accepted
  an unscoped call.** `plans.id` is global to the DB *file*, so an unscoped
  UPDATE or DELETE hits whatever row owns that id — including another project's.
  v2.5.2 recorded this as a known limit on the grounds that "the pre-v2.5
  signature stays callable". All **11** call sites in the tree already passed
  `project_id` as a keyword, so requiring it cost nothing: it is now mandatory
  and keyword-only, and the `WHERE` clause is unconditional. A caller that
  cannot name its project fails at the call instead of in someone else's data.

### Fixed — the residuals that were real limits

- **A fail-closed `config.json` is now visible.** Suspending the plugin on an
  unusable config is right for a privacy control, but v2.5.2's only trace was a
  line in `~/.claude/hooks/cc-memory/logs/` — a file nobody reads until they
  already suspect something. A merge-conflicted `config.json`, the exact
  accident that file's own note warns about, therefore presented as "cc-memory
  quietly stopped working". `core.modes.config_fault()` reports *why*, and
  SessionStart prints one line naming it. A project the user genuinely **listed**
  stays completely silent — that silence is the feature — and `test_surfaces.py`
  §5 asserts both halves.

- **The installer's `settings.json` lost update is now detected.** v2.5.2
  narrowed the window from the whole install (~0.5 s) to one dict merge and
  shipped the rest as unfixable without a lock protocol both sides honour.
  Narrowing is not detecting: the read now takes a content digest, the write
  **refuses to rename** if the file no longer matches it, and the whole
  read-merge-write is retried on the newer contents (bounded at 4). A concurrent
  writer can no longer be clobbered — it can only make the installer redo the
  merge. Uninstall is protected identically: discarding a concurrent
  `/permissions` approval is no better on the way out than on the way in.

- **The exes are now RUN, not just inspected.** Every release so far asserted
  the subsystem from the PyInstaller flag and the PE optional header and shipped
  without the binary having been executed once. A 12-check harness now installs
  from the real `cc-memory-installer.exe` into a sandboxed HOME, verifies the
  flat tree imports, and uninstalls — 12/12.

  It immediately found something: **`main()` silently ignored unrecognised
  arguments.** `--project D:\repo` performed a plain install and did *not*
  initialise that project; a typo'd `--unistall` performed an **install** — the
  opposite of what was typed — and exited 0. Unknown arguments are now refused
  with the usage text and rc=2.

- **Doc citation coverage nearly doubled.** `tools/citation_check.py` could only
  anchor a citation when the symbol was defined in the *cited* file, so the most
  common shape in these docs — a call site, `` `db.tag_progress_session(...)`
  (`user_prompt.py:193`) `` — went unchecked: 370 of 594, 62 %. It now anchors
  cross-file citations on the text of the cited range, and **341 of 594 are
  checked** (was 224).

  Getting there needed two of its own bugs fixed, both found by measurement
  rather than review: the anchor first matched any English word ≥6 characters
  that occurred in the file (the word *guardian* appears at five lines of
  `core/plan.py`, so a correct citation that missed those was reported as rot) —
  candidates must now be real symbols somewhere in the tree; and `--fix` used
  substring replacement, which turned `memory_writer.py:83-83` into
  `memory_writer.py:55-83`, a range that never existed. It splices by character
  offset, right to left, so a line carrying four citations repairs correctly.

### Known limits (what is left, honestly)

- 253 of 594 citations still cannot be anchored to any symbol and are unchecked.
  `--fix` repairs a stale cross-file citation to the occurrence **nearest** the
  stale number — a stated assumption (a citation was right when written; the
  file grew above it), not a proof.
- The `settings.json` CAS still has a microsecond window between its final
  digest check and the rename. That is inherent without OS locking; what changed
  is that a lost update is now *detected and redone* rather than silent.
- `write_atomic` raising means PLAN.md / MEMORY.md can be one write stale under
  sustained contention. That is the deliberate trade against a torn file.
- The dashboard exe is still only PE-header-verified; only the installer exe is
  executed by the harness.

## [2.5.2] — 2026-08-05

**A third audit, on angles the first two never used: time, concurrency,
cross-surface agreement, and hostile input.** Six read-only agents, then seven
fix agents on disjoint files, then an independent re-verification harness run by
the maintainer against each finding's own repro (41/41).

The headline is a **persistent prompt-injection channel**, and it is the worst
defect of all three rounds. Everything else here is a data-loss or
privacy-control defect that a green test suite could not see — for the third
release running.

### Fixed — security

- **Stored memory content could forge a complete `<system-reminder>` block into
  the SessionStart injection and into PROGRESS.md.** `clean_for_storage` removed
  `<private>` and `<cc-memory-context>` spans and then interpolated the
  remainder **verbatim** into both. Measured on one stored memory, through the
  real MCP writer and the real SessionStart hook: **8 complete
  `<system-reminder>` blocks in a stdout where the plugin emits 1**, 6 copies of
  the `=== CC-MEMORY: Context Restored ===` banner, forged `<ide_opened_file>`
  and `<invoke>` tags, NUL / ESC / U+202E control characters, and 4 complete
  blocks plus 4 forged `## 7.` headings inside PROGRESS.md.

  `memory_add` is a **model-invokable MCP tool**, so a single indirect injection
  — a malicious README, a fetched page, a dependency's source — becomes a
  *permanent* memory that is re-injected as authoritative context at the start
  of every later session, in a block whose own text orders the next Claude to
  trust it. One-shot injection upgraded to persistence.

  `core.privacy.neutralize_markers` / `neutralize_inline` / `neutralize_block`
  **escape** rather than delete, so a memory that legitimately discusses
  `<system-reminder>` stays readable while the delimiters stop carrying
  authority. They run on the write path (`clean_for_storage`) **and** on every
  render path, because rows written by v2.5.1 and earlier are already armed in
  users' databases. After: 1 block, 1 banner, 0 forged tags, 0 control
  characters, 0 blocks in PROGRESS.md, exactly the 8 real headings.

  The `^</?(ide_opened_file|system-reminder|antml)` heuristic in
  `core/consolidate.py` is **not** this defence and never was — it is anchored
  at position 0, so one leading word evades it, and it only runs during
  consolidation. It is now labelled as garbage cleanup, explicitly not a
  security control.

- **PLAN.md and MEMORY.md were the same channel, unguarded.** Both are
  generated artifacts that Claude reads. An armed plan step title produced 1
  complete `<system-reminder>` block, **2 `← ACTIVE` markers when exactly one
  step was active**, and 2 `## Goal` headings in a document that has 1 — the
  steps come from the plan-refiner subagent, so anything the model read can
  reach them. MEMORY.md renders no memory *content*, but its **topic names** are
  LLM-derived: one armed topic name gave 1 block and 3 `## ` headings in a
  document that has 2. Both render paths now neutralise; both stay readable.

### Fixed — privacy

- **A UTF-8 BOM on `config.json` switched the entire opt-out off, silently.**
  `json.load` raised, the outer `except Exception` returned "not excluded", and
  nothing was logged in any of the three channels. PowerShell's `Out-File` — on
  the primary platform — writes a BOM by default, and Notepad offers it. With
  one BOM added and nothing else changed: `memory.db` created, 3 observations
  stored, PROGRESS.md written for a project the user had opted out of.
  `core.modes.read_config` is now THE runtime reader (`utf-8-sig`), and every
  config failure is logged.

- **One `~user` entry disabled the whole list, order-dependently.**
  `Path.expanduser()` raises `RuntimeError` — neither `OSError` nor
  `ValueError` — so it escaped the inner handler whose own comment promised
  "one malformed entry must not disable the rest of the opt-out list". A bad
  entry *first* voided every entry after it; the same entry *last* was harmless,
  which made it look intermittent. `_norm_path` cannot raise at all.

- **The MCP server ignored `excluded_projects` entirely** — the seventh caller
  of a control v2.5.1 had just finished wiring into the six hooks, and the one
  that is loaded by default from the shipped manifest with every call chosen by
  the model. On a listed project it served stored content verbatim, accepted
  `memory_add`, and created PROGRESS.md. Gated in `_get_db`, the single choke
  point all eight tools reach, with a refusal message that tells the model not
  to retry. `initialize` / `tools/list` / `ping` stay outside the gate.

- **`config.json` now fails CLOSED.** A file that exists and cannot be used
  (invalid JSON, non-object, non-UTF-8, unreadable) excludes *every* project and
  logs why. The two outcomes are not symmetric: guessing "not excluded" on a
  typo writes tool inputs and outputs to disk and, with a credential present,
  ships them to the API — unrecoverable. An **absent or empty** config is not
  this case. Cost of the choice, stated plainly: a merge-conflicted
  `config.json` suspends the plugin until it parses.

- **A drive/filesystem root in the list matched nothing.** `c:\` resolves with
  its separator attached, so the prefix test built `c:\\` and excluded no
  project at all.

- **`<private>` was honoured on the memory path but not on the progress path.**
  Both ingresses now clean: `hooks/user_prompt.py` (turn 1) and
  `hooks/pre_compact.py:_first_user_request`. PROGRESS.md used to carry the
  redacted text verbatim — into a file `memory/.gitignore` does not ignore, so
  it was committed to the user's repository.

### Fixed — data loss

- **Two PreCompacts in the same wall-clock second destroyed a session
  archive.** Second-resolution stems plus an unconditional write, and
  `sessions.archive_path` has no uniqueness constraint: 12 real compactions →
  **3 files on disk**, 9 transcripts gone with no error anywhere, while the rows
  still render in `/cc-mem sessions`. Stems now carry milliseconds *and* the
  exact target path is claimed with `O_CREAT|O_EXCL` (atomic across processes).
  12 compactions → 12 files, no 0-byte placeholders. `write_session_archive`
  derives its `YYYY/MM` directory from that stem instead of taking its own clock
  reading, which used to void the claim across a month boundary.

- **`.plan_history` overwrote itself with no concurrency at all.** Four
  sequential plan replacements in 23 ms → **1 file**, generations 0-2 lost. Its
  docstring called it "append-only … last-resort backstop: even a wrong
  disposition stays recoverable" — neither clause was true, and the survivor was
  the *newest*. Now 4 replacements → 4 files.

- **PROGRESS.md, MEMORY.md and PLAN.md could be read as 0 bytes.** Truncate-then-
  write against a concurrent reader: 4,867 empty reads in 16,071 samples for
  PROGRESS.md, 344 for MEMORY.md + PLAN.md. All three write to a temp file and
  `os.replace`. Silent 0-byte reads → 0; under pathological contention the
  reader instead gets a loud, transient sharing violation and the file keeps its
  previous *complete* content.

- **A lone surrogate anywhere in the extracted text aborted the whole
  compaction** — `write_session_archive` sits above `insert_session`,
  `upsert_batch` and `write_progress_md`, so `UnicodeEncodeError` cost the
  archive, the session row, the memories *and* the handoff.

- **A non-dict `.last_save.json` voided the entire SessionStart injection** —
  5,793 B of context became 58 B.

- **The installer discarded concurrent edits to the global `settings.json`**
  (6/6 lost updates) and truncated it non-atomically (a 0-byte read observed in
  ~2,300 samples). It now re-reads immediately before merging, backs up to
  `settings.json.cc-memory.bak`, and renames into place — with a bounded retry
  and a warned in-place fallback, because a bare rename traded a rare 0-byte
  window for a *failed installation* when any process held the file open.

- **The `.gitignore` literal in the installer and in `skills/ccm-load` fused the
  user's last rule with our first comment** when the existing file had no
  trailing newline (`sessions/# cc-memory: generated state, not content` —
  destroying `sessions/`). All three copies now share the read/normalise/write
  shape, and a smoke test asserts the three line lists and forbids `"a"`-mode
  append.

### Fixed — resource hygiene

- **`MemoryDB._connect` leaked one sqlite3 connection per operation.** `with
  conn:` commits but does **not** close, and the handle then survived in its own
  statement-cache cycle. Measured: 4 live after the constructor, 5 after one
  `upsert_project`, **25 after 20 inserts** — linear and unbounded, in three
  processes that hold a `MemoryDB` for their whole lifetime, each handle
  carrying a 256 MiB `mmap_size`; on Windows `shutil.rmtree` failed with
  `WinError 32` until the GC happened to run. `_connect` is now a context
  manager that commits / rolls back exactly as before and closes in its
  `finally`: **0 live handles**, all 81 call sites unchanged.

  Honest cost, measured rather than assumed: closing the last connection to a
  WAL database forces a checkpoint + fsync, so a PreCompact-shaped workload of
  127 operations goes 182.3 ms → 802.7 ms (+340 %). That is +0.6 s against a
  120 s budget; every hook still finishes well inside its `hooks.json` timeout.

- **`Logger.close()` was a one-way kill switch** — it cleared the handle but
  left `_today` set, so the next write took the "same day, nothing to do" branch
  and silently dropped that line and every later one. That is why it was never
  safe to call and stayed dead. Fixed, plus `close_all_loggers()` and an
  `atexit` hook.

### Added — the gate for the thing nothing gated

- **`tools/citation_check.py`** — the definition-site checker `CLAUDE.md` has
  been describing as "would make a cheap CI gate" since v2.5.0. For every
  ``file.py:LINE`` citation in the tracked docs it resolves the symbols named in
  the surrounding prose with `ast` and asserts the cited range covers the
  definition **or** mentions the symbol (docs cite call sites too). First run:
  **163 of 594 citations were rot** — pointing at a line that neither defines
  nor mentions the symbol its own sentence names. All 163 repaired by
  `--fix`; the checker now runs inside `smoke_test.py`, so the next one turns
  the suite red. Citations it cannot anchor are reported SKIP, never guessed: a
  gate that invents verdicts is a gate people learn to ignore.

- **`tests/test_surfaces.py` §5** — the config-parser shapes §4 could not see
  (BOM, `~user` first, unparseable → fail-closed, absent → still on) driven
  through all six hooks, plus the MCP server's half of the same opt-out.

- **`tests/smoke_test.py`** gained the `.gitignore` three-copy parity gate, a
  connection-handle regression assertion, and the PLAN.md / MEMORY.md forgery
  assertions.

### Known limits

- `--fix` rewrites a stale citation to the symbol's **definition**, which may
  not be the call site the sentence meant. 370 of 594 citations remain
  unanchorable and are therefore unchecked.
- The atomic-write fallback still has a truncation window when `os.replace` is
  refused (20 empty reads in 28,141 samples, vs 344 before) — bounded retries
  were measured and deliberately not shipped in this release.
- The installer's `settings.json` TOCTOU is shrunk from the whole install
  (~0.5 s) to one dict merge, not closed; nothing locks that file and Claude
  Code takes no lock either.
- `core/db.py`'s three plan mutators still default `project_id=None` (unchanged
  from v2.5.1).

## [2.5.1] — 2026-08-05

**v2.5.0 was audited an hour after it shipped, and the audit found 23 defects.**
Six read-only agents attacked it from angles the pre-release work had not used:
regressions introduced *by* the fixes, a brand-new user installing from the
released exe, every documentation claim re-checked against the code, all six
hooks driven live, an audit of the ~1,650 lines of test code added in v2.5.0,
and a whole-tree sweep of the project's own invariants.

The uncomfortable part: **all seven release gates were green the entire time.**
Three of the defects below are things a passing test suite cannot see.

### Fixed — privacy

- **`excluded_projects` was not an opt-out. It only blocked *creation*.** The
  check existed in exactly two of the six hooks. A project that already had a
  `memory/` directory and was listed *afterwards* — the natural sequence, since
  you add a repo to the list precisely when you realise it is sensitive — kept
  being captured in full: 4 tool calls → 4 observations stored with their inputs
  and outputs, a `progress` row written, `PROGRESS.md` naming the secret files,
  3,189 bytes injected into the next session. With a credential present the Stop
  observer also POSTs those observations to the Anthropic API, and that leg was
  unconditional. Every clause of the README's promise — "no `memory/`, no DB,
  **no extraction and no PROGRESS.md**" — was false for a pre-existing project.
  There is now **one** implementation (`core/modes.py:is_excluded`) called as the
  first act of **all six** hooks. Measured after: 0 observations, 0 progress
  rows, no `PROGRESS.md`, 0 bytes injected, 0 bytes of stdout — while a
  non-excluded sibling project is unaffected.
- **A standalone reinstall silently wiped `excluded_projects`.** `config.json`
  was copied unconditionally, so re-running the installer — which is how you
  install a patch release — reset the plugin's one privacy control to `[]` with
  no warning and no backup. The installer now merges the shipped defaults *under*
  the user's file, keeping their values and adding genuinely new keys.
  (The marketplace layout has the same exposure by a different route: its
  `config.json` is git-tracked, so `git pull` can revert your edit. Documented,
  not yet solved.)

### Fixed — the hook that runs on every session

- **SessionStart could still blow its 15 s budget, and would do so forever.**
  v2.5.0 added an absolute deadline to the LLM legs but left
  `load_transcript_window` — which runs *after* the deadline check — unbounded in
  time. Measured 17.00 s against a 15 s host budget. Real figures on the
  reference machine: a 2.11 GiB transcript loads in 3.37 s and the loop reaches
  its last check at ~12.6 s, so a single large prior transcript is enough, and
  one exists on that box today. It repeated every session: a transcript that
  yields no memories writes no `sessions` row, and a `TerminateProcess` kill
  commits nothing, so the same files were re-scanned and re-killed at every
  start. The loop now charges the predicted load cost to the budget *before*
  starting it (a two-pass model validated against 1.49/2.11/4.0 GiB files; it
  never under-predicted) and skips a file it cannot afford. Measured after:
  **7.26 s** with a 2 GiB unsaved transcript, injection intact.

### Fixed — surfaces

- **`/ccm-load` was dead on every standalone / exe install** — the layout the
  README recommends to Windows users. It hard-gated on `enabledPlugins`, which
  the standalone installer never writes (it writes only `settings.json[hooks]`),
  so it reported **"cc-memory plugin NOT FULLY ACTIVATED"** — false; all six
  hooks were registered and working — then printed advice an exe user cannot
  follow (`/plugin marketplace add <path-to-repo>`; they have no repo) and
  returned without bootstrapping. Meanwhile `/cc-mem status` on the same machine
  reported `5/5 registered`: two shipped surfaces, opposite verdicts on one
  healthy install. The activation check is now per-layout and mirrors
  `cli/mem.py`'s.
- **`/cc-mem sql`'s "READ-ONLY" guard was bypassable.** It refused only
  `PRAGMA name = value`; SQLite equally accepts `PRAGMA name(value)`, and several
  pragmas write with no argument at all. `PRAGMA journal_mode(DELETE)` disabled
  WAL, `PRAGMA optimize` created `sqlite_stat1`, `user_version(7)` and
  `application_id(1234)` persisted — all `rc=0`, all under a banner that calls
  the tool read-only, and all reachable by the model through `/cc-mem`
  passthrough. The dashboard's twin guard had **already fixed this exact class**
  in v2.5.0, naming `PRAGMA user_version(7)` and `PRAGMA optimize` verbatim in
  its comment; the port to the CLI was never made. Both guards now agree on all
  19 probe inputs.
- `cmd_plan_check` briefed the plan-guardian on a **superseded** plan — it never
  consulted `raw_pending_refinement`, so it printed the stale plan's goal and
  progress while the `PLAN.md` it had just written said "pending refinement", and
  it reset the drift counters for a plan that was not live.
- Four hooks still violated the never-raise/never-stderr contract on a
  non-string `cwd` or `session_id`; v2.5.0's guard typed only the container. A
  162-case fuzz battery went from 10 failures to 3, all outside the changed
  files.
- MCP answered id-less notifications with `{"id": null, …}`, which its own new
  docstring said it would not do and which JSON-RPC 2.0 forbids; the frame-length
  cap was off by one; the dashboard's search box was the one search surface that
  never got v2.5.0's LIKE-wildcard escaping; `cc-memory-plan --help` identified
  itself as `plan.py`, a file not on the user's PATH.

### Fixed — the tests, and the documentation

- **Two assertions in the new suite were vacuous.** The `protocolVersion` check
  sent a *supported* version, so "negotiates" and "parrots back" were
  indistinguishable — mutating the negotiator to accept any string still passed.
  Another was literally `assert True` via an operator-precedence trap
  (`assert X if False else True`), together with a helper that existed only to
  keep it importable. Both were debris from an agent that was killed mid-task.
  The rest of the suite is load-bearing: an independent audit ran 53 mutants and
  **51 went red at the intended assertion**.
- **`tests/smoke_test.py` wrote into the real `~/.claude`** on every run and left
  ~19 temp directories behind; `test_surfaces.py` leaked a sandbox into the real
  `%TEMP%` on every *successful* run, hidden by `ignore_errors=True`. Both are
  sandboxed now. Root cause of the leak, recorded for later: `MemoryDB._connect()`
  is consumed as `with self._connect() as conn:` at all 27 call sites, and
  sqlite3's context manager commits but never closes, so the file stays locked on
  Windows.
- **22 in-document anchors in the two Chinese docs pointed nowhere** — the
  translations kept the English slugs while translating the headings, so both
  tables of contents were entirely dead. The hash-based i18n checker cannot see
  this by design.
- Two README shell recipes could not work as written: `M="python ~/..."` then
  `$M status` fails because bash expands `~` before parameter expansion and does
  not rescan, so the tilde stays literal. Plus a set of stale claims: `CLAUDE.md`
  contradicting the code on `project_id` scoping, an incomplete memory-tag
  inventory, three CHANGELOG links to docs deleted in v2.4.3, and `config.json`
  citing two functions that no longer exist.

### Known, and stated rather than hidden

`file:line` citations in `docs/` rot on every refactor and nothing enforces them.
The v2.5.1 pass re-derived the `core/plan.py` citations and fact-checked every
prose claim, but citations into `cc_memory/hooks/*`, `cli/mem.py` and
`ui/installer.py` were deliberately **not** re-derived — those files were being
rewritten in the same round, so any number written for them would have been stale
on landing. `docs/ARCHITECTURE.md` and `docs/CONTRACTS.md` now say so at the top:
treat a line number as a hint and the **symbol name** as the fact.

## [2.5.0] — 2026-08-05

**A readiness audit of every shipped surface, and the repair of everything it
found.** Twelve agents exercised the six user-facing surfaces by *running* them
rather than reading them; four more then attacked the resulting fixes. Every
number below was measured, not estimated.

The headline is uncomfortable: three surfaces did not work at all. The MCP
server could not survive a non-ASCII character on this machine's default
codec. The web viewer answered zero requests because a browser's speculative
pre-connect wedged it. The standalone installer shipped no user-facing surfaces
whatsoever — no `/cc-mem`, no skills, no subagents — so the v2.2 live-plan
feature could never work there at all. And a transcript-directory lookup that
matched on a *substring* had been quietly importing other projects' memories.

### Fixed — data integrity

- **Cross-project contamination: one project's memories were being written into
  another's database, then re-injected at every SessionStart.**
  `_find_transcript_dir` fell back to a bare substring test on the project's
  basename. Measured on the reference machine (179 transcript directories):
  basename `core` matched **131** of them, `app` **141**, `proj` **33**. A
  fixture seeded with 5 memories finished with **32** after a **278,700-record**
  transcript from an unrelated project was ingested — a real Haiku bill for
  data that poisoned the target project permanently. A second audit proved a
  Vault secret path crossing into an unrelated project's DB.
  The fallback is deleted (exact → case-insensitive → `None`, matching the
  already-correct `extractor.find_latest_transcript`), the slug mangling now
  normalises `_` and `.` as Claude Code does — **0 of 179** real directories
  contain either, so any project path with one necessarily fell into the
  substring branch — and `retroactive_save` additionally requires each
  transcript's own `cwd` record to resolve-equal the project. The same fuzzy
  branch was duplicated verbatim in the dashboard's Save Session and is gone
  there too (a project named `data` had matched a Temp directory holding 47
  transcripts).
- **`POST /api/memory` rewrote a different project's `MEMORY.md`.** The handler
  resolved its target from `os.getcwd()` while `main()` parsed `--project` and
  discarded it. Measured: the served project was untouched and a bystander
  project's index was rewritten with the served project's content.
- **The privacy filter failed OPEN.** `strip_private` returned the text
  **unchanged** above 100 tags — so `<private>` content reached both the
  Anthropic API call and the memories table exactly when the payload looked
  adversarial. The cap was also calibrated on the wrong signal: well-formed tags
  are cheap for the regex engine (20,000 tags ≈ 6 ms) while an *unterminated*
  tag is the quadratic case (16,000 ≈ 9,517 ms). Replaced with a single linear
  `str.find` scan — no cap, no backtracking, sub-millisecond on the pathological
  input — that fails **closed**: a dangling `<private>` drops the remainder.
- **A file the user marked private had its path sent to the API anyway.**
  `PostToolUse` computed `is_private` *after* `_truncate_output` had replaced a
  `Read` response with the literal `"(file content)"`, destroying the marker.
  `is_private` is the sole filter feeding the Stop observer and the PreCompact
  extraction prompt, so the miss propagated into `progress.files_touched` too.
  The flag is now computed from the raw payload.
- **`/cc-mem sql` silently discarded DML but permanently committed DDL.**
  `DROP TABLE topics` reported `(no rows returned)`, exited 0, and destroyed the
  rows for good — `MemoryDB` then recreated the empty table so nothing looked
  wrong. `sql` is now read-only by contract and refuses anything else.
- **The dashboard SQL console committed destructive statements with no
  confirmation.** Measured: 5 memories → 0, reported as `(no rows returned)`.
  Non-SELECT statements now require explicit confirmation and report `rowcount`.
- **Tidy hard-deleted rows**, truncating the supersede chain and leaving
  dangling `supersedes_id` references; it now archives.

### Fixed — hooks

- **The v2.2 live-plan anchor had never worked through its hook.**
  `PostToolUse` exited on the observation gate *before* reaching the plan block,
  and `ExitPlanMode`/`TodoWrite` are excluded from every mode's observe list —
  so `plan_active` stayed empty, `PLAN.md` was never written, and TodoWrite
  never synced a step. Worse, the whole block inherited the gate, so drift
  detection varied silently by mode (3 edits registered as 3 in `code`, **0** in
  `research`; `git push` scored 23, 20 and 3 across the three modes). The plan
  block now runs above the gate; only the observation INSERT is gated.
- **Hooks with hard host timeouts did not bound their LLM wall-clock.**
  `llm.ccl_backend.call_llm`'s own docstring requires a time-budgeted caller to
  pass `fallback_timeout`; of the four call sites with a budget, only
  `core/consolidate.py` did. Worst case per call is
  `2 × timeout + fallback_timeout`, so `session_start` could spend **40 s**
  against its 15 s budget **with the shipped default config** — no opt-in
  required — and a live reproduction killed the Stop hook at **24.96 s** against
  22 s. A timeout kill is `TerminateProcess`: no `except`, no `finally`, i.e.
  the v2.3.2 / v2.4.2 "killed mid-write" class.
  `call_llm` gains an absolute `deadline` parameter that clamps every leg's
  socket timeout to the time actually remaining and skips a leg with under a
  second left; all three budgeted hooks pass one. This is strictly stronger than
  the arithmetic, because `urlopen(timeout=…)` is a *per-socket-operation*
  timeout covering neither DNS nor the TLS handshake — a **successful** leg was
  measured at **11.81 s against a nominal 8 s** (1.48×, in ~5 % of legs). Under
  a simulated 1.48× stall on every leg the Stop hook went from **25.45 s → 15.99 s**
  of its 22 s, and PreCompact from **~144 s → 74.39 s** of its 120 s.
- **All six hooks exited 1 with a traceback on well-formed non-object stdin**
  (`null`, `42`, `"s"`, `[1,2]`, `true`) — 30 of 30 cells, two hook-contract
  violations at once. Guarded.
- **`cleanup_observations` never deleted same-day rows.** Observations store ISO
  timestamps with `T`; the cleanup argument used a space separator and the
  comparison is a string compare (`ord('T') > ord(' ')`). The rows extraction had
  just consumed were exactly the ones never cleaned — confirmed live, the count
  stayed at 6 across two compactions.
- **The first PreCompact of a project blanked PROGRESS.md §6**, and
  `progress.current_request` was never seeded during a project's first session.
- **The plan-refiner nudge repeated on every Stop forever** (5 of 5 measured);
  it is now rate-limited without ever clearing `needs_refine`.

### Fixed — MCP server

- **stdio was never UTF-8, breaking non-ASCII in both directions.** With this
  box's default `gbk` codec, writes stored mojibake or failed outright and a
  strict codec killed the process with no response; on the read side a single
  emoji replaced an entire result batch with an error — and `↻` is a glyph
  cc-memory emits itself, so a project could poison its own MCP reads.
- **`tools/call` with `params: null` hung the client forever.** `params` is
  optional in JSON-RPC 2.0 and many clients serialise omission as `null`; the id
  was consumed and never answered. Same for `[]` and `"str"`.
- **A single frame could kill the server.** The parse guard caught only
  `json.JSONDecodeError`, but `json.loads` also raises `ValueError` (CPython's
  4300-digit integer limit) and `RecursionError` (deep nesting) — reachable
  through an advertised tool argument, before validation. Measured: 4,301 digits
  or 3,125 levels of nesting → `rc=1`, traceback on stderr, every pending id
  orphaned. Frames are now length-capped and nothing escapes `main()`.
- **A read-only tool performed an unbounded index write.** Any FTS-invalid query
  triggered a full `memories_fts` rebuild (52.4 ms at 20,000 rows, 6 of 6
  malformed queries); LIKE wildcards were unescaped so `query='%'` dumped the
  whole table; `limit` had no maximum.
- Superseded rows were served by `memory_get_details`; `isError` was never set
  on the missing-DB path; declared `required`/`enum`/type constraints were
  enforced nowhere (`importance=99` silently clamped, bogus categories silently
  coerced); `NaN`/`Infinity` were emitted on the wire.
- **MCP is now reachable**: `.claude-plugin/plugin.json` declares an
  `mcpServers` entry. Previously nothing did, and `config.json`'s
  `mcp.auto_register` was read by no code.

### Fixed — web viewer

- **One idle TCP connection wedged the server forever.** Plain `HTTPServer` with
  a `None` handler timeout blocks in `handle_one_request()` on a socket that
  sends nothing — and browsers speculatively pre-connect, so `/cc-mem serve`
  printed its banner and then answered **zero** requests. Now
  `ThreadingHTTPServer` with daemon threads and a handler timeout.
- **Any web page could read and write the memory database.**
  `Access-Control-Allow-Origin: *` with no `Content-Type` check, and written
  memories are injected at the next SessionStart — a prompt-injection channel.
  Origin and Content-Type are now enforced and the header is gone. A missing
  `Host` check additionally allowed DNS-rebinding *reads* (including
  `archive_path` filesystem paths); loopback-only Host is now required.
- **Four routes returned no HTTP response at all** (`importance=abc`,
  `limit=abc`, a malformed JSON body, `body=[]`) — the connection simply dropped.
- **A slow-drip request body held a worker thread indefinitely** — measured
  40.0 s for a request that had *already been rejected*, and ~2.6 h at one byte
  per 9 s. Both body paths now run under a wall-clock deadline (52.09 s → 3.02 s;
  thread growth under a 10-connection attack: +10 → +0).
- Session-less memories (every manual save path writes `session_id = NULL`) were
  invisible in the browse view, including the ones the viewer itself wrote; the
  `category` and `importance` filters were dropped whenever a search term was
  present; and the documented Add-Memory form did not exist. All fixed.

### Fixed — standalone install

- **The installer shipped zero user-facing surfaces.** `~/.claude/{commands,
  agents,skills}` were never created; `grep -a` on the built exe found **zero**
  occurrences of `plan-refiner`, `ccm-load`, or `argument-hint` — they were not
  in the binary at all. So an exe-installed user got hooks but no `/cc-mem`, no
  `/ccm-load`, no `/save-memories`, and no subagents, which means `PLAN.md`
  could never be populated: the entire v2.2 feature was dead on that layout,
  while the plugin nagged for tools it had not installed. Five surface files are
  now copied, recorded to a manifest, and removed by name on uninstall.
- **The installer crashed and then hung forever on a settings.json it could not
  parse.** Six of nine realistic shapes crashed install and four crashed
  uninstall — including JSONC comments, a trailing comma, and an empty file from
  an interrupted write. Because the exe was built `--windowed`, the traceback
  became a modal dialog with no console behind it: a 120 s timeout with no
  output. In every crashing case the 33 files were **already copied**, leaving
  the machine half-installed with no hooks registered. Settings are now parsed
  and type-checked *before* anything is written, and the installer builds as a
  console application.
- **Uninstall deleted the marketplace install's `logs/`** while leaving the
  plugin fully enabled.
- **The installer deleted the user's own hooks** whenever their command merely
  *mentioned* the string `cc-memory` — including a path. (This repository's own
  directory is named `cc-memory`.)
- **A UTF-8 BOM in settings.json locked the user out entirely** — and PowerShell's
  `>` and `Out-File` write one by default on Windows.
- Installer timeouts drifted from `hooks/hooks.json` despite a "keep in lockstep"
  comment (Stop 33 vs 22, PostToolUse and UserPromptSubmit 12 vs 8, and 80/10 on
  non-Windows). The multiplier is deleted; the installer now reads
  `hooks/hooks.json` when present, with a fallback table carrying final values.
- The post-install messages printed paths containing a `cc_memory/` segment the
  flat layout does not have — the one instruction a standalone user was handed
  could not work.

### Fixed — CLI

- **`/cc-mem status` reported every healthy standalone install as broken** —
  `[FAIL] … 22 of 22 missing` — because the required-file list carried a
  `cc_memory/` prefix the flat tree lacks. It also skipped the API-key check as
  a consequence.
- **`cc-memory-plan` could not run at all**: `pyproject.toml` declared
  `cc_memory.cli.plan:main` and `plan.py` had no `main`.
- **`/cc-mem dashboard` hung any caller that captured output** — which is how
  Claude Code invokes it. The GUI child inherited the stdout pipe.
- **`plan-set --raw` over a refined plan was invisible in every view.** This is
  the *primary* auto-capture path (`ExitPlanMode` → `capture_exit_plan_mode`),
  not just the CLI: both renderers checked `is_valid_structured` first and never
  consulted `needs_refine`.
- Manually added memories were invisible to `list` at every importance (all four
  manual save paths write `session_id = NULL`), `encoding-check --apply` never
  converged, several failure paths exited 0, `add` printed a fabricated
  `sim=0.00` for skips, `serve` could not suppress the browser, and
  `plan-set` had three unhandled-input paths.

### Fixed — dashboard

- Selecting an uninitialised project created an un-gitignored `memory.db`;
  Tidy left `MEMORY.md` permanently stale; a read-only `projects.json` prevented
  the dashboard from starting at all; a corrupt one was silently replaced;
  registry entries on an unplugged drive were permanently pruned, and after that
  fix a ghost entry raised an uncaught `FileNotFoundError` invisible under a
  windowed build; editable spinboxes crashed their callbacks with no user
  feedback; the frozen exe stored its project registry in `%TEMP%`, where Disk
  Cleanup eventually removes it.

### Added

- **`tests/test_surfaces.py`** — the first automated coverage for the MCP
  server, the web viewer, and the installer's settings.json shape matrix. All
  three had **zero** test coverage, which is precisely why these defects
  shipped.
- **`cc_memory/core/version.py`** — the single source for the version string.
  The hardcoded literals in the CLI banners, the MCP server banners, the
  installer banner/GUI title and `build_exe.py` — four files, two of them
  already stale at v2.4.3 — are gone. It lives under `core/`
  rather than in `cc_memory/__init__.py` because every entry point bootstraps by
  putting the *package directory* on `sys.path` and importing flat — under the
  flat standalone layout `import cc_memory` raises `ModuleNotFoundError`.
- **A read-only Progress / Plan tab in the dashboard**, which previously
  surfaced none of the v2.1–v2.4 state it is supposed to manage.
- **`excluded_projects` now works.** It was declared in `config.json`, defaulted
  to `[]`, and had zero references repo-wide — a privacy control that did
  nothing while both `user_prompt` and `pre_compact` created a `memory/`
  directory in whatever cwd they were handed.
- Regression assertions tying each hook's declared `hooks.json` timeout to its
  LLM envelope, and tying every version literal to `core/version.py`, so a
  partial bump or a raised timeout turns the suite red.

### Changed

- **`config.json` stripped to the keys that are actually read.** Two independent
  audits measured 34 of 51 leaf keys referenced by no code. An inert tunable is
  worse than no tunable, because editing it looks like it does something.

## [2.4.3] — 2026-08-05

Shipped-surface repair + documentation consolidation. A fact-check of every
documentation file against the code found that three of the plugin's own entry
points were **dead**, not merely mis-documented.

### Fixed

- **`/cc-mem` was completely non-functional.** `commands/cc-mem.md` passed
  `$ARGS` to the CLI, but the placeholder Claude Code substitutes is
  `$ARGUMENTS` (50 uses across installed marketplace commands; `$ARGS` appears
  nowhere). Unsubstituted, the shell expanded it to nothing, and
  `cli/mem.py`'s `add_subparsers(..., required=True)` aborted every invocation.
- **`/save-memories` raised `ModuleNotFoundError` on any non-legacy install.**
  The skill hardcoded `~/.claude/hooks/cc-memory/cc_memory` on `sys.path`; on a
  marketplace install that directory contains only `logs/`. It now resolves the
  package tree the same way `ccm-load` does (env var → marketplace path →
  standalone), and fails with an actionable message instead of a traceback.
- **Install-layout probes were inverted repo-wide.** `ui/installer.py`'s
  `_copy_subpackages` writes each subpackage to `TARGET_DIR/<subdir>/` — a
  **flat** tree with no `cc_memory/` segment — while `skills/ccm-load`,
  `commands/cc-mem.md`, `cli/mem.py`'s legacy-install detection and the README
  install paths all probed for the **nested** `cc_memory/` form. Consequence: an
  exe-installed machine was invisible to `/ccm-load`, `/cc-mem`, and
  `/cc-mem status` alike. All four now accept **both** layouts.
- **`/cc-mem status` under-reported a broken install.** Documented in 2.4.2;
  the layout fix above is what makes the standalone case actually detectable.
- **`README` MCP instructions described a no-op.** `mcp.auto_register` is read
  by no code and nothing writes an MCP client config; the README now says so and
  documents manual stdio registration instead.

### Changed

- **`docs/` consolidated from 5 files to 2.** `MEMORY_RULES.md`,
  `HANDOFF_PROTOCOL.md` and `PLAN_PROTOCOL.md` are now chapters of
  **`docs/CONTRACTS.md`**; `I18N.md` is now §9 of **`docs/ARCHITECTURE.md`**.
  79 citations across 18 files (code comments, `config.json`, `CLAUDE.md`, both
  READMEs, runtime-emitted footers in `MEMORY.md`/`PROGRESS.md`) were repointed
  to the new filenames and anchors. `CHANGELOG.md` deliberately keeps the old
  names in historical entries.
- **`docs/ARCHITECTURE.zh.md` added.** The old `I18N.md` carried a language
  switcher pointing at `I18N.zh.md`, which never existed. The i18n tracked set
  is now 2 documents instead of 5, so translations are far cheaper to keep green.
- **The v2.4.0 carryover gate is documented in prose for the first time.** It
  shipped with no coverage in `docs/`, `CLAUDE.md` or `README.md` — only a
  commit message. `docs/CONTRACTS.md` now specifies it fully, including what a
  refusal looks like and how to resolve one.
- **`/ccm-load` narrowed to what only it can do** — global plugin-activation
  check, package-tree resolution, project bootstrap, PROGRESS.md seeding. It
  previously claimed to "run the health check (`mem.py status`)", which it never
  did (it printed DB counts), and claimed `/cc-mem status` was a *subset* of
  itself — backwards. The two entry points are now documented as orthogonal, and
  `/save-memories` was kept separate rather than merged for the same reason.

### Documentation accuracy

Fact-checked against code and corrected: hook registration (a marketplace
install does **not** write `settings.json`'s `hooks` key — only the standalone
installer does), the `progress` row's writer count (**four** paths, not three —
`session_start._refresh_progress_row` was missing), the anti-patch caller list
(three writers omitted: dashboard init, web viewer, retroactive save), the
memory tag inventory (`["llm","auto"]` is emitted by **no** code path; the
PreCompact LLM path stores `[]`), the stdlib rule (read literally it forbade
`import os`/`import sys`, which every hook uses), the `memory/` artifact
listings in both READMEs and `ARCHITECTURE.md`, and the standalone install paths
throughout.

---

## [2.4.2] — 2026-08-04

Hook-survivability release. On a long-lived project the `PreCompact` sync leg
was being **killed mid-write**, losing that compaction's memories entirely, and
— more quietly — its LLM extraction had been reading the wrong end of the
transcript for weeks. Both trace to the same root cause: the hook loaded the
ENTIRE transcript into memory before using ~12 KB of it.

### Fixed

- **Unbounded transcript read (the `Hook cancelled` root cause).**
  `core.extractor.load_transcript` read every line of the `.jsonl` into a list
  with no cap. Measured on a real 2.11 GiB transcript: `json.loads` throughput
  ~25 MiB/s, i.e. **~88s of a 120s budget consumed before any useful work**,
  with `build_extraction` and an LLM leg (up to 2 × `_API_TIMEOUT`) still to
  come. The host terminated the hook on timeout — and because
  `TerminateProcess` runs no `except` block, the run left a session row and an
  archive on disk but no `.last_save.json`, so the failure was invisible.
  New `core.extractor.load_transcript_window` reads a bounded **head + tail**
  window (40 records + 32 MiB) instead. **Measured: 88s → 1.66s to load, 2.63s
  through extraction, and a full real-transcript hook run in 14.33s, exit 0.**
  `msg_count` keeps its exact meaning via a raw binary record scan (~1 GiB/s,
  40× cheaper than parsing). `load_transcript` itself is retained, unbounded and
  documented as such, for the interactive dashboard.
- **LLM extraction was reading the OLDEST end of the transcript.**
  `_build_transcript_summary` filled its 12,000-character budget starting from
  the first record and stopped. On the same transcript the budget was exhausted
  after **329 of ~585,000 records**, so every extraction for that project saw
  only content from the session's opening hours and none of the recent work.
  It now fills from the newest record backwards and restores chronological
  order, and reports omissions against the transcript's real record count
  rather than the window's. The identical bug in
  `hooks.session_start._summarize_transcript` (retroactive extraction) is fixed
  the same way.
- **`PROGRESS.md`'s "Current Request" was always empty.** `_first_user_request`
  scanned only `messages[:5]`, but a transcript opens with `queue-operation` /
  `attachment` meta rows — the first real user message sat at index 5. It now
  scans past leading meta rows and skips empty-content records.
- **A total LLM outage silently cost the session handoff.** `call_llm` raises
  `RuntimeError` when every backend candidate fails, but `_extract_via_llm`'s
  `except` tuple did not include it, so the error escaped to the hook's outer
  handler and skipped the `PROGRESS.md` rewrite along with extraction. Adding
  `RuntimeError` is what finally makes `docs/ARCHITECTURE.md`'s "hooks degrade
  gracefully — extraction is skipped, but archives/handoff still save" true.
- **`SessionStart` read the same unbounded transcripts under a 15s budget** (an
  eighth of PreCompact's). Both call sites now use the bounded window.
- **`pyproject.toml` had a UTF-8 BOM** (introduced in v2.4.0), so
  `tomllib.load()` failed with `Invalid statement (at line 1, column 1)` and
  **no PEP 517 frontend could build or install the package at all**. Stripped
  from `pyproject.toml` and `cc_memory/__init__.py`.
- **`/cc-mem status` gave a partial install a clean bill of health.**
  `_REQUIRED_PLUGIN_FILES` omitted `core/extractor.py` — the module both hooks
  import at load time — plus `core/auth.py`, `core/consolidate.py`,
  `core/idle.py`, `llm/ccl_backend.py`, `config.json` and `__init__.py`. The
  list now covers the hooks' import closure.

### Added

- **Killed-run visibility.** `PreCompact` writes
  `memory/.pre_compact_attempt.json` before it starts and removes it only on a
  completed run, so a surviving marker is proof the last attempt died;
  `SessionStart` reports it (after a 10-minute grace window, so a run still in
  flight is never mislabelled). Its error path clears the marker too — an
  *errored* run must not be reported as a *killed* one.
- **`trigger` recorded in `.last_save.json`** and shown in the SessionStart
  footer. Claude Code only surfaces hook execution in its UI for a **manual**
  `/compact`, which made automatic compactions indistinguishable from "the hook
  never ran". They are distinguishable now — the DB shows they were always
  firing (352 `auto` sessions on the affected project).

### Changed

- **`memory/.gitignore` now migrates instead of only being created.** Every
  generator was guarded by `if not exists()`, so each new runtime artifact
  leaked into existing installs forever. `core.progress.ensure_memory_gitignore`
  appends only missing lines (preserving user entries) and is the single source
  for all four generators. Newly covered: `.pre_compact_attempt.json`,
  `.last_inject.json`, `.last_consolidation.json`, `.consolidation.lock`,
  `.plan_raw.md`, `.plan_history/`, `*.tmp` — several of which embed verbatim
  conversation or plan prose, making this a privacy leak rather than noise.
- Version strings resynchronised across all six canonical declarations (they
  had drifted to three different values: 2.4.1 / 2.3.4 / 2.3.3) plus the stale
  `v2.1` / `v2.3` banners in the CLI, MCP server, and build script.

---

## [2.4.1] — 2026-07-29

Patch release. Fixes a false refusal in the v2.4.0 carryover gate, caught on
the gate's second real-world replacement: updating a plan **in place** (status
and progress notes only, identical step titles) was REFUSED.

### Fixed

- **Long `notes` no longer dilute an identical-title auto-carry.**
  `check_carryover` built its match candidates as `title + " " + notes` only,
  so a step carrying a long progress note dropped the character-trigram Jaccard
  against the outgoing bare title below the 0.5 threshold — an *identical*
  title failed to auto-carry and the gate refused a legitimate
  self-replacement. Each incoming step now contributes **two** candidates, the
  bare `title` AND `title + notes` (the combined form is kept, and skipped when
  it equals the bare title, so a step folded into another step's notes still
  carries).
- Regression pinned as `tests/test_plan_carryover.py` §4b — a step whose title
  is unchanged but whose `notes` field is 321 characters must auto-carry, and
  the notes must survive the replacement. Suite is now 14 checks.

---

## [2.4.0] — 2026-07-28

Plan-integrity release. `plan_active` is a SINGLE-row slot, so every
`plan-set --from-refiner` replaced the current plan wholesale — unfinished
steps vanished with no accounting that they ever existed. v2.4.0 closes that
hole with a mandatory carryover gate at the one replacement door, a matching
gate on `plan-clear`, and an append-only archive of every outgoing plan. There
is deliberately **no force flag**: a drop with no recorded reason is exactly
the failure mode the gate exists to kill.

### Added

- **Mandatory carryover gate on plan replacement** (`core.plan.check_carryover`).
  Every step of the outgoing plan whose status is `pending` / `in_progress` /
  `blocked` must be accounted for in the incoming JSON, either (a) **auto-carried**
  — some step in the new plan matches its title with trigram-Jaccard ≥ 0.5
  (`CARRYOVER_MATCH_THRESHOLD`) — or (b) **explicitly dispositioned** via a new
  top-level `"dispositions": [{"old_title": …, "action":
  "done|dropped|merged|carried", "reason": …}]` array. A disposition with an
  unknown `action`, or with an empty `reason`, is itself a violation.
- **Enforcement at the only replacement door.** `core.plan.apply_refined_plan`
  runs the gate before it writes and raises `ValueError` listing every
  unaccounted step by id and title; `cli/mem.py` surfaces it as
  `[FAIL] refined plan rejected: …` and exits 1, leaving the old plan intact.
  The gate reads dispositions from the **raw** refiner dict (normalisation runs
  on a copy), so the schema stays additive for older refiner outputs.
- **Append-only plan archive** (`core.plan.archive_plan`). Every outgoing plan —
  replaced or cleared, cleanly dispositioned or not — is written to
  `memory/.plan_history/plan_<timestamp>_<replace|clear>.json` with the
  archived-at time, the event, the reason, and the full `structured` / `raw` /
  `active_step` payload. An archive-write `OSError` warns and proceeds rather
  than blocking planning — the dispositions, not the archive, are the primary
  anti-loss guarantee.
- **Dispositions are retained for audit.** `normalize_structured` keeps the
  `dispositions` array in the stored plan, so `plan_active.structured` records
  what happened to the previous plan's unfinished steps and why.
- **`agents/plan-refiner.md` rule 8.** The refiner must read the current plan
  (`plan-show`, or `memory/PLAN.md`) before emitting JSON, and either carry each
  unfinished step into `steps` or disposition it. If the raw document does not
  say what happened to a step, it must be marked `carried` and re-added —
  never an invented `done` / `dropped`.
- **`tests/test_plan_carryover.py`** — new suite, 13 checks over six sections:
  bootstrap replacement with no old plan, refusal that names BOTH lost steps,
  old plan untouched after a refusal, auto-carry by similar title, explicit
  dispositions stored for audit, reasonless disposition refused, and the CLI
  `plan-clear` gate end-to-end including archive contents.

### Changed

- **`/cc-mem plan-clear` now refuses to sink work.** With unfinished steps in
  the active plan it prints the gate message plus every pending step and exits
  1 unless a new `--reason "<why these steps are being dropped>"` is supplied;
  the reason is recorded in the archive. The plan is archived before clearing
  in either case, and the success line is now
  `[OK] Active plan cleared (archived to memory/.plan_history/).`

No schema migration: `dispositions` rides inside the existing
`plan_active.structured` JSON blob, and the archive is a plain directory under
`memory/`. The raw-capture path (`ExitPlanMode` → `plan_active.raw`,
`plan-set --raw`) is unaffected — it only arms `needs_refine`, it never
replaces the structured plan, so the gate stays at the single door that can
actually lose steps.

---

## [2.3.4] — 2026-07-14

Auth + local-fallback behavior release. Root-caused why every LLM call was
landing on the local Ollama model (GPU spikes during gaming) and why compaction
extraction kept failing while a healthy Claude subscription sat unused.

### Fixed

- **OAuth token no longer blackholed behind a dead env key.** `core.auth` now
  exposes `get_api_candidates()` — ANTHROPIC_API_KEY env var first, then the
  Claude Code OAuth token — and `llm.ccl_backend.call_llm` FALLS THROUGH to the
  next candidate on any failure. Pre-2.3.4, a zero-credit env key (HTTP 400)
  consumed the only Anthropic attempt and pushed every call onto Ollama.
- **OAuth tokens sent with the correct wire format.** `sk-ant-oat…` subscription
  tokens are sent as `Authorization: Bearer` + `anthropic-beta: oauth-2025-04-20`
  (verified live: the same token via `x-api-key` is HTTP 401; via Bearer it is
  HTTP 200). Platform `sk-ant-api…` keys keep `x-api-key`.
- **BudgetGate cost model updated**: `_worst_call_cost` reserves 2 Anthropic
  legs + the fallback leg, so the deadline guarantee holds with fall-through.

### Changed

- **Local Ollama fallback is now OPT-IN** (`config.json` `ccl.enabled: false`
  default). With OAuth fall-through the Anthropic leg is reliable; cold-loading
  a local model per consolidation batch cost more (GPU spikes, timeouts → "Hook
  cancelled") than the nicety was worth. Set `ccl.enabled: true` to restore.
- Version bump `2.3.3 → 2.3.4` across the usual six files.

---

## [2.3.3] — 2026-07-11

Documentation + version-metadata release. **No runtime behavior changed** — the
memory engine, hooks, schema, and extraction logic are byte-for-byte unchanged;
only the docs, the new multilingual version-control system, and the version
strings move. Bumps `2.3.2 → 2.3.3` across `cc_memory/__init__.py`,
`config.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
`pyproject.toml`, and the MCP `serverInfo`.

### Docs / Tooling

- **Documentation multilingual version-control (English skeleton + `*.zh.md`).**
  Established a three-tier language model: English is the canonical skeleton for
  docs and all LLM-facing strings (Tier 1); Chinese `NAME.zh.md` siblings are
  drift-tracked translations produced on demand (Tier 2); stored memory content
  stays any-language via the existing bilingual detectors (Tier 3). Full spec in
  the new `docs/I18N.md`.
- **Drift marker + checker.** Each translation carries a first-line HTML-comment
  marker recording a normalized-sha256 of its English source
  (`<!-- i18n-source: … | sha256: … | version: … | translated: … -->`). Drift is
  decided solely by that hash. New pure-stdlib `tools/i18n_check.py` classifies
  every tracked doc (IN-SYNC / MISSING-TRANSLATION / STALE / ORPHAN / NO-MARKER),
  emits markers (`--emit-marker`), and lists recorded-vs-current hashes
  (`--list`). A shared normalizer (strip BOM → LF → per-line rstrip → single
  trailing newline) makes the digest stable across CRLF/LF and Windows/Unix. The
  tool is dev/CI-only and deliberately excluded from `SUBPACKAGE_FILES`,
  `build_exe.py`, and the layout inspector, so the packaged plugin is unchanged.
- **`README.zh.md`** added as the reference translation, tied to the corrected
  `README.md` via the marker.
- **`README.md` brought current to v2.3.3.** Refreshed tagline and subtitle
  label, "What's new in v2.3.3 / v2.3 / 2.3.1 / 2.3.2" sections, the two-leg
  `PreCompact` (sync `pre_compact.py` + async `consolidate_async.py`, 300s)
  architecture diagram, the `inject-show` / `inject-usage` / `encoding-check`
  CLI surface, and `docs/PLAN_PROTOCOL.md` + `docs/I18N.md` in the docs list.
  Because the version label lives in a hashed i18n source, `README.zh.md` was
  re-translated and its marker re-emitted so the drift gate stays green.
- **Smoke-test drift gate.** `tests/smoke_test.py` now imports `i18n_check` and
  fails on any STALE/ORPHAN/NO-MARKER, and asserts `README.zh.md`'s marker hash
  equals the current `README.md` hash — so a stale translation turns the suite red.
- **Tier-3 durability notes.** Added a "Bilingual by design" subsection to
  `docs/ARCHITECTURE.md` and behavior-neutral `i18n Tier 3` comments at the
  any-language detection sites (`core/extractor.py`, `hooks/user_prompt.py`,
  `hooks/session_start.py`) so a future refactor won't reduce them to English-only.

---

## [2.3.2] — 2026-07-10

Patch release. **Permanently** fixes the intermittent `Compacted PreCompact
[...] failed: Hook cancelled` that still occurred on large memory DBs after
v2.3.1's timeout raise. Raising a timeout only moves the goalpost; v2.3.2
removes the failure mode by taking the variable-latency LLM work off the
blocking compaction path entirely.

### Fixed

- **Consolidation moved to a sibling `async` PreCompact hook.** `PreCompact`
  now declares two command hooks in `hooks/hooks.json`: the sync leg
  (`hooks/pre_compact.py`, timeout 120s) does only fast extraction +
  PROGRESS.md (~1-5s), and a new background leg (`hooks/consolidate_async.py`,
  `"async": true`, timeout 300s) runs the every-Nth-session consolidation.
  Claude Code starts the async hook and continues compaction without waiting,
  so a slow consolidation can no longer surface as a compaction failure no
  matter how large the DB grows. The exe-installer path (`ui/installer.py`)
  emits the same two-hook shape (async flag, flat 300s) and ships the new file.
- **Root cause of the residual overrun: one ungated LLM stage + a dishonest
  budget cost model.** `consolidate_topics` (`core/consolidate.py`) looped an
  LLM summary per topic with NO budget gate, and every "gated" stage under-
  counted a call's cost as a flat 20s while a real `call_llm` could run
  `haiku_timeout + min(3×timeout, 120)` ≈ 120s (Haiku hang → Ollama fallback).
  A call the gate "allowed" near the budget edge therefore overran. Fixes:
  `consolidate_topics` is now budget-gated (falls back to the no-LLM summary
  when exhausted); `call_llm` takes a bounded `fallback_timeout`; and each
  stage reserves the TRUE worst-case call cost (`_worst_call_cost`). The gate
  now GUARANTEES a run finishes by `total_s − safety_s` (232s) < the 300s async
  timeout, so the worker is never killed mid-write.
- **Consolidation cadence hardened.** Replaced the `session_count % N` trigger
  (racy against the concurrent sync hook) with an interval marker
  (`memory/.last_consolidation.json`) + a lock file (`.consolidation.lock`,
  stale-reclaimed). Race-immune and single-owner; concurrent DB access with the
  sync leg is safe on the existing WAL + `busy_timeout=5000` connection.

Marketplace / git-checkout users pick up the two-hook PreCompact on their next
Claude Code session (hooks.json is read at session start); exe-install users get
it after reinstalling with the v2.3.2 installer.

---

## [2.3.1] — 2026-07-09

Patch release. Fixes the frequent `Compacted PreCompact [...] failed: Hook
cancelled` message during compaction.

### Fixed

- **PreCompact hook timeout raised 45s → 120s** (`hooks/hooks.json`; the
  exe-installer path in `ui/installer.py` bumped in lockstep, base 30 → 80 ×
  the 1.5 Windows multiplier = 120s, matching the marketplace manifest). The
  hook does synchronous network LLM work — up to ~25s Haiku extraction, worst-
  case ~100s if Haiku fails and falls back to local Ollama, plus a heavier
  consolidation pass every 5th session — and the old 45s ceiling was too tight,
  so the hook was killed mid-write.
- **Root cause: the consolidation `BudgetGate` sub-budget equalled the hook's
  hard timeout.** The gate can only refuse to START a new LLM call, never
  interrupt one already in flight, so a call it allowed at the budget edge
  always overran the ceiling. The 120s ceiling now sits comfortably above the
  45s consolidation sub-budget + worst-case in-flight call (~80s), so
  consolidation can no longer trigger a kill. Documented at the gate site;
  de-hardcoded the stale "45s" references in `core/consolidate.py`.

Marketplace / git-checkout users pick up the new timeout on their next Claude
Code session (hooks.json is read at session start); exe-install users get it
after reinstalling with the v2.3.1 installer.

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
  `docs/PLAN_PROTOCOL.md`.
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
  `docs/MEMORY_RULES.md`.
- **`memories.supersedes_id`** column + `db.get_supersede_chain(id)` — preserves
  update history. Walk a chain via `mem.py supersedes <id>`.
- **`progress` SQL table** + **`memory/PROGRESS.md`** — replaces v2.0
  `SESSION_HANDOFF.md`. Always full-rewritten from the SQL row, never appended.
  See `docs/HANDOFF_PROTOCOL.md`.
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
