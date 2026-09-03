# CLAUDE.md — Project Instructions for Claude Code

## Project: cc-memory

**Claude Code persistent memory plugin (v2.14.0)** — anti-patch reconcile-on-write
+ LLM-judged semantic de-duplication with **backpressure-triggered
consolidation**, forced PROGRESS.md handoff with per-session annotation, live
PLAN.md anchor with plan-refiner / plan-guardian subagents + mandatory
carryover gate, **an enforced directive ledger**, bounded transcript reads,
injection observability, FTS5 search, AI-judged extraction with Haiku
(optional local Ollama fallback).

- **Language**: Python 3.8+ (pure stdlib, zero pip dependencies at runtime)
- **Version**: 2.14.0
- **License**: MIT
- **Platform**: Windows-primary, cross-platform compatible (Tkinter required for GUI)

## What changed in v2.14.0 (over v2.13.2)

**A project's identity is its database, not the path string inside it.** A
whole-repository debug pass (six read-only reviewers over disjoint file sets,
every finding reproduced before it was reported) produced 38 findings; eight
shared one upstream cause — `projects.path` WAS the identity, and every surface
decided "which project is this" with its own path arithmetic — and all eight
are closed here, plus four of the pass's other named findings, each with its
own cause (rules 5-8), plus the remaining twenty-seven, each at its own cause
(rules 10-20; `CHANGELOG.md` § *The rest of the pass* has the measurements).
The report is in the tree as `docs/debug-pass-2026-09.md` — an evidence
record: never edited, excluded from the citation and symbol gates. Full
narrative in `CHANGELOG.md`; the specification is `docs/ARCHITECTURE.md` §7.
Twenty rules a future change must not break:

1. **`core.layout.canonical_path` is THE comparable spelling of a path.**
   Resolved, then `normcase`d, never raises, and a non-path spells as `""` so
   it can only ever MISS. Every "are these one directory" decision goes through
   it or `same_path`: `MemoryDB.upsert_project`, the consolidation marker's
   fallback compare, `roots._home_dirs` (which now carries every spelling
   RESOLVED too — `project_root` walks a resolved chain, and an unresolved
   boundary let it through a symlinked home), the dashboard's `_registry_key`,
   and `modes._norm_path`, which delegates. Do not compare paths with a fresh
   `normcase`, `.lower()` or `str ==` anywhere a directory's identity is at
   stake; that is how a renamed project minted a second row while its
   memories sat one `project_id` away.

2. **`upsert_project` re-attaches; `find_project_id` never inserts.** A miss on
   exact and canonical match RE-ATTACHES a row only when the database sits at
   `<cwd>/.ccm/memory.db` (`layout.database_owner` — the file's location is the
   declaration of identity ARCHITECTURE §7 already stated for the resolver),
   and only the most recently active row whose directory no longer exists. A
   row whose directory still exists elsewhere is another live directory's and
   is never taken — not even as the only row in the file (a first draft took
   it, and `tests/test_surfaces.py` §9a caught it taking a sibling's row); a
   database that is not the caller's own never re-attaches anything (a sibling
   row sharing one file keeps its identity — the shape §9a seeds). The surfaces that ask a question
   (`status`, `stats`, `list`, `sessions`, `keywords`) use `find_project_id`:
   a question never creates a row — `status` used to, and reported the empty
   row it had just made. Gate: `smoke_test.py` § *identity*; `falsify --case
   r13reattach` / `r13statuscreate`.

3. **The consolidation marker follows the ROW, by `project_id`.** The path
   check it grew in v2.12.0 existed only because a rename minted a second row;
   it stays as the fallback for unstamped markers and compares canonical on
   BOTH sides, and `project_path` is stored resolved — the CLI's documented
   `--project .` used to store `"."`, so every manual `/cc-mem consolidate`
   read as foreign and the Stop probe kicked the redundant run the shared
   writer was added to prevent. Gate: `falsify --case r13markerid` /
   `r13markerpath` / `r13markersame`.

4. **Both skills ask `core.layout` where the state directory is.**
   `skills/save-memories/SKILL.md` still joined `memory/` by hand after the
   v2.13.0 rename and wrote every memory into a database nothing read; the
   v2.13.0 sweep had registered TWO deliberate literal copies and this was a
   third inline script it never listed. `ccm-load`'s registered literal is the
   ONE permitted spelling in `skills/`, the smoke gate scans both files for a
   hand-spelled join, and the save-memories body is RUN against an initialised
   project. Gate: `falsify --case r13skilldir`.

5. **A handle follows the migration, one way.** `MemoryDB` keeps the `db_path`
   it was constructed with, and the dashboard, the web viewer and the MCP
   server keep one instance per process; on Windows `migrate_legacy_dir`'s
   rename is refused while such a handle is open and completed by another
   surface later, after which every operation on the stale handle raised
   "unable to open database file". `_connect` retries ONCE through
   `MemoryDB._follow_state_dir`: from `memory/` to `.ccm/` only, only when the
   new file exists and passes the constructor's link refusal, and only after a
   connect actually failed — the settled case pays nothing. Never the reverse:
   nothing but the migration may join the legacy name (`core/layout.py`).
   Gate: `smoke_test.py` § *identity* (j); `falsify --case r13handlefollow`.

6. **Span tags match case-insensitively, through `privacy._token_re`.**
   `_MARKER_TAG_RE` had ignored case since v2.5.2 and the span scanner had
   not, so `<PRIVATE>…</PRIVATE>` was neither stripped on the write path nor
   escaped on the render path — measured, the secret left `clean_for_storage`
   verbatim. `has_private` (the `is_private` classifier) uses the same regex.
   Do not test for a tag with `in` or `str.find`. Gate: `falsify --case
   r13privatecase`; `r6quadratic`'s anchor now sits on the regex loop and was
   re-driven RED after the move.

7. **The escape budget is per EPISODE.** `_block_attempt` counts consecutive
   refusals of one condition set and nothing ended a streak, so a condition
   resolved after two refusals resumed at 3 when it next arose (`plan-drift`
   returns every 8 turns by design), and after three resolved refusals a
   session was advisory-only for the rest of its life — the v2.11.0
   measurement waiting to recur. `hooks/stop._block_reset` clears the marker
   on every Stop that may close, live plan or not. Gate:
   `test_directive_enforcement.py` §5(a) and §8 (the real hook: refuse,
   refuse, resolve, and the next episode opens at attempt 1); `falsify --case
   r13budgetreset`.

8. **Never call `Path.home()` bare on a hook path.** `core/auth._credentials_path`
   returns None when no home resolves (`RuntimeError`, measured on Windows
   with `USERPROFILE`/`HOMEPATH`/`HOMEDRIVE`/`HOME` unset), so an explicit
   `ANTHROPIC_API_KEY` is returned instead of discarded with the exception —
   the failure class `core/markers.marker_dir`'s docstring records for
   `core/logger.py`'s module-scope `Path.home()`, one frame deeper. Gate:
   `smoke_test.py` § *v2.14.0 auth*; `falsify --case r13authhome`.

9. **A gate's condition must be SUFFICIENT for the sentence it certifies.**
   Four checkers passed on states they exist to refuse, each measured on the
   v2.13.2 tree before the tightening: `doc_coverage` counted a substring as
   documentation (the `<!-- i18n-source: … -->` marker satisfied a column
   called `source`) and enumerated MCP tools by a name prefix (a tool
   outside `memory_` / `progress_` was required 0 times) — membership is now
   NAMING (`_names`: a code span or a quoted JSON key), the `TOOLS` registry
   is read whole, and `CREATE VIRTUAL TABLE` counts; `doc_claims` let a
   count with two modifier words through ("nine shipped plugin hooks" was
   not a claim) — the gap is one or two words now, and the first sweep
   found one unbound count in `ui/installer.py`; `citation_check` printed a
   bounds-only citation as `ok` — the summary now says "NOT verified against
   a symbol", and six such citations had rotted; `i18n_check --emit-marker`
   certified a translation nobody translated (README.md edited, marker
   re-pasted, README.zh.md untouched: `IN-SYNC`) — the marker records the
   translation body's hash and the emitter refuses an untranslated re-stamp
   (`--translation-unchanged "<why>"` for an English-only change). Recorded,
   not redesigned: a bounds-only citation still cannot rot LOUDLY, and a
   count whose noun is not in the trigger list is still not a claim. Gates:
   `smoke_test.py` § *v2.14.0 gate checkers*; `falsify --case
   r13i18nrestamp` / `r13coveragename` / `r13coverageenum` /
   `r13coveragetools` / `r13claimsgap`.

