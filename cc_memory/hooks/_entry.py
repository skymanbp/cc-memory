#!/usr/bin/env python3
"""Shared hook entry: stdin payload parsing + the opt-out→anchor gate.

Every hook process starts the same way: read stdin to EOF, parse JSON,
require an object, consult config.json's `excluded_projects` on the RAW
cwd, and only then anchor that cwd to the project root. Through v2.9.0
each of the six hooks <!--ce:hooks:asof--> hand-rolled that ladder, and
every drift between the copies became a shipped defect: v2.7.0's release
theme was rungs that missed a guard, and v2.9.0 fixed the one hook
<!--ce:hooks:subset--> whose missing isinstance check let `{"cwd": 123}`
plant a database in the hook process's own directory. This module is the
ladder, once — the same consolidation `cli_opt_out_notice` gave the three
CLI surfaces after three inline `is_excluded` copies drifted there too.

What stays IN the hooks, deliberately: field policies (which payload keys
are load-bearing enough to abort on, which are annotation to coerce —
pre_compact must survive a malformed `trigger`, user_prompt must NOT
survive a malformed `session_id`), the NUL check (only pre_compact mkdirs
unconditionally), and each hook's reaction to an excluded project (silent
for per-turn hooks, logged for rare ones, `config_fault` visibility for
SessionStart). Mechanism here, policy there.

Hook contract: nothing in this module raises. A `None` return means
"nothing to do — exit 0", and the caller owns the `sys.exit`.
"""

import json
import sys

from core.modes import is_excluded
from core.roots import project_root


def parse_payload(log=None, *, replace_errors=False):
    """Read stdin to EOF and parse the hook payload. None -> caller exits 0.

    Read to EOF, never a prefix cap: PostToolUse once capped at 512 KiB,
    and any larger payload (a package-lock.json Read result is routinely
    600 KiB) truncated mid-JSON, raised, and dropped the WHOLE event with
    rc=0 and empty stderr. `replace_errors` keeps that hook's lossy-decode
    behaviour (observation text tolerates U+FFFD); the default is strict,
    matching the other five.

    json.loads SUCCEEDS on well-formed non-object payloads (`null`, `42`,
    `"s"`, `[1,2]`, `true`), and `.get()` on those raises AttributeError
    outside any try — rc=1 plus a traceback on stderr, the exact pair of
    hook-contract violations this guard exists to close. The isinstance
    check is therefore part of the parse, not the caller's job.

    `log` is a core.logger instance or None. None = fully silent (the
    per-turn hooks); a logger = one line per failure (the rare hooks).
    """
    try:
        raw = sys.stdin.buffer.read()
        data = json.loads(raw.decode(
            "utf-8", errors="replace" if replace_errors else "strict"))
    except Exception as exc:
        try:
            if log is not None:
                log.error(f"stdin parse error: {exc}")
        except Exception:
            # why: this is the shared ladder for every hook, and the hook
            # contract (exit 0, empty stderr) outranks the log line. The
            # pre-v2.10.0 post_tool_use carried exactly this guard; losing
            # it here would let a broken logger crash all six hooks
            # <!--ce:hooks--> at once.
            pass
        return None
    if not isinstance(data, dict):
        try:
            if log is not None:
                log.warn(f"stdin payload is {type(data).__name__}, "
                         f"not an object")
        except Exception:
            # why: same contract as above — a diagnostic must never be what
            # breaks the hook it explains
            pass
        return None
    return data


def resolve_project(cwd, *, log=None):
    """Opt-out gate + root anchor, IN THAT ORDER. None -> excluded.

    `is_excluded` sees the RAW cwd and runs FIRST: anchoring first would
    resolve an excluded subdirectory to its unexcluded parent project and
    widen the user's narrow opt-out away — the inversion
    tests/test_surfaces.py §4 drives red. The anchor then
    rebinds cwd so memory_dir, db_path and upsert_project agree on ONE
    directory; fixing each use site instead is how they drifted apart
    before v2.6.0.

    `log` is passed through to `project_root`, which uses it ONLY to
    announce a redirection — pass a logger only from a rare hook, where a
    line per event is not a line per turn. Neither branch logs the exclusion
    itself: whether a skipped project is silent (per-turn hooks) or
    explained (rare hooks, SessionStart's config_fault) is the caller's
    policy, not this gate's.

    Callers validate `cwd` is a non-empty str BEFORE this gate (their
    field policies differ); both callees are misuse-safe anyway —
    `is_excluded` rejects non-strings by design and `project_root` never
    raises.
    """
    if is_excluded(cwd):
        return None
    return str(project_root(cwd, log=log))
