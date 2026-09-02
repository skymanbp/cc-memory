"""
Per-session temp markers — ONE directory, owner-only, symlink-proof.

Every hook keeps a little cross-process state that must NOT live under the
project's `.ccm/`: the turn counter, the last user prompt, the last
observation-eval timestamp, the idle-reorg stamp, the plan-refine nudge. Anything
written under `.ccm/` also has to be added to
`core.progress.MEMORY_GITIGNORE_LINES` or it leaks into the user's repository
forever — the v2.4.2 lesson — so these deliberately live in the temp directory
instead.

`tempfile.gettempdir()` is a per-user directory on Windows (`%TEMP%`), and this
plugin's threat model was written for that. On Linux with `TMPDIR` unset it is
mode-1777 `/tmp`, shared by every local account, and `Path.write_text` is
`open(..., "w")`: it creates at 0644 and it FOLLOWS SYMLINKS. Two consequences,
both real on a shared dev box or CI runner:

* the prompt marker holds 500 characters of the user's current request, and a
  co-tenant can read it — plus the session id from the turn marker's name, which
  is the only unguessable component of every other marker's name;
* a co-tenant can pre-create any marker name as a symlink to a file the user
  owns, and the next hook write truncates that file. The prompt marker is worse
  still: `hooks/stop.py` reads it back and splices it into the Anthropic request,
  so a planted marker is a prompt-injection channel into the user's own context.

So: one 0700 directory per uid inside the temp dir, and every write goes through
`O_CREAT | O_EXCL`-or-`O_TRUNC` with `O_NOFOLLOW` where the platform has it.

**The DIRECTORY is part of the guard, not just the leaves.** v2.8.0 shipped the
leaf checks alone, and a container nobody validates makes them decorative:
`Path.mkdir(mode=0o700, exist_ok=True)` swallows `FileExistsError` whenever
`self.is_dir()` is true, and `is_dir()` FOLLOWS a link — so a reparse point
pre-planted at the (fully predictable) directory name was accepted, and
`O_NOFOLLOW` on the leaf does nothing about a redirected parent. Measured on
Windows with a plain `mklink /J` junction, no admin rights: `marker_dir()`
returned the planted name, `write_marker` deposited the 500-character prompt
marker inside the attacker's directory, and `read_marker` read the attacker's
replacement back — into the Anthropic request. `_is_link` said False the whole
time, because `stat.S_ISLNK` is blind to junctions; `MemoryDB._is_reparse`,
already in this package, says True.

Two consequences, both implemented below:

* link detection is junction-aware (`os.path.isjunction`, 3.12+, beside the
  portable `S_ISLNK`), and
* every read and write re-validates the PARENT at use time
  (`_dir_is_private`), so a directory that is a link, foreign-owned, or
  group/other-writable refuses service rather than degrading into it. The
  fallback to the shared temp root is the same shape: it is the pre-v2.8.0
  location and it is fine on Windows (`%TEMP%` is per-user by construction),
  but on a mode-1777 `/tmp` it is exactly the state this module exists to
  avoid, so markers there simply do not persist. Losing a marker costs an
  extra LLM call or a repeated nudge — the price this module already says it
  is willing to pay.
"""
import hashlib
import os
import stat
import tempfile
from pathlib import Path

def _owner_tag():
    """A stable per-user component for the directory name.

    `os.getuid` does not exist on Windows, where `gettempdir()` is already
    per-user and the directory name only has to be stable.
    """
    try:
        return str(os.getuid())
    except AttributeError:
        return "user"


def _norm(path):
    """`path` resolved and case-folded for comparison. `""` when it cannot be.

    Deliberately local rather than imported from `core.layout`: this module is
    loaded by every hook before anything else and must not grow a package
    dependency to answer a question about two strings.
    """
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except Exception:
        # why: a non-path, an unresolvable drive, a cwd that has been deleted.
        # A spelling that cannot be computed can only ever MISS, and a miss is
        # the refusing direction for both callers below.
        return ""