10. **Identification is TRI-STATE, and only a POSITIVE licenses the
    irreversible move.** `core.layout.UNKNOWN` (falsy) is what every probe
    returns when it could not RUN — `sqlite3.OperationalError`, an unreadable
    marker file, an unprobeable link; `DatabaseError` ("file is not a
    database") stays a real negative. A write guard keeps failing closed
    (`if is_ccm_dir(d):` still means "positively ours"); `migrate_legacy_dir`
    alone asks `is UNKNOWN` and takes the refused-rename branch, its settled
    case requires `.ccm/memory.db` to hold bytes, and an empty `.ccm/` beside
    a positively-ours `memory/` returns `memory/`. Measured before: one second
    of `BEGIN EXCLUSIVE` on a 25-row legacy database orphaned it permanently.
    A linked `.ccm` is not a state directory at all (`_is_usable_state_dir`,
    through `core.markers._is_link`, junctions included): never followed,
    never renamed onto, on the read side too — and a recovery path that
    re-derives the location (`pre_compact.main`'s last-resort handler did,
    and wrote through the link) re-applies the same probe before it writes.
    Gate: `smoke_test.py` § roots (c)(d); `test_surfaces.py` §7 (linked state
    dir, fails rather than skips); `falsify --case r14probe3` / `r14emptyccm`
    / `r14linkdir` / `r14findlink` / `r14linkrecover`.

11. **In `core/roots.py` a declaration beats a name, and a volume root is a
    boundary in every spelling.** `.ccm-root` is consulted ONCE, in
    `_candidates`, and short-circuits every rule — it had been bolted onto two
    rules separately and the dependency-name rule never got it, so a project
    called `external` resolved every subdirectory to itself, pinned or not.
    `_dependency_cut` also spares a directory that owns a database, and the
    verdict is on record: the database wins at every depth
    (`node_modules/left-pad` with its own `.ccm/memory.db` resolves to itself,
    as rung 0 already said for the directory itself). `_is_volume_root`
    recognises `/mnt/c`, `/cygdrive/c`, `/host_mnt/c` and `/c` beside `C:\`
    and `/`, so a Windows profile reached from WSL is a profile and its home
    database is not adopted. `_is_container` has exactly one caller. Gate:
    `smoke_test.py` § roots (a)(b); `falsify --case r14a1b` / `r14depcut` /
    `r14depdb`.

12. **Fill-only-empty is decided INSIDE the write, and EMPTY is not NEVER
    WRITTEN.** `MemoryDB.fill_empty_progress` tests emptiness in the UPDATE
    itself (`CASE WHEN COALESCE(col, '') IN ('', '[]')`, `BEGIN IMMEDIATE`);
    `_refresh_progress_row` no longer reads a verdict on one connection and
    writes it on another with a transcript load in between — a PreCompact
    rewrite committing in that window was overwritten by heuristics. Where
    the row's `trigger_type` says a full rewrite settled it
    (`progress_was_fully_written`; the PATCH-only writers are the enumerated
    set, so a new host trigger string still counts as a rewrite), the mined
    work lists stay as written, empty included; on `source="compact"` /
    `"resume"` tier 3 mines the CURRENT transcript (`tier3_exclusion`),
    because excluding it handed the mine to another session's todos. Known
    limit: the Stop hook's per-turn patch stamps `"stop"`, so the settled fact
    protects the compact/resume start that follows a rewrite and no longer.
    Gate: `smoke_test.py`; `falsify --case r14fillrace` / `r14emptytodos` /
    `r14curtranscript`.

13. **An empty extraction is a RESULT, and no transcript is read before the
    credential is resolved.** `_retroactive_extract` returns `[]` when the
    model found nothing worth keeping and `None` only when it did not run;
    `retroactive_save` records the session either way (a transcript that
    yielded nothing was re-decoded and re-sent at every SessionStart, forever)
    and resolves `core.auth.get_api_key()` above the loop (2.98 s per start
    with no key, 0.25 s after). Gate: `smoke_test.py`; `falsify --case
    r14retroempty` / `r14retrokey`.

14. **Every line of stdout Claude reads is a render path, and a one-line
    slot is an INLINE slot.** The Stop advisory printed when the escape
    budget is spent joined refusal keys raw — a key carries a directive slug,
    which `upsert_directive` never cleans, and a stored `</system-reminder>`
    reached the model live. `neutralize_inline` on the advisory and on every
    `[key]` / `what` / `fix` slot of `render_block_reason`
    (`neutralize_document` escapes tags but leaves newlines, and a CR/LF in a
    slug forged extra entries). Gate: `test_directive_enforcement.py`
    §9(a)(b); `falsify --case r14advisoryslug` / `r14blockinline`.

15. **The consolidation lock has ONE policy point:
    `consolidate_async._acquire_lock`.** The Stop probe imports its
    `_STALE_LOCK_S` and compares the lock's age; it used to refuse on
    `.exists()` alone — a copy of the policy minus its staleness rule — so a
    lock left by a killed worker vetoed the only process that reclaims one,
    forever (a 2-hour-old lock held the kick at False over three Stops). A
    young lock still defers the spawn; `stop.py` holds no lock constant of
    its own. Gate: `test_directive_enforcement.py` §9(c); `falsify --case
    r14stalelock`.

16. **`user_prompt.strip_scaffolding` is THE scaffolding predicate for both
    `current_request` ingresses, and the seed happens once per session.**
    `/ccm-load` used to become PROGRESS.md §1 (`ccm-load`) and the observer's
    "User request:"; the live hook and `pre_compact._first_user_request` ask
    the one function now (the wrapped `<command-name>` form and the bare
    `/command` form; a request that opens with a path keeps its slash). The
    seed fires on the first NON-scaffolding prompt, recorded by a
    `cc_mem_seeded_` marker registered in `ui/installer.py:
    _TEMP_MARKER_PREFIXES` — NOT by the prompt marker being empty, which a
    scaffolding or an entirely-private turn also leaves empty (a first draft
    keyed on that and re-seeded `real → /cc-mem status → 继续` as a
    mid-session `resume_request`). Gate: `test_surfaces.py` §7
    `_user_prompt_seeds_the_first_real_request`; `falsify --case
    r14slashseed` / `r14seedturn1` / `r14seedprev`.

17. **An exact-hash restatement REINFORCES; the tag cap never eats the
    writer's marker.** `memory_writer._fold_into_hash_match` folds importance
    (max) and tags (union) into the hash-matched row and nothing else, action
    `reinforced`, no write when nothing is new (`skipped` still means nothing
    was written); the fold runs after `reconcile_upsert` commits, so two
    simultaneous identical saves can miss a bump — never a row. `_merged_tags`
    caps the caller-supplied union and re-appends `_ACTION_TAGS`, so a stored
    list can hold `MAX_TAGS + 2`. Every consumer of `upsert_batch`'s counts
    renders `reinforced`. Gate: `smoke_test.py` § C3/C4; `falsify --case
    r14hashfold` / `r14foldnoop` / `r14tagcap`.

18. **The CLI boundary catches the CLASS external input can raise, and keys
    a remedy on the MEASURED message.** `(OSError, sqlite3.Error,
    UnicodeError, OverflowError, JSONDecodeError)`, never a bare
    `ValueError`; ids and counts are bounded at argparse (`_row_id` /
    `_plan_int`); stdin loses its BOM and `--raw-file` is `utf-8-sig`. A
    remedy is printed only for the four sqlite messages driven first-party
    (`_SQLITE_ENV_FAULTS`); any other `sqlite3.Error` is re-raised — a first
    draft told `no such column` (our bug) to "check that memory.db is a
    writable FILE". `plan-set --from-refiner` judges the plan it WROTE
    (`result`, normalised), not the raw payload, and `goal` / `context` are
    TEXT by one rule in `normalize_structured`. Gate: `test_surfaces.py`
    §9h-j; `falsify --case r14cliboundary` / `r14sqlremedy` / `r14rowid` /
    `r14stdinbom` / `r14criteriacore` / `r14criteriaraw` / `r14goalrepr`.

19. **The frozen installer never runs a `.py` through `sys.executable`; a
    settings write follows the link; a value is rendered at its sink.** In a
    onefile exe `sys.executable` is the installer, so "Open Dashboard"
    re-entered `main()` and was refused with exit 2 — `_python_for_script`
    hands scripts to the interpreter the hooks use, and `test_surfaces.py` §3
    asserts nothing else reads `sys.executable`. `_settings_write_target`
    writes THROUGH a symlinked `settings.json` (a dotfiles-managed home lost
    its hooks on every sync). The SQL console renders rows on both branches
    (`_format_sql_result`, pure); Save Session stores `archive_path=""`
    because nothing writes that file; `_manifest_slot` bounds, flattens and
    escapes every manifest or filesystem value the CLAUDE.md generator
    interpolates (a `description` grew a `## Rules` section; a list-valued
    `name` raised out of a Tk callback). Gate: `test_surfaces.py` §3 / §8;
    `falsify --case r14frozendash` / `r14settingslink` /
    `r14installerprose` / `r14sqlrows` / `r14archivestamp` / `r14pkgdesc` /
    `r14pkgname`.

