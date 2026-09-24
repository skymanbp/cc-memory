# INVARIANTS

The rules this tree must not break, one per entry, grouped by the component
that owns them. Each entry gives the rule, the reason it exists, the gate that
turns red when it is broken, and the version that recorded it. Line numbers are
deliberately absent: an entry names symbols, and `grep -n "def <symbol>"` is
the way to find one. The narrative behind every rule — what was measured, what
the first fix got wrong — lives in `CHANGELOG.md`, in the version's own entry
and, for rules first written down in `CLAUDE.md`, under that version's
*Rules recorded in CLAUDE.md at release* heading.

An entry is added when a change records a rule a future change must not
break, in the same commit as the gate that enforces it. Number the entry after
the last one in its group's range, name the gate and the `tools/falsify_fixes.py`
case, and add the id to the version table below and beside the code comment
that cites the rule. An entry that names no gate is a wish, not an invariant,
and is marked *ungated* on purpose.

## Version → invariants

| Version | Recorded here as |
|---|---|
| v2.16.0 | INV-016, INV-017, INV-018, INV-019, INV-020, INV-028, INV-030, INV-038, INV-041, INV-042, INV-043, INV-044, INV-045, INV-049, INV-050, INV-052, INV-053, INV-054, INV-055, INV-056, INV-057, INV-058, INV-059, INV-060, INV-061, INV-062, INV-063, INV-065, INV-069, INV-081, INV-082, INV-088, INV-089, INV-094, INV-095, INV-096, INV-112, INV-121, INV-122 |
| v2.15.2 | INV-086 |
| v2.15.1 | INV-057, INV-066, INV-109 |
| v2.15.0 | INV-013, INV-051, INV-055, INV-063, INV-067, INV-068, INV-074, INV-081, INV-082, INV-106, INV-116, INV-122 |
| v2.14.1 | INV-098, INV-114, INV-117 |
| v2.14.0 | INV-003, INV-007, INV-009, INV-021, INV-022, INV-023, INV-024, INV-025, INV-026, INV-027, INV-029, INV-039, INV-040, INV-042, INV-051, INV-063, INV-064, INV-072, INV-079, INV-087, INV-090, INV-091, INV-101, INV-102, INV-111, INV-112, INV-115, INV-116 |
| v2.13.2 | INV-062, INV-118, INV-119 |
| v2.13.1 | INV-110 |
| v2.13.0 | INV-023, INV-024, INV-114 |
| v2.12.2 | INV-026, INV-052, INV-053, INV-069, INV-070, INV-111, INV-113 |
| v2.12.1 | INV-097, INV-103, INV-104 |
| v2.12.0 | INV-083, INV-085, INV-086, INV-091, INV-093, INV-103 |
| v2.11.4 | INV-109, INV-115 |
| v2.11.3 | INV-120 |
| v2.11.2 | INV-084, INV-104 |
| v2.11.1 | INV-028, INV-079, INV-080, INV-083, INV-090, INV-100, INV-105, INV-109, INV-116 |
| v2.11.0 | INV-079, INV-083, INV-088 |
| v2.10.1 | INV-098, INV-107, INV-108 |
| v2.10.0 | INV-031 |
| v2.9.0 | INV-006, INV-008, INV-011, INV-012, INV-015, INV-027, INV-031, INV-046, INV-047, INV-048, INV-101 |
| v2.8.0 | INV-002, INV-003, INV-004, INV-005, INV-006, INV-027, INV-034, INV-035, INV-037, INV-047, INV-097, INV-099, INV-108, INV-112 |
| v2.6.0 | INV-026 |
| v2.5.4 | INV-010, INV-098, INV-103, INV-111 |
| v2.5.3 | INV-010, INV-015, INV-033, INV-101 |
| v2.5.2 | INV-011, INV-012, INV-032, INV-033, INV-051, INV-073, INV-075, INV-111 |
| v2.5.0 | INV-013, INV-014, INV-032, INV-034, INV-045, INV-046, INV-047, INV-048, INV-063, INV-071, INV-074, INV-077, INV-097, INV-102, INV-105, INV-106 |
| v2.4.3 | INV-100 |
| v2.4.2 | INV-035, INV-036, INV-038, INV-105 |
| v2.4.0, v2.4.1 | INV-078 |
| v2.3.4 | INV-041, INV-092 |
| v2.3.2 | INV-091, INV-092 |
| v2.2.0 | INV-077, INV-088, INV-089 |
| v2.1.0 | INV-001, INV-031, INV-057, INV-099 |

## 1. Storage, identity and the write path

**INV-001 · Every memory write goes through `llm.memory_writer.upsert_smart` or
`upsert_batch`.** The writer MERGES, SUPERSEDES, REINFORCES or INSERTS;
`db.insert_memory` has no caller outside the writer's own `supersede_memory` and
the tests. *Why:* stacking duplicates is the failure the plugin exists to
prevent. *Gate:* `tests/smoke_test.py` § anti-patch; `falsify --case r8antipatch`.
*Source:* v2.1.0; v2.16.0 D4 (the dead direct writers were deleted).

**INV-002 · `core/textsim.py` is THE similarity substrate.** `memory_writer`,
`consolidate` and `plan` import from it; none may re-grow a private
`_trigram_set`. CJK runs shingle as BIGRAMS, everything else keeps trigrams,
and ASCII output is byte-identical to the retired copies. *Why:* a one-character
Chinese correction scored 0.4545 under `MID_SIM`, so every Chinese correction
was filed as a new fact; every tuned threshold was calibrated on the ASCII
numbers. *Gate:* `tests/smoke_test.py` asserts identity, not equality;
`falsify --case cjk`. *Source:* v2.8.0 §1.

**INV-003 · Tags are UNIONED with the surviving row's and capped.**
`_merged_tags` caps the caller-supplied union at `MAX_TAGS` and re-appends the
writer's own action tags; MERGE never replaces provenance. *Why:* MERGE wrote
`set(incoming + ["merged"])` and destroyed `["observer","realtime"]`;
`memory_add` is model-invokable and stored an unbounded list verbatim.
*Gate:* `falsify --case tags` / `tagcap` / `r14tagcap`. *Source:* v2.8.0 §2;
v2.14.0 §17.

**INV-004 · `supersede_memory` is ONE transaction, and every `id IN (...)`
writer chunks through `MemoryDB._id_chunks`.** *Why:* a kill between a separate
insert and archive left BOTH rows active; an unchunked statement raised
`too many SQL variables` past the cap. *Gate:* `falsify --case supersede` /
`sqlvars`. *Source:* v2.8.0 §3.

**INV-005 · A verdict computed from a SNAPSHOT writes through
`archive_if_unchanged`.** `cleanup_garbage`, `merge_near_duplicates` and
`archive_consolidated` read in one transaction and write in another while the
PreCompact writer runs; the `content_hash` condition turns a stale verdict into
a no-op. The blind `bulk_archive` / `delete_memories` writers are gone.
*Gate:* `falsify --case snapguard` / `hashguard`. *Source:* v2.8.0 §4; v2.16.0 D4.

**INV-006 · `supersedes_id` stays a DAG, and `archive_obsolete` COALESCEs it.**
A link that would close a cycle is refused and logged; a loser that already
carries a link keeps its FIRST lineage fact. *Why:* overwriting made the
original unreachable from every chain walk while `/cc-mem supersedes` still
said "newest first". *Gate:* `falsify --case cycle` / `r9chain`. *Source:*
v2.8.0 §5; v2.9.0 §1.

**INV-007 · An exact-hash restatement REINFORCES.** `_fold_into_hash_match`
folds importance (max) and tags (union) into the matched row, action
`reinforced`, and writes nothing when nothing is new — `skipped` still means
nothing was written. Every consumer of `upsert_batch`'s counts renders
`reinforced`. *Gate:* `tests/smoke_test.py` § C3/C4; `falsify --case r14hashfold`
/ `r14foldnoop`. *Source:* v2.14.0 §17.

