"""Live plan anchor for cc-memory (v2.2+).

memory/PLAN.md is the project's single source of truth for "what we're
trying to accomplish right now and how far we've got." Separate from
PROGRESS.md (which is the cross-session handoff document) because plans
have a different lifecycle: they outlive a single turn, but they're not
the right place to record session-level handoff state.

Lifecycle:
  1. CAPTURE  — PostToolUse hook special-cases ExitPlanMode and stores its
                raw output into plan_active.raw, setting needs_refine=1.
                User can also feed a plan via `/cc-mem plan-set` CLI.
  2. REFINE   — A subagent (plan-refiner) reads the raw text and produces
                structured JSON. The CLI writes it back; PLAN.md is
                regenerated from the structured form.
  3. SYNC     — On every TodoWrite, this module fuzzy-matches each todo to
                a plan step (trigram-Jaccard) and updates step.status.
                Mechanical; no LLM.
  4. GUARD    — Periodically (turn count + edit count thresholds), the
                Stop hook nudges the main Claude to invoke the plan-guardian
                subagent to confirm the live work is still on track.

The structured plan is a JSON dict with the following schema:
  {
    "version": 1,
    "goal": "single-sentence goal",
    "success_criteria": ["...", "..."],
    "steps": [
      {"id": 1, "title": "...", "status": "done|in_progress|pending|blocked|skipped",
       "notes": "<optional one-liner>"}
    ],
    "context": "<optional background>",
    "refined_at": "<ISO8601>",
    "refined_by": "plan-refiner | manual"
  }
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.atomic import write_atomic, _DERIVED_BUDGET_S
from core.logger import get_logger
# Render-path marker defence. PLAN.md is read by Claude as the live plan
# anchor, and every field it renders originates outside the plugin.
from core.privacy import (clean_for_storage, neutralize_block,
                          neutralize_document, neutralize_inline)
from core.progress import ensure_memory_dir

# This module had no logger through v2.5.2, which is why every failure inside
# it had to be either raised or silently swallowed. File-only by contract —
# hooks must never write to stderr.
_log = get_logger("plan")


# ── Atomic artifact write ───────────────────────────────────────────────────
# Was a private copy through v2.5.2, documented as a "deliberate literal twin"
# of llm/memory_writer.py's. It was not a twin of core/progress.py's, which
# retried and re-raised: this one had no retry and fell back to the plain
# TRUNCATING write, reintroducing the torn-read defect for that call. That
# fallback was the whole residual (20 empty reads in 28,141 samples). One
# implementation now — `core` may be imported by `llm`, so the split never had
# a dependency reason.
_atomic_write_text = write_atomic


# ── Similarity (core/textsim.py — the ONE substrate, CJK-aware) ─────────────
# The private English-only trigram copy this replaced made the carryover gate
# and TodoWrite step sync collapse on CJK step titles exactly the way
# memory_writer's copy collapsed on CJK memories (see textsim's docstring).

from core.textsim import (jaccard as _jaccard, shingle_set as _shingles,
                          CJK_RUN as _CJK_RUN)


def _trigram_set(text: str) -> set:
    # Empty stays an EMPTY set, not {""}: an empty title must match nothing
    # (jaccard's empty-set guard), where {""} would score 1.0 against another
    # empty title. This is the one behavioural difference this module had
    # from the other two retired copies, and it is load-bearing here.
    t = (text or "").strip()
    if not t:
        return set()
    return _shingles(t)


def _has_cjk(*texts) -> bool:
    """True when any argument contains a CJK run — see `_carryover_bar`."""
    return any(_CJK_RUN.search(t or "") for t in texts)


# Match threshold: a todo whose closest step similarity is below this is
# considered "off-plan" and counted as a drift signal.
MATCH_THRESHOLD = 0.35


# ── Schema validation ───────────────────────────────────────────────────────

_VALID_STATUSES = ("pending", "in_progress", "done", "blocked", "skipped")

# Common LLM status aliases, shared by normalize_structured and the carryover
# gate's candidate filter so both read a raw refiner dict identically.
_STATUS_ALIASES = {"todo": "pending", "wip": "in_progress",
                   "complete": "done", "completed": "done",
                   "doing": "in_progress"}


def _norm_status(status) -> str:
    """A raw status value as a member of _VALID_STATUSES (default pending)."""
    if status in _VALID_STATUSES:
        return status
    return _STATUS_ALIASES.get(str(status).lower(), "pending")


def _s(x) -> str:
    """None-safe str(). `str(plan.get("goal", ""))` turned a JSON null into
    the LITERAL string "None" — goal='None', title='None' — and
    is_valid_structured then accepted the plan (register A6). A missing key
    and an explicit null must coerce identically: to ''."""
    return "" if x is None else str(x)


def is_valid_structured(plan: Optional[Dict]) -> bool:
    """Return True iff `plan` has at least a goal and ≥1 step in the expected shape."""
    if not isinstance(plan, dict):
        return False
    if not isinstance(plan.get("goal"), str) or not plan["goal"].strip():
        return False
    steps = plan.get("steps", [])
    if not isinstance(steps, list) or not steps:
        return False
    for s in steps:
        if not isinstance(s, dict):
            return False
        if not isinstance(s.get("title"), str) or not s["title"].strip():
            return False
        if s.get("status") not in _VALID_STATUSES:
            return False
    return True


def normalize_structured(plan: Dict) -> Dict:
    """Coerce a refined plan into the canonical schema. Defensive — handles
    LLM output that's mostly right but missing fields / has odd statuses.

    A non-dict payload (a refiner subagent emitting a top-level JSON *array*
    is a realistic LLM failure mode) yields an empty — and therefore invalid —
    plan instead of raising `AttributeError: 'list' object has no attribute
    'get'`. apply_refined_plan turns that into a ValueError the CLI can print.
    """
    if not isinstance(plan, dict):
        plan = {}
    out = {
        "version": 1,
        "goal": _s(plan.get("goal")).strip(),
        "success_criteria": [],
        "steps": [],
        "context": _s(plan.get("context")).strip(),
        "refined_at": plan.get("refined_at") or datetime.now().isoformat(timespec="seconds"),
        "refined_by": _s(plan.get("refined_by")) or "plan-refiner",
    }
    sc = plan.get("success_criteria", [])
    if isinstance(sc, list):
        out["success_criteria"] = [_s(x).strip() for x in sc if _s(x).strip()]

    # R610: keep dispositions in the stored plan for audit (what happened
    # to the previous plan's unfinished steps, and why).
    dispositions = plan.get("dispositions", [])
    if isinstance(dispositions, list):
        kept = [d for d in dispositions if isinstance(d, dict)]
        if kept:
            out["dispositions"] = kept

    raw_steps = plan.get("steps", [])
    if isinstance(raw_steps, list):
        for i, s in enumerate(raw_steps, start=1):
            if not isinstance(s, dict):
                continue
            title = _s(s.get("title")).strip()
            if not title:
                continue
            status = _norm_status(s.get("status", "pending"))
            try:
                sid = int(s.get("id", i))
            except (ValueError, TypeError, OverflowError):
                # why: this function is DEFENSIVE normalisation of LLM output
                # and its docstring promises callers only ValueError. A JSON
                # overflow float (`1e999`) raises OverflowError and a list
                # raises TypeError — neither is caught by the CLI, so
                # `plan-set --from-refiner` answered a mostly-correct refiner
                # payload with a raw traceback. An id we cannot read falls
                # back to the enumeration index, which is what `id` defaults
                # to when the field is absent entirely.
                sid = i
            out["steps"].append({
                "id": sid,
                "title": title,
                "status": status,
                "notes": _s(s.get("notes")).strip(),
            })
    return out


# ── TodoWrite ↔ step sync ───────────────────────────────────────────────────

def match_todos_to_steps(structured: Dict, todos: List[Dict],
                         threshold: float = MATCH_THRESHOLD) -> Tuple[List[Tuple[int, Dict, float]], List[Dict]]:
    """Match each todo to its closest step by trigram-Jaccard.

    Returns:
      matches:   list of (step_index, todo_dict, similarity) for todos that
                 met the threshold
      unmatched: list of todo_dicts that no step covers (drift signal)
    """
    steps = structured.get("steps", [])
    if not steps:
        return [], list(todos or [])

    step_grams = [_trigram_set(s.get("title", "")) for s in steps]
    matches: List[Tuple[int, Dict, float]] = []
    unmatched: List[Dict] = []
    for todo in todos or []:
        content = todo.get("content", "") if isinstance(todo, dict) else str(todo)
        if not content:
            continue
        tgrams = _trigram_set(content)
        best_idx, best_sim = -1, 0.0
        for i, sgrams in enumerate(step_grams):
            sim = _jaccard(tgrams, sgrams)
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_idx >= 0 and best_sim >= threshold:
            matches.append((best_idx, todo, best_sim))
        else:
            unmatched.append(todo)
    return matches, unmatched


# TodoWrite status → step status mapping
_TODO_TO_STEP_STATUS = {
    "completed": "done",
    "in_progress": "in_progress",
    "pending": "pending",
    "cancelled": "skipped",
    "canceled": "skipped",
    "blocked": "blocked",
}


def sync_todos_to_steps(structured: Dict, todos: List[Dict]) -> Tuple[Dict, Dict]:
    """Update step statuses from TodoWrite snapshot. Returns (updated_plan,
    sync_info) where sync_info = {n_matched, n_unmatched, active_step_id}.

    Rules:
      - For each (step, todo) match, the step's status is updated from the
        todo's status, unless the step is already 'done' (don't regress).
      - 'in_progress' step is recorded as `active_step` for PLAN.md rendering.
      - Steps with no matching todo retain their existing status.
    """
    if not is_valid_structured(structured):
        return structured, {"n_matched": 0, "n_unmatched": len(todos or []), "active_step_id": 0}

    matches, unmatched = match_todos_to_steps(structured, todos)
    steps = structured["steps"]
    seen_step_indices = set()
    active_step_id = 0

    # Apply highest-similarity match per step (LLM may give duplicate todos)
    matches.sort(key=lambda m: m[2], reverse=True)
    for step_idx, todo, _ in matches:
        if step_idx in seen_step_indices:
            continue
        seen_step_indices.add(step_idx)
        old_status = steps[step_idx].get("status")
        new_status = _TODO_TO_STEP_STATUS.get(
            (todo.get("status") or "pending").lower(), "pending"
        )
        if old_status == "done" and new_status != "done":
            continue  # don't regress completed steps
        if (new_status not in _UNFINISHED_STATUSES
                and old_status in _UNFINISHED_STATUSES
                and not _carried(
                    steps[step_idx].get("title", ""),
                    todo.get("content", "") if isinstance(todo, dict) else str(todo))):
            # `done` AND `skipped` both remove a step from the carryover
            # gate's protection (`unfinished_steps`) — promoting to either on
            # MATCH_THRESHOLD (0.35) let a todo about DIFFERENT work retire a
            # step the gate itself would have refused to auto-carry at 0.50.
            # Measured for done: 'Delete the legacy cron entry' (completed)
            # retired 'Delete the legacy session store' at sim 0.4474.
            # v2.7.0 gated done only, and a CANCELLED todo walked the same
            # door: 「把超时设为三十秒」was skipped by「把超时设为六十秒」—
            # opposite facts — at 0.5556 against a 0.6667 CJK bar (register
            # A1), after which the replacement owed it no disposition. Any
            # status that ESCAPES the gate must clear the gate's own bar; the
            # statuses that stay unfinished keep the looser one.
            continue
        steps[step_idx]["status"] = new_status
        if new_status == "in_progress" and not active_step_id:
            active_step_id = steps[step_idx].get("id", step_idx + 1)

    # If no todo set a step in_progress THIS sync, an existing in_progress
    # step keeps the pointer — an UNMATCHED in_progress step used to lose it
    # to the first pending step (register A2: step1 still in_progress,
    # active_step_id=2), telling the reader work had moved on when it hadn't.
    # Only when nothing at all is in flight does the next pending step become
    # active.
    if not active_step_id:
        for s in steps:
            if s.get("status") == "in_progress":
                active_step_id = s.get("id", 0)
                break
    if not active_step_id:
        for s in steps:
            if s.get("status") == "pending":
                active_step_id = s.get("id", 0)
                break

    return structured, {
        "n_matched": len(matches),
        "n_unmatched": len(unmatched),
        "active_step_id": active_step_id,
    }


# ── PLAN.md rendering ───────────────────────────────────────────────────────

_STATUS_GLYPH = {
    "done": "[x]",
    "in_progress": "[~]",
    "pending": "[ ]",
    "blocked": "[!]",
    "skipped": "[-]",
}


_PLAN_MD_HEADER = [
    "# PLAN",
    "",
    "> AUTO-GENERATED by cc-memory · DO NOT EDIT THIS FILE BY HAND",
    "> Source of truth: SQLite `plan_active` table. Edit via",
    "> `/cc-mem plan-set` (manual replace), Claude's plan mode",
    "> (auto-captured), or `/cc-mem plan-replan` (force re-refine).",
    "",
]


def is_live_plan(row: Optional[Dict]) -> bool:
    """True when this `plan_active` row is a real plan, not a cleared slot.

    `db.clear_plan_active` deliberately keeps the row as a TOMBSTONE — that is
    what keeps `revision` monotonic across clears and closes the CAS ABA window
    a DELETE would reopen — with `raw` and `structured` both emptied. So the
    existence of a row says nothing about whether a plan exists, and a bare
    truthiness test on it kept the Stop hook enforcing forever on a project
    whose plan the user had explicitly dropped: the turn counter went on
    accruing and every 8 turns the hook refused, demanding a drift check
    against a plan that was no longer there.

    A NAMED predicate rather than an inline boolean at the call site, because
    the call site is the thing under test: with the condition inlined, a test
    could only re-implement it, and re-implementing the predicate is a
    tautology that passes whatever the hook actually does (proved exactly that
    way — `falsify --case r11tombstone` ran GREEN until this function existed).
    """
    if not isinstance(row, dict):
        return False
    return bool(str(row.get("raw") or "").strip()
                or str(row.get("structured") or "").strip())


def raw_pending_refinement(row: Optional[Dict]) -> bool:
    """True when `plan_active.raw` has NOT been folded into `structured` yet.

    Every live-plan renderer must consult this BEFORE rendering the structured
    form. Until v2.5.0 none of them did, and the newest plan was invisible
    everywhere: `/cc-mem plan-set --raw "..."` — and the PRIMARY auto-capture
    path, PostToolUse ExitPlanMode -> capture_exit_plan_mode — stored a brand
    new raw plan with needs_refine=1, while PLAN.md and `plan-status` happily
    kept printing the PREVIOUS refined plan's goal and steps.

    Any one of these means the raw text is the newest plan:
      * `needs_refine` is set (capture_exit_plan_mode / `plan-replan` armed it);
      * `structured` is missing or invalid while a raw text exists;
      * no refine has ever run (`last_refined_at` empty) while raw exists.

    Deliberately NOT derived from `updated_at`: every TodoWrite sync bumps that
    column, which would flag a perfectly fresh refined plan as stale.

    Accepts a `plan_active` row dict (as returned by `db.get_plan_active`), so
    core renderers and `cli/mem.py cmd_plan_status` share one predicate.
    """
    if not isinstance(row, dict):
        return False
    if not (row.get("raw") or "").strip():
        return False
    if row.get("needs_refine"):
        return True
    if not is_valid_structured(row.get("structured") or {}):
        return True
    return not str(row.get("last_refined_at") or "").strip()


# ── enforcement (v2.11.0) ───────────────────────────────────────────────────
# Why this exists, stated plainly because the absence of it caused real damage:
# every piece of plan machinery below was ADVISORY. `raw_pending_refinement`
# could return True forever while work continued against a stale structured
# plan, and the Stop hook's own comment said so ("The plan-refiner nudge is
# advisory"). Measured in the lore_disaster project on 2026-08-15: a 51,237-char
# raw plan sat unrefined while `plan-status` reported goals from a superseded
# era, the guardian dutifully drift-checked against that stale baseline, and a
# full-transcript audit later found a mechanic the user had demanded SIX times
# with zero implementation. A mechanism nobody is forced to use is a mechanism
# that does not exist.
#
# Two safety properties this MUST keep, or it becomes worse than advisory:
#   1. It can never brick a session. After _BLOCK_MAX_CONSECUTIVE refusals the
#      same condition degrades to a loud advisory, so a genuinely stuck state
#      (a refiner that keeps failing) cannot trap the user in a Stop loop.
#   2. It only governs projects that OPTED IN by having a live plan at all.
#      No plan row -> no enforcement, so the 35 other projects on this machine
#      are untouched until they use the feature.
# Kill switch: CC_MEMORY_PLAN_ENFORCE=0.

_BLOCK_MAX_CONSECUTIVE = 3


def enforcement_enabled() -> bool:
    """False when the operator has switched enforcement off for this run."""
    import os
    return str(os.environ.get("CC_MEMORY_PLAN_ENFORCE", "1")).strip().lower() \
        not in ("0", "false", "no", "off")


def blocking_reasons(plan_row: Optional[Dict],
                     directives: Optional[list] = None,
                     stale_turns: int = 25) -> list:
    """Conditions that should stop the turn, worst first. Empty = let it end.

    `directives` is the v8 ledger (list of rows). A directive is only ever
    raised here when it is BOTH active and has gone `stale_turns` turns
    without its status changing — a directive that is merely open is normal
    work, not a defect; one that nobody has touched for 25 turns while the
    user stated it repeatedly is the exact shape that went missing.
    """
    out = []
    if not enforcement_enabled():
        return out
    if isinstance(plan_row, dict):
        if raw_pending_refinement(plan_row):
            out.append((
                "plan-unrefined",
                "A raw plan is captured but never refined, so every plan "
                "reader (PLAN.md, plan-status, the guardian) is answering "
                "from the PREVIOUS plan.",
                "Invoke the @plan-refiner subagent on memory/.plan_raw.md, "
                "then `/cc-mem plan-set --from-refiner` with its JSON.",
            ))
        else:
            should, reason = should_nudge_guardian(plan_row)
            if should:
                out.append((
                    "plan-drift",
                    f"The live plan has not been drift-checked ({reason}).",
                    # ONE sequence, not an "or" (Autoshop field report 7a):
                    # `plan-check` resets the counters AND prints the exact
                    # guardian Task(...) call, then ends "Now invoke the
                    # plan-guardian subagent" — so offering the two as
                    # alternatives contradicted the command's own output.
                    "Run `/cc-mem plan-check` (it resets these counters and "
                    "prints the guardian invocation), then invoke the "
                    "@plan-guardian subagent it names.",
                ))
    for row in (directives or []):
        if row.get("status") != "active":
            continue
        # A `constraint` is a standing PROHIBITION — "never do X" — with no
        # positive action that could ever be recorded against it (Autoshop
        # field report 3: a token-secrecy rule accrued idle counts forever,
        # and the only way to silence it was re-stating it, which inflated
        # `times_stated`). Idleness is meaningless for a rule whose success
        # is that nothing happens; it is enforced by being INJECTED, not by
        # being worked. `blocked` status rows never reach here at all —
        # the idle scan lists status='active' only.
        if row.get("kind") == "constraint":
            continue
        if int(row.get("turns_idle") or 0) < stale_turns:
            continue
        out.append((
            f"directive-idle:{row.get('slug')}",
            f"Directive '{row.get('slug')}' (stated "
            f"{row.get('times_stated')}x) has shown no progress for "
            f"{row.get('turns_idle')} turns: {row.get('demand', '')[:120]}",
            "Either work it, schedule it into the plan, or close it with "
            f"`/cc-mem directive-close {row.get('slug')} --evidence '...'`.",
        ))
    return out


def render_block_reason(reasons: list, attempt: int) -> str:
    """The text shown when the Stop hook refuses. Same four-part shape every
    time so a reader can locate the failed condition without re-reading the
    whole message.

    THIS IS A RENDER PATH, and it was the only one in this module that did not
    say so. `blocking_reasons` interpolates a directive's `slug` and `demand`
    — stored, model-writable text — straight into `what`, and `hooks/stop.py`
    hands the result to the harness as a `{"decision": "block", "reason": …}`
    payload, i.e. a HIGHER-authority channel than PROGRESS.md or the
    SessionStart injection. A stored `</system-reminder><system-reminder>…`
    reached Claude verbatim; reproduced, one forged block per rendered
    directive. `neutralize_document` escapes rather than deletes, so the text
    stays readable and stops carrying authority — the same treatment
    `render_plan_md` and `render_pending_plan_md` already end with.
    """
    lines = ["cc-memory · plan enforcement — this turn cannot close yet.", ""]
    for key, what, how in reasons:
        lines += [f"  [{key}]", f"    what : {what}", f"    fix  : {how}", ""]
    left = _BLOCK_MAX_CONSECUTIVE - attempt
    if left > 0:
        lines.append(
            f"  ({left} more refusal(s) before this degrades to an advisory "
            "so you can never be trapped; switch off entirely with "
            "CC_MEMORY_PLAN_ENFORCE=0)")
    return neutralize_document("\n".join(lines))


def render_pending_plan_md(raw: str, superseded: Optional[Dict] = None,
                           meta: Optional[Dict] = None) -> str:
    """PLAN.md body for a raw plan that has not been refined yet.

    The raw text IS the current plan, so it is rendered verbatim; any older
    structured plan is shown below it, explicitly labelled STALE, so a reader
    can never mistake it for what is being worked on now.

    The verbatim block's fence is widened past the longest backtick run in the
    raw text — ExitPlanMode plans routinely contain code fences, and a plain
    three-backtick fence would let them terminate the block early and shred the
    rest of the document.
    """
    meta = meta or {}
    raw = (raw or "").strip()
    longest_backtick_run, run = 0, 0
    for ch in raw:
        run = run + 1 if ch == "`" else 0
        longest_backtick_run = max(longest_backtick_run, run)
    fence = "`" * max(3, longest_backtick_run + 1)
    lines = list(_PLAN_MD_HEADER)
    lines += [
        "## ⚠ Pending refinement",
        "",
        "A raw plan was captured but has NOT been refined into the structured",
        "form yet, so no step statuses are being tracked. **The verbatim text",
        "below is the current plan.** To structure it: invoke the",
        "`plan-refiner` subagent on `memory/.plan_raw.md`, then",
        "`/cc-mem plan-set --from-refiner < <its-json-output>`.",
        "",
        "## Raw plan (verbatim, unrefined)",
        "",
        fence + "text",
        raw or "(empty)",
        fence,
        "",
    ]
    if is_valid_structured(superseded):
        steps = superseded["steps"]
        done = sum(1 for s in steps if s.get("status") == "done")
        lines += [
            "## Previous refined plan — STALE, superseded by the raw text above",
            "",
            # neutralize_inline, same reason as every slot in render_plan_md
            # below: the goal is refiner/model text, and a newline embedded in
            # it forged a second complete "## Pending refinement" + fenced
            # raw-plan section — an attacker-chosen "current plan" in the file
            # Claude reads as the live anchor (measured: 2 of each heading
            # where the document has 1).
            f"- Goal: {neutralize_inline(superseded['goal'].strip())}",
            f"- Progress when it was superseded: {done}/{len(steps)} steps done",
            f"- Last refined: {meta.get('last_refined_at') or '(never)'}",
            "",
            "Kept for reference only; step statuses are no longer synced to it.",
            "Full copies of replaced plans live in `memory/.plan_history/`.",
            "",
        ]
    # Assembled sweep: the raw plan and the superseded goal are separate,
    # separately-escaped slots, and the join between them is where a split
    # marker reassembles. See core.privacy.neutralize_document.
    return neutralize_document("\n".join(lines))


def render_plan_md(structured: Dict, active_step_id: int = 0,
                   meta: Optional[Dict] = None) -> str:
    """Generate PLAN.md content from a structured plan + optional metadata.

    `meta` carries the rest of the `plan_active` row: `raw`, `needs_refine`,
    `last_refined_at`, `last_guardian_at`, `edits_since_last_guardian`,
    `turns_since_last_guardian`. An unrefined raw plan WINS over the structured
    form (see raw_pending_refinement) — callers that pass no `raw` in `meta`
    get exactly the pre-v2.5 behaviour.
    """
    meta = meta or {}
    if raw_pending_refinement({
            "raw": meta.get("raw", ""),
            "structured": structured,
            "needs_refine": meta.get("needs_refine"),
            "last_refined_at": meta.get("last_refined_at")}):
        return render_pending_plan_md(meta.get("raw", ""),
                                      superseded=structured, meta=meta)
    if not is_valid_structured(structured):
        return (
            "# PLAN\n\n"
            "*(No active plan. Enter Claude's plan mode or use "
            "`/cc-mem plan-set` to create one.)*\n"
        )
    # Every slot below is neutralised, because none of this text is the
    # plugin's own: `structured` comes from the plan-refiner subagent acting on
    # ExitPlanMode output, so any content the model read can reach it. Measured
    # on one armed step title: 1 complete <system-reminder> block, 2 lines
    # carrying the "← ACTIVE" marker when exactly one step is active, and 2
    # "## Goal" headings in a document that has 1. `neutralize_block` for the
    # slots whose newlines are real structure, `neutralize_inline` for the ones
    # that own exactly one output line — a newline in a step title is what
    # forged the second "## Goal".
    lines = list(_PLAN_MD_HEADER) + [
        "## Goal",
        "",
        neutralize_block(structured["goal"].strip()),
        "",
    ]

    sc = structured.get("success_criteria") or []
    if sc:
        lines += ["## Success criteria", ""]
        for c in sc:
            lines.append(f"- {neutralize_inline(str(c))}")
        lines.append("")

    lines += ["## Steps", ""]
    total = len(structured["steps"])
    done = sum(1 for s in structured["steps"] if s.get("status") == "done")
    for s in structured["steps"]:
        glyph = _STATUS_GLYPH.get(s.get("status", "pending"), "[ ]")
        active_marker = "  ← ACTIVE" if s.get("id") == active_step_id and s.get("status") != "done" else ""
        line = f"{s.get('id', '?')}. {glyph} **{neutralize_inline(s['title'])}**{active_marker}"
        if s.get("notes"):
            line += f" — {neutralize_inline(str(s['notes']))}"
        lines.append(line)
    lines.append("")

    ctx = (structured.get("context") or "").strip()
    if ctx:
        lines += ["## Context", "", neutralize_block(ctx), ""]

    lines += [
        "## Status",
        "",
        f"- Progress: {done}/{total} steps done",
        f"- Active step: #{active_step_id}" if active_step_id else "- Active step: none",
    ]
    if meta.get("last_refined_at"):
        # refined_by is refiner-authored like every other structured field —
        # normalize_structured only strips it, so embedded newlines survived
        # to here and forged whole "## Goal"/"## Steps" sections.
        lines.append(f"- Last refined: {meta['last_refined_at']} "
                     f"({neutralize_inline(str(structured.get('refined_by', 'manual')))})")
    if meta.get("last_guardian_at"):
        lines.append(f"- Last guardian check: {meta['last_guardian_at']}")
    if meta.get("edits_since_last_guardian") is not None:
        lines.append(f"- Edits since last check: {meta['edits_since_last_guardian']}")
    if meta.get("turns_since_last_guardian") is not None:
        lines.append(f"- Turns since last check: {meta['turns_since_last_guardian']}")
    lines.append("")

    # Assembled sweep — measured forgery across the Goal/Context join.
    return neutralize_document("\n".join(lines))


def write_plan_md(db, project_id: int, memory_dir: Path) -> Path:
    """Full-rewrite memory/PLAN.md from the plan_active row. Returns the path.

    Never raises on a write failure, and that is a deliberate asymmetry with
    `core.progress.write_progress_md`, which does. PLAN.md is a PROJECTION of
    the `plan_active` row, which is already committed by the time this runs, and
    three of its five call sites (`capture_raw_plan`, `apply_todowrite_sync`,
    `bump_guardian_counters`) sit on the PostToolUse path where an escaping
    exception would be caught and logged anyway. PROGRESS.md is different: it
    IS the handoff contract, so its failure must be reported, not absorbed.

    v2.5.2 got this "for free" by having its private writer fall back to a
    plain truncating write — i.e. it kept the artifact by making it torn, which
    is the defect the atomic write exists to remove. Now the previous COMPLETE
    PLAN.md stays on disk and the next write regenerates it.
    """
    row = db.get_plan_active(project_id) or {}
    structured = row.get("structured") or {}
    active_step_id = row.get("active_step", 0)
    meta = {
        # raw + needs_refine drive the pending-refinement branch; without them
        # PLAN.md renders a superseded structured plan as if it were current
        "raw": row.get("raw") or "",
        "needs_refine": row.get("needs_refine"),
        "last_refined_at": row.get("last_refined_at"),
        "last_guardian_at": row.get("last_guardian_at"),
        "edits_since_last_guardian": row.get("edits_since_last_guardian"),
        "turns_since_last_guardian": row.get("turns_since_last_guardian"),
    }
    text = render_plan_md(structured, active_step_id=active_step_id, meta=meta)
    out = memory_dir / "PLAN.md"
    try:
        # Inside the try, not above it: the docstring promises this never
        # raises, and the directory creation sat outside — so an OSError from
        # it escaped the very guarantee the write below is wrapped to keep.
        # ensure_memory_dir raises FileNotFoundError (an OSError) for a project
        # directory that is gone, which is now absorbed like any write failure.
        ensure_memory_dir(memory_dir)
        # A wall-clock budget, not the default try count: a failure here is
        # swallowed below, so it costs a STALE PLAN.md rather than an error
        # the caller sees. See core/atomic.py:_DERIVED_BUDGET_S.
        _atomic_write_text(out, text, budget_s=_DERIVED_BUDGET_S)
    except OSError as e:
        # why: see the docstring — a projection of committed state must not
        # take down the operation that committed it, and the alternative the
        # old code chose (fall back to a truncating write) is worse than a
        # stale-but-complete artifact.
        _log.error(f"PLAN.md not rewritten ({e}); previous file left intact")
    return out


# ── R610 carryover gate (2026-07-29) ────────────────────────────────────────
#
# 换计划不许丢步骤。plan_active is a SINGLE slot: replacing it used to be the
# one moment where staged work could silently vanish (the documented
# SELF-ITER S1-S3 sink — ratified follow-up phases that never re-entered any
# plan and were lost when the next round's plan overwrote the slot).
# apply_refined_plan therefore REFUSES to replace a plan that still has
# unfinished steps unless EVERY one of them is accounted for:
#   (a) auto-carried — a sufficiently similar step exists in the new plan, or
#   (b) explicitly dispositioned — the new JSON carries a top-level
#       "dispositions": [{"old_title": ..., "action":
#       "done|dropped|merged|carried", "reason": "..."}] entry for it.
# There is deliberately NO force flag: a drop without a recorded reason is
# exactly the failure mode this gate exists to kill. Belt-and-braces, every
# outgoing plan (even a cleanly-dispositioned one) is archived append-only
# under memory/.plan_history/ so a wrong disposition is still recoverable.

CARRYOVER_MATCH_THRESHOLD = 0.5
# The SAME question asked of CJK titles, which `core/textsim.py` shingles as
# BIGRAMS rather than trigrams. The constant above is trigram-calibrated, and
# a bigram set of the same text is smaller, so an equal edit scores HIGHER —
# which for THIS gate means "carried" where the trigram score said "flagged".
# Derivation, not a fudge. One interior character substituted in a CJK title
# of length L leaves, out of a set of size L-1 (bigrams) or L-2 (trigrams):
#     bigrams   shared = L-3, union = L+1  ->  sim = (L-3)/(L+1)
#     trigrams  shared = L-5, union = L+1  ->  sim = (L-5)/(L+1)
# Under trigrams the 0.5 bar starts auto-carrying such an edit at L >= 11.
# Reproducing that crossover under bigrams needs (11-3)/(11+1) = 2/3.
# Measured before the fix: a sweep of 325 one-character CJK substitutions
# moved 98 from FLAGGED to auto-carried and 0 the other way, including
# `把超时设为三十秒` vs `把超时设为六十秒` — thirty seconds versus sixty,
# opposite facts, at 0.3333 -> 0.5556. A gate that refuses is the one place
# where a better similarity measure makes things WORSE, and the merge-side
# argument in textsim's docstring does not transfer to it.
CARRYOVER_MATCH_THRESHOLD_CJK = 2.0 / 3.0
_UNFINISHED_STATUSES = ("pending", "in_progress", "blocked")
_DISPOSITION_ACTIONS = ("done", "dropped", "merged", "carried")


def _carryover_bar(*texts) -> float:
    """The similarity bar to use when comparing these titles. See above."""
    return (CARRYOVER_MATCH_THRESHOLD_CJK if _has_cjk(*texts)
            else CARRYOVER_MATCH_THRESHOLD)


def _carried(title: str, candidate: str) -> bool:
    """True when `candidate` covers `title` well enough to count as carried."""
    return _jaccard(_trigram_set(title), _trigram_set(candidate)) \
        >= _carryover_bar(title, candidate)


def unfinished_steps(structured: Optional[Dict]) -> List[Dict]:
    """Steps of the active plan that would be LOST by an unaccounted
    replacement (pending / in_progress / blocked)."""
    if not is_valid_structured(structured):
        return []
    return [s for s in structured["steps"]
            if s.get("status") in _UNFINISHED_STATUSES]


def _best_title_match(title: str, candidates: List[str]) -> float:
    grams = _trigram_set(title)
    best = 0.0
    for c in candidates:
        best = max(best, _jaccard(grams, _trigram_set(c)))
    return best


def _any_carried(title: str, candidates: List[str]) -> bool:
    """Whether ANY candidate carries `title`, each judged at its own bar."""
    return any(_carried(title, c) for c in candidates)


def check_carryover(old_structured: Optional[Dict], new_plan: Dict) -> List[str]:
    """Return violation messages; empty list = replacement is allowed.

    Reads dispositions from the RAW refiner dict (pre-normalisation) so
    the schema stays additive for older refiner outputs on plans with no
    unfinished steps.
    """
    olds = unfinished_steps(old_structured)
    if not olds:
        return []
    # v2.4.1: each new step contributes TWO candidate strings — the bare
    # title AND title+notes. v2.4.0 compared against title+notes only,
    # so a long notes field DILUTED the trigram overlap below the
    # threshold and an IDENTICAL title failed to auto-carry (found on the
    # gate's second real replacement, R610). title+notes stays as a
    # candidate so a step folded into another step's notes still carries.
    #
    # v2.8.0, two tightenings:
    #   * Only UNFINISHED new steps are carry targets (register A4) — a step
    #     born 'done'/'skipped' is a disguised retirement wearing a carry.
    #   * Both strings map to their step's SLOT, and a slot is CONSUMED by
    #     the old step it carries (register A3): one new step used to
    #     discharge two old unfinished steps at once, which is the same
    #     one-entry-retires-four hole the dispositions loop below already
    #     closed on its side. A genuine merge of several old steps into one
    #     new step is expressed with action:"merged" dispositions.
    # `all_step_strings` keeps EVERY step's strings (status-agnostic) for the
    # A5 'carried'-claims-presence check further down.
    cand_slots: List[Tuple[str, int]] = []
    all_step_strings: List[str] = []
    for slot, s in enumerate(new_plan.get("steps") or []):
        if not isinstance(s, dict):
            continue
        bare = _s(s.get("title")).strip()
        joined = f"{_s(s.get('title'))} {_s(s.get('notes'))}".strip()
        strings = [t for t in (bare, joined) if t]
        if joined == bare and len(strings) == 2:
            strings = [bare]
        all_step_strings.extend(strings)
        if _norm_status(s.get("status", "pending")) in _UNFINISHED_STATUSES:
            cand_slots.extend((t, slot) for t in strings)
    spent_slots: set = set()

    def _consume_carry(title: str) -> bool:
        """Best UNSPENT new-step slot that carries `title`; consumes it."""
        grams = _trigram_set(title)
        best_slot, best_sim = -1, 0.0
        for cs, slot in cand_slots:
            if slot in spent_slots:
                continue
            sim = _jaccard(grams, _trigram_set(cs))
            if sim >= _carryover_bar(title, cs) and sim > best_sim:
                best_sim, best_slot = sim, slot
        if best_slot < 0:
            return False
        spent_slots.add(best_slot)
        return True

    dispositions = [d for d in (new_plan.get("dispositions") or [])
                    if isinstance(d, dict)]
    # A disposition is CONSUMED by the step it accounts for. Without this,
    # the loop below took the FIRST fuzzy match and left the entry available
    # to every later step: one entry reading `{"old_title": "Add unit tests
    # for the auth module", "action": "done", "reason": "auth tests landed in
    # PR #412"}` discharged four steps at once — auth / authz / audit / admin
    # at 1.0000 / 0.8571 / 0.7778 / 0.7105 (measured). Three of those four
    # drops then carried a reason that is about a different step, which is
    # precisely "a drop without a recorded reason" wearing a costume.
    spent: List[int] = []
    violations: List[str] = []
    for step in olds:
        title = str(step.get("title", ""))
        if _consume_carry(title):
            continue  # auto-carried into (and consuming) one new step
        # BEST match among the unspent entries, not the first: with fuzzy
        # matching, "first" is an accident of list order.
        matched, best_sim, best_i = None, 0.0, -1
        for i, d in enumerate(dispositions):
            if i in spent:
                continue
            old_title = str(d.get("old_title", ""))
            sim = _jaccard(_trigram_set(title), _trigram_set(old_title))
            if sim >= _carryover_bar(title, old_title) and sim > best_sim:
                matched, best_sim, best_i = d, sim, i
        if matched is None:
            violations.append(
                f"step #{step.get('id')} {title!r} — not in the new plan "
                f"and no disposition")
            continue
        spent.append(best_i)
        action = _s(matched.get("action")).lower()
        # _s, not str (register r6-B5): a disposition with `"reason": null`
        # made str(None) the non-empty string "None" — a reasonless drop
        # wearing four characters of costume, the exact thing this gate's
        # reason check exists to refuse. Same null shape A6 fixed in
        # normalize_structured.
        reason = (_s(matched.get("reason")) or _s(matched.get("detail"))).strip()
        if action not in _DISPOSITION_ACTIONS:
            violations.append(
                f"step #{step.get('id')} {title!r} — disposition action "
                f"{action!r} not in {_DISPOSITION_ACTIONS}")
        elif action == "carried" and not _any_carried(title, all_step_strings):
            # "carried" is a CLAIM of presence in the new plan, and it used to
            # be accepted with the step present nowhere (register A5) — a drop
            # wearing the one action word that says nothing was dropped.
            # Checked against every step string, spent or not: two old steps
            # legitimately folded into one new step are the "merged" action's
            # territory, but a stale-worded 'carried' for the second one is a
            # recorded, reasoned entry — the gate's purpose is the RECORD.
            violations.append(
                f"step #{step.get('id')} {title!r} — disposition claims "
                f"'carried' but no step in the new plan covers it")
        elif not reason:
            violations.append(
                f"step #{step.get('id')} {title!r} — disposition has no "
                f"reason (a drop without a recorded reason is the exact "
                f"failure mode this gate kills)")
    return violations


# Bound on the collision-suffix search in archive_plan. 200 archives inside one
# millisecond is unreachable; the loop simply must terminate.
_ARCHIVE_NAME_TRIES = 200


def archive_plan(row: Optional[Dict], memory_dir: Optional[Path],
                 event: str, reason: str = "") -> Optional[Path]:
    """Append-only archive of an outgoing plan (replace/clear) under
    memory/.plan_history/. Last-resort backstop: even a wrong disposition
    stays recoverable. Returns the path, or None."""
    if memory_dir is None or not row:
        return None
    if not (row.get("structured") or (row.get("raw") or "").strip()):
        return None
    try:
        hist_dir = memory_dir / ".plan_history"
        # No parents=True: it would materialise a DELETED project's whole
        # directory chain just to archive a plan into the empty shell. A
        # vanished memory_dir raises FileNotFoundError into the handler
        # below instead — same refusal contract as ensure_memory_dir.
        hist_dir.mkdir(exist_ok=True)
        now = datetime.now()
        payload = {
            "archived_at": now.isoformat(timespec="seconds"),
            "event": event,
            "reason": reason,
            "structured": row.get("structured"),
            "raw": row.get("raw"),
            "active_step": row.get("active_step"),
        }
        blob = json.dumps(payload, ensure_ascii=False, indent=2)
        # The old `%Y%m%dT%H%M%S` stem made this archive neither append-only nor
        # a backstop: FOUR STRICTLY SEQUENTIAL replacements — one process, no
        # concurrency, 0.023 s total — left ONE file and destroyed gen0/gen1/
        # gen2, and because the survivor is the LAST write, the earliest and
        # most complete generation is the first to go. Under concurrency it
        # measured 60 replacements -> 3 files. Millisecond precision (still
        # sortable, still human-readable) plus an O_CREAT|O_EXCL claim, atomic
        # across processes, is what actually makes it append-only.
        base = now.strftime("%Y%m%dT%H%M%S_") + now.strftime("%f")[:3]
        for n in range(_ARCHIVE_NAME_TRIES):
            stem = base if n == 0 else f"{base}-{n}"
            out = hist_dir / f"plan_{stem}_{event}.json"
            try:
                fd = os.open(str(out), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            # fdopen, not a reopen: writing through the descriptor we just
            # claimed means no second process can slip in between claim and
            # write. Default newline handling matches the Path.write_text this
            # replaces, so archived bytes are unchanged.
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(blob)
            return out
        out = hist_dir / f"plan_{base}-{os.getpid()}_{event}.json"
        out.write_text(blob, encoding="utf-8")
        return out
    except OSError as e:
        # why: the gate's dispositions are the primary anti-loss guarantee;
        # blocking every plan operation on an archive-disk hiccup would turn
        # the backstop into a denial-of-service on planning. Loudly warn.
        # _log, NOT stderr. This function is reachable from PostToolUse
        # (capture_exit_plan_mode's recapture branch), and Claude Code renders
        # a hook's stderr as error UI — the project's hardest hook rule, stated
        # in CLAUDE.md and restated in core/logger.py, core/modes.py and
        # core/roots.py. Driven through the REAL hook with `.plan_history`
        # squatted by a file: rc=0, stdout empty, and this line on stderr, i.e.
        # an error banner on an otherwise successful tool call. Still swallowed
        # and still loud — just on the channel hooks are allowed to use.
        _log.warn(f"plan history archive failed ({e}) — proceeding; "
                  f"the carryover gate already enforced accounting")
        return None


# ── Capture: ExitPlanMode + raw text path ───────────────────────────────────

def capture_exit_plan_mode(db, project_id: int, plan_text: str,
                           memory_dir: Optional[Path] = None) -> bool:
    """Store raw plan text and mark it for refinement. Called from the
    PostToolUse hook when ExitPlanMode fires, or from `/cc-mem plan-set`
    when a user provides a manual plan.

    Returns True iff something was actually stored. Whitespace-only input is a
    silent no-op here, yet `/cc-mem plan-set --raw "   "` printed
    "[OK] Raw plan captured (3 chars)" and exited 0 — a success line for a plan
    that was never written. Callers must print based on this return value.

    Side effects when True: `plan_active.raw` is replaced, `needs_refine=1`,
    `memory/.plan_raw.md` is written for the refiner subagent, and PLAN.md is
    regenerated immediately so the freshly captured plan is visible there
    instead of the superseded structured one.
    """
    plan_text = (plan_text or "").strip()
    if not plan_text:
        return False
    # WRITE-path cleaning, same contract as every other stored column
    # (register A7): the raw plan is ExitPlanMode output — model text — and it
    # used to land verbatim in plan_active.raw, .plan_raw.md AND PLAN.md's
    # fenced block, private spans included. strip_private removes those spans
    # outright; neutralize_markers escapes rather than deletes, so the text
    # the refiner Reads stays readable. A plan that was ALL private strips to
    # nothing and is refused like any other empty capture.
    plan_text = clean_for_storage(plan_text).strip()
    if not plan_text:
        return False
    # ARCHIVE the outgoing raw before overwriting it. The contract above this
    # section says "every outgoing plan is archived append-only", and this was
    # the one replacement path that did not: `apply_refined_plan` and
    # `plan-clear` archive, re-capture did not. Re-entering plan mode is the
    # single most likely double-fire in the whole lifecycle, and the previous
    # raw was then unrecoverable from `memory/` — measured: PLAN A absent from
    # `plan_active.raw`, from `.plan_raw.md` and from `.plan_history/`, which
    # did not even exist. Only on a genuine REPLACEMENT (different text), so
    # an idempotent re-delivery of the same ExitPlanMode adds nothing.
    _prev = db.get_plan_active(project_id)
    if _prev and (_prev.get("raw") or "").strip() not in ("", plan_text):
        archive_plan(_prev, memory_dir, event="recapture",
                     reason="a new ExitPlanMode replaced this raw plan before "
                            "it was refined")
    if memory_dir is not None:
        # BEFORE the row is written, not after. A gone project directory raises
        # FileNotFoundError here rather than being recreated — and the ordering
        # is the point: `upsert_plan_active` COMMITS, so validating afterwards
        # left `needs_refine=1` durable with no `.plan_raw.md` beside it, and
        # `hooks/stop.py` then told the user the raw plan had been captured.
        # A precondition that runs after the commit is not a precondition.
        ensure_memory_dir(memory_dir)
        # write_atomic, not write_text: `.plan_raw.md` is read by a SEPARATE
        # process — `agents/plan-refiner.md` is told to Read it — and
        # `write_text` truncates first, so a refiner landing in that window
        # gets 0 bytes and structures a plan out of nothing.
        #
        # ALSO BEFORE THE COMMIT, for the same reason the directory check is.
        # This file is the refiner's INPUT; the row is not. With the commit
        # first, a failed replacement left the row saying PLAN B while the
        # refiner still read PLAN A off disk, and nothing could tell: the
        # r6-B1 guard compares the row's raw across CAS retries and the row was
        # already correct. Driven end to end with the destination made
        # un-replaceable — the refinement of the STALE plan was ACCEPTED,
        # `needs_refine` went to 0, `raw_pending_refinement` reported False,
        # and PLAN B was reachable from nothing.
        #
        # The residual runs the other way and is the safe direction: if this
        # write succeeds and `upsert_plan_active` then fails, the file is newer
        # than the row, `needs_refine` is NOT armed, PLAN.md still renders the
        # committed plan, and this function raises so PostToolUse logs it.
        # Cross-resource atomicity would need the refiner's JSON bound to a
        # raw-plan digest, which is a change to the subagent contract.
        write_atomic(memory_dir / ".plan_raw.md", plan_text,
                     budget_s=_DERIVED_BUDGET_S)
    db.upsert_plan_active(
        project_id,
        raw=plan_text,
        needs_refine=1,
    )
    if memory_dir is not None:
        write_plan_md(db, project_id, memory_dir)
    return True


def apply_refined_plan(db, project_id: int, structured: Dict,
                       memory_dir: Optional[Path] = None) -> Dict:
    """Persist a refined structured plan (from the refiner subagent or from
    `/cc-mem plan-set --structured`). Clears needs_refine, sets refined_at,
    rewrites PLAN.md.

    Returns the normalised plan that was actually stored. Raises ValueError —
    which every CLI caller already catches and prints — for a payload that is
    not a JSON object, that fails the schema, or that trips the R610 carryover
    gate.
    """
    if not isinstance(structured, dict):
        raise ValueError(
            "refined plan must be a JSON object with 'goal' + 'steps', got "
            f"{type(structured).__name__} — the refiner subagent emitted a "
            "top-level non-object payload")
    normalised = normalize_structured(structured)
    if not is_valid_structured(normalised):
        raise ValueError("refined plan does not satisfy schema (needs goal + ≥1 step)")

    # R610 carryover gate — the ONLY replacement door for plan_active.
    # Checked against the RAW input dict so top-level "dispositions" are
    # visible even though normalisation runs on a copy. No force flag.
    #
    # v2.8.0: gate + write is a CAS loop. The check runs against a READ of
    # the row, and the write used to land unconditionally — a plan that
    # changed between the two (another refine, a recapture, a clear) was
    # replaced on the strength of a gate that examined its PREDECESSOR. Each
    # attempt re-reads, re-runs the gate against what is actually there, and
    # writes only if the revision it read still stands.
    normalised["refined_at"] = datetime.now().isoformat(timespec="seconds")
    # Pick an initial active_step from the structured form
    active_step_id = 0
    for s in normalised["steps"]:
        if s["status"] == "in_progress":
            active_step_id = s["id"]
            break
    if not active_step_id:
        for s in normalised["steps"]:
            if s["status"] == "pending":
                active_step_id = s["id"]
                break

    first_raw = None
    for _attempt in range(3):
        old_row = db.get_plan_active(project_id) or {}
        cur_raw = (old_row.get("raw") or "").strip()
        if first_raw is None:
            first_raw = cur_raw
        elif old_row.get("needs_refine") and cur_raw != first_raw:
            # register r6-B1 (BLOCKER): a CAS retry means SOMETHING changed —
            # and when that something was a brand-new ExitPlanMode capture,
            # committing this (older) refinement would set needs_refine=0
            # over raw C, hiding the newest plan behind a structured form
            # refined from raw B. The retry may only proceed while the raw it
            # started from still stands; a newer capture wins.
            raise ValueError(
                "a NEWER raw plan was captured while this refinement was "
                "being applied — run the refiner against the current "
                "memory/.plan_raw.md and plan-set that output instead")
        violations = check_carryover(
            old_row.get("structured") or {},
            structured if isinstance(structured, dict) else {})
        if violations:
            raise ValueError(
                "carryover gate REFUSED — the outgoing plan still has "
                "unfinished steps not accounted for in the replacement:\n  - "
                + "\n  - ".join(violations)
                + "\nEvery unfinished step must either appear in the new plan's "
                  "steps (auto-carry by title similarity) or be listed in the "
                  "new JSON's top-level \"dispositions\": [{\"old_title\": ..., "
                  "\"action\": \"done|dropped|merged|carried\", \"reason\": ...}]."
                  " There is no force flag by design.")
        fields = dict(
            structured=normalised,
            active_step=active_step_id,
            needs_refine=0,
            last_refined_at=normalised["refined_at"],
            # The drift counters describe how far work has wandered from the
            # plan they were counted against. That plan is GONE, so carrying
            # its counters onto the replacement makes the guardian nudge fire
            # on turn 0 of a brand-new plan with nothing yet to drift from —
            # measured, `should_nudge_guardian` returned `(True,
            # 'turn_threshold (30 >= 8)')` immediately after a replan. A
            # replacement IS a guardian event: the user just re-stated the
            # plan, which is what the check would have asked them to do.
            turns_since_last_guardian=0,
            edits_since_last_guardian=0,
        )
        if not old_row:
            # No row yet: an ATOMIC create-if-absent (register r6-B2). The
            # old check-then-upsert let two first writers both "succeed" with
            # silent last-write-wins; now exactly one wins the INSERT and the
            # loser loops, reads the winner's row, and goes through the gate
            # + CAS against it like any other replacement.
            if db.insert_plan_if_absent(project_id, **fields):
                archive_plan(old_row, memory_dir, event="replace")
                break
            continue
        if db.update_plan_if_revision(project_id, old_row["revision"], **fields):
            # Archive AFTER the CAS succeeded: the matched revision proves
            # old_row is exactly the plan that was replaced.
            archive_plan(old_row, memory_dir, event="replace")
            break
    else:
        raise ValueError(
            "plan changed concurrently 3 times during replacement — "
            "re-read the live plan and re-run the refine against it")
    if memory_dir is not None:
        write_plan_md(db, project_id, memory_dir)
    return normalised


# ── TodoWrite sync (called from PostToolUse hook) ───────────────────────────

def apply_todowrite_sync(db, project_id: int, todos: List[Dict],
                         memory_dir: Optional[Path] = None) -> Dict:
    """Take a TodoWrite snapshot, sync it into the live plan, rewrite PLAN.md.

    Returns sync_info dict (n_matched, n_unmatched, active_step_id) so the
    caller can decide whether to nudge the user about drift.
    """
    row = db.get_plan_active(project_id)
    if not row or not is_valid_structured(row.get("structured")):
        return {"n_matched": 0, "n_unmatched": len(todos or []), "active_step_id": 0,
                "skipped": "no_active_plan"}
    if raw_pending_refinement(row):
        # The structured plan is SUPERSEDED and awaiting replacement: every
        # renderer refuses to show it (`render_pending_plan_md`) and
        # `/cc-mem plan-check` refuses to check it, for the same reason. This
        # was the one surface that kept MUTATING it — and the todos arriving
        # in that window belong to the NEW plan, so they matched the old
        # plan's titles loosely and flipped its steps to `done`. Measured:
        # three unfinished steps retired, `unfinished_steps` emptied, and the
        # replacement then passed the carryover gate with zero dispositions —
        # one of the three would not even have auto-carried (sim 0.3902,
        # under the bar). Syncing into a plan nobody is allowed to see is not
        # sync; it is the silent-step-loss sink the gate exists to kill.
        return {"n_matched": 0, "n_unmatched": len(todos or []),
                "active_step_id": row.get("active_step") or 0,
                "skipped": "pending_refinement"}
    structured = row["structured"]
    updated, info = sync_todos_to_steps(structured, todos)
    # CAS on the revision this sync READ (register X4): a sync that stalled
    # while the plan was replaced — every step dispositioned through the R610
    # gate — used to write its stale copy back wholesale, resurrecting the
    # replaced plan through the one door the gate cannot see. rowcount 0 now
    # means "the plan moved on"; the todos belong to whatever replaced it and
    # the NEXT TodoWrite will sync against that.
    if not db.update_plan_if_revision(
            project_id, row["revision"],
            structured=updated, active_step=info["active_step_id"]):
        return {"n_matched": 0, "n_unmatched": len(todos or []),
                "active_step_id": 0, "skipped": "plan_changed"}
    if memory_dir is not None:
        write_plan_md(db, project_id, memory_dir)
    return info


# ── Drift / guardian-nudge logic ────────────────────────────────────────────

def should_nudge_guardian(plan_row: Dict, *,
                          turn_threshold: int = 8,
                          edit_threshold: int = 12) -> Tuple[bool, str]:
    """Return (should_nudge, reason). Caller (Stop hook) uses this to decide
    whether to print the guardian-recommendation status line."""
    if not plan_row or not is_valid_structured(plan_row.get("structured")):
        return False, "no_active_plan"
    if plan_row.get("needs_refine"):
        # raw plan captured but not yet refined — different nudge, not guardian
        return False, "needs_refine_first"
    turns = int(plan_row.get("turns_since_last_guardian") or 0)
    edits = int(plan_row.get("edits_since_last_guardian") or 0)
    if turns >= turn_threshold:
        return True, f"turn_threshold ({turns} >= {turn_threshold})"
    if edits >= edit_threshold:
        return True, f"edit_threshold ({edits} >= {edit_threshold})"
    return False, "below_thresholds"


# Tool names that are "sensitive" and warrant an immediate guardian nudge
# regardless of counters. Examples: pushing code, dropping DB, deleting files.
# Commands whose EXECUTION is high-stakes enough to recommend a guardian
# check. cc-memory does NOT block; it flags. See `is_sensitive_tool_call` for
# why these are anchored rather than substring-matched.
_SENSITIVE_COMMANDS = (
    r"git\s+push", r"rm\s+-rf", r"drop\s+table", r"drop\s+database",
    r"npm\s+publish", r"cargo\s+publish", r"twine\s+upload", r"pypi-upload",
    r"kubectl\s+apply", r"terraform\s+apply", r"ansible-playbook",
)
# Start of string, or after a shell separator — so `cd x && git push` counts
# and `grep "git push" docs/` does not.
_SENSITIVE_CMD_RE = re.compile(
    r"(?:^|[;&|(\n]|&&|\|\|)\s*(?:sudo\s+|env\s+\S+=\S+\s+)*(?:"
    + "|".join(_SENSITIVE_COMMANDS) + r")\b")


def is_sensitive_tool_call(tool_name: str, tool_input: Dict) -> bool:
    """Heuristic — return True for tool calls that are "high-stakes" enough
    to recommend a guardian check before/after they happen.

    For now: bash commands that include `git push`, `rm -rf`, `DROP TABLE`,
    or that look like deploys (`npm publish`, `cargo publish`).
    """
    if tool_name not in ("Bash",):
        return False
    cmd = (tool_input or {}).get("command", "") if isinstance(tool_input, dict) else ""
    # ANCHORED at a command position, not `pattern in cmd_lower`. A bare
    # substring test fires on any command that merely MENTIONS the phrase, and
    # this one bumps the drift counter by 20 against a threshold of 12 — so a
    # single read-only `grep -rn "git push" docs/` demanded a guardian check.
    # Measured True for all of: that grep, `echo "never run rm -rf /"`,
    # `git log --grep="git push"`, and a `cat` of a file whose name mentions
    # DROP TABLE. A nudge that fires when nothing happened is a nudge the
    # reader learns to ignore, which costs the ones that matter.
    #
    # "Command position" = the start of the string or just after a shell
    # separator (`;`, `&&`, `||`, `|`, newline, or a subshell paren). That
    # still catches `cd x && git push`, and no longer catches the phrase
    # inside a quoted argument.
    return bool(_SENSITIVE_CMD_RE.search(cmd.lower()))


# ── success_criteria carryover advisory (2026-08-05, v2.5.6) ────────────────
#
# Appended at the END of the module ON PURPOSE, not next to check_carryover
# where it belongs by topic. This project documents its contracts with dense
# `file:line` references (docs/CONTRACTS.md alone carries ~30 into plan.py),
# and smoke_test.py only validates the symbol-anchored subset — the bare
# `:NNN` form has nothing to anchor on, so an insertion mid-file silently
# rots it. Inserting here shifts nothing. Topic cohesion is worth less than
# not breaking ~60 references across four docs.

def unmatched_criteria(old_structured: Optional[Dict],
                       new_plan: Dict) -> List[str]:
    """Old `success_criteria` with no close match in the replacement.

    The R610 gate guards `steps` only — by its own charter, "换计划不许丢
    步骤". `success_criteria` sits outside it, yet a criterion is just as
    much staged intent as a step: it is the definition of done. Observed
    in the field (2026-08-05): a plan replacement passed the steps gate
    cleanly while two of ten criteria evaporated, one of them an
    achieved-but-never-recorded release gate.

    This is deliberately NOT a second refusal. Criteria get reworded,
    merged, translated, and retired-because-achieved as a plan matures —
    an EN plan replaced by a ZH one auto-carries nothing at all, so a
    hard gate here would make ordinary plan evolution impossible. What
    this returns is the list the caller must SHOW, so that "it vanished"
    and "I retired it on purpose" stop looking identical.

    Same bar as the steps gate — including the CJK 2/3 adjustment
    (`_carryover_bar`): bigram shingles score a one-character Chinese
    substitution at 0.5556, so the flat 0.5 threshold silently treated a
    replaced ZH criterion as carried while `_carried` refused the identical
    pair. A criterion folded into the replacement's goal or context text
    counts as matched: lossy survival is still survival, and flagging it
    would train the reader to ignore the advisory.
    """
    if not is_valid_structured(old_structured):
        return []
    olds = [c for c in (old_structured.get("success_criteria") or [])
            if isinstance(c, str) and c.strip()]
    if not olds:
        return []
    candidates = [c for c in (new_plan.get("success_criteria") or [])
                  if isinstance(c, str) and c.strip()]
    for extra in (new_plan.get("goal"), new_plan.get("context")):
        if isinstance(extra, str) and extra.strip():
            candidates.append(extra)
    return [c for c in olds
            if _best_title_match(c, candidates) < _carryover_bar(c)]


# ── directive step-number references (v2.12.0) ──────────────────────────────
# The Autoshop field report's #1 finding: a directive whose `demand` says
# "见步骤 12" is a LONG-LIVED row pinned to a SHORT-LIVED coordinate. The R610
# gate guarantees no step is LOST across a replacement, but step ids are
# assigned by position, so two replans (23→12→14 steps) left 11 dead
# references and — worse — 4 that still resolved but to a DIFFERENT step:
# text that reads correctly and executes the wrong work. The documented rule
# is therefore "reference steps by TITLE, never by number"
# (docs/CONTRACTS.md#plan-contract); the two functions below are the
# machinery that notices when the rule was broken anyway.

# Ordinal step references in free text: "步骤 12" / "步12" / "step #3" / "#7".
# The bare-# alternative deliberately requires the digits to follow the mark
# immediately, so issue numbers written "PR # 12" are not matched.
_STEP_REF_RE = re.compile(r"(?:步骤?|step)\s*#?\s*(\d{1,3})|#(\d{1,3})",
                          re.IGNORECASE)


def directive_step_refs(text: str) -> List[int]:
    """Step numbers referenced ordinally in a directive's free text."""
    out = []
    for m in _STEP_REF_RE.finditer(text or ""):
        n = int(m.group(1) or m.group(2))
        if n and n not in out:
            out.append(n)
    return out