20. **`marker_dir()` returns None rather than a directory inside the user's
    tree, and a gate copy is a git repository.** `tempfile.gettempdir()`
    falls back to `os.getcwd()` — the project, under a hook — so markers were
    written into the repository; a base that IS the cwd or is not a
    designated temp root is refused, `marker_path` propagates None and both
    leaves refuse it (every call site flows only into `read_marker` /
    `write_marker`; the installer's sweep tests for None). `_is_cwd` is
    equality, not containment, by design: a project opened at `~` or a drive
    root keeps its markers. `tools/falsify_fixes.py` runs each gate once on
    an UNTOUCHED copy (`gate_baseline`) and reports UNSOUND instead of RED
    when that baseline is red — the copy lacked `.git`, `git check-ignore`
    exited 128, and every smoke-gated case had been unsound until v2.14.0;
    `tools/citation_check.py` walks only this tree's directories
    (`_tree_files`) and verifies a `verbatim` region IN ORDER. Gate:
    `smoke_test.py` § markers / § citations; `falsify --case r14markercwd` /
    `r14markernone` / `r14baseline` / `r14verbatimorder` / `r14dotdirs`.

## What changed in v2.13.2 (over v2.13.1)

**A rename is not finished when the joins are.** v2.13.0 swept path joins and
tracked markdown; 95 lines of prose inside `.py` files still spelled
`memory/<something>`, and one of them was not prose:
`llm/memory_writer._render_memory_index` built `MEMORY.md`'s archive links from
a hard-coded `"memory/"` instead of the directory it was handed, so every
generated index pointed at a path that had stopped existing. Three rules:

1. **A rename sweep must cover strings the user or the model READS, not only
   paths the code JOINS.** Nine such lines survived v2.13.0: two argparse
   `help=` strings, four `print()`s, and three that `core/plan.py` renders into
   `PLAN.md` — the live plan anchor, telling Claude to open
   `memory/.plan_raw.md`. Grep for the old name in `help=`, `print(`, and any
   list of strings a renderer joins, not just for `/ "name"`.

2. **A test that spells the same literal as the code cannot catch the code.**
   `smoke_test.py` § *v2.8.0 a6* filtered on `"- \`memory/sessions/"` and so
   passed on the wrong output for a whole release. Assertions about a rendered
   path take the name from the fixture (`_MEM` / `_OLDMEM`), never from a
   literal — and the legacy case gets its own render, because a renderer must
   name the directory it was given.

3. **Do not rewrite a file with `splitlines()` + `"\n".join()`.** That splits
   on CR, VT, FF, FS, GS, RS, NEL, U+2028 and U+2029 as well as newlines, and
   `core/privacy.py`, `tools/falsify_fixes.py` and `tests/smoke_test.py` carry
   those characters INSIDE string literals — measured, the round-trip broke a
   regex literal across a line and stopped `core/privacy.py` compiling. Also
   pass `newline=""` when writing: `Path.write_text` otherwise translates every
   `\n` to `\r\n` on Windows, and `Path.read_text` translates it back, so the
   damage is invisible to a read-back check.

31 of the 95 lines were deliberately left saying `memory/`: dated
measurements, pre-v2.13.0 narratives, and sentences whose SUBJECT is the legacy
name — the same rule CHANGELOG.md states for its own entries.

## What changed in v2.13.1 (over v2.13.0)

**Nothing that runs.** `git diff v2.13.0..v2.13.1 -- cc_memory/ scripts/
.claude-plugin/` is empty apart from the version literals. Two things were
wrong ABOUT v2.13.0 rather than IN it, and one of them is a rule worth
carrying forward:

- **`tools/falsify_fixes.py --anchors` is a CI step, not a local gate.** It
  runs in `.github/workflows/gates.yml`; `tests/run_gates.py` does not run it.
  So `[OK] all 11 gates green` locally is NOT the same evidence CI produces,
  and v2.13.0 was tagged on that assumption — its `_has_db` rewrite had
  invalidated the `r5y1roots` anchor, and only the post-tag CI run said so.
  Before tagging, run `python tools/falsify_fixes.py --anchors` as well; when
  an anchor is repaired, re-verify it still DETECTS (`--case <id>`), because
  an anchor edited until it merely matches proves nothing.

- **A path substitution inside fixed-width ASCII art must re-pad.** `.ccm/`
  is two columns narrower than `memory/`, which pulled two rows of the README
  hook diagram off the right border in each language. The boxes are now
  normalised whole (English 77 columns, Chinese 76, counting CJK as two and
  the East-Asian *Ambiguous* arrows as one).

## What changed in v2.13.0 (over v2.12.2)

**The state directory is `.ccm/`, and a name that lived at 34 call sites now
lives at one.** Per-project state moved from `memory/` — an undotted, generic
name that sat beside the user's own code and collided with any project that
already had a package called `memory` — to `.ccm/`, dotted state beside `.git`
and `.venv`, matching the `.ccm-root` pin this plugin already owned. Measured
at v2.12.2: the literal `"memory"` was joined onto a path at **34 lines across
15 modules** (both CLIs, the MCP server, the dashboard, the web viewer, the
installer, the consolidation worker and all six hooks <!--ce:hooks-->), plus
167 fixture sites in `tests/` and `demo/`. Six rules a future change must not
break:

1. **The name is `core/layout.MEMORY_DIRNAME`, and nothing else spells it.**
   `core/layout.py` is the new module: names, identification, migration. Every
   surface asks `memory_dir(root)` (write side) or `find_memory_dir(root)`
   (read side) instead of joining. TWO literal copies survive, for the same
   reason the `.gitignore` line list has two — `ui/installer.py` is a
   stdlib-only bootstrap and `skills/ccm-load/SKILL.md` is an inline script,
   and neither can rely on importing the package. Gate: `smoke_test.py`
   § *v2.13.0 state directory* asserts both literals against the constant AND
   greps `cc_memory/**.py` for the join returning. A bootstrap that creates
   the wrong directory initialises a project the hooks then cannot find.

2. **A RENAME is not the MERGE `core/roots.py` refuses.** That module's
   PREVENTION, NOT MIGRATION rule is about ADOPTING a stray database: two
   `memory.db` files are byte-for-byte indistinguishable from a deliberate
   nested sub-project, so choosing one destroys data. Renaming one directory
   merges nothing, chooses nothing, and leaves the contents untouched — the
   generated `.gitignore` needed no edit at all, because every line in it
   names an entry INSIDE the directory. That asymmetry is the whole licence
   for doing this one automatically.

3. **A refused move returns the LEGACY directory, never the new name.**
   Measured on the primary platform: Windows refuses to rename a directory
   while a handle inside it is open, so a second session or the dashboard
   holding `memory.db` blocks the move. Handing back `.ccm/` there would have
   the caller create a fresh empty one beside a `memory/` holding everything
   the user has, and the project would come up looking brand new. The retry
   costs one stat per turn and converges as soon as the handle closes.

4. **Identification, never name-matching.** `memory` is a name real projects
   use for real content. `layout.is_ccm_dir` migrates only a directory
   carrying THIS plugin's `.gitignore` marker line or a `memory.db` that is a
   real SQLite file with this schema's tables — and the magic-byte pre-filter
   is load-bearing, because `sqlite3.connect` on a non-database CREATES one,
   which would be a probe manufacturing its own evidence. (Measured in
   v2.14.0 while registering falsification cases: the pre-filter is
   defence-in-depth, not the only guard — `_safe_is_file` short-circuits an
   absent file and the `mode=ro` URI refuses to create one, so a case that
   removed the pre-filter ran GREEN and was not kept. Keep the pre-filter;
   do not cite it as the thing that prevents the create.)

5. **The read side never migrates.** `find_memory_dir` / `find_db_path` exist
   because migration is a WRITE: `ui/dashboard.py` enumerates every sibling of
   a project to fill its picker and `cli/mem.py status` scans a whole projects
   folder. Routing those through the migrating resolver would rename the state
   directory of every project on the machine because the user opened a list.

6. **`roots._has_db` and `nested_databases` know BOTH names.** Resolution runs
   BEFORE anything asks for the state directory, so a project whose rename has
   not happened yet — or could not — must still resolve as a project root. If
   rung 0 and rung 1 knew only `.ccm`, the marker rung would answer instead:
   the stray-database shape that whole module exists to prevent, reintroduced
   by the rename. Same reason `.gitignore` needed the `!.ccm/` re-include
   under its `.*/` blanket: a blanket-ignored state directory is exactly the
   invisibility the anchored `/memory/` rule was written to stop, and the
   dotted name walked straight into it (verified with `git status`: the repo's
   own `.ccm/` is ignored, a stray one under a subdirectory is untracked).

`core/layout.memory_dir` also carries `_safe_path`, and it is there because
this module reproduced the exact defect `core/roots.py` documents from v2.6.0:
the handler that catches a non-path `project_root` re-raised the TypeError by
calling `Path()` on it again on the way out. Measured before the fix:
`memory_dir(123)` and `memory_dir([1, 2])` both escaped a function whose
docstring promises it never raises — and `{"cwd": 123}` is a real hook payload
shape (`test_surfaces.py` § 7 drives 48 of them).

**CHANGELOG.md is NOT swept to the new name.** Its entries are dated records,
and in v2.12.2 the directory really was `memory/`. Rewriting them would make
the file lie about the past to agree with the present. Docs that describe
CURRENT behaviour — README(.zh), `docs/`, this file, `skills/`, `commands/`,
`agents/` — are swept, because a path in an operating manual that does not
exist on disk is not history, it is a wrong instruction.

## What changed in v2.12.2 (over v2.12.1)

**A before/after demo, and the directive that never reached the model.**
README gained § *Before and after*: real `claude -p` sessions on the fixture
project `demo/tally/`, same model and prompt on both sides, every other
plugin switched off, captured by `demo/run_demo.py` into `demo/captures/`.
The guardian scenario seeded a `constraint` directive and measured it
reaching the session **zero times**: the docs said a constraint "is enforced
by being injected", and nothing injected it — `list_directives` had exactly
two callers, the CLI and the Stop hook's idle scan; `session_start.py` and
`core/progress.py` contained the word "directive" zero times. Four rules a
future change must not break:

1. **The ledger is the FIRST layer of the SessionStart injection and a
   section of PLAN.md.** `session_start._build_directives_layer` renders
   active rows constraints-first, then most-repeated-first, one neutralised
   line per row, skipping an over-budget row rather than the layer
   (`_LAYER_SKIP_NOTE`); `plan._render_directives_section` renders the same
   rows into PLAN.md — **also when there is no plan**, because the ledger
   outlives the plan and the guardian reads PLAN.md and nothing else of the
   plugin's. `_LAYER_BUDGETS["directives"] = 0.10`, taken from topics
   (0.30 → 0.25) and timeline (0.20 → 0.15); the shares still sum to 1.0. The
   inject manifest records `directive_slugs`, so `/cc-mem inject-show` can
   say which ones reached the model. Gate: `tests/test_directive_enforcement.py`
   §7; `falsify --case r12directiveinject` / `r12directiveplan`.

2. **The demo is evidence, not a mockup, and it stays reproducible.**
   `demo/run_demo.py` is the protocol as code: fixtures copied to a temp
   directory, plugins disabled per side through `--settings` (never
   `--bare`, which also drops CLAUDE.md discovery and OAuth), stream-json
   captured, transcripts rendered to `.txt` on purpose — every tracked
   markdown file runs through the citation/claims gates, and a transcript is
   quoted evidence, not a document. Re-runs differ; the committed captures
   are the ones the README text was written against, and a README quote
   must be copied from them, never paraphrased — and that is GATED, not
   promised: each quote sits in a `<!-- verbatim: <capture> -->` region that
   `tools/citation_check.py` verifies against the capture and never scans
   for citations, because its own `--fix` rewrote the quoted guardian
   report's `cli.py` line 12 into line 33 on its first run (§ Tests). The
   renderer prints a subagent's report in full and `--render-only` rebuilds
   every `.txt` from its stream. `demo/README.md` and `demo/tally/README.md`
   are in `tools/citation_check.py:TRACKED`.

3. **A documented mechanism needs a gate that measures the mechanism.**
   "Enforced by being injected" passed all eleven gates for two releases
   because no gate asked whether an injection *contains* a thing — the same
   class as v2.11.3's lesson from the other direction (there the design was
   undocumented; here the documentation had no design). §7 asks now.

4. **`_is_container`'s NEGATIVE verdict is bounded.** Proving "not a
   container" read every subdirectory of every ancestor, on every hook and
   every MCP call; `%TEMP%` on the reporting machine holds 6,366
   subdirectories, so one no-database MCP call cost 3.5-4.4 s and the stdio
   suite answered 5 of its 8 calls inside its window — red locally, green on
   CI's clean runners, since v2.6.0. `core.roots._CONTAINER_SCAN_CAP = 256`
   (0.27-0.32 s after); `tests/test_surfaces.py` §7 counts the probes rather
   than timing them; `falsify --case r12scancap`. A gate that only runs on
   clean machines measures clean machines.

## What changed in v2.12.1 (over v2.12.0)

**The release workflow's first run, and what the Linux lanes caught — twice.**
The first run of `.github/workflows/release.yml` on the v2.12.0 tag failed
at the exe-verification step; the Linux gate lanes caught a Windows-only
assumption in the smoke suite, and once that was fixed, a plugin bug that
had shipped since v2.8.0. v2.12.0's tag stays where it is — a moved tag is
a rewritten history. Three rules a future change must not break:

1. **A CI step that expects a NON-ZERO native exit must not run it through
   `&`.** GitHub prepends `$ErrorActionPreference = 'Stop'` to every pwsh
   step, PowerShell 7.4+ defaults `$PSNativeCommandUseErrorActionPreference`
   to `$true`, and the runner appends `exit $LASTEXITCODE` — any of which
   ends the step before an explicit `if ($LASTEXITCODE …)` can judge the
   code. The unknown-flag refusal probe uses `Start-Process -Wait -PassThru`
   and reads `.ExitCode`; the steps that judge exit codes switch the
   automatic throw off. Measured: the v2.12.0 run ended with exit 1 right
   after the refusal's usage text with NO error record, while the exe's true
   exit code is 2 (`Start-Process`, locally, after a green sandboxed
   `--cli` / `--uninstall` round trip).

2. **A platform-dependent expectation is asserted per platform, never
   skipped.** `os.path.normcase` folds case on Windows and is the identity
   on POSIX, so "a different-case path reads the same marker" is TRUE on
   Windows and FALSE on Linux — the smoke test now asserts each platform's
   correct answer. Both ubuntu lanes went RED on the v2.12.0 gates run; the
   Windows lane was green. That is the Linux lanes doing exactly what
   v2.11.2 added them for.

3. **`core.db._readonly_uri` is tested for all THREE path shapes on EVERY
   platform.** `readonly_connect` gave a POSIX absolute path the Windows
   drive-path prefix, producing `file://tmp/...` — SQLite reads `tmp` as a
   URI authority — so `/cc-mem sql` and the dashboard console had never
   worked on Linux or macOS (v2.8.0 through v2.12.0; the register-E2 note
   said "verified on the primary platform", which was the defect stated as
   a credential). The builder is a pure function precisely so the smoke
   suite can assert the POSIX, drive and UNC forms as literals everywhere;
   a shape tested only on the platform that has it is how this shipped for
   four minor versions. Gate: `falsify --case r12posixuri`.

## What changed in v2.12.0 (over v2.11.4)

**The field-report release: consolidation that actually runs, and a ledger you
can maintain.** Driven by two measurements — this repository's own database
(349 memories written in one month against a consolidation marker 17 days old,
SessionStart injecting topic summaries that still said "v2.5.4") and a
seven-finding field report from the Autoshop project (2026-08-25). Full
narrative in `CHANGELOG.md`. Invariants a future change must not break:

1. **Consolidation has a BACKPRESSURE trigger, and the marker has ONE
   writer.** The sessions-interval gate assumes compactions happen; a project
   worked in short sessions never compacts, so batch work (cross-topic
   dedup, topic summaries) starved while the per-row write path looked
   healthy. `core.consolidate.consolidation_backlog` measures the backlog
   against the marker's `last_memory_id` row-id watermark (50 rows, or 7 days
   with ≥ 10 new rows — an idle project never pays on schedule alone); the
   Stop hook probes it every turn and spawns `consolidate_async.py --cwd`
   DETACHED; the worker re-checks under the lock. Marker I/O is
   `read_consolidation_marker` / `write_consolidation_marker` in core,
   shared by the async hook AND `/cc-mem consolidate` — the CLI never wrote
   the marker, so a manual run left the probe reading "due" and a redundant
   background pass followed. The read compares paths with `normcase`
   (hook-written `d:\…` vs CLI-written `D:\…`); an exact compare makes every
   manual run invisible to the probe. The `.consolidation.kick` cooldown
   fails CLOSED (cannot write → do not spawn): a spawn that cannot be
   rate-limited is a spawn storm behind one failing worker.

2. **`deep_dedup` converges because judged groups are REMEMBERED.**
   Nomination is deterministic, so "loop until dry" re-judges the same
   refused groups forever without the `skip_signatures` set. Signatures are
   recorded even when the judge errors (a dead API must end the loop, not
   spin it), and nomination over-fetches past the seen set so the 12-group
   cap cannot mask unseen groups. Both the round cap and budget exhaustion
   are announced — no silent caps.