**INV-008 · `patch_progress` bootstraps and patches in ONE transaction.**
`INSERT OR IGNORE` under `BEGIN IMMEDIATE`, leaning on the primary key and the
schema DEFAULTs, which are verified identical to `upsert_progress`'s defaults.
*Why:* three transactions on three connections let a stale "row absent" verdict
replay the default row over a landed patch. *Gate:* `falsify --case r9progtx`.
*Source:* v2.9.0 §2.

**INV-009 · Fill-only-empty is decided INSIDE the UPDATE, and EMPTY is not
NEVER WRITTEN.** `MemoryDB.fill_empty_progress` tests emptiness in the statement
itself; where `progress_was_fully_written` says a rewrite settled the row, the
mined work lists stay as written, empty included; tier 3 mines the CURRENT
transcript on a compact/resume start. *Gate:* `falsify --case r14fillrace` /
`r14emptytodos` / `r14curtranscript`. *Source:* v2.14.0 §12.

**INV-010 · `core/atomic.py:write_atomic` is THE artifact writer: replace
completely, or raise — never truncate, never fall back.** Derived artifacts
retry against the wall-clock budget `_DERIVED_BUDGET_S`; PROGRESS.md's writer
propagates the raise (it is the handoff contract), PLAN.md's and MEMORY.md's
keep the previous COMPLETE file. The inject manifest and the PreCompact attempt
marker go through it too. *Gate:* `tests/smoke_test.py` asserts the three old
names ARE that function and no module defines `_atomic_write*`; `falsify --case
archiveatomic`. *Source:* v2.5.3 §1; v2.5.4 §3; v2.16.0 D5.

**INV-011 · Generated artifacts are named uniquely.** Session archives carry
millisecond stems AND an `O_CREAT|O_EXCL` claim of the exact path;
`.plan_history/` likewise; `write_session_archive` derives `YYYY/MM` from the
stem it is given, never from its own clock. *Gate:* `falsify --case r7arcname` /
`r8arcorder`. *Source:* v2.5.2 §4; v2.9.0.

**INV-012 · `MemoryDB._connect` is a context manager that CLOSES, and MEMORY.md's
moved-under-us probe is `PRAGMA data_version` on a HELD connection.** *Why:* the
retired fingerprint (row counts + `MAX(id)` + `MAX(updated_at)`) was blind to an
in-place UPDATE inside one clock second. Do not "optimise" `_connect` back into a
factory: the +340 % per-operation cost was measured and accepted. *Gate:*
`tests/smoke_test.py` handle-count regression; `falsify --case r9dataver`.
*Source:* v2.5.2 §5; v2.9.0 §3.

**INV-013 · `memories_fts` is built with the trigram tokenizer, chosen by a
runtime PROBE, and an empty MATCH is never an answer.** `_retokenize_if_stale`
self-heals an index built by an earlier version; `_match_fts` routes an empty
FTS result to the LIKE fallback unconditionally. *Why:* unicode61 indexed a
whole Chinese clause as ONE token, and `mcp/server.py:_is_failed_result` counts
an empty result as SUCCESS. *Gate:* `tests/test_recall.py` §1-§2; `falsify --case
r8ftsempty` / `r7ftsprobe`. *Source:* v2.15.0 §1.

**INV-014 · `_MAX_SEARCH_LIMIT` is clamped at both ends, `LIKE ? ESCAPE '\'`,
and a bare `%` or `_` returns 0 rows.** SQLite reads `LIMIT -1` as no limit.
*Gate:* `tests/test_recall.py` §2 (eleven hostile query shapes, none dumps the
table); `falsify --case r5y5limit`. *Source:* v2.5.0 §4.

**INV-015 · `project_id` is REQUIRED and keyword-only on every plan mutator,
and every `/cc-mem` command that touches a table scopes it to the project.**
`memories.id` and `plans.id` are global to the database FILE, which
legitimately holds several projects. *Gate:* `tests/test_surfaces.py` §9 CLI
scoping; `falsify --case r9cliscope` / `r9supscope` / `planid`. *Source:*
v2.5.3 §2; v2.9.0 §4.

**INV-016 · Rows are ordered by `id`, never by a timestamp string.**
`get_recent_memories`, `list_sessions` (the ONE listing behind `/cc-mem
sessions`, the dashboard's Sessions tab and `/api/sessions`) and the
`get_recent_sessions` timeline all order on ids; `compacted_at` is a naive
local-time string. *Gate:* `tests/smoke_test.py` § D2-D7; `falsify --case
r16sessorder` / `sessionorder`. *Source:* v2.16.0 B8 and D3.

**INV-017 · The bootstrap stamp: `PRAGMA user_version` carries
`_bootstrap_stamp`, DERIVED from the schema text and the ledger's names.**
A settled open reads one pragma; the two self-healing probes
(`_detect_fts5`, `_ensure_active_hash_unique`) stay OUTSIDE the stamp because
they answer the state of the file, which a record of intent cannot vouch for.
Blind spot, recorded: a base table dropped by hand is not re-created until the
schema or ledger changes. *Gate:* `tests/smoke_test.py` § bootstrap stamp;
`falsify --case r16stamp`. *Source:* v2.16.0 A6.

**INV-018 · `MemoryDB.observer_cursor` is a PURE read, and PreCompact feeds only
the rows above it.** `pre_compact._observations_to_feed` slices above the
cursor; rows at or below it are deleted whether extraction ran or not, because
the cursor advances only after the observer's upsert returned. *Why:* every
observation reached a model twice. *Gate:* `tests/smoke_test.py` § observer
cursor; `falsify --case r16prefeed`. *Source:* v2.16.0 A4.

**INV-019 · Manual saves store `session_id` NULL; the observer's rows carry the
session `db.claim_session` reuses or inserts.** The MCP `memory_add` tool is a
manual path like the CLI, the dashboard and the web viewer. *Gate:*
`tests/test_surfaces.py` §1; `falsify --case r16mcpsession`. *Source:*
v2.16.0 B8 and D7.

**INV-020 · MEMORY.md is regenerated only after a batch that WROTE something.**
A batch of pure skips re-read the whole table for a byte-identical file on
every observer call that found nothing new; PreCompact renders once, after its
own batch. *Gate:* `tests/smoke_test.py` § A7; `falsify --case r16noregen`.
*Source:* v2.16.0 A7.

**INV-021 · `core.layout.canonical_path` is THE comparable spelling of a path.**
Resolved, then `normcase`d, never raises; a non-path spells as `""` so it can
only ever MISS. Every "are these one directory" decision goes through it or
`same_path`; never compare paths with a fresh `normcase`, `.lower()` or `==`.
*Why:* a renamed project minted a second row while its memories sat one
`project_id` away. *Gate:* `tests/smoke_test.py` § identity; `falsify --case
r12canoncase`. *Source:* v2.14.0 §1.

**INV-022 · `upsert_project` re-attaches; `find_project_id` never inserts.**
A miss re-attaches only when the database sits at `<cwd>/.ccm/memory.db`
(`layout.database_owner`) and only the most recently active row whose directory
no longer exists; a row whose directory still exists elsewhere is another live
directory's. The surfaces that ask a question (`status`, `stats`, `list`,
`sessions`, `keywords`) use `find_project_id`. *Gate:* `falsify --case
r13reattach` / `r13statuscreate`. *Source:* v2.14.0 §2.

**INV-023 · The state directory is `core/layout.MEMORY_DIRNAME` (`.ccm/`), and
nothing else spells it.** Every surface asks `memory_dir(root)` (write side) or
`find_memory_dir(root)` (read side, which NEVER migrates). Two literal copies are
permitted because they cannot import the package: `ui/installer.py` and
`skills/ccm-load/SKILL.md`. A refused rename returns the LEGACY directory,
never the new name; `roots._has_db` and `nested_databases` know both names.
*Gate:* `tests/smoke_test.py` § state directory; `falsify --case r13statelit` /
`r13statejoin` / `r13readmigrate` / `r13hasdbboth` / `r13skilldir`. *Source:*
v2.13.0 §1-§6; v2.14.0 §4.

