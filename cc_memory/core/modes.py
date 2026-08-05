"""
Mode system — domain-specific observation/extraction profiles, plus the
project opt-out every hook consults.

Each mode defines which tools to observe, which categories to prioritize,
and what to suffix onto extraction prompts.

`is_excluded` lives here rather than in a hook because ALL SIX hooks have to
call it and hooks must not import each other (see its comment below).
`read_config` lives here for the same reason: it is THE runtime reader of
cc_memory/config.json, and three independent copies of that six-line read is
exactly how the BOM hole below stayed open for a release.
"""
import json
import os
from pathlib import Path
from typing import Dict, List

# cc_memory/ — the directory holding config.json under BOTH install layouts
# (marketplace/dev checkout: <repo>/cc_memory/core/modes.py; flat standalone:
# <target>/core/modes.py with config.json at <target>/).
_PKG_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PKG_ROOT / "config.json"


def _log_config(level: str, msg: str) -> None:
    """File-log a config.json problem. Never stderr, never raises.

    A config.json that cannot be read is a SECURITY-relevant event — it is the
    file holding the only privacy control the plugin has — so it must leave a
    trace. Through v2.5.1 every failure mode below was silent in all three
    channels at once (no stdout, no stderr, no log), which is why a BOM could
    disable the opt-out for a whole release without a single report of it being
    diagnosable.

    `core.logger` is imported LAZILY: it resolves the home directory at import
    time, and this is the failure path only — `is_excluded` runs on the
    PostToolUse hot path and must not pay for that on every call.
    """
    try:
        from core.logger import get_logger
        getattr(get_logger("config"), level)(msg)
    except Exception:
        # why: logging is best-effort. A hook must never fail because the log
        # directory is unwritable, and stderr is forbidden (hook contract).
        pass


def read_config():
    """Parse cc_memory/config.json. THE reader for every runtime path.

    Returns ``(cfg, note)``:

      * ``cfg`` is a dict — usable settings. ``{}`` means "no user settings"
        (file absent or empty), which every consumer reads as its compiled-in
        defaults.
      * ``cfg`` is ``None`` — the file EXISTS and could not be used; ``note``
        says why, in a sentence the user can act on. A consumer of a
        *defaultable* key degrades to its default; the PRIVACY control does
        not — see `is_excluded`.

    Decoded as ``utf-8-sig``, not ``utf-8``. On Windows — the primary platform
    — PowerShell's ``Out-File`` defaults to UTF-8 *with* a BOM and Notepad
    offers it, and a BOM makes ``json.load`` raise ``JSONDecodeError``.
    ``ui/installer.py:_read_json_config`` has read this way since v2.5.0 with
    that same comment; the six hook paths did not, so a user who edited the
    only privacy control the plugin has, with the tools the platform hands
    them, silently lost it — every tool input AND output stored, and with a
    credential POSTed to the Anthropic API by the Stop observer. Write paths
    still emit no BOM.
    """
    try:
        raw = _CONFIG_PATH.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}, None
    except OSError as e:
        return None, f"cannot be read ({e})"
    except UnicodeDecodeError as e:
        return None, (f"is not UTF-8 text ({e}); re-save it with "
                      f"'Set-Content -Encoding utf8' or a UTF-8 editor")
    if not raw.strip():
        return {}, None
    try:
        data = json.loads(raw)
    except ValueError as e:
        # ValueError, not json.JSONDecodeError: the latter is a subclass of it,
        # so this catches strictly more (and json can raise the plain base).
        return None, (f"is not valid JSON ({e}) - // comments and trailing "
                      f"commas are not allowed")
    if not isinstance(data, dict):
        return None, (f"contains a top-level JSON {type(data).__name__}, not "
                      f"an object")
    return data, None