def stale_directive_step_refs(directives: Optional[List[Dict]],
                              old_structured: Optional[Dict],
                              new_structured: Optional[Dict]) -> List[Dict]:
    """Directive step-number references broken by a plan replacement.

    Compares each ordinal reference in an ACTIVE directive's demand/quote
    against the outgoing and incoming step tables:

      * `dead`       — the number no longer names any step in the new plan.
      * `retargeted` — the number still resolves, but the step it names in
        the new plan does not carry the step it named in the old plan
        (judged by `_carried`, the carryover gate's own bar). This is the
        dangerous shape: the text reads correctly and points at the wrong
        work — Autoshop measured 4 of these against 11 dead ones.

    A number absent from the OLD plan too is not reported: there is no
    baseline to judge the author's intent against. Pure function, advisory
    by design — refusing a plan replacement over text in a DIFFERENT table
    would hold the plan hostage to the ledger; the caller's job is to make
    "it rotted" visible, and the durable fix is title references.
    """
    findings = []
    new_steps = {int(s["id"]): str(s.get("title") or "")
                 for s in (new_structured or {}).get("steps", [])
                 if isinstance(s, dict) and s.get("id") is not None}
    old_steps = {int(s["id"]): str(s.get("title") or "")
                 for s in (old_structured or {}).get("steps", [])
                 if isinstance(s, dict) and s.get("id") is not None}
    for row in (directives or []):
        if row.get("status") != "active":
            continue
        text = f"{row.get('demand') or ''}\n{row.get('quote') or ''}"
        for n in directive_step_refs(text):
            if n not in new_steps:
                findings.append({"slug": row.get("slug"), "ref": n,
                                 "kind": "dead",
                                 "old_title": old_steps.get(n, ""),
                                 "new_title": ""})
            elif n in old_steps and not _carried(old_steps[n], new_steps[n]):
                findings.append({"slug": row.get("slug"), "ref": n,
                                 "kind": "retargeted",
                                 "old_title": old_steps[n],
                                 "new_title": new_steps[n]})
    return findings