def _designated_temp_roots():
    """Every directory the user or the platform actually NAMES as temp.

    `tempfile.gettempdir()` walks `TMPDIR`, `TEMP`, `TMP`, then the platform's
    conventional locations, and **falls back to `os.getcwd()`** when none of
    them is usable. Under a hook the cwd is the USER'S PROJECT, so that last
    rung silently relocates every marker into the repository — the measurement
    is in `marker_dir`. This is the same list WITHOUT that rung, so a base
    that is not in it was chosen by the fallback and by nothing else.

    Mirrors `tempfile._candidate_tempdir_list` from the documented sources
    rather than calling it: a private helper is not a contract, and reading
    the env vars and the platform conventions here keeps this module
    self-contained. Entries that do not exist are harmless — membership is the
    only question asked.

    Read BEFORE `gettempdir()` runs, and that ordering is load-bearing:
    `tempfile.tempdir` is both the embedder's declaration slot AND
    `gettempdir()`'s own cache, so once the fallback has been taken the
    fallback directory is sitting in it and would answer this question with
    its own output. `_is_cwd` still refuses it — the cached value IS
    `os.getcwd()` as of that first call — but a net that depends on nothing
    ever chdir-ing is one net too few.
    """
    names = []
    explicit = getattr(tempfile, "tempdir", None)
    if explicit:
        # an assignment to `tempfile.tempdir` is a deliberate declaration by
        # the embedding program, and `gettempdir()` returns it unconditionally.
        names.append(explicit)
    for env in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(env)
        if value:
            names.append(value)
    if os.name == "nt":
        names.extend([os.path.expanduser(r"~\AppData\Local\Temp"),
                      os.path.expandvars(r"%SYSTEMROOT%\Temp"),
                      r"c:\temp", r"c:\tmp", r"\temp", r"\tmp"])
    else:
        names.extend(["/tmp", "/var/tmp", "/usr/tmp"])
    return {n for n in (_norm(x) for x in names) if n}


def _is_cwd(base):
    """True when `base` IS the current working directory. Never raises.

    The second half of the refusal: the environment can point `TMPDIR`
    straight at the checkout, in which case the base IS designated and only
    this test catches it.

    EQUALITY, deliberately not containment. A first draft refused any base
    INSIDE the cwd, which is a different and much larger set: a project opened
    at the user's home directory has `%TEMP%` (`~/AppData/Local/Temp`) beneath
    it, and a checkout at a drive root has `D:\\Temp` beneath it — both are
    real per-user temp directories, and refusing them would silently turn off
    every marker for that project (turn counter, prompt, idle cooldown, and
    the Stop hook's escape budget, whose failure mode is a session that cannot
    end). Neither is the leak this module is guarding: the markers land in the
    temp directory, not beside the user's source. Only the cwd ITSELF is both
    the getcwd fallback's answer and the user's working tree.
    """
    here, there = _norm(os.curdir), _norm(base)
    return bool(here) and here == there


def marker_dir():
    """The 0700 directory this process's markers live in, or None.

    Created on demand. Never raises: a caller that cannot get a private
    directory falls back to the shared temp dir, which is exactly the
    pre-v2.8.0 behaviour — degraded, but a hook that cannot write a turn
    counter must not fail the user's turn.

    `gettempdir()` is INSIDE the try. It sits at the top of a function that
    promises never to raise, yet it raises `FileNotFoundError` ("No usable
    temporary directory found") when every candidate is missing or unwritable
    — a state reproduced on a locked-down sandbox. `hooks/user_prompt.py` and
    `hooks/stop.py` call this outside any handler, so the promise being false
    meant rc=1 with a stderr traceback: byte-for-byte the failure class the
    `core/logger.py` module-scope `Path.home()` fix removed one release
    earlier, reintroduced one frame deeper.

    **None is a real return value, and the no-temp-directory state gets it.**
    Through v2.14.0 the last resort was a CWD-relative `Path(".")` on the
    stated assumption that `write_marker` would then fail. It does not — and
    the rung ABOVE it is the worse half, because the stdlib degrades before
    this function ever runs: when every candidate temp directory is unusable
    `gettempdir()` returns `os.getcwd()`, which under a hook is the user's
    project, so this function created `<project>/cc-memory-<uid>/` there at
    0700, `_dir_is_private` passed it, and the writes SUCCEEDED. Measured on
    Windows against the shipped v2.14.0 tree: `write_marker(prompt) -> True`,
    with the 500-character prompt marker sitting in the repository listing
    beside the user's own source — untracked, and matched by no `.gitignore`
    line. Keeping markers out of the user's repository is the first paragraph
    of this module's docstring; a degradation that lands them IN it defeats the
    module rather than degrading it. `marker_path` propagates the None and both
    leaf functions refuse it, so the cost is the one this module already says
    it is willing to pay: markers do not persist, which costs an extra LLM call
    or a repeated nudge and never correctness.
    """
    designated = _designated_temp_roots()   # BEFORE gettempdir(): see above
    try:
        base = Path(tempfile.gettempdir())
    except Exception:
        # why: no temp directory exists at all, and there is nowhere safe left
        # to name — under a hook the cwd is the user's repository. Callers
        # handle None (marker_path propagates it; both leaves refuse it).
        return None
    if _is_cwd(base) or _norm(base) not in designated:
        # A base nobody designated can only be the `os.getcwd()` fallback rung;
        # a base that IS the cwd is the same leak declared out loud. Both put
        # markers in the user's working tree, so both are refused.
        return None
    try:
        d = base / f"cc-memory-{_owner_tag()}"
        if _is_link(d):
            # why: `mkdir(exist_ok=True)` would ACCEPT this — its FileExistsError
            # branch tests `is_dir()`, which follows the link. Returning the
            # shared root instead keeps the caller's path stable and hands the
            # decision to `_dir_is_private` below, rather than chmod-ing and
            # then writing through someone else's reparse point.
            return base
        d.mkdir(mode=0o700, exist_ok=True)
        if os.name != "nt":
            # mkdir's mode is masked by umask, and an existing directory keeps
            # whatever mode it already had — so state it outright.
            os.chmod(d, 0o700)
        return d
    except OSError:
        # why: no private directory is available (read-only temp, exotic
        # filesystem). Degrade to the old location rather than break the hook.
        return base


