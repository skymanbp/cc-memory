#!/usr/bin/env python3
"""
cc-memory/stop.py -- Stop hook (fires after each Claude response)
=================================================================
Checks if the conversation is substantial and /save-memories hasn't
been called yet. If so, injects a gentle reminder to stdout.

Only reminds once per session (uses a temp marker file).
Only triggers after the conversation has enough turns.

Stdout: reminder text (Claude sees it)
Stderr: logging only
"""
import json
import sys
import tempfile
from pathlib import Path

_MIN_TURNS_FOR_REMINDER = 8  # Don't remind on short conversations
_MARKER_PREFIX = "cc_memory_reminded_"


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    # stop_hook_data includes transcript stats if available
    transcript_path = data.get("transcript_path", "")

    if not cwd or not session_id:
        sys.exit(0)

    # Check if we already reminded in this session
    marker = Path(tempfile.gettempdir()) / f"{_MARKER_PREFIX}{session_id[:16]}"
    if marker.exists():
        sys.exit(0)

    # Check if this project has cc-memory initialized
    memory_dir = Path(cwd) / "memory"
    if not (memory_dir / "memory.db").exists():
        sys.exit(0)

    # Count conversation turns from transcript
    turn_count = 0
    if transcript_path and Path(transcript_path).exists():
        try:
            with open(transcript_path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        role = msg.get("message", {}).get("role", "")
                        if role == "user":
                            turn_count += 1
                    except (json.JSONDecodeError, AttributeError):
                        pass
        except Exception:
            pass
    else:
        # No transcript access — estimate from stop_hook_data
        turn_count = data.get("turn_count", 0)

    if turn_count < _MIN_TURNS_FOR_REMINDER:
        sys.exit(0)

    # Create marker so we only remind once
    try:
        marker.write_text(session_id, encoding="utf-8")
    except Exception:
        pass

    # Inject reminder (Claude sees this via stdout)
    print(
        "\n[cc-memory] This conversation has been substantial. "
        "Remember to call /save-memories before ending to preserve important decisions, "
        "results, and insights."
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