def _norm_path(value: str) -> str:
    """Absolute, normcase'd form of one path, for comparison. NEVER raises.

    ``Path.expanduser()`` raises **RuntimeError** — not OSError, not ValueError
    — when ``os.path.expanduser`` cannot resolve a leading ``~user`` and hands
    the string back unchanged. POSIX: any unknown user. Windows: ``~name``
    where the name differs from the USERNAME environment variable AND the
    profile directory's own basename differs from it too — that is, every
    domain/roaming profile (example: a profile directory named ``jdoe.CORP``).
    config.json's own documentation invites ``~`` entries, so this is a shape
    users are told to write; through v2.5.1 it escaped BOTH handlers in
    `is_excluded` and disabled the entire opt-out list, order-dependently (an
    entry sitting after the first match was masked by the loop's early return,
    which made it look intermittent).

    Every degradation keeps the path COMPARABLE rather than aborting the match:
    unexpandable ``~`` → compare the literal text; unresolvable (unreachable
    share, illegal name) → abspath; NUL byte → raw text. The worst outcome for
    a degraded entry is that it fails to match — never that the entries around
    it stop being checked.
    """
    try:
        expanded = str(Path(value).expanduser())
    except Exception:
        # why: an unexpandable ~user is still a path. Compare it literally
        # instead of letting one entry decide the fate of the whole list.
        expanded = value
    try:
        return os.path.normcase(str(Path(expanded).resolve()))
    except Exception:
        # why: resolve() touches the filesystem and can raise on an unreachable
        # share or an illegal name; abspath below is pure string work.
        pass
    try:
        return os.path.normcase(os.path.abspath(expanded))
    except Exception:
        # why: abspath still raises ValueError on an embedded NUL byte. Raw
        # text can only ever produce a MISS for that one broken entry.
        return os.path.normcase(expanded)


