"""
Mode system — domain-specific observation/extraction profiles, plus the
project opt-out every hook consults.

Each mode defines which tools to observe, which categories to prioritize,
and what to suffix onto extraction prompts.

`is_excluded` lives here rather than in a hook because ALL SIX hooks have to
call it and hooks must not import each other (see its comment below).
"""
import json
import os
from pathlib import Path
from typing import Dict, List

# cc_memory/ — the directory holding config.json under BOTH install layouts
# (marketplace/dev checkout: <repo>/cc_memory/core/modes.py; flat standalone:
# <target>/core/modes.py with config.json at <target>/).
_PKG_ROOT = Path(__file__).resolve().parent.parent


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
    """
    if not isinstance(cwd, str) or not cwd.strip():
        return False
    try:
        with open(_PKG_ROOT / "config.json", encoding="utf-8") as f:
            entries = json.load(f).get("excluded_projects") or []
        if not isinstance(entries, list):
            return False
        target = os.path.normcase(str(Path(cwd).expanduser().resolve()))
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                continue
            try:
                listed = os.path.normcase(str(Path(entry).expanduser().resolve()))
            except (OSError, ValueError):
                # why: one malformed entry must not disable the rest of the
                # opt-out list — skip it and keep checking the others
                continue
            if target == listed or target.startswith(listed + os.sep):
                return True
    except Exception:
        # why: an absent / malformed / unreadable config must never break the
        # hook. "not excluded" is the behaviour that shipped before this
        # control existed, so a broken config degrades to exactly that.
        return False
    return False


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
