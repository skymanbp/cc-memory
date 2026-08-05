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
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Render-path marker defence. PLAN.md is read by Claude as the live plan
# anchor, and every field it renders originates outside the plugin.
from core.privacy import neutralize_block, neutralize_inline


# ── Atomic artifact write ───────────────────────────────────────────────────

def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` without ever exposing a truncated file.

    ``Path.write_text`` truncates before it writes, so a reader landing in that
    window gets 0 bytes — measured 456 empty reads in 13,594 samples with ONE
    writer and one reader. PLAN.md is a live anchor another window's Claude
    reads at will, and an empty read is indistinguishable from "no active
    plan": silent, no error, wrong. ``os.replace`` is atomic on POSIX and
    Windows alike; it is already the idiom used for the smaller state files
    (hooks/session_start.py:310, hooks/pre_compact.py:_write_attempt).

    DELIBERATE literal twin in llm/memory_writer.py (which writes MEMORY.md):
    the two live in different subpackages and `core` must not depend on `llm`,
    so a private copy on each side is cheaper than the cross-package import.
    Same convention as the memory/.gitignore literals noted in CLAUDE.md — if
    you change one, change the other.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(path))
        return
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            # why: removing our own temp file is best-effort — memory/.gitignore
            # already carries `*.tmp`, so a leftover never reaches a user's repo
            pass
    # why no re-raise: on Windows os.replace onto a destination another process
    # currently holds open fails with ERROR_SHARING_VIOLATION. Falling back to
    # the plain truncating write preserves the pre-v2.5.2 guarantee that the
    # artifact is ALWAYS written; it forfeits atomicity for that one call only.
    path.write_text(text, encoding="utf-8")


# ── Similarity (re-uses the trigram-Jaccard from memory_writer) ─────────────

def _trigram_set(text: str) -> set:
    t = (text or "").lower().strip()
    if len(t) < 3:
        return {t} if t else set()
    return {t[i:i + 3] for i in range(len(t) - 2)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Match threshold: a todo whose closest step similarity is below this is
# considered "off-plan" and counted as a drift signal.
MATCH_THRESHOLD = 0.35


# ── Schema validation ───────────────────────────────────────────────────────

_VALID_STATUSES = ("pending", "in_progress", "done", "blocked", "skipped")


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
        "goal": str(plan.get("goal", "")).strip(),
        "success_criteria": [],
        "steps": [],
        "context": str(plan.get("context", "")).strip(),
        "refined_at": plan.get("refined_at") or datetime.now().isoformat(timespec="seconds"),
        "refined_by": plan.get("refined_by", "plan-refiner"),
    }
    sc = plan.get("success_criteria", [])
    if isinstance(sc, list):
        out["success_criteria"] = [str(x).strip() for x in sc if str(x).strip()]

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
            title = str(s.get("title", "")).strip()
            if not title:
                continue
            status = s.get("status", "pending")
            if status not in _VALID_STATUSES:
                # tolerate common LLM aliases
                aliases = {"todo": "pending", "wip": "in_progress",
                           "complete": "done", "completed": "done",
                           "doing": "in_progress"}
                status = aliases.get(str(status).lower(), "pending")
            out["steps"].append({
                "id": int(s.get("id", i)),
                "title": title,
                "status": status,
                "notes": str(s.get("notes", "")).strip(),
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
        steps[step_idx]["status"] = new_status
        if new_status == "in_progress" and not active_step_id:
            active_step_id = steps[step_idx].get("id", step_idx + 1)

    # If nothing's in_progress, the next pending step is the active one
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
            f"- Goal: {superseded['goal'].strip()}",
            f"- Progress when it was superseded: {done}/{len(steps)} steps done",
            f"- Last refined: {meta.get('last_refined_at') or '(never)'}",
            "",
            "Kept for reference only; step statuses are no longer synced to it.",
            "Full copies of replaced plans live in `memory/.plan_history/`.",
            "",
        ]
    return "\n".join(lines)


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
        lines.append(f"- Last refined: {meta['last_refined_at']} ({structured.get('refined_by', 'manual')})")
    if meta.get("last_guardian_at"):
        lines.append(f"- Last guardian check: {meta['last_guardian_at']}")
    if meta.get("edits_since_last_guardian") is not None:
        lines.append(f"- Edits since last check: {meta['edits_since_last_guardian']}")
    if meta.get("turns_since_last_guardian") is not None:
        lines.append(f"- Turns since last check: {meta['turns_since_last_guardian']}")
    lines.append("")

    return "\n".join(lines)


def write_plan_md(db, project_id: int, memory_dir: Path) -> Path:
    """Full-rewrite memory/PLAN.md from the plan_active row. Returns the path."""
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
    memory_dir.mkdir(parents=True, exist_ok=True)
    out = memory_dir / "PLAN.md"
    _atomic_write_text(out, text)
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
_UNFINISHED_STATUSES = ("pending", "in_progress", "blocked")
_DISPOSITION_ACTIONS = ("done", "dropped", "merged", "carried")


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
    new_titles: List[str] = []
    for s in (new_plan.get("steps") or []):
        if isinstance(s, dict):
            bare = str(s.get("title", "")).strip()
            if bare:
                new_titles.append(bare)
            t = f"{s.get('title', '')} {s.get('notes', '')}".strip()
            if t and t != bare:
                new_titles.append(t)
    dispositions = new_plan.get("dispositions") or []
    violations: List[str] = []
    for step in olds:
        title = str(step.get("title", ""))
        if _best_title_match(title, new_titles) >= CARRYOVER_MATCH_THRESHOLD:
            continue  # auto-carried into the new plan
        matched = None
        for d in dispositions:
            if not isinstance(d, dict):
                continue
            if _jaccard(_trigram_set(title),
                        _trigram_set(str(d.get("old_title", "")))) \
                    >= CARRYOVER_MATCH_THRESHOLD:
                matched = d
                break
        if matched is None:
            violations.append(
                f"step #{step.get('id')} {title!r} — not in the new plan "
                f"and no disposition")
            continue
        action = str(matched.get("action", "")).lower()
        reason = str(matched.get("reason", "")
                     or matched.get("detail", "")).strip()
        if action not in _DISPOSITION_ACTIONS:
            violations.append(
                f"step #{step.get('id')} {title!r} — disposition action "
                f"{action!r} not in {_DISPOSITION_ACTIONS}")
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
        hist_dir.mkdir(parents=True, exist_ok=True)
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
        import sys as _sys
        print(f"[WARN] plan history archive failed ({e}) — proceeding; "
              f"the carryover gate already enforced accounting",
              file=_sys.stderr)
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
    db.upsert_plan_active(
        project_id,
        raw=plan_text,
        needs_refine=1,
    )
    if memory_dir is not None:
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / ".plan_raw.md").write_text(plan_text, encoding="utf-8")
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
    old_row = db.get_plan_active(project_id) or {}
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
    archive_plan(old_row, memory_dir, event="replace")

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

    db.upsert_plan_active(
        project_id,
        structured=normalised,
        active_step=active_step_id,
        needs_refine=0,
        last_refined_at=normalised["refined_at"],
    )
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
    structured = row["structured"]
    updated, info = sync_todos_to_steps(structured, todos)
    db.upsert_plan_active(
        project_id,
        structured=updated,
        active_step=info["active_step_id"],
    )
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
SENSITIVE_TOOLS = {
    # cc-memory does NOT block; it just flags. The Stop hook reads this set
    # to surface a guardian-recommendation status line proactively.
}


def is_sensitive_tool_call(tool_name: str, tool_input: Dict) -> bool:
    """Heuristic — return True for tool calls that are "high-stakes" enough
    to recommend a guardian check before/after they happen.

    For now: bash commands that include `git push`, `rm -rf`, `DROP TABLE`,
    or that look like deploys (`npm publish`, `cargo publish`).
    """
    if tool_name not in ("Bash",):
        return False
    cmd = (tool_input or {}).get("command", "") if isinstance(tool_input, dict) else ""
    cmd_lower = cmd.lower()
    sensitive_patterns = (
        "git push", "git push -f", "git push --force",
        "rm -rf", "drop table", "drop database",
        "npm publish", "cargo publish", "pypi-upload", "twine upload",
        "kubectl apply", "terraform apply", "ansible-playbook",
    )
    return any(p in cmd_lower for p in sensitive_patterns)
