#!/usr/bin/env python3
"""
cc-memory/post_tool_use.py -- PostToolUse hook
===============================================
Fires after EVERY tool call. Captures tool events as lightweight
observation rows in SQLite. No LLM call -- just structured logging.

Stdin (JSON from Claude Code):
  session_id   str
  cwd          str
  tool_name    str
  tool_input   dict
  tool_response str/dict

Output:
  stdout: EMPTY (required by hook contract)
  stderr: SUPPRESSED (Claude Code shows stderr as error UI)
  logging: to file only via logger.py

Always exits 0. Target latency: <50ms.
"""

import json
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))

# Max field sizes to keep hook fast and DB small
_MAX_INPUT_CHARS = 2000
_MAX_OUTPUT_CHARS = 1000
_MAX_STDIN_BYTES = 1024 * 512  # 512KB guard against huge tool outputs


def _truncate_input(tool_name: str, tool_input: dict) -> str:
    """Extract and truncate the most useful part of tool input."""
    if not isinstance(tool_input, dict):
        s = str(tool_input)
        return s[:_MAX_INPUT_CHARS]

    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        # Only keep file path, not content
        return tool_input.get("file_path", tool_input.get("notebook_path", ""))

    if tool_name == "Read":
        return tool_input.get("file_path", "")

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:500]

    if tool_name in ("Grep", "Glob"):
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern} in {path}" if path else pattern

    if tool_name == "Agent":
        return tool_input.get("prompt", "")[:300]

    # Default: serialize and truncate
    s = json.dumps(tool_input, ensure_ascii=False)
    return s[:_MAX_INPUT_CHARS]


def _truncate_output(tool_name: str, output) -> str:
    """Truncate tool output to essentials."""
    if output is None:
        return ""
    s = str(output) if not isinstance(output, str) else output
    if not s:
        return ""

    # For Read, skip output entirely (file contents are too large)
    if tool_name == "Read":
        return "(file content)"

    return s[:_MAX_OUTPUT_CHARS]


def main():
    try:
        raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES)
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        sys.exit(0)

    cwd = data.get("cwd", "")
    if not cwd:
        sys.exit(0)

    # Early exit: not a cc-memory project
    db_path = Path(cwd) / "memory" / "memory.db"
    if not db_path.exists():
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if not tool_name:
        sys.exit(0)

    try:
        from modes import should_observe
        from privacy import clean_for_storage, has_private
        from db import MemoryDB

        # Check mode-based skip list
        db = MemoryDB(db_path)
        project_id = db.upsert_project(cwd)
        mode = db.get_project_mode(project_id)

        if not should_observe(mode, tool_name):
            sys.exit(0)

        # Extract and truncate fields
        tool_input = data.get("tool_input", {})
        tool_response = data.get("tool_response", "")

        input_str = _truncate_input(tool_name, tool_input)
        output_str = _truncate_output(tool_name, tool_response)

        # Privacy check
        is_private = 0
        if has_private(input_str) or has_private(output_str):
            is_private = 1
            input_str = clean_for_storage(input_str)
            output_str = clean_for_storage(output_str)

        # Strip context tags to prevent recursive storage
        input_str = clean_for_storage(input_str)
        output_str = clean_for_storage(output_str)

        session_id = data.get("session_id", "")

        db.insert_observation(
            project_id=project_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=input_str,
            tool_output=output_str,
            is_private=is_private,
        )

    except Exception:
        # Log to file, never to stderr
        try:
            from logger import get_logger
            import traceback
            log = get_logger("post_tool_use")
            log.error_tb("PostToolUse hook error")
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
