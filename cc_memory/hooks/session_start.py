#!/usr/bin/env python3
"""
SessionStart hook — forced-handoff injection point.

Fires on every new session (startup, resume, post-compaction). Three jobs:

  1. INJECT layered context (topics, critical memories, recent timeline,
     handoff summary, footer).

  2. EMIT A FORCED <system-reminder> directing Claude to Read PROGRESS.md
     and MEMORY.md BEFORE responding. This is the hook-level enforcement
     of the handoff contract (see docs/CONTRACTS.md#handoff-contract).

  3. Best-effort RETROACTIVE SAVE — if previous JSONL transcripts were
     never compacted, extract memories from them now via Haiku.

Stdout: injected context (Claude reads it as additional system input).
Stderr: suppressed (file log only).
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Captured as early as possible: this is the reference instant for the hook's
# wall-clock budget (see _RETRO_DEADLINE_S). Taken before the package imports
# below so their cost is charged against the budget, not hidden from it.
_HOOK_T0 = time.monotonic()

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio BEFORE injecting context; the injected text can hold
# arbitrary unicode (emoji in MEMORY.md, ↻ in status, math symbols), and
# Windows gbk default would crash the hook on first print().
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

from core.db import MemoryDB
from core.extractor import load_transcript_window, mangle_project_path
from core.logger import get_logger
from core.modes import is_excluded
from core.progress import write_progress_md
from llm.memory_writer import upsert_batch

_log = get_logger("session_start")


# ── Layered context injection ──────────────────────────────────────────────
_DEFAULT_BUDGET = 16000  # ~4000 tokens at 4 chars/token

_LAYER_BUDGETS = {
    "topics":   0.30,
    "critical": 0.15,
    "timeline": 0.20,
    "progress": 0.25,  # PROGRESS.md preview gets a larger share now
    "footer":   0.10,
}


def _build_topics_layer(db, project_id, budget):
    topics = db.get_topics(project_id)
    if not topics:
        return "", set()
    lines = ["### Knowledge Base (by topic)", ""]
    used = 0
    topic_names = set()
    for t in topics:
        summary = t["content"]
        max_len = min(250, (budget - used) // max(len(topics), 1))
        if len(summary) > max_len:
            cut = summary[:max_len].rfind(".")
            summary = summary[:cut+1] if cut > 50 else summary[:max_len-3] + "..."
        entry = f"**[{t['name']}]** {summary}\n"
        if used + len(entry) > budget:
            break
        lines.append(entry)
        used += len(entry)
        topic_names.add(t["name"])
    return "\n".join(lines), topic_names


def _build_critical_layer(db, project_id, budget, topic_names):
    critical = db.get_critical_memories(project_id, min_importance=5)
    unmerged = [
        m for m in critical
        if not m.get("topic") or m.get("topic") not in topic_names
    ]
    if not unmerged:
        return "", set()
    lines = ["### Critical (unmerged)", ""]
    used = 0
    shown = set()
    for m in unmerged[:8]:
        entry = f"- [{m['category']}] {m['content']}"
        if used + len(entry) > budget:
            break
        lines.append(entry)
        used += len(entry)
        shown.add(m["id"])
    lines.append("")
    return "\n".join(lines), shown


def _build_timeline_layer(db, project_id, budget, shown_ids, mode_name="code"):
    """Returns (text, injected_ids). injected_ids are the memory ids actually
    rendered into the timeline (used for the inject manifest + reference bump)."""
    from core.modes import get_injection_priority
    priority = get_injection_priority(mode_name)
    recent = db.get_recent_memories(project_id, sessions_back=3, min_importance=3, limit=20)
    fresh = [m for m in recent if m["id"] not in shown_ids]
    if not fresh:
        return "", []
    cat_rank = {cat: i for i, cat in enumerate(priority)}
    fresh.sort(key=lambda m: (cat_rank.get(m["category"], 99), -m["importance"]))
    lines = ["### Recent", ""]
    used = 0
    injected = []
    for i, m in enumerate(fresh):
        if i < 5:
            prefix = "! " if m["importance"] >= 4 else "- "
            entry = f"{prefix}[{m['category']}] {m['content']}"
        else:
            short = m["content"][:60] + "..." if len(m["content"]) > 60 else m["content"]
            entry = f"#{m['id']} {m['category']}: {short}"
        if used + len(entry) > budget:
            break
        lines.append(entry)
        used += len(entry)
        injected.append(m["id"])
    lines.append("")
    return "\n".join(lines), injected


def _build_progress_preview(memory_dir, budget):
    """Render a compact preview of PROGRESS.md.

    The FORCED reminder block below asks Claude to read the full file, but we
    also embed a preview here so the model has the highlights even if it
    skips the Read (defense in depth).
    """
    progress = memory_dir / "PROGRESS.md"
    if not progress.exists():
        return ""
    try:
        text = progress.read_text(encoding="utf-8")
    except OSError:
        # why: read failure shouldn't break SessionStart; fall through to empty
        return ""
    # Trim to budget
    if len(text) > budget:
        text = text[:budget].rsplit("\n", 1)[0] + "\n…[truncated, read memory/PROGRESS.md]"
    return "### Last Session PROGRESS (preview)\n\n" + text + "\n"


def _build_footer(db, project_id, memory_dir):
    lines = []
    last_save = memory_dir / ".last_save.json"
    if last_save.exists():
        try:
            info = json.loads(last_save.read_text(encoding="utf-8"))
            ts = info.get("timestamp", "?")
            # trigger makes AUTO compactions visible. Claude Code only surfaces
            # hook execution in its UI for MANUAL /compact, so an automatic
            # compaction was previously indistinguishable from "never ran".
            trig = info.get("trigger", "")
            trig_s = f" ({trig})" if trig else ""
            if info.get("success"):
                method = info.get("method", "?")
                ni = info.get("n_inserted", 0)
                nm = info.get("n_merged", 0)
                ns = info.get("n_superseded", 0)
                lines.append(
                    f"[Last save: {ts}{trig_s} | +{ni}/~{nm}/↻{ns} via {method}]"
                )
            else:
                lines.append(f"[Last save FAILED at {ts}{trig_s}]")
        except (json.JSONDecodeError, OSError):
            # why: malformed status file shouldn't block injection;
            # the next PreCompact will overwrite it
            pass

    # A PreCompact killed by the host timeout dies on TerminateProcess: no
    # except block, no finally, so .last_save.json above still describes the
    # PREVIOUS successful run and the failure is invisible. pre_compact.py
    # writes a start marker it removes only on completion — a surviving marker
    # is therefore proof the last attempt died. Report it, but only once it is
    # old enough that it cannot be a run still in flight.
    attempt = memory_dir / ".pre_compact_attempt.json"
    if attempt.exists():
        try:
            a = json.loads(attempt.read_text(encoding="utf-8"))
            if not isinstance(a, dict):
                # A JSON list/int/string parses fine but has no .get(); the
                # AttributeError would escape _build_footer and take the ENTIRE
                # context injection with it (no memories, no PROGRESS preview,
                # no forced reminder). Normalise to the handled path instead.
                raise ValueError("marker is not a JSON object")
            started = datetime.strptime(a.get("started_at", ""), "%Y-%m-%d %H:%M:%S")
            age_min = (datetime.now() - started).total_seconds() / 60.0
            if age_min >= 10:
                mib = float(a.get("transcript_bytes") or 0) / 1024 ** 2
                lines.append(
                    f"[WARNING: PreCompact at {a.get('started_at')} "
                    f"({a.get('trigger', '?')}) DID NOT FINISH — killed before "
                    f"save (transcript {mib:.0f} MiB). Memories from that "
                    f"compaction were lost.]"
                )
        except (json.JSONDecodeError, OSError, ValueError, TypeError, AttributeError):
            # why: an unreadable/garbled marker is purely advisory. This tuple is
            # deliberately wide — a corrupt marker escaping here would abort
            # build_context() and silently drop the whole injection payload,
            # trading a missing diagnostic line for total memory loss.
            pass
    try:
        from core.auth import get_api_key
        _key, source = get_api_key()
        if source == "oauth_expired":
            lines.append("[WARNING: OAuth expired — LLM extraction disabled]")
        elif not _key:
            lines.append("[WARNING: No API key — LLM extraction disabled]")
    except Exception:
        # why: auth check is purely informational here; never block startup
        pass

    stats = db.get_stats(project_id)
    n_obs = db.get_observation_count(project_id)
    lines.append(
        f"[{stats['n_sessions']} sessions, {stats['n_memories']} memories, "
        f"{stats.get('n_topics', 0)} topics, {n_obs} observations]"
    )
    lines += ["", "=== END CC-MEMORY ===", ""]
    return "\n".join(lines)


def _build_forced_reminder(memory_dir):
    """Emit a <system-reminder> that FORCES the next response to Read PROGRESS.md.

    This is the core of the v2.1 forced-handoff mechanism. Soft reminders
    were unreliable (cf. v2.0 SESSION_HANDOFF.md drift). The system-reminder
    block is honored as authoritative context by Claude.
    """
    progress = memory_dir / "PROGRESS.md"
    memory_md = memory_dir / "MEMORY.md"
    has_progress = progress.exists()
    has_memory = memory_md.exists()
    if not (has_progress or has_memory):
        return ""

    lines = [
        "",
        "<system-reminder>",
        "CC-MEMORY HANDOFF — MANDATORY READ-FIRST PROTOCOL",
        "",
        "Before responding to any user request in this session, you MUST:",
    ]
    n = 1
    if has_progress:
        lines.append(f"  {n}. Use the Read tool on `memory/PROGRESS.md` "
                     f"(absolute: `{progress.as_posix()}`).")
        n += 1
    if has_memory:
        lines.append(f"  {n}. Use the Read tool on `memory/MEMORY.md` "
                     f"(absolute: `{memory_md.as_posix()}`).")
        n += 1
    lines += [
        "",
        "After reading, explicitly state in your first reply:",
        '  "Read PROGRESS.md — prior progress: <one-sentence summary>."',
        "",
        "RESUME PROTOCOL — if the user's first message is exactly one of:",
        # i18n Tier 3: bilingual resume tokens INTENTIONAL — keep in sync with
        # user_prompt.py resume_signals; do NOT reduce to English-only (docs/ARCHITECTURE.md#9-documentation-language-convention-i18n §1).
        '    "" (empty)  ·  "继续"  ·  "接着"  ·  "接着做"  ·  "接着干"  ·',
        '    "继续干"  ·  "resume"  ·  "continue"  ·  "go on"  ·  "keep going"',
        "  then DO NOT ask for clarification. Instead:",
        "    1. Read PROGRESS.md §3 (Open Todos) and §4 (Plan).",
        "    2. If §3 has at least one open todo, announce",
        '       "Resuming prior task: <todos[0].content>" and start executing it.',
        "    3. If §3 is empty but §4 (Plan) is non-empty, follow the plan's first step.",
        "    4. If both are empty, fall back to a one-sentence prior-progress",
        '       summary plus "what would you like to do next?".',
        "",
        "Why: this is the project's handoff contract (single source of truth).",
        "Skipping it risks duplicating work or contradicting prior decisions.",
        "Spec: `docs/CONTRACTS.md#handoff-contract`.",
        "</system-reminder>",
        "",
    ]
    return "\n".join(lines)


def _write_inject_manifest(memory_dir, manifest):
    """Atomically persist the inject manifest to memory/.last_inject.json.

    tempfile + os.replace is genuinely atomic (unlike the plain write_text used
    by .last_save.json), so a concurrent /cc-mem inject-show never reads a
    half-written file. Best-effort: failure must not break SessionStart.
    """
    try:
        fd, tmp = tempfile.mkstemp(dir=str(memory_dir), prefix=".last_inject.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, str(memory_dir / ".last_inject.json"))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as e:
        _log.error(f".last_inject.json write failed: {e}")


def build_context(memory_dir, db, project_id, project_name, current_session_id=""):
    total_budget = _DEFAULT_BUDGET
    mode_name = db.get_project_mode(project_id)

    header = (
        f"=== CC-MEMORY: Context Restored ===\n"
        f"Project: {project_name}  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    )
    parts = [header]

    budget = int(total_budget * _LAYER_BUDGETS["topics"])
    topics_text, topic_names = _build_topics_layer(db, project_id, budget)
    if topics_text:
        parts.append(topics_text)

    budget = int(total_budget * _LAYER_BUDGETS["critical"])
    critical_text, shown_ids = _build_critical_layer(db, project_id, budget, topic_names)
    if critical_text:
        parts.append(critical_text)

    budget = int(total_budget * _LAYER_BUDGETS["timeline"])
    timeline_text, timeline_ids = _build_timeline_layer(
        db, project_id, budget, shown_ids, mode_name)
    if timeline_text:
        parts.append(timeline_text)

    budget = int(total_budget * _LAYER_BUDGETS["progress"])
    progress_text = _build_progress_preview(memory_dir, budget)
    if progress_text:
        parts.append(progress_text)

    footer = _build_footer(db, project_id, memory_dir)
    parts.append(footer)

    # The forced reminder block goes LAST so it's the freshest context.
    parts.append(_build_forced_reminder(memory_dir))

    result = "\n".join(parts)

    # ── v2.3 observability: persist exactly WHAT was injected, and mark those
    # memories as referenced (keeps them "young" for the staleness net). The
    # manifest backs `/cc-mem inject-show` so the user has ground truth of what
    # cc-memory loaded, independent of whether Claude echoes it.
    critical_ids = sorted(shown_ids)
    timeline_ids = sorted(set(timeline_ids))
    all_ids = sorted(set(critical_ids) | set(timeline_ids))
    manifest = {
        "session_id": current_session_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "project": project_name,
        "topic_names": sorted(topic_names),
        "critical_ids": critical_ids,
        "timeline_ids": timeline_ids,
        "n_injected_memories": len(all_ids),
        "progress_preview_included": bool(progress_text),
        "total_chars": len(result),
        "est_tokens": len(result) // 4,
    }
    _write_inject_manifest(memory_dir, manifest)
    try:
        db.bump_last_referenced(all_ids)
    except Exception as e:
        _log.error(f"bump_last_referenced failed: {e}")

    _log.info(f"injected ~{len(result)//4} tokens ({len(result)} chars), "
              f"{len(all_ids)} memories tagged referenced")
    return result


# ── Retroactive save from prior session JSONL ──────────────────────────────
# ── LLM wall-clock envelope (v2.5.0) ───────────────────────────────────────
# hooks/hooks.json gives SessionStart 15s. The injection is this hook's entire
# product; it is printed and FLUSHED before any of this runs (see main()), so a
# kill here can no longer lose it — but a killed process still surfaces as a
# failed-hook notice and throws away the extraction it was in the middle of.
#
# Two bounds, because one alone is not enough:
#
#   _RETRO_DEADLINE_S  an absolute instant (from _HOOK_T0) by which ALL
#                      retroactive work must be FINISHED. Passed down to
#                      call_llm, which clamps every leg's socket timeout to the
#                      time actually remaining and skips a leg with <1s left.
#                      This is the bound that actually holds: it is independent
#                      of how many credential candidates exist.
#   _API_TIMEOUT       the per-leg ceiling for the COMMON case, when there is
#                      plenty of budget left.
#
# The pre-v2.5 code had only a "don't start another FILE" check, which cannot
# interrupt a leg already in flight: worst case was 2 candidates × 20s = 40s
# against a 15s host timeout — over budget with the SHIPPED default config, no
# opt-in required (and 100s with ccl.enabled=true).
#
# 13s of 15s leaves ~2s for the DB commit and interpreter teardown.
_API_TIMEOUT = 10
_FALLBACK_TIMEOUT = 5
_RETRO_DEADLINE_S = 13.0

_RETROACTIVE_PROMPT = """\
You are a memory extraction system. Given a Claude Code conversation transcript, \
extract the most important information worth remembering across sessions.

