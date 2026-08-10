"""Atomic file replacement for the generated artifacts. ONE implementation.

`Path.write_text` truncates first and writes second, so a reader landing in
that window gets **0 bytes** — silently, with no error on either side. Every
artifact this plugin generates is read by another process: PROGRESS.md by the
next session's Claude (under a `<system-reminder>` that ORDERS it to), PLAN.md
by any window at will, MEMORY.md by Claude and by the dashboard. Measured with
one writer and one reader at full duty cycle: 4,867 empty PROGRESS.md reads in
16,071 samples, 176 for MEMORY.md and 168 for PLAN.md in 26,048.

## Why this module exists rather than three private copies

v2.5.2 shipped three `_atomic_write*` functions — `core/progress.py`,
`core/plan.py`, `llm/memory_writer.py` — written by different agents in the
same wave and documented as "deliberate literal twins". They were not twins:

  * `core/progress.py` retried `os.replace` five times and **re-raised**;
  * the other two had **no retry** and, on failure, fell back to the plain
    truncating `write_text` — reintroducing, for that one call, precisely the
    defect the function existed to remove. That fallback is where the residual
    20-empty-reads-in-28,141 came from.

A "literal twin" that has to be hand-synced diverges the first time anyone
touches one side; this package has now proved that three times (`is_excluded`,
the `.gitignore` line list, and this). `core` may be imported by `llm`, so
there was never a dependency reason for the split.

## Contract

`write_atomic` either replaces the file **completely** or raises. It never
leaves a truncated file, and it never silently falls back to one. A caller
that must not fail — a hook — catches `OSError` and keeps the PREVIOUS
complete artifact, which is strictly better than a torn one and is regenerated
on the next write anyway.
"""
import os
import tempfile
import time
from pathlib import Path

# Bounded, and bounded deliberately. On Windows `os.replace` onto a path
# another process holds open without FILE_SHARE_DELETE fails with
# ERROR_SHARING_VIOLATION; the holder is a reader that releases in
# microseconds, so a short retry converts almost every one of them. Measured
# against a permanently-open reader (the pathological case, not a real one):
# fallbacks 1249 with no retry, 700 at 3 tries, 471 at 5. Retrying forever
# would hang a hook that has a hard host timeout, so it stops and raises.
_RETRIES = 5
_BACKOFF_S = 0.01
_MAX_BACKOFF_S = 0.02

# The two DERIVED artifacts (PLAN.md, MEMORY.md) retry against a wall-clock
# BUDGET instead of a try count. Their writers swallow a final failure and keep
# the previous complete file, so for them a failure means "the artifact is one
# write stale" rather than "the caller is told" — and the destination is
# unavailable for as long as someone else holds it open, which is a duration.
#
# What the budget buys, HONESTLY (register D5 — an earlier revision of this
# comment claimed the 3 s budget "lost 0" of 150 renames, and re-measurement
# showed 20-28 lost depending on machine load; a number used as justification
# was itself the unverified-claim class this project treats as most serious).
# Re-measured 2026-08-09, 3 readers at 100 % duty cycle, 150 write rounds
# each: 12 fixed tries lost 125/150; the 3 s budget lost 28/150. The budget
# bounds the WAIT and sharply improves the odds; it does NOT guarantee the
# rename against a reader that never yields — no finite wait can. The
# CONTRACT is what guarantees correctness: fail means the previous COMPLETE
# artifact stays, never a torn one. Worst case 3 s against an 8 s PostToolUse
# budget and a 120 s PreCompact one, paid only while the OS is actively
# refusing. PROGRESS.md keeps the short count because its writer RAISES —
# there the caller finds out and can act.
_DERIVED_BUDGET_S = 3.0


def write_atomic(path: Path, text: str, retries: int = _RETRIES,
                 budget_s: float = 0.0) -> None:
    """Replace `path` with `text` atomically, or raise.

    `budget_s`, when > 0, keeps retrying the rename until that many seconds
    have elapsed instead of stopping after `retries` attempts. A fixed count is
    the wrong shape for this failure: the destination is unavailable for as
    long as some other process holds it open, which is a DURATION, not a number
    of tries. Re-measured 2026-08-09 with three readers at 100 % duty cycle,
    150 write rounds each: 12 fixed tries lost 125 renames, the 3 s budget
    lost 28 — a large improvement, NOT a guarantee (see `_DERIVED_BUDGET_S`
    for why the earlier "lost none" claim was retracted). Correctness comes
    from the contract: on failure the previous complete artifact stays.
    Only the derived artifacts use it — see `_DERIVED_BUDGET_S`.

    `errors="replace"` on the encode: a lone surrogate — reachable from a
    `surrogateescape`-decoded filename that lands in `files_touched` — would
    otherwise raise `UnicodeEncodeError` and take the whole write with it. In
    `write_session_archive` that cost the archive, the session row, the
    memories AND the handoff, because it runs above all three.

    The temp file is created in the destination's own directory (`os.replace`
    is only atomic within a filesystem) with a leading dot and a `.tmp`
    suffix — `*.tmp` is already in `core.progress.MEMORY_GITIGNORE_LINES`, so
    a leftover never reaches a user's repository.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
            # fsync BEFORE the rename publishes the name. `close()` flushes to
            # the OS page cache, not to stable storage, and `os.replace` orders
            # the metadata operation only — so a host crash between the rename
            # and writeback leaves the new directory entry pointing at zero or
            # partial bytes. That is the same torn read this module's contract
            # ("replaces the file completely or raises") promises to eliminate,
            # merely moved from a truncating write to the page cache.
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # why: some filesystems and some Windows handles refuse fsync.
                # A durability guarantee that cannot be obtained is not a
                # reason to abandon a write that is otherwise correct — the
                # replace below is still atomic with respect to readers.
                pass
        last_err = None
        deadline = (time.monotonic() + budget_s) if budget_s > 0 else None
        attempt = 0
        while True:
            try:
                os.replace(tmp, str(path))
                return
            except PermissionError as e:
                # why: Windows sharing violation from a concurrent reader.
                # Retry; do NOT fall back to a truncating write, which is the
                # defect this module exists to remove.
                last_err = e
            attempt += 1
            if deadline is not None:
                if time.monotonic() >= deadline:
                    break
            elif attempt >= max(1, retries):
                break
            # Linear backoff capped at _MAX_BACKOFF_S: growing without a cap
            # turns a 3 s budget into three sleeps, which is fewer chances to
            # catch the gap between the holder's close and its next open.
            time.sleep(min(_BACKOFF_S * attempt, _MAX_BACKOFF_S))
        raise last_err
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