3. **Only `directive-add` may bump `times_stated`.** The count is the
   ledger's one importance signal and `directive-list` sorts by it; when
   `directive-add` was also the only edit path, nine reference repairs
   inflated nine counts and the most-EDITED directives outranked the
   most-DEMANDED ones. `db.edit_directive` corrects fields without touching
   the count or `last_seen_at`, stamps `turns_at_touch` (an edit is
   attention), and REFUSES to create — an edit door that creates is a second
   upsert with divergent defaults. The CLI's `directive-edit --status`
   accepts only `active`/`blocked`, so the edit door cannot bypass
   `directive-close`'s evidence gate. Gate: `falsify --case r12nobump`.

4. **Idle enforcement skips `status='blocked'` and `kind='constraint'`.**
   Blocked = waiting on the USER (the idle scan reads active rows only);
   constraint = a standing prohibition with no recordable positive action —
   its success is that nothing happens. The constraint skip lives in
   `core.plan.blocking_reasons` (the policy point) and ONLY there; putting
   it in the scan too is how two copies drift. Gate: `falsify --case
   r12constraint`.

5. **Directives reference plan steps by TITLE, never by number.** Step ids
   are positional and die with their plan; Autoshop measured 11 dead and 4
   silently-RETARGETED references after two replans — the retargeted ones
   read correctly and point at the wrong work. `core.plan.
   stale_directive_step_refs` audits active directives on every `plan-set
   --from-refiner` (dead / retargeted, carry judged at the carryover gate's
   own bar) and `directive-add`/`edit` warn at write time. Advisory by
   design: the rot lives in the ledger and must not hold the plan hostage.
   Gate: `falsify --case r12stepref`.

Also: `sql`/`directive-list` gained `--full` (untruncated) and `--json`
(pure-ASCII wire format — the capturing shell picks its own decode codec, and
PowerShell 5.1 uses the console codepage, so UTF-8 CJK reached consumers as
`�`; `\uXXXX` escapes cannot be garbled by any codec); `/cc-mem paths` prints
the resolved artifact paths without creating anything; the Stop refusal's
contradictory "or" became the one real sequence; and `docs/ARCHITECTURE.md`
§3/§5 stopped describing the v2.10-era advisory nudge two releases after
enforcement replaced it.

## What changed in v2.11.3 (over v2.11.2)

**A green gate run is not evidence that a design was written down.** v2.11.2
migrated the schema and attached a load-bearing rule to it; all ten gates
passed, and `turns_total` / `turns_at_touch` appeared **0 times** in
`docs/CONTRACTS.md`, `docs/ARCHITECTURE.md`, `commands/cc-mem.md` and both
Chinese siblings. The doc gates check citation line numbers, bound counts and
translation hashes — none of them asks whether a new invariant reached the
specification.

The rule now lives in `docs/CONTRACTS.md` § Plan contract as the fourth
load-bearing property of a refusal, with the two earlier shapes recorded and
why each looked right. **Put a new invariant in CONTRACTS, not only in
CHANGELOG**: the person about to break it will be reading the specification.

Also: `README.md` stopped claiming cross-platform support "by construction" —
an intention, not a measurement — and now states what CI runs (all gates on
Windows and Linux 3.11/3.13) and that macOS is unmeasured.

**Known limit, recorded rather than papered over:** no gate detects an
undocumented design. This release fixed the instance by hand; the class is
open.

## What changed in v2.11.2 (over v2.11.1)

**The three items v2.11.1 recorded as open, closed — including the one it had
approximated rather than fixed.** Invariants a future change must not break:

1. **Directive idleness is measured on a MONOTONIC clock, never on
   `turns_since_last_guardian`.** That counter is RESET by `/cc-mem plan-check`
   and by every plan replacement, so v2.11.1's "has it been touched since the
   guardian window opened?" guard — which correctly killed v2.11.0's false
   positives — inherited a worse failure from the counter it still read: a
   directive genuinely untouched for 30 turns looked freshly attended to the
   moment anyone ran a guardian check. The ledger forgave exactly the neglect
   it exists to surface. Schema **v9** adds `plan_active.turns_total` (only ever
   incremented; `bump_plan_turn_counter` bumps both, nothing resets this one)
   and `directives.turns_at_touch`. Idleness is now
   `turns_total - turns_at_touch` — subtraction between two numbers that only
   increase. **Do not re-point it at a resettable counter**, and do not make any
   caller responsible for supplying the stamp: `upsert_directive` and
   `set_directive_status` read the clock inside their own `BEGIN IMMEDIATE`,
   because the one caller that forgot would write a row that can never be seen
   as idle. Gate: `falsify --case r11resetforgives` proves a guardian check no
   longer forgives; `--case r11idle` proves the per-row measurement.

2. **Linux runs ALL TEN gates**, not a subset. The workflow used to run
   `--fast` on Linux behind a comment asserting `smoke_test`/`test_surfaces`
   were Windows-specific. That was an assumption, never a measurement, and it
   left the single largest unknown in the project unmeasured — whether this
   plugin works on Linux at all. `python3-tk` is the one real dependency those
   suites need. If a genuinely platform-specific case ever appears, skip THAT
   CASE with a stated reason; do not silently shrink the job back to `--fast`.

