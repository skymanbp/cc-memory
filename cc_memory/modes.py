"""
cc-memory/modes.py -- Mode system for domain-specific observation/extraction
Each mode defines which tools to observe, which categories to prioritize,
and how to adjust extraction and injection behavior.
"""

from typing import Dict, List, Optional

# Built-in mode definitions
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
        "categories": [
            "decision", "result", "config", "bug",
            "task", "arch", "note",
        ],
        "injection_priority": [
            "bug", "decision", "task", "config",
            "arch", "result", "note",
        ],
        "extraction_prompt_suffix": "",
    },
    "research": {
        "description": "Research and data analysis",
        "observe_tools": [
            "Bash", "Read", "WebFetch", "WebSearch",
            "Grep", "Glob",
        ],
        "skip_tools": [
            "TodoWrite", "AskUserQuestion", "Skill",
            "ListMcpResourcesTool", "Edit", "Write",
            "MultiEdit", "NotebookEdit",
            "TaskCreate", "TaskUpdate", "TaskList",
        ],
        "categories": [
            "result", "decision", "note", "config",
            "task", "arch",
        ],
        "injection_priority": [
            "result", "decision", "task", "note",
            "config", "arch",
        ],
        "extraction_prompt_suffix": (
            "\nFocus on: experimental results with specific numbers, "
            "data analysis conclusions, methodology decisions."
        ),
    },
    "writing": {
        "description": "Writing and documentation",
        "observe_tools": [
            "Write", "Edit", "Read", "MultiEdit",
            "WebFetch", "WebSearch",
        ],
        "skip_tools": [
            "TodoWrite", "AskUserQuestion", "Skill",
            "ListMcpResourcesTool", "Bash", "Grep", "Glob",
            "TaskCreate", "TaskUpdate", "TaskList",
        ],
        "categories": [
            "decision", "note", "task", "config",
        ],
        "injection_priority": [
            "decision", "task", "note", "config",
        ],
        "extraction_prompt_suffix": (
            "\nFocus on: structural decisions, content outlines, "
            "style guidelines, revision notes."
        ),
    },
}

# Valid mode names
VALID_MODES = set(MODES.keys())


def get_mode(mode_name: str) -> Dict:
    """Get mode config by name, defaulting to 'code'."""
    return MODES.get(mode_name, MODES["code"])


def should_observe(mode_name: str, tool_name: str) -> bool:
    """Check if a tool should be observed in the given mode."""
    mode = get_mode(mode_name)
    if tool_name in mode["skip_tools"]:
        return False
    # If observe_tools is defined, only observe those
    if mode["observe_tools"]:
        return tool_name in mode["observe_tools"]
    return True


def get_injection_priority(mode_name: str) -> List[str]:
    """Get category injection priority for the given mode."""
    return get_mode(mode_name).get("injection_priority", MODES["code"]["injection_priority"])


def get_extraction_suffix(mode_name: str) -> str:
    """Get extraction prompt suffix for the given mode."""
    return get_mode(mode_name).get("extraction_prompt_suffix", "")


def list_modes() -> List[Dict]:
    """Return list of available modes with descriptions."""
    return [
        {"name": name, "description": mode["description"]}
        for name, mode in MODES.items()
    ]
