#!/usr/bin/env python3
"""Counterfactual falsification for the v2.8.0 fixes (rounds 3 through 6).

DISCIPLINE: every breakage is applied to a TEMPORARY COPY of the repo, never
to the working tree. A check that cannot go red against the state it exists
to catch is not a check — it is a comment that costs CI time.

Each case reverts ONE fix in the copy and asserts the corresponding gate
FAILS there, while the same gate passes on the untouched tree.

That second half is ESTABLISHED, not assumed (v2.14.0 — it had been assumed
since this file was written). `gate_baseline` runs each gate script once on an
untouched copy before any case gated on it is judged; a case whose gate is red
BEFORE the breakage is reported `UNSOUND` and counted as a failure, never as
`RED (detected)`. Without that negative control a gate red for an unrelated
reason "detects" every breakage put in front of it — measured on a box with no
tkinter, where three cases gated on `tests/test_surfaces.py` all reported RED.

    python tools/falsify_fixes.py             # every case
    python tools/falsify_fixes.py --list      # what each one breaks
    python tools/falsify_fixes.py --case tags # just one
    python tools/falsify_fixes.py --anchors   # anchors only, runs NO gate
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = "cc_memory"

# Anchor-building constants. A breakage anchor has to quote SOURCE text
# verbatim — including its quotes, backslashes and newlines — and doing
# that with ordinary escapes makes the anchors unreadable and, worse,
# easy to get wrong by one level (a doubled backslash matches nothing and
# reports as ROT). Composing them keeps each anchor legible.
BS = chr(92)      # a literal backslash
Q = chr(34)       # a literal double quote
N = chr(10)       # a real newline: anchors quote whole source LINES.
                  # A literal backslash-n INSIDE a source string is
                  # spelled Q + BS + "n" + Q, e.g. the .join(lines) sites.



def _copy_repo():
    box = Path(tempfile.mkdtemp(prefix="ccm-falsify-"))
    dst = box / "cc-memory"
    shutil.copytree(
        REPO, dst,
        # `.ce` is cc-enforcer's index (untracked, git-ignored) and its
        # daemon holds `index.db-shm` locked — copying it raised WinError 33
        # and took `--anchors` down with it. A live SQLite side file is never
        # part of the tree under test, whichever tool owns it.
        # Both state-directory names: `.ccm` since v2.13.0, `memory` for a
        # checkout whose first post-upgrade session has not migrated it yet.
        # Copying either drags the maintainer's live memory.db into every
        # falsification sandbox, and its -wal/-shm are the same WinError 33
        # the `.ce` note above describes.
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git",
                                      "dist", "build", ".ccm", "memory",
                                      ".ce", "*.db-shm", "*.db-wal"))
    # `.git` is omitted above (a full object database per case is minutes of
    # copying), but two gates ASK git about the copy: `smoke_test.py` runs
    # `git check-ignore` over eight shipped and two private paths, and its
    # citation gate enumerates markdown with `git ls-files` +
    # `--others --exclude-standard`. With no repository at all `check-ignore`
    # exits 128 — which reads as "not ignored", so the shipped-path half
    # passed VACUOUSLY and the private-path half failed outright. Measured the
    # moment `gate_baseline` was added: smoke_test was RED on an untouched
    # copy at `.gitignore no longer excludes per-user state:
    # .claude/settings.json`, and had been for every one of the 138 cases
    # gated on it. An empty repository is enough for both questions — the
    # `.gitignore` FILE is what `check-ignore` reads, and `ls-files` +
    # `--others` enumerate the same markdown when nothing is staged.
    try:
        subprocess.run(["git", "init", "-q"], cwd=str(dst),
                       capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass  # why: git may be absent on this box; the copy is still a
        # faithful subject for every gate that does not ask git, and the ones
        # that do now report a RED BASELINE — visibly UNSOUND — instead of
        # being silently judged against.
    return box, dst


def _patch(root, rel, old, new, count=1):
    path = root / rel
    text = path.read_text(encoding="utf-8")
    found = text.count(old)
    if found != count:
        raise SystemExit(
            f"BREAKAGE ANCHOR ROTTED: {rel} contains {found} copies of "
            f"{old[:70]!r}, expected {count}. Fix this script, not the tree.")
    path.write_text(text.replace(old, new), encoding="utf-8")


def _run(root, *args, timeout=900):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, *args], cwd=str(root),
                          capture_output=True, encoding="utf-8",
                          errors="replace", env=env, timeout=timeout)


# name -> (breakage(root), gate argv, one-line description)
CASES = {}


def case(name, gate, description):
    def register(fn):
        CASES[name] = (fn, gate, description)
        return fn
    return register


@case("cjk", ["tests/smoke_test.py"],
      "restore the English-only trigram set -> CJK correction falls below MID_SIM")
def _break_cjk(root):
    # anchor repaired 2026-08-09: r5 made the constant public (_CJK_RUN ->
    # CJK_RUN) and the old anchor aborted every full run
    _patch(root, "cc_memory/core/textsim.py",
           "    if not CJK_RUN.search(t):",
           "    if True:  # BREAKAGE: pretend nothing is CJK")


@case("tags", ["tests/smoke_test.py"],
      "MERGE replaces tags again -> the surviving row's provenance is destroyed")
def _break_tags(root):
    # anchor repaired 2026-08-09: r5 moved the merge into a merge_fields
    # callable handed to reconcile_upsert
    _patch(root, "cc_memory/llm/memory_writer.py",
           '"tags": _merged_tags(_row_tags(row), tags, ["merged"])}',
           '"tags": list(set(tags + ["merged"]))}  # BREAKAGE')


@case("tagcap", ["tests/smoke_test.py"],
      "remove the tag ceiling -> a 10,000-entry model-supplied list is stored")
def _break_tagcap(root):
    _patch(root, "cc_memory/llm/memory_writer.py",
           "MAX_TAGS = 32", "MAX_TAGS = 100000  # BREAKAGE")


@case("scan", ["tests/smoke_test.py"],
      "scan cap back to 50 -> a 0.95 match ranked 51st is inserted as new")
def _break_scan(root):
    _patch(root, "cc_memory/llm/memory_writer.py",
           "MAX_CANDIDATES_TO_SCAN = 500",
           "MAX_CANDIDATES_TO_SCAN = 50  # BREAKAGE")


@case("supersede", ["tests/smoke_test.py"],
      "split supersede back into two transactions -> a kill leaves both active")
def _break_supersede(root):
    path = root / "cc_memory" / "core" / "db.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("        now = self._now()\n        content_hash = "
                       "self.compute_content_hash(new_content)")
    end = text.index("    _journal_warned = False")
    text = text[:start] + (
        "        new_id = self.insert_memory(\n"
        "            project_id, session_id, category, new_content,\n"
        "            importance=importance, tags=tags, topic=topic,\n"
        "            supersedes_id=old_id,\n"
        "        )\n"
        "        self.archive_memory(old_id)   # BREAKAGE: second transaction\n"
        "        return new_id\n\n") + text[end:]
    path.write_text(text, encoding="utf-8")


@case("sqlvars", ["tests/smoke_test.py"],
      "un-chunk bulk_archive -> 'too many SQL variables' past 32766 ids")
def _break_sqlvars(root):
    _patch(root, "cc_memory/core/db.py", "    _SQL_VAR_CHUNK = 900",
           "    _SQL_VAR_CHUNK = 10 ** 9  # BREAKAGE")


@case("hashguard", ["tests/smoke_test.py"],
      "drop the snapshot condition -> a repaired row is archived anyway")
def _break_hashguard(root):
    # anchor repaired 2026-08-09: r5-X7 moved this guard from content_hash to
    # full content; the r3 revert (no condition at all) still applies
    _patch(root, "cc_memory/core/db.py",
           '"WHERE id = ? AND is_active = 1 AND content = ?",\n'
           "                    (now, mid, content)",
           '"WHERE id = ? AND is_active = 1",\n'
           "                    (now, mid)  # BREAKAGE")


@case("cycle", ["tests/smoke_test.py"],
      "remove the DAG guard -> archive_obsolete closes an A->B->A cycle")
def _break_cycle(root):
    _patch(root, "cc_memory/core/db.py",
           "                looped = [i for i in ids if i in chain_ids]\n"
           "                linked = [i for i in ids if i not in chain_ids]",
           "                looped = []  # BREAKAGE\n"
           "                linked = list(ids)")


@case("nonrecord", ["tests/smoke_test.py"],
      "accept non-record JSONL again -> build_extraction raises, handoff lost")
def _break_nonrecord(root):
    _patch(root, "cc_memory/core/extractor.py",
           "        if isinstance(record, dict):\n            out.append(record)",
           "        out.append(record)  # BREAKAGE")


@case("markerid", ["tests/smoke_test.py"],
      "truncate the session id again -> two sessions share every marker")
def _break_markerid(root):
    _patch(root, "cc_memory/core/markers.py",
           'return hashlib.sha256(session_id.encode("utf-8", "replace"))'
           '.hexdigest()[:16]',
           'return session_id[:16].replace("/", "_")  # BREAKAGE')


@case("symlink", ["tests/smoke_test.py"],
      "drop the portable link check -> read_marker follows a planted symlink")
def _break_symlink(root):
    # anchor repaired 2026-08-09 (r7-C1): _is_link gained a junction probe,
    # so zeroing only the S_ISLNK half no longer follows a planted link.
    _patch(root, "cc_memory/core/markers.py",
           "        if stat.S_ISLNK(os.lstat(str(path)).st_mode):\n"
           "            return True",
           "        if False:  # BREAKAGE\n            return True")
    _patch(root, "cc_memory/core/markers.py",
           "    isj = getattr(os.path, \"isjunction\", None)",
           "    isj = None  # BREAKAGE")


@case("creators", ["tests/smoke_test.py"],
      "forget the backstop creators -> the contract certifies 6 of 8")
def _break_creators(root):
    _patch(root, "tools/contracts.py",
           '    f"{PKG}/core/db.py": "self.db_path.parent.mkdir(",\n'
           '    f"{PKG}/ui/installer.py": "memory_dir.mkdir(",',
           "    # BREAKAGE: backstops removed")


@case("shed", ["tests/test_surfaces.py"],
      "shed without a response -> the client sees ConnectionResetError")
def _break_shed(root):
    _patch(root, "cc_memory/ui/web_viewer.py",
           "            _shed_response(request)\n",
           "            # BREAKAGE: no HTTP response\n")


@case("initret", ["tests/test_surfaces.py"],
      "return None from _init_project -> a refusal reports Success")
def _break_initret(root):
    _patch(root, "cc_memory/ui/installer.py",
           '            return ("refused", notice)',
           "            return None  # BREAKAGE")


@case("deadline", ["tests/test_surfaces.py"],
      "close instead of aborting the socket -> the drain blocks past the deadline")
def _break_deadline(root):
    _patch(root, "cc_memory/llm/ccl_backend.py",
           "            _abort_response(resp)",
           "            resp.close()  # BREAKAGE: drains the rest of the body")


@case("trigger", ["tests/test_surfaces.py"],
      "drop the annotation coercion -> a list trigger costs the whole compaction")
def _break_trigger(root):
    _patch(root, "cc_memory/hooks/pre_compact.py",
           '    if not isinstance(trigger, str) or not trigger:\n'
           '        _log.warn(f"trigger is {type(trigger).__name__}; coercing '
           "to 'auto'\")\n"
           '        trigger = "auto"',
           "    pass  # BREAKAGE: no trigger coercion")


@case("skillgate", ["tests/test_surfaces.py"],
      "put the opt-out back inside the anchoring try -> excluded project inited")
def _break_skillgate(root):
    path = root / "skills" / "ccm-load" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "    from core.modes import cli_opt_out_notice",
        "    from core.roots import project_root  # BREAKAGE: gate now shares\n"
        "    from core.modes import cli_opt_out_notice", 1)
    path.write_text(text, encoding="utf-8")


@case("gitignore", ["tests/test_surfaces.py"],
      "strict-decode .gitignore again -> a GBK line aborts the surface")
def _break_gitignore(root):
    _patch(root, "skills/ccm-load/SKILL.md",
           "_cur = gi.read_text(encoding='utf-8', errors='replace') "
           "if gi.exists() else ''",
           "_cur = gi.read_text(encoding='utf-8') if gi.exists() else ''")


@case("claimword", ["tests/test_surfaces.py"],
      "un-bound the back-scan -> 'done' binds as 1 and 'often' as 10")
def _break_claimword(root):
    _patch(root, "tools/doc_claims.py",
           'nums = re.findall(rf"({_NUM_BOUNDED})", head, re.IGNORECASE)',
           'nums = re.findall(rf"({_NUM})", head, re.IGNORECASE)  # BREAKAGE')


@case("claimof", ["tests/test_surfaces.py"],
      "remove the quantifier pattern -> 'seven of the hooks' is not a claim")
def _break_claimof(root):
    # anchor repaired 2026-08-09: the tuple gained TRIGGER_GAP_RE when the gate
    # learned the "6 command hooks" shape (one modifier word between the count
    # and the noun).
    _patch(root, "tools/doc_claims.py",
           "        (hit for pattern in (TRIGGER_OF_RE, TRIGGER_GAP_RE, "
           "TRIGGER_RE,\n                             TRIGGER_ZH_RE, "
           "TRIGGER_PAREN_RE)",
           "        (hit for pattern in (TRIGGER_GAP_RE, TRIGGER_RE,\n"
           "                             TRIGGER_ZH_RE, TRIGGER_PAREN_RE)"
           "  # BREAKAGE")


@case("pendingsync", ["tests/smoke_test.py"],
      "let TodoWrite sync into a plan pending refinement -> the gate empties")
def _break_pendingsync(root):
    _patch(root, f"{PKG}/core/plan.py",
           "    if raw_pending_refinement(row):",
           "    if False:  # BREAKAGE")


@case("recapture", ["tests/smoke_test.py"],
      "drop the re-capture archive -> an unrefined raw plan is lost silently")
def _break_recapture(root):
    _patch(root, f"{PKG}/core/plan.py",
           '    _prev = db.get_plan_active(project_id)\n'
           '    if _prev and (_prev.get("raw") or "").strip() not in ("", plan_text):',
           "    _prev = None  # BREAKAGE\n    if False:")


@case("dispreuse", ["tests/smoke_test.py"],
      "stop consuming dispositions -> one reason discharges four steps")
def _break_dispreuse(root):
    _patch(root, f"{PKG}/core/plan.py",
           "            if i in spent:\n                continue",
           "            pass  # BREAKAGE: dispositions are never consumed")


@case("donebar", ["tests/smoke_test.py"],
      "promote to done at 0.35 again -> a step exits the 0.50 gate one-way")
def _break_donebar(root):
    # anchor repaired 2026-08-09: r5-A1 widened the gate to every promotion
    # OUT of _UNFINISHED_STATUSES (done AND skipped) and rewrote the line
    _patch(root, f"{PKG}/core/plan.py",
           "        if (new_status not in _UNFINISHED_STATUSES\n"
           "                and old_status in _UNFINISHED_STATUSES\n"
           "                and not _carried(",
           "        if (False  # BREAKAGE: promote out of the gate at 0.35 again\n"
           "                and old_status in _UNFINISHED_STATUSES\n"
           "                and not _carried(")


@case("cjkbar", ["tests/smoke_test.py"],
      "share one bar with the writer -> the CJK refusal gate loosens")
def _break_cjkbar(root):
    _patch(root, f"{PKG}/core/plan.py",
           "CARRYOVER_MATCH_THRESHOLD_CJK = 2.0 / 3.0",
           "CARRYOVER_MATCH_THRESHOLD_CJK = 0.5  # BREAKAGE")


@case("planid", ["tests/smoke_test.py"],
      "un-guard the step id coercion -> a 1e999 id raises OverflowError")
def _break_planid(root):
    _patch(root, f"{PKG}/core/plan.py",
           '            try:\n                sid = int(s.get("id", i))',
           '            if True:\n                sid = int(s.get("id", i))')


@case("obswatermark", ["tests/smoke_test.py"],
      "bound observations by timestamp -> a backwards clock destroys them")
def _break_obswatermark(root):
    _patch(root, f"{PKG}/core/db.py",
           # anchor repaired 2026-08-09 (r7-B2): the reader gained LIMIT ?
           "                   WHERE project_id = ? AND id > ? AND is_private = 0\n"
           '                   ORDER BY id ASC LIMIT ?""",',
           "                   WHERE project_id = ? AND timestamp > ? "
           'AND is_private = 0\n                   ORDER BY timestamp ASC LIMIT ?""",')