Also: the stray `.pytest_cache/` is gone from a project that documents "no
pytest", and `tests/test_directive_enforcement.py` was 53 checks at v2.11.2
(the v2.11.0 entry's "27" is historical and correct for that release).

## What changed in v2.11.1 (over v2.11.0)

**The enforcement engine shipped with zero coverage, and the gate list was
prose.** v2.11.0 was released with `tests/smoke_test.py` **RED** — the directive
ledger added three CLI subcommands and `commands/cc-mem.md` was never updated —
and because `main()` is one sequential function, that first failing assert also
hid the `12 tables` assert below it. Nothing caught either, because "run all ten
gates" was a sentence in this file rather than an executable.

Six defects in the enforcement path, each reproduced before it was fixed:

1. **The escape budget could never release.** `core.markers.write_marker`
   **never raises** (its docstring's first line); all three failure paths
   `return False`. `_block_attempt`'s `except OSError` was therefore dead code
   and the return value was discarded, so on any temp directory `core.markers`
   refuses, nothing persisted, every read came back empty, `n` stayed 1, and the
   hook refused **forever** — measured `[1,1,1,1,1,1,1,1]` over eight Stops.
   "An unbreakable block is worse than no block" is the v2.11.0 invariant this
   line exists to hold, and it did not hold.
2. **A stored directive reached Claude as a live authority marker.**
   `render_block_reason` was the ONLY renderer in `core/plan.py` that did not
   neutralize, and the block `reason` is fed back as a `{"decision": "block"}`
   payload — a higher-authority channel than PROGRESS.md. Escaped on **both**
   sides now (`db.upsert_directive` → `clean_for_storage`, and
   `neutralize_document` on the way out).
3. **A refusal's stdout was not a JSON document.** An unconditional status line
   printed before the decision. The status line is now built first and emitted
   only on the paths where the turn is allowed to close.
4. **A cleared plan enforced forever.** `clear_plan_active` keeps a tombstone
   (that is what keeps `revision` monotonic across clears); the hook tested the
   row's truthiness. `core.plan.is_live_plan` is now the named predicate —
   **named on purpose**, because a test can only re-implement an inline
   condition, and a re-implementation is a tautology (`falsify --case
   r11tombstone` ran GREEN until the predicate existed).
5. **A just-stated directive was reported idle** — `_idle_directives` stamped
   every row with the PLAN's counter. It now requires the directive to be
   untouched since the guardian window opened, comparing with `>=` because
   `_now()` stamps WHOLE SECONDS.
6. **Re-stating a directive erased it.** The CLI's argparse defaults are `''` /
   `'standing'`, not `None`, so a bare `directive-add <slug>` overwrote `demand`
   and `quote` — the one operation the ledger exists for.

Plus: `upsert_directive` takes `BEGIN IMMEDIATE` (8 concurrent creators of one
slug → 0 exceptions, 1 row, `times_stated=8`); `hooks/_entry.py` joined
`_REQUIRED_PLUGIN_FILES` — the THIRD time that list went stale, so the
requirement is now **derived** from the hooks' module-level import graph rather
than hand-maintained; and a `.*/` line in `.gitignore` had taken `.github/` to
zero tracked files invisibly, which is now gated.

Nine new falsification cases (`r11*`), **every one driven RED individually** —
two of them ran GREEN first and the CHECKS were fixed, not the cases.

## What changed in v2.11.0 (over v2.10.1)

**Advisory became enforced, because advisory did not work.** Every piece of
plan machinery in this package was a suggestion, and `hooks/stop.py` said so
in its own comment: *"The plan-refiner nudge is advisory."* On top of that the
nudge was rate-limited to once per five turns.

The measurement that forced this, from a real consuming project
(`lore_disaster`, 2026-08-15): a **51,237-char raw plan sat unrefined** while
`PLAN.md`, `plan-status` **and the drift guardian** all answered from the
PREVIOUS plan — the guardian was dutifully drift-checking against a superseded
baseline. A full-transcript audit of **416 deduped user messages** then found a
feature the user had demanded **six separate times** with zero implementation,
and a pause rule stated **three times** that was violated the first time it
mattered. Nothing detected any of it, because nothing was ever forced.

Three additions a future change must not break:

1. **Stop enforcement, with a guaranteed escape.** `core/plan.blocking_reasons`
   returns the conditions that must stop a turn (unrefined plan, undrift-checked
   plan, idle directive); `hooks/stop.py:_emit_block` emits
   `{"decision": "block", "reason": ...}`. **The escape budget is load-bearing**:
   after `_BLOCK_MAX_CONSECUTIVE` refusals of the *same condition set* it
   degrades to a loud advisory, and `_block_attempt` keys the counter by a
   digest of the condition keys so fixing one problem never spends the budget
   of the next. An unbreakable block is worse than no block. Kill switch:
   `CC_MEMORY_PLAN_ENFORCE=0`. Projects with no plan row are never enforced,
   so opting in is what turns it on.

2. **The directive ledger is NOT plan steps.** A plan step is a unit of
   EXECUTION and dies when the plan is replaced or the step is marked done. A
   directive is a unit of INTENT and outlives every plan. Folding one into the
   other is exactly how the six-times-repeated demand vanished — it was never a
   step in whichever plan happened to be active. `times_stated` accumulates on
   ONE row because repetition is the importance signal a plan cannot express,
   and `directive-close` **refuses without `--evidence`**: a directive closed
   on an assertion is the failure the ledger exists to prevent.

3. **`source` mirrors Scanned/Manual.** Rows authored from what the user
   actually said are never rewritten by machinery; only `derived` rows may be
   refreshed. Same principle that keeps a rescan from destroying hand
   annotation.

Gate: `tests/test_directive_enforcement.py` (27 checks) plus a live hook drive
proving the wire format reaches the harness and that the escape budget really
releases (`[True, True, True, False, False]` over five consecutive Stops).

## What changed in v2.10.1 (over v2.10.0)

**The three items v2.10.0 left open, closed.** Full narrative in
`CHANGELOG.md`. Two additions a future change must not break:

1. **The dashboard's logic cores are PURE staticmethods, driven by §8.**
   `DashboardApp._render_progress_plan` (Progress/Plan tab text, including
   the register-E3 marker escaping) and `DashboardApp._normalize_tidy_verdict`
   (the LLM tidy verdict normaliser) take plain data and return plain data —
   no Tk, no DB. Do not fold them back into their callbacks "for locality":
   the callbacks keep widget plumbing and dialogs ONLY, and
   `tests/test_surfaces.py` §8 drives both cores headlessly
   (`falsify --case r10dashrender` proves the escape assertion is not
   vacuous). The Tk shells hold no logic now; that is what makes their
   remaining zero coverage tolerable.

2. **The contracts registries fail LOUD when their proxy goes hollow.**
   `tools/contracts.py:_verify_entry_gate` errors both registries if
   `hooks/_entry.py` stops consulting `is_excluded` before `project_root` —
   six hooks listed as protected by a gate that is not one was the registry's
   one way to lie (`falsify --case r10gateproxy`). Same fail-loud rule as
   `_BACKSTOP_CREATORS`; keep them in step.

## What changed in v2.10.0 (over v2.9.0)

**An anti-bloat architecture round, driven by measurement.** A function-level
LOC + cyclomatic-complexity sweep of the whole tree against the v2.5.0
baseline (487 → 818 functions, 12,514 → 20,836 function-LOC) found exactly ONE
structural duplication worth a mechanism — and confirmed the rest of the
growth is machinery this file already documents (atomic / textsim / markers /
roots / snapshot-verdict guards, each line traceable to a measured defect).
Full register: the complexity data and per-finding dispositions were written to
`.ccm/arch-review-2026-08-10.md` — which is **maintainer-local and in no
clone**, because the state directory is git-ignored by design (see
`.gitignore`, which anchors BOTH `/memory/` and `/.ccm/`). Cited
here as provenance for how the round was conducted, NOT as a file a reader can
open; anything a future change must actually obey is restated below or in
CHANGELOG.md. The invariant a future change must not break:

1. **`hooks/_entry.py` is THE hook entry ladder.** Every hook parses stdin
   through `parse_payload` (read-to-EOF, JSON, object check — the guard
   comments that used to be pasted verbatim into six files live on the ONE
   implementation now) and routes cwd through `resolve_project`, which owns
   the `is_excluded`-on-RAW-cwd-THEN-`project_root` order. Six hand-rolled
   copies of that ladder is how every drift between them became a shipped
   defect — v2.7.0's whole release theme, and the v2.9.0 junk-cwd database
   plant, were both single-rung misses. Field POLICIES stay per-hook on
   purpose (coerce vs abort, pre_compact's NUL check, each hook's
   excluded-branch reaction); the gate carries the mechanism only.
   `tests/test_surfaces.py` §7 asserts the order once inside the gate and
   refuses a direct `is_excluded`/`project_root` import in any hook; §4
   gained the NARROW-exclusion drive (a listed subdirectory inside a live
   project) that goes red when the order is inverted —
   `tools/falsify_fixes.py --case r10entryorder` proves it. Do not re-inline
   the ladder "for one hook's special case": the special cases are already
   parameters.

Deliberately NOT refactored, with the reasoning on record: `pre_compact.main`
stays one linear pipeline (its length is documentation of measured failure
modes, not duplication); `db.py`'s snapshot-verdict cluster and
`session_start._refresh_progress_row`'s three-tier fill are essential
complexity (every branch is a distinct measured defect); the dashboard's three
cx-47..100 functions stay untouched because they have ZERO executable coverage
(measured into `.ccm/falsify-coverage.md`, maintainer-local — the state
directory is git-ignored, so no clone has it; re-derive with `python tools/falsify_fixes.py
--list` rather than looking for the file) and refactoring an untested 2.9k-line
GUI is the exact越改越错 entry point this round exists to avoid — user-ratified
deferral, 2026-08-10.

## What changed in v2.9.0 (over v2.8.0)

**A dual-perspective review, not another audit round.** Two readers with
disjoint file sets went over the shipped v2.8.0 tree at the same time — a
six-scope fan-out of my own with adversarial refutation, and an independent
read-only pass by codex — and 18 defects survived being reproduced here.
Full narrative in `CHANGELOG.md`. The invariants a future change must not
break:

1. **`archive_obsolete` COALESCEs `supersedes_id`, never overwrites it.** A
   loser produced by an earlier SUPERSEDE already carries a link to the row it
   replaced; overwriting made the original unreachable from every chain walk
   (chain `[2,1]` → `[2,3]`) while `/cc-mem supersedes` still labelled the
   result "newest first". The slot records the FIRST lineage fact; a second is
   logged.

2. **`patch_progress` bootstraps and patches in ONE transaction.** The old
   three-transaction shape (read → conditional `upsert_progress` → UPDATE, each
   on its own connection) let a stale "row absent" verdict replay the default
   row over a landed patch. `INSERT OR IGNORE` under `BEGIN IMMEDIATE` leans on
   the PK and the schema DEFAULTs, which are verified identical to
   `upsert_progress`'s defaults dict.

3. **MEMORY.md's moved-under-us probe is `PRAGMA data_version` on a HELD
   connection.** Every writer here commits on its own connection, so the
   counter sees any concurrent commit. The retired fingerprint (row counts +
   `MAX(id)` + `MAX(updated_at)`) was blind to an in-place UPDATE inside one
   clock second, because `_now()` stamps whole seconds.

4. **Every `/cc-mem` command that touches a table scopes it to the project.**
   `memories.id` is global to the DB file and one file legitimately holds
   several projects: `encoding-check --apply` archived another project's rows,
   `supersedes` printed another project's content into the session, and
   `sessions` / `keywords` listed it. `cmd_archive` had the guard; the rest did
   not. A new command that reads a table without `WHERE project_id = ?` is a
   cross-project leak, not a style nit.

5. **The installer judges hook ownership per ENTRY on BOTH paths.** Register Y2
   fixed the uninstall path and left the install path dropping whole matcher
   groups, so a reinstall deleted a user hook sharing a group with ours. Do not
   re-introduce `_is_ccm_group` as a strip criterion.

6. **`_settings_fingerprint` returns a SENTINEL for an absent file, never
   `None`.** Both halves of the compare-and-swap are gated on
   `expect is not None`, so `None` disarmed the whole guard on exactly the
   machines where settings.json does not exist yet.

7. **Both fail-closed link guards use `core.markers._is_link`.**
   `stat.S_ISLNK` is False for a Windows junction (`mklink /J`, no admin), so
   the `is_symlink()`-only probes in `core/progress.py` and `core/roots.py`
   were inert on the primary platform — a junctioned `.ccm/` was written
   into and adopted as a project root.

8. **Hooks read stdin to EOF.** `post_tool_use` was the only one with a prefix
   cap; a payload over it truncated mid-JSON and the silent handler dropped the
   whole event — the observation row AND the mode-independent live-plan block.

9. **The web viewer bounds the HEADER phase by absolute wall clock**
   (`_HEADER_DEADLINE_S`), not only the body. `timeout` is per-recv, so a
   drip-feeder held an admission permit indefinitely; 16 of them shed all real
   traffic. The shed 503 also half-closes and drains before closing, or Windows
   answers with an RST that discards it.

10. **MCP requires `jsonrpc == "2.0"`** and answers `-32600` otherwise.

11. **The gates are subject to the same review as the code.** This round found
    five holes in them: a `.py`-only citation regex (25 citations exempt, 2
    already rotten), a `doc_claims` grammar that one modifier word defeated,
    a `render_paths` probe missing `neutralize_document` and aliased imports,
    a `verify_anchors` handler catching only `SystemExit` (a `BaseException` —
    naming it is not naming `Exception`), and `_HOOK_ORDER` bound to nothing.
    A gate that cannot go red is a gate that is lying.

## What changed in v2.8.0 (over v2.7.0)

Full narrative in `CHANGELOG.md` — including rounds 4 through 8 (state
machines / clock / injection budgets, two hazard-closure passes, and the
two-round cc-tree radial audit), whose invariants are enforced by the gates
and the falsify register rather than restated here. The list below is the
round-3 set, which attacked memory CONTENT rather than paths:

1. **`core/textsim.py` is THE similarity substrate.** `llm/memory_writer.py`,
   `core/consolidate.py` and `core/plan.py` import from it; none may re-grow a
   private `_trigram_set` (`smoke_test.py` asserts identity, not equality).
   Character trigrams collapse on CJK — a one-character correction to a
   ten-character Chinese fact scored **0.4545**, under `MID_SIM`, so neither
   MERGE nor SUPERSEDE could fire and every Chinese correction was filed as a
   new fact (measured at 0.23 on a live database). CJK runs shingle as
   BIGRAMS; everything else keeps trigrams, and ASCII output is
   byte-identical to the retired copies — do not "simplify" that to one
   granularity, because every tuned threshold in the tree was calibrated on
   the ASCII numbers. `_word_set` is CJK-aware for the same reason: the old
   `[a-z0-9_]{3,}` grammar returned an EMPTY set for a Chinese memory, so
   `semantic_dedup` could never nominate one to the judge.

2. **Tags are UNIONED with the surviving row's, never replaced, and capped.**
   MERGE wrote `set(incoming + ["merged"])` and destroyed provenance
   (`["observer","realtime"]` → `["merged"]`). `MAX_TAGS` exists because
   `memory_add` is model-invokable and an unbounded list was stored verbatim.

3. **`supersede_memory` is ONE transaction**, and every `id IN (...)` writer
   chunks through `MemoryDB._id_chunks`. A kill between a separate insert and
   archive left BOTH rows active; an unchunked statement raised
   `too many SQL variables` past the cap.

4. **A verdict computed from a SNAPSHOT writes through
   `archive_if_unchanged`, not `bulk_archive`.** `cleanup_garbage`,
   `merge_near_duplicates` and `archive_consolidated` all read in one
   transaction and write in another while the PreCompact writer runs
   concurrently; the `content_hash` condition is what makes a stale verdict a
   no-op instead of data loss.

5. **`supersedes_id` stays a DAG.** `archive_obsolete` refuses a link that
   would close a cycle and logs it.

6. **Both loaders drop non-record JSONL lines.** `json.loads` succeeds on
   `null` / `42` / `"s"` / `[1,2]` / `true`, and one such line used to abort
   an entire compaction — including the PROGRESS.md handoff.

7. **`core.markers.safe_id` hashes the WHOLE session id**, and BOTH marker
   paths refuse a symlink. The truncating `[:16]` copies cross-wired any two
   sessions sharing a prefix. `O_NOFOLLOW` is 0 on Windows and an `fstat`
   after the open describes the TARGET — `os.lstat` is the portable guard,
   and dropping it re-opens an exfiltration channel into the Anthropic
   request (the prompt marker is spliced into it).

8. **`call_llm`'s `deadline` is TRUE wall-clock.** Clamping the socket
   timeout bounds only the idle gap; a drip held a "3 s" leg for 11.07 s.
   `_abort_response` cuts the socket rather than calling `resp.close()`,
   which DRAINS the body and blocked for 8.10 s of that.

9. **`pre_compact` COERCES `trigger` / `session_id` and exits early only on
   `cwd` / `transcript_path`.** The first two are annotation; abandoning a
   compaction over them costs the handoff, which is not optional.

10. **`/cc-mem archive` is the user-facing retirement path**, and it archives
    — `db.delete_memories` still has no caller. Reconciliation handles a
    RESTATEMENT of a fact; nothing else handled a REPUDIATION of one.

11. **`tools/contracts.py` must count the BACKSTOP creators too**
    (`_BACKSTOP_CREATORS`, verified against each module's source). Counting
    `ensure_memory_dir` callers alone certified 6 of 8 — the prose-enumeration
    disease recurring inside its own cure.

## What changed in v2.5.4 (over v2.5.3)

**Zero known limits.** v2.5.3's four residuals, closed by measurement. The
invariants:

1. **No citation may be UNCHECKED.** `tools/citation_check.py` anchors on a
   symbol where it can and BOUNDS-checks (inside the file, non-blank) where it
   cannot; `smoke_test.py` fails on any `SKIP`. If a new citation shape cannot
   be anchored, teach the checker that shape — do not let it opt out. The bounds
   check found 34 stale citations on its first run.

2. **The installer verifies its `settings.json` write BOTH before and after the
   rename.** The pre-check cannot cover the window between itself and the
   rename; the post-check reads the file back and compares it byte-for-byte.
   Removing either half reopens a lost update in one direction.

3. **Derived artifacts retry against a wall-clock BUDGET**
   (`core/atomic.py:_DERIVED_BUDGET_S`), not a try count: the destination is
   unavailable for a duration, not for a number of attempts. 12 fixed tries lost
   2 of 150 renames under three 100 %-duty readers; a 3 s budget lost 0.
   PROGRESS.md keeps the short count because its writer RAISES.

4. **Both exes are RUN before release**, not just PE-header inspected.

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
   newlines are real structure. **`python tools/contracts.py` lists the render
   paths covered today** — do not restate the list here. This paragraph named
   four while the tree had six, went on saying so for three releases, and each
   convergence round rediscovered it as a new defect; enumerating a set in
   prose IS the defect, so the enumeration now lives in the generator and
   `tools/doc_claims.py` fails the build when a bound count disagrees with it.
   `core/consolidate.py`'s `^</?(ide_opened_file|system-reminder|antml)` list is
   garbage cleanup, **not** this defence — it is anchored at position 0 and one
   leading word evades it.

2. **`core.modes.is_excluded` is consulted by every surface that can open a
   project**, hooks and hand-run tools alike — `python tools/contracts.py`
   prints which. The MCP server's `_get_db` is the one to keep in mind: it is
   the single choke point every MCP tool reaches, it is loaded by default from
   the shipped manifest, and every call is model-initiated, which makes it the
   *least* optional of them. Do not add an MCP handler that opens a DB path
   itself. (This entry used to assert "SEVEN callers, not six". It was seven
   when written and is twelve now — the same prose-enumeration defect as §1.)

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
   the `finally` is the only new behaviour. Every call site keeps
   `with self._connect() as conn:`. Cost is real and measured: +340 % per
   operation (WAL checkpoint on last-close), +0.6 s on a 120 s PreCompact
   budget. Do not "optimise" it back into a factory.

6. **`<private>` is honoured on BOTH progress ingresses** —
   `hooks/user_prompt.py` and `hooks/pre_compact.py:_first_user_request` — and
   cleaning happens **before** the 500-char cut so a span straddling the cut
   stays a matched pair. PROGRESS.md is not in `.ccm/.gitignore`, so a leak
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
   varied by mode. `_apply_plan_integration` (`post_tool_use.py:88-120`) now runs
   **above** the gate; the gate wraps only the `insert_observation` block.
   Plan control is not observation — `core/modes.py`'s `should_observe`
   docstring now forbids re-inverting this. Per mode: ExitPlanMode → plan rows
   0/0/0 → 1/1/1; Edit counter 1/0/1 → 1/1/1; `git push` 21/20/1 → 21/21/21.
   A raw plan awaiting refinement is also no longer invisible:
   `core.plan.raw_pending_refinement` (`plan.py:402-431`) makes PLAN.md and
   `plan-status` lead with a PENDING REFINEMENT banner + the raw text.

3. **`core/privacy.py` failed OPEN.** `strip_private` was a non-greedy `re.sub`
   behind a `count("<private>") > 100` ReDoS guard that **returned the text
   unchanged** — 100 tags stripped, 101 leaked, into both the Anthropic call and
   the `memories` table. The cap was calibrated on the wrong signal too: 20,000
   well-formed tags cost `re.sub` 6.0 ms, but 16,000 **unterminated** ones
   (140.6 KiB) cost 9,517.4 ms. Replaced by a single left-to-right `str.find`
   scan (`_strip_spans`): no cap, 0.0 ms on that input, and a dangling
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
   hooks that CREATE `.ccm/`), which left a project initialised BEFORE it was
   listed fully instrumented — the other four gate only on `.ccm/memory.db`
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

- `core/db.py`'s three plan mutators — `update_plan_status` (`db.py:3341-3385`),
  `delete_plan` (`:1410`) and `update_plan_content` (`:1427`) — all accept
  `project_id`, and `cli/plan.py` + `ui/dashboard.py` pass it at every call
  site, but none of them *requires* it (it defaults to `None`). An unscoped raw
  call from new code would therefore still cross projects, because `plans.id` is
  global to the DB file. This is the wording `README.md` § "What is *not* fixed"
  uses; the pre-v2.5.1 text here claimed `delete_plan` / `update_plan_content`
  "take no `project_id`", which contradicted both the code and the README.
  *(Closed in v2.5.3 — see § "What changed in v2.5.3" item 2: `project_id` is
  REQUIRED and keyword-only on all three, asserted by `smoke_test.py`.)*
- `ThreadingHTTPServer` has no worker cap — body reads are deadline-bounded, the
  thread count is not. Loopback-only. DNS rebinding was verified with forged
  `Host` headers, not real DNS; the SPA escaping hardening is defence-in-depth
  (no XSS was executed). *(The cap half closed in v2.8.0 — `_MAX_CONCURRENT =
  16` admission, excess shed with a 503; the other caveats stand.)*
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
3. **Killed runs are visible.** `.ccm/.pre_compact_attempt.json` is written
   at entry and removed only on completion, so a surviving marker proves the
   last attempt died (a timeout kill runs no `except` block, which is why the
   failure used to leave no trace at all). `.last_save.json` gained `trigger`,
   making AUTO compactions distinguishable from "never ran".
4. **`RuntimeError` added to the extraction `except` tuple** — a total LLM
   outage no longer skips the PROGRESS.md rewrite.
5. **`.ccm/.gitignore` migrates existing installs** via
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
outgoing plan is archived to `.ccm/.plan_history/`. **No force flag, by
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
   `session_count % N` check. `.ccm/.last_consolidation.json` records the
   count at the last run; a lock file prevents overlapping workers. Race-immune
   against the concurrent sync leg (WAL + busy_timeout make it safe).
3. **Honest budget cost model.** `consolidate_topics` is now budget-gated (it
   was the one ungated LLM loop), and `call_llm` takes a bounded
   `fallback_timeout` so a budgeted call's worst-case wall-clock is known up
   front. The BudgetGate therefore GUARANTEES the run finishes by
   `total_s - safety_s` (232s) < the 300s async timeout — never killed mid-write.

## What's new in v2.2 (over v2.1)

1. **Live PLAN.md anchor.** `.ccm/PLAN.md` is a new generated artifact that
   captures the project's current goal + step status. ExitPlanMode output
   (or user-supplied `/cc-mem plan-set` text) lands in the `plan_active`
   SQL table; TodoWrite events sync step statuses mechanically; sensitive
   Bash patterns (`git push`, `rm -rf`, deploys) flag drift.
2. **Two plugin-shipped subagents.** `agents/plan-refiner.md` normalises a
   raw plan into a structured JSON schema; `agents/plan-guardian.md` does a
   read-only ≤150-word drift check on demand. Stop hook emits an advisory
   status line when guardian thresholds trip (default: 8 turns OR 12 edits).
3. **`/cc-mem dashboard` + 6 new `/cc-mem plan-*` subcommands.** The GUI
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
3. **Forced handoff.** `.ccm/PROGRESS.md` (new in v2.1) replaces
   `SESSION_HANDOFF.md`. SessionStart emits a `<system-reminder>` block that
   directs the next Claude to `Read .ccm/PROGRESS.md` BEFORE responding.
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
├── docs/                        ← TWO English specifications since v2.4.3,
│   │                              each with a drift-tracked .zh.md sibling,
│   │                              plus the v2.14.0 evidence record
│   ├── ARCHITECTURE.md          ← overview + install layouts + i18n convention (§9)
│   ├── ARCHITECTURE.zh.md
│   ├── CONTRACTS.md             ← anti-patch + forced handoff + live plan anchor
│   ├── CONTRACTS.zh.md
│   ├── debug-pass-2026-09.md    ← the v2.14.0 debug pass report (untranslated,
│   │                              no .zh.md sibling)
│   └── debug-pass-2026-09/      its evidence files, repros and report.html
├── demo/                        ← the before/after evidence README § "Before
│   │                              and after" quotes
│   ├── run_demo.py              the capture protocol, as code
│   ├── tally/                   the fixture project both sides run against
│   └── captures/                handoff/ + guardian/ streams and transcripts
├── README.md / README.zh.md     ← drift-tracked pair
├── .github/                     ← CI + community health (v2.11.1)
│   ├── workflows/gates.yml      the release gates, as an executable
│   ├── workflows/release.yml    tag push → gates → build exes → RUN them → Release
│   ├── ISSUE_TEMPLATE/          bug_report.yml, feature_request.yml
│   └── PULL_REQUEST_TEMPLATE.md
├── tools/                       ← dev/CI checkers, NEVER packaged
│   ├── i18n_check.py            translation drift
│   ├── citation_check.py        every file.py:LINE citation in tracked docs
│   ├── doc_claims.py            prose counts vs the computed sets
│   ├── doc_coverage.py          every public surface is named by its doc
│   ├── contracts.py             computes each set from the tree (not a gate)
│   └── falsify_fixes.py         reverts each fix on a COPY (not a gate)
├── cc_memory/
│   ├── __init__.py              (re-exports core/version.py)
│   ├── config.json
│   ├── core/                    db, extractor, consolidate, idle, progress,
│   │                            plan, privacy, modes, roots, auth, logger,
│   │                            encoding_setup, version, atomic, markers,
│   │                            textsim, layout (the state dir's name +
│   │                            its one-way migration, v2.13.0, and the
│   │                            one comparable path spelling)
│   ├── hooks/                   _entry (shared entry ladder, v2.10.0),
│   │                            post_tool_use, pre_compact, consolidate_async,
│   │                            session_start, stop, user_prompt
│   ├── llm/                     ccl_backend, memory_writer, parse
│   ├── cli/                     mem, plan
│   ├── mcp/                     server
│   └── ui/                      installer, dashboard, web_viewer
├── tests/
│   ├── run_gates.py             ← THE gate runner: one command, all 11
│   ├── smoke_test.py            end-to-end anti-patch + PROGRESS.md +
│   │                            tier-3 transcript + layout-inspector +
│   │                            live-plan + i18n gate + bounded-window tests
│   ├── test_plan_carryover.py   carryover gate (v2.4.0+), 20 checks
│   ├── test_surfaces.py         installer surfaces + settings shapes + timeout
│   │                            lockstep, MCP stdio, web-viewer guards, hook
│   │                            LLM deadline (v2.5.0)
│   └── test_directive_enforcement.py
│                                directive ledger + Stop enforcement (v2.11.0)
├── scripts/                     ← build + release helpers (off the root, v2.11.1)
│   ├── build_exe.py             PyInstaller build
│   └── release_notes.py         CHANGELOG section → GitHub Release body
├── pyproject.toml
├── CONTRIBUTING.md / SECURITY.md
├── CLAUDE.md                    ← This file
├── CHANGELOG.md
└── LICENSE
```

## Hooks (6) <!--ce:hooks-->

Declared in `hooks/hooks.json`. A **marketplace / dev-checkout** install is
discovered via `enabledPlugins` + `extraKnownMarketplaces` in
`~/.claude/settings.json` → the plugin manifest → `hooks/hooks.json`; the
`hooks` key of `settings.json` stays untouched. Only the **standalone**
installer (`ui/installer.py:_merge_into_settings`) writes hook entries there.
`PreCompact` fires TWO command hooks <!--ce:hooks:subset--> (v2.3.2): a
blocking sync leg + a background `async` leg.

| Hook | Entry | Timeout | Purpose |
|------|-------|---------|---------|
| `PreCompact` (sync) | `cc_memory/hooks/pre_compact.py` | 120s | LLM extract → memory_writer.upsert_batch → FULL-REWRITE PROGRESS.md → archive (fast, ~1-5s) |
| `PreCompact` (async) | `cc_memory/hooks/consolidate_async.py` | 300s, `async:true` | Background consolidation every N sessions OR on write backlog (interval marker + lock, budget-gated) — off the blocking path; also spawnable standalone (`--cwd`) by the Stop probe |
| `SessionStart` | `cc_memory/hooks/session_start.py` | 15s | Inject layered context (the directive ledger FIRST, v2.12.2) + FORCED `<system-reminder>` to Read PROGRESS.md |
| `Stop` | `cc_memory/hooks/stop.py` | 22s | Observer (Haiku) + per-turn PROGRESS.md patch + idle reorg every 5 turns + consolidation backpressure probe (v2.12.0) + plan enforcement |
| `PostToolUse` | `cc_memory/hooks/post_tool_use.py` | 8s | Live plan anchor in EVERY mode (ExitPlanMode capture / TodoWrite step sync / drift counters), THEN an observation row for observed tools only (no LLM) |
| `UserPromptSubmit` | `cc_memory/hooks/user_prompt.py` | 8s | Auto-init `.ccm/` (migrating a pre-v2.13.0 `memory/`) + turn count + seed `progress.current_request` on the first NON-scaffolding prompt, once per session (v2.14.0) |

Hook contract (NEVER violate):
- Hooks must NEVER write to stderr (Claude Code shows stderr as error UI).
  Use `core.logger.get_logger(...)`; it writes to `~/.claude/hooks/cc-memory/logs/`.
- Hooks must NEVER raise an unhandled exception. Always `sys.exit(0)`.
- Each hook's stdout has a specific role:
  - `SessionStart` stdout → injected context (read by Claude)
  - `Stop` stdout → status line (read by Claude)
  - `PreCompact` (sync) stdout → ONE status line (shows in next session's compacted context)
  - `PreCompact` (async) / `PostToolUse` / `UserPromptSubmit` stdout → empty

## Database schema (12 tables)

Defined in `cc_memory/core/db.py`. See `docs/ARCHITECTURE.md` for full diagram.

- `projects`, `sessions`, `memories`, `topics`, `keywords`, `plans`
- `observations` (PostToolUse events, cleaned after extraction)
- `session_summaries` (6-field structured summary per session)
- `progress` (v2.1: single row per project, SOT for PROGRESS.md)
- `plan_active` (NEW in v2.2: single row per project, SOT for PLAN.md)
- `directives` (NEW in v2.11.0: the user-INTENT ledger, `v8_directives`)
- `_migrations` (tracks applied migrations)

Key columns added in v2.1:
- `memories.supersedes_id` — forms the update chain (anti-patch contract)
- `memories.content_hash` — sha256[:16] of normalized content for cheap dedup

## Anti-patch contract

> Every memory save path routes through `llm.memory_writer.upsert_smart`,
> which MERGES in place, SUPERSEDES with a chain link, REINFORCES an
> exact-hash duplicate (no new row — only the restatement's higher
> importance and new tags are folded into the row it matched; a
> restatement that adds nothing is still a plain SKIP that writes
> nothing), or INSERTS based on
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

> `.ccm/PROGRESS.md` is the single source of truth for session handoff.
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
- `UserPromptSubmit` patches `current_request` on the session's first
  NON-scaffolding prompt, once (`patch_progress`; `strip_scaffolding` is the
  predicate both ingresses share, and the `cc_mem_seeded_` marker records
  that the seed happened — v2.14.0, rule 16).
- `SessionStart` fills ONLY still-empty fields via
  `_refresh_progress_row` (`fill_empty_progress`, which tests emptiness
  INSIDE the UPDATE; `trigger_type="session_start_refresh"` is filled the
  same conditional way — v2.14.0, rule 12).
  Fill-only-empty by contract — it must never overwrite a populated field.

`SESSION_HANDOFF.md` from v2.0 is renamed to `SESSION_HANDOFF.md.v2.bak` on
first PreCompact under v2.1 (one-shot migration in `core/progress.py`).

## Live plan anchor (v2.2)

> `.ccm/PLAN.md` is the single source of truth for the current goal +
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
- `Stop` **refuses the turn** when guardian thresholds are crossed (default:
  8 turns OR 12 edits), when a raw plan is unrefined, or when an active
  directive has been idle past its threshold. Main Claude responds by
  invoking the **`plan-guardian`** subagent (also in `agents/`), then
  `/cc-mem plan-check` to reset counters. Only `plan.apply_refined_plan` may
  clear `needs_refine`.
  (`_claim_refine_nudge` — the once-per-5-turns rate limiter — was DELETED in
  v2.11.0 along with the advisory it throttled. Do not cite it; a rate-limited
  advisory is how a 51,237-char plan sat unrefined while every reader answered
  from the previous one. Its temp-marker prefix stays registered in
  `ui/installer.py` so an uninstall still sweeps what older installs wrote.)

**All of the `PostToolUse` legs above run in EVERY mode, above the
`should_observe` gate** (`hooks/post_tool_use.py:183`). They shipped below it
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
- Read files before modifying them; respect the cc-enforcer-style discipline.

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
  - `["manual","dashboard"]` — `ui/dashboard.py:1791` (Add-Memory dialog)
  - `[method, "manual"]` where `method` is `"llm"` or `"regex"` —
    `ui/dashboard.py:2296` (Save Session)
  - `["regex","manual"]` — `ui/dashboard.py:2322` (Save Session, regex leg)
  - `["metric","manual"]` — `ui/dashboard.py:2327` (Save Session, metric leg)
  - `["auto-detected","init"]` — `ui/dashboard.py:2464` (new-project init)
  - `["web"]` — `ui/web_viewer.py`
  - `["llm-dedup","merged"]` — `core/consolidate.py`

  The writer appends `"merged"` / `"supersedes"` on top of whatever the caller
  passed. NOTE: the PreCompact LLM path sets no `tags` key at all, so those rows
  store `[]` — do not document a `["llm","auto"]` tag that no code emits. If you
  add an emitter, add it to this list; the four `ui/dashboard.py` Save-Session /
  init shapes were missing from it through v2.5.0.
- `.ccm/PROGRESS.md` and `.ccm/MEMORY.md` are generated artifacts. Edit
  the SQL source of truth (`progress` table for PROGRESS.md, `memories`/
  `topics`/`keywords` for MEMORY.md) instead.

## Tests

**ELEVEN release gates, and there is ONE command that runs them:**

```bash
python tests/run_gates.py          # all 11; prints a table, exits nonzero on any red
python tests/run_gates.py --list   # what each gate checks
python tests/run_gates.py --fast   # skips the 2 slow suites — NOT a release run
```

Eleven = four suites, four dev checkers, `compileall`, a `tomllib` parse of
`pyproject.toml`, and version-site agreement. `tests/smoke_test.py` asserts
that this section names every gate script, that every suite/checker ON DISK is
on that list, and that `run_gates.py` actually runs each one — so the list
cannot fall behind the tree in either direction.

**Run the runner, not the list.** "Run all ten" was a sentence in this file
and nothing executed it: v2.11.0 was released with `smoke_test.py` RED (the
directive ledger added three CLI subcommands and `commands/cc-mem.md` was not
updated), and because `main()` is one sequential function, that first failing
assert also hid the `12 tables` assert below it. A gate list that is prose is
a gate list that does not run. `.github/workflows/gates.yml` runs the full set
on Windows (3.13) and on Linux (3.11 and 3.13).

(This paragraph said EIGHT, then NINE, each time by hand. It is now derived:
`run_gates.py:GATES` is the single list, and the count above is asserted
against `len(GATES)` rather than typed.)

`tests/smoke_test.py` is the canonical end-to-end check. In a throwaway temp
project it exercises: v3/v6 migrations, `upsert_smart` decisions
(INSERT/MERGE/SUPERSEDE/SKIP), the `progress` row + `PROGRESS.md`
full-rewrite, the fill-only-empty refresh contract, last-wins TodoWrite
extraction, the tier-3 transcript fallback, the legacy `SESSION_HANDOFF.md`
migration, the layout inspector, the v2.3.2 async consolidation lock/marker,
the v2.3.3 i18n drift gate, the v2.4.2 bounded-window / summary-direction /
killed-run-visibility contracts, and — added across v2.5.2-v2.5.5 — the
`.gitignore` three-copy parity, `_connect` handle-count regression, PLAN.md /
MEMORY.md forgery resistance, the single-atomic-writer rule with its
never-truncate contract and wall-clock budget, the keyword-only `project_id` on
the three plan mutators, and the two DOC gates below.

`tests/test_plan_carryover.py` covers the v2.4.0 carryover gate (20 checks) —
the only coverage of that feature.

`tests/test_surfaces.py` (v2.5.0, nine sections since v2.9.0) covers the
surfaces neither of the others touches: §1 the MCP stdio server, §2 the web
viewer's request guards, §3 the standalone installer (surfaces installed and
removed by name, malformed-`settings.json` shapes, hook-timeout lockstep
against `hooks/hooks.json`, manifest parity so a new runtime module cannot
ship unpackaged), §4 `excluded_projects` across all six hooks <!--ce:hooks-->, §5 the config.json
parser shapes plus the MCP half of the same opt-out, §6 the `settings.json`
compare-and-swap, §7 project-root anchoring (v2.6.0), §8 the v2.8.0 surfaces
(installer init outcomes, `/cc-mem archive`, the wall-clock LLM deadline, the
pre_compact annotation guard, the doc-claims grammar, the plan anchor driven
through its own hook in every mode, the MCP scope gate, the `memory_topics`
bound, and `ui/dashboard.py` executed headlessly — its CLAUDE.md generator
driven against a hostile `package.json` and its SQL read-only classifier
both ways), and §9 the v2.9.0 dual-review surfaces (CLI project scoping across
four commands plus `status` on four wrong-typed `settings.json` shapes, the
installer's per-entry strip and its absent-file compare-and-swap, the surface
manifest union, a 600 KiB PostToolUse payload, the empty-prompt marker
overwrite, the MCP `jsonrpc` member, and 16 header-phase drippers failing to
hold the admission permits). It also asserts the source-level rule that every
LLM-calling hook passes an absolute deadline. v2.14.0 added to §3 (the
frozen `Open Dashboard` spawn, a symlinked `settings.json`, installer prose
naming the old state directory), to §7 (a linked state directory on
`pre_compact`'s recovery path; the first-real-prompt seed, eight sequences),
to §8 (the SQL console's two branches, the manifest slots) and §9h-j (the
CLI boundary, 45 checks); `tests/test_directive_enforcement.py` gained §9
(the advisory line and the refusal slots as render paths, the stale-lock
kick through the real hook).

**§4 now opens by binding its own enumeration.** `_HOOK_ORDER` is the sole
list behind the opt-out gate, §7's subdirectory drive, the
is_excluded-then-`project_root` source rule and the junk-cwd probe, and
nothing tied it to `hooks/hooks.json` — a seventh hook would have been covered
by none of them while the banner still said "all 6". It is asserted equal to
the `hooks` set `tools/contracts.py` computes from the manifest.

**§7 is the twin of §4.** It drives the same six hooks <!--ce:hooks--> from a SUBDIRECTORY of
a seeded project and asserts no second `.ccm/` appears down there while the
root database receives the writes, walks the resolution ladder over a real
filesystem, and asserts the source rule that every hook routes cwd through
`hooks/_entry.py:resolve_project` — the opt-out→anchor ORDER is asserted once,
inside the gate itself, and a direct `is_excluded`/`project_root` import in a
hook is refused (v2.10.0; the same shared-gate shape `_cli_opt_out_gate`
asserts for the three CLI surfaces). A hook that skips the gate is a
split-brain regression AND a privacy regression, not a style nit.

v2.8.0 added the checks below, each verified to FAIL against the state it
exists to catch before being kept. (This used to say "four checks" while
naming five functions in four bullets; `git diff v2.7.0 -- tests/
test_surfaces.py | grep '^+def'` is the actual count and reports nine new §7
functions. The bullets are the interesting subset, not the whole — stating a
total here was the prose-enumeration disease one more time.)

- `_hooks_never_plant_on_junk_cwd` — 48 (hook, malformed-cwd) pairs assert
  rc **and** stderr **and** that no database appears in the hook's own
  directory. Asserting only rc is precisely how `pre_compact`'s side effect
  survived one round of review: it exited 0, wrote nothing to stderr, and
  created `.ccm/memory.db` where the hook process happened to be standing.
- `_cli_opt_out_gate` — drives the real CLIs as subprocesses against a COPY
  of the package, over five `--project` spellings including the blank ones,
  and asserts all three surfaces route through the one shared gate rather
  than calling `is_excluded` directly. Three inline copies is how it drifted.
- `_roots_anchor_announce` — a redirection is announced exactly when one
  occurred, never for `.`, an absolute root, or a trailing `/.`.
- `_roots_skill_bootstrap` / `_skill_shell_metachars` — the `/ccm-load` body
  is a shell double-quoted `python3 -c` blob, so `compileall` cannot see a bad
  layout key (it becomes a swallowed `KeyError`) and a backtick in a *comment*
  is command substitution. Both are static, so they cost no sandbox.

Two of its 23 ladder cases are the ones that cost a design round, and neither
may be weakened: **a directory that already owns a `.ccm/memory.db` is never
re-rooted** (a stray and a deliberate nested sub-project are byte-for-byte
identical on disk — this machine has four genuinely nested ones, the largest
holding 3725 memories, so any rule that "heals" the first orphans the second),
and **a container of projects is never returned** (this machine's projects
folder has 27 project-shaped children). The `~`-boundary case guards the
third: a `memory.db` sitting in a home directory must not capture everything
beneath it, and the boundary is deliberately doubled — environment *and*
platform-conventional structure — because the sandbox this suite runs in
redirects the environment.

**FOUR DOC gates.** Three run inside `smoke_test.py`; `tools/doc_coverage.py`
(v2.11.4) is the fourth and runs standalone, because it asks a different
question from the other three: they verify the docs that EXIST — a citation
still points at its symbol, a counted sentence matches the tree, a
translation is bound to its source — while it asks whether a public surface
produced any documentation AT ALL. v2.11.2's two schema columns appeared 0
times in the specification and all ten gates passed. It enumerates schema
tables, `ALTER`-added columns, MCP tools and config keys from the CODE and
requires the owning document, in BOTH languages, to name each one — NAME,
since v2.14.0, as a code span or a quoted JSON key: a bare substring test
let the `<!-- i18n-source: … -->` marker document a column called `source`,
and the MCP enumerator's name-prefix guess (`memory_` / `progress_`) never
enumerated a tool outside the prefix at all. It now reads the `TOOLS`
registry whole and counts `CREATE VIRTUAL TABLE` (`memories_fts`) as schema,
minus a probe table the file itself drops.
Deliberately NOT checked, measured rather than assumed: migration KEYS
against `CHANGELOG.md` (27 of 29 are absent, and the only remedy would be
rewriting history entries). `tools/citation_check.py`
resolves every `file.py:LINE` citation in **every** tracked markdown file
(the list is `tools/citation_check.py:TRACKED`) —
symbol-anchored where a symbol can be resolved, bounds-checked (inside the
file, non-blank) where it cannot — and no citation may be unchecked. A second
block asserts hand-picked doc facts: that `commands/cc-mem.md` names every
subcommand `cli/mem.py` defines, that this section names every gate script,
and that the "12 tables" claim matches `core/db.py`. Prose facts rot exactly
like line numbers do; nothing checked them until v2.5.5, and three had already
drifted.

**`tools/doc_claims.py` (v2.8.0) generalises that third check.** A citation
gate proves `file.py:LINE` still points at its symbol and says nothing about
the sentence around it, which is why "four renderers <!--ce:render_paths:asof-->
are covered" survived three releases with six in the tree and why five
convergence rounds each
rediscovered the same class of defect. `tools/contracts.py` COMPUTES each set
from the tree — run it to see which sets and their members; this sentence
used to enumerate five of them while the tree had six, which is the disease
this paragraph documents — and prose binds to one with an HTML comment:

```markdown
all six hooks <!--ce:hooks--> resolve after the opt-out
Four of the six hooks <!--ce:hooks:subset--> gate on memory.db
v2.5.1 fixed the six hooks <!--ce:hooks:asof--> and missed the seventh
```

Whole set, strict subset, or a statement about the past. **Do not enumerate a
set in prose** — name the count, bind it, and point at `python
tools/contracts.py` for the members. Version-titled `## What's new in vX`
sections and fenced diagrams are exempt: a history edited to stay current is
not a history, and an HTML comment is literal inside a fence. Since the A5
close of cc-tree round 2 the gate scans THREE surfaces with one grammar: the
tracked markdown, `cc_memory/config.json`, and the docstrings + comment runs
of the shipped package — because the first sweep of the latter two found
three counts already wrong ("the seventh caller" with twelve surfaces
consulting the opt-out, "66 call sites" in a file holding 80, and a comment
claiming three hooks <!--ce:hooks:asof--> imported a module that two hooks
<!--ce:hooks:asof--> and core/idle.py import). `tools/` and
`tests/` are deliberately unscanned; their numbers are examples and expected
output.

```bash
python tests/run_gates.py
# THE command. expect: "[OK] all 11 gates green"
# Individually, when you need one gate's own output:
python tests/smoke_test.py
# expect: [OK] lines ending with "===== ALL SMOKE TESTS PASSED ====="
python tests/test_plan_carryover.py
# expect: "RESULT: 20 passed, 0 failed"
python tests/test_surfaces.py
# expect: "===== ALL SURFACE TESTS PASSED ====="  (§1-§9)
python tests/test_directive_enforcement.py
# expect: "ALL CHECKS PASSED"
python tools/i18n_check.py
# expect: "3 in-sync", exit 0
python tools/citation_check.py
# expect: "0 unchecked, 0 stale", exit 0 (also asserted in smoke_test.py)
python tools/doc_claims.py
# expect: "0 problem(s)", exit 0 (also asserted in smoke_test.py)
python tools/contracts.py
# not a gate — prints what the code currently says each set contains
python tools/falsify_fixes.py
# not a gate either, and the one to run when you doubt a check. It reverts
# each registered fix on a TEMPORARY COPY and asserts the gate goes RED —
# every case in the register was verified RED individually before being
# kept, and `--anchors` proves the register itself has not rotted. A green
# case means the check is vacuous: fix the check, not the case.
# Since v2.14.0 each gate is first run ONCE on an untouched copy
# (`gate_baseline`); a red baseline reports the case UNSOUND, never RED —
# before that control existed the copy had no `.git`, `git check-ignore`
# exited 128, and every smoke-gated case was being judged against a gate
# that was already red. A copy is `git init`ed now. Expect ~7 s per case
# for the copy alone on a machine whose antivirus scans new files.
# `--list` shows what each case breaks; `--case <name>` runs one.
```

No pytest / pip dependencies — every script above is stdlib and reflects the
runtime contract (pure stdlib, see Development guidelines below). When you add a
behavior to `memory_writer`, `progress`, `extractor.load_transcript_window`, or
`session_start._refresh_progress_row`, add a corresponding assertion block.

**Tests MUST use `tempfile` directories only, and MUST remove them.** All
four suites redirect `USERPROFILE`/`HOME` **and** `TMPDIR`/`TEMP`/`TMP` into a
sandbox before importing the package, assert `Path.home()` really moved, and
tear the sandbox down in a `finally` at the ENTRY POINT — on the failure
path too, because falsification runs fail a suite on purpose once per case
and 173 kept sandboxes (941 MB) were measured in the real `%TEMP%` on
2026-09-02 before `smoke_test.py` and `test_surfaces.py` did so — and an
uncleanable leak is a test FAILURE, not a warning. `test_plan_carryover.py` was the exception through v2.8.0: it
ran against the real home and left two project directories per run in the real
`%TEMP%` (found: 270 of them, 42 MB). Every subprocess capture needs an
explicit `encoding="utf-8"` — the default codec on this box is gbk and the CLI
emits real UTF-8.

**`excluded_projects` is covered by `tests/test_surfaces.py` §4** — it drives
all six hooks <!--ce:hooks--> against a fresh excluded directory, a
subdirectory of one, a NARROW exclusion (a listed subdirectory INSIDE a live
project — the direction an anchor-before-opt-out inversion widens away,
v2.10.0), and a
project that was initialised BEFORE it was listed (the case the two-copy v2.5.0
implementation got wrong). Keep that block in step with any hook you add: a hook
that does not route through `hooks/_entry.py:resolve_project` (which consults
`core.modes.is_excluded` before anchoring) is a privacy regression, not a style
nit.

**Doc `file:line` citations ARE gated now — `tools/citation_check.py` (v2.5.2).**
For each `path:lines` citation in the tracked docs it resolves the symbols named
in the surrounding prose with `ast` and asserts the cited range covers the
definition **or** mentions the symbol (the docs cite call sites at least as often
as definitions). It runs inside `smoke_test.py`, so rot turns the suite red.
`python tools/citation_check.py --fix` repairs what it can; `--list` shows every
verdict. First run measured **163 of 594 citations stale** — the cost of three
releases with no gate.

**Quoted evidence is the one thing `--fix` must never touch (v2.12.2).** A
README quote of a captured transcript can itself contain `file:line` text —
the guardian's report cites the FIXTURE's files — and the checker read those
as citations into this tree: it flagged the report's `README.md` line 20 as
stale (that line is blank in THIS repository's README) and `--fix` rewrote
the report's `cli.py` line 12 into line 33 inside a block the README calls
verbatim. Fence such a block with `<!-- verbatim: <capture> -->` …
`<!-- /verbatim -->`: nothing inside is scanned, and every segment between
the markers (split on `[…]`, `>` and fences stripped, whitespace collapsed)
must occur in the named capture or the verdict is `QUOTE` and the gate is
red. A marker inside inline code — like the one in this sentence — is a
description, not a region. A quote that cannot be found in its source is
restored from the source by hand — there is no `--fix` for it, by design.
Gate: `falsify --case r12verbatim` / `r12verbatimskip`.

Two limits to know before trusting a green result: a citation whose sentence
names no resolvable symbol at all is **bounds-checked only** — inside the
file and non-blank, NOT verified against a symbol — and the summary says so
in those words since v2.14.0 (261 of 631 at v2.14.0; the class was 253 of
594 as `SKIP` at v2.5.4, down from 370 once v2.5.3 taught it to anchor
CROSS-FILE citations on the text of the cited range — the
`` `db.tag_progress_session(...)` (`user_prompt.py:276`) `` shape, which is
the commonest in these docs). A bounds-only citation can rot silently: six
had, all in prose that names a section rather than a symbol, and were
repointed by hand in v2.14.0. `--fix`
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
python scripts/build_exe.py
# produces dist/cc-memory-installer.exe + dist/cc-memory-dashboard.exe
```

**Release binaries come from CI, not from this machine (v2.12.0).**
`.github/workflows/release.yml` runs on a `v*` tag push and does, in order:
refuse a tag that disagrees with `core/version.py`; `python
tests/run_gates.py` on the tagged commit (tag pushes do not trigger
`gates.yml`); `scripts/build_exe.py`; **run both exes** — the installer does
a real `--cli` install and `--uninstall` against a sandboxed `USERPROFILE`
and must refuse an unknown flag with exit 2, the dashboard is launched with
`--help` and must exit 0 (the v2.5.4 rule: run them, never PE-inspect);
`gh release create` with both exes attached and the CHANGELOG section as the
body (`scripts/release_notes.py` — fails loud if the section is missing).
Release procedure is therefore: bump the five version sites → write the
CHANGELOG entry → gates green → commit → `git tag vX.Y.Z` → `git push
origin main vX.Y.Z`. Never upload a locally built exe to a release.

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

To deploy to another machine without a git checkout, use
`cc-memory-installer.exe` from the GitHub Release (built by
`.github/workflows/release.yml` via `scripts/build_exe.py`). That installer lays the package
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
