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
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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
    LLM output that's mostly right but missing fields / has odd statuses."""
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


def render_plan_md(structured: Dict, active_step_id: int = 0,
                   meta: Optional[Dict] = None) -> str:
    """Generate PLAN.md content from a structured plan + optional metadata
    (last_guardian_at, edits_since_last_guardian, etc.)."""
    if not is_valid_structured(structured):
        return (
            "# PLAN\n\n"
            "*(No active plan. Enter Claude's plan mode or use "
            "`/cc-mem plan-set` to create one.)*\n"
        )
    meta = meta or {}
    lines = [
        "# PLAN",
        "",
        "> AUTO-GENERATED by cc-memory · DO NOT EDIT THIS FILE BY HAND",
        "> Source of truth: SQLite `plan_active` table. Edit via",
        "> `/cc-mem plan-set` (manual replace), Claude's plan mode",
        "> (auto-captured), or `/cc-mem plan-replan` (force re-refine).",
        "",
        "## Goal",
        "",
        structured["goal"].strip(),
        "",
    ]

    sc = structured.get("success_criteria") or []
    if sc:
        lines += ["## Success criteria", ""]
        for c in sc:
            lines.append(f"- {c}")
        lines.append("")

    lines += ["## Steps", ""]
    total = len(structured["steps"])
    done = sum(1 for s in structured["steps"] if s.get("status") == "done")
    for s in structured["steps"]:
        glyph = _STATUS_GLYPH.get(s.get("status", "pending"), "[ ]")
        active_marker = "  ← ACTIVE" if s.get("id") == active_step_id and s.get("status") != "done" else ""
        line = f"{s.get('id', '?')}. {glyph} **{s['title']}**{active_marker}"
        if s.get("notes"):
            line += f" — {s['notes']}"
        lines.append(line)
    lines.append("")

    ctx = (structured.get("context") or "").strip()
    if ctx:
        lines += ["## Context", "", ctx, ""]

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
        "last_refined_at": row.get("last_refined_at"),
        "last_guardian_at": row.get("last_guardian_at"),
        "edits_since_last_guardian": row.get("edits_since_last_guardian"),
        "turns_since_last_guardian": row.get("turns_since_last_guardian"),
    }
    text = render_plan_md(structured, active_step_id=active_step_id, meta=meta)
    memory_dir.mkdir(parents=True, exist_ok=True)
    out = memory_dir / "PLAN.md"
    out.write_text(text, encoding="utf-8")
    return out


# ── Capture: ExitPlanMode + raw text path ───────────────────────────────────

def capture_exit_plan_mode(db, project_id: int, plan_text: str,
                           memory_dir: Optional[Path] = None) -> None:
    """Store raw plan text and mark it for refinement. Called from
    PostToolUse hook when ExitPlanMode fires, or from `/cc-mem plan-set`
    when a user provides a manual plan.

    Side effect: writes memory/.plan_raw.md so the refiner subagent can Read it.
    """
    plan_text = (plan_text or "").strip()
    if not plan_text:
        return
    db.upsert_plan_active(
        project_id,
        raw=plan_text,
        needs_refine=1,
    )
    if memory_dir is not None:
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / ".plan_raw.md").write_text(plan_text, encoding="utf-8")


def apply_refined_plan(db, project_id: int, structured: Dict,
                       memory_dir: Optional[Path] = None) -> Dict:
    """Persist a refined structured plan (from the refiner subagent or from
    `/cc-mem plan-set --structured`). Clears needs_refine, sets refined_at,
    rewrites PLAN.md.

    Returns the normalised plan that was actually stored.
    """
    normalised = normalize_structured(structured)
    if not is_valid_structured(normalised):
        raise ValueError("refined plan does not satisfy schema (needs goal + ≥1 step)")
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
