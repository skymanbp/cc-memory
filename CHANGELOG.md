# Changelog

All notable changes to cc-memory are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
  (`user_prompt.py:195`) `` — went unchecked: 370 of 594, 62 %. It now anchors
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