**INV-024 · Identification is TRI-STATE, never name-matching, and only a POSITIVE
licenses the irreversible move.** `layout.is_ccm_dir` recognises this plugin's
`.gitignore` marker line or a real SQLite file with this schema's tables (the
magic-byte pre-filter is defence in depth); `core.layout.UNKNOWN` (falsy) is
what a probe returns when it could not RUN; `migrate_legacy_dir` alone asks
`is UNKNOWN`; a linked `.ccm` (`_is_usable_state_dir`, junctions included) is
never followed and never renamed onto. *Gate:* `tests/smoke_test.py` § roots
(c)(d); `tests/test_surfaces.py` §7; `falsify --case r13ccmident` / `r14probe3`
/ `r14emptyccm` / `r14linkdir` / `r14findlink` / `r14linkrecover`. *Source:*
v2.13.0 §4; v2.14.0 §10.

**INV-025 · A handle follows the migration, one way.** `MemoryDB._connect`
retries ONCE through `MemoryDB._follow_state_dir`, from `memory/` to `.ccm/`
only, only when the new file exists and passes the link refusal, and only after
a connect actually failed. Nothing but the migration may join the legacy name.
*Gate:* `tests/smoke_test.py` § identity (j); `falsify --case r13handlefollow`.
*Source:* v2.14.0 §5.

