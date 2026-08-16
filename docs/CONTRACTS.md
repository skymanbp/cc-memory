> **English** · [简体中文](CONTRACTS.zh.md)

# cc-memory — Contracts

Three invariants the plugin enforces **in code**, not by convention. Each one
has a single choke-point function, a single generated artifact, and automated
assertions. Violating one is a bug, not a style choice: the mechanisms exist
because the corresponding failure mode was observed in production (stacked
duplicate memories in v2.0, unread handoffs in v2.0, silently sunk plan steps
before v2.4.0).

Where the assertions live: `tests/smoke_test.py` covers the anti-patch decisions,
the PROGRESS.md full-rewrite + fill-only-empty refresh, and the plan lifecycle
(v4 migration → capture → refine → TodoWrite sync → PLAN.md). The **R610
carryover gate** is covered by its own suite, `tests/test_plan_carryover.py`
(20 checks) — `grep -n "carryover\|dispositions" tests/smoke_test.py` still
returns nothing, so run both. `tests/test_surfaces.py` (v2.5) covers the
shipped surfaces these contracts are reached through.

This document supersedes the pre-2.4.3 trio `docs/MEMORY_RULES.md`,
`docs/HANDOFF_PROTOCOL.md` and `docs/PLAN_PROTOCOL.md`; the canonical version
string is `cc_memory/core/version.py`.

**On `file:line` citations.** They are now enforced, by
`tools/citation_check.py` (v2.5.2), which runs inside `tests/smoke_test.py`: for
each citation it resolves the symbols named in the surrounding prose with `ast`
and asserts the cited range covers that symbol's definition, or at least
mentions it. Its first run found **163 of 594 citations stale** and repaired
them mechanically. A citation whose sentence names no uniquely resolvable
function, class or ALL_CAPS constant is reported SKIP and is **not** checked, so
treat a line number here as a hint and the **symbol name** as the fact:
`grep -n "def <symbol>" <file>` is authoritative, and
`python tools/citation_check.py --fix` is how you repair a number.

## Contents

