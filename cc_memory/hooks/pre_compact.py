#!/usr/bin/env python3
"""
PreCompact hook (SYNC leg).

Triggered by Claude Code BEFORE context compaction. Two jobs — the fast,
handoff-critical work that must complete before the next session:

  1. Extract memories from the full transcript via Haiku LLM,
     routing every save through llm.memory_writer.upsert_smart so
     reconciliation (merge / supersede / insert) happens at write time.

  2. FULL-REWRITE memory/PROGRESS.md from the `progress` SQLite table.
     This is the handoff contract for the next session.

Consolidation (the every-Nth-session LLM cleanup) is NO LONGER done here.
As of v2.3.2 it runs in the sibling `async` PreCompact hook
(hooks/consolidate_async.py, declared with "async": true in hooks/hooks.json),
so its variable LLM latency can never block compaction or trigger the
"Hook cancelled" timeout. This sync leg finishes in ~1-5s.

Stdin (JSON):
  session_id, transcript_path, cwd, trigger ("auto"|"manual")

Output:
  stderr suppressed.
  stdout: ONE compact status line (visible in next session's context).
  Always exits 0 — must never block compaction.
"""
import json
import os
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path

# Captured as early as possible: the reference instant for this hook's
# wall-clock budget (see _LLM_DEADLINE_S below). Taken BEFORE the package
# imports so their cost is charged against the budget instead of hidden from
# it. Same idiom as hooks/session_start.py:31.
_HOOK_T0 = time.monotonic()

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent  # cc_memory/
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio BEFORE anything prints; Windows gbk crashes on ↻ etc.
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

from core.db import MemoryDB
from core.extractor import (build_extraction, load_transcript_window, group_sentences,
                            CATEGORY_ORDER, CATEGORY_LABELS)
from core.logger import get_logger
# Project opt-out. This USED to be a private literal copy in this file plus a
# second byte-identical one in hooks/user_prompt.py; the other four hooks had no
# check at all, so a project initialised BEFORE being listed stayed fully
# captured. One implementation now, called by all six. See core/modes.py.
from core.modes import is_excluded
from core.roots import project_root  # v2.6.0 root anchoring — core/roots.py
# Privacy filter for the PROGRESS ingress below. The observation and memory
# write paths honoured <private> from v2.5.0; this hook's progress ingress
# never did (see _first_user_request).
from core.privacy import clean_for_storage
from core.progress import (write_progress_md, write_session_archive, collect_progress_state,
                           migrate_legacy_handoff, ensure_memory_gitignore)
from llm.memory_writer import upsert_batch, regenerate_memory_index

_log = get_logger("pre_compact")

# ── LLM wall-clock envelope (v2.5.0) ───────────────────────────────────────
# hooks/hooks.json gives this sync leg 120s. TWO bounds, because per-leg
# timeouts alone are NOT one:
#
#   _API_TIMEOUT /     the per-leg ceilings for the COMMON case. call_llm
#   _FALLBACK_TIMEOUT  bounds the Anthropic legs at 2 candidates, so ONE
#                      call's NOMINAL cost is 2*25 + 30 = 80s (the Ollama leg
#                      runs only when config `ccl.enabled` is true).
#   _LLM_DEADLINE_S    an ABSOLUTE instant (from _HOOK_T0) by which extraction
#                      must be FINISHED. call_llm clamps every leg's socket
#                      timeout to the time actually remaining and skips a leg
#                      with <1s left. Because it is measured from _HOOK_T0, the
#                      transcript window read + build_extraction that run
#                      BEFORE the call are charged against it automatically.
#
# Why the nominal arithmetic is not enough: `urlopen(req, timeout=t)` sets a
# PER-SOCKET-OPERATION timeout — it covers neither DNS resolution nor the TLS
# handshake, so a leg overruns t (an adversarial verifier measured a SUCCESSFUL
# leg at 1.48x its nominal timeout). Reproduced here with both credential
# candidates live, ccl.enabled=true and every leg stalling at 1.48x: 3 legs =
# 37.0 + 37.0 + 44.4 = 118.4s, hook total 118.79s on a 1.6 MiB transcript whose
# non-LLM work is only 0.4s. Add the 25.6s of non-LLM work measured on a real
# 2.1 GiB / 585,007-record transcript and that run is ~144s — a kill, which is
# TerminateProcess: no `except`, no `finally` (the v2.4.2 failure class).
#
# 75s reserves 45s of the 120s: up to 0.48*25 = 12s for the final leg's
# in-flight overrun, and 33s for post-LLM work (upsert_batch, session summary,
# PROGRESS.md full rewrite, observation cleanup, MEMORY.md regen) + teardown.
# That reserve stays honest even against the pessimal assumption that ALL 25.6s
# of the measured non-LLM work lands AFTER the call: 75 + 12 + 25.6 = 112.6s.
# The common single-candidate path is untouched — after ~3s of transcript work
# a 25s leg has 72s of room.
_API_TIMEOUT = 25
_FALLBACK_TIMEOUT = 30
_LLM_DEADLINE_S = 75.0