**INV-026 · In `core/roots.py` a declaration beats a name, and a volume root is
a boundary in every spelling.** `.ccm-root` is consulted ONCE, in `_candidates`;
`_dependency_cut` spares a directory that owns a database (the database wins at
every depth); `_is_volume_root` recognises `/mnt/c`, `/cygdrive/c`, `/host_mnt/c`
and `/c` beside `C:\` and `/`; `_home_dirs` carries every spelling RESOLVED.
A directory that already owns a `.ccm/memory.db` is never re-rooted, and a
container of projects is never returned. `_is_container`'s NEGATIVE verdict is
bounded by `_CONTAINER_SCAN_CAP`. *Gate:* `tests/test_surfaces.py` §7 (the
ladder over a real filesystem); `falsify --case r14a1b` / `r14depcut` /
`r14depdb` / `r12scancap` / `r5y1roots`. *Source:* v2.6.0; v2.12.2 §4;
v2.14.0 §1 and §11.

**INV-027 · Markers refuse links, hash the WHOLE session id, and never land
inside the user's tree.** `core.markers.safe_id` hashes the whole id (a
truncating `[:16]` cross-wired sessions sharing a prefix); both marker paths
refuse a symlink through `os.lstat` (`O_NOFOLLOW` is 0 on Windows);
`core.markers._is_link` is THE link probe, junctions included, for every
fail-closed guard; `marker_dir()` returns None rather than a directory inside
the cwd (`_is_cwd` is equality, not containment), and `marker_path` propagates
the None into `read_marker` / `write_marker`, which refuse it. *Gate:*
`tests/smoke_test.py` § markers; `falsify --case symlink` / `r7junc` /
`r9junction` / `r14markercwd` / `r14markernone`. *Source:* v2.8.0 §7; v2.9.0
§7; v2.14.0 §20.

**INV-028 · `core.markers.write_marker` never raises.** Its three failure paths
`return False`, so a caller must read the return value; an `except OSError`
around it is dead code. *Why:* the escape budget could never release because
its persistence failure was invisible. *Gate:* `tests/test_directive_enforcement.py`
§5; `falsify --case r11budget`. *Source:* v2.11.1 §1; v2.16.0 D4.

**INV-029 · A gate copy is a git repository, and the temp directory is never
the cwd.** `tempfile.gettempdir()` falls back to `os.getcwd()` under a hook, so
a base that IS the cwd or is not a designated temp root is refused;
`tools/falsify_fixes.py` runs each gate once on an UNTOUCHED copy
(`gate_baseline`) and reports UNSOUND, not RED, when that baseline is red.
*Gate:* `falsify --case r14baseline`. *Source:* v2.14.0 §20.

**INV-030 · A constant is spelled once.** `core.markers.TURN_MARKER_PREFIX` /
`PROMPT_MARKER_PREFIX`, `core.recall.RECALL_MANIFEST`,
`core.extractor._DEFAULT_TAIL_BYTES`, `core.db.CRITICAL_IMPORTANCE`,
`core.plan.GUARDIAN_TURN_THRESHOLD` / `GUARDIAN_EDIT_THRESHOLD` /
`DIRECTIVE_IDLE_TURNS`, `core.textsim.HIGH_SIM` / `MID_SIM`,
`core.consolidate.CONSOLIDATION_LOCK` / `STALE_LOCK_S`,
`core.modes.HOOK_TOOL_MATCHER`, `core.plan.BLOCK_MARKER_PREFIX`. A prefix whose
writer is gone stays registered in `ui/installer.py:_TEMP_MARKER_PREFIXES` so
an uninstall still sweeps what older installs wrote. *Gate:* `tests/smoke_test.py`
§ D2-D7 greps for a second spelling. *Source:* v2.16.0 C2, C3, D3.

## 2. Hook entry and safety

**INV-031 · A hook never writes to stderr, never raises, and always exits 0;
`hooks/_entry.py` is THE entry ladder.** Every hook parses stdin through
`parse_payload` (read to EOF — a prefix cap truncated a payload mid-JSON and
dropped the whole event) and routes cwd through `resolve_project`, which owns
the order `is_excluded` on the RAW cwd THEN `project_root`. No hook imports
`is_excluded` or `project_root` directly; field POLICIES stay per hook. Logging
goes through `core.logger`. *Gate:* `tests/test_surfaces.py` §4 and §7;
`falsify --case r10entryorder` / `r7stderr` / `r9bigstdin`. *Source:* v2.1.0;
v2.9.0 §8; v2.10.0 §1.

**INV-032 · `core.modes.is_excluded` is consulted by every surface that can open
a project, hooks and hand-run tools alike.** `python tools/contracts.py` prints
which; the MCP server's `_get_db` is the single choke point every model-initiated
call reaches, so no MCP handler opens a database path itself. *Gate:*
`tests/test_surfaces.py` §4-§5; `falsify --case r7mcpscope`. *Source:* v2.5.0
§9; v2.5.2 §2.

**INV-033 · `core.modes.read_config` is THE runtime reader of `config.json`, reads
`utf-8-sig`, and fails CLOSED but VISIBLE.** A file that exists and cannot be
used excludes every project, and `config_fault()` reports why through one
SessionStart line; a project the user genuinely LISTED stays completely
silent; `_norm_path` cannot raise. *Gate:* `tests/test_surfaces.py` §5;
`falsify --case r6failopen`. *Source:* v2.5.2 §3; v2.5.3 §3.

**INV-034 · Every LLM-calling hook passes an absolute `deadline`, and
`call_llm`'s deadline is TRUE wall-clock.** Clamping the socket timeout bounds
only the idle gap; `_abort_response` cuts the socket rather than draining the
body. `stop.py:_LLM_DEADLINE_S`, `session_start.py:_RETRO_DEADLINE_S` and the
PreCompact envelope are the budgets. *Gate:* `tests/test_surfaces.py` source-level
rule; `falsify --case deadline`. *Source:* v2.5.0 §7; v2.8.0 §8.

**INV-035 · Transcript reads from a hook are BOUNDED.**
`core.extractor.load_transcript_window` reads a head+tail window (40 records +
32 MiB); summaries fill from the NEWEST record backwards; the unbounded
`load_transcript` is for the dashboard only. Both JSONL loaders drop non-record
lines. *Gate:* `tests/smoke_test.py` bounded-window / summary-direction;
`falsify --case nonrecord`. *Source:* v2.4.2 §1-§2; v2.8.0 §6.

**INV-036 · Killed runs are visible.** `.ccm/.pre_compact_attempt.json` is
written at entry and removed only on completion; `.last_save.json` carries
`trigger`. *Why:* a timeout kill runs no `except` block. *Gate:*
`tests/smoke_test.py` killed-run visibility. *Source:* v2.4.2 §3.

**INV-037 · `pre_compact` COERCES `trigger` and `session_id` and exits early
only on `cwd` and `transcript_path`.** The first two are annotation; abandoning
a compaction over them costs the handoff. *Gate:* `tests/test_surfaces.py` §8
annotation guard; `falsify --case trigger`. *Source:* v2.8.0 §9.

**INV-038 · `RuntimeError` is in every extraction `except` tuple.** `call_llm`
folds every failed leg into ONE RuntimeError; a total outage must not skip the
PROGRESS.md rewrite or escape the observer. *Gate:*
`tests/test_directive_enforcement.py` §12. *Source:* v2.4.2 §4; v2.16.0.

**INV-039 · Never call `Path.home()` bare on a hook path.**
`core/auth._credentials_path` returns None when no home resolves, so an explicit
`ANTHROPIC_API_KEY` is returned instead of discarded with the exception.
*Gate:* `tests/smoke_test.py` § auth; `falsify --case r13authhome` / `r13home`.
*Source:* v2.14.0 §8.

**INV-040 · The CLI boundary catches the CLASS external input can raise and keys
a remedy on a MEASURED message.** `(OSError, sqlite3.Error, UnicodeError,
OverflowError, JSONDecodeError)`, never a bare `ValueError`; a remedy is printed
only for the sqlite messages driven first-party (`_SQLITE_ENV_FAULTS`); ids and
counts are bounded at argparse (`_row_id`, `_bounded_limit`); stdin loses its
BOM. *Gate:* `tests/test_surfaces.py` §9h-j; `falsify --case r14cliboundary` /
`r14sqlremedy` / `r14rowid` / `r14stdinbom`. *Source:* v2.14.0 §18.

**INV-041 · `core.auth.get_api_key()` is the only credential resolver, and a
failed call is remembered.** `get_api_candidates()` orders env key then OAuth
token, `call_llm` uses the right wire per credential; `.ccm/.llm_backoff.json`
is written by `note_llm_failure` alone (one minute after the first failure,
doubling to a 30-minute ceiling), read by `llm_backoff`, forgotten by the first
success (`clear_llm_backoff`). A parsed-but-useless answer is a bad answer, not
an outage. *Gate:* `tests/test_directive_enforcement.py` §12; `falsify --case
r16obsbackoff`. *Source:* v2.3.4; v2.16.0.

**INV-042 · The observer and the retroactive save are DETACHED workers; the hook
only DECIDES.** `stop._maybe_spawn_observer` (a credential, at least three
observations above the cursor, no backoff, no live `.observer.lock`) and
`session_start._maybe_spawn_retro` (`_retro_candidates` by `stat()` alone; never
on a resume or fork start) spawn `--observe` / `--retro` through the one recipe
`hooks/_entry.py:spawn_detached`, which the backpressure kick uses too. Each
worker takes its lock through `consolidate_async._acquire_lock`, THE lock
policy point (`stale_s`), and advances the cursor only after a call that came
back. *Gate:* `tests/test_directive_enforcement.py` §13;
`tests/smoke_test.py` § A2; `falsify --case r16obslock` / `r16retrospawn` /
`r14stalelock`. *Source:* v2.16.0 A1-A2; v2.14.0 §15.

**INV-043 · A continuation Stop is the same turn, judged again.**
`stop_hook_active` skips the observer, the idle reorg, the PROGRESS patch, the
backpressure probe and the plan turn bump; enforcement still runs, so the escape
budget counts down and a resolved condition releases. *Gate:*
`tests/test_directive_enforcement.py` §11; `falsify --case r16continuation`.
*Source:* v2.16.0 A3.

**INV-044 · Stop opens ONE `MemoryDB` handle per turn and hands it to
`_observer_evaluate(db=)` and `maybe_run_idle(db=)`.** Every construction pays
the bootstrap probes. *Gate:* `tests/smoke_test.py` § one handle; `falsify
--case r16onehandle`. *Source:* v2.16.0 A6.

**INV-045 · PostToolUse is bound by matcher, and `core.modes.HOOK_TOOL_MATCHER`
is the ONE spelling.** `hook_tool_matcher()` derives an anchored alternation from
every mode's `observe_tools` plus `PLAN_CONTROL_TOOLS`, `EDIT_TOOLS` and
`SENSITIVE_TOOLS` (which the hook and `core.plan.is_sensitive_tool_call` read);
`hooks/hooks.json` declares it and `ui/installer.py:HOOK_MATCHERS` carries it
for the flat install. The plan legs run in EVERY mode, above `should_observe`.
*Gate:* `tests/smoke_test.py` § A5; `falsify --case r16matcher` /
`r16matcherfrozen` / `r8planhook`. *Source:* v2.5.0 §2; v2.16.0 A5.

**INV-046 · MCP is correct on the wire and enforced at the schema.** UTF-8 + LF
forced on both handles; exactly one frame per parsed id; `jsonrpc == "2.0"` or
`-32600`; `tools/call` arguments validated against the advertised `inputSchema`
and refused with `-32602`; nothing escapes `main()`; frames are length-capped.
*Gate:* `tests/test_surfaces.py` §1; `falsify --case r9jsonrpc` / `r7mcpanchor`.
*Source:* v2.5.0 §4; v2.9.0 §10.

**INV-047 · The web viewer is loopback-only, origin-checked, and bounded by wall
clock.** `ThreadingHTTPServer` with per-connection timeouts; no
`Access-Control-Allow-Origin`; `Origin`, `Host` and `Content-Type` enforced; the
header phase bounded by `_HEADER_DEADLINE_S`; `_MAX_CONCURRENT` admission with
a drained 503. *Gate:* `tests/test_surfaces.py` §2 and §9; `falsify --case shed`
/ `r9hdrdead`. *Source:* v2.5.0 §5; v2.8.0; v2.9.0 §9.

**INV-048 · Every hook's stdin is read to EOF, and the PostToolUse `is_private`
classification runs on the RAW input.** `_truncate_output` turns a Read body
into `"(file content)"`, so classifying after it shipped a private path to the
API. *Gate:* `falsify --case r9bigstdin` / `r7dirpriv`. *Source:* v2.5.0 §3;
v2.9.0 §8.

**INV-049 · Stop's stdout is EMPTY on a turn that may close, and PreCompact's
always is.** Claude Code shows only SessionStart and UserPromptSubmit stdout to
the model, so a status line printed anywhere else goes to the log; a refusal
writes the `{"decision": "block"}` document and nothing else, and the advisory a
spent escape budget degrades to is PARKED on the block marker for the next
UserPromptSubmit, which is a channel the model reads. *Gate:*
`tests/test_directive_enforcement.py` §9(b); `tests/test_surfaces.py`;
`falsify --case r16advisorychannel` / `r7stdout`. *Source:* v2.16.0 D8.

**INV-050 · Extraction has ONE prompt builder and ONE normaliser.**
`llm.parse.build_extraction_prompt(kind, body, mode_suffix=)` and
`normalize_memories(items)` serve the observer, PreCompact, the retroactive save
and the dashboard; the mode's `extraction_prompt_suffix` reaches the prompt
through `core.modes.get_extraction_suffix`. *Gate:* `tests/smoke_test.py`
§ D2-D7; `falsify --case r16suffix` / `r16normfloor`. *Source:* v2.16.0 D2.

## 3. Injection and rendering

**INV-051 · Stored content is NEVER interpolated raw into anything Claude
reads.** `core.privacy.neutralize_markers` (escape, never delete) runs on the
write path via `clean_for_storage` AND again on every render path —
`neutralize_inline` for one-line slots, `neutralize_block` for slots whose
newlines are structure; `python tools/contracts.py` lists the render paths.
The refusal's `[key]` / `what` / `fix` slots and the parked advisory are inline
slots; `cc-memory-recall` is registered in `_MARKER_TAG_RE` with every other
frame the plugin emits. *Why:* a memory row forged a complete
`<system-reminder>` block, and `memory_add` is model-invokable. *Gate:*
`tests/smoke_test.py` forgery resistance; `tests/test_recall.py` §3e;
`falsify --case r7render` / `r14advisoryslug` / `r14blockinline` /
`r15recallframe`. *Source:* v2.5.2 §1; v2.11.1 §2; v2.14.0 §14; v2.15.0 §4.

**INV-052 · The directive ledger is the FIRST layer of the SessionStart
injection and a section of PLAN.md, also when there is no plan, drawn by one
function.** `core.plan.render_directive_lines(rows, style)` serves both
`session_start._build_directives_layer` and `plan._render_directives_section`;
constraints first, then most-repeated first; an over-budget row is skipped
(`_LAYER_SKIP_NOTE`), never the layer. *Gate:*
`tests/test_directive_enforcement.py` §7; `falsify --case r12directiveinject` /
`r12directiveplan`. *Source:* v2.12.2 §1; v2.16.0 B5.

**INV-053 · The injection is budgeted, and the shares sum to one.**
`session_start._DEFAULT_BUDGET` is 16 000 characters; `_LAYER_BUDGETS` divides it
directives 0.10 / topics 0.25 / critical 0.20 / timeline 0.20 / progress 0.15 /
footer 0.10. *Gate:* `tests/smoke_test.py` § B1; `falsify --case layerbreak` /
`footerbudget`. *Source:* v2.12.2; v2.16.0 B1.

**INV-054 · The PROGRESS layer is a §1-§4 digest of the row, not the file.**
`core.progress.render_progress_digest` draws request, status, open todos
(capped, and the cap announces itself) and the plan summary with the file's own
slot renderers (`_render_request_lines` / `_render_status_lines` /
`_render_todo_lines`), so the two cannot disagree; the file preview is only for
a project with a file and no row. *Gate:* `tests/smoke_test.py` § B1;
`falsify --case r16digest`. *Source:* v2.16.0 B1.

**INV-055 · The handshake demands PROGRESS.md and nothing else.**
`_build_forced_reminder` emits ONE numbered Read line; no PROGRESS.md means no
block; `demand_ack=False` keeps the RESUME PROTOCOL and drops the ack sentence.
`core.progress.ACK_TEMPLATE` is the one string that EMITS the ack and
`ack_present` the one detector; the measurement is tri-state and `unmeasured`
is never printed as `no`. *Gate:* `tests/smoke_test.py` § B3 and § ack;
`falsify --case r16memoryread` / `r15ackspell` / `r15acktemplate` /
`r15acktristate`. *Source:* v2.15.0 §3; v2.16.0 B3.

**INV-056 · The injection is shaped by `source`.** `_injection_mode`: `startup`
and `clear` build the full layered context; `resume` and `fork` restate the
directive ledger and nothing else, with no manifest write and no injection-count
bump; `compact` rebuilds every layer and demands no ack. The manifest records
`source` and `ack_demanded`, and `cli/mem.py:_ack_signal` answers `unmeasured`
when no ack was demanded. *Gate:* `tests/smoke_test.py` § B4/B9; `falsify --case
r16resume` / `r16ackdemand`. *Source:* v2.16.0 B4.

**INV-057 · `.ccm/PROGRESS.md` is ALWAYS full-rewritten from the `progress` row,
never appended, and `.ccm/PLAN.md` from `plan_active`.** PROGRESS.md §4 reads
`get_plan_active` and §5 reads the memories store through
`_render_critical_lines` at `CRITICAL_IMPORTANCE`; the `critical_context` column
is retired and stays empty; `**Blocked**` renders only when set. *Gate:*
`tests/test_plan_carryover.py` §8; `tests/smoke_test.py` § B9 and § D2-D7;
`falsify --case r16critstore` / `r16blockedline` / `r16critical4`. *Source:*
v2.1.0; v2.15.1 §1; v2.16.0 B9 and D6.

**INV-058 · ONE seen set.** The inject manifest records `shown_ids` (the rows the
model saw) and `topic_covered_ids` (a critical row dropped because its topic
already rendered — recorded, NOT fed to recall); `build_context` hands
`shown_ids | covered` to the timeline so Recent never re-lists a fact the topic
layer carries; `user_prompt._already_shown` reads `shown_ids` and skips a
manifest whose `session_id` belongs to another session. *Gate:*
`tests/smoke_test.py` § B2; `tests/test_recall.py` §3i-§3j; `falsify --case
r16seenset` / `r16recallscope`. *Source:* v2.16.0 B2.

**INV-059 · A topic's fallback summary is one line and does NOT cover its
critical rows.** `_summarize_topic_fallback` emits `FALLBACK_SUMMARY_PREFIX` and
a bounded line; a topic carrying a fallback summary lets its critical rows
render in the Critical layer. *Gate:* `falsify --case r16fallbackcover`.
*Source:* v2.16.0 B6.

**INV-060 · `core/prompts.py` is the one home of Claude-visible text.**
`RESUME_TRIGGERS`, the handshake sentences, the recall frame and the
generated-document notices are spelled there and nowhere else; the module is
registered in the installer manifest, `scripts/build_exe.py`,
`cli/mem.py:_REQUIRED_PLUGIN_FILES` and both FLAT diagrams. *Gate:*
`tests/smoke_test.py` derives the required-file list from the hooks' import
graph. *Source:* v2.16.0 B5-B7.

**INV-061 · A Read of PROGRESS.md, MEMORY.md or PLAN.md is the handshake, not an
observation.** `extractor.is_handshake_read` filters it from the observer feed,
the PreCompact feed and `files_from_observations`. *Gate:* `falsify --case
r16handshake`. *Source:* v2.16.0 B10.

**INV-062 · MEMORY.md's maintainer notice is short, and its index links name the
directory it was handed.** `memory_writer._render_memory_index` never spells a
directory name of its own. *Gate:* `tests/smoke_test.py` § a6 takes the name
from the fixture; `falsify --case r13renderdir`. *Source:* v2.13.2; v2.16.0 B7.

**INV-063 · The retroactive save decides by `stat()` alone, and an empty
extraction is a RESULT.** `_retroactive_extract` returns `[]` when the model
found nothing and `None` only when it did not run; `retroactive_save` records
the session either way and resolves the credential above the loop.
`core.extractor.find_transcript_dir` is THE slug ladder, `mangle_project_path`
the convention, and a miss is "no transcript", never a fuzzy guess;
`_transcript_belongs_to` is fail-closed and demands positive `cwd` proof.
*Gate:* `tests/smoke_test.py` § A2 and § ack (f); `falsify --case r14retroempty`
/ `r14retrokey` / `r15ladder`. *Source:* v2.5.0 §1; v2.14.0 §13; v2.15.0;
v2.16.0 A2.

**INV-064 · `user_prompt.strip_scaffolding` is THE scaffolding predicate for
both `current_request` ingresses, and the seed happens once per session.** The
live hook and `pre_compact._first_user_request` ask the one function; the seed
fires on the first NON-scaffolding prompt, recorded by a `cc_mem_seeded_`
marker, never by the prompt marker being empty. *Gate:*
`tests/test_surfaces.py` §7; `falsify --case r14slashseed` / `r14seedturn1` /
`r14seedprev`. *Source:* v2.14.0 §16.

**INV-065 · A todo list is the LAST TodoWrite snapshot, and a plan is not a
todo list.** PreCompact stores the latest snapshot only, with no fall-back to
extracted todos; SessionStart never splits `next_steps` into open todos, because
the RESUME PROTOCOL executes `todos[0]`. *Gate:* `tests/smoke_test.py` § D2-D7.
*Source:* v2.16.0 D6.

**INV-066 · A generated document reads the STORE, not the column the store
replaced.** `core/progress.py:_render_plan_section` renders §4 from
`get_plan_active`; a free-text column with no writer is not a source. *Gate:*
`tests/test_plan_carryover.py` §8. *Source:* v2.15.1 §1.

**INV-067 · Query-time recall is CONSERVATIVE and emits ZERO BYTES when nothing
clears the bar.** `core/recall.py` retrieves with FTS5 BM25 plus the CJK-aware
overlap coefficient; the floor is `RECALL_MIN_RELEVANCE` on an OVERLAP
coefficient (never `textsim.jaccard`, whose union denominator scores a
contained query near zero); a multi-word FTS5 query is reduced to OR-ed terms
with CJK terms as 3-character windows; `is_query_like` is THREE tests; the
block is a render path. `memories.recall_count` is not a second meaning for
`last_referenced_at`. *Gate:* `tests/test_recall.py` §3 (every zero-bytes
assertion paired with a control that EMITS); `falsify --case r15recallchannel`
/ `r15recallfloor` / `r15recallgate` / `r15recallcjk` / `r15recalland` /
`r15recallprivate` / `r15recallcount`. *Source:* v2.15.0 §4.

**INV-068 · `/cc-mem inject-usage` measures DELIVERY by default and USE only on
`--judge`.** Layer 1 is deterministic and free; layer 2
(`llm/usage_judge.py:judge_usage`) is billed, tri-state, and `unknown` is never
rendered as `unused`; `strip_private` runs on the rows and the replies alike;
`call_llm` is an ARGUMENT so the gate drives every branch with no network;
`cli/mem.py:_session_window` is the one resolver of "which session, which
transcript". *Gate:* `tests/smoke_test.py` § judge; `falsify --case
r15judgedefault` / `r15judgetristate` / `r15judgeforge`. *Source:* v2.15.0 §3.

**INV-069 · `/cc-mem inject-show` says which template, which layer and which
ids reached the model.** The manifest records `directive_slugs`, `progress_layer`,
`shown_ids`, `topic_covered_ids`, `source` and `ack_demanded`. *Gate:*
`tests/smoke_test.py` § B1/B2/B4. *Source:* v2.12.2; v2.16.0 B1-B4.

**INV-070 · A gate that only runs on clean machines measures clean machines.**
`tests/test_surfaces.py` §7 counts `_is_container`'s probes rather than timing
them, because the reporting machine's temp directory holds thousands of
subdirectories and CI's runners hold none. *Source:* v2.12.2 §4.

## 4. Privacy

**INV-071 · `core/privacy.py` fails CLOSED.** `strip_private` is a single
left-to-right scan (`_strip_spans`) with no cap; a dangling open tag drops the
remainder. *Why:* the regex-plus-cap version returned the text UNCHANGED past
100 tags, into the API call and the table. *Gate:* `tests/smoke_test.py`
privacy block; `falsify --case r6quadratic`. *Source:* v2.5.0 §3.

**INV-072 · Span tags match case-insensitively, through `privacy._token_re`.**
`has_private` (the `is_private` classifier) uses the same regex; never test for
a tag with `in` or `str.find`. *Gate:* `falsify --case r13privatecase`.
*Source:* v2.14.0 §6.

**INV-073 · `<private>` is honoured on BOTH progress ingresses, before the
500-character cut.** `hooks/user_prompt.py` and
`hooks/pre_compact.py:_first_user_request` clean before they cut, so a span
straddling the cut stays a matched pair. PROGRESS.md is not in the state
directory's `.gitignore`, so a leak there is a leak into the repository.
*Gate:* `tests/smoke_test.py`; `falsify --case r9emptypr`. *Source:* v2.5.2 §6.

**INV-074 · `is_private` is classified on the raw tool input and response, and
a private row never reaches an Anthropic request.** The judge and the observer
run `strip_private` on what they send. *Gate:* `falsify --case r7dirpriv`.
*Source:* v2.5.0 §3; v2.15.0 §3.

**INV-075 · `core/consolidate.py`'s `^</?(ide_opened_file|system-reminder|antml)`
pattern is garbage cleanup, NOT the marker defence.** It is anchored at position
0 and one leading word evades it; INV-051 is the defence. *Source:* v2.5.2 §1.

**INV-076 · No token or app password is ever hard-coded, written to a file or
printed.** `core.auth` reads credentials at call time; the backoff record holds
timestamps only. *Ungated by design* (a gate would have to spell the secret).

## 5. Plan and directives

**INV-077 · Plan control is not observation.** `hooks/post_tool_use.py:
_apply_plan_integration` (ExitPlanMode capture, TodoWrite step sync, the drift
counters) runs ABOVE the `should_observe` gate in every mode; `TodoWrite` is in
every mode's `skip_tools` and `ExitPlanMode` in no mode's `observe_tools`, so
moving the block below the gate kills the anchor through its own hook. A raw
plan awaiting refinement leads PLAN.md and `plan-status` with a PENDING
REFINEMENT banner (`core.plan.raw_pending_refinement`). *Gate:*
`tests/test_surfaces.py` §8 drives the hook in every mode; `falsify --case
r8planhook` / `r6rawguard`. *Source:* v2.2.0; v2.5.0 §2.

**INV-078 · The carryover gate has no force flag.** Replacing `plan_active`
requires each unfinished step to be auto-carried (trigram-Jaccard at or above
0.5, bare titles included) or explicitly dispositioned; `plan-clear` refuses
without `--reason`; every outgoing plan is archived by `archive_plan` to
`.ccm/.plan_history/`. *Gate:* `tests/test_plan_carryover.py`; `falsify --case
dispreuse` / `donebar`. *Source:* v2.4.0; v2.4.1.

**INV-079 · Stop enforcement refuses with a guaranteed escape.**
`core.plan.blocking_reasons` returns the conditions; `stop._emit_block` writes
`{"decision": "block", "reason": ...}` as the ONLY stdout of a refused turn;
after `_BLOCK_MAX_CONSECUTIVE` refusals of the same condition set (`_block_attempt`
keys the counter by a digest of the keys) it degrades to a loud advisory; the
budget is per EPISODE (`stop._block_reset` clears the marker on every Stop that
may close); `CC_MEMORY_PLAN_ENFORCE=0` is the kill switch; a project with no plan
row is never enforced. *Gate:* `tests/test_directive_enforcement.py` §5 and §8;
`falsify --case r11budget` / `r11blockmarker` / `r13budgetreset`. *Source:*
v2.11.0 §1; v2.11.1 §1 and §3; v2.14.0 §7.

**INV-080 · `core.plan.is_live_plan` is the named predicate.** `clear_plan_active`
keeps a tombstone so `revision` stays monotonic; a hook that tests the row's
truthiness enforces a cleared plan forever. *Gate:* `falsify --case r11tombstone`.
*Source:* v2.11.1 §4.

**INV-081 · `core.plan.guardian_verdict` is THE guardian policy point.**
`blocking_reasons` and `/cc-mem plan-status` both read it, with
`GUARDIAN_TURN_THRESHOLD` and `GUARDIAN_EDIT_THRESHOLD` as its defaults; two
readers with private interpretations of one row is the defect. *Gate:*
`tests/test_directive_enforcement.py` §10(a)(d); `falsify --case
r15statusverdict`. *Source:* v2.15.0 §6; v2.16.0 D3.

**INV-082 · The drift remedy CONVERGES: the guardian runs FIRST and is recorded
LAST.** `plan_active.guardian_checked_at_turn` grants immunity for EXACTLY the
turn the check happened in; the refusal text, `/cc-mem plan-check` and its
output all state the order. *Gate:* `tests/test_directive_enforcement.py` §10;
`falsify --case r15driftconverge` / `r15driftescape` / `r15remedyorder`.
*Source:* v2.15.0 §5; v2.16.0 D5.

**INV-083 · The directive ledger is NOT plan steps, and only `directive-add`
may bump `times_stated`.** A step is a unit of EXECUTION and dies with its plan;
a directive is a unit of INTENT; `db.edit_directive` corrects fields without
touching the count and REFUSES to create; `directive-close` refuses without
`--evidence`; `source` mirrors Scanned/Manual so a rescan never rewrites a row
authored from what the user said; re-stating a directive never erases its
`demand` or `quote`; `upsert_directive` takes `BEGIN IMMEDIATE`. *Gate:*
`tests/test_directive_enforcement.py`; `falsify --case r12nobump` /
`r11restate` / `r11directiverace`. *Source:* v2.11.0 §2-§3; v2.11.1 §6;
v2.12.0 §3.

**INV-084 · Directive idleness is measured on a MONOTONIC clock:
`turns_total - turns_at_touch`.** Both are stamped inside `upsert_directive` and
`set_directive_status`'s own transaction, never by a caller; never re-point it
at a resettable counter. *Gate:* `falsify --case r11resetforgives` / `r11idle`.
*Source:* v2.11.2 §1.

**INV-085 · Idle enforcement skips `status='blocked'` and `kind='constraint'`,
and the constraint skip lives in `core.plan.blocking_reasons` ONLY.** *Gate:*
`falsify --case r12constraint`. *Source:* v2.12.0 §4.

**INV-086 · Directives reference plan steps by TITLE, never by number.**
`core.plan.stale_directive_step_refs` audits active directives on every
`plan-set --from-refiner`, advisory by design; `_STEP_REF_RE` bounds a step id at
three digits on BOTH sides, so `#437832` is an issue number, not step 437.
*Gate:* `tests/test_directive_enforcement.py` § (f); `falsify --case r12stepref`
/ `steprefdigits`. *Source:* v2.12.0 §5; v2.15.2.

