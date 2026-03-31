#!/usr/bin/env python3
"""
cc-memory/user_prompt.py -- UserPromptSubmit hook
==================================================
Fires on every user message. Two jobs:
  1. Track turn count per session (temp file)
  2. Save user prompt text so Stop hook can use it as observer context
     (mirrors claude-mem's session-init: gives the observer "what the user wants")

Stdin (JSON from Claude Code):
  session_id   str
  cwd          str
  prompt       str   (user's message text)

Output:
  stdout: EMPTY
  stderr: SUPPRESSED
  Always exits 0. Target: <30ms.
"""

import json
import sys
import tempfile
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))

_TURN_FILE_PREFIX = "cc_mem_turns_"
_PROMPT_FILE_PREFIX = "cc_mem_prompt_"
_LAST_EVAL_PREFIX = "cc_mem_eval_"


def _safe_id(session_id: str) -> str:
    return session_id[:16].replace("/", "_").replace("\\", "_")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    if not cwd or not session_id:
        sys.exit(0)

    # Early exit: not a cc-memory project
    db_path = Path(cwd) / "memory" / "memory.db"
    if not db_path.exists():
        sys.exit(0)

    safe = _safe_id(session_id)
    tmp = Path(tempfile.gettempdir())

    try:
        # 1. Track turn count
        turn_file = tmp / f"{_TURN_FILE_PREFIX}{safe}"
        turn_count = 1
        if turn_file.exists():
            try:
                turn_count = int(turn_file.read_text(encoding="utf-8").strip()) + 1
            except (ValueError, OSError):
                turn_count = 1
        try:
            turn_file.write_text(str(turn_count), encoding="utf-8")
        except OSError:
            pass

        # 2. Save user prompt for Stop hook observer context
        prompt = data.get("prompt", "")
        if prompt and isinstance(prompt, str):
            # Clean slash commands
            if prompt.startswith("/"):
                prompt = prompt[1:]
            # Truncate
            prompt = prompt[:500]
            prompt_file = tmp / f"{_PROMPT_FILE_PREFIX}{safe}"
            try:
                prompt_file.write_text(prompt, encoding="utf-8")
            except OSError:
                pass

    except Exception:
        try:
            from logger import get_logger
            log = get_logger("user_prompt")
            log.error_tb("UserPromptSubmit hook error")
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