@case("sessionorder", ["tests/smoke_test.py"],
      "order sessions by compacted_at -> the newest ranks last after a step back")
def _break_sessionorder(root):
    # anchor repaired 2026-08-09: r6-A6 added the complete=1 predicate; the
    # r3 revert (timestamp identity instead of id) keeps it
    # anchor repaired 2026-08-09 (r7-A1): the predicate is now
    # "receipted OR carrying an active memory"; the r3 revert (order by a
    # wall-clock string instead of the monotonic id) is unchanged.
    _patch(root, f"{PKG}/core/db.py",
           '                "ORDER BY s.id DESC LIMIT ?",',
           '                "ORDER BY s.compacted_at DESC LIMIT ?",')


@case("ftsrepair", ["tests/smoke_test.py"],
      "make _detect_fts5 observe only -> a missing index is never rebuilt")
def _break_ftsrepair(root):
    _patch(root, f"{PKG}/core/db.py",
           "        try:\n            with self._connect() as conn:\n"
           "                self._setup_fts5(conn)",
           "        try:\n            with self._connect() as conn:\n"
           "                pass  # BREAKAGE: observe, never repair")


@case("snapguard", ["tests/smoke_test.py"],
      "guard the snapshot archive on the dedup hash -> a case-only rewrite passes")
def _break_snapguard(root):
    _patch(root, f"{PKG}/core/db.py",
           '                    "WHERE id = ? AND is_active = 1 AND content = ?",\n'
           "                    (now, mid, content)",
           '                    "WHERE id = ? AND is_active = 1 AND content_hash = ?",\n'
           "                    (now, mid, MemoryDB.compute_content_hash(content))")


@case("queueback", ["tests/smoke_test.py"],
      "drop the approve/set-eval status predicate -> a DONE plan re-enters the queue")
def _break_queueback(root):
    _patch(root, f"{PKG}/cli/plan.py",
           "    _require_plans(db, pid, [args.id], statuses=_APPROVABLE_STATUSES)",
           "    _require_plans(db, pid, [args.id])  # BREAKAGE")


@case("execflag", ["tests/smoke_test.py"],
      "let a flag silently override the positional id -> the wrong plan runs")
def _break_execflag(root):
    _patch(root, f"{PKG}/cli/plan.py",
           "    if args.id is not None and (args.next or args.all):",
           "    if False:  # BREAKAGE")


@case("sensitive", ["tests/smoke_test.py"],
      "substring-match sensitive commands -> a read-only grep demands a check")
def _break_sensitive(root):
    _patch(root, f"{PKG}/core/plan.py",
           "    return bool(_SENSITIVE_CMD_RE.search(cmd.lower()))",
           '    return any(p in cmd.lower() for p in ("git push", "rm -rf",\n'
           '                                          "drop table"))  # BREAKAGE')


@case("guardreset", ["tests/smoke_test.py"],
      "carry drift counters across a replan -> the nudge fires on turn 0")
def _break_guardreset(root):
    # anchor repaired 2026-08-09: r5 folded the reset into the CAS-loop
    # fields dict (one indent deeper)
    _patch(root, f"{PKG}/core/plan.py",
           "            turns_since_last_guardian=0,\n"
           "            edits_since_last_guardian=0,\n",
           "")


@case("crheading", ["tests/smoke_test.py"],
      "split on '\\n' only -> a CR smuggles a forged '## 7.' heading through")
def _break_crheading(root):
    _patch(root, f"{PKG}/core/privacy.py",
           'for ln in re.split(r"\\r\\n|[\\n\\r  ]",\n'
           "                       neutralize_markers(text or \"\")):",
           'for ln in neutralize_markers(text or "").split("\\n"):')


@case("joinedtag", ["tests/smoke_test.py"],
      "neutralise per value only -> two rows reassemble a live authority tag")
def _break_joinedtag(root):
    # anchor repaired 2026-08-09 (round 8): the sweep moved off the whole join
    # onto parts[1:], so the plugin's own frame banners stop being escaped.
    _patch(root, f"{PKG}/hooks/session_start.py",
           'body = neutralize_document("\\n".join(parts[1:]))',
           'body = ("\\n".join(parts[1:]))  # BREAKAGE')


@case("layerbreak", ["tests/smoke_test.py"],
      "break instead of continue -> one oversized row empties a whole layer")
def _break_layerbreak(root):
    path = root / PKG / "hooks" / "session_start.py"
    text = path.read_text(encoding="utf-8")
    old = "            continue   # skip THIS entry; see _LAYER_SKIP_NOTE"
    if text.count(old) != 3:
        raise SystemExit(f"BREAKAGE ANCHOR ROTTED: {text.count(old)} skip sites")
    path.write_text(text.replace(old, "            break  # BREAKAGE"),
                    encoding="utf-8")


@case("footerbudget", ["tests/smoke_test.py"],
      "drop the footer clamp -> the one 'bounded' layer is the unbounded one")
def _break_footerbudget(root):
    _patch(root, f"{PKG}/hooks/session_start.py",
           "    if budget is not None and len(text) > budget:",
           "    if False:  # BREAKAGE")


@case("previewdecode", ["tests/smoke_test.py"],
      "strict-decode PROGRESS.md -> one GBK byte deletes the whole injection")
def _break_previewdecode(root):
    _patch(root, f"{PKG}/hooks/session_start.py",
           '        text = progress.read_text(encoding="utf-8", errors="replace")\n'
           "    except (OSError, ValueError):",
           '        text = progress.read_text(encoding="utf-8")\n'
           "    except OSError:")


@case("archiveatomic", ["tests/smoke_test.py"],
      "truncate-write session archives -> 14.7% of concurrent reads see 0 bytes")
def _break_archiveatomic(root):
    _patch(root, f"{PKG}/core/progress.py",
           "    write_atomic(archive_path, archive_text, budget_s=_DERIVED_BUDGET_S)",
           "    archive_path.write_text(archive_text, encoding=\"utf-8\", "
           "errors=\"replace\")")


@case("claimzh", ["tests/test_surfaces.py"],
      "narrow the Chinese trigger -> 六条钩子 and `6 个 hook` go unseen")
def _break_claimzh(root):
    # anchor repaired 2026-08-09: the package-prose scan gave TRIGGER_ZH_RE
    # the same dot/hyphen/word guards as the English patterns (it re-matched
    # every false positive they had just learned to decline)
    _patch(root, "tools/doc_claims.py",
           r'rf"(?<![每任这哪意第])(?<![A-Za-z.])(?P<n>{_NUM})\s*[个条道处项]?\s*"'
           "\n    "
           r'rf"(?P<noun>钩子|渲染路径|hooks?|render\s*paths?|renderers?)(?!\s*[-/])",',
           r'rf"(?<![每任这哪意第])(?P<n>{_NUM})\s*个?\s*(?P<noun>钩子|渲染路径)(?!\s*/)",')


# ── v2.8.0 rounds 5/6 ───────────────────────────────────────────────────────

@case("r6planrace", ["tests/smoke_test.py"],
      "bare INSERT for first-plan creation -> the losing creator raises")
def _break_r6planrace(root):
    _patch(root, f"{PKG}/core/db.py",
           '               ON CONFLICT(project_id) DO NOTHING""",',
           '               """,  # BREAKAGE: bare INSERT, the loser raises')


@case("r6rawguard", ["tests/smoke_test.py"],
      "retry a refine blindly -> an older refinement buries a newer capture")
def _break_r6rawguard(root):
    _patch(root, f"{PKG}/core/plan.py",
           '        elif old_row.get("needs_refine") and cur_raw != first_raw:',
           "        elif False:  # BREAKAGE: retry against whatever raw is there")


@case("r6failopen", ["tests/smoke_test.py"],
      "emit the remainder after a dangling open -> the scanner fails OPEN")
def _break_r6failopen(root):
    # anchor repaired 2026-08-09 (round 8): see r5spnopen.
    _patch(root, f"{PKG}/core/privacy.py",
           "    if all(d == 0 for d in depths) or not fail_closed:\n        out.append(text[pos:])",
           "    if True:  # BREAKAGE: fail OPEN\n        out.append(text[pos:])")


@case("r6quadratic", ["tests/smoke_test.py"],
      "re-search the suffix per token -> 32k dangling opens cost seconds again")
def _break_r6quadratic(root):
    # The breakage keeps the v2.14.0 case-insensitive tokens (`_token_re`) so
    # this case proves the QUADRATIC cost alone, not the case rule too.
    _patch(root, f"{PKG}/core/privacy.py",
           "    events = []  # (position, token_length, family, is_open)\n"
           "    for fi, (o, c) in enumerate(families):\n"
           "        for tok, is_open in ((o, True), (c, False)):\n"
           "            for m in _token_re(tok).finditer(text):\n"
           "                events.append((m.start(), m.end() - m.start(), fi, is_open))\n"
           "    events.sort()",
           "    events = []  # BREAKAGE: per-step suffix re-search (quadratic)\n"
           "    _scan = 0\n"
           "    while True:\n"
           "        best = None\n"
           "        for fi, (o, c) in enumerate(families):\n"
           "            for tok, is_open in ((o, True), (c, False)):\n"
           "                m = _token_re(tok).search(text, _scan)\n"
           "                if m and (best is None or m.start() < best[0]):\n"
           "                    best = (m.start(), m.end() - m.start(), fi, is_open)\n"
           "        if best is None:\n"
           "            break\n"
           "        events.append(best)\n"
           "        _scan = best[0] + best[1]")


@case("r5x1idx", ["tests/smoke_test.py"],
      "skip the unique-index heal -> the engine-level dup backstop is gone")
def _break_r5x1idx(root):
    _patch(root, "cc_memory/core/db.py",
           '            with self._connect() as conn:\n                conn.execute(\n                    f"CREATE UNIQUE INDEX IF NOT EXISTS "\n                    f"{self._ACTIVE_HASH_INDEX} "\n                    f"ON memories(project_id, content_hash) "\n                    f"WHERE is_active = 1")\n',
           '            # BREAKAGE (r5-X1a revert): the presence-check heal no longer\n            # creates the engine-level uniqueness backstop, so any path that\n            # bypasses reconcile_upsert can re-grow duplicate active rows.\n            pass\n')


@case("r5x3ref", ["tests/smoke_test.py"],
      "drop the never-referenced predicate -> an injected row is archived")
def _break_r5x3ref(root):
    _patch(root, "cc_memory/core/db.py",
           '        if require_never_referenced:\n            guard += " AND last_referenced_at IS NULL"\n',
           '        if False:  # BREAKAGE (r5-X3 revert): the WRITE no longer re-asserts\n            # the never-referenced predicate the snapshot verdict rests on\n            guard += " AND last_referenced_at IS NULL"\n')


@case("r5x7cnt", ["tests/smoke_test.py"],
      "ignore expected_contents -> a repaired row is archived on a stale verdict")
def _break_r5x7cnt(root):
    _patch(root, "cc_memory/core/db.py",
           '                if expected_contents is None:\n',
           '                if True:  # BREAKAGE (r5-X7/X2 revert): the write no longer\n                    # re-asserts the content the verdict was computed FROM\n')


@case("r6a1dflt", ["tests/smoke_test.py"],
      "sessions.complete DEFAULT 1 again -> an old hook's insert reads as a receipt")
def _break_r6a1dflt(root):
    _patch(root, "cc_memory/core/db.py",
           '    ("v7_sessions_complete",\n     "ALTER TABLE sessions ADD COLUMN complete INTEGER NOT NULL DEFAULT 0"),\n',
           '    # BREAKAGE (r6-A1a revert): DEFAULT 1 — during the mixed-version upgrade\n    # window a still-running v2.7 hook INSERTs without naming the column and\n    # the default becomes a receipt it never earned (X6 re-opened).\n    ("v7_sessions_complete",\n     "ALTER TABLE sessions ADD COLUMN complete INTEGER NOT NULL DEFAULT 1"),\n')


@case("r6a1bfil", ["tests/smoke_test.py"],
      "drop the backfill -> pre-upgrade rows are unreceipted claims forever")
def _break_r6a1bfil(root):
    _patch(root, "cc_memory/core/db.py",
           '    ("v7_sessions_complete_backfill",\n     "UPDATE sessions SET complete = 1"),\n',
           '    # BREAKAGE (r6-A1b revert): no one-shot backfill — every PRE-upgrade\n    # sessions row stays an unreceipted claim forever, so its transcript is\n    # eligible for retroactive re-save and its memories drop out of recency.\n')


@case("r6a6recid", ["tests/smoke_test.py"],
      "drop complete=1 from recency -> killed claims evict saved sessions")
def _break_r6a6recid(root):
    # anchor repaired 2026-08-09 (r7-A1): r6-A6's bare complete=1 became
    # "receipted OR carrying an active memory". Reverting to NO predicate
    # is still exactly the r6-A6 breakage: an EMPTY killed claim consumes a
    # recency slot and evicts a truly-saved session.
    _patch(root, "cc_memory/core/db.py",
           '                "AND (s.complete = 1 OR EXISTS (SELECT 1 FROM memories m "\n'
           '                "     WHERE m.session_id = s.id AND m.is_active = 1)) "\n',
           '                # BREAKAGE (r6-A6 revert): unreceipted CLAIM rows consume\n'
           '                # recency slots again, so killed compactions push truly-saved\n'
           '                # sessions (and their memories) out of the window.\n')