**INV-087 · `plan-set --from-refiner` judges the plan it WROTE.** The carryover
gate reads the normalised `result`, not the raw payload; `goal` and `context`
are TEXT by one rule in `normalize_structured`. *Gate:* `falsify --case
r14criteriacore` / `r14criteriaraw` / `r14goalrepr`. *Source:* v2.14.0 §18.

**INV-088 · A sensitive Bash call bumps the drift counter by 20, and the next
Stop REFUSES the turn.** `core.plan.is_sensitive_tool_call` reads
`SENSITIVE_TOOLS`; nothing in the tree still says "flags". *Gate:*
`tests/test_surfaces.py` §8; `falsify --case sensitive`. *Source:* v2.2.0;
v2.11.0; v2.16.0 D5.

**INV-089 · Hooks never spawn subagents; the parked advisory rides
`core.plan.BLOCK_MARKER_PREFIX`.** `stop._note_advisory` writes one spelling;
UserPromptSubmit prints it neutralised and clears it. *Gate:*
`tests/test_directive_enforcement.py` §9. *Source:* v2.2.0; v2.16.0 D8.

**INV-090 · Every refusal slot is a render path.** `render_block_reason` runs
`neutralize_inline` on `[key]`, `what` and `fix`; a stored directive slug reached
the model as a live authority marker before this. See INV-051. *Source:*
v2.11.1 §2; v2.14.0 §14.

