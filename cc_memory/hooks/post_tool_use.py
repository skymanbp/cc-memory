#!/usr/bin/env python3
"""
PostToolUse hook — fires after every tool call.

Captures tool events as lightweight observation rows. No LLM call — just
structured logging for the Stop-hook observer to later sift through.

Output: stdout EMPTY, stderr suppressed, always exits 0.
Target latency: <50ms.
"""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio. PostToolUse has empty stdout by contract, but error
# paths still print tracebacks; gbk would crash the hook on emoji glyphs.
from core.encoding_setup import enable_utf8_io
enable_utf8_io()

_MAX_INPUT_CHARS = 2000
_MAX_OUTPUT_CHARS = 1000
_MAX_STDIN_BYTES = 1024 * 512


def _truncate_input(tool_name, tool_input):
    if not isinstance(tool_input, dict):
        return str(tool_input)[:_MAX_INPUT_CHARS]
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return tool_input.get("file_path", tool_input.get("notebook_path", ""))
    if tool_name == "Read":
        return tool_input.get("file_path", "")
    if tool_name == "Bash":
        return tool_input.get("command", "")[:500]
    if tool_name in ("Grep", "Glob"):
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern} in {path}" if path else pattern
    if tool_name == "Agent":
        return tool_input.get("prompt", "")[:300]
    return json.dumps(tool_input, ensure_ascii=False)[:_MAX_INPUT_CHARS]


def _truncate_output(tool_name, output):
    if output is None:
        return ""
    s = str(output) if not isinstance(output, str) else output
    if not s:
        return ""
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

    db_path = Path(cwd) / "memory" / "memory.db"
    if not db_path.exists():
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if not tool_name:
        sys.exit(0)

    try:
        from core.modes import should_observe
        from core.privacy import clean_for_storage, has_private
        from core.db import MemoryDB

        db = MemoryDB(db_path)
        project_id = db.upsert_project(cwd)
        mode = db.get_project_mode(project_id)

        if not should_observe(mode, tool_name):
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        tool_response = data.get("tool_response", "")
        input_str = _truncate_input(tool_name, tool_input)
        output_str = _truncate_output(tool_name, tool_response)

        is_private = 0
        if has_private(input_str) or has_private(output_str):
            is_private = 1
        input_str = clean_for_storage(input_str)
        output_str = clean_for_storage(output_str)

        db.insert_observation(
            project_id=project_id,
            session_id=data.get("session_id", ""),
            tool_name=tool_name,
            tool_input=input_str,
            tool_output=output_str,
            is_private=is_private,
        )

    except Exception:
        try:
            from core.logger import get_logger
            get_logger("post_tool_use").error_tb("PostToolUse hook error")
        except Exception:
            # why: logger itself failing in a hook means we already lost
            # observability; there's nothing left to do but exit cleanly
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
