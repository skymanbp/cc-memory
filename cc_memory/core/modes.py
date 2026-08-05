"""
Mode system — domain-specific observation/extraction profiles.

Each mode defines which tools to observe, which categories to prioritize,
and what to suffix onto extraction prompts.
"""
from typing import Dict, List

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