## 6. Consolidation and the workers

**INV-091 · Consolidation is OFF the blocking compaction path and has a
BACKPRESSURE trigger with ONE marker writer.** The async leg
`hooks/consolidate_async.py` runs under the interval marker and the lock;
`core.consolidate.consolidation_backlog` measures the backlog against the
marker's `last_memory_id` (`BACKLOG_ROWS`, or `BACKLOG_DAYS` with at least ten
new rows); the Stop probe spawns the worker DETACHED under the
`_CONSOLIDATE_KICK_COOLDOWN_S` cooldown, which fails CLOSED; `read_/
write_consolidation_marker` are the marker's only I/O, shared by the hook and
`/cc-mem consolidate`; the marker follows the ROW by `project_id`, with a
canonical-path fallback for unstamped markers. *Gate:* `tests/smoke_test.py`
§ async consolidation; `falsify --case r12backlogrows` / `r13markerid` /
`r13markerpath` / `r13markersame`. *Source:* v2.3.2; v2.12.0 §1; v2.14.0 §3.

**INV-092 · The budget cost model is honest.** `_worst_call_cost` is
`2*haiku + fallback`; `consolidate_topics` is budget-gated; `BudgetGate`
guarantees the run finishes by `total_s - safety_s`, inside the async timeout,
so a worker is never killed mid-write. *Source:* v2.3.2; v2.3.4.