@case("r6a13rai", ["tests/smoke_test.py"],
      "swallow every IntegrityError as a skip -> an FK violation loses the write")
def _break_r6a13rai(root):
    _patch(root, "cc_memory/core/db.py",
           '            winner = self.find_by_hash(project_id, h)\n            if winner is None:\n',
           '            winner = self.find_by_hash(project_id, h) or {"id": None}\n            if False:  # BREAKAGE (r6-A13 revert): EVERY IntegrityError is\n                # treated as the benign duplicate race again, so a foreign-key\n                # or CHECK violation is reported as a successful-looking skip\n                # for a write that was silently LOST\n')


@case("r5d8fld", ["tests/smoke_test.py"],
      "drop unknown plan fields silently again -> a typo'd field write vanishes")
def _break_r5d8fld(root):
    _patch(root, "cc_memory/core/db.py",
           '        unknown = set(fields) - cls._PLAN_ACTIVE_FIELDS\n        if unknown:\n            raise ValueError(\n                f"unknown plan_active field(s): {sorted(unknown)} — writable "\n                f"columns are {sorted(cls._PLAN_ACTIVE_FIELDS)}")',
           '        # BREAKAGE: pre-fix INSERT-branch silent drop\n        for _k in set(fields) - cls._PLAN_ACTIVE_FIELDS:\n            fields.pop(_k, None)')


@case("r5spnopen", ["tests/smoke_test.py"],
      "fail OPEN on a dangling open tag -> the tail leaks past the span")
def _break_r5spnopen(root):
    # anchor repaired 2026-08-09 (round 8): the tail guard gained `or not
    # fail_closed`, for strip_harness_blocks only. `<private>` still fails
    # closed, which is exactly what this case proves.
    _patch(root, "cc_memory/core/privacy.py",
           '    if all(d == 0 for d in depths) or not fail_closed:\n        out.append(text[pos:])',
           '    if True:  # BREAKAGE: fail OPEN again on a dangling open tag\n        out.append(text[pos:])')


@case("r5spndepth", ["tests/smoke_test.py"],
      "first close ends the span again -> a nested open escapes early")
def _break_r5spndepth(root):
    _patch(root, "cc_memory/core/privacy.py",
           '        elif depths[fam] > 0:\n            depths[fam] -= 1',
           '        elif depths[fam] > 0:\n            depths[fam] = 0  # BREAKAGE: first close ends the span, no depth')


@case("r6c8split", ["tests/smoke_test.py"],
      "splitlines() in the small branch -> CR-separated counts disagree")
def _break_r6c8split(root):
    _patch(root, "cc_memory/core/extractor.py",
           '                total = sum(1 for l in data.split(b"\\n") if l.strip())',
           '                total = sum(1 for l in data.splitlines() if l.strip())  # BREAKAGE')


@case("r5d7parsed", ["tests/smoke_test.py"],
      "count parsed instead of present records -> the two branches disagree")
def _break_r5d7parsed(root):
    _patch(root, "cc_memory/core/extractor.py",
           '                total = sum(1 for l in data.split(b"\\n") if l.strip())',
           '                total = len(msgs)  # BREAKAGE: parsed count, not present count')


@case("r5y1roots", ["tests/smoke_test.py"],
      "follow a symlinked memory/ in _has_db -> rung 0 adopts a linked identity")
def _break_r5y1roots(root):
    # anchor repaired 2026-08-09: junction-aware probe, same change as r5y1emd.
    # anchor repaired 2026-08-30 (v2.13.0): `_has_db` became a LOOP over both
    # state-directory names, so the guard is indented one level deeper and
    # `continue`s to the next candidate instead of returning. The name itself
    # is `core.layout.DB_FILENAME` now, not a literal. Same guard, same
    # breakage — this register anchors on text, so a refactor that leaves the
    # fix intact still rots it, which is exactly what CI caught here.
    _patch(root, "cc_memory/core/roots.py",
           '            if _markers_is_link(mem) or _markers_is_link(mem / DB_FILENAME):\n                continue\n',
           '            if False:  # BREAKAGE: pre-fix, a linked state dir was followed\n                continue\n')


@case("r5y1emd", ["tests/smoke_test.py"],
      "write through a linked memory/ again -> ensure_memory_dir stops refusing")
def _break_r5y1emd(root):
    # anchor repaired 2026-08-09: the probe became core.markers._is_link
    # (junction-aware) — S_ISLNK alone is False for a Windows junction, so the
    # is_symlink()-only guard was inert on the primary platform.
    _patch(root, "cc_memory/core/progress.py",
           '    if _markers_is_link(memory_dir):\n',
           '    if False:  # BREAKAGE: pre-fix, ensure_memory_dir wrote through links\n')


@case("r6c2db", ["tests/smoke_test.py"],
      "drop the constructor reparse probe -> MemoryDB opens through a link")
def _break_r6c2db(root):
    _patch(root, "cc_memory/core/db.py",
           '        for probe in (self.db_path.parent, self.db_path):\n            if self._is_reparse(probe):\n',
           '        for probe in ():  # BREAKAGE: pre-fix, MemoryDB opened through a link\n            if self._is_reparse(probe):\n')


@case("r6c4own", ["tests/smoke_test.py"],
      "own any command that MENTIONS the path -> a user hook is claimed as ours")
def _break_r6c4own(root):
    _patch(root, "cc_memory/ui/installer.py",
           '        if t2.lower().endswith(".py"):\n            return bool(_CCM_COMMAND_RE.search(t2))\n    return False\n',
           '        if t2.lower().endswith(".py"):\n            pass  # BREAKAGE: ownership was a regex match ANYWHERE in the command\n    return bool(_CCM_COMMAND_RE.search(cmd))\n')


@case("r6c6keys", ["tests/smoke_test.py"],
      "emit dict keys raw from MCP -> a stored topic name reaches the model live")
def _break_r6c6keys(root):
    _patch(root, "cc_memory/mcp/server.py",
           '        return {_defang(k) if isinstance(k, str) else k: _defang(v)\n                for k, v in obj.items()}\n',
           '        # BREAKAGE: pre-fix, only VALUES were neutralised\n        return {k: _defang(v) for k, v in obj.items()}\n')


@case("r5d2supp", ["tests/smoke_test.py"],
      "drop the supplementary CJK intervals -> Ext-B corrections fall to trigrams")
def _break_r5d2supp(root):
    _patch(root, "cc_memory/core/textsim.py",
           '    "\U00020000-\U0002EE5D"   # Extensions B-F + I (r6-C5: I begins at U+2EBF0)\n'
           '    "\U0002F800-\U0002FA1F"   # Compatibility Supplement\n'
           '    "\U00030000-\U000323AF"   # Extensions G-H\n'
           '"]+")',
           '"]+")  # BREAKAGE: BMP only — supplementary planes fall to trigrams')


@case("r6c5exti", ["tests/smoke_test.py"],
      "end the first interval before Ext-I -> Ext-I alone falls through")
def _break_r6c5exti(root):
    _patch(root, "cc_memory/core/textsim.py",
           '    "\U00020000-\U0002EE5D"   # Extensions B-F + I (r6-C5: I begins at U+2EBF0)',
           '    "\U00020000-\U0002EBE0"   # BREAKAGE: ends before Ext-I (U+2EBF0)')


@case("r6l4txn", ["tests/smoke_test.py"],
      "archive losers before the survivor check -> a stale verdict half-applies")
def _break_r6l4txn(root):
    _patch(root, "cc_memory/core/db.py",
           '        with self._connect() as conn:\n'
           '            conn.execute("BEGIN IMMEDIATE")\n'
           '            row = conn.execute(\n'
           '                "SELECT content FROM memories WHERE id = ? AND is_active = 1",\n'
           '                (survivor_id,)).fetchone()',
           '        self.archive_obsolete(  # BREAKAGE: losers in their own txn\n'
           '            losers, canonical_id=survivor_id,\n'
           '            expected_contents=expected_loser_contents)\n'
           '        with self._connect() as conn:\n'
           '            conn.execute("BEGIN IMMEDIATE")\n'
           '            row = conn.execute(\n'
           '                "SELECT content FROM memories WHERE id = ? AND is_active = 1",\n'
           '                (survivor_id,)).fetchone()')


@case("r6l3imp", ["tests/smoke_test.py"],
      "drop the importance predicate -> an importance-only bump is archived")
def _break_r6l3imp(root):
    _patch(root, "cc_memory/core/db.py",
           '        if max_importance is not None:\n'
           '            guard += " AND importance <= ?"\n'
           '            guard_params.append(max_importance)',
           '        if False:  # BREAKAGE: importance-only bumps archived again\n'
           '            guard += " AND importance <= ?"\n'
           '            guard_params.append(max_importance)')


@case("r6l5topic", ["tests/smoke_test.py"],
      "split relabel and summary drop into two transactions again")
def _break_r6l5topic(root):
    _patch(root, "cc_memory/core/db.py",
           '            if drop_summary is not None:\n'
           '                conn.execute(\n'
           '                    "DELETE FROM topics WHERE project_id = ? AND name = ?",\n'
           '                    (project_id, drop_summary))',
           '        if drop_summary is not None:  # BREAKAGE: second transaction\n'
           '            with self._connect() as conn2:\n'
           '                conn2.execute(\n'
           '                    "DELETE FROM topics WHERE project_id = ? AND name = ?",\n'
           '                    (project_id, drop_summary))')


@case("r6l2gc", ["tests/smoke_test.py"],
      "drop the trace guards -> a claim with attached memories is deleted")
def _break_r6l2gc(root):
    # anchor repaired 2026-08-09 (r7-H2): the archive_path clause is gone —
    # an archive FILE is not database lineage, and requiring it made the
    # collector a no-op against its own dominant input. The two lineage
    # guards below are what must still refuse.
    _patch(root, "cc_memory/core/db.py",
           '                     AND id NOT IN (SELECT session_id FROM memories\n'
           '                                    WHERE session_id IS NOT NULL)\n'
           '                     AND id NOT IN (SELECT session_id FROM session_summaries\n'
           '                                    WHERE session_id IS NOT NULL)""",',
           '                     """,  # BREAKAGE: trace guards dropped')


@case("r5a1skip", ["tests/smoke_test.py"],
      "gate done only -> a cancelled todo retires a step below the bar")
def _break_r5a1skip(root):
    _patch(root, f"{PKG}/core/plan.py",
           "        if (new_status not in _UNFINISHED_STATUSES\n"
           "                and old_status in _UNFINISHED_STATUSES\n"
           "                and not _carried(",
           '        if (new_status == "done"  # BREAKAGE: skipped exits ungated\n'
           "                and old_status in _UNFINISHED_STATUSES\n"
           "                and not _carried(")


@case("r5a2fall", ["tests/smoke_test.py"],
      "skip the in_progress fallback -> an unmatched step loses the pointer")
def _break_r5a2fall(root):
    _patch(root, f"{PKG}/core/plan.py",
           "    if not active_step_id:\n"
           "        for s in steps:\n"
           '            if s.get("status") == "in_progress":\n'
           '                active_step_id = s.get("id", 0)\n'
           "                break",
           "    if not active_step_id:\n"
           "        for s in steps:\n"
           "            if False:  # BREAKAGE: pointer falls to first pending\n"
           '                active_step_id = s.get("id", 0)\n'
           "                break")


@case("r5a3slot", ["tests/smoke_test.py"],
      "stop consuming carry slots -> one new step discharges two old steps")
def _break_r5a3slot(root):
    _patch(root, f"{PKG}/core/plan.py",
           "        if best_slot < 0:\n"
           "            return False\n"
           "        spent_slots.add(best_slot)\n"
           "        return True",
           "        if best_slot < 0:\n"
           "            return False\n"
           "        pass  # BREAKAGE: slot never consumed\n"
           "        return True")


@case("r5a4born", ["tests/smoke_test.py"],
      "let steps born done be carry targets -> a disguised retirement passes")
def _break_r5a4born(root):
    _patch(root, f"{PKG}/core/plan.py",
           '        if _norm_status(s.get("status", "pending")) '
           "in _UNFINISHED_STATUSES:\n"
           "            cand_slots.extend((t, slot) for t in strings)",
           "        if True:  # BREAKAGE: born-done steps are carry targets\n"
           "            cand_slots.extend((t, slot) for t in strings)")


@case("r5a5claim", ["tests/smoke_test.py"],
      "trust the carried label -> a drop wearing 'carried' passes the gate")
def _break_r5a5claim(root):
    _patch(root, f"{PKG}/core/plan.py",
           '        elif action == "carried" and not _any_carried'
           "(title, all_step_strings):",
           "        elif False and not _any_carried"
           "(title, all_step_strings):  # BREAKAGE")


@case("r5a6null", ["tests/smoke_test.py"],
      "str(None) again -> a null goal/title validates as the string 'None'")
def _break_r5a6null(root):
    _patch(root, f"{PKG}/core/plan.py",
           '    return "" if x is None else str(x)',
           "    return str(x)  # BREAKAGE: JSON null becomes the word None")


@case("r5a7clean", ["tests/smoke_test.py"],
      "store the ExitPlanMode raw unwashed -> private spans reach PLAN.md")
def _break_r5a7clean(root):
    _patch(root, f"{PKG}/core/plan.py",
           "    plan_text = clean_for_storage(plan_text).strip()",
           '    plan_text = (plan_text or "").strip()  # BREAKAGE: unwashed')


@case("r5x4site", ["tests/smoke_test.py"],
      "write blind when the CAS misses -> a stale sync resurrects the plan")
def _break_r5x4site(root):
    _patch(root, f"{PKG}/core/plan.py",
           "    if not db.update_plan_if_revision(\n"
           '            project_id, row["revision"],\n'
           '            structured=updated, active_step=info["active_step_id"]):\n'
           '        return {"n_matched": 0, "n_unmatched": len(todos or []),\n'
           '                "active_step_id": 0, "skipped": "plan_changed"}',
           "    if not db.update_plan_if_revision(  # BREAKAGE: blind write\n"
           '            project_id, row["revision"],\n'
           '            structured=updated, active_step=info["active_step_id"]):\n'
           "        db.upsert_plan_active(project_id, structured=updated,\n"
           '                              active_step=info["active_step_id"])')


@case("r6b5null", ["tests/smoke_test.py"],
      "accept str(None) as a reason -> a reasonless drop wears four letters")
def _break_r6b5null(root):
    _patch(root, f"{PKG}/core/plan.py",
           '        reason = (_s(matched.get("reason")) or '
           '_s(matched.get("detail"))).strip()',
           '        reason = (str(matched.get("reason", "")) or '
           'str(matched.get("detail", ""))).strip()  # BREAKAGE')


@case("r6m1slice", ["tests/smoke_test.py"],
      "drop the outer-pair slice -> prose-wrapped and one-line-fenced payloads die")