def safe_id(session_id):
    """A filename-safe, collision-free token for one session id.

    THE implementation — hooks/stop.py, hooks/user_prompt.py and core/idle.py
    each carried a private ``session_id[:16].replace(...)`` copy, which is two
    defects in one: three literal copies of a naming convention drift exactly
    the way the `.gitignore` copies did, and the truncation means any two
    session ids sharing their first 16 characters share EVERY marker — turn
    counter, prompt, eval stamp, nudge cooldown — silently cross-wired.
    Hashing the WHOLE id costs the same and cannot collide in practice; it
    also removes the path-separator problem the `.replace()` calls patched.

    A non-string id (hooks guard against these, but this module must not rely
    on that) hashes its repr rather than raising.
    """
    if not isinstance(session_id, str):
        session_id = repr(session_id)
    return hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:16]


def marker_path(prefix, safe_id):
    """The path one marker lives at, or None when there is no safe directory.

    Propagates `marker_dir()`'s None rather than joining onto it: every caller
    hands the result straight to `read_marker` / `write_marker`, both of which
    refuse a None path, so the refusal reaches the leaves without any of the
    call sites having to know about it.
    """
    base = marker_dir()
    if base is None:
        return None
    return base / f"{prefix}{safe_id}"


def _is_link(path):
    """True when `path` itself is a symlink OR a Windows junction. Never raises.

    The PORTABLE half of the symlink defence. ``O_NOFOLLOW`` is POSIX-only —
    ``getattr(os, "O_NOFOLLOW", 0)`` is literally 0 on Windows — and an
    ``fstat`` taken after the open describes the TARGET, so a link to a
    regular file passes ``S_ISREG`` happily. Measured on Windows with
    developer mode on: an O_NOFOLLOW+fstat guard read the linked file's
    contents in full. ``os.lstat`` does not follow on either platform.

    ``S_ISLNK`` ALONE IS NOT ENOUGH, and that is not a theoretical gap: a
    junction is the reparse point a Windows user can create with no admin
    rights and no developer mode (`mklink /J`), and `os.lstat` reports it with
    `S_ISLNK` False — measured `st_file_attributes = 1040`
    (DIRECTORY | REPARSE_POINT) on a directory this function called "not a
    link". `os.path.isjunction` (3.12+) is the second probe; on an older
    interpreter only the symlink half applies, which is recorded here rather
    than silently absorbed. Same shape as `MemoryDB._is_reparse`, which had
    this right for `memory.db` while this module did not.

    lstat-then-open is a TOCTOU window on POSIX, which is exactly why the
    O_NOFOLLOW flag stays below: there the flag is the atomic guard and this
    is redundant. On Windows the temp directory is already per-user, so the
    attacker who could win this race could equally well write the marker
    directly — the check is defence in depth, not the load-bearing wall.
    """
    try:
        if stat.S_ISLNK(os.lstat(str(path)).st_mode):
            return True
    except OSError:
        # why: an absent path is not a link, and every caller here treats a
        # missing marker as "no state yet" anyway.
        return False
    isj = getattr(os.path, "isjunction", None)
    if isj is None:
        return False
    try:
        return bool(isj(str(path)))
    except OSError:
        # why: a path we cannot classify is not one to write through.
        return True