Output a JSON array of objects: {"category": str, "content": str, "importance": int, "topic": str}
- category: decision|result|config|bug|task|arch|note
- content: one concise, self-contained sentence with specific values
- importance: 1-5 (5=critical, 4=important, 3=useful)
- topic: a short keyword for the topic

Rules: Only conclusions, not process. Self-contained. Specific values. 5-15 items max.
Output ONLY valid JSON array."""


def _find_transcript_dir(project_path):
    """Resolve `~/.claude/projects/<slug>/` for this project, or None.

    EXACT slug match, then ONE case-insensitive pass, then None. There is
    deliberately NO fuzzy/substring fallback and there must never be one again:
    the transcripts found here are LLM-extracted, persisted, and re-injected at
    every future SessionStart, so a wrong directory permanently contaminates
    this project's memory with another project's content.

    The removed fallback accepted any slug directory whose name merely
    *contained* the project's basename. Measured on the reference machine
    (179 slug dirs): basename 'core' matched 131 of them, 'app' 141, 'proj' 33;
    a fixture seeded with 5 memories finished with 32 after a 278,700-record
    transcript from an unrelated project was ingested. Guessing which project a
    transcript belongs to is never safe here — returning None is.

    Slug construction lives in core.extractor.mangle_project_path (which also
    normalises '_' and '.', the omission that used to push most projects into
    the fuzzy branch in the first place).
    """
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None
    hash_candidate = mangle_project_path(str(Path(project_path).resolve()))
    candidate = claude_projects / hash_candidate
    if candidate.exists():
        return candidate
    hash_lower = hash_candidate.lower()
    for d in claude_projects.iterdir():
        if d.is_dir() and d.name.lower() == hash_lower:
            return d
    return None


def _transcript_cwd(messages):
    """First `cwd` value recorded in a transcript window, or "" if absent.

    Claude Code stamps `cwd` on transcript records (verified: record index 2 of
    a live transcript carries {'cwd': 'd:\\\\Projects\\\\cc-memory', ...}), well
    inside the 40-record head of a bounded window.
    """
    for rec in messages:
        if not isinstance(rec, dict):
            continue
        c = rec.get("cwd")
        if isinstance(c, str) and c.strip():
            return c
    return ""


def _transcript_belongs_to(messages, project_path):
    """POSITIVE ownership check: does this transcript name THIS project?

    Defence in depth behind _find_transcript_dir's exact match. Fail-closed —
    a transcript that records no `cwd` at all is treated as not ours, because
    the cost of a false positive (foreign memories persisted and re-injected
    forever) hugely outweighs the cost of a false negative (one prior session
    not retroactively saved).
    """
    tcwd = _transcript_cwd(messages)
    if not tcwd:
        return False
    try:
        return Path(tcwd).resolve() == Path(project_path).resolve()
    except (OSError, ValueError):
        # why: an unresolvable path recorded inside a foreign transcript must
        # count as "not mine"; never let a resolution failure read as a match
        return False


def _transcript_is_foreign(messages, project_path):
    """Does this transcript positively name a DIFFERENT project?

    The tier-3 counterpart to _transcript_belongs_to — and deliberately NOT the
    same polarity, which is why it is a separate function rather than a `not`.

    retroactive_save persists LLM-extracted memories that are re-injected at
    every future SessionStart, so it demands positive proof of ownership and
    treats an unstamped transcript as foreign. Tier-3 mining only fills
    still-empty PROGRESS.md fields for one session, and its input set includes
    transcripts carrying no `cwd` at all (the fixture shape used by
    tests/smoke_test.py), so requiring positive proof there would disable the
    whole tier rather than harden it.

    So gate on DISAGREEMENT: absent cwd -> allow, present-and-equal -> allow,
    present-and-different -> refuse. Every transcript Claude Code actually
    writes stamps `cwd` (verified on a live transcript, record index 2), so the
    contamination path is fully closed while cwd-less inputs keep working.
    """
    tcwd = _transcript_cwd(messages)
    if not tcwd:
        return False
    try:
        return Path(tcwd).resolve() != Path(project_path).resolve()
    except (OSError, ValueError):
        # why: a cwd the transcript DID record but that will not resolve cannot
        # be shown to name this project — refuse rather than trust it
        return True


def _get_saved_session_ids(db, project_id):
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT claude_session_id FROM sessions WHERE project_id = ?",
            (project_id,)
        ).fetchall()
    return {r["claude_session_id"] for r in rows if r["claude_session_id"]}


def _summarize_transcript(messages, max_chars=12000, total_records=None):
    """Render the most RECENT slice of a transcript, up to max_chars.

    Same contract as pre_compact._build_transcript_summary, and fixed for the
    same reason: filling the budget from the OLDEST record pinned retroactive
    extraction to a session's opening minutes and never showed it the work that
    actually happened. Fills newest-first, then restores chronological order.
    """
    parts, total, scanned = [], 0, 0
    for msg in reversed(messages):
        scanned += 1
        message = msg.get("message", {})
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        content = message.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        if name in ("Edit", "Write", "MultiEdit"):
                            text_parts.append(f"[Tool: {name} {inp.get('file_path', '')}]")
                        elif name == "Bash":
                            text_parts.append(f"[Bash: {inp.get('command', '')[:100]}]")
                        else:
                            text_parts.append(f"[Tool: {name}]")
            text = "\n".join(text_parts)
        else:
            continue
        if not text.strip():
            continue
        if len(text) > 800:
            text = text[:400] + "\n...\n" + text[-400:]
        line = f"[{role}] {text}\n"
        if total + len(line) > max_chars:
            scanned -= 1  # this one didn't make it in
            break
        parts.append(line)
        total += len(line)
    parts.reverse()  # newest-first accumulation → chronological
    # `messages` may be a bounded window, so its length understates the
    # transcript; total_records is the real count when the caller has it.
    universe = len(messages) if total_records is None else max(total_records, len(messages))
    omitted = universe - scanned
    if omitted > 0:
        parts.insert(0, f"[...{omitted} earlier messages omitted, showing most recent...]\n")
    return "\n".join(parts)


def _retroactive_extract(messages, total_records=None, deadline=None):
    from core.auth import get_api_key
    api_key, _ = get_api_key()
    if not api_key:
        return None
    transcript_text = _summarize_transcript(messages, total_records=total_records)
    if len(transcript_text) < 100:
        return None
    try:
        from llm.ccl_backend import call_llm
        text = call_llm(_RETROACTIVE_PROMPT,
                        f"Extract memories:\n\n{transcript_text}",
                        api_key, max_tokens=2000, timeout=_API_TIMEOUT,
                        fallback_timeout=_FALLBACK_TIMEOUT,
                        deadline=deadline)
        text = text.strip()
        if text.startswith("```"):
            text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
        memories = json.loads(text)
        if not isinstance(memories, list):
            return None
        valid = []
        for m in memories:
            if not isinstance(m, dict):
                continue
            cat = m.get("category", "note")
            content = m.get("content", "").strip()
            imp = m.get("importance", 3)
            topic = m.get("topic", "")
            if not content or len(content) < 10:
                continue
            if cat not in ("decision", "result", "config", "bug", "task", "arch", "note"):
                cat = "note"
            valid.append({
                "category": cat, "content": content,
                "importance": max(1, min(int(imp), 5)),
                "topic": topic if isinstance(topic, str) else "",
            })
        return valid if valid else None
    except Exception:
        # why: retroactive save is best-effort; any LLM/JSON failure
        # should be silent — the rest of the hook still works
        return None


def _refresh_progress_row(db, project_id, memory_dir, current_session_id=None):
    """Fill EMPTY progress fields from authoritative sources before injection.

    Three-tier fallback (run in order; each only fills currently-empty fields):

      Tier 1 (PreCompact upstream):
        If a PreCompact already wrote the row, all fields are non-empty and
        this function is effectively a no-op.

      Tier 2 (DB):
        - critical_context  ← db.get_critical_memories(min_importance=4)[:10]
        - status_done       ← latest session_summary.completed
        - status_in_flight  ← latest session_summary.learned
        - plan              ← latest session_summary.next_steps
        - open_todos        ← split next_steps by ';' (heuristic)
        - files_touched     ← recent observations table

      Tier 3 (transcript JSONL of the PREVIOUS session):
        - open_todos     ← extract_latest_todo_state on the prior .jsonl
        - files_touched  ← extract_file_changes on the prior .jsonl
        - transcript_ptr ← absolute path to the prior .jsonl
        (Last resort — only fires when DB sources are also empty, e.g. very
         short prior session, or PreCompact never ran for that session.)

    Fill-only-empty contract: a non-empty value written by upsert_progress()
    or patch_progress() upstream is NEVER overwritten. This guarantees the
    PreCompact full-rewrite remains authoritative.
    """
    # v5: tag the session BEFORE reading `cur` so the fill-only-empty checks
    # below see the same row a downstream patch_progress would. If this
    # session is brand new, both current_session_id and session_started_at
    # get set here, which makes PROGRESS.md §0 attribute the next writes to
    # the correct owner.
    if current_session_id:
        db.tag_progress_session(project_id, current_session_id)

    cur = db.get_progress(project_id) or {}
    patch = {}

    # ── Tier 2A: critical_context from DB ──────────────────────────────────
    if not cur.get("critical_context"):
        crit = db.get_critical_memories(project_id, min_importance=4)[:10]
        if crit:
            patch["critical_context"] = [
                {"id": m["id"], "category": m["category"],
                 "topic": m.get("topic", "") or "",
                 "content": (m["content"] or "")[:200]}
                for m in crit
            ]

    # ── Tier 2B: status + plan from latest session_summary ────────────────
    # NOTE: open_todos is deliberately NOT filled here. Tier 3 (transcript
    # mining) gives much cleaner data via TodoWrite tool_use blocks; we let
    # tier 3 fire first and only fall back to next_steps split below if
    # tier 3 has nothing.
    summary = db.get_latest_summary(project_id) or {}
    next_steps_text = (summary.get("next_steps") or "").strip()
    if summary:
        if not cur.get("status_done") and summary.get("completed"):
            patch["status_done"] = summary["completed"]
        if not cur.get("status_in_flight") and summary.get("learned"):
            patch["status_in_flight"] = summary["learned"]
        if not cur.get("plan") and next_steps_text:
            patch["plan"] = next_steps_text

    # ── Tier 2C: files_touched from recent observations ────────────────────
    if not cur.get("files_touched"):
        obs = db.get_recent_observations(project_id, limit=40)
        files_read = list(dict.fromkeys(
            o["tool_input"] for o in obs
            if o["tool_name"] == "Read" and o["tool_input"]
        ))[:15]
        files_modified = list(dict.fromkeys(
            o["tool_input"] for o in obs
            if o["tool_name"] in ("Edit", "Write", "MultiEdit") and o["tool_input"]
        ))[:15]
        ft = (
            [{"path": p, "action": "edit"} for p in files_modified] +
            [{"path": p, "action": "read"} for p in files_read if p not in files_modified]
        )
        if ft:
            patch["files_touched"] = ft

    # ── Tier 3: mine the previous session's transcript JSONL ───────────────
    # Higher quality than tier-2 heuristics — TodoWrite tool_use blocks are
    # structured data, far more reliable than splitting next_steps text by
    # semicolons. Reads IO only when there are still empty fields to fill.
    cwd = str(memory_dir.parent.resolve())
    needs_todos = not cur.get("open_todos")
    needs_files = "files_touched" not in patch and not cur.get("files_touched")
    needs_ptr   = not cur.get("transcript_ptr")
    todos_from_transcript = None

    if needs_todos or needs_files or needs_ptr:
        try:
            from core.extractor import (
                find_latest_transcript, load_transcript_window,
                extract_latest_todo_state, extract_file_changes,
            )
            prior_jsonl = find_latest_transcript(cwd, exclude_session_id=current_session_id)
            if prior_jsonl and prior_jsonl.stat().st_size > 200:
                # Bounded: SessionStart has a 15s budget — an EIGHTH of what
                # PreCompact gets — and a long-lived project's transcript can
                # reach GiB. Both consumers below want the RECENT end anyway
                # (last TodoWrite snapshot, files touched most recently).
                #
                # The window is now loaded even when transcript_ptr is the only
                # empty field, because the ownership check below needs the
                # records to decide — and a pointer to ANOTHER project's
                # transcript, written into PROGRESS.md and read by the next
                # session, is contamination in its own right. The read is the
                # bounded head+tail one, so the cost is capped, not unbounded.
                prior_msgs = load_transcript_window(str(prior_jsonl)).messages
                if _transcript_is_foreign(prior_msgs, cwd):
                    # find_latest_transcript is exact-slug-match only, so this
                    # should be unreachable; it is the defence-in-depth layer
                    # retroactive_save already has (_transcript_belongs_to).
                    _log.error(
                        f"tier-3 mine REFUSED {prior_jsonl.name}: transcript cwd "
                        f"{_transcript_cwd(prior_msgs)!r} != project {cwd!r}"
                    )
                elif prior_msgs:
                    if needs_ptr:
                        patch["transcript_ptr"] = str(prior_jsonl.resolve())
                    if needs_todos:
                        mined = extract_latest_todo_state(prior_msgs)
                        pending = [t for t in mined
                                   if t.get("status") != "completed"]
                        if pending:
                            todos_from_transcript = [
                                {"content": t["content"][:300],
                                 "priority": t.get("priority", "medium"),
                                 "status": t.get("status", "pending")}
                                for t in pending[:10]
                            ]
                            patch["open_todos"] = todos_from_transcript
                    if needs_files:
                        mined_files = extract_file_changes(prior_msgs)[:15]
                        if mined_files:
                            patch["files_touched"] = [
                                {"path": p, "action": "edit"}
                                for p in mined_files
                            ]
        except Exception as e:
            _log.error(f"tier-3 transcript mine failed: {e}")

    # ── Tier 2B (deferred): next_steps split as LAST-RESORT open_todos ─────
    # Only fires if tier 3 transcript mining didn't find a TodoWrite snapshot.
    # The split-by-semicolon heuristic produces low-quality items (a single
    # long prose sentence collapses to one phantom todo) so we keep it as a
    # final fallback to avoid an empty §3 in PROGRESS.md.
    if needs_todos and todos_from_transcript is None and next_steps_text:
        steps = [s.strip() for s in next_steps_text.split(";") if s.strip()]
        if steps:
            patch["open_todos"] = [
                {"content": s[:300], "priority": "medium", "status": "pending"}
                for s in steps[:8]
            ]

    if patch:
        patch["trigger_type"] = "session_start_refresh"
        db.patch_progress(project_id, **patch)
        try:
            write_progress_md(db, project_id, memory_dir)
        except Exception as e:
            _log.error(f"PROGRESS.md write after refresh failed: {e}")
        _log.info(f"refreshed empty progress fields: {sorted(k for k in patch if k != 'trigger_type')}")


# ── Cost model for load_transcript_window (v2.5.1) ─────────────────────────
# _RETRO_DEADLINE_S used to be enforced in exactly two places: the top of the
# per-file loop below, and inside call_llm. load_transcript_window ran BETWEEN
# them with no time bound of its own, so a file could be STARTED just under the
# deadline and then load for seconds. Measured on the reference repro (three
# unsaved prior transcripts, the oldest 4 GiB, a healthy-but-slow backend):
# `wall=17.12s` against a 15s host timeout. Real transcripts are big enough to
# do it on their own — 2.11 GiB loads in 3.37s and such a file exists on this
# machine, while the loop reaches its last check at ~12.6s.
#
# The kill is TerminateProcess: no `except`, no `finally`, nothing committed.
# A file that yields no memories also writes no `sessions` row, so the SAME
# files are re-scanned and re-killed at every SessionStart, forever.
#
# The load is two linear passes (see core.extractor.load_transcript_window):
#   * a JSON decode of at most a 32 MiB tail window (~25 MiB/s), and
#   * a raw record scan of the WHOLE file to keep total_records exact (~1 GiB/s).
# Predicted vs measured: 2.11 GiB -> 3.39s / 3.37s; 1.49 GiB -> 2.77s / 2.36s;
# 4 GiB synthetic -> 5.28s / 3.67s. The model never under-predicted; _LOAD_SAFETY
# on top covers a cold page cache and a contended disk.
_WINDOW_TAIL_BYTES = 32 << 20   # keep in sync with extractor._DEFAULT_TAIL_BYTES
_DECODE_BYTES_S = 25 << 20
_SCAN_BYTES_S = 1 << 30
_LOAD_SAFETY = 1.5


def _estimate_load_s(size_bytes):
    """Seconds load_transcript_window is expected to need for `size_bytes`."""
    decode = min(size_bytes, _WINDOW_TAIL_BYTES) / _DECODE_BYTES_S
    scan = size_bytes / _SCAN_BYTES_S
    return (decode + scan) * _LOAD_SAFETY


def retroactive_save(cwd, db, project_id, current_session_id="", deadline=None):
    """Best-effort: LLM-extract memories from prior, never-compacted transcripts.

    TWO independent ownership gates, because a wrong transcript here is not a
    cosmetic bug — its memories are stored and re-injected at every future
    SessionStart of the WRONG project:

      1. `_find_transcript_dir` is exact-match only (no fuzzy fallback).
      2. Every candidate .jsonl must additionally NAME this project in its own
         `cwd` field (`_transcript_belongs_to`, fail-closed).

    `deadline` is a `time.monotonic()` instant by which this function must be
    DONE — not merely the instant after which no new file is started. Three
    checks enforce it: before the file (loop top), before the file's window load
    (whose cost is charged UP FRONT from its size, see _estimate_load_s), and
    again immediately after that load. Each file is committed as it completes,
    so stopping keeps everything already saved; only the rest are skipped.
    """
    transcript_dir = _find_transcript_dir(cwd)
    if not transcript_dir:
        _log.info(f"retroactive save: no exact transcript dir for {cwd} — skipped")
        return
    saved_ids = _get_saved_session_ids(db, project_id)
    jsonls = sorted(transcript_dir.glob("*.jsonl"),
                    key=lambda f: f.stat().st_mtime, reverse=True)

    memory_dir = Path(cwd) / "memory"
    n_retroactive = 0
    for jsonl in jsonls[:3]:
        if deadline is not None and time.monotonic() >= deadline:
            _log.info(f"retroactive save: wall-clock budget spent, stopping "
                      f"after {n_retroactive} session(s)")
            break
        session_uuid = jsonl.stem
        if session_uuid == current_session_id:
            continue
        if session_uuid in saved_ids:
            continue
        size = jsonl.stat().st_size
        if size < 1024:
            continue
        # Charge the window load to the budget BEFORE starting it. `continue`
        # rather than `break`: the list is newest-first, so a later file may
        # still be small enough to afford.
        if deadline is not None:
            est = _estimate_load_s(size)
            left = deadline - time.monotonic()
            if est >= left:
                _log.info(
                    f"retroactive save: skipping {jsonl.name} — {size/1024**2:.0f} "
                    f"MiB needs ~{est:.1f}s, only {left:.1f}s of budget left"
                )
                continue
        try:
            window = load_transcript_window(str(jsonl))
            messages = window.messages
            # The estimate is a model; the clock is the truth. If the load
            # actually ran past the deadline, stop: call_llm would skip every
            # leg anyway, _retroactive_extract would return None, and no
            # `sessions` row would be written — pure budget burn.
            if deadline is not None and time.monotonic() >= deadline:
                _log.info(f"retroactive save: budget spent loading "
                          f"{jsonl.name}, stopping")
                break
            if not messages or len(messages) < 5:
                continue
            if not _transcript_belongs_to(messages, cwd):
                _log.error(
                    f"retroactive save REFUSED {jsonl.name}: transcript cwd "
                    f"{_transcript_cwd(messages)!r} != project {cwd!r}"
                )
                continue
            memories = _retroactive_extract(messages,
                                            total_records=window.total_records,
                                            deadline=deadline)
            if not memories:
                continue

            sid = db.insert_session(
                project_id=project_id,
                claude_session_id=session_uuid,
                trigger_type="retroactive_llm",
                msg_count=window.total_records,
                archive_path="",
                brief_summary=f"Retroactive save at {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            )
            counts = upsert_batch(db, project_id, sid, memories, memory_dir=memory_dir)
            n_retroactive += 1
            _log.info(
                f"retroactive {session_uuid[:8]}: +{counts.get('inserted',0)} "
                f"~{counts.get('merged',0)} ↻{counts.get('superseded',0)}"
            )
        except Exception as e:
            _log.error(f"retroactive save error: {e}")


def _flush_stdout():
    """Force the injection out of the stdout buffer, now.

    A SessionStart killed by the host timeout dies on TerminateProcess: no
    atexit, no interpreter shutdown, no implicit flush. stdout on a pipe is
    block-buffered, so every completed print() is still sitting in userspace
    when the process is torn down. Measured: 5069 B of already-printed
    injection arrived as 0 B; with this flush, 5071 B.

    This does NOT depend on how core.encoding_setup configures the stream. If
    that module also enables line buffering the two are redundant, which is
    the intent — stdout IS this hook's entire product and there is no second
    chance at it, so the guarantee is made here explicitly rather than
    inherited from a stream-configuration detail elsewhere.
    """
    try:
        sys.stdout.flush()
    except (ValueError, OSError):
        # why: detached or closed pipe — the bytes are already written or
        # already unrecoverable; never abort the hook over a flush
        pass


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception as e:
        _log.error(f"session_start stdin error: {e}")
        sys.exit(0)

    # json.loads succeeds for well-formed but NON-OBJECT payloads (null, 42,
    # "s", [1,2], true). .get() on those raises outside the try above, so the
    # hook would exit 1 with a traceback on stderr — a hook-contract violation
    # that Claude Code renders as error UI.
    if not isinstance(data, dict):
        _log.error(f"session_start: non-object stdin payload "
                   f"({type(data).__name__}) — nothing to do")
        sys.exit(0)

    # FIELD types, not just the container type. The guard above only makes
    # `.get()` legal; a non-string cwd/session_id still flows into Path() and
    # into the DB from inside the try below. This hook already survives them
    # (its body sits in a broad try), but the guard belongs with the parse, not
    # with the recovery.
    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    if not isinstance(cwd, str) or not cwd:
        sys.exit(0)
    if not isinstance(session_id, str):
        session_id = ""

    # Project opt-out — the FIRST act after resolving cwd, before the DB is
    # opened. Gating on memory/memory.db existing is not an opt-out: for a
    # project initialised before the user listed it, this hook would otherwise
    # still print that project's memories and PROGRESS.md preview into the next
    # session's context, still write memory/.last_inject.json, and still run
    # retroactive LLM extraction over its transcripts. Logged (unlike Stop /
    # PostToolUse) because SessionStart fires once per session and a silently
    # empty injection is otherwise unexplainable.
    if is_excluded(cwd):
        _log.info(f"skipped: {cwd} is in config.json excluded_projects")
        sys.exit(0)

    try:
        memory_dir = Path(cwd) / "memory"
        db_path = memory_dir / "memory.db"
        if not db_path.exists():
            _log.info(f"no DB for {cwd}")
            sys.exit(0)

        db = MemoryDB(db_path)
        project_id = db.upsert_project(cwd)

        # Tier 2 + 3 fallback: fill PROGRESS.md empty fields before injection.
        # PreCompact remains the authoritative full-rewrite path; this only
        # populates fields PreCompact didn't get to. See _refresh_progress_row
        # docstring for the source priority and fill-only-empty contract.
        try:
            _refresh_progress_row(db, project_id, memory_dir,
                                  current_session_id=session_id)
        except Exception as e:
            _log.error(f"progress refresh failed: {e}")

        print(f"\n[cc-memory] Session start — loading memory for '{Path(cwd).name}'...")
        print(build_context(memory_dir, db, project_id, Path(cwd).name,
                            current_session_id=session_id))
        # The injection is complete and irreplaceable — get it out of the
        # buffer before any further work can run us into the 15s timeout.
        _flush_stdout()
        stats = db.get_stats(project_id)
        # v2.3 observability: user-visible one-liner of WHAT was injected, read
        # from the manifest build_context just wrote (ground truth, not a guess).
        try:
            man = json.loads((memory_dir / ".last_inject.json").read_text(encoding="utf-8"))
            print(
                f"[cc-memory OK] Injected {man.get('n_injected_memories', 0)} memories"
                f" ({len(man.get('topic_names', []))} topics, "
                f"~{man.get('est_tokens', 0)} tokens"
                f"{', +PROGRESS.md' if man.get('progress_preview_included') else ''})"
                f" · see `/cc-mem inject-show`"
            )
        except (OSError, json.JSONDecodeError, ValueError):
            print(
                f"[cc-memory OK] Context loaded: "
                f"{stats['n_memories']} memories, {stats.get('n_topics', 0)} topics"
            )
        _flush_stdout()
        _log.info(f"injected context for {Path(cwd).name}")

        try:
            # Budgeted: retroactive save runs LLM legs and must never be the
            # reason the 15s hook dies. Everything above is already flushed.
            retroactive_save(cwd, db, project_id, session_id,
                             deadline=_HOOK_T0 + _RETRO_DEADLINE_S)
        except Exception as e:
            _log.error(f"retroactive save failed: {e}")

    except Exception:
        _log.error_tb("session_start ERROR")
    sys.exit(0)


if __name__ == "__main__":
    main()
