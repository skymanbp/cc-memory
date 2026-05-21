# MEMORY_RULES.md — Anti-patch write contract

## The rule

> **Memory updates must be source-style, not patch-style.**
>
> When a new memory M would describe the same fact as an existing memory E,
> the writer **modifies E in place** (or supersedes it with a link) instead
> of appending a separate row. There is no situation in which two active
> rows describe the same fact.

This document is the spec the `llm.memory_writer.upsert_smart` implementation
enforces. Every save path is required to route through that one function.
Skills, CLI, MCP, hooks — no exceptions.

## Why

v2.0 had four independent save paths (`pre_compact`, `stop` observer,
`/save-memories` skill, `mcp_server.handle_memory_add`), each with its own
dedup logic. They produced **stacked memories** — semantically identical
facts saved 3-5 times with slightly different wording — and a polluted
`SESSION_HANDOFF.md` whose contents proved the patch-style anti-pattern
(mixed user prompts, tool outputs, and decisions in one section).

The fix is structural, not cosmetic: write paths converge on one function
that **looks before writing** and chooses the right action.

## Decision tree (the contract)

Inputs: `content`, `topic`, `category`, `importance`.

```
1. Compute content_hash = sha256(content.strip().lower())[:16].
   If db.find_by_hash(project_id, content_hash):
       → SKIP. Action: "skipped". Reason: hash_match.
       Rationale: exact text duplicate.

2. Search for the most similar ACTIVE memory in scope:
       primary scope:   topic == new_topic
       fallback scope:  category == new_category, last 50 by updated_at

   Compute Jaccard similarity on character trigrams of content vs candidate.
   Let sim = max similarity.

3. If sim ≥ HIGH_SIM (default 0.80):
       → MERGE_IN_PLACE.
       db.update_memory(existing.id,
                         content=new_content,
                         importance=max(new_imp, existing_imp),
                         topic=new_topic or existing_topic,
                         tags=existing_tags ∪ {"merged"})
       Action: "merged". No new row.
       Rationale: "essentially the same sentence" — keep the latest wording.

4. Else if sim ≥ MID_SIM (default 0.50):
       → SUPERSEDE.
       new_id = db.supersede_memory(existing.id,
                                     new_content,
                                     supersedes_id=existing.id,
                                     tags=existing_tags ∪ {"supersedes"})
       db.archive_memory(existing.id)
       Action: "superseded".
       Rationale: refined / consolidated version of the same fact;
       preserve history via the chain so we can audit what changed.

5. Otherwise:
       → INSERT NEW.
       db.insert_memory(...)
       Action: "inserted".
       Rationale: independent fact.

6. After 1-5 (any non-skipped path), the batch writer calls
   regenerate_memory_index(project_id, memory_dir) to keep memory/MEMORY.md
   in sync. NEVER let MEMORY.md drift.
```

`HIGH_SIM` and `MID_SIM` are tunable in `cc_memory/config.json` →
`writer.high_similarity_threshold` / `writer.mid_similarity_threshold`. The
defaults (0.80 / 0.50) were chosen empirically: 0.80 demands the new content
is essentially the same sentence (different wording, same fact); 0.50 catches
"refined" versions while still letting genuinely-related-but-distinct facts
through.

## What this rules out

The following anti-patterns are mechanically blocked because they require
bypassing `upsert_smart`:

1. **Stacked duplicates.** v2.0 had `cc-memory` topic with 10 entries that
   collectively re-stated the same plugin description in slightly different
   wording. With `upsert_smart`, attempt #2 would have merged into #1.

2. **Patch updates without history.** If a fact genuinely changes ("we
   switched from lr=3e-4 to lr=1e-4 because…"), the supersede path
   preserves the old fact as `is_active=0` linked via `supersedes_id`.
   `db.get_supersede_chain(id)` walks the history. No "git blame for
   memories" hack needed.

3. **MEMORY.md staleness.** Auto-regeneration after every batch write
   prevents the 50-day-stale failure mode observed in v2.0 (where
   PreCompact wrote MEMORY.md but Stop/skill/MCP/CLI didn't).

4. **Hash-only dedup hiding semantic dupes.** Hash dedup is step 1, but step
   2-5 catch "fix bug" vs "fix bug." (same fact, different punctuation) which
   v2.0 missed.

## How to honor this from each save path

| Save path | Entry function |
|-----------|---------------|
| `PreCompact` hook | `upsert_batch(db, pid, sid, extracted_list, memory_dir)` |
| `Stop` observer | `upsert_batch(db, pid, None, observer_list, memory_dir)` |
| `/save-memories` skill | `upsert_batch(db, pid, None, memories, memory_dir=Path('./memory'))` (the skill body shows the exact invocation) |
| `mem.py add` CLI | `upsert_smart(...)` + `regenerate_memory_index(...)` |
| `mcp/server.py handle_memory_add` | `upsert_smart(...)` + `regenerate_memory_index(...)` |
| Dashboard UI Add Memory | TODO — currently still calls `db.insert_memory` directly. Pre-existing v2.0 path slated for v2.2. |

## What you should NOT do

- Don't call `db.insert_memory` directly from any save path. (It's still
  exposed for migration / bulk-load, but not for everyday writes.)
- Don't roll your own `"SELECT content FROM memories ..."` dedup. That's
  what `find_by_hash` and `find_similar` are for.
- Don't "patch" MEMORY.md by hand or expect another path to refresh it. Call
  `regenerate_memory_index` after any non-trivial state change.

## Verification

In a project with cc-memory installed:

```bash
# Show supersede-chain count (proves anti-patch is active)
python ~/.claude/hooks/cc-memory/cc_memory/cli/mem.py --project . stats

# Walk a specific chain
python ~/.claude/hooks/cc-memory/cc_memory/cli/mem.py --project . supersedes <memory_id>
```

If `Supersede chains: N update events recorded` shows up, the contract is
working. Zero is fine (no facts have been refined yet), but a steadily
growing number means real-world consolidation is happening.
