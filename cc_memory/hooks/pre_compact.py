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

from core.db import CATEGORIES, MemoryDB
from core.extractor import (build_extraction, load_transcript_window, group_sentences,
                            summarize_transcript, files_from_observations,
                            CATEGORY_ORDER, CATEGORY_LABELS)
from core.logger import get_logger
# Project opt-out. This USED to be a private literal copy in this file plus a
# second byte-identical one in hooks/user_prompt.py; the other four hooks
# <!--ce:hooks:asof--> had no check at all, so a project initialised BEFORE
# being listed stayed fully captured. One implementation now, called by all
# six. See core/modes.py.
# Shared entry ladder (v2.10.0): stdin parsing + the opt-out→anchor gate,
# once, in hooks/_entry.py — six hand-rolled copies is how guard drift
# between hooks kept becoming shipped defects (this hook's own junk-cwd
# database plant, fixed in v2.9.0, was a rung the other five carried).
from hooks._entry import parse_payload, resolve_project
# Privacy filter for the PROGRESS ingress below. The observation and memory
# write paths honoured <private> from v2.5.0; this hook's progress ingress
# never did (see _first_user_request).
from core.privacy import clean_for_storage, strip_harness_blocks
from core.layout import DB_FILENAME, memory_dir as resolve_memory_dir
from core.progress import (write_progress_md, write_session_archive, collect_progress_state,
                           migrate_legacy_handoff, ensure_memory_dir)
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

# Oldest-first observation slice fed to one extraction. The bound used to be
# an inline `[-50:]` INSIDE the prompt builder while the cleanup watermark
# covered everything read — the two disagreeing is register B3.
#
# 50 was BELOW THE ARRIVAL RATE, which makes the queue a leak rather than a
# buffer. Measured on this repository's own database: 1 575 observations over
# 18 compactions = 88 per compaction against a service rate of 50, so the
# backlog grew ~38 every time and `trim_observations` eventually deleted the
# oldest rows at the 5 000 cap — unread. B3 fixed WHICH end got fed and left
# the queue unable to drain, so the outcome its comment describes ("deleted
# having never been shown to any model") still arrived, just later.
#
# Two bounds now, because a row count alone does not bound a prompt: the
# character budget is what actually protects the 75 s LLM deadline, and the
# row cap sits above any realistic arrival rate so the queue converges.
# Measured on the same data: the oldest 300 rows truncated at 200 chars each
# cost 43.6 KiB, and 24 KiB buys ~226 rows at the observed 106-chars-per-row
# average — comfortably above 88, so the backlog shrinks every compaction.
# 200 chars per line matches what hooks/stop.py already feeds its observer.
_OBS_PER_EXTRACTION = 300
_OBS_CHARS_BUDGET = 24000
_OBS_LINE_CHARS = 200