def _dir_is_private(d):
    """True when `d` is a directory only this user can write. Never raises.

    Re-checked on EVERY read and write rather than once at creation, because
    the directory name is fully predictable and `marker_dir()` has two paths
    that legitimately return the shared temp root. Refusing here is what makes
    that degradation safe: on Windows `%TEMP%` is per-user and passes, on a
    mode-1777 `/tmp` it does not, and markers simply stop persisting there.

    The POSIX half checks ownership and the group/other write bits — a
    directory someone else owns, or one anybody may write into, is not private
    no matter what its name says. Windows has no meaningful `st_uid` here, so
    the reparse check plus the per-user `%TEMP%` is the whole test.
    """
    try:
        if _is_link(d):
            return False
        st = os.lstat(str(d))
        if not stat.S_ISDIR(st.st_mode):
            return False
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            return True
        return st.st_uid == getuid() and not (st.st_mode & 0o022)
    except OSError:
        return False


def write_marker(path, text):
    """Write a marker owner-only, refusing to follow a symlink. Never raises.

    The symlink refusal is the half that matters: without it a pre-created
    symlink turns this write into a truncation of whatever the attacker
    pointed at. Two mechanisms, because neither covers both platforms —
    ``O_NOFOLLOW`` (atomic, POSIX only) and `_is_link` (portable, racy) — plus
    `_dir_is_private` on the PARENT, because guarding the leaf while trusting
    the directory it sits in guards nothing: a reparse point at the directory
    name redirects the write before the leaf checks ever run.

    A None path is `marker_dir()`'s refusal arriving here: there is no
    directory outside the user's repository to write into, so there is no
    write. Refusing is what the docstring's "last resort" always claimed.
    """
    if path is None or not _dir_is_private(Path(path).parent) or _is_link(path):
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)   # POSIX only; 0 is a no-op elsewhere
    flags |= getattr(os, "O_BINARY", 0)     # Windows: keep the bytes we wrote
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError:
        # why: ELOOP means someone planted a symlink — refusing to write is the
        # correct outcome, not a reason to fall back to an unguarded write.
        # Any other errno is a temp dir we cannot use; markers are best-effort.
        return False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8", errors="replace"))
        return True
    except OSError:
        # why: a marker is an optimisation (turn counts, cooldowns). Losing one
        # costs an extra LLM call or a repeated nudge, never correctness.
        return False


def read_marker(path, default=""):
    """Read a marker's text, or `default`. Never raises, never follows a link.

    The read-side twin of `write_marker`'s refusal. Guarding only the writes
    closed half the hole: every consumer read markers back with a bare
    ``read_text``, which FOLLOWS a planted symlink — and the prompt marker's
    content is spliced into the Stop observer's Anthropic request, so on a
    shared temp dir a symlink to any file the user can read became an
    exfiltration-and-injection channel.

    Same mechanisms as the writer, INCLUDING the parent check — a marker read
    out of a redirected directory is attacker text, and this is the one marker
    whose content is spliced into the Anthropic request. The ``fstat`` below is
    a further, weaker net (it catches a fifo or a device, not a link — see
    `_is_link`).

    A None path is `marker_dir()`'s refusal (see `write_marker`): nothing was
    ever written, so `default` is the honest answer — the same one every
    caller already gives an absent marker.
    """
    if path is None or not _dir_is_private(Path(path).parent) or _is_link(path):
        return default
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)   # POSIX only; 0 is a no-op elsewhere
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        # why: absent marker, or ELOOP from a planted symlink — both read as
        # "no state yet" for every caller.
        return default
    fh = None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return default
        fh = os.fdopen(fd, "rb")
        return fh.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        # why: an unreadable or undecodable marker is indistinguishable from an
        # absent one for every caller — all of them treat it as "no state yet".
        return default
    finally:
        if fh is not None:
            fh.close()
        else:
            try:
                os.close(fd)
            except OSError:
                # why: the descriptor is already gone; there is nothing left
                # to release and a raise here would break the never-raises
                # contract this function promises its hook callers.
                pass