**INV-093 · `deep_dedup` converges because judged groups are REMEMBERED.**
`skip_signatures` records a group even when the judge errors; nomination
over-fetches past the seen set; both the round cap and budget exhaustion are
announced. *Gate:* `tests/smoke_test.py` § C1-C4. *Source:* v2.12.0 §2.

**INV-094 · A run without a credential SAYS which stages it skipped.**
`run_consolidation` records `llm_stages` (semantic_dedup / obsolescence / topic
summaries, each `ran:N` or `skipped:<why>`) in the marker; the SessionStart
footer names every disabled stage and `/cc-mem status` prints the last run's
stages, the backlog and whether a run is due. *Gate:* `falsify --case
r16llmvisible`. *Source:* v2.16.0 C1.

**INV-095 · MERGE and nomination cross categories at `HIGH_SIM`.**
`reconcile_upsert` scans OTHER categories at `HIGH_SIM` after the
category-scoped pick, `merge_near_duplicates` allows a cross-category pair at
`HIGH_SIM`, and `_nominate_groups` hands a HIGH-band cross-category pair to the
judge (`cross_floor`). *Why:* a decision restated as a note was INSERTED beside
the fact it restates. *Gate:* `falsify --case r16crosscat` / `r16writercross` /
`r16nominatecross`. *Source:* v2.16.0 C2.

**INV-096 · The idle reorg defers to a live consolidation lock.**
`maybe_run_idle` returns while a worker's lock is younger than `STALE_LOCK_S`
(`consolidation_lock_age`), so it never archives rows the judge is re-reading
across a network round-trip. *Gate:* `falsify --case r16idlelock`. *Source:*
v2.16.0 C3.

**INV-097 · `/cc-mem archive` is the user-facing retirement path, `/cc-mem sql`
is genuinely read-only, and the dashboard's console names any non-`SELECT`
statement before running it.** `core.db._readonly_uri` is a pure function
asserted for the POSIX, drive and UNC path shapes on EVERY platform. *Gate:*
`tests/test_surfaces.py` §8; `falsify --case r12posixuri` / `r14sqlrows`.
*Source:* v2.5.0; v2.8.0 §10; v2.12.1 §3.

**INV-098 · Every gate copy and every test sandbox is a `tempfile` directory
that is torn down in a `finally`, on the failure path too.** All suites
redirect `USERPROFILE`/`HOME` and `TMPDIR`/`TEMP`/`TMP` before importing the
package and assert `Path.home()` moved; every subprocess capture passes
`encoding="utf-8"`. *Source:* v2.5.4; v2.10.1; v2.14.1.

**INV-099 · The extractor and the consolidator are plugin-agnostic.** No
project-specific vocabulary in `core/extractor.py` or `core/consolidate.py`.
*Ungated.* *Source:* v2.1.0; v2.8.0.

## 7. Installer, build and release

**INV-100 · Every install-layout probe accepts BOTH the nested and the flat
form.** The standalone installer lays the package FLAT under
`~/.claude/hooks/cc-memory/` with no `cc_memory/` segment; `cli/mem.py`'s
`_REQUIRED_PLUGIN_FILES` is DERIVED from the hooks' module-level import graph.
*Gate:* `tests/test_surfaces.py` §3 manifest parity; `falsify --case r11entryreq`
/ `r11flattree`. *Source:* v2.4.3 §3; v2.11.1.

**INV-101 · The `settings.json` write is a compare-and-swap, verified BEFORE and
AFTER the rename, judged per ENTRY on both paths, and follows a symlink.**
`_settings_fingerprint` returns a SENTINEL for an absent file, never `None`;
`_merge_into_settings` retries the whole merge (`_MERGE_ATTEMPTS`); a user hook
sharing a matcher group with ours survives install and uninstall;
`_settings_write_target` writes THROUGH a linked file; the installer refuses
unknown arguments (`_KNOWN_FLAGS`). *Gate:* `tests/test_surfaces.py` §3, §6,
§9; `falsify --case r9instgrp` / `r9instcas` / `r14settingslink`. *Source:*
v2.5.3 §4-§5; v2.5.4 §2; v2.9.0 §5-§6; v2.14.0 §19.

**INV-102 · The frozen installer never runs a `.py` through `sys.executable`,
and a manifest value is rendered at its sink.** `_python_for_script` hands
scripts to the interpreter the hooks use; `_manifest_slot` bounds, flattens and
escapes every value the CLAUDE.md generator interpolates; `_read_settings`
validates before anything is copied and preserves malformed hook groups
verbatim. *Gate:* `tests/test_surfaces.py` §3 asserts nothing else reads
`sys.executable`; `falsify --case r14frozendash` / `r14pkgdesc` / `r14pkgname`.
*Source:* v2.5.0 §6; v2.14.0 §19.