# ── Project opt-out (config.json `excluded_projects`) ──────────────────────
# SINGLE implementation, called by every hook. Through v2.5.0 this existed as
# two byte-identical private copies (hooks/user_prompt.py, hooks/pre_compact.py)
# justified by "those two hooks are the ONLY paths that create memory/". True,
# and beside the point: the other four hooks gate on memory/memory.db merely
# EXISTING, so a project that was already initialised and listed AFTERWARDS —
# the natural sequence, since a user reaches for this control on realising a
# repo is sensitive — kept storing observations (inputs AND outputs), kept
# getting a progress row + PROGRESS.md naming its files, kept being injected at
# SessionStart, and with a live credential kept POSTing those observations to
# the Anthropic API from the Stop observer. README called it an opt-out
# "entirely"; every clause of that was false for a pre-existing project.
# Do NOT re-copy this function into a hook: a second copy is exactly how the
# call-site set drifted out of sync in the first place. Importing it from
# `core` (which every hook already imports for db/logger/encoding) does not
# reintroduce the cross-hook coupling the old comment warned about.
def is_excluded(cwd) -> bool:
    """True when `cwd` is opted out of cc-memory via config.json.

    Matching is on the RESOLVED absolute path, so symlinks, ".." and relative
    entries all normalise to one form, and through ``os.path.normcase`` so
    Windows compares case- and separator-insensitively while POSIX stays
    case-sensitive.

    A listed directory also excludes everything BENEATH it. Claude Code's cwd
    is routinely a subdirectory of the project a user meant to exclude, and an
    exact-match-only test would happily create memory/ there — the control
    failing at the one job it has.

    A non-string or empty `cwd` returns False rather than raising: callers gate
    on that themselves, and `Path("").resolve()` would silently widen the match
    to the interpreter's own working directory.

    FAIL-CLOSED on an unusable config (v2.5.2). If config.json EXISTS and
    cannot be parsed, or `excluded_projects` is neither a list nor a single
    string, this returns True for EVERY project and logs an ERROR naming the
    reason. The two outcomes are not symmetric: guessing "not excluded" on a
    typo stores the user's tool inputs and outputs to disk and, with a
    credential present, ships them to the Anthropic API — unrecoverable.
    Guessing "excluded" suspends the plugin, which the user sees inside one
    session (no SessionStart injection, no PROGRESS.md), the log explains, and
    fixing the file undoes completely. An ABSENT or EMPTY config is NOT this
    case: no exclusion list exists, so nothing is excluded and nothing is
    logged.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        return False
    try:
        cfg, note = read_config()
        if cfg is None:
            _log_config("error",
                        f"config.json {note} -- FAIL-CLOSED: treating every "
                        f"project as excluded (no memory is being recorded "
                        f"anywhere) until it parses")
            return True
        entries = cfg.get("excluded_projects")
        if not entries:
            # None / missing / [] / "" — no list, so nothing is excluded. The
            # overwhelmingly common case, and not a misconfiguration.
            return False
        if isinstance(entries, str):
            # One bare path instead of a list: unambiguous intent, and the
            # alternative (fail closed) would punish an obvious typo.
            entries = [entries]
        if not isinstance(entries, list):
            _log_config("error",
                        f"config.json excluded_projects is a "
                        f"{type(entries).__name__}, not a list of paths -- "
                        f"FAIL-CLOSED: treating every project as excluded "
                        f"until it is a list")
            return True
        # (a genuine listing follows; `config_fault()` reports None for it)
        target = _norm_path(cwd)
        skipped = []
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                skipped.append(repr(entry)[:60])
                continue
            # _norm_path never raises, so no entry can abort the loop; the
            # pre-v2.5.2 inner `except (OSError, ValueError)` promised exactly
            # this and did not deliver it (RuntimeError from ~user escaped).
            listed = _norm_path(entry)
            # rstrip(os.sep) before re-appending: a drive/filesystem root
            # resolves WITH its separator already attached ("c:\\", "/"), so the
            # naive `listed + os.sep` built "c:\\\\" / "//" and matched nothing
            # — listing a root excluded no project at all, silently. Stripping
            # first makes root behave like every other directory. The `==`
            # comparison above still covers the project that IS the root.
            prefix = listed.rstrip(os.sep) + os.sep
            if target == listed or target.startswith(prefix):
                return True
        if skipped:
            _log_config("warn",
                        f"config.json excluded_projects: ignored "
                        f"{len(skipped)} entr(y/ies) that are not non-empty "
                        f"strings: {', '.join(skipped[:5])}. The remaining "
                        f"entries were checked normally.")
    except Exception:
        # why: hook contract — is_excluded must never raise. Everything above
        # is already non-raising, so reaching here means a bug in this module;
        # log it with a traceback instead of disabling the control in silence,
        # and degrade to the pre-control behaviour ("not excluded") rather than
        # switching the whole plugin off on an unknown fault.
        _log_config("error_tb", "is_excluded failed unexpectedly; "
                                "treating the project as NOT excluded")
        return False
    return False


def config_fault():
    """Why `is_excluded` is failing CLOSED right now — or None if it is not.

    Returns a short, user-actionable sentence when config.json EXISTS and
    cannot be used, and ``None`` in every other case (absent, empty, valid —
    including a valid config that genuinely lists the project).

    This exists so the fail-closed decision can be SEEN. v2.5.2 made an
    unusable config suspend the plugin globally, which is the right call for a
    privacy control, but its only trace was a line in
    ``~/.claude/hooks/cc-memory/logs/`` — a file nobody reads until they
    already suspect something. A merge-conflicted config.json (the exact
    scenario config.json's own note warns about, since a marketplace checkout
    keeps it under git) would therefore present as "the plugin quietly stopped
    working", which is indistinguishable from a bug and is how a user ends up
    reinstalling instead of fixing one line.

    `hooks/session_start.py` is the only consumer: it prints ONE line when this
    is non-None. A project that is genuinely listed stays completely silent,
    because that silence is the feature.
    """
    try:
        cfg, note = read_config()
        if cfg is None:
            return (f"cc-memory is SUSPENDED: cc_memory/config.json {note}. "
                    f"No memory is being recorded for any project until it "
                    f"parses. This is fail-closed by design — the privacy "
                    f"opt-out lives in that file.")
        entries = cfg.get("excluded_projects")
        if entries and not isinstance(entries, (list, str)):
            return (f"cc-memory is SUSPENDED: cc_memory/config.json "
                    f"'excluded_projects' is a {type(entries).__name__}, not a "
                    f"list of paths. No memory is being recorded for any "
                    f"project until it is a list.")
    except Exception:
        # why: this is a diagnostic accessory to a hook. It must never be the
        # thing that breaks one, and "no fault to report" is the safe answer —
        # is_excluded has already made the actual decision.
        return None
    return None


MODES = {
    "code": {
        "description": "Software development (default)",
        "observe_tools": [
            "Edit", "Write", "MultiEdit", "NotebookEdit",
            "Bash", "Grep", "Glob", "Read",
            "WebFetch", "WebSearch",
        ],
        "skip_tools": [
            "TodoWrite", "AskUserQuestion", "Skill",
            "ListMcpResourcesTool", "TaskCreate", "TaskUpdate",
            "TaskList", "TaskGet", "TaskStop", "TaskOutput",
        ],
        "categories": ["decision", "result", "config", "bug", "task", "arch", "note"],
        "injection_priority": ["bug", "decision", "task", "config", "arch", "result", "note"],
        "extraction_prompt_suffix": "",
    },
    "research": {
        "description": "Research and data analysis",
        "observe_tools": ["Bash", "Read", "WebFetch", "WebSearch", "Grep", "Glob"],
        "skip_tools": [
            "TodoWrite", "AskUserQuestion", "Skill", "ListMcpResourcesTool",
            "Edit", "Write", "MultiEdit", "NotebookEdit",
            "TaskCreate", "TaskUpdate", "TaskList",
        ],
        "categories": ["result", "decision", "note", "config", "task", "arch"],
        "injection_priority": ["result", "decision", "task", "note", "config", "arch"],
        "extraction_prompt_suffix": (
            "\nFocus on: experimental results with specific numbers, "
            "data analysis conclusions, methodology decisions."
        ),
    },
    "writing": {
        "description": "Writing and documentation",
        "observe_tools": ["Write", "Edit", "Read", "MultiEdit", "WebFetch", "WebSearch"],
        "skip_tools": [
            "TodoWrite", "AskUserQuestion", "Skill", "ListMcpResourcesTool",
            "Bash", "Grep", "Glob",
            "TaskCreate", "TaskUpdate", "TaskList",
        ],
        "categories": ["decision", "note", "task", "config"],
        "injection_priority": ["decision", "task", "note", "config"],
        "extraction_prompt_suffix": (
            "\nFocus on: structural decisions, content outlines, "
            "style guidelines, revision notes."
        ),
    },
}

VALID_MODES = set(MODES.keys())


def get_mode(mode_name: str) -> Dict:
    return MODES.get(mode_name, MODES["code"])


def should_observe(mode_name: str, tool_name: str) -> bool:
    """Should this tool call become an `observations` row?

    SCOPE — this gate decides ONE thing: whether a tool call is worth
    *remembering*. It must never gate control-plane work.

    In particular `TodoWrite` is in every mode's ``skip_tools`` and
    `ExitPlanMode` is in no mode's non-empty ``observe_tools`` allow-list, so
    this returns False for both plan-control tools in all three modes — by
    design, because neither is interesting as an observation. Between v2.2 and
    v2.4.3 `hooks/post_tool_use.py` early-exited on this result BEFORE its
    live-plan block, which silently killed the whole v2.2 plan anchor and made
    the guardian drift counters mode-dependent. The plan block now runs above
    the gate; do not move it back, and do not "fix" that by adding
    ExitPlanMode / TodoWrite here — that would only start storing junk
    observation rows.
    """
    mode = get_mode(mode_name)
    if tool_name in mode["skip_tools"]:
        return False
    if mode["observe_tools"]:
        return tool_name in mode["observe_tools"]
    return True


def get_injection_priority(mode_name: str) -> List[str]:
    return get_mode(mode_name).get("injection_priority", MODES["code"]["injection_priority"])


def get_extraction_suffix(mode_name: str) -> str:
    return get_mode(mode_name).get("extraction_prompt_suffix", "")


def list_modes() -> List[Dict]:
    return [
        {"name": name, "description": mode["description"]}
        for name, mode in MODES.items()
    ]