_EXTRACTION_PROMPT = """\
You are a memory extraction system. Given a Claude Code conversation transcript, \
extract the most important information worth remembering across sessions.

Output a JSON array of objects with these fields:
- "category": one of "decision", "result", "config", "bug", "task", "arch", "note"
- "content": one concise, self-contained sentence with specific values (numbers, file names, parameters)
- "importance": 1-5 (5=critical/never-forget, 4=important, 3=useful, 2=minor, 1=trivial)
- "topic": a short lowercase keyword for grouping (e.g. "auth", "pipeline", "config", "ui")

Rules:
- Only save CONCLUSIONS, not discussion process or debugging steps
- Each memory must be understandable WITHOUT context
- Include specific values: "lr=3e-4 chosen over 1e-3 because val_loss flatlined" not "tuned lr"
- Skip: conversation logistics, tool errors, meta-discussion, trivial Q&A
- Output 5-15 memories maximum. Quality over quantity.
- Do NOT include memories about the memory plugin itself unless it's a critical bug fix

Output ONLY a valid JSON array, no markdown, no explanation."""


def _build_transcript_summary(messages, max_chars=12000, total_records=None):
    """Render the most RECENT slice of the conversation, up to max_chars.

    Fills from the NEWEST message BACKWARDS, then restores chronological order.
    The previous version filled from the oldest message and broke at the budget,
    so on a long-lived session the LLM only ever saw the session's opening
    minutes: measured on a real 2.1 GiB transcript, the 12k budget was exhausted
    after 329 of ~585,000 records, pinning every extraction to content 70 days
    stale while all recent work went unseen. A handoff needs the recent end.

    ``total_records`` is the transcript's REAL record count. Without it the
    "earlier messages omitted" figure is window-relative and badly understates
    reality — on that same transcript it would claim ~10,000 omitted when
    ~575,000 were, which actively misleads the extracting model about how much
    context it is missing.
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
                        elif name == "TodoWrite":
                            text_parts.append(
                                f"[TodoWrite: {json.dumps(inp.get('todos', [])[:5], ensure_ascii=False)[:200]}]"
                            )
                        else:
                            text_parts.append(f"[Tool: {name}]")
            text = "\n".join(text_parts)
        else:
            continue

        if not text.strip():
            continue
        if len(text) > 800:
            text = text[:400] + "\n...[truncated]...\n" + text[-400:]
        line = f"[{role}] {text}\n"
        if total + len(line) > max_chars:
            scanned -= 1  # this one didn't make it in
            break
        parts.append(line)
        total += len(line)
    parts.reverse()  # newest-first accumulation → chronological for the LLM
    universe = len(messages) if total_records is None else max(total_records, len(messages))
    omitted = universe - scanned
    if omitted > 0:
        parts.insert(0, f"[...{omitted} earlier messages omitted, showing most recent...]\n")
    return "\n".join(parts)


def _extract_via_llm(messages, observations=None, total_records=None):
    from core.auth import get_api_key
    api_key, source = get_api_key()
    if not api_key:
        reason = "OAuth token expired" if source == "oauth_expired" else "no API key found"
        _log.info(f"{reason}, skipping LLM extraction")
        return None

    transcript_text = _build_transcript_summary(messages, total_records=total_records)
    if len(transcript_text) < 100:
        return None

    obs_context = ""
    if observations:
        obs_lines = [f"[{o['tool_name']}] {o['tool_input']}" for o in observations[-50:]]
        if obs_lines:
            obs_context = "\n\nTool observations (for context):\n" + "\n".join(obs_lines)

    user_content = f"Extract memories from this conversation:\n\n{transcript_text}{obs_context}"

    try:
        from llm.ccl_backend import call_llm
        text = call_llm(_EXTRACTION_PROMPT, user_content, api_key,
                        max_tokens=2500, timeout=_API_TIMEOUT,
                        fallback_timeout=_FALLBACK_TIMEOUT,
                        deadline=_HOOK_T0 + _LLM_DEADLINE_S)
        text = text.strip()
        if text.startswith("```"):
            text = "\n".join(
                l for l in text.split("\n") if not l.strip().startswith("```")
            )
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
            imp = max(1, min(int(imp), 5))
            valid.append({
                "category": cat, "content": content, "importance": imp,
                "topic": topic if isinstance(topic, str) else "",
            })
        _log.info(f"LLM extracted {len(valid)} memories")
        return valid if valid else None

    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, OSError, KeyError, ValueError, RuntimeError,
            AttributeError, TypeError) as e:
        # RuntimeError: llm.ccl_backend.call_llm raises it when EVERY backend
        # candidate fails. Without it here that escapes to main()'s handler and
        # aborts the whole hook — skipping the PROGRESS.md rewrite — so a
        # transient API outage would silently cost a session handoff. Extraction
        # is optional; the handoff is not.
        # AttributeError / TypeError, added in v2.5.2: a config.json whose `ccl`
        # key held a string, or whose top level was not an object, made
        # _load_local_config raise AttributeError THROUGH call_llm (which is
        # called outside any try of its own) and out of this tuple — costing the
        # handoff for a config typo. v2.5.2 fixed that reader, so this is
        # defence in depth for the next shape-mismatch, of which there will be
        # one: the sentence above is a contract, and the tuple has to hold it
        # against causes nobody enumerated yet.
        _log.error(f"LLM extraction failed: {e}")
        return None


def _fmt_archive(ext, timestamp, trigger, project_name):
    lines = [
        f"# Session Archive -- {project_name}",
        f"**Timestamp**: {timestamp}  |  **Trigger**: {trigger}  "
        f"|  **Messages**: {ext['msg_count']}",
        "",
    ]
    if ext["metrics"]:
        lines += ["## Metrics & Results", ""]
        for m in ext["metrics"][:20]:
            lines.append(f"- `{m}`")
        lines.append("")
    grouped = group_sentences(ext["sentences"])
    for cat in CATEGORY_ORDER:
        if cat not in grouped:
            continue
        label = CATEGORY_LABELS[cat]
        lines += [f"## {label}", ""]
        for text, imp in grouped[cat][:12]:
            prefix = "! " if imp >= 4 else "- "
            lines.append(f"{prefix}{text}")
        lines.append("")
    if ext["todos"]:
        lines += ["## Todos", ""]
        for t in ext["todos"]:
            box = "[x]" if t["status"] == "completed" else "[ ]"
            lines.append(f"- {box} `{t['priority']}` {t['content']}")
        lines.append("")
    if ext["file_changes"]:
        lines += ["## Files Changed", ""]
        for f in ext["file_changes"][:15]:
            lines.append(f"- `{f}`")
        lines.append("")
    if ext["keywords"]:
        top = sorted(ext["keywords"].items(), key=lambda x: -x[1])[:15]
        lines += ["## Top Keywords", ""]
        lines.append(", ".join(f"`{k}`" for k, _ in top))
        lines.append("")
    if ext["assistant_texts"]:
        last = ext["assistant_texts"][-1]
        if len(last) > 600:
            last = last[:597] + "..."
        lines += ["## Last Response (truncated)", "", last, ""]
    return "\n".join(lines)


def _first_user_request(messages, max_scan=200):
    """Return the session's opening user request, PRIVACY-CLEANED.

    Scans further than the first handful of records on purpose: a transcript
    opens with meta rows (`queue-operation`, `attachment`, …) that carry no
    role, and empty-content user rows also appear. Measured on a real
    transcript the first genuine user message sat at index 5, so the previous
    `messages[:5]` slice returned "" and PROGRESS.md's current_request came up
    empty. Empty candidates are skipped rather than returned.

    `clean_for_storage` is applied HERE rather than at the two call sites
    (`session_summaries.request` and `progress.current_request`, both below)
    because both store the value and PROGRESS.md renders `current_request`
    verbatim — into a file `memory/.gitignore` does NOT ignore, so a
    `<private>` span in the opening request used to be committed to the user's
    repository. Cleaning at the source covers every present and future
    consumer; cleaning at the call sites is the shape that let this ingress
    diverge from the observation and memory write paths in the first place.

    Cleaned BEFORE the 500-char cut, for the same reason as
    hooks/user_prompt.py: a span straddling the boundary is still a matched
    pair, and `clean_for_storage` fails CLOSED on a dangling open tag. A
    request that redacts away entirely returns "" rather than falling through
    to the next user message — the opening request WAS the private one, and
    silently substituting a later turn would misreport the session.
    """
    for msg in messages[:max_scan]:
        message = msg.get("message", {})
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content_field = message.get("content", "")
        candidate = ""
        if isinstance(content_field, str):
            candidate = content_field
        elif isinstance(content_field, list):
            for block in content_field:
                if isinstance(block, dict) and block.get("type") == "text":
                    if (block.get("text") or "").strip():
                        candidate = block["text"]
                        break
        if candidate.strip():
            return clean_for_storage(candidate)[:500]
    return ""


_ATTEMPT_FILE = ".pre_compact_attempt.json"


def _write_attempt(memory_dir, trigger, claude_sid, transcript_bytes):
    """Record that a PreCompact run STARTED, before any slow work.

    A hook killed by the host's timeout dies on TerminateProcess, which runs no
    `except` block and no `finally` — so a crashed run leaves .last_save.json
    untouched and the next SessionStart happily reports the PREVIOUS success.
    This marker is the only trace such a run can leave: it is written at entry
    and removed only on a completed run, so "marker still present" == "the last
    attempt did not finish".
    """
    try:
        payload = {
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger,
            "session_id": claude_sid,
            "pid": os.getpid(),
            "transcript_bytes": transcript_bytes,
        }
        # PID-suffixed temp name: a fixed one is shared by any concurrent run,
        # and two interleaved truncating writes would leave a mangled marker
        # that the reader has to defend against.
        tmp = memory_dir / f"{_ATTEMPT_FILE}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(memory_dir / _ATTEMPT_FILE))
    except OSError as e:
        # why: the marker is diagnostic only; failing to write it must never
        # stop the compaction work it is meant to observe
        _log.error(f"attempt marker write failed: {e}")


def _clear_attempt(memory_dir):
    try:
        (memory_dir / _ATTEMPT_FILE).unlink()
    except OSError:
        # why: absent or locked marker is harmless — a stale marker only ever
        # causes an advisory "previous run did not finish" line at SessionStart
        pass


# Bound on the collision-suffix search below. 200 same-millisecond archives in
# one project is already far past anything reachable; the loop must terminate.
_ARCHIVE_TS_TRIES = 200


def _reserve_archive_ts(memory_dir, now):
    """Return a session-archive stem no other run can already be using.

    core.progress.write_session_archive names the file ``session_<stem>.md``
    and writes it unconditionally, and ``sessions.archive_path`` has no
    uniqueness constraint — so a stem that already exists on disk DESTROYS that
    archive. Second-resolution stems made that routine: two PreCompacts in one
    wall-clock second produced two `sessions` rows pointing at ONE file, the
    first session's transcript gone with no error anywhere (measured: 12 real
    compactions across two windows -> 3 archive files on disk). The rows still
    render in `cli/mem.py sessions` and the dashboard, so the misattribution is
    user-visible while the loss is silent.

    Two defences, because neither alone closes it:
      * millisecond precision, so the ordinary case (a manual /compact landing
        in the same second as an auto one) never collides at all — and the stem
        stays sortable and human-readable: ``20260805_170025_412``;
      * an ``O_CREAT|O_EXCL`` claim of the exact target path, which is atomic
        across processes, with a ``-N`` suffix for the loser. Precision alone
        narrows the race; it does not end it.

    The claimed placeholder is 0 bytes and write_session_archive overwrites it
    microseconds later. Both sides derive ``sessions/YYYY/MM`` from the SAME
    instant: this function from the ``now`` its caller passes, and
    core.progress.write_session_archive from the ``%Y%m%d`` prefix of the stem
    returned here — it must never take its own clock reading, or a claim and a
    write that straddle a month boundary would land in different directories,
    voiding this claim and orphaning the placeholder.
    """
    base = now.strftime("%Y%m%d_%H%M%S_") + now.strftime("%f")[:3]
    try:
        archive_dir = memory_dir / "sessions" / now.strftime("%Y/%m")
        archive_dir.mkdir(parents=True, exist_ok=True)
        for n in range(_ARCHIVE_TS_TRIES):
            stem = base if n == 0 else f"{base}-{n}"
            try:
                fd = os.open(str(archive_dir / f"session_{stem}.md"),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            os.close(fd)
            return stem
    except OSError as e:
        # why: this is a collision guard, not the archive write itself. A
        # failing probe must cost at most uniqueness, never the archive.
        _log.error(f"archive name reservation failed: {e}")
    return f"{base}-{os.getpid()}"


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception as exc:
        _log.error(f"stdin parse error: {exc}")
        sys.exit(0)

    # json.loads SUCCEEDS on well-formed non-object payloads (`null`, `42`,
    # `"s"`, `[1,2]`, `true`). The `.get()` calls below sit outside the try, so
    # without this guard those raise AttributeError, print a traceback to
    # stderr and exit 1 — two hook-contract violations at once.
    if not isinstance(data, dict):
        _log.warn(f"stdin payload is {type(data).__name__}, not an object")
        sys.exit(0)

    cwd = data.get("cwd", "")
    transcript_path = data.get("transcript_path", "")
    trigger = data.get("trigger", "auto")
    claude_sid = data.get("session_id", "")

    if not cwd or not transcript_path:
        _log.warn("missing cwd or transcript_path")
        sys.exit(0)

    # Project opt-out — MUST precede the try block below, whose first act is to
    # mkdir memory/ + sessions/ + topics/ in cwd. Placed after the empty-cwd
    # guard so `Path("").resolve()` can never widen the match to the
    # interpreter's own working directory. Logged (unlike UserPromptSubmit,
    # which fires every message) because PreCompact is rare and a skipped
    # handoff is worth being able to explain afterwards. No stdout: an excluded
    # project must produce no artifact at all, including a status line.
    if is_excluded(cwd):
        _log.info(f"skipped: {cwd} is in config.json excluded_projects")
        sys.exit(0)

    # Anchor AFTER the opt-out so a narrow per-subdirectory exclusion is not
    # widened away by resolving to its parent. This is the SECOND path that
    # mkdir's memory/ (see the line below), so leaving it on the raw cwd
    # would have kept a compaction able to create the very stray database
    # UserPromptSubmit no longer creates. Rare hook: it logs the redirection.
    cwd = str(project_root(cwd, log=_log))

    try:
        memory_dir = Path(cwd) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "sessions").mkdir(exist_ok=True)
        (memory_dir / "topics").mkdir(exist_ok=True)

        # Migrate old SESSION_HANDOFF.md aside (one-shot)
        migrate_legacy_handoff(memory_dir)

        # Ensure memory/.gitignore covers everything we generate — including the
        # attempt marker written a few lines below. This runs on EVERY compaction
        # rather than only at project creation: user_prompt's initializer returns
        # early once memory.db exists, so an install created before an artifact
        # was introduced would otherwise never learn to ignore it.
        ensure_memory_gitignore(memory_dir)

        db = MemoryDB(memory_dir / "memory.db")
        project_id = db.upsert_project(cwd)
        project_name = Path(cwd).name

        # Marker FIRST — before the transcript load. A kill during the load is
        # precisely the failure this marker exists to make visible, so writing
        # it afterwards would leave the original symptom untraceable.
        try:
            _tp_bytes = os.path.getsize(transcript_path)
        except OSError:
            # why: size is diagnostic decoration on the marker; an unreadable
            # transcript is handled by the loader's own degradation path below
            _tp_bytes = 0
        _write_attempt(memory_dir, trigger, claude_sid, _tp_bytes)

        # Bounded head+tail read. An unbounded load here is what killed this
        # hook on large projects: a 2.1 GiB transcript parses at ~25 MiB/s
        # (~88s) and the sync leg only has 120s total, of which the LLM leg can
        # claim 2 × _API_TIMEOUT. See core.extractor.load_transcript_window.
        window = load_transcript_window(transcript_path)
        messages = window.messages
        if not messages:
            _log.info("empty transcript, skipping")
            _clear_attempt(memory_dir)  # nothing was lost; don't report a kill
            sys.exit(0)
        if window.truncated:
            _log.info(
                f"transcript windowed: {window.total_bytes/1024**2:.0f} MiB / "
                f"{window.total_records} records -> {len(messages)} read "
                f"(head {len(window.head)} + tail {len(window.tail)})"
            )
            if not window.tail:
                # One record larger than the tail budget. The window degrades to
                # head-only, i.e. the stale-content bug this release fixes — so
                # say so rather than shipping it silently.
                _log.warn(
                    "transcript tail window decoded EMPTY (a single record "
                    "exceeds the tail budget); extraction sees the head only"
                )

        project_kw = db.get_top_keywords(project_id, 40)
        ext = build_extraction(messages, project_kw,
                               total_records=window.total_records)

        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        # NOT now.strftime("%Y%m%d_%H%M%S"): that stem collides at second
        # resolution and the collision silently overwrites an existing
        # archive. See _reserve_archive_ts.
        file_ts = _reserve_archive_ts(memory_dir, now)

        # Session archive
        archive_text = _fmt_archive(ext, timestamp, trigger, project_name)
        archive_path = write_session_archive(memory_dir, project_name, archive_text, file_ts)

        # Session row
        session_id = db.insert_session(
            project_id=project_id,
            claude_session_id=claude_sid,
            trigger_type=trigger,
            msg_count=ext["msg_count"],
            archive_path=str(archive_path.relative_to(memory_dir)),
            brief_summary=archive_text[:1000],
        )

        # Observations from this session (for LLM context)
        last_session_ids = db.get_recent_session_ids(project_id, 2)
        last_ts = ""
        if len(last_session_ids) >= 2:
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT compacted_at FROM sessions WHERE id = ?",
                    (last_session_ids[1],),
                ).fetchone()
                if row:
                    last_ts = row["compacted_at"]
        if last_ts:
            observations = db.get_observations_since(project_id, last_ts)
        else:
            # FIRST compaction of a project: insert_session above already ran,
            # so last_session_ids has exactly ONE entry (this session) and there
            # is no previous compacted_at to bound by. The old `else []` threw
            # away every observation the session had accumulated, blanking
            # PROGRESS.md §6 ("*(no files touched)*") and the session summary's
            # files_read/files_modified on the one compaction a fresh project
            # gets. Fall back to the recent-N window — the same tier-2C fallback
            # session_start.py uses. Returned newest-first; reversed so the
            # downstream ordering (LLM context, files_read/modified) stays
            # chronological like get_observations_since.
            observations = list(reversed(db.get_recent_observations(project_id, 40)))

        # LLM extraction → upsert through memory_writer (anti-patch path)
        extracted = _extract_via_llm(messages, observations,
                                     total_records=window.total_records) or []
        method = "llm" if extracted else "none"

        counts = upsert_batch(db, project_id, session_id, extracted, memory_dir=memory_dir)
        n_inserted = counts.get("inserted", 0)
        n_merged   = counts.get("merged", 0)
        n_super    = counts.get("superseded", 0)
        n_skipped  = counts.get("skipped", 0)

        # Update keyword vocabulary
        if ext["keywords"]:
            db.upsert_keywords(project_id, ext["keywords"])

        # Session summary (structured)
        obs_files_read = list(dict.fromkeys(
            o["tool_input"] for o in observations
            if o["tool_name"] == "Read" and o["tool_input"]
        ))
        obs_files_modified = list(dict.fromkeys(
            o["tool_input"] for o in observations
            if o["tool_name"] in ("Edit", "Write", "MultiEdit") and o["tool_input"]
        ))
        # HEAD slice, not `messages`: the session's opening request lives in the
        # first records. A tail-only window would lose it entirely.
        first_user_msg = _first_user_request(window.head)
        task_mems = [m["content"] for m in extracted if m.get("category") == "task"]
        # Pull the CURRENT todo snapshot from the transcript (last TodoWrite
        # tool_use). This is the live state — much more reliable than LLM-
        # extracted "task" memories or regex aggregation. Used as the primary
        # source for both PROGRESS.md.open_todos and session_summary.next_steps.
        latest_todos = ext.get("latest_todos") or []
        pending_todos = [t["content"] for t in latest_todos
                         if t.get("status") != "completed"]
        next_steps_str = (
            "; ".join(pending_todos[:5]) if pending_todos
            else "; ".join(task_mems[:5])
        )

        try:
            db.insert_session_summary(session_id, project_id, {
                "request": first_user_msg,
                "investigated": ", ".join(obs_files_read[:10]),
                "learned": "",
                "completed": ", ".join(obs_files_modified[:10]),
                "next_steps": next_steps_str,
                "notes": "",
                "files_read": obs_files_read[:20],
                "files_modified": obs_files_modified[:20],
            })
        except Exception as e:
            _log.error(f"session summary save error: {e}")

        # === PROGRESS.md: FULL REWRITE from authoritative state ===========
        progress_state = collect_progress_state(
            db, project_id, memory_dir,
            current_request=first_user_msg,
            todos=latest_todos or ext.get("todos", []),
            files_read=obs_files_read,
            files_modified=obs_files_modified,
            transcript_ptr=str(Path(transcript_path).resolve()),
            trigger_type=trigger,
        )
        # v5: tag the session BEFORE the full-rewrite. upsert_progress
        # preserves the (sid, started_at) tag when the caller doesn't pass
        # it (see db.upsert_progress docstring), so the tag we set here
        # survives the rewrite and PROGRESS.md §0 attributes this compaction
        # to the right session.
        db.tag_progress_session(project_id, claude_sid)
        db.upsert_progress(project_id, **progress_state)
        progress_path = write_progress_md(db, project_id, memory_dir)

        # Clean up observations consumed by this extraction.
        # NOTE the format: observations.timestamp is written by db._now() as
        # `datetime.now().isoformat(timespec="seconds")` — "2026-08-05T14:03:11"
        # — and db.cleanup_observations compares it as a STRING. `timestamp`
        # above is space-separated ("2026-08-05 14:03:11") and ord(' ')=32 <
        # ord('T')=84, so every row from the SAME DAY sorted ABOVE the bound and
        # survived: the rows this extraction just consumed were exactly the ones
        # never cleaned. Pass the same ISO shape the rows are stored in.
        if observations:
            db.cleanup_observations(project_id, now.isoformat(timespec="seconds"))

        # MEMORY.md was already regenerated by upsert_batch. Touch again
        # to make sure it reflects any non-batch state changes.
        regenerate_memory_index(db, project_id, memory_dir)

        # Save status (consumed by SessionStart for the footer warning)
        status = {
            "timestamp": timestamp,
            "trigger": trigger,
            "method": method,
            "n_inserted": n_inserted,
            "n_merged": n_merged,
            "n_superseded": n_super,
            "n_skipped": n_skipped,
            "n_observations": len(observations),
            "msg_count": ext["msg_count"],
            "transcript_ptr": str(Path(transcript_path).resolve()),
            "success": True,
        }
        try:
            (memory_dir / ".last_save.json").write_text(
                json.dumps(status, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            # why: status file is purely cosmetic for the next SessionStart;
            # disk failure here must not propagate into the compact path
            _log.error(f".last_save.json write failed: {e}")

        # Run completed — retire the start marker so SessionStart doesn't
        # report this (successful) attempt as abandoned.
        _clear_attempt(memory_dir)

        # Consolidation runs in the sibling async hook (consolidate_async.py),
        # NOT here — see module docstring. This sync leg is done.
        _log.info(
            f"pre_compact OK: ins={n_inserted} mrg={n_merged} sup={n_super} skp={n_skipped} "
            f"obs={len(observations)} archive={archive_path.name}"
        )
        # One-line visible status for the next session
        print(
            f"[cc-memory] Pre-compact: "
            f"+{n_inserted} new, ~{n_merged} merged, ↻{n_super} superseded, "
            f"={n_skipped} skipped  ({ext['msg_count']} msgs, via {method}). "
            f"PROGRESS.md regenerated."
        )

    except Exception:
        _log.error_tb("pre_compact ERROR")
        try:
            memory_dir = Path(cwd) / "memory"
            (memory_dir / ".last_save.json").write_text(
                json.dumps({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "trigger": trigger,
                    "success": False,
                    "error": "see logs",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            # This run FAILED but it did not get killed — it reached its own
            # handler and recorded success:false above. Leaving the start marker
            # behind would make SessionStart report "killed before save" on
            # every start until the next fully successful compaction, i.e. a
            # confidently wrong cause. Retire it; .last_save.json carries the
            # real story.
            _clear_attempt(memory_dir)
        except OSError:
            # why: status write also failing means disk is unavailable; we
            # can't surface anything further without breaking the hook contract
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