**INV-103 · Both exes are RUN before release, by CI, never PE-inspected.**
`.github/workflows/release.yml` refuses a tag that disagrees with
`core/version.py`, runs the gates on the tagged commit, builds, runs a real
`--cli` install and `--uninstall` against a sandboxed home, requires exit 2 on
an unknown flag, and attaches the exes to the Release with the CHANGELOG
section as body (`scripts/release_notes.py`, which fails loud without a `###`
headline). A pwsh step that expects a NON-ZERO native exit reads it through
`Start-Process`. A moved tag is a rewritten history. *Source:* v2.5.4 §4;
v2.12.0; v2.12.1 §1.

**INV-104 · A platform-dependent expectation is asserted per platform, never
skipped, and Linux runs EVERY gate.** *Gate:* `.github/workflows/gates.yml`
declares the lanes. *Source:* v2.11.2 §2; v2.12.1 §2.

**INV-105 · `.gitignore` keeps the state directory visible to the checker and
the surfaces installed by name.** The generated `.ccm/.gitignore` has three
copies (`core.progress.ensure_memory_gitignore`, the installer, `ccm-load`)
kept in parity; the repository's own `.gitignore` re-includes `.ccm/` under
its `.*/` blanket and `.github/` stays tracked; `SURFACE_FILES` are recorded in
`installed_surfaces.json` and removed BY NAME. *Gate:* `tests/smoke_test.py`
§ gitignore; `falsify --case gitignore` / `r11gitignore`. *Source:* v2.4.2 §5;
v2.5.0 §6; v2.11.1.

**INV-106 · The `~/.claude/projects` slug convention has one implementation.**
`core.extractor.mangle_project_path` replaces every character outside
`[A-Za-z0-9]`; `find_transcript_dir` is the ladder with the `Path.home()` guard.
See INV-063. *Source:* v2.5.0 §1; v2.15.0.

**INV-107 · The dashboard's logic cores are PURE staticmethods.**
`DashboardApp._render_progress_plan` and `_normalize_tidy_verdict` take plain
data and return plain data; the Tk callbacks keep widget plumbing only, which
is what makes their zero coverage tolerable. *Gate:* `tests/test_surfaces.py`
§8; `falsify --case r10dashrender`. *Source:* v2.10.1 §1.

**INV-108 · `tools/contracts.py` counts the BACKSTOP creators and fails LOUD
when its proxy goes hollow.** `_BACKSTOP_CREATORS` is verified against each
module's source; `_verify_entry_gate` errors both registries if `hooks/_entry.py`
stops consulting `is_excluded` before `project_root`. *Gate:* `falsify --case
creators` / `r10gateproxy`. *Source:* v2.8.0 §11; v2.10.1 §2.

## 8. Documentation and the gates

**INV-109 · `tests/run_gates.py` is THE gate runner, and every count of the
gates is derived from `len(GATES)`.** The suites and checkers on disk must all be
on that list and the list must run each one; `CLAUDE.md` § Tests opens with the
derived count word and names every gate script; `README.md`, `README.zh.md`,
`CONTRIBUTING.md` and the PR template are checked in both languages, fenced
command comments included. *Gate:* `tests/smoke_test.py` § doc facts and § gate
count; `falsify --case r15gatecount` / `r15gatecountcjk`. *Source:* v2.11.1;
v2.11.4; v2.15.1 §2.

**INV-110 · `tools/falsify_fixes.py --anchors` is a CI step OUTSIDE the runner
and is run before tagging; a repaired anchor is re-driven RED.** A green gate run
is not the evidence CI produces; an anchor edited until it merely matches proves
nothing. *Source:* v2.13.1.

**INV-111 · No `file:line` citation may be UNCHECKED, and quoted evidence is
never repaired.** `tools/citation_check.py` anchors on a symbol where it can and
bounds-checks where it cannot, saying "NOT verified against a symbol" in those
words; `TRACKED` is the list of files it scans; a `<!-- verbatim: <capture> -->`
region is verified IN ORDER against its capture and never scanned or fixed.
*Gate:* `tests/smoke_test.py` § citations; `falsify --case r12verbatim` /
`r12verbatimskip` / `r14verbatimorder` / `r14dotdirs`. *Source:* v2.5.2 §7;
v2.5.4 §1; v2.12.2 §2; v2.14.0 §9 and §20.

**INV-112 · Do not enumerate a set in prose — bind the count.**
`python tools/contracts.py` computes each set; a count is bound with
`<!--ce:<set>-->`, a strict subset with `:subset`, a statement about the past
with `:asof`; a history heading (`Previously`, `What's new`, `What changed`,
`此前`, `…有什么新变化`, `…有什么新东西`) and `CHANGELOG.md` as a whole lift the
must-be-bound requirement, not the verification; fenced blocks are literal.
`tools/doc_claims.py` scans the tracked markdown, `cc_memory/config.json` and
the docstrings and comment runs of the shipped package. *Gate:*
`tests/smoke_test.py` § doc claims; `falsify --case claimword` / `claimof` /
`r13claimsgap` / `r8claimpy` / `r8claimjson`. *Source:* v2.8.0; v2.14.0 §9;
v2.16.0 E5.

**INV-113 · The demo is evidence, not a mockup.** `demo/run_demo.py` is the
protocol as code; the committed captures are what the README text was written
against, and a README quote is copied from them inside a verbatim region.
*Source:* v2.12.2 §2.

**INV-114 · `CHANGELOG.md` is never swept to a new name, and a dated measurement
keeps its number.** Entries describe the tree as it was; documents that describe
CURRENT behaviour are swept. State the SET where a fresh number would rot the
same way the last one did. *Source:* v2.13.0; v2.14.1 §1.

**INV-115 · A gate's condition must be SUFFICIENT for the sentence it
certifies.** `tools/doc_coverage.py` requires NAMING (a code span or a quoted
JSON key) of every schema table, `ALTER`-added column, MCP tool and config key
by its owning document in both languages; `tools/i18n_check.py --emit-marker`
refuses to re-stamp an untranslated sibling (`--translation-unchanged "<why>"`
for an English-only change). *Gate:* `tests/smoke_test.py` § gate checkers;
`falsify --case r13i18nrestamp` / `r13coveragename` / `r13coverageenum` /
`r13coveragetools` / `r11doccoverage`. *Source:* v2.11.4; v2.14.0 §9.

**INV-116 · A ZERO-BYTES assertion is evidence only when paired with a control
that EMITS, and a falsification case that runs GREEN indicts the check.** Each
input-side gate of the recall channel is a PAIR in a project of its own;
`--case <id>` is driven RED before a case is kept; a red untouched copy reports
UNSOUND. *Source:* v2.11.1; v2.14.0 §20; v2.15.0 §3.

**INV-117 · An unbound sentence rots silently; a matrix stated as a product
invents lanes; a symbol quoted from a previous shape survives the citation
gate.** Name the lanes the workflow declares, re-anchor prose to the symbol, and
keep `SECURITY.md`'s supported-versions table at the current minor.
*Source:* v2.14.1.

**INV-118 · A rename sweep covers the strings the user or the model READS, and
a file is never rewritten with `splitlines()` + `"\n".join()`.** Grep `help=`,
`print(`, and every list a renderer joins; pass `newline=""` when writing.
*Source:* v2.13.2.

**INV-119 · A test that spells the same literal as the code cannot catch the
code.** Assertions about a rendered path take the name from the fixture.
*Source:* v2.13.2 §2.

**INV-120 · A new invariant goes into the specification, not only into the
CHANGELOG.** `docs/CONTRACTS.md` for a contract, this file for a rule; the
person about to break it reads the specification. Known limit: no gate detects
an undocumented design. *Source:* v2.11.3.

**INV-121 · `CLAUDE.md` is an operating manual, bounded at 45 KB.** Version
narratives live in `CHANGELOG.md`, rules live here. *Gate:* `tests/smoke_test.py`
§ manual size. *Source:* v2.16.0 E1 and E5.

**INV-122 · Comments and code agree.** A comment that names a function names
one that exists; "flags" is not "refuses"; a stage count is the count in the
code; a docstring that promises a measurement is backed by a line that computes
it. Swept in v2.16.0 D5; every later sweep starts from `git grep` of the claim.
*Ungated* beyond `tools/doc_claims.py`'s trigger nouns. *Source:* v2.15.0 §3;
v2.16.0 D5.