def _break_r6m1slice(root):
    # 2026-08-09: this case replaced `r6a12fence`, whose target (a
    # fences-to-spaces variant) falsification proved UNREACHABLE — the slice
    # here is what actually carries both the A12 one-line-fence shape and
    # the prose-tolerance behaviour, so it is the thing worth breaking.
    _patch(root, f"{PKG}/llm/parse.py",
           "        if s >= 0 and e > s:\n"
           "            candidates.append(v[s:e + 1])",
           "        if False:  # BREAKAGE: no outer-pair slice\n"
           "            candidates.append(v[s:e + 1])")


@case("r5y5limit", ["tests/smoke_test.py"],
      "pass limits through unclamped -> --limit -1 dumps the whole table")
def _break_r5y5limit(root):
    _patch(root, f"{PKG}/cli/mem.py",
           "    n = int(value)\n"
           "    if n < 1:\n"
           "        raise argparse.ArgumentTypeError(\n"
           '            f"--limit must be >= 1 (SQLite reads a negative LIMIT as "\n'
           '            f"unbounded); got {n}")\n'
           "    return min(n, MemoryDB._MAX_SEARCH_LIMIT)",
           "    n = int(value)\n"
           "    if False:  # BREAKAGE: negative + oversized limits pass through\n"
           "        raise argparse.ArgumentTypeError(\n"
           '            f"--limit must be >= 1 (SQLite reads a negative LIMIT as "\n'
           '            f"unbounded); got {n}")\n'
           "    return n")


@case("r7idxname", ["tests/smoke_test.py"],
      "probe the index NAME again -> a wrong-shape index self-certifies")
def _break_r7idxname(root):
    _patch(root, f"{PKG}/core/db.py",
           '        row = conn.execute(\n'
           '            "SELECT sql FROM sqlite_master WHERE type = \'index\' AND name = ?",\n'
           "            (cls._ACTIVE_HASH_INDEX,)).fetchone()\n"
           '        if row is None:\n'
           '            return "absent"',
           '        row = conn.execute(  # BREAKAGE: name probe only\n'
           '            "SELECT sql FROM sqlite_master WHERE type = \'index\' AND name = ?",\n'
           "            (cls._ACTIVE_HASH_INDEX,)).fetchone()\n"
           '        return "absent" if row is None else "canonical"\n'
           '        if row is None:\n'
           '            return "absent"')


@case("r7wrote", ["tests/smoke_test.py"],
      "report wrote=1 after a rollback -> the result contradicts the DB")
def _break_r7wrote(root):
    _patch(root, f"{PKG}/core/db.py",
           '                return {"archived": n, "wrote": 0,\n'
           '                        "skipped": "canonical_collision"}',
           '                return {"archived": n, "wrote": 1,  # BREAKAGE\n'
           '                        "skipped": "canonical_collision"}')


@case("r7render", ["tests/smoke_test.py"],
      "drop the render generation check -> an older MEMORY.md overwrites a newer")
def _break_r7render(root):
    # anchor repaired 2026-08-09: the probe became `PRAGMA data_version` read
    # twice on ONE held connection. The old row-count/MAX() fingerprint was
    # blind to an in-place update landing in the same clock second.
    _patch(root, f"{PKG}/llm/memory_writer.py",
           "        if not _moved:\n"
           "            break",
           "        break  # BREAKAGE: write whatever was rendered, unordered")



# ── round 7 (cc-tree audit) ─────────────────────────────────────────────────
# One case per fix. Each was run individually and confirmed RED before being
# kept; `--anchors` verifies every anchor below still exists in the tree.

@case("r7dirpriv", ["tests/smoke_test.py"],
      "trust any directory -> the marker fallback lands in shared /tmp")
def _break_r7dirpriv(root):
    _patch(root, f"{PKG}/core/markers.py",
           "        return st.st_uid == getuid() and not (st.st_mode & 0o022)",
           "        return True  # BREAKAGE")


@case("r7junc", ["tests/smoke_test.py"],
      "drop the junction probe -> a planted reparse point reads as 'not a link'")
def _break_r7junc(root):
    _patch(root, f"{PKG}/core/markers.py",
           "    isj = getattr(os.path, " + Q + "isjunction" + Q + ", None)",
           "    isj = None  # BREAKAGE")


@case("r7ndprog", ["tests/smoke_test.py"],
      "escape per slot only -> PROGRESS.md reassembles a marker at the join")
def _break_r7ndprog(root):
    _patch(root, f"{PKG}/core/progress.py",
           "    text = neutralize_document(" + Q + BS + "n" + Q + ".join(lines))",
           "    text = " + Q + BS + "n" + Q + ".join(lines)  # BREAKAGE")


@case("r7ndplan", ["tests/smoke_test.py"],
      "escape per slot only -> PLAN.md reassembles a marker at the join")
def _break_r7ndplan(root):
    _patch(root, f"{PKG}/core/plan.py",
           "    # Assembled sweep — measured forgery across the Goal/Context join." + N
           + "    return neutralize_document(" + Q + BS + "n" + Q + ".join(lines))",
           "    return " + Q + BS + "n" + Q + ".join(lines)  # BREAKAGE")


@case("r7ndmem", ["tests/smoke_test.py"],
      "escape per slot only -> MEMORY.md reassembles a marker at the join")
def _break_r7ndmem(root):
    _patch(root, f"{PKG}/llm/memory_writer.py",
           "    return neutralize_document(" + Q + BS + "n" + Q + ".join(lines))",
           "    return " + Q + BS + "n" + Q + ".join(lines)  # BREAKAGE")


@case("r7arcname", ["tests/smoke_test.py"],
      "interpolate the archive filename raw -> the 'all values escaped' comment lies")
def _break_r7arcname(root):
    _patch(root, f"{PKG}/llm/memory_writer.py",
           "                rel = neutralize_inline(af.relative_to(memory_dir).as_posix())",
           "                rel = af.relative_to(memory_dir).as_posix()  # BREAKAGE")


@case("r7wordset", ["tests/smoke_test.py"],
      "Latin-only word grammar -> Cyrillic/Greek/Arabic nominate nothing")
def _break_r7wordset(root):
    _patch(root, f"{PKG}/core/textsim.py",
           "        words.update(w for w in re.findall(r" + Q + "[^" + BS + "W" + BS + "d_]{3,}" + Q + ", seg)" + N
           + "                     if not w.isascii())",
           "        pass  # BREAKAGE")


@case("r7claimmem", ["tests/smoke_test.py"],
      "believe only the receipt -> a killed compaction's memories vanish")
def _break_r7claimmem(root):
    _patch(root, f"{PKG}/core/db.py",
           '                "AND (s.complete = 1 OR EXISTS (SELECT 1 FROM memories m "' + N
           + '                "     WHERE m.session_id = s.id AND m.is_active = 1)) "' + N,
           '                "AND s.complete = 1 "  # BREAKAGE' + N)


@case("r7gcarch", ["tests/smoke_test.py"],
      "count the archive as lineage -> gc collects none of its real input")
def _break_r7gcarch(root):
    _patch(root, f"{PKG}/core/db.py",
           "                   WHERE project_id = ? AND complete = 0" + N
           + "                     AND compacted_at < ?" + N,
           "                   WHERE project_id = ? AND complete = 0" + N
           + "                     AND compacted_at < ?" + N
           + "                     AND (archive_path IS NULL OR archive_path = '')"
           + "  -- BREAKAGE" + N)


@case("r7j3dedup", ["tests/smoke_test.py"],
      "list compactions -> one session eats every 'prior session' slot")
def _break_r7j3dedup(root):
    _patch(root, f"{PKG}/core/db.py",
           # anchor repaired 2026-08-09 (round 8): the escape hatch is now
           # COALESCE(...) = '' — `''` is the sentinel pre_compact writes, and
           # `'' IS NULL` is false, so the old spelling collapsed them all.
           "                     AND (COALESCE(s.claude_session_id, '') = ''" + N
           + "                          OR s.id = (SELECT MAX(s2.id) FROM sessions s2" + N
           + "                                     WHERE s2.project_id = s.project_id" + N
           + "                                       AND s2.complete = 1" + N
           + "                                       AND s2.claude_session_id" + N
           + "                                           = s.claude_session_id))" + N,
           "                     -- BREAKAGE: fan-out restored" + N)


@case("r7obsbudget", ["tests/smoke_test.py"],
      "serve below the arrival rate -> the observation queue never drains")
def _break_r7obsbudget(root):
    _patch(root, f"{PKG}/hooks/pre_compact.py",
           "_OBS_PER_EXTRACTION = 300",
           "_OBS_PER_EXTRACTION = 50  # BREAKAGE")


@case("r7obsread", ["tests/smoke_test.py"],
      "unbounded oldest-first reader -> the Stop feed and its watermark disagree")
def _break_r7obsread(root):
    _patch(root, f"{PKG}/core/db.py",
           "                   ORDER BY id ASC LIMIT ?" + Q * 3 + ",",
           "                   ORDER BY id DESC LIMIT ?" + Q * 3 + ",  # BREAKAGE")


@case("r7harness", ["tests/smoke_test.py"],
      "take the first user record -> the harness caveat becomes the request")
def _break_r7harness(root):
    _patch(root, f"{PKG}/hooks/pre_compact.py",
           "        candidate = strip_harness_blocks(candidate)",
           "        pass  # BREAKAGE")


@case("r7stderr", ["tests/smoke_test.py"],
      "warn on stderr -> a hook paints an error banner on a successful call")
def _break_r7stderr(root):
    # The v2.7.0 form, restored verbatim from `git diff HEAD -- core/plan.py`.
    # An earlier draft of this case dropped only the `_log.warn` name and left
    # a bare `print()`, i.e. it moved the line to STDOUT — a real breach of the
    # same rule, which the then-current source grep did not match, so the case
    # reported GREEN. That is now case r7stdout below; this one is the stream
    # the rule is named for.
    _patch(root, f"{PKG}/core/plan.py",
           "        _log.warn(f" + Q + "plan history archive failed ({e}) — proceeding; " + Q + N
           + "                  f" + Q + "the carryover gate already enforced accounting" + Q + ")",
           "        import sys as _sys" + N
           + "        print(f" + Q + "[WARN] plan history archive failed ({e}) — proceeding; " + Q + N
           + "              f" + Q + "the carryover gate already enforced accounting" + Q + "," + N
           + "              file=_sys.stderr)")


@case("r7stdout", ["tests/smoke_test.py"],
      "warn on stdout -> a PostToolUse hook whose stdout must be empty speaks")
def _break_r7stdout(root):
    _patch(root, f"{PKG}/core/plan.py",
           "        _log.warn(f" + Q + "plan history archive failed ({e}) — proceeding; " + Q,
           "        print(f" + Q + "plan history archive failed ({e}) — proceeding; " + Q)


@case("r7planorder", ["tests/smoke_test.py"],
      "commit the row first -> the refiner refines a plan already replaced")