_EXTRACTION_PROMPT = """\
You are a memory extraction system. Given a Claude Code conversation transcript, \
extract the most important information worth remembering across sessions.

Output a JSON array of objects with these fields:
- "category": one of """ + ", ".join(f'"{c}"' for c in CATEGORIES) + """
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


# THE transcript summariser lives in core.extractor (register M2): this
# file, session_start and the dashboard each carried a near-identical
# copy, and the v2.4.2 fill-direction fix had to be applied three times.
_build_transcript_summary = summarize_transcript


def _extract_via_llm(messages, observations=None, total_records=None):
    """Returns a (possibly EMPTY) list when extraction RAN, None when it did
    not — no key, nothing to summarise, or a failed call. The distinction is
    load-bearing (register C1): the caller deletes the observations it fed
    ONLY on a list, because a None used to be coerced to [] and the cleanup
    then destroyed every observation the model never saw.

    `observations` must already be the caller's bounded, oldest-first slice —
    every row passed in IS shown to the model, so the caller's consumption
    watermark and this prompt cannot disagree (register B3)."""
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
        obs_lines = [f"[{o['tool_name']}] "
                     f"{(o['tool_input'] or '')[:_OBS_LINE_CHARS]}"
                     for o in observations]
        if obs_lines:
            obs_context = "\n\nTool observations (for context):\n" + "\n".join(obs_lines)

    user_content = f"Extract memories from this conversation:\n\n{transcript_text}{obs_context}"

    try:
        from llm.ccl_backend import call_llm
        from llm.parse import extract_json
        text = call_llm(_EXTRACTION_PROMPT, user_content, api_key,
                        max_tokens=2500, timeout=_API_TIMEOUT,
                        fallback_timeout=_FALLBACK_TIMEOUT,
                        deadline=_HOOK_T0 + _LLM_DEADLINE_S)
        memories = extract_json(text, kind="array")
        if memories is None:
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
            if cat not in CATEGORIES:
                cat = "note"
            imp = max(1, min(int(imp), 5))
            valid.append({
                "category": cat, "content": content, "importance": imp,
                "topic": topic if isinstance(topic, str) else "",
            })
        _log.info(f"LLM extracted {len(valid)} memories")
        # An empty list is a RESULT (the model ran and found nothing worth
        # keeping), not a failure — see the docstring. `valid if valid else
        # None` conflated the two and cost the observations either way.
        return valid

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
        # Claude Code wraps a slash command in its own `<local-command-*>` /
        # `<command-*>` blocks and files them as a role=user record with
        # non-empty text — so a session opened with `/model` or `/compact`
        # made the FIRST match the harness's scaffolding, and its text
        # ("Caveat: … DO NOT respond to these messages …") became the stored
        # request. Seen in this repo's own committed PROGRESS.md §1, and
        # unrepairable afterwards: `session_start._refresh_progress_row` is
        # fill-only-empty by contract, so a wrong non-empty value is final.
        # A record that is ENTIRELY scaffolding strips to "" and we keep
        # scanning; a real request that merely FOLLOWS one keeps its words.
        candidate = strip_harness_blocks(candidate)
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
        # Component-by-component, NOT parents=True: parents=True materialises
        # the WHOLE chain, so a project deleted between ensure_memory_dir at
        # hook entry and this line (the transcript load sits between them) was
        # silently resurrected as an empty shell — the exact failure
        # ensure_memory_dir exists to refuse. Without parents, a missing
        # memory_dir raises FileNotFoundError into the handler below instead.
        for part in (memory_dir / "sessions",
                     memory_dir / "sessions" / now.strftime("%Y"),
                     archive_dir):
            part.mkdir(exist_ok=True)
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
    # Logged: PreCompact is rare, and a dropped compaction costs the handoff.
    data = parse_payload(log=_log)
    if data is None:
        sys.exit(0)

    cwd = data.get("cwd", "")
    transcript_path = data.get("transcript_path", "")
    trigger = data.get("trigger", "auto")
    claude_sid = data.get("session_id", "")

    # Coerce, don't abort: these two fields are ANNOTATION (a sessions-row
    # column and the PROGRESS.md §0 tag), while cwd/transcript_path below are
    # load-bearing. A list-valued `trigger` used to travel all the way to
    # db.insert_session, raise sqlite3.InterfaceError there, and the outer
    # handler then dropped the ENTIRE compaction — extraction, session
    # archive and the PROGRESS.md handoff — over a field whose only job is to
    # say "auto" or "manual" (measured: last_save.success=false, no
    # PROGRESS.md; session_id={} identically). stop.py/user_prompt.py exit
    # early instead because THEIR session_id names the marker files; here the
    # handoff is the point and must survive.
    if not isinstance(trigger, str) or not trigger:
        _log.warn(f"trigger is {type(trigger).__name__}; coercing to 'auto'")
        trigger = "auto"
    if not isinstance(claude_sid, str):
        _log.warn(f"session_id is {type(claude_sid).__name__}; coercing to ''")
        claude_sid = ""

    if not isinstance(cwd, str) or not isinstance(transcript_path, str):
        # The other five hooks <!--ce:hooks:subset--> all carry this
        # isinstance check; this one did not, and it is the only hook that
        # MKDIRS unconditionally. A payload
        # carrying `{"cwd": 123}` therefore went: is_excluded -> False (it
        # rejects non-strings by design), project_root -> raises -> _safe_path
        # -> Path("."), and the try block below then created memory/memory.db
        # in the HOOK PROCESS'S OWN directory — whatever the agent last cd'd
        # to. Measured: cwd=123 and cwd=["a"] each planted a database; the
        # other five hooks <!--ce:hooks:subset--> planted none. Exit 0 and
        # silent, per hook contract.
        _log.warn(f"cwd is {type(cwd).__name__}, transcript_path is "
                  f"{type(transcript_path).__name__}; both must be strings")
        sys.exit(0)

    if not cwd or not transcript_path:
        _log.warn("missing cwd or transcript_path")
        sys.exit(0)

    if "\x00" in cwd or "\x00" in transcript_path:
        # A NUL is the one character no filesystem accepts, and every stdlib
        # path call rejects it with `ValueError: embedded null character` —
        # NOT an OSError. That is what let it walk through every handler here:
        # the mkdir below raised, the outer `except Exception` caught it, and
        # then the RECOVERY path wrote `.last_save.json` under the same
        # poisoned cwd, raised the same ValueError, and escaped its narrower
        # `except OSError` to module level. Measured rc=1 with 1780 bytes of
        # traceback — the two things the hook contract forbids. Rejecting it
        # at the entry is one check that covers every downstream use, instead
        # of widening an except clause per call site.
        _log.warn("cwd or transcript_path contains a NUL; no filesystem "
                  "accepts it, so there is nothing this compaction can do")
        sys.exit(0)

    # Opt-out gate + root anchor via the ONE shared gate (hooks/_entry.py) —
    # MUST precede the try block below, whose first act is to mkdir the
    # state directory + sessions/ + topics/ in cwd (this is the SECOND path
    # that mkdirs it, so an unanchored cwd would keep a compaction able to create
    # the very stray database UserPromptSubmit no longer creates). Placed
    # after the empty-cwd guard so `Path("").resolve()` can never widen the
    # match to the interpreter's own working directory. Logged (unlike the
    # per-turn hooks) because PreCompact is rare and a skipped handoff is
    # worth being able to explain afterwards. No stdout: an excluded project
    # must produce no artifact at all, including a status line.
    resolved = resolve_project(cwd, log=_log)
    if resolved is None:
        _log.info(f"skipped: {cwd} is in config.json excluded_projects")
        sys.exit(0)
    cwd = resolved

    try:
        # ensure_memory_dir refuses a project directory that no longer exists
        # (FileNotFoundError -> the handler at the bottom of this try), and
        # writes .ccm/.gitignore on EVERY compaction rather than only at
        # project creation: user_prompt's initializer returns early once
        # memory.db exists, so an install created before an artifact was
        # introduced would otherwise never learn to ignore it.
        memory_dir = ensure_memory_dir(resolve_memory_dir(cwd, log=_log))

        # Migrate old SESSION_HANDOFF.md aside (one-shot)
        migrate_legacy_handoff(memory_dir)

        db = MemoryDB(memory_dir / DB_FILENAME)
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

        # Session archive. Written atomically since v2.8.0 — the plain write
        # it replaced tore under a concurrent reader (332 empty reads in 2,264
        # samples) and left a PERMANENT torn file, because `_reserve_archive_ts`
        # has already claimed this exact path and nothing rewrites it.
        #
        # Atomic means it can now RAISE where the truncating write silently
        # tore (a Windows sharing violation on `os.replace`). That must not
        # cost the compaction: PROGRESS.md is the handoff contract and the
        # archive is supporting evidence, so a failed archive downgrades to
        # "no archive path recorded" and everything else proceeds. The 0-byte
        # placeholder the reservation created stays on disk; the `sessions`
        # row simply does not point at it.
        archive_text = _fmt_archive(ext, timestamp, trigger, project_name)
        try:
            archive_path = write_session_archive(memory_dir, project_name,
                                                 archive_text, file_ts)
            archive_rel = str(archive_path.relative_to(memory_dir))
        except OSError as exc:
            _log.error(f"session archive not written ({exc}); the compaction "
                       f"continues — PROGRESS.md is the contract, the archive "
                       f"is evidence")
            archive_rel = ""

        # Session row
        session_id = db.insert_session(
            project_id=project_id,
            claude_session_id=claude_sid,
            trigger_type=trigger,
            msg_count=ext["msg_count"],
            archive_path=archive_rel,
            brief_summary=archive_text[:1000],
        )

        # Observations to feed this extraction. EVERYTHING still present for
        # this project: `cleanup_observations` below deletes exactly what is
        # consumed here, so whatever survives is by definition unconsumed.
        #
        # This used to bound on the PREVIOUS session's `compacted_at` — a
        # naive-local-time string. A clock that stepped back (DST fall-back,
        # NTP correction) put every subsequent observation BELOW that bound,
        # so extraction saw none of them and the cleanup at the bottom of
        # this function deleted them anyway: measured, 3 written, 0 seen, 3
        # destroyed. Reading "everything not yet cleaned" removes the
        # watermark from the read side entirely; the write side keeps one,
        # and it is now a monotonic row id.
        observations = db.get_observations_since(project_id, 0)
        # Feed the OLDEST prefix and watermark EXACTLY what was fed (register
        # B3): the prompt used to take the newest 50 while the cleanup bound
        # was the highest id READ, so on a >50-observation backlog the oldest
        # rows were deleted having never been shown to any model. Rows past
        # the prefix keep their ids above the watermark and are fed by the
        # next compaction; the deferral is logged (no silent caps). The
        # watermark also predates the slow LLM leg, so anything a concurrent
        # PostToolUse writes meanwhile survives too.
        obs_fed, _obs_chars = [], 0
        for _o in observations[:_OBS_PER_EXTRACTION]:
            _cost = len((_o["tool_input"] or "")[:_OBS_LINE_CHARS]) + len(
                _o["tool_name"] or "") + 4
            if obs_fed and _obs_chars + _cost > _OBS_CHARS_BUDGET:
                break
            obs_fed.append(_o)
            _obs_chars += _cost
        consumed_through = obs_fed[-1]["id"] if obs_fed else 0
        if len(observations) > len(obs_fed):
            _log.info(f"observations: feeding oldest {len(obs_fed)} of "
                      f"{len(observations)} ({_obs_chars} chars); the rest "
                      f"wait for the next run")

        # LLM extraction → upsert through memory_writer (anti-patch path).
        # None = did not run / failed; a list (even empty) = ran. Register C1:
        # the observations below are deleted only when extraction RAN.
        extracted = _extract_via_llm(messages, obs_fed,
                                     total_records=window.total_records)
        llm_ok = extracted is not None
        extracted = extracted or []
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
        obs_files_read, obs_files_modified =             files_from_observations(observations)
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

        summary_ok = True
        try:
            # `completed` and `learned` feed PROGRESS.md §2's **Done** and
            # **In-flight** through core/progress.py, so filling them with a
            # comma-joined path list and a hard-coded "" made the handoff
            # contract's status section a path dump and a permanently empty
            # line — visible in this repo's own committed PROGRESS.md. The
            # files already have their own columns two lines below; what §2
            # is asking for is what was CONCLUDED, and this function already
            # holds it in `extracted`. Split by category: outcomes vs. things
            # now known about the system.
            _done = [m["content"] for m in extracted
                     if m.get("category") in ("result", "decision")]
            _learned = [m["content"] for m in extracted
                        if m.get("category") in ("arch", "config", "bug")]
            db.insert_session_summary(session_id, project_id, {
                "request": first_user_msg,
                "investigated": ", ".join(obs_files_read[:10]),
                "learned": "; ".join(_learned[:5]),
                "completed": ("; ".join(_done[:5]) if _done
                              else ", ".join(obs_files_modified[:10])),
                "next_steps": next_steps_str,
                "notes": "",
                "files_read": obs_files_read[:20],
                "files_modified": obs_files_modified[:20],
            })
        except Exception as e:
            # The receipt below is conditional on this flag (register r6-B4):
            # swallowing the failure AND receipting made the transcript
            # permanently ineligible for retroactive repair while its summary
            # row simply did not exist.
            summary_ok = False
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

        # Clean up exactly the observations this extraction consumed — by ROW
        # ID, the same watermark the read used. The bound used to be a
        # wall-clock string, which needed the two formats to agree (they did
        # not: `timestamp` here is space-separated while rows store the `T`
        # form, and ord(' ') < ord('T') meant same-day rows survived) and
        # still deleted rows a backwards clock had hidden from extraction. An
        # id bound cannot disagree with itself, and anything written while
        # the LLM leg ran has a HIGHER id and is left for the next run.
        #
        # Only when extraction RAN (register C1): a failed or skipped call
        # used to delete every observation unread — a credential outage
        # measured 4 -> 0 rows with 0 memories extracted. Kept rows are
        # bounded by trim_observations below, and hitting that cap is logged.
        if llm_ok and consumed_through:
            db.cleanup_observations(project_id, consumed_through)
        elif not llm_ok and observations:
            _log.warn(f"extraction did not run; keeping "
                      f"{len(observations)} observation(s) for the next "
                      f"compaction")
        trimmed = db.trim_observations(project_id)
        if trimmed:
            _log.warn(f"observations over the {MemoryDB._MAX_OBSERVATIONS} "
                      f"cap: deleted the oldest {trimmed} row(s) that were "
                      f"NEVER fed to extraction. Arrival is outrunning "
                      f"_OBS_PER_EXTRACTION={_OBS_PER_EXTRACTION} / "
                      f"_OBS_CHARS_BUDGET={_OBS_CHARS_BUDGET}")

        # MEMORY.md was already regenerated by upsert_batch. Touch again
        # to make sure it reflects any non-batch state changes.
        regenerate_memory_index(db, project_id, memory_dir)

        # The RECEIPT half of insert_session's claim (register X6): the row
        # went in complete=0 up top, and readers that mean "this transcript
        # was saved" (_get_saved_session_ids) only believe complete=1. Every
        # dependent write — memories, summary, progress, cleanup — has landed
        # by this line, so a kill anywhere above leaves the row incomplete
        # and the next retroactive save re-transcribes instead of skipping.
        # A FAILED summary withholds the receipt too (register r6-B4): the
        # re-transcription is idempotent, a missing summary row is not.
        if summary_ok:
            db.mark_session_complete(session_id)
        else:
            _log.warn("summary write failed; session left incomplete so the "
                      "retroactive pass re-transcribes it")

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
            f"obs={len(observations)} archive={archive_rel or '(none)'}"
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
            # resolve_memory_dir never raises, unlike the bare join this
            # replaced — load-bearing in a LAST-RESORT handler.
            memory_dir = resolve_memory_dir(cwd)
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
        except Exception:
            # why: this is the LAST-RESORT handler, and a last-resort handler
            # that can itself raise is not one. It runs on the same `cwd` that
            # just failed, so whatever broke the main path breaks this one too
            # — measured with a NUL-bearing cwd, where the retry raised
            # `ValueError: embedded null character`, sailed past the previous
            # `except OSError`, and took the hook to rc=1 with a traceback.
            # Nothing further can be surfaced without breaking the contract.
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
