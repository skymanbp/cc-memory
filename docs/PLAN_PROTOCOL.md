# PLAN protocol (v2.2)

cc-memory's **live plan anchor**: a single `memory/PLAN.md` per project that
stays in sync with what's actually being worked on, so the AI doesn't
forget the plan as context grows or drift onto unrelated work.

## Why a separate file from PROGRESS.md

`PROGRESS.md` is the **cross-session handoff** document — what the next
Claude needs to know to pick up where the previous one left off. It's
overwritten at every PreCompact, patched at every Stop.

`PLAN.md` is the **task anchor** — what we're trying to accomplish *right
now*, with explicit step status. It outlives single turns and single
sessions. Mixing them would make PROGRESS.md too long and PLAN.md
unstable.

Both share the same SQLite database (`plan_active` and `progress` tables
respectively) so they cannot drift out of sync with their source of truth.

## Lifecycle

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
              → outputs JSON {goal, success_criteria, steps[]}
                                  │
                                  ▼
              `/cc-mem plan-set --from-refiner` (stdin = JSON)
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

## Data model: `plan_active`

Single row per project. Schema (v4 migration):

| Column                          | Type    | Purpose |
|---------------------------------|---------|---------|
| `project_id`                    | INTEGER | PK, FK → projects.id |
| `raw`                           | TEXT    | Verbatim plan-mode output (or user-pasted) |
| `structured`                    | TEXT    | JSON {goal, success_criteria, steps[], context, ...} |
| `active_step`                   | INTEGER | id of the step currently in progress |
| `edits_since_last_guardian`     | INTEGER | Drift counter (incremented by Edit/Write/...) |
| `turns_since_last_guardian`     | INTEGER | Drift counter (incremented by Stop) |
| `last_guardian_at`              | TEXT    | ISO timestamp of last guardian check |
| `last_refined_at`               | TEXT    | ISO timestamp of last refine |
| `needs_refine`                  | INTEGER | 1 = raw is fresh but structured is stale |
| `created_at`, `updated_at`      | TEXT    | Standard timestamps |

## Structured plan JSON schema

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
  "refined_at": "2026-05-25T14:30:00",
  "refined_by": "plan-refiner"
}
```

Valid `status` values: `pending`, `in_progress`, `done`, `blocked`, `skipped`.

## Sync algorithm (TodoWrite ↔ steps)

When `TodoWrite` is observed, `core.plan.sync_todos_to_steps`:

1. For each todo, compute trigram-Jaccard similarity to every step's title.
2. Pick the best-matching step IF similarity ≥ `MATCH_THRESHOLD` (0.35).
3. Update the step's status from the todo's status, using:
   - `completed` → `done`
   - `in_progress` → `in_progress`
   - `pending` → `pending`
   - `cancelled`/`canceled` → `skipped`
   - `blocked` → `blocked`
4. Steps already `done` never regress (a stray `pending` todo doesn't undo it).
5. Unmatched todos are counted as drift signal (todo content has no
   corresponding plan step).

## Nudge thresholds

Configurable (default in `config.json`):

| Trigger                                  | Threshold      | What gets emitted |
|------------------------------------------|----------------|-------------------|
| `turns_since_last_guardian` reaches      | 8 (default)    | One-line Stop status |
| `edits_since_last_guardian` reaches      | 12 (default)   | One-line Stop status |
| Sensitive bash tool detected             | n/a (immediate via +20 bump) | One-line Stop status next turn |
| `needs_refine = 1`                       | n/a (immediate) | "NEW PLAN captured" line |

The Stop hook NEVER emits a `<system-reminder>` for plans — only a soft
advisory status line. Use `/cc-mem plan-check` to explicitly request
a guardian sweep.

## Subagent contracts

- **`plan-refiner`** (`agents/plan-refiner.md`): One-shot raw→structured
  conversion. Tools: Read, Grep, Bash. Output: JSON on stdout, nothing else.
- **`plan-guardian`** (`agents/plan-guardian.md`): Drift check.
  Tools: Read, Grep, Bash (read-only operations only). Output: a fixed
  report block of ≤150 words.

Both default to the `haiku` model — they're focused, low-context tasks.

## CLI surface

```bash
/cc-mem plan-status              # counters + freshness summary (no LLM)
/cc-mem plan-show                # regen + print PLAN.md
/cc-mem plan-set --raw '<text>'  # store raw, mark needs_refine
/cc-mem plan-set --raw-file FILE # same, from a file
/cc-mem plan-set --from-refiner  # store structured JSON from stdin
/cc-mem plan-check               # reset counters + print guardian invocation hint
/cc-mem plan-replan              # re-arm needs_refine on stored raw
/cc-mem plan-clear               # drop the plan + delete PLAN.md
```

## Sensitive-tool list

`core.plan.is_sensitive_tool_call` flags these Bash patterns for an
immediate guardian-nudge bump (+20 edits):

- `git push`, `git push -f`, `git push --force`
- `rm -rf`, `drop table`, `drop database`
- `npm publish`, `cargo publish`, `pypi-upload`, `twine upload`
- `kubectl apply`, `terraform apply`, `ansible-playbook`

Extend the list in `cc_memory/core/plan.py:is_sensitive_tool_call` as
needed.