1. [Anti-patch contract](#anti-patch-contract) — every memory write reconciles
   against what already exists (`llm/memory_writer.py:upsert_smart`).
2. [Handoff contract](#handoff-contract) — `memory/PROGRESS.md` is a
   full-rewrite projection of one SQL row, and the next session is *forced* to
   read it (`core/progress.py`, `hooks/session_start.py`).
3. [Plan contract](#plan-contract) — `memory/PLAN.md` is the live task anchor,
   and replacing or clearing it cannot silently drop unfinished steps
   (`core/plan.py`, `cli/mem.py`).

Architecture overview, module layout and the i18n convention live in
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## Anti-patch contract

### The rule

> **Memory updates must be source-style, not patch-style.**
>
> When a new memory M would describe the same fact as an existing memory E,
> the writer **modifies E in place** (or supersedes it with a link) instead
> of appending a separate row. There is no situation in which two active
> rows describe the same fact.

This is the spec that the `llm.memory_writer.upsert_smart` implementation
enforces. Every save path is required to route through that one function.
Skills, CLI, MCP, hooks, GUI, web viewer — no exceptions. The complete caller
list is in [the save-path table](#how-to-honor-this-from-each-save-path); if
you add a save path it goes in that table.

### Why

v2.0 had four independent save paths (`pre_compact`, `stop` observer,
`/save-memories` skill, `mcp_server.handle_memory_add`), each with its own
dedup logic. They produced **stacked memories** — semantically identical
facts saved 3-5 times with slightly different wording — and a polluted
`SESSION_HANDOFF.md` whose contents proved the patch-style anti-pattern
(mixed user prompts, tool outputs, and decisions in one section).

The fix is structural, not cosmetic: write paths converge on one function
that **looks before writing** and chooses the right action.

### Decision tree (the contract)

Inputs: `content`, `topic`, `category`, `importance`, `tags`, `session_id`
(`llm/memory_writer.py:95-158`).

```
0. content = clean_for_storage(content.strip()).           (memory_writer.py:66)
   SKIP (reason: too_short) if len < MIN_CONTENT_LEN (10).      (:110-111)
   Coerce category outside {decision,result,config,bug,task,arch,note}
     → "note".                                                  (:113-114)
   Clamp importance to 1-5; tags default to [].                 (:115-116)

0b. Everything from here through step 5 runs inside ONE `BEGIN IMMEDIATE`
   transaction — `db.reconcile_upsert` (core/db.py:1207-1335). `upsert_smart`
   supplies the POLICY as parameters: the thresholds, the best-candidate
   function (`_make_pick`) and the tag-union rule (`_merge_fields`); the
   database owns the ATOMICITY. Before the transaction existed, two
   concurrent savers of the same sentence both observed an empty table and
   both inserted (measured: actions=['inserted','inserted'], 2 active rows
   with one hash).

1. Compute content_hash = sha256(content.strip().lower())[:16].
                                                     (db.compute_content_hash)
   An exact-hash match, checked by SQL INSIDE the transaction:
       → SKIP. Action: "skipped". Reason: hash_match.
       Rationale: exact text duplicate.
   (`db.find_by_hash` survives only as the IntegrityError recovery path —
   the race loser re-reads the winner instead of raising.)

2. Search for the most similar ACTIVE memory in scope (`_make_pick`):
       primary scope:   topic == new_topic
       fallback scope:  category == new_category, is_active = 1,
                        ORDER BY updated_at DESC LIMIT max_candidates
                        — fires when topic is empty OR the topic query
                          returned no rows
   Similarity = Jaccard over `core.textsim.shingle_set` — character
   trigrams for non-CJK text, character BIGRAMS for CJK runs (a
   one-character CJK correction scored 0.4545 under trigrams and could
   never merge or supersede). At most MAX_CANDIDATES_TO_SCAN (500)
   candidates are scored. Let sim = max similarity.

3. If sim >= HIGH_SIM (0.80):
       → MERGE_IN_PLACE, by SQL in the same transaction.
       content=new_content, importance=max(new_imp, existing_imp),
       topic=new_topic or existing_topic,
       tags=_merged_tags(existing_tags, new_tags, ["merged"])
       # UNION with the SURVIVING row's tags, never replace — provenance
       # (["observer","realtime"], ["mcp"], …) is inherited — capped at
       # MAX_TAGS (32) because memory_add is model-invokable.
       Action: "merged". No new row. content_hash is recomputed.
       Rationale: "essentially the same sentence" — keep the latest wording.

4. Else if sim >= MID_SIM (0.50):
       → SUPERSEDE: insert the new row with supersedes_id=existing.id AND
       archive the old row, both inside the SAME transaction (a kill
       between the two used to leave both rows active).
       tags=_merged_tags(existing_tags, new_tags, ["supersedes"]), capped.
       Action: "superseded".
       Rationale: refined / consolidated version of the same fact;
       preserve history via the chain so we can audit what changed.

5. Otherwise:
       → INSERT NEW (still inside the transaction).
       Action: "inserted".
       Rationale: independent fact.

6. After the loop, `upsert_batch` calls
   regenerate_memory_index(db, project_id, memory_dir) to keep memory/MEMORY.md
   in sync — unconditionally (even if every item was skipped), but ONLY when a
   `memory_dir` was passed (memory_writer.py:106-169). `upsert_smart` called on
   its own never regenerates. NEVER let MEMORY.md drift.
```

`upsert_smart` returns
`{"action": "skipped"|"merged"|"superseded"|"inserted", "id": ..., "similarity": ..., "old_id": ...}`
(`memory_writer.py:200-235`); the skip paths add a `"reason"` of `too_short` or
`hash_match`. `upsert_batch` aggregates those into per-action counts plus a
`results` list (`memory_writer.py:200-235`).

### Thresholds and constants

`HIGH_SIM` / `MID_SIM` are module constants in `cc_memory/llm/memory_writer.py:64`
(0.80 / 0.50), alongside `MIN_CONTENT_LEN` (10), `MAX_CANDIDATES_TO_SCAN` (500)
and `MAX_TAGS` (32). There are no `writer.*` keys in `cc_memory/config.json` —
they were deleted in v2.5.0 with the other 34 inert keys (`config.json`'s
`removed_keys` note records it), precisely because they were never read.
**Change the constants, not the config.** The defaults (0.80 / 0.50)
were chosen empirically: 0.80 demands the new content is essentially the same
sentence (different wording, same fact); 0.50 catches "refined" versions while
still letting genuinely-related-but-distinct facts through.

### What this rules out

The following anti-patterns are mechanically blocked because they require
bypassing `upsert_smart`:

1. **Stacked duplicates.** v2.0 had a `cc-memory` topic with 10 entries that
   collectively re-stated the same plugin description in slightly different
   wording. With `upsert_smart`, attempt #2 would have merged into #1.

2. **Patch updates without history.** If a fact genuinely changes ("we
   switched from lr=3e-4 to lr=1e-4 because…"), the supersede path
   preserves the old fact as `is_active=0` linked via `supersedes_id`.
   `db.get_supersede_chain(id)` (`core/db.py:1387-1402`) walks the history. No
   "git blame for memories" hack needed.

3. **MEMORY.md staleness.** Auto-regeneration after every batch write
   prevents the 50-day-stale failure mode observed in v2.0 (where
   PreCompact wrote MEMORY.md but Stop/skill/MCP/CLI didn't). Further
   refreshers keep it honest outside the save paths: the PreCompact tail
   (`hooks/pre_compact.py:509`), the Stop-hook idle reorg (`core/idle.py:96`),
   the async consolidation leg (`hooks/consolidate_async.py:187-188`),
   `/cc-mem cleanup` (`cli/mem.py:1100`) and the `ccm-load` skill
   (`skills/ccm-load/SKILL.md:318`).

4. **Hash-only dedup hiding semantic dupes.** Hash dedup is step 1, but steps
   2-5 catch "fix bug" vs "fix bug." (same fact, different punctuation) which
   v2.0 missed.

### How to honor this from each save path

Every caller of `llm/memory_writer` in the tree, and the exact entry it uses.
There are no direct `db.insert_memory` callers outside `core/db.py` and the
writer — and since v2.8.0 that is a COMPUTED contract, not prose:
`tools/contracts.py` derives `insert_memory_callers` from the tree,
`smoke_test.py` asserts it is empty, and `tools/falsify_fixes.py --case
r8antipatch` proves the assertion goes red when a bypass caller appears.

| Save path | Entry function |
|-----------|---------------|
| `PreCompact` hook | `upsert_batch(db, pid, sid, extracted_list, memory_dir)` (`hooks/pre_compact.py:672`) |
| `Stop` observer | `upsert_batch(db, pid, None, observer_list, memory_dir)` (`hooks/stop.py:327`) |
| `SessionStart` retroactive save | `upsert_batch(db, pid, sid, memories, memory_dir=memory_dir)` — un-saved prior sessions (`hooks/session_start.py:1073`) |
| `/save-memories` skill | `upsert_batch(db, pid, None, memories, memory_dir=Path(project) / 'memory')` (`skills/save-memories/SKILL.md:100`) |
| `mem.py add` CLI | `upsert_smart(...)` + `regenerate_memory_index(...)` (`cli/mem.py:1089,524`) |
| `mcp/server.py handle_memory_add` | `upsert_smart(...)` + `regenerate_memory_index(...)` (`mcp/server.py:629-656,192`) |
| Dashboard UI "Add Memory" | `upsert_smart(...)` + `regenerate_memory_index(...)` — routed since v2.2 (`ui/dashboard.py:1664,956`). `ui/dashboard.py` contains no `db.insert_memory` call. |
| Dashboard UI "Save Session" | `upsert_batch(...)` (`ui/dashboard.py:2246`) |
| Dashboard UI "Init Project" scan | `upsert_batch(db, pid, None, batch, memory_dir=memory_dir)` (`ui/dashboard.py:2246`) |
| web_viewer POST `/api/memory` | `upsert_smart(...)` + `regenerate_memory_index(...)` (`ui/web_viewer.py:65`) |

### Consolidation backstop exception (v2.3)

The **consolidation** pipeline (`core/consolidate.py`) is the documented
exception to "route every write through `memory_writer`". It is the cleanup
backstop, not a save path, and it operates on memories that ALREADY exist:

- `semantic_dedup` (LLM-judged same-fact merge, `consolidate.py:405-492`) and
  `detect_obsolete_llm` (newer-fact-contradicts-older, `:817-897`) call
  `db.update_memory` + `db.archive_obsolete` directly. They never create
  user-facing content from scratch — a survivor row already exists; losers are
  archived (`is_active=0`) with a forward `supersedes_id` link
  (`db.archive_obsolete`, `core/db.py:1782-1911`) so the lineage stays traceable
  and recoverable. Since v2.9.0 that link is written with `COALESCE`, never
  over an existing one: a loser produced by an earlier SUPERSEDE already points
  at the row IT replaced, and overwriting made that older version unreachable
  from every chain walk (measured: chain `[2,1]` became `[2,3]`). The slot
  records the FIRST lineage fact it learns; when it is already occupied the
  replacement is written to the log instead. `semantic_dedup` unions the
  survivor's existing tags before writing (`consolidate.py:479-487`); as of
  v2.8.0 `upsert_smart` does too (`llm/memory_writer.py:_merged_tags`), so this
  is no longer a difference between them — the MERGE branch used to write
  `set(incoming + ["merged"])` and destroyed the surviving row's provenance
  tags outright.
- `decay_and_archive` (reference-aware staleness net, `consolidate.py:822-861`)
  archives ONLY very old + low-importance + never-injected rows — a
  zero-false-archive safety net. Effective age is
  `now - COALESCE(last_referenced_at, created_at)` (`core/db.py:224-234`;
  `consolidate.effective_age_days`, `:56`).
- **EVERY consolidation stage is reversible** (`is_active=0`, never `DELETE`),
  `cleanup_garbage` included as of v2.8.0. It used to be the one exception,
  and it was the wrong one: it ran unattended from the Stop hook every five
  turns and hard-DELETEd anything shorter than its own private 20-character
  floor — against the writer's 10, so it destroyed what four surfaces had just
  accepted. Measured: `/cc-mem add note "lr=3e-4 wins"` reported `[inserted]`
  and five turns later the table held zero rows. It now imports the single
  floor from `llm.memory_writer` and archives through
  `db.archive_if_unchanged` (`core/db.py:1546-1581`), like the other two
  snapshot-verdict stages. That variant, not `bulk_archive`: this stage's
  verdict is computed from a snapshot read in a SEPARATE transaction while the
  PreCompact writer runs concurrently, so a row whose garbage content was
  repaired in that window used to be archived anyway — measured, the freshly
  merged good content went `is_active=0`. Guarding on the `content_hash` the
  verdict was computed from turns a stale verdict into a no-op.
  `db.delete_memories` remains for user-driven purges only
  and has no caller in the tree — the contract `core/db.py` states for itself.
  `/cc-mem archive <id>...` is the USER-facing retirement path added in
  v2.8.0, and it archives too: `sql` is read-only and `add` reconciles only on
  similarity, so a memory discovered to be WRONG previously had no supported
  exit at all.
- `merge_near_duplicates` archives via `bulk_archive`, i.e. reversibly but
  WITHOUT a `supersedes_id` link; `semantic_dedup` sets the link, and re-reads
  its chosen survivor after the LLM judge call before writing, because that
  call is a network round-trip the Stop hook's idle reorg can mutate under.

Nothing in the consolidation path removes a row; the SAVE-path rule is
therefore not loosened anywhere.

### What you should NOT do

- Don't call `db.insert_memory` directly from any save path. (It's still
  exposed for migration / bulk-load, but not for everyday writes —
  `core/db.py:450`.)
- Don't roll your own `"SELECT content FROM memories ..."` dedup. That's
  what `db.find_by_hash` (`core/db.py:1955-1963`) and the writer's `_find_similar`
  (`llm/memory_writer.py:179`) are for. (There is no `db.find_similar`; the
  matcher lives in the writer, private by design.)
- Don't "patch" MEMORY.md by hand or expect another path to refresh it. Call
  `regenerate_memory_index` after any non-trivial state change. The generated
  file carries its own DO-NOT-EDIT banner listing every overwriting path
  (`llm/memory_writer.py:215-229`).

### Verification

In a project with cc-memory installed:

```bash
# Show supersede-chain count (proves anti-patch is active)
/cc-mem stats

# Walk a specific chain
/cc-mem supersedes <memory_id>
```

`/cc-mem` resolves the CLI itself (`commands/cc-mem.md:54-69`): it probes
`${CLAUDE_PLUGIN_ROOT}` first, then `$HOME/.claude/hooks/cc-memory`, and inside
each root tries the NESTED layout `<root>/cc_memory/cli/mem.py` (marketplace /
dev checkout) before the FLAT one `<root>/cli/mem.py`. The standalone installer
copies each subpackage straight into `TARGET_DIR/<subdir>/`
(`cc_memory/ui/installer.py:77-89` `TARGET_DIR`, `:37-48` `SUBPACKAGE_FILES`,
`:74` `_copy_subpackages`), so a standalone install has **no** `cc_memory/`
path segment — its CLI is `~/.claude/hooks/cc-memory/cli/mem.py`. Under a
marketplace install that tree holds only `logs/` (verified on this machine),
so any hardcoded `python ~/.claude/hooks/cc-memory/.../mem.py` invocation fails
there — this repo is a marketplace/directory install.

If `Supersede chains: N update events recorded` shows up
(`cli/mem.py:853`), the contract is working. Zero is fine (no facts have
been refined yet), but a steadily growing number means real-world consolidation
is happening.

---

## Handoff contract

### The problem v2.1 solved

v2.0 wrote `memory/SESSION_HANDOFF.md` as an *append-style* document — each
PreCompact added sections, and over time the file accumulated stray Bash
output, conversation fragments, and contradictory state from earlier sessions.
The next Claude was *softly reminded* to "remember to call /save-memories"
but nothing actually made it read SESSION_HANDOFF.md. Result: handoff
unreliable; new sessions repeated already-done work.

v2.1 fixed this with **PROGRESS.md** (always-full-rewrite from a SQL row) +
a **forced `<system-reminder>` injection at SessionStart**. The legacy
`SESSION_HANDOFF.md` is renamed to `SESSION_HANDOFF.md.v2.bak` on the first
PreCompact under v2.1+ (one-shot migration `migrate_legacy_handoff`,
`core/progress.py:590-608`, called from `hooks/pre_compact.py:542`).

### PROGRESS.md is the SOT

`memory/PROGRESS.md` is generated by `cc_memory/core/progress.py:write_progress_md`
from the `progress` SQL row. Schema (`cc_memory/core/db.py:_MIGRATIONS:v3_progress`
at `db.py:176-190`, plus the two v5 session-annotation columns at `db.py:219-222`).
§0 additionally reads the `sessions` / `session_summaries` tables via
`db.get_recent_sessions` (`core/progress.py:301`; `core/db.py:2335-2389`):

| Column | Type | Primary source · Fallbacks |
|--------|------|---------------------------|
| `project_id` | INTEGER PK | `upsert_project` |
| `current_request` | TEXT | UserPromptSubmit turn 1 (`user_prompt.py:145`) → PreCompact `_first_user_request(window.head)` (`pre_compact.py:269-311`) — scans up to 200 records past the leading `queue-operation` / `attachment` meta rows and skips empty-content user rows (`pre_compact.py:269-311`, v2.4.2) → `session_summaries.request` (`progress.py:241`) |
| `status_done` | TEXT | `session_summaries.completed` (`progress.py:236`), which PreCompact fills from the extraction's `result` / `decision` memories (`pre_compact.py:666-700`), falling back to the observed Edit/Write paths only when the extractor returned no outcome. Before v2.8.0 it was ALWAYS that path list, so §2 "Done" rendered a file dump instead of what was accomplished. SessionStart fills it if empty (`session_start.py:589-590`) |
| `status_in_flight` | TEXT | `session_summaries.learned`, filled from the extraction's `arch` / `config` / `bug` memories (`pre_compact.py:666-700`). Before v2.8.0 PreCompact hard-coded it to `""`, so §2 "In-flight" rendered `*(none active)*` unconditionally — structurally, not because nothing was in flight |
| `status_blocked` | TEXT | Explicit `patch_progress(status_blocked=...)` — no in-tree caller does this today; it is an API for external tooling. A repo-wide grep finds only the schema default (`core/db.py:2268-2307,853`), the empty seed (`core/progress.py:253`) and the read (`core/progress.py:253`) |
| `open_todos` | JSON | PreCompact `extract_latest_todo_state(window)` via `ext["latest_todos"]` (`core/extractor.py:478-513,558`; `pre_compact.py:630,656`) → SessionStart tier-3 prior-transcript mine (`session_start.py:882`) → LAST RESORT `session_summary.next_steps` split by `;` (`session_start.py:882`). Only non-`completed` todos are kept (`progress.py:253`) |
| `plan` | TEXT | `session_summaries.next_steps` — sourced from the latest TodoWrite pending items if any, else from LLM-extracted `task` memories (`pre_compact.py:462-468`); propagated at `progress.py:255`, filled-if-empty at `session_start.py:882` |
| `critical_context` | JSON | Top 10 memories with importance ≥ 4, content truncated to 200 chars (`progress.py:107-113`; `session_start.py:882`) |
| `files_touched` | JSON | `observations` table (`pre_compact.py:446-453` → `progress.py:128-134`; Stop per-turn patch `stop.py:193-211`; SessionStart tier-2C `session_start.py:882`) → tier-3 prior-transcript `extract_file_changes` (`session_start.py:882`) |
| `transcript_ptr` | TEXT | PreCompact `transcript_path` resolved absolute (`pre_compact.py:750`) → tier-3 `find_latest_transcript(cwd, exclude_session_id=...)` (`session_start.py:881`) |
| `updated_at` | TEXT | ISO timestamp, stamped by `upsert_progress` / `patch_progress` (`db.py:2099-2175`, `:937-943`) |
| `trigger_type` | TEXT | "auto" \| "manual" (PreCompact passes the host's own trigger string through — `pre_compact.py:749,492`; `"precompact"` is only `collect_progress_state`'s default kwarg at `progress.py:200-260` and is always overridden) \| "stop" (`stop.py:434`) \| "user_prompt" \| "resume_request" (`user_prompt.py:193`) \| "session_start_refresh" (`session_start.py:825`) |
| `current_session_id` | TEXT | `db.tag_progress_session` only (`db.py:2309-2333`) — tagged by PreCompact (`pre_compact.py:749`), Stop (`stop.py:434`), SessionStart (`session_start.py:825`), UserPromptSubmit (`user_prompt.py:193`) |
| `session_started_at` | TEXT | `db.tag_progress_session` — reset only when the stored sid changes; `upsert_progress` preserves both across a full rewrite (`db.py:2099-2175`) |

The rendered Markdown (sections 0-7 in
[`cc_memory/core/progress.py`](../cc_memory/core/progress.py)) is generated
from this row. Hand-editing PROGRESS.md is pointless: any of the four automatic
update paths (PreCompact / Stop / UserPromptSubmit / SessionStart refresh) —
plus the two manual regenerators, `/cc-mem progress` (`cli/mem.py:1238`) and the
MCP `progress_regenerate` tool (`mcp/server.py:742`) — will overwrite it.
All six `write_progress_md` call sites: `pre_compact.py:751`, `stop.py:373`,
`user_prompt.py:209`, `session_start.py:948`, `cli/mem.py:1277`,
`mcp/server.py:243`.

### Rendered layout (§0-§7)

`write_progress_md` (`core/progress.py:239-368`) emits, in order:

| Block | Source | Empty-state text |
|-------|--------|------------------|
| `# PROGRESS — <project name>` + `*Generated: <updated_at>* · via <trigger> · <project path>` | `progress.py:261-265` | — |
| Blockquote: "SINGLE SOURCE OF TRUTH for session handoff … **Never append. Never patch by hand.**" | `:267-268` | — |
| `## 0. Session` | `_render_session_section`, `:172-236` (heading emitted at `:188`) | `⚪ **Current session**: *(no session tagged …)*` (`:206`) and `*(no prior compacted sessions yet)*` (`:215`) |
| `## 1. Current Request` | `:279-281` | `*(no request recorded yet)*` |
| `## 2. Status` — **Done** / **In-flight** / **Blocked** | `:285-293` | `*(none yet)*` / `*(none active)*` / `*(none)*` |
| `## 3. Open Todos` — `- [ ] \`priority\` content`, `[~]` for non-pending | `:297-306` | `*(no open todos)*` |
| `## 4. Plan (sequenced next steps)` | `:310-312` | `*(no plan recorded)*` |
| `## 5. Critical Context (must-know memories)` — up to 10 `- #id \`category\` [topic] content` | `:316-327` | `*(no critical memories)*` |
| `## 6. Files Touched This Session` — grouped by action, ≤30 paths per action | `:331-344` | `*(no files touched)*` |
| `## 7. Pre-compact Transcript Pointer` | `:347-356` | `*(transcript pointer not yet recorded)*` |
| Footer: `---` + "This file is the handoff contract for the next session. Read it FIRST." + a spec pointer line | `:360-364` | — |

§0 is the v5 session annotation and sits **first** on purpose: a reader must be
able to tell immediately whether the row is its own session's write or a stale
write from another session. The current session line is
`🟢 **Current session**: \`#<sid8>\` · started \`<ts>\` · last write \`<ts>\` · trigger \`<t>\``
followed by an explicit warning to treat §3/§6 as another session's work if the
short sid doesn't match (`:191-204`). The prior-session timeline lists up to 5
rows from `db.get_recent_sessions`, excluding the current sid, each as
`` - `#sid` · ended `<ts>` · <n> msgs · <summary> `` where the summary prefers
`session_summaries.completed` and falls back to `sessions.brief_summary`,
whitespace-flattened and truncated at 100 chars (`:210-234`).

### When PROGRESS.md is rewritten

1. **PreCompact** (full rewrite, all fields fresh):
   - Triggered: Claude Code's automatic compaction OR manual `/compact`.
   - `collect_progress_state(...)` builds the full state from
     `extracted_memories + observations + session_summaries`
     (`progress.py:332`).
   - `db.tag_progress_session(...)` runs FIRST so the tag survives
     (`pre_compact.py:749`; see the preservation logic at `db.py:2309-2333`).
   - `db.upsert_progress(**all_fields)` overwrites the row (`pre_compact.py:750`).
   - `write_progress_md(db, pid, memory_dir)` rewrites the file (`:501`).

2. **Stop** (partial update, every turn):
   - `db.tag_progress_session(...)` then
     `db.patch_progress(files_touched=<from observations>, trigger_type="stop")`
     (`stop.py:359`, `:211`).
   - `write_progress_md(...)` rewrites the file with the patched state (`:213`).
   - This keeps "Files Touched This Session" current without waiting for the
     next compaction.

3. **UserPromptSubmit** (turn 1 only):
   - `db.tag_progress_session(...)` (`user_prompt.py:193`) then
     `db.patch_progress(current_request=<prompt>, trigger_type="user_prompt" | "resume_request")`
     (`:132`).
   - `write_progress_md(...)` rewrites (`:133`).
   - Captures the goal of the session immediately, not 8 turns later.
   - If the prompt is exactly one of the **resume signals** (`""`, `"继续"`,
     `"接着"`, `"接着做"`, `"接着干"`, `"继续干"`, `"resume"`, `"continue"`,
     `"go on"`, `"keep going"` — `user_prompt.py:127-131`), the trigger_type is
     set to `"resume_request"` so downstream tooling (and the forced reminder's
     RESUME PROTOCOL) can act on it.

4. **SessionStart refresh** (every session start, tier-2/3 fallback):
   - `_refresh_progress_row(db, pid, memory_dir, current_session_id)`
     (`session_start.py:684-850`).
   - Fill-only-empty: never overwrites a non-empty field upstream wrote
     (contract stated at `session_start.py:815`).
   - Sources, in order: DB critical_memories / session_summary / observations,
     then (if still empty) mining the previous session's `.jsonl` transcript
     for `open_todos`, `files_touched`, and `transcript_ptr`.
   - Exception: `open_todos` is filled from the transcript FIRST — the
     `next_steps`-split heuristic is only the last resort
     (`session_start.py:582-585`, `:663-674`). TodoWrite `tool_use` blocks are
     structured data; splitting a prose `next_steps` string on `;` collapses a
     single long sentence into one phantom todo.
   - This is what guarantees PROGRESS.md doesn't render as a wall of
     `*(none)*` placeholders when PreCompact didn't run (manual `/compact`
     after the transcript was already pruned, very short prior session,
     PreCompact crash, or first-ever session in a project).

### How the next session is FORCED to read it

`cc_memory/hooks/session_start.py:_build_forced_reminder` (`:234-288`) emits
this at the end of the injected context:

```
<system-reminder>
CC-MEMORY HANDOFF — MANDATORY READ-FIRST PROTOCOL

Before responding to any user request in this session, you MUST:
  1. Use the Read tool on `memory/PROGRESS.md` (absolute: <path>).
  2. Use the Read tool on `memory/MEMORY.md` (absolute: <path>).

After reading, explicitly state in your first reply:
  "Read PROGRESS.md — prior progress: <one-sentence summary>."

RESUME PROTOCOL — if the user's first message is exactly one of:
    "" (empty)  ·  "继续"  ·  "接着"  ·  "接着做"  ·  "接着干"  ·
    "继续干"  ·  "resume"  ·  "continue"  ·  "go on"  ·  "keep going"
  then DO NOT ask for clarification. Instead:
    1. Read PROGRESS.md §3 (Open Todos) and §4 (Plan).
    2. If §3 has at least one open todo, announce
       "Resuming prior task: <todos[0].content>" and start executing it.
    3. If §3 is empty but §4 (Plan) is non-empty, follow the plan's first step.
    4. If both are empty, fall back to a one-sentence prior-progress
       summary plus "what would you like to do next?".

Why: this is the project's handoff contract (single source of truth).
Skipping it risks duplicating work or contradicting prior decisions.
Spec: `docs/CONTRACTS.md#handoff-contract`.
</system-reminder>
```

Each numbered Read line is emitted only if that file exists (`n` renumbers), and
the whole block is suppressed when neither exists (`session_start.py:243-246,255-262`).

The bilingual resume-token list is deliberate and must stay in sync with
`user_prompt.py`'s `resume_signals` — it carries a `# i18n Tier 3` guard comment
(`session_start.py:270-271`; see [ARCHITECTURE.md](ARCHITECTURE.md#9-documentation-language-convention-i18n)).

Claude treats `<system-reminder>` blocks as authoritative, much like
cc-enslaver's discipline rules. The wording is deliberate:

- "You MUST" — not "please consider".
- "Use the Read tool on `<absolute path>`" — no ambiguity about which file.
- "Explicitly state in your first reply" — forces a visible acknowledgement
  the user can see (and the next PreCompact will catch).
- Cites the spec so future-Claude can reason about why.

### Pre-compact transcript pointer (deeper context, if needed)

When `current_request` plus the layered injection aren't enough, the next
Claude can fall back to reading the raw transcript directly. PROGRESS.md
Section 7 contains:

````
## 7. Pre-compact Transcript Pointer

If you need raw conversation history before compaction, read:

```
C:\Users\<user>\.claude\projects\<project-hash>\<session-uuid>.jsonl
```

This is a JSONL file: one message per line. Read with the Read tool.
````

This makes it possible to recover information that didn't fit into the
extraction budget without re-running compaction. **It's a deliberate
last-resort path**, not the primary handoff signal.

### What if the next session doesn't follow the reminder?

The reminder is a contract, not a hard gate. Claude *should* honor it, but
if it doesn't, the next `PreCompact` will rewrite PROGRESS.md from whatever
state the new session produced — so the system is self-healing. There's no
catastrophic failure mode; just a missed handoff for one session.

If you observe that Claude is systematically ignoring the reminder, the
likely fix is to (a) tighten the wording in `_build_forced_reminder`, or
(b) add a `PreToolUse(Read)` hook that blocks the FIRST Read if it isn't of
PROGRESS.md (analogous to cc-enslaver's rule 08 enforcement). As of v2.4.2 no
such hook exists — `hooks/hooks.json` declares 5 events / 6 command hooks
<!--ce:hooks--> (PreCompact carries two legs: the 120s sync one and the 300s
`async` consolidation one; run `python tools/contracts.py` for the computed
membership) — and the reminder remains advisory-only.

### Verification

To audit handoff health in a project:

```bash
# 1. Show what PROGRESS.md currently says (this ALSO force-regenerates the
#    file from SQL — cli/mem.py:1238 rewrites it on every invocation)
/cc-mem progress

# 2. Show whether the SQL row has current data
/cc-mem sql "SELECT current_request, trigger_type, updated_at FROM progress"
```

`/cc-mem` resolves the CLI for both install layouts — see
[the anti-patch verification note](#verification) for the full resolution order
(`commands/cc-mem.md:54-69`; nested `<root>/cc_memory/cli/mem.py` for a
marketplace/dev checkout, flat `<root>/cli/mem.py` for the standalone installer
output, `cc_memory/ui/installer.py:33,37-48,74`). A hardcoded
`python ~/.claude/hooks/cc-memory/...` invocation is wrong under a marketplace
install, where that tree holds only `logs/`.

A healthy PROGRESS.md has:

- A non-empty `current_request` (set within turns 1-2 of the session).
- Non-empty `files_touched` after any edit-heavy turn.
- A recent `updated_at` (less than ~5 minutes old during active work).
- No surviving `memory/.pre_compact_attempt.json` (v2.4.2). PreCompact writes
  that marker at entry and removes it only on completion
  (`pre_compact.py:281-320`; written at `:368`, cleared at `:378,536,571`);
  a marker older than 10 minutes means the last compaction was KILLED before its
  save and its memories were lost — SessionStart surfaces it as
  `[WARNING: PreCompact … DID NOT FINISH …]` (`session_start.py:187-206`, age
  gate at `:199`). `.last_save.json` cannot show this: a timeout kill runs no
  `except` block and no `finally`, so that file still describes the PREVIOUS
  successful run.

---

## Plan contract

cc-memory's **live plan anchor**: a single `memory/PLAN.md` per project that
stays in sync with what's actually being worked on, so the AI doesn't
forget the plan as context grows or drift onto unrelated work. Introduced in
v2.2; the mandatory carryover gate landed in v2.4.0 and was tightened in v2.4.1.

### Why a separate file from PROGRESS.md

`PROGRESS.md` is the **cross-session handoff** document — what the next
Claude needs to know to pick up where the previous one left off. It's
overwritten at every PreCompact, patched at every Stop.

`PLAN.md` is the **task anchor** — what we're trying to accomplish *right
now*, with explicit step status. It outlives single turns and single
sessions. Mixing them would make PROGRESS.md too long and PLAN.md
unstable.

Both share the same SQLite database (`plan_active` and `progress` tables
respectively) so they cannot drift out of sync with their source of truth.
`write_plan_md` (`core/plan.py:630-678`) is a full rewrite from the row, and
the generated file carries a DO-NOT-EDIT banner naming the SQL table and the
three legitimate edit entries (`core/plan.py:257-260`).

### Lifecycle

```
                      ┌───────────────────────┐
                      │   ExitPlanMode call   │
                      │   (or `plan-set`)     │
                      └───────────┬───────────┘
                                  │
                                  ▼
              PostToolUse hook captures `plan` field
              → plan_active.raw = <markdown>
              → plan_active.needs_refine = 1
              → writes memory/.plan_raw.md
                                  │
                                  ▼  (next Stop hook turn)
              [cc-memory.plan] NEW PLAN captured → invoke @plan-refiner
                                  │
                                  ▼
              Main Claude spawns plan-refiner subagent (Haiku)
              → subagent reads .plan_raw.md
              → reads the CURRENT plan (plan-show / PLAN.md)
              → outputs JSON {goal, success_criteria, steps[], dispositions?}
                                  │
                                  ▼
              `/cc-mem plan-set --from-refiner` (stdin = JSON)
              → R610 carryover gate: REFUSES (exit 1) unless every unfinished
                step of the outgoing plan is carried or dispositioned
              → outgoing plan archived to memory/.plan_history/
              → plan_active.structured = JSON
              → plan_active.needs_refine = 0
              → write memory/PLAN.md
                                  │
                                  ▼
              ┌─── live work continues ─────────────┐
              │                                     │
              ▼                                     ▼
   PostToolUse: TodoWrite          PostToolUse: Edit/Write/...
   → sync_todos_to_steps()         → bump edits_since_last_guardian
   → rewrite PLAN.md               (sensitive tools bump by 20)
   (both no-op without an active plan row: post_tool_use.py:126,132;
    the todo sync also needs a schema-valid structured plan: core/plan.py:551-556)
              │                                     │
              └─────────────────┬───────────────────┘
                                ▼
              Stop hook checks should_nudge_guardian()
              If turns≥8 OR edits≥12:
                [cc-memory.plan] guardian check recommended
                                ▼
              Main Claude spawns plan-guardian subagent (Haiku)
              → reads PLAN.md + PROGRESS.md + recent git activity
              → reports ALIGNMENT + DRIFT + NEXT ACTION (≤150 words)
                                ▼
              `/cc-mem plan-check` (resets counters)
                                ▼
              [continue, or `/cc-mem plan-replan` if drift severe]
```

Hooks never spawn subagents themselves — they only nudge. The Stop hook's turn
counter is bumped on every turn an active plan row exists (`stop.py:274-277`),
and the two nudge kinds are mutually exclusive: `needs_refine` wins over the
guardian nudge (`stop.py:279-296`).

### Data model: `plan_active`

Single row per project. Schema (v4 migration, `core/db.py:198-211`):

| Column                          | Type    | Purpose |
|---------------------------------|---------|---------|
| `project_id`                    | INTEGER | PK, FK → projects.id |
| `raw`                           | TEXT    | Verbatim plan-mode output (or user-pasted) |
| `structured`                    | TEXT    | JSON {goal, success_criteria, steps[], context, dispositions?, ...} |
| `active_step`                   | INTEGER | id of the step currently in progress |
| `edits_since_last_guardian`     | INTEGER | Drift counter (incremented by Edit/Write/MultiEdit/NotebookEdit — `hooks/post_tool_use.py:131-133`) |
| `turns_since_last_guardian`     | INTEGER | Drift counter (incremented by Stop) |
| `last_guardian_at`              | TEXT    | ISO timestamp of last guardian check |
| `last_refined_at`               | TEXT    | ISO timestamp of last refine |
| `needs_refine`                  | INTEGER | 1 = raw is fresh but structured is stale |
| `created_at`, `updated_at`      | TEXT    | Standard timestamps |

### Structured plan JSON schema

```json
{
  "version": 1,
  "goal": "Implement JWT-based auth for the dashboard",
  "success_criteria": [
    "All routes return 401 without a token",
    "Token refresh works without re-login",
    "Tests in tests/test_auth.py pass"
  ],
  "steps": [
    {"id": 1, "title": "Wire up token refresh",   "status": "done",        "notes": ""},
    {"id": 2, "title": "Add CSRF protection",     "status": "in_progress", "notes": "blocked on framework choice"},
    {"id": 3, "title": "Write integration tests", "status": "pending",     "notes": ""}
  ],
  "context": "Chose JWT over sessions for horizontal scaling.",
  "dispositions": [
    {"old_title": "Wire up token refresh",
     "action": "carried",
     "reason": "no evidence it shipped; re-listed as step 2"}
  ],
  "refined_at": "2026-05-25T14:30:00",
  "refined_by": "plan-refiner"
}
```

Valid `status` values: `pending`, `in_progress`, `done`, `blocked`, `skipped`
(`core/plan.py:145-204`). `normalize_structured` (`core/plan.py:89-134`) is
defensive: it tolerates common LLM status aliases (`todo`→`pending`,
`wip`/`doing`→`in_progress`, `complete`/`completed`→`done`), drops step entries
with no title, and renumbers missing `id`s from their position.
`is_valid_structured` (`:70-86`) requires a non-empty `goal` and ≥1 well-formed
step — anything less is rejected by `apply_refined_plan` with
`"refined plan does not satisfy schema (needs goal + ≥1 step)"` (`:496`).

`dispositions` is optional and only meaningful when this plan REPLACES another
one. Valid `action` values: `done`, `dropped`, `merged`, `carried`; `reason`
must be non-empty (`core/plan.py:347,415-423`). It is preserved in the stored
plan for audit (`core/plan.py:105-111`).

### Sync algorithm (TodoWrite ↔ steps)

When `TodoWrite` is observed, `core.plan.sync_todos_to_steps`
(`core/plan.py:235-280`, matcher at `:139-170`):

1. For each todo, compute Jaccard similarity over `core.textsim.shingle_set`
   shingles (trigrams for non-CJK, bigrams for CJK runs) to every step's
   title.
2. Pick the best-matching step IF similarity ≥ `MATCH_THRESHOLD` (0.35,
   `core/plan.py:98`).
3. Update the step's status from the todo's status, using
   (`_TODO_TO_STEP_STATUS`, `core/plan.py:244-251`):
   - `completed` → `done`
   - `in_progress` → `in_progress`
   - `pending` → `pending`
   - `cancelled`/`canceled` → `skipped`
   - `blocked` → `blocked`
4. Steps already `done` never regress (a stray `pending` todo doesn't undo it)
   (`:212-213`).
5. Unmatched todos are counted as drift signal (todo content has no
   corresponding plan step) and returned as `n_unmatched`.
6. Matches are applied highest-similarity-first, one per step, so duplicate
   todos can't fight over the same step (`:202-207`). The first step that ends
   up `in_progress` becomes `active_step`; if none is, the first `pending` step
   is (`:215-223`).

The whole path is mechanical — no LLM. `apply_todowrite_sync`
(`core/plan.py:1149-1190`) persists the updated plan and rewrites PLAN.md, but
returns `{"skipped": "no_active_plan"}` without touching anything if there is no
row or the stored `structured` is not schema-valid (`:551-554`).

### Carryover gate (R610, mandatory since v2.4.0)

`plan_active` is a SINGLE slot, so replacing a plan is the one moment staged
work can silently vanish. This was a real, documented loss (the SELF-ITER S1-S3
sink: ratified follow-up phases that never re-entered any plan and were gone the
moment the next round's plan overwrote the slot — `core/plan.py:443-458`). Both
doors into that slot are gated, and there is deliberately **no force flag**
(`core/plan.py:455-456`, restated inside the error text at `:647`): a drop
without a recorded reason is exactly the failure mode this gate exists to kill.
`plan-set` accordingly accepts only `--raw / --raw-file / --from-refiner`
(`cli/mem.py`, `cmd_plan_set`) — there is nothing to pass to bypass it.

#### Door 1 — REPLACE (`/cc-mem plan-set --from-refiner` → `core.plan.apply_refined_plan`)

`check_carryover(old_structured, new_plan)` (`core/plan.py:754-871`) collects
the outgoing plan's unfinished steps — status in `pending | in_progress |
blocked` (`_UNFINISHED_STATUSES`, `:461`; selector `unfinished_steps` at
`:465-472`) — and requires each one to be either

  (a) **auto-carried**: shingle-Jaccard at or above `_carryover_bar` — 0.5
      (`CARRYOVER_MATCH_THRESHOLD`) for non-CJK titles, 2/3
      (`CARRYOVER_MATCH_THRESHOLD_CJK`) when either title contains a CJK run,
      because the CJK bigram substrate that HELPS the merge-side writer
      LOOSENS a gate whose false match silently drops a step (measured: 98 of
      325 one-character CJK substitutions flipped from FLAGGED to
      auto-carried, including 三十秒 vs 六十秒 — opposite facts) —
      against a new step's bare `title` OR its `title + notes` (both are
      candidates since v2.4.1, `:492-506` — comparing against `title+notes`
      alone let a long notes field dilute an identical title below the
      threshold, found on the gate's second real replacement, R610; the
      `title+notes` candidate stays so a step folded into another step's notes
      still carries), or
  (b) **dispositioned**: a top-level `"dispositions"` entry whose `old_title`
      matches at ≥ 0.5, with `action` in `done | dropped | merged | carried`
      and a NON-EMPTY `reason` — `detail` is accepted as a synonym for `reason`
      (`:507-538`, synonym at `:528-529`).

The dispositions are read from the **RAW refiner dict**, before normalisation
(`apply_refined_plan` passes `structured`, not `normalised`, at `:635-637`;
rationale in the `check_carryover` docstring at `:485-487`): the schema stays
additive, so an older refiner's output still works on plans with no unfinished
steps.

Any violation raises `ValueError` (`core/plan.py:639-647`). `plan-set
--from-refiner` catches it, prints `[FAIL] refined plan rejected: …` and exits 1
(`cli/mem.py`, `cmd_plan_set`). Nothing is written — the old plan stays exactly
as it was.

##### What the gate does NOT cover — the `success_criteria` advisory (v2.5.6)

The gate's charter is "换计划不许丢步骤": it reads `steps` and nothing else.
`success_criteria` sits outside it, and on 2026-08-05 that showed: a real
replacement passed the steps gate cleanly while **two of ten criteria
evaporated**, one an achieved-but-never-recorded release gate. `context`
likewise.

`unmatched_criteria(old_structured, new_plan)` (appended at the **end** of
`core/plan.py` on purpose — inserting it beside `check_carryover` would have
rotted ~60 line citations in these docs) returns every outgoing criterion whose
best shingle-Jaccard against the replacement's `success_criteria` **plus its
`goal` and `context`** is below the same `_carryover_bar` (0.5, or 2/3 for
CJK). A
criterion folded into the new context counts as carried: lossy survival is
still survival, and flagging it would train the reader to ignore the advisory.

This is deliberately **not** a second refusal. Criteria get reworded, merged,
translated and retired-because-achieved; an EN plan replaced by a ZH one
auto-carries nothing at all, so a hard gate here would make ordinary plan
evolution impossible. `cmd_plan_set` snapshots the outgoing plan *before*
`apply_refined_plan` (afterwards it exists only in `memory/.plan_history/`) and
prints:

```
[!] carryover advisory — 2 of 10 previous success_criteria have no close match
    in the replacement.
    The R610 gate covers `steps`, so these did not block the write. Retiring a
    criterion is fine; losing one silently is not. Confirm each was deliberate:
      - no XXXXXX placeholder survives into a shipped string
      - all seven machine-breaking defects are fixed and re-verified
    `context` is free text and is NOT compared at all — re-read it yourself.
    The outgoing plan is archived under memory/.plan_history/.
```

The advisory names its own blind spot in its last line: `context` is prose and
is never compared. Pinned by `tests/test_plan_carryover.py` §7 (core result,
context-fold suppression, and that the CLI actually prints it — a core function
nobody surfaces is the same silence with extra steps).

A refusal looks like this to the user:

```
[FAIL] refined plan rejected: carryover gate REFUSED — the outgoing plan still has
unfinished steps not accounted for in the replacement:
  - step #4 'Add CSRF protection' — not in the new plan and no disposition
  - step #6 'Write integration tests' — disposition has no reason (a drop without
    a recorded reason is the exact failure mode this gate kills)
Every unfinished step must either appear in the new plan's steps (auto-carry by
title similarity) or be listed in the new JSON's top-level "dispositions":
[{"old_title": ..., "action": "done|dropped|merged|carried", "reason": ...}].
There is no force flag by design.
```

The three violation shapes, verbatim from `core/plan.py:522-538`:

| Condition | Message |
|-----------|---------|
| No similar new step and no matching disposition | `step #N '<title>' — not in the new plan and no disposition` |
| Disposition present, `action` not in the enum | `step #N '<title>' — disposition action '<x>' not in ('done', 'dropped', 'merged', 'carried')` |
| Disposition present, `reason` empty | `step #N '<title>' — disposition has no reason (a drop without a recorded reason is the exact failure mode this gate kills)` |

**How to resolve one.** Do not try to route around it; there is no route. Pick,
per named step:

- The step is still real → add it to the new plan's `steps` (any title over
  the carryover bar auto-carries; re-using the old title verbatim always
  works).
- The step actually shipped → add
  `{"old_title": "<exact outgoing title>", "action": "done", "reason": "<evidence — commit, file:line, test>"}`.
  The refiner is instructed never to claim `done` without evidence in the raw
  document or the current PLAN.md (`agents/plan-refiner.md:79-82`).
- The step is being abandoned → `"action": "dropped"` with the reason it is no
  longer wanted.
- The step was folded into another one → `"action": "merged"` with the absorbing
  step named in the reason.
- Unknown → `"action": "carried"` **and** re-list it in `steps`. This is the
  refiner's mandated default (`agents/plan-refiner.md:79-82`).

Then re-pipe the JSON through `/cc-mem plan-set --from-refiner`.

#### Door 2 — CLEAR (`/cc-mem plan-clear`)

`cmd_plan_clear` (`cli/mem.py:1612-1641`) refuses with exit 1 when
`unfinished_steps(row["structured"])` is non-empty and no `--reason` was given
(`:788-798`):

```
[FAIL] carryover gate: the active plan still has 2 unfinished step(s):
    - #4 Add CSRF protection
    - #6 Write integration tests
  Clearing would silently sink them. Re-run with --reason "<why these steps are
  being dropped>" -- the reason is recorded in memory/.plan_history/.
```

Resolve by re-running with `--reason "<why>"`. The reason is not decoration —
it is written into the archive payload. Only after the gate passes does the
command archive, `db.clear_plan_active(pid)`, and delete `memory/PLAN.md` +
`memory/.plan_raw.md` (`cli/mem.py:1634`).

#### Backstop — append-only plan history

Every outgoing plan — even a cleanly-dispositioned one — is archived by
`archive_plan` (`core/plan.py:879-945`) to

```
memory/.plan_history/plan_<YYYYmmddTHHMMSS>_<replace|clear>.json
```

with `archived_at`, `event`, `reason`, the `structured` form, the `raw` text and
`active_step` (`:556-563`). Called at `core/plan.py:1025` (replace, no reason
string) and from `cmd_plan_clear` in `cli/mem.py` (clear, with the user's
`--reason`). Rows with neither `structured` nor a non-blank `raw` are skipped
(`:549-550`).

An archive-write failure is **non-blocking**: it prints
`[WARN] plan history archive failed (<err>) — proceeding; the carryover gate
already enforced accounting` to stderr and returns `None`
(`core/plan.py:567-575`). The reasoning is explicit in the code: the gate's
dispositions are the primary anti-loss guarantee, so blocking every plan
operation on an archive-disk hiccup would turn a backstop into a
denial-of-service on planning.

### Nudge thresholds

Hardcoded defaults in `core/plan.py:1195-1211` (`turn_threshold=8`,
`edit_threshold=12`); the Stop hook calls `should_nudge_guardian(plan_row)` with
no overrides (`hooks/stop.py`). There is NO `config.json` key for these —
change the signature defaults, or pass explicit kwargs. The `+20` sensitive-call
bump is likewise hardcoded (`hooks/post_tool_use.py`):

| Trigger                                  | Threshold      | What gets emitted |
|------------------------------------------|----------------|-------------------|
| `turns_since_last_guardian` reaches      | 8 (default)    | One-line Stop status |
| `edits_since_last_guardian` reaches      | 12 (default)   | One-line Stop status |
| Sensitive bash tool detected             | n/a (immediate via +20 bump) | One-line Stop status next turn |
| `needs_refine = 1`                       | n/a (immediate) | "NEW PLAN captured" line |

`should_nudge_guardian` returns `(False, "no_active_plan")` without a
schema-valid plan and `(False, "needs_refine_first")` while a raw plan is
awaiting refinement, so the two nudges never collide (`core/plan.py:708-711`).

The Stop hook NEVER emits a `<system-reminder>` for plans — only a soft
advisory status line (`hooks/stop.py:270-272`). Use `/cc-mem plan-check` to
explicitly request a guardian sweep; it refreshes PLAN.md first so the subagent
reads current state, then resets the counters (`cli/mem.py:834-836`).

### Subagent contracts

- **`plan-refiner`** (`agents/plan-refiner.md`): One-shot raw→structured
  conversion. Tools: Read, Grep, Bash; model `haiku` (`:4-5`). Output: JSON on
  stdout, nothing else — it must parse with `json.loads()` directly, no fences,
  no commentary (`:25-26`, `:84`). Its normalisation rules: statuses default to
  `pending` unless the raw text marks otherwise; steps are imperative phrases
  with leading numbering stripped; near-duplicate steps collapse; plan-mode
  meta-chatter is dropped; success criteria must be testable; `context` carries
  only durable why-this-matters information; between 1 and 12 steps (`:45-60`).
  Since v2.4.0 it must ALSO read the current plan (`plan-show` / `PLAN.md`) and
  emit `dispositions` for any unfinished step it does not carry — the storage
  layer refuses the JSON otherwise (`agents/plan-refiner.md:61-82`).
- **`plan-guardian`** (`agents/plan-guardian.md`): Drift check.
  Tools: Read, Grep, Bash (read-only operations only); model `haiku` (`:4-5`).
  Output: a fixed report block of ≤150 words — `ACTIVE STEP / ALIGNMENT /
  EVIDENCE / DRIFT / NEXT ACTION` (`:24-35`). It reads PLAN.md then PROGRESS.md,
  may verify claims with Grep/Bash against the working tree, and calibrates:
  small goal-serving detours are `on-track`, genuinely off-plan work is
  `drifting`, a plan that no longer matches reality is `replan-needed`. It never
  edits, never pushes, and reports `replan-needed` and stops if PLAN.md is
  missing or invalid (`:37-52`).

Both default to the `haiku` model — they're focused, low-context tasks. Both are
shipped inside the plugin's `agents/` directory so they resolve under both
marketplace and standalone installs.

### CLI surface

```bash
/cc-mem plan-status              # counters + freshness summary (no LLM)
/cc-mem plan-show                # regen + print PLAN.md
/cc-mem plan-set --raw '<text>'  # store raw, mark needs_refine
/cc-mem plan-set --raw-file FILE # same, from a file
/cc-mem plan-set --from-refiner  # store structured JSON from stdin
                                 # → R610 carryover gate; exits 1 on refusal
/cc-mem plan-check               # reset counters + print guardian invocation hint
/cc-mem plan-replan              # re-arm needs_refine on stored raw
/cc-mem plan-clear               # drop the plan + delete PLAN.md.
                                 # Archives to memory/.plan_history/ first, and
                                 # REFUSES (exit 1) when unfinished steps exist
                                 # unless --reason "<why>" is given (v2.4.0).
```

Handlers: `cli/mem.py:704-709` (show), `:712-737` (status), `:740-775` (set),
`:778-807` (clear), `:810-821` (replan), `:824+` (check); parser wiring at
`:1041-1059`, dispatch at `:1081-1083`. `plan-status` distinguishes three
states: no row at all, a raw-but-unrefined plan (prints the raw length and tells
you to invoke `@plan-refiner`), and a refined plan (goal, N/M done, active step,
last refine, last guardian check, both counters). `plan-replan` fails with exit 1
if no raw text is stored (`:815-817`).

### Sensitive-tool list

`core.plan.is_sensitive_tool_call` (`core/plan.py:1231-1254`) flags these Bash
patterns — case-insensitive substring match on the `command` input, `Bash` tool
only — for an immediate guardian-nudge bump (+20 edits):

- `git push`, `git push -f`, `git push --force`
- `rm -rf`, `drop table`, `drop database`
- `npm publish`, `cargo publish`, `pypi-upload`, `twine upload`
- `kubectl apply`, `terraform apply`, `ansible-playbook`

The semantics of the +20 are stated in `hooks/post_tool_use.py`: "this single act
carries the same drift risk as ~20 ordinary edits", so the next Stop hook
surfaces a guardian recommendation immediately. cc-memory does NOT block these
calls; it only flags (`core/plan.py:721-726`). The bump, like the ordinary edit
bump, is a no-op when there is no active plan row.

Extend the list in `cc_memory/core/plan.py:is_sensitive_tool_call` as needed.