def _break_r7planorder(root):
    path = root / PKG / "core" / "plan.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("def capture_exit_plan_mode(")
    write_stmt = ('        write_atomic(memory_dir / ".plan_raw.md", plan_text,' + N
                  + "                     budget_s=_DERIVED_BUDGET_S)" + N)
    if write_stmt not in text[start:]:
        raise AssertionError("BREAKAGE ANCHOR ROTTED: plan.py no longer "
                             "publishes .plan_raw.md as one statement "
                             "inside capture_exit_plan_mode")
    i = text.index(write_stmt, start)
    commit = ("    db.upsert_plan_active(" + N
              + "        project_id," + N
              + "        raw=plan_text," + N
              + "        needs_refine=1," + N
              + "    )" + N)
    j = text.index(commit, start)
    assert i < j, "BREAKAGE ANCHOR ROTTED: plan.py already commits first"
    text = text[:i] + text[i + len(write_stmt):]
    j = text.index(commit, start)
    text = text[:j + len(commit)] + write_stmt + text[j + len(commit):]
    path.write_text(text, encoding="utf-8")


@case("r7ftsprobe", ["tests/smoke_test.py"],
      "probe the declaration -> a corrupt index reports HEALTHY")
def _break_r7ftsprobe(root):
    _patch(root, f"{PKG}/core/db.py",
           "                conn.execute(" + N
           + '                    "SELECT rowid FROM memories_fts "' + N
           + '                    "WHERE memories_fts MATCH ? LIMIT 1",' + N
           + '                    ("ccmemoryftshealthprobe",)).fetchall()' + N,
           '                conn.execute("SELECT rowid FROM memories_fts LIMIT 0")'
           + "  # BREAKAGE" + N)


@case("r7ftsguard", ["tests/smoke_test.py"],
      "catch only OperationalError -> a corrupt index raises past the LIKE fallback")
def _break_r7ftsguard(root):
    _patch(root, f"{PKG}/core/db.py",
           "                return [dict(r) for r in rows]" + N
           + "            except sqlite3.DatabaseError:",
           "                return [dict(r) for r in rows]" + N
           + "            except sqlite3.OperationalError:  # BREAKAGE")


@case("r7ftstrig", ["tests/smoke_test.py"],
      "keep the triggers -> 'LIKE fallback' is a total write outage")
def _break_r7ftstrig(root):
    _patch(root, f"{PKG}/core/db.py",
           "        for name in self._FTS_TRIGGERS:",
           "        for name in ():  # BREAKAGE")


@case("r7onconflict", ["tests/smoke_test.py"],
      "read then insert -> the first DB call every surface makes is a race")
def _break_r7onconflict(root):
    _patch(root, f"{PKG}/core/db.py",
           '                "ON CONFLICT(path) DO UPDATE SET last_active = excluded.last_active",',
           '                "",  # BREAKAGE')


@case("r7topiclimit", ["tests/smoke_test.py"],
      "unbounded topic scan -> 5000 rows fetched for a 500-row decision")
def _break_r7topiclimit(root):
    _patch(root, f"{PKG}/core/db.py",
           "                           ORDER BY importance DESC, created_at DESC" + N
           + "                           LIMIT ?" + Q * 3 + "," + N
           + "                        (project_id, topic, max_candidates)).fetchall()]",
           "                           ORDER BY importance DESC, created_at DESC" + Q * 3 + "," + N
           + "                        (project_id, topic)).fetchall()]  # BREAKAGE")


@case("r7j2sum", ["tests/smoke_test.py"],
      "hard-code the summary fields -> PROGRESS.md section 2 is a path dump")
def _break_r7j2sum(root):
    _patch(root, f"{PKG}/hooks/pre_compact.py",
           '                "learned": "; ".join(_learned[:5]),' + N
           + '                "completed": ("; ".join(_done[:5]) if _done' + N
           + '                              else ", ".join(obs_files_modified[:10])),',
           '                "learned": "",  # BREAKAGE' + N
           + '                "completed": ", ".join(obs_files_modified[:10]),')


@case("r7mcpscope", ["tests/test_surfaces.py"],
      "drop the scope gate -> an injected model plants an importance-5 memory "
      "in another project")
def _break_r7mcpscope(root):
    _patch(root, f"{PKG}/mcp/server.py",
           "    own = _server_root()",
           "    own = None  # BREAKAGE")


@case("r7mcpanchor", ["tests/test_surfaces.py"],
      "compare the RAW argument -> a subdirectory of this project is refused")
def _break_r7mcpanchor(root):
    # The other half of the same gate. Comparing before anchoring is not a
    # weaker gate but a WRONG one: it refuses the server's own project spelled
    # as any subdirectory, on the one surface where the caller cannot see why.
    _patch(root, f"{PKG}/mcp/server.py",
           "    project = _anchor_mcp_project(project)",
           "    pass  # BREAKAGE: gate now compares the unanchored argument")



# ── round 8 (cc-tree round 2: attacking round 7's own fixes) ────────────────
# Round 7 fixed real defects and introduced these. Each case reverts ONE of the
# round-8 repairs; every one was run individually and confirmed RED.

@case("r8nest", ["tests/smoke_test.py"],
      "one peeling pass -> a depth-2 nested marker survives every render path")
def _break_r8nest(root):
    _patch(root, f"{PKG}/core/privacy.py",
           "    for _ in range(_MAX_MARKER_PASSES):" + N
           + "        peeled = _MARKER_TAG_RE.sub(_escape_tag, text)" + N
           + "        if peeled == text:" + N
           + "            break" + N
           + "        text = peeled" + N
           + "    else:" + N
           + "        if _MARKER_TAG_RE.search(text):" + N
           + "            text = text.replace(" + Q + "<" + Q + ", " + Q + "&lt;" + Q
           + ").replace(" + Q + ">" + Q + ", " + Q + "&gt;" + Q + ")" + N,
           "    text = _MARKER_TAG_RE.sub(_escape_tag, text)  # BREAKAGE" + N)


@case("r8harness", ["tests/smoke_test.py"],
      "harness strip fails CLOSED -> an ordinary question is truncated at the tag")
def _break_r8harness(root):
    _patch(root, f"{PKG}/core/privacy.py",
           "    return _strip_spans(text, _HARNESS_FAMILIES, fail_closed=False)",
           "    return _strip_spans(text, _HARNESS_FAMILIES)  # BREAKAGE")


@case("r8literal", ["tests/smoke_test.py"],
      "consume the unpaired tag -> the sentence loses the word being asked about")
def _break_r8literal(root):
    _patch(root, f"{PKG}/core/privacy.py",
           "    if not fail_closed:" + N + "        stacks = [[] for _ in families]",
           "    if False:  # BREAKAGE" + N + "        stacks = [[] for _ in families]")


@case("r8banner", ["tests/smoke_test.py"],
      "sweep the whole join -> the injection loses its own header and terminator")
def _break_r8banner(root):
    _patch(root, f"{PKG}/hooks/session_start.py",
           "    body = neutralize_document(" + Q + BS + "n" + Q
           + ".join(parts[1:])) if len(parts) > 1 else " + Q + Q + N
           + "    result = parts[0] + (" + Q + BS + "n" + Q + " + body if body else " + Q + Q + ")" + N
           + "    result = result + " + Q + BS + "n" + BS + "n" + Q + " + _BANNER_TAIL + " + Q + BS + "n" + Q + N,
           "    result = neutralize_document(" + Q + BS + "n" + Q + ".join(parts))"
           + "  # BREAKAGE" + N)


@case("r8emptysid", ["tests/smoke_test.py"],
      "IS NULL only -> every compaction the harness gave no session id collapses to one")
def _break_r8emptysid(root):
    _patch(root, f"{PKG}/core/db.py",
           "                     AND (COALESCE(s.claude_session_id, '') = ''",
           "                     AND (s.claude_session_id IS NULL  -- BREAKAGE")


@case("r8obsseed", ["tests/smoke_test.py"],
      "seed the observer at row 0 -> every session re-walks the whole backlog")
def _break_r8obsseed(root):
    _patch(root, f"{PKG}/core/db.py",
           "            seed = max(0, int(newest) - max(0, int(window)))",
           "            seed = 0  # BREAKAGE")


@case("r8obsmono", ["tests/smoke_test.py"],
      "plain assignment -> a slow session rewinds the shared observer cursor")
def _break_r8obsmono(root):
    _patch(root, f"{PKG}/core/db.py",
           '                "UPDATE projects SET obs_watermark = MAX(COALESCE(obs_watermark, 0), ?) "',
           '                "UPDATE projects SET obs_watermark = ? "  # BREAKAGE')


@case("r8ftsdrop", ["tests/smoke_test.py"],
      "drop the triggers on any error -> one handle unindexes every other handle's writes")
def _break_r8ftsdrop(root):
    _patch(root, f"{PKG}/core/db.py",
           "        if not drop_triggers:" + N + "            return",
           "        pass  # BREAKAGE")


@case("r8ftsempty", ["tests/smoke_test.py"],
      "trust an empty MATCH -> a frozen index reports the memory does not exist")
def _break_r8ftsempty(root):
    _patch(root, f"{PKG}/core/db.py",
           "                if not rows and not self._fts_triggers_present(conn):",
           "                if False:  # BREAKAGE")


@case("r8idxmem", ["tests/smoke_test.py"],
      "drop idx_memories_session -> the recency EXISTS scans memories per session")
def _break_r8idxmem(root):
    _patch(root, f"{PKG}/core/db.py",
           '     "CREATE INDEX IF NOT EXISTS idx_memories_session "' + N
           + '     "ON memories (session_id, is_active)"),',
           '     "SELECT 1"),  # BREAKAGE')


@case("r8idxsid", ["tests/smoke_test.py"],
      "drop idx_sessions_sid -> the per-turn timeline query is quadratic again")
def _break_r8idxsid(root):
    _patch(root, f"{PKG}/core/db.py",
           '     "CREATE INDEX IF NOT EXISTS idx_sessions_sid "' + N
           + '     "ON sessions (project_id, claude_session_id, id)"),',
           '     "SELECT 1"),  # BREAKAGE')


@case("r8planhook", ["tests/test_surfaces.py"],
      "plan control back under the mode gate -> the whole anchor is dead again")
def _break_r8planhook(root):
    # The v2.5.0 flagship defect, restored. `TodoWrite` is in every mode's
    # skip_tools and `ExitPlanMode` is in no mode's observe_tools, so under the
    # gate `_apply_plan_integration` never runs in ANY mode. It passed all nine
    # release gates in that state until §8i existed.
    path = root / PKG / "hooks" / "post_tool_use.py"
    text = path.read_text(encoding="utf-8")
    block = ("        try:" + N
             + "            _apply_plan_integration(db, project_id, cwd, tool_name, tool_input)" + N)
    gate = "        if should_observe(mode, tool_name):" + N
    i = text.index(block)
    j = text.index(gate)
    assert i < j, "BREAKAGE ANCHOR ROTTED: the plan block is already below the gate"
    text = text[:i] + text[i + len(block):]
    j = text.index(gate)
    moved = ("            _apply_plan_integration(db, project_id, cwd, tool_name, tool_input)" + N
             + "            try:" + N)
    text = text[:j + len(gate)] + moved + text[j + len(gate):]
    path.write_text(text, encoding="utf-8")


@case("r8antipatch", ["tests/smoke_test.py"],
      "a direct db.insert_memory caller -> <private> is never stripped from it")
def _break_r8antipatch(root):
    # The anti-patch contract was prose-only until tools/contracts.py computed
    # it. This adds the cheapest possible bypass: one caller outside the writer.
    path = root / PKG / "cli" / "mem.py"
    text = path.read_text(encoding="utf-8")
    marker = "def main("
    i = text.index(marker)
    text = (text[:i]
            + "def _breakage_direct_insert(db, pid):" + N
            + "    return db.insert_memory(pid, None, 'note', 'x', 3, [], '')" + N
            + N + N + text[i:])
    path.write_text(text, encoding="utf-8")


@case("r8mcpdot", ["tests/test_surfaces.py"],
      "compare unresolved paths -> the server refuses its OWN project spelled '.'")
def _break_r8mcpdot(root):
    _patch(root, f"{PKG}/mcp/server.py",
           "    if own is not None and not _same_root(project, own):",
           "    if own is not None and Path(project) != own:  # BREAKAGE")


# The two cases below break a CLAIM, not code: doc_claims scans package prose
# (Python docstrings/comments) and config.json since round 2 closed finding
# #5 — the markdown-only gate shipped "the seventh caller" (twelve surfaces),
# "66 call sites" (80) and "three hooks import this" (two hooks + core/idle).
# If a later change drops either scan, these go GREEN and flag it as vacuous.

@case("r8claimpy", ["tools/doc_claims.py"],
      "wrong hook count in a package docstring -> the md-only gate would ship it")
def _break_r8claimpy(root):
    _patch(root, f"{PKG}/core/modes.py",
           "because ALL SIX hooks",
           "because ALL SEVEN hooks")


@case("r8claimjson", ["tools/doc_claims.py"],
      "wrong hook count in config.json notes -> caught only because JSON is scanned")
def _break_r8claimjson(root):
    _patch(root, f"{PKG}/config.json",
           "all six hooks <!--ce:hooks-->",
           "all five hooks <!--ce:hooks-->")


@case("r8dashsweep", ["tests/test_surfaces.py"],
      "skip the assembled sweep -> a stranger's package.json is live authority")
def _break_r8dashsweep(root):
    _patch(root, f"{PKG}/ui/dashboard.py",
           '        return neutralize_document("\\n".join(sections))',
           '        return "\\n".join(sections)  # BREAKAGE: unswept')


@case("r8topicrows", ["tests/test_surfaces.py"],
      "memory_topics unbounded again -> every topic returned to the model")
def _break_r8topicrows(root):
    _patch(root, f"{PKG}/mcp/server.py",
           "    limit = args.get(\"limit\", _TOPICS_DEFAULT)\n"
           "    try:\n"
           "        limit = max(1, min(int(limit), 200))\n"
           "    except (TypeError, ValueError):\n"
           "        limit = _TOPICS_DEFAULT\n"
           "    rows = db.get_topics(pid, limit=limit)",
           "    rows = db.get_topics(pid)  # BREAKAGE: unbounded")


@case("r8topicbody", ["tests/test_surfaces.py"],
      "drop the body clip -> a 272 KB tool result reads as an answer")
def _break_r8topicbody(root):
    _patch(root, f"{PKG}/mcp/server.py",
           "_TOPIC_BODY_CHARS = 2000",
           "_TOPIC_BODY_CHARS = 10 ** 9  # BREAKAGE")


@case("r8memmdcap", ["tests/smoke_test.py"],
      "MEMORY.md topic list unbounded again -> ~298 KB at 2000 topics")
def _break_r8memmdcap(root):
    _patch(root, f"{PKG}/llm/memory_writer.py",
           "        for t in topics[:_MEMORY_MD_TOPICS]:",
           "        for t in topics:  # BREAKAGE: unbounded")


@case("r8arcorder", ["tests/smoke_test.py"],
      "rank archives by mtime again -> a restored archive outranks a newer one")
def _break_r8arcorder(root):
    _patch(root, f"{PKG}/llm/memory_writer.py",
           "        archive_files = []\n"
           "        try:\n"
           "            for year in sorted((d for d in sessions_dir.iterdir() "
           "if d.is_dir()),\n"
           "                               key=lambda d: d.name, reverse=True):\n"
           "                for month in sorted((d for d in year.iterdir() "
           "if d.is_dir()),\n"
           "                                    key=lambda d: d.name, "
           "reverse=True):\n"
           "                    archive_files += sorted(month.glob(\"*.md\"),\n"
           "                                            key=lambda p: p.name, "
           "reverse=True)\n"
           "                    if len(archive_files) >= 5:\n"
           "                        break\n"
           "                if len(archive_files) >= 5:\n"
           "                    break\n"
           "        except OSError:",
           "        archive_files = sorted(sessions_dir.rglob(\"*.md\"),\n"
           "                               key=lambda p: p.stat().st_mtime,\n"
           "                               reverse=True)  # BREAKAGE: mtime\n"
           "        try:\n"
           "            pass\n"
           "        except OSError:")


# ── v2.9.0 (dual-perspective review: this maintainer + codex) ───────────────
# One case per fix. Each was run individually and confirmed RED before being
# kept; `--anchors` verifies every anchor below still exists in the tree.

@case("r9chain", ["tests/smoke_test.py"],
      "overwrite an existing supersedes link -> the older version is orphaned")
def _break_r9chain(root):
    _patch(root, f"{PKG}/core/db.py",
           "                    set_sql = (\"is_active = 0, \"\n"
           "                               \"supersedes_id = COALESCE(supersedes_id, ?), \"\n"
           "                               \"updated_at = ?\")",
           "                    set_sql = (\"is_active = 0, \"  # BREAKAGE\n"
           "                               \"supersedes_id = ?, \"\n"
           "                               \"updated_at = ?\")")


@case("r9progtx", ["tests/smoke_test.py"],
      "split the progress bootstrap back out -> a stale verdict wipes a patch")
def _break_r9progtx(root):
    _patch(root, f"{PKG}/core/db.py",
           "        with self._connect() as conn:\n"
           "            conn.execute(\"BEGIN IMMEDIATE\")\n"
           "            conn.execute(\n"
           "                \"INSERT OR IGNORE INTO progress (project_id, updated_at) \"\n"
           "                \"VALUES (?, ?)\",\n"
           "                (project_id, now)\n"
           "            )",
           "        if not self.get_progress(project_id):  # BREAKAGE\n"
           "            self.upsert_progress(project_id)\n"
           "        with self._connect() as conn:")


@case("r9dataver", ["tests/smoke_test.py"],
      "fingerprint by row counts again -> a same-second update reads as no change")
def _break_r9dataver(root):
    # The breakage must model "the probe is BLIND", not "the probe always
    # fires": a first attempt at `_dv_before = 0` made every render look
    # moved, so the loop retried and the SECOND render — taken after the
    # concurrent write — was correct, and the case ran GREEN. That is the
    # retired fingerprint's real failure: it reported UNCHANGED across a
    # same-second in-place update.
    _patch(root, f"{PKG}/llm/memory_writer.py",
           "            _moved = (conn.execute(\"PRAGMA data_version\").fetchone()[0]\n"
           "                      != _dv_before)",
           "            _moved = False  # BREAKAGE: blind to a same-second update")


@case("r9tags", ["tests/smoke_test.py"],
      "iterate a str tags argument -> 'manual' is stored as five one-char tags")
def _break_r9tags(root):
    _patch(root, f"{PKG}/llm/memory_writer.py",
           "        if isinstance(group, str):",
           "        if False:  # BREAKAGE: pre-fix, a bare string was iterated")


@case("r9transit", ["tests/smoke_test.py"],
      "keep comparing a doomed anchor -> a row is archived with no live twin")
def _break_r9transit(root):
    _patch(root, f"{PKG}/core/consolidate.py",
           "                if mi[\"id\"] in to_archive:",
           "                if False:  # BREAKAGE: transitive chaining is back")


@case("r9todos", ["tests/smoke_test.py"],
      "strip a non-string todo content -> the whole compaction + handoff dies")
def _break_r9todos(root):
    _patch(root, f"{PKG}/core/extractor.py",
           "    c = item.get(\"content\")\n"
           "    return c.strip() if isinstance(c, str) else \"\"",
           "    return item.get(\"content\", \"\").strip()  # BREAKAGE")


@case("r9planslot", ["tests/smoke_test.py"],
      "interpolate the superseded goal raw -> PLAN.md grows a forged section")
def _break_r9planslot(root):
    _patch(root, f"{PKG}/core/plan.py",
           "            f\"- Goal: {neutralize_inline(superseded['goal'].strip())}\",",
           "            f\"- Goal: {superseded['goal'].strip()}\",  # BREAKAGE")


@case("r9cjkcrit", ["tests/smoke_test.py"],
      "flat 0.5 bar on CJK criteria -> a replaced Chinese criterion reads as carried")
def _break_r9cjkcrit(root):
    _patch(root, f"{PKG}/core/plan.py",
           "            if _best_title_match(c, candidates) < _carryover_bar(c)]",
           "            if _best_title_match(c, candidates) < "
           "CARRYOVER_MATCH_THRESHOLD]  # BREAKAGE")


@case("r9junction", ["tests/smoke_test.py"],
      "symlink-only link guard -> a Windows junction passes both fail-closed checks")
def _break_r9junction(root):
    _patch(root, f"{PKG}/core/progress.py",
           "    if _markers_is_link(memory_dir):",
           "    if memory_dir.is_symlink():  # BREAKAGE: blind to junctions")


@case("r9cliscope", ["tests/test_surfaces.py"],
      "unpredicated encoding-check -> --apply archives another project's rows")
def _break_r9cliscope(root):
    _patch(root, f"{PKG}/cli/mem.py",
           "            q = (f\"SELECT * FROM {table} WHERE project_id = ?\"\n"
           "                 + _ENCODING_SCAN_PREDICATE.get(table, \"\"))\n"
           "            try:\n"
           "                rows = conn.execute(q, (pid,)).fetchall()",
           "            q = f\"SELECT * FROM {table}\"  # BREAKAGE: no scope\n"
           "            try:\n"
           "                rows = conn.execute(q).fetchall()")


@case("r9supscope", ["tests/test_surfaces.py"],
      "drop the supersedes scope check -> another project's memory is printed")
def _break_r9supscope(root):
    _patch(root, f"{PKG}/cli/mem.py",
           "    head = db.get_memory(args.memory_id)\n"
           "    if head is not None and head[\"project_id\"] != pid:\n"
           "        print(f\"Error: memory {args.memory_id} belongs to a "
           "different project\")\n"
           "        sys.exit(1)",
           "    # BREAKAGE: any id in the file is walkable")


@case("r9instgrp", ["tests/test_surfaces.py"],
      "strip settings.json per-GROUP again -> a reinstall deletes a user hook")
def _break_r9instgrp(root):
    _patch(root, f"{PKG}/ui/installer.py",
           "            survivor, _n = _strip_ccm_entries(mg)\n"
           "            if survivor is None:\n"
           "                continue  # the group was entirely ours — the "
           "upgrade replaces it",
           "            if _is_ccm_group(mg):  # BREAKAGE: per-group again\n"
           "                continue\n"
           "            survivor = mg")


@case("r9instcas", ["tests/test_surfaces.py"],
      "return None for an absent settings.json -> the whole CAS is disarmed")
def _break_r9instcas(root):
    _patch(root, f"{PKG}/ui/installer.py",
           "    except FileNotFoundError:\n"
           "        return _FP_ABSENT",
           "    except FileNotFoundError:\n"
           "        return None  # BREAKAGE: conflated with 'no expectation'")


@case("r9manifest", ["tests/test_surfaces.py"],
      "record only this run's surfaces -> uninstall orphans every skipped one")
def _break_r9manifest(root):
    _patch(root, f"{PKG}/ui/installer.py",
           "             \"files\": sorted(set(written) | still_ours)}, indent=2),",
           "             \"files\": sorted(written)}, indent=2),  # BREAKAGE")


@case("r9bigstdin", ["tests/test_surfaces.py"],
      "cap PostToolUse stdin again -> a large tool result drops the whole event")
def _break_r9bigstdin(root):
    # anchor repaired 2026-08-10: v2.10.0 moved the stdin read from
    # post_tool_use.py into the shared hooks/_entry.py:parse_payload, so the
    # cap is re-applied there — same breakage, one level down.
    _patch(root, f"{PKG}/hooks/_entry.py",
           "        raw = sys.stdin.buffer.read()",
           "        raw = sys.stdin.buffer.read(1024 * 512)  # BREAKAGE")


@case("r9emptypr", ["tests/test_surfaces.py"],
      "guard the marker write on a truthy prompt -> the previous turn's request survives")
def _break_r9emptypr(root):
    _patch(root, f"{PKG}/hooks/user_prompt.py",
           "        prompt_file = marker_path(_PROMPT_FILE_PREFIX, safe)\n"
           "        try:",
           "        prompt_file = marker_path(_PROMPT_FILE_PREFIX, safe)\n"
           "        try:\n"
           "            if not prompt:  # BREAKAGE: skip the overwrite\n"
           "                raise OSError(\"skipped\")")


@case("r9jsonrpc", ["tests/test_surfaces.py"],
      "accept any jsonrpc member -> a 1.0 frame is answered as a valid Request")
def _break_r9jsonrpc(root):
    _patch(root, f"{PKG}/mcp/server.py",
           "    if req.get(\"jsonrpc\") != \"2.0\":",
           "    if False:  # BREAKAGE: the member is not checked")


@case("r9hdrdead", ["tests/test_surfaces.py"],
      "drop the header deadline -> 16 drip-feeders hold every admission permit")
def _break_r9hdrdead(root):
    _patch(root, f"{PKG}/ui/web_viewer.py",
           "        left = self.deadline - time.monotonic()\n"
           "        if left <= 0:",
           "        left = 1e9  # BREAKAGE: the header phase is unbounded\n"
           "        if left <= 0:")


@case("r9hookbind", ["tests/test_surfaces.py"],
      "unbind _HOOK_ORDER from hooks.json -> a new hook is covered by nothing")
def _break_r9hookbind(root):
    _patch(root, f"{PKG}/hooks/pre_compact.py",
           "def main():",
           "def main():\n"
           "    pass  # BREAKAGE placeholder so the manifest gains a 7th hook\n")
    spec = root / "hooks" / "hooks.json"
    data = json.loads(spec.read_text(encoding="utf-8"))
    # Structural, not textual: the entry is found by walking the parsed spec
    # for the pre_compact command, so a reformat of hooks.json cannot rot this
    # anchor. The check is still exact — one entry, or refuse.
    entries = [e for group in data.get("hooks", {}).values() for mg in group
               for e in mg.get("hooks", [])
               if "pre_compact.py" in str(e.get("command", ""))]
    if len(entries) != 1:
        raise SystemExit(
            f"BREAKAGE ANCHOR ROTTED: hooks/hooks.json holds {len(entries)} "
            f"pre_compact entries, expected 1. Fix this script, not the tree.")
    (root / PKG / "hooks" / "rogue.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8")
    rogue = dict(entries[0])
    rogue["command"] = entries[0]["command"].replace("pre_compact.py",
                                                     "rogue.py")
    for group in data["hooks"].values():
        for mg in group:
            if any("pre_compact.py" in str(e.get("command", ""))
                   for e in mg.get("hooks", [])):
                mg["hooks"].append(rogue)   # BREAKAGE: a 7th registered hook
                break
    spec.write_text(json.dumps(data, indent=2), encoding="utf-8")


@case("r9gateleak", ["tests/test_plan_carryover.py"],
      "run the carryover gate outside a sandbox -> it writes to the real home")
def _break_r9gateleak(root):
    _patch(root, "tests/test_plan_carryover.py",
           "assert Path.home() == _HOME, (",
           "assert Path.home() != _HOME, (  # BREAKAGE: sandbox inverted")


@case("r10dashrender", ["tests/test_surfaces.py"],
      "render the Progress/Plan tab without escaping -> a stored authority "
      "marker leaves the dashboard live")
def _break_r10dashrender(root):
    # NOT "raise ImportError above the fallback": the fallback ALSO escapes
    # (that is its whole point), so that breakage stays green — the same
    # inverted-counterfactual trap r9dataver fell into. Render RAW instead.
    _patch(root, f"{PKG}/ui/dashboard.py",
           "        try:\n"
           "            from core.privacy import neutralize_inline as _ni, \\\n"
           "                neutralize_markers as _nm\n"
           "        except Exception:\n"
           "            # why: a read-only view must render even on a broken install;\n"
           "            # over-escaping every '<' is the same fail-closed fallback the\n"
           "            # CLI's _neutralize uses (register Y3)\n"
           "            def _ni(t):\n"
           '                return str(t).replace("<", "&lt;")\n'
           "            _nm = _ni",
           "        def _ni(t):  # BREAKAGE: render raw\n"
           "            return str(t)\n"
           "        _nm = _ni")


@case("r10gateproxy", ["tools/doc_claims.py"],
      "gut is_excluded from the shared gate -> the registries must ERROR, "
      "not keep listing six protected hooks")
def _break_r10gateproxy(root):
    _patch(root, f"{PKG}/hooks/_entry.py",
           "    if is_excluded(cwd):\n"
           "        return None\n"
           "    return str(project_root(cwd, log=log))",
           "    return str(project_root(cwd, log=log))  # BREAKAGE: gate gutted")


@case("r10lograise", ["tests/test_surfaces.py"],
      "unguard the shared ladder's log call -> a broken logger crashes every "
      "hook at once")
def _break_r10lograise(root):
    _patch(root, f"{PKG}/hooks/_entry.py",
           "        try:\n"
           "            if log is not None:\n"
           '                log.error(f"stdin parse error: {exc}")\n'
           "        except Exception:\n"
           "            # why: this is the shared ladder for every hook, and the hook\n"
           "            # contract (exit 0, empty stderr) outranks the log line. The\n"
           "            # pre-v2.10.0 post_tool_use carried exactly this guard; losing\n"
           "            # it here would let a broken logger crash all six hooks\n"
           "            # <!--ce:hooks--> at once.\n"
           "            pass\n"
           "        return None",
           "        if log is not None:\n"
           '            log.error(f"stdin parse error: {exc}")  # BREAKAGE: guard removed\n'
           "        return None")


@case("r10entryorder", ["tests/test_surfaces.py"],
      "anchor before the opt-out in the shared gate -> a narrow exclusion is "
      "widened away for every hook at once")
def _break_r10entryorder(root):
    # v2.10.0 moved the six hand-rolled entry ladders into
    # hooks/_entry.py:resolve_project, so ONE inversion now breaks all six
    # hooks together — which is exactly why the gate must catch it: §4's
    # narrow-exclusion drive records the private zone's activity in the
    # PARENT project's database, and §7's source rule sees the order flip.
    _patch(root, f"{PKG}/hooks/_entry.py",
           "    if is_excluded(cwd):\n"
           "        return None\n"
           "    return str(project_root(cwd, log=log))",
           "    anchored = str(project_root(cwd, log=log))  # BREAKAGE: anchor first\n"
           "    if is_excluded(anchored):\n"
           "        return None\n"
           "    return anchored")


# ── v2.11.1: the enforcement engine, which shipped with ZERO coverage ──────
# v2.11.0 made the Stop hook able to REFUSE a turn and registered no case for
# any of it. Each breakage below reproduces the shipped defect exactly.

@case("r11budget", ["tests/test_directive_enforcement.py"],
      "discard write_marker's return -> the escape budget never releases and traps the session")
def _break_r11budget(root):
    # write_marker NEVER RAISES (its docstring's first line), so the original
    # `except OSError` was dead and the False return was dropped: nothing
    # persisted, every read came back empty, n stayed 1, and the block repeated
    # forever. Restoring the dead handler restores the trap.
    _patch(root, f"{PKG}/hooks/stop.py",
           "    if not write_marker(f, f\"{digest}:{n}\"):",
           "    write_marker(f, f\"{digest}:{n}\")\n"
           "    if False:  # BREAKAGE: the dead except-OSError shape")


@case("r11blockmarker", ["tests/test_directive_enforcement.py"],
      "stop escaping the block reason -> a stored directive forges a live <system-reminder>")
def _break_r11blockmarker(root):
    # The block `reason` is fed back to Claude as a DECISION, which is a higher
    # authority channel than PROGRESS.md. render_block_reason was the only
    # renderer in core/plan.py that did not neutralize.
    _patch(root, f"{PKG}/core/plan.py",
           '    return neutralize_document("\\n".join(lines))\n\n\ndef _render_directives_section',
           '    return "\\n".join(lines)  # BREAKAGE\n\n\ndef _render_directives_section')


@case("r11idle", ["tests/test_directive_enforcement.py"],
      "stamp every directive with the plan counter -> a just-stated directive blocks the turn")
def _break_r11idle(root):
    # anchor moved 2026-08-17: v9 replaced the touched-since heuristic with
    # subtraction on a monotonic clock, so the breakage is now "measure every
    # directive from the project counter again" rather than "drop the guard".
    _patch(root, f"{PKG}/hooks/stop.py",
           "        idle = turns_total - int(row.get(\"turns_at_touch\") or 0)",
           "        idle = turns_total  # BREAKAGE: project clock, not per-row")


@case("r11resetforgives", ["tests/test_directive_enforcement.py"],
      "measure idleness off the RESETTABLE counter -> plan-check forgives real neglect")
def _break_r11resetforgives(root):
    # The v2.11.1 approximation read `turns_since_last_guardian`, which
    # `/cc-mem plan-check` zeroes — so a directive untouched for 30 turns
    # looked freshly attended to the moment anyone ran a guardian check. This
    # is the exact property the v9 monotonic clock exists to hold.
    _patch(root, f"{PKG}/hooks/stop.py",
           '    turns_total = int(plan_row.get("turns_total") or 0)',
           '    turns_total = int(plan_row.get("turns_since_last_guardian")'
           ' or 0)  # BREAKAGE')


@case("r11tombstone", ["tests/test_directive_enforcement.py"],
      "enforce on a truthy plan row -> a CLEARED plan keeps refusing turns forever")
def _break_r11tombstone(root):
    # anchor moved 2026-08-16: the inline condition became the named predicate
    # core.plan.is_live_plan, precisely so a test could drive the HOOK'S rule
    # instead of re-implementing it (with it inlined, this case ran GREEN).
    _patch(root, f"{PKG}/core/plan.py",
           '    return bool(str(row.get("raw") or "").strip()\n'
           '                or str(row.get("structured") or "").strip())',
           '    return True  # BREAKAGE: a tombstone counts as a live plan')


@case("r11directiverace", ["tests/test_directive_enforcement.py"],
      "drop BEGIN IMMEDIATE -> concurrent creators of one slug collide on the unique index")
def _break_r11directiverace(root):
    # anchor moved 2026-08-17: v9 inserted the monotonic-clock read between the
    # BEGIN IMMEDIATE and the SELECT, so the old multi-line anchor no longer
    # matched. Anchored on the single line that IS the fix.
    _patch(root, f"{PKG}/core/db.py",
           '            conn.execute("BEGIN IMMEDIATE")\n'
           '            # v9: stamp the monotonic turn clock',
           '            # BREAKAGE: no write lock\n'
           '            # v9: stamp the monotonic turn clock')


@case("r11restate", ["tests/test_directive_enforcement.py"],
      "argparse defaults back to '' -> a bare re-statement wipes the directive's demand and quote")
def _break_r11restate(root):
    _patch(root, f"{PKG}/cli/mem.py",
           '    fields = {}\n    if args.quote:',
           '    fields = {"quote": args.quote, "demand": args.demand,\n'
           '              "kind": args.kind}  # BREAKAGE\n    if False and args.quote:')


@case("r11gitignore", ["tests/smoke_test.py"],
      "blanket-ignore dotted dirs -> .github/ goes to zero tracked files, invisibly")
def _break_r11gitignore(root):
    _patch(root, ".gitignore", ".*/\n!.github/\n!.claude-plugin/\n", ".*/\n")


@case("r11flattree", ["tests/smoke_test.py"],
      "drop a module from the flat-install diagram -> the docs describe a tree that is not shipped")
def _break_r11flattree(root):
    _patch(root, "docs/ARCHITECTURE.md",
           "├── hooks/   _entry.py consolidate_async.py",
           "├── hooks/   consolidate_async.py")


@case("r11entryreq", ["tests/smoke_test.py"],
      "drop hooks/_entry.py from _REQUIRED_PLUGIN_FILES -> status certifies a dead install healthy")
def _break_r11entryreq(root):
    _patch(root, f"{PKG}/cli/mem.py",
           '    "cc_memory/hooks/_entry.py",\n',
           '')


@case("r11doccoverage", ["tools/doc_coverage.py"],
      "undocument a schema column -> the code exposes a surface no spec mentions")
def _break_r11doccoverage(root):
    # THE case this gate was built for, replayed against the real history: the
    # v9 columns landed with zero mentions in the specification and every one
    # of the ten gates passed. Removing the sentence v2.11.3 added must now
    # turn the eleventh red.
    #
    # Targets `turns_at_touch`, which appears ONCE in that document.
    # `turns_total` appears twice (the plan_active row defines it, the
    # directives row references it), so patching only its definition left the
    # word present and this case ran GREEN — the gate was right and the
    # breakage was too small. A substring check is only falsified by removing
    # every occurrence.
    _patch(root, "docs/ARCHITECTURE.md",
           "Carries `turns_at_touch` since `v9_directives_turns_at_touch`",
           "BREAKAGE: the column is undocumented")


@case("r12nobump", ["tests/test_directive_enforcement.py"],
      "make directive-edit bump times_stated again -> nine repairs reorder the ledger")
def _break_r12nobump(root):
    # The anchor is unique to edit_directive: upsert_directive's tail also
    # stamps last_seen_at, so its sets-line differs.
    _patch(root, f"{PKG}/core/db.py",
           '            sets += ["updated_at = ?", "turns_at_touch = ?"]',
           '            sets += ["times_stated = times_stated + 1",  # BREAKAGE\n'
           '                     "updated_at = ?", "turns_at_touch = ?"]')


@case("r12constraint", ["tests/test_directive_enforcement.py"],
      "idle-enforce constraint directives again -> a prohibition blocks the turn forever")
def _break_r12constraint(root):
    _patch(root, f"{PKG}/core/plan.py",
           '        if row.get("kind") == "constraint":\n            continue',
           '        if False:  # BREAKAGE: constraints accrue idle again\n'
           '            continue')


@case("r12backlogrows", ["tests/smoke_test.py"],
      "kill the rows trigger -> a 50-row backlog under a fresh marker is never due")
def _break_r12backlogrows(root):
    _patch(root, f"{PKG}/core/consolidate.py",
           "    if n_new >= BACKLOG_ROWS:",
           "    if False:  # BREAKAGE: rows never trigger")


@case("r12posixuri", ["tests/smoke_test.py"],
      "restore the drive-path prefix for POSIX paths -> file://tmp/... reads "
      "'tmp' as a URI authority and `sql` fails on Linux/macOS")
def _break_r12posixuri(root):
    _patch(root, f"{PKG}/core/db.py",
           '    prefix = "file:" if posix_path.startswith("/") else "file:/"',
           '    prefix = "file:" if posix_path.startswith("//") else "file:/"'
           '  # BREAKAGE: v2.8.0-v2.12.0 rule')


@case("r12directiveinject", ["tests/test_directive_enforcement.py"],
      "drop the directives layer from the SessionStart injection -> the ledger "
      "never reaches the model again (the v2.12.1 state the README demo measured)")
def _break_r12directiveinject(root):
    _patch(root, f"{PKG}/hooks/session_start.py",
           "    if directives_text:\n        parts.append(directives_text)",
           "    if False:  # BREAKAGE: the ledger never reaches the model\n"
           "        parts.append(directives_text)")


@case("r12directiveplan", ["tests/test_directive_enforcement.py"],
      "stop passing the ledger to the PLAN.md renderer -> the guardian reads a "
      "PLAN.md with no standing directives")
def _break_r12directiveplan(root):
    _patch(root, f"{PKG}/core/plan.py",
           "                          directives=_active_directives(db, project_id))",
           "                          directives=None)  # BREAKAGE: ledger dropped")


@case("r12verbatim", ["tools/citation_check.py"],
      "re-apply the class of edit `--fix` made inside the quoted guardian report "
      "(a fixture line number rewritten) -> the verbatim region no longer matches its capture")
def _break_r12verbatim(root):
    _patch(root, "README.md",
           'tally/cli.py:12 — --file default now "tally.db"',
           'tally/cli.py:33 — --file default now "tally.db"')


@case("r12verbatimskip", ["tools/citation_check.py"],
      "drop a line from a quoted report with no [...] elision -> the segment "
      "spanning the gap is not in the capture")
def _break_r12verbatimskip(root):
    _patch(root, "README.md",
           "ALIGNMENT: on-track\n",
           "")


@case("r12scancap", ["tests/test_surfaces.py"],
      "remove the _is_container scan cap -> proving 'not a container' reads every "
      "subdirectory of every ancestor again (3.5-4.4 s per call under a 6,366-entry %TEMP%)")
def _break_r12scancap(root):
    _patch(root, f"{PKG}/core/roots.py",
           "                if examined > _CONTAINER_SCAN_CAP:\n",
           "                if False:  # BREAKAGE: unbounded scan\n")


@case("r12stepref", ["tests/test_directive_enforcement.py"],
      "blind the retargeted-reference branch -> a reference that reads right and points wrong passes silently")
def _break_r12stepref(root):
    _patch(root, f"{PKG}/core/plan.py",
           "            elif n in old_steps and not _carried(old_steps[n], "
           "new_steps[n]):",
           "            elif False:  # BREAKAGE: retargeting is invisible")


# ── identity round: the project row follows its database ─────────────────────

@case("r13reattach", ["tests/smoke_test.py"],
      "never re-attach a moved project's own row -> a rename mints a second row and every memory goes dark")
def _break_r13reattach(root):
    _patch(root, f"{PKG}/core/db.py",
           "        owner = database_owner(self.db_path)\n"
           "        if owner is None or not rows or canonical_path(owner) != canon:\n"
           "            return None",
           "        owner = database_owner(self.db_path)\n"
           "        if True:  # BREAKAGE: identity is the path string again\n"
           "            return None")


@case("r13statuscreate", ["tests/smoke_test.py"],
      "let `status` look the row up through upsert_project -> a health check mints a row in a foreign database")
def _break_r13statuscreate(root):
    _patch(root, f"{PKG}/cli/mem.py",
           "    pid = db.find_project_id(project)\n"
           "    if pid is None:\n"
           "        others = db.project_paths()",
           "    pid = db.upsert_project(project)  # BREAKAGE: a question creates\n"
           "    if pid is None:\n"
           "        others = db.project_paths()")


@case("r13markerid", ["tests/smoke_test.py"],
      "drop the marker's project_id match -> a rename makes the marker foreign and consolidation re-runs")
def _break_r13markerid(root):
    _patch(root, f"{PKG}/core/consolidate.py",
           "    if (project_id is not None and isinstance(stamped, int)\n"
           "            and not isinstance(stamped, bool) and stamped == project_id):\n"
           "        return marker",
           "    if False:  # BREAKAGE: the id no longer identifies the row\n"
           "        return marker")


@case("r13markerpath", ["tests/smoke_test.py"],
      "store the marker's cwd unresolved again -> the CLI's `--project .` writes '.' and no hook ever matches it")
def _break_r13markerpath(root):
    _patch(root, f"{PKG}/core/consolidate.py",
           '        "project_path": _resolved_text(cwd),',
           '        "project_path": str(cwd),  # BREAKAGE: relative spelling stored')


@case("r13markersame", ["tests/smoke_test.py"],
      "compare marker paths with bare normcase again -> an unresolved reader spelling reads as foreign")
def _break_r13markersame(root):
    _patch(root, f"{PKG}/core/consolidate.py",
           '    if not same_path(marker.get("project_path") or "", cwd):',
           '    if (__import__("os").path.normcase(str(marker.get("project_path") or ""))\n'
           '            != __import__("os").path.normcase(str(cwd))):  # BREAKAGE')


@case("r13home", ["tests/smoke_test.py"],
      "drop the resolved home spelling from the boundary set -> a symlinked home is walked into (detected on POSIX; on Windows without the symlink privilege the two spellings coincide)")
def _break_r13home(root):
    _patch(root, f"{PKG}/core/roots.py",
           "            out.add(_norm(Path(candidate).resolve()))",
           "            pass  # BREAKAGE: unresolved spelling only")


@case("r13skilldir", ["tests/smoke_test.py"],
      "make save-memories join the legacy state-directory name again -> memories written where nothing reads")
def _break_r13skilldir(root):
    _patch(root, "skills/save-memories/SKILL.md",
           "mem_dir = _state_dir(project)\n"
           "db = MemoryDB(mem_dir / 'memory.db')",
           "mem_dir = Path(project) / 'memory'\n"
           "db = MemoryDB(mem_dir / 'memory.db')")


@case("r13registry", ["tests/test_surfaces.py"],
      "fold the dashboard registry key with .lower() again -> two POSIX directories collapse into one entry (detected on POSIX)")
def _break_r13registry(root):
    _patch(root, f"{PKG}/ui/dashboard.py",
           "        return canonical_path(path)",
           "        return str(path).lower()  # BREAKAGE: folds case on every platform")


@case("r13handlefollow", ["tests/smoke_test.py"],
      "keep the constructed db_path after the state directory is renamed -> a long-lived dashboard / web-viewer handle fails on every operation")
def _break_r13handlefollow(root):
    _patch(root, f"{PKG}/core/db.py",
           "            if not self._follow_state_dir():\n"
           "                raise",
           "            if True:  # BREAKAGE: the stale path is kept\n"
           "                raise")


@case("r13privatecase", ["tests/smoke_test.py"],
      "match span tags case-sensitively again -> <PRIVATE>secret</PRIVATE> is neither stripped nor escaped")
def _break_r13privatecase(root):
    _patch(root, f"{PKG}/core/privacy.py",
           "        r = _TOKEN_RES[tok] = re.compile(re.escape(tok), re.IGNORECASE)",
           "        r = _TOKEN_RES[tok] = re.compile(re.escape(tok))  # BREAKAGE: case-sensitive")


@case("r13authhome", ["tests/smoke_test.py"],
      "call Path.home() bare again -> a missing home discards an explicit ANTHROPIC_API_KEY")
def _break_r13authhome(root):
    _patch(root, f"{PKG}/core/auth.py",
           "    try:\n"
           "        return Path.home() / \".claude\" / \".credentials.json\"\n"
           "    except Exception:",
           "    if True:  # BREAKAGE: the RuntimeError escapes with the key\n"
           "        return Path.home() / \".claude\" / \".credentials.json\"\n"
           "    if False:")


@case("r13budgetreset", ["tests/test_directive_enforcement.py"],
      "stop resetting the refusal streak when a turn may close -> the budget is per session, and enforcement is advisory after three resolved refusals")
def _break_r13budgetreset(root):
    _patch(root, f"{PKG}/hooks/stop.py",
           "        if not reasons:\n"
           "            # The turn may close, so the streak is over",
           "        if False:  # BREAKAGE: the streak survives a clean Stop\n"
           "            # The turn may close, so the streak is over")


# ── gate checkers: a necessary condition is not a sufficient one ─────────────

@case("r13i18nrestamp", ["tests/smoke_test.py"],
      "let --emit-marker certify an untranslated re-stamp again -> the digest proves a marker was re-typed, not that anything was translated")
def _break_r13i18nrestamp(root):
    _patch(root, "tools/i18n_check.py",
           "    return bool(prev and prev.get(\"translation\") and prev[\"digest\"] != digest\n"
           "                and prev[\"translation\"] == translation)",
           "    return False  # BREAKAGE: any re-stamp certifies the translation")


@case("r13coveragename", ["tests/smoke_test.py"],
      "count a bare substring as documentation again -> a column named `source` is satisfied by the i18n marker")
def _break_r13coveragename(root):
    _patch(root, "tools/doc_coverage.py",
           "    return re.search(r\"`(?:[\\w.-]+\\.)?\" + re.escape(needle) + r\"`\"\n"
           "                     r'|\"' + re.escape(needle) + r'\"\\s*:', text) is not None",
           "    return needle in text  # BREAKAGE: containment, not naming")


@case("r13coverageenum", ["tests/smoke_test.py"],
      "enumerate only plain CREATE TABLE again -> the FTS5 index is not a surface the docs must name")
def _break_r13coverageenum(root):
    _patch(root, "tools/doc_coverage.py",
           '    created = set(re.findall(r"CREATE (?:VIRTUAL )?TABLE IF NOT EXISTS (\\w+)", src))',
           '    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\\w+)", src))  # BREAKAGE')


@case("r13coveragetools", ["tests/smoke_test.py"],
      "enumerate MCP tools by name prefix again -> a tool outside memory_/progress_ is never required to be documented")
def _break_r13coveragetools(root):
    _patch(root, "tools/doc_coverage.py",
           "    return sorted(set(re.findall(r'\"name\":\\s*\"(\\w+)\"', src[start:end])))",
           "    return sorted(set(re.findall(r'\"name\":\\s*\"((?:memory|progress)_\\w+)\"', src[start:end])))  # BREAKAGE")


@case("r13claimsgap", ["tests/smoke_test.py"],
      "allow exactly one modifier word again -> 'nine shipped plugin hooks' states a count nothing must bind")
def _break_r13claimsgap(root):
    _patch(root, "tools/doc_claims.py",
           '    rf"(?:(?!of\\b)(?!{_NUM}\\b)[A-Za-z][A-Za-z-]*\\s+){{1,2}}"',
           '    rf"(?:(?!of\\b)(?!{_NUM}\\b)[A-Za-z][A-Za-z-]*\\s+){{1}}"  # BREAKAGE')


# ── v2.14.0: markers, quoted evidence, and this harness's own control ───────

@case("r14markercwd", ["tests/smoke_test.py"],
      "accept the os.getcwd() temp-dir fallback again -> the 500-char prompt "
      "marker is written INTO the user's repository, untracked and un-ignored")
def _break_r14markercwd(root):
    # `tempfile.gettempdir()` ends at `os.getcwd()` when no candidate temp
    # directory is usable, and under a hook the cwd is the user's project.
    _patch(root, f"{PKG}/core/markers.py",
           "    if _is_cwd(base) or _norm(base) not in designated:",
           "    if False:  # BREAKAGE: the cwd fallback is a temp directory again")


@case("r14markernone", ["tests/smoke_test.py"],
      "drop the leaf functions' None guard -> the refusal reaches a hook as "
      "an AttributeError, and the hook silently stops doing its OTHER jobs")
def _break_r14markernone(root):
    # The refusal is only safe because every consumer hands `marker_path`'s
    # result straight to these two. Without the guard `Path(None).parent`
    # raises inside `user_prompt.main`, which is NOT an OSError, so it escapes
    # the local handler and the turn-1 request seed is never written — the
    # hook still exits 0 with empty stderr, which is exactly why the gate has
    # to assert the hook's other WORK and not just its contract.
    _patch(root, f"{PKG}/core/markers.py",
           "    if path is None or not _dir_is_private(Path(path).parent) "
           "or _is_link(path):\n        return False",
           "    if not _dir_is_private(Path(path).parent) or _is_link(path):"
           "  # BREAKAGE\n        return False")


@case("r14verbatimorder", ["tests/smoke_test.py"],
      "check verbatim segments for membership ANYWHERE again -> a quote "
      "rebuilt back-to-front reads as VERBATIM")
def _break_r14verbatimorder(root):
    _patch(root, "tools/citation_check.py",
           "        pos, broke, why = 0, None, \"\"\n"
           "        for seg in segments:\n"
           "            at = haystack.find(seg, pos)\n"
           "            if at < 0:\n"
           "                broke = seg\n"
           "                why = (\"out of order at\" if seg in haystack\n"
           "                       else \"not in source:\")\n"
           "                break\n"
           "            pos = at + len(seg)",
           "        _missing = [s for s in segments if s not in haystack]  # BREAKAGE\n"
           "        broke = _missing[0] if _missing else None\n"
           "        why = \"not in source:\"")


@case("r14baseline", ["tests/smoke_test.py"],
      "judge a case by the broken copy's exit code alone -> a gate that is "
      "red for an unrelated reason 'detects' every breakage")
def _break_r14baseline(root):
    _patch(root, "tools/falsify_fixes.py",
           "        base_green, base_tail = gate_baseline(gate)\n"
           "        if not base_green:",
           "        base_green, base_tail = True, \"\"  # BREAKAGE: no control\n"
           "        if not base_green:")


# ── the v2.13.0 / v2.13.2 / v2.12.1 rules that had no case (F7) ─────────────
# Every one of these breaks a rule CLAUDE.md states and an assertion that was
# already in the tree; none of them invents a gate. Each was driven RED on its
# own before being kept.

@case("r13statelit", ["tests/smoke_test.py"],
      "spell the state directory by hand in the installer bootstrap again -> "
      "a fresh install initialises a directory the hooks then cannot find")
def _break_r13statelit(root):
    # One of the TWO registered literal copies (the other is
    # skills/ccm-load/SKILL.md, which r13skilldir already covers from the
    # save-memories side). Neither can import the package, so both are gated.
    _patch(root, f"{PKG}/ui/installer.py",
           '    _STATE_DIRNAME = ".ccm"',
           '    _STATE_DIRNAME = "memory"  # BREAKAGE')


@case("r13statejoin", ["tests/smoke_test.py"],
      "join the state directory's name by hand in a module again -> the name "
      "lives at two call sites, which is how the last rename took 34")
def _break_r13statejoin(root):
    _patch(root, f"{PKG}/core/idle.py",
           "    memory_dir = resolve_memory_dir(cwd)",
           "    memory_dir = resolve_memory_dir(cwd).parent / \".ccm\"  # BREAKAGE")


@case("r13ccmident", ["tests/smoke_test.py"],
      "identify a state directory by NAME again -> a real project's package "
      "called `memory` is renamed out from under it")
def _break_r13ccmident(root):
    _patch(root, f"{PKG}/core/layout.py",
           "    return (_has_ccm_gitignore(directory)\n"
           "            or _has_ccm_database(directory / DB_FILENAME))",
           "    return directory.name in (MEMORY_DIRNAME, LEGACY_MEMORY_DIRNAME)"
           "  # BREAKAGE")


@case("r13readmigrate", ["tests/smoke_test.py"],
      "route the READ side through the migrating resolver -> opening a picker "
      "renames the state directory of every project on the machine")
def _break_r13readmigrate(root):
    _patch(root, f"{PKG}/core/layout.py",
           "    new, old = state_dir_candidates(project_root)\n"
           "    if _safe_is_dir(new):\n"
           "        return new\n"
           "    if _safe_is_file(old / DB_FILENAME):\n"
           "        return old\n"
           "    return new",
           "    return memory_dir(project_root)  # BREAKAGE: a look is a write")


@case("r13hasdbboth", ["tests/smoke_test.py"],
      "let _has_db know only the new name -> an unmigrated project stops "
      "resolving as a root and the marker rung answers for it")
def _break_r13hasdbboth(root):
    _patch(root, f"{PKG}/core/roots.py",
           "    for mem in state_dir_candidates(directory):",
           "    for mem in state_dir_candidates(directory)[:1]:  # BREAKAGE")


@case("r13safepath", ["tests/smoke_test.py"],
      "let the path coercion raise again -> memory_dir({'cwd': 123}) escapes "
      "a function whose docstring promises it never raises, inside a hook")
def _break_r13safepath(root):
    # The v2.6.0 defect this module reproduced: `memory_dir`'s handler catches
    # the TypeError and then re-raises it by coercing again on the way out, so
    # the breakage has to be in the coercion itself, not in the handler.
    _patch(root, f"{PKG}/core/layout.py",
           "    try:\n"
           "        return Path(value)\n"
           "    except Exception:",
           "    if True:  # BREAKAGE: the coercion raises again\n"
           "        return Path(value)\n"
           "    if False:")


@case("r13renderdir", ["tests/smoke_test.py"],
      "hard-code the state directory in MEMORY.md's archive links again -> "
      "every generated index points at a path that stopped existing")
def _break_r13renderdir(root):
    _patch(root, f"{PKG}/llm/memory_writer.py",
           '                lines.append(f"- `{memory_dir.name}/{rel}`")',
           '                lines.append(f"- `memory/{rel}`")  # BREAKAGE')


@case("r12canoncase", ["tests/smoke_test.py"],
      "fold path case on every platform again -> two POSIX directories that "
      "differ only in case become one identity (detected on POSIX; on Windows "
      "the filesystem folds case and the expectation is the other one)")
def _break_r12canoncase(root):
    _patch(root, f"{PKG}/core/layout.py",
           "        return os.path.normcase(str(Path(text).resolve()))",
           "        return str(Path(text).resolve()).lower()  # BREAKAGE")


def verify_anchors():
    """Count every registered case's breakage anchors WITHOUT running a gate.

    A rotted anchor otherwise aborts a full run at whichever case sorts
    first (measured 2026-08-09: `cjk` and `donebar` both rotted after r5/r6
    rewrote their fix sites, and the whole suite died on `cjk`). Each case
    gets a fresh copy so one case's patch cannot shadow another's anchor.
    `skillgate` uses str.replace with no count check, so it cannot rot here.
    """
    rotted = []
    for name in sorted(CASES):
        box, root = _copy_repo()
        try:
            CASES[name][0](root)
        except (Exception, SystemExit) as e:
            # EVERY failure shape. `_patch` raises SystemExit — which is a
            # BaseException, NOT an Exception, so the two must be named
            # together; catching only one of them leaves the other uncaught.
            # Four registered cases detect rot with a bare `str.index`/
            # `assert` instead (ValueError / AssertionError), so a single one
            # of those rotting killed this whole scan with an uncaught
            # traceback — no [ROT] line, no summary, every remaining case
            # unverified. That is exactly the failure mode this function
            # exists to replace, and it was reachable through the four cases
            # the SystemExit-only handler could not catch.
            rotted.append((name, f"{type(e).__name__}: "
                                 f"{(str(e) or '(no message)').splitlines()[0]}"))
        finally:
            shutil.rmtree(box, ignore_errors=True)
    for name, msg in rotted:
        print(f"[ROT] {name}: {msg}")
    print(f"{len(CASES) - len(rotted)}/{len(CASES)} anchors intact")
    return 1 if rotted else 0


_BASELINES = {}


def gate_baseline(gate):
    """(green, tail) for `gate` run on an UNTOUCHED copy. Cached per gate.

    THE negative control, and it was missing entirely through v2.14.0. A case
    was judged RED purely by the gate's exit code on the BROKEN copy, so the
    docstring's promise — "while the same gate passes on the untouched tree" —
    was never established, and a gate that is red for a reason unrelated to
    the breakage "detects" every breakage put in front of it. Measured twice:
    synthetically (`sys.exit(1)` injected into the three checkers ->
    `r8claimpy`, `r12verbatim`, `r11doccoverage` all `RED (detected)`, 3/3),
    and for real on a box with no tkinter, where `tests/test_surfaces.py`
    fails at `ui/dashboard.py`'s import and `r10lograise` / `claimword` /
    `r12scancap` — all three gated on that suite — still reported RED
    (`docs/debug-pass-2026-09/evidence/F-baseline_gates.txt`,
    `F-falsify_slow.txt`). CLAUDE.md § v2.13.1 tells maintainers to prove a
    repaired anchor "still DETECTS (`--case <id>`)"; without this control that
    proof proves nothing.

    Cached by gate ARGV, so a full run pays one baseline per distinct gate
    script (7 today, against 190 cases) and `--case` pays exactly one. A fresh
    copy per gate, never a shared one: the discipline of this file is that
    nothing runs against the working tree, and a gate that ever writes must
    not carry into the next baseline. `--anchors` still runs no gate at all.
    """
    key = tuple(gate)
    if key not in _BASELINES:
        box, root = _copy_repo()
        try:
            proc = _run(root, *gate)
            tail = (proc.stdout or "")[-600:] + (proc.stderr or "")[-600:]
            _BASELINES[key] = (proc.returncode == 0, tail.strip()[-400:])
        finally:
            shutil.rmtree(box, ignore_errors=True)
    return _BASELINES[key]


def run_case(name, keep=False):
    """Judge one case: "RED" / "GREEN" / "ROT" / "UNSOUND".

    Only "RED" counts as detected. The other three are failures of three
    different kinds and `main` reports each under its own heading, because
    "the gate did not notice" and "the gate was red before the breakage" are
    different facts and only the first one is about the fix.
    """
    breaker, gate, description = CASES[name]
    box, root = _copy_repo()
    try:
        try:
            breaker(root)
        except (Exception, SystemExit) as e:
            # Both, for the reason spelled out in verify_anchors: `_patch`
            # raises SystemExit (a BaseException), the hand-written anchors
            # raise ValueError/AssertionError.
            # A rotted anchor is a HARNESS failure, reported as one. It used
            # to abort the whole run mid-way with a traceback and no summary
            # (same gap as verify_anchors above); reporting it per-case keeps
            # the remaining cases running and never lets rot masquerade as a
            # detected breakage.
            print(f"[ROT ] {name:<11} anchor no longer matches the tree: "
                  f"{type(e).__name__}: "
                  f"{(str(e) or '(no message)').splitlines()[0]}")
            print(f"          {description}")
            print("          fix this script, not the tree — then re-run")
            return "ROT"
        base_green, base_tail = gate_baseline(gate)
        if not base_green:
            # The verdict below would be meaningless: an exit code that was
            # already non-zero cannot be evidence that THIS breakage was seen.
            print(f"[FAIL] {name:<11} UNSOUND (gate red before the breakage)")
            print(f"          {description}")
            print(f"          {gate[0]} fails on an UNTOUCHED copy, so no "
                  f"verdict about this fix is possible; repair the gate first")
            print("          baseline tail:", base_tail.replace("\n", " | "))
            return "UNSOUND"
        proc = _run(root, *gate)
        red = proc.returncode != 0
        tail = (proc.stdout or "")[-600:] + (proc.stderr or "")[-600:]
        status = "RED (detected)" if red else "GREEN — NOT DETECTED"
        print(f"[{'ok ' if red else 'FAIL'}] {name:<11} {status}")
        print(f"          {description}")
        if not red:
            print("          the breakage survived every gate; the check that "
                  "was supposed to catch it is vacuous")
            print("          gate tail:", tail.strip()[-400:].replace("\n", " | "))
        return "RED" if red else "GREEN"
    finally:
        if keep:
            print(f"          copy kept at {root}")
        else:
            shutil.rmtree(box, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--case", action="append", choices=sorted(CASES),
                    help="run only these cases (repeatable)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the broken copies for inspection")
    ap.add_argument("--anchors", action="store_true",
                    help="verify every case's anchors against the tree; "
                         "runs no gates, reports ALL rot at once")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.anchors:
        return verify_anchors()
    if args.list:
        for name, (_, gate, desc) in sorted(CASES.items()):
            print(f"{name:<11} {gate[0]:<24} {desc}")
        return 0
    names = args.case or sorted(CASES)
    print(f"Falsifying {len(names)} fix(es) against a temporary copy of "
          f"{REPO}\n")
    results = {n: run_case(n, keep=args.keep) for n in names}
    n_red = sum(1 for v in results.values() if v == "RED")
    print(f"\nSummary: {n_red}/{len(names)} breakages detected")
    for label, verdict in (
            ("UNDETECTED", "GREEN"),
            # Reported apart from UNDETECTED on purpose: an unsound case says
            # nothing about the fix it names, so filing it as "the check is
            # vacuous" would be the same unearned verdict in the other
            # direction.
            ("UNSOUND (gate red before the breakage)", "UNSOUND"),
            ("ROTTED ANCHORS", "ROT")):
        hits = sorted(n for n, v in results.items() if v == verdict)
        if hits:
            print(f"{label}: {hits}")
    return 0 if n_red == len(names) else 1


if __name__ == "__main__":
    sys.exit(main())
