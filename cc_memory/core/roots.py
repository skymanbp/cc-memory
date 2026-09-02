r"""Project-root resolution for the `cwd` a Claude Code hook is handed.

RAW docstring on purpose: this module's whole subject is Windows paths, and a
plain one turns `D:\Projects` into the invalid escape `\P`. Python answers
that with a SyntaxWarning on **stderr**, and Claude Code renders any hook
stderr as an error — so the prose below would have broken all six hooks
<!--ce:hooks--> the first time they were compiled from a fresh copy. Caught
by test_surfaces §4.

WHY THIS EXISTS
---------------
Every hook used to compute `Path(cwd) / "memory"` and hand the same raw `cwd`
to `db.upsert_project()`. There was no notion of a project ROOT anywhere:
grepping `CLAUDE_PROJECT_DIR` and `.git` across `hooks/` and `core/` returned
nothing. But Claude Code's hook payload carries the session's CURRENT working
directory, which follows the agent's own `cd` — so a session launched at
`D:\Projects\CodeEraser` that ran one command inside `cli/` started reporting
`cwd = D:\Projects\CodeEraser\cli`, and `hooks/user_prompt.py` dutifully
mkdir'd a SECOND, fully independent database there.

That stray then self-sustains: the other five hooks <!--ce:hooks:subset-->
gate on `memory/memory.db` merely EXISTING at `cwd`, so once born it keeps
being written. Measured on the reporting machine before the fix: 27 memories,
its
own `projects` row, a `PROGRESS.md` titled "PROGRESS — cli", still receiving
writes — while the real database two levels up had 161 and knew nothing about
them. The same directory also had no `.gitignore` (the init path writes one
only for the dir it creates), so a 184 KB binary `memory.db` rode into three
of that repo's commits.

PREVENTION, NOT MIGRATION — the load-bearing decision
-----------------------------------------------------
An earlier draft of this module tried to HEAL an existing stray: it took the
outermost end of a contiguous run of database-bearing ancestors, on the theory
that "a stray is by construction deeper than the real root". An adversarial
review killed it against ground truth on the reporting machine. Enumerating
every `memory/memory.db` under that machine's project tree found 20 databases,
and FOUR of them are legitimately nested inside another one:

    Claude-Code-Local\companion               3725 memories, its own .git
    Claude-Code-Local\models                    14 memories
    Claude-Code-Local\references\claude-code-rev 4 memories
    cc-tree\tools                                9 memories

A stray sub-database and a deliberate nested sub-project are **byte-for-byte
indistinguishable on disk**: both carry `memory/memory.db` whose `projects`
row names their own directory, because `upsert_project` writes whatever cwd
it was handed. Outermost-wins resolves that ambiguity unconditionally in the
direction that destroys data — the first post-upgrade session in `companion`
would have silently moved 3725 memories out of reach.

So an existing database is now a DECLARATION OF IDENTITY and is never
overridden. The reported bug is fixed by PREVENTION: the marker rung runs
before any database exists, so the stray is never born in the first place.
Adopting an already-born stray means merging two SQLite files — destructive,
irreversible, and therefore an explicit user-confirmed command, never
something a hook does on every prompt.

THE LADDER
----------
Over a BOUNDED ancestor chain (see `_chain`), first hit wins:

  0. `cwd` itself has `.ccm/memory.db` — or, on a project whose rename
     has not happened yet, `memory/memory.db` — → `cwd`, terminal, before
     else is even consulted.
  1. The NEAREST ancestor with `.ccm/memory.db`. No outward extension: see
     above.
  2. `CLAUDE_PROJECT_DIR`, when it names a directory in the chain. Ranked
     BELOW the database rungs on purpose — it says where Claude Code was
     launched, which is not authority to orphan a database. It was measured
     EMPTY in the tool process of the client this was written against
     (2026-08-07), so it is a free bonus, not the mechanism.
  3. Project markers, nearest-then-extend-outward, with two ceilings (see
     `_marker_root`). The only rung that can fire before any database exists,
     i.e. the one that actually prevents the stray.
  4. The `cwd` as given — today's behaviour, unchanged.

BOUNDARIES ARE THE SAFETY PROPERTY
----------------------------------
The chain stops BELOW the user's home directory and below any filesystem
root. This is not hypothetical tidiness: on the reporting machine
`C:\Users\<user>\memory\memory.db` exists (495 KB, left by one session that
ran in the home directory), and an unbounded database rung would have
re-pointed every project under the user profile at it. Home is never a
candidate — with the one exception that a `cwd` which IS the home directory
stays itself, because that is a project the user really did start there.

`.ccm-root` (an empty file) truncates the walk at the directory holding it.
That is the escape hatch for a project deliberately nested inside another
one, and for any layout these heuristics read wrong.

COST
----
Runs on every UserPromptSubmit and every PostToolUse. Rung 0 is a single
`stat` and covers every already-initialised project. Rung 3 is up to
`len(_MARKERS)` stats per ancestor plus one `scandir` per upward step, but it
can only run while NO database exists anywhere in the chain — i.e. once per
project, before init.
"""
import os
from pathlib import Path

# Junction-aware link probe (symlink OR `mklink /J` reparse point) — the
# shared implementation core/markers.py carries for exactly this check.
# Module-level like every other import: this file is on every hook's path
# already, and markers.py is pure stdlib with no imports back into core.
from core.markers import _is_link as _markers_is_link
# The state directory's NAME — both the current `.ccm` and the pre-v2.13.0
# `memory` this resolver must still recognise — lives in core/layout.py, once.
# Module-level like the import above: layout.py is pure stdlib plus markers.py
# and imports nothing back into this file, so the hook path pays a name lookup,
# not a new dependency (its `core/db` import is deliberately lazy).
from core.layout import DB_FILENAME, state_dir_candidates

# Depth cap. Nothing legitimate is 25 levels below its own project root; the
# cap exists so a pathological path (a mount loop, a fuzzed payload) costs a
# bounded number of stat calls rather than an unbounded walk.
_MAX_DEPTH = 25

# How far ABOVE cwd the marker rung may look. A marker is a guess, and a guess
# must not travel: test_surfaces §4 caught this rung climbing SEVEN levels out
# of a temp-dir fixture and into the real user profile, where it matched and
# pointed a brand-new project at the home database. Real projects put their
# cwd 2-4 levels under the root (`repo/packages/app/src`); six is generous and
# still far short of "somewhere else entirely".
_MARKER_MAX_RISE = 6

# A directory with at least this many project-shaped children is a CONTAINER
# of projects, not a project. `D:\Projects` on the reporting machine has 27.
_CONTAINER_CHILDREN = 2

# How many SUBDIRECTORIES `_is_container` examines before it stops looking.
# A positive verdict is cheap (the second project-shaped child ends the scan),
# but proving the NEGATIVE meant reading every child — and every hook and
# every MCP call resolves its root through every ancestor. Measured on the
# reporting machine: `%TEMP%`, where every test sandbox lives, held 6,366
# subdirectories, so ONE no-database MCP call cost 25,520 stat calls and
# 3.5-4.4 s, and the stdio suite's eight calls answered five inside its 25 s
# window. CI's clean runners never saw it. A real projects folder shows its
# second project-shaped child long before this many entries; a directory
# that has not is judged not a container.
_CONTAINER_SCAN_CAP = 256

# Directories whose CONTENTS are somebody else's code. A cwd inside one of
# these belongs to the project that depends on the package, never to the
# package — and a database planted in here is invisible to every reporting
# path, so it can never be cleaned up. Matched by directory NAME, lowercased.
_DEPENDENCY_DIRS = frozenset((
    "node_modules", "vendor", "site-packages", "bower_components",
    ".venv", "venv", "virtualenv", ".tox", ".nox", "eggs", ".eggs",
    "third_party", "thirdparty", "external", "deps", "_deps",
))

# Explicit pin. Truncates the walk INCLUSIVELY: the directory holding it is
# the root, and no ancestor of it is ever considered.
PIN_MARKER = ".ccm-root"

# Version-control roots. Used ONLY as a ceiling and as a marker — never as a
# requirement, because plenty of projects here are not repositories at all.
_VCS_MARKERS = (".git", ".hg", ".svn")

# Root markers, cheapest/most decisive first.
#
# Two deliberate ABSENCES, both because they mark "a directory Claude Code
# reads from" rather than "a project root":
#   CLAUDE.md — Claude Code supports per-subdirectory CLAUDE.md files.
#   .claude/  — the user's HOME has one, and Claude Code writes one into
#               whatever directory a session happens to approve a permission
#               in. It is per-cwd session residue, exactly like CLAUDE.md.
#               Nothing is lost: every surveyed project carries `.git`, and
#               any initialised project is found by the database rungs.
#
# Manifests that legitimately nest (Cargo workspace members, monorepo
# packages) are handled by taking the OUTERMOST of a contiguous run — the
# member and its workspace are adjacent, so the workspace wins — bounded by
# the two ceilings in `_marker_root`.
_MARKERS = _VCS_MARKERS + (
    PIN_MARKER,
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "setup.py",
    "cabal.project", "stack.yaml", "pom.xml", "build.gradle",
    "build.gradle.kts", "Gemfile", "composer.json", "deno.json", "mix.exs",
)


def _norm(path):
    """Comparable spelling of a path. Never raises."""
    try:
        return os.path.normcase(str(path))
    except Exception:
        # why: normcase is pure string work, but `str(path)` on an exotic
        # Path subclass is not. A path that cannot be spelled simply never
        # matches anything, which costs at most a fallthrough to the last rung.
        return ""


def _exists(path):
    """`path.exists()` that cannot abort the walk.

    Every ancestor probe goes through here. `exists()` raises on more than
    "not found": permission denied on a parent, a stale NFS handle, ELOOP on
    a symlink cycle, an embedded NUL byte (ValueError), and on Windows an
    unreachable UNC share. Any of those must cost that ONE probe, never the
    whole resolution — the alternative is a hook that stops resolving because
    one unrelated directory up the tree is unreadable.
    """
    try:
        return path.exists()
    except Exception:
        return False


def _home_dirs():
    """Every spelling of "the user's home" this platform might use.

    `Path.home()` raises RuntimeError when it cannot resolve one, and inside
    a container `HOME` is routinely unset or `/`. Collecting all of them and
    treating the set as boundaries is cheaper than choosing correctly per
    platform, and a false entry can only ever stop the walk one level early —
    which degrades to the pre-v2.6.0 behaviour, never to the home database.
    """
    out = set()

    def _add(candidate):
        # BOTH spellings: as given, and resolved. `project_root` resolves the
        # cwd before walking (`Path(cwd).resolve()` below), so every chain
        # entry is a resolved path — while `Path.home()` and the env vars
        # come back UNRESOLVED. When `/home` is a symlink onto another volume
        # (or the profile is reached through any link), the unresolved
        # boundary matched nothing on the resolved chain and the walk went
        # straight through home to the `memory.db` a session run in `~` had
        # left there — the exact adoption this set exists to prevent.
        # Measured: HOME=/tmp/x/home/alice via a link to /tmp/x/vol/home/alice,
        # project_root(<home>/proj/src) -> <home>. A false entry can only stop
        # the walk one level early, so adding a spelling is always safe.
        out.add(_norm(candidate))
        try:
            out.add(_norm(Path(candidate).resolve()))
        except Exception:
            # why: an unresolvable home spelling (dangling link, unreachable
            # share) still contributes its literal form above
            pass

    try:
        _add(Path.home())
    except Exception:
        # why: no resolvable home (bare container, no passwd entry). The env
        # vars below may still name one; if none does, `_is_profile_dir` and
        # the depth cap are what remain.
        pass
    for var in ("USERPROFILE", "HOME"):
        val = os.environ.get(var, "")
        if val:
            _add(Path(val))
    out.discard("")
    return out


# Directory names that CONTAIN home directories. Their children are profile
# roots by platform convention, whatever the environment claims. These are
# fixed OS conventions, not machine paths: no user name, no drive letter.
_PROFILE_PARENTS = ("users", "home")

# Directories holding one entry per MOUNTED VOLUME, named for the volume:
# `/mnt/c` (WSL), `/cygdrive/c` (Cygwin), `/host_mnt/c` (Docker Desktop) and
# `/c` (Git-Bash) are all `C:\`. Fixed OS conventions, no machine specifics.
_MOUNT_CONTAINERS = ("mnt", "cygdrive", "host_mnt")

# A volume entry's name is ONE ASCII letter — spelled out because
# `str.isalpha` is Unicode-aware and would accept `é` as a drive.
_DRIVE_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyz")


def _is_fs_root(path):
    """True for `C:\\`, `/`, `\\\\server\\share` — a path that is its own parent."""
    try:
        return path.parent == path
    except Exception:
        # why: an undecomposable path is not a filesystem root; the depth cap
        # still bounds the walk
        return False


def _is_volume_root(path):
    """True when `path` is the root of a VOLUME: `/`, `C:\\`, `/mnt/c`, `/c`.

    A mounted volume's entry point IS a filesystem root seen from another
    namespace, and `_is_profile_dir` needs both spellings. Through v2.13.2 it
    could say "at the filesystem root" only, so a Windows profile reached from
    WSL was not a profile at all: measured on that tree,
    `_is_profile_dir(/mnt/c/Users/bob)` was False, `_chain` walked through the
    profile, and `project_root(/mnt/c/Users/bob/Projects/foo/src)` returned
    `/mnt/c/Users/bob` — adopting the home database the module docstring names
    (`C:\\Users\\<user>\\memory\\memory.db`, 495 KB on the reporting machine).

    Deliberately NARROW: the name must be ONE letter, so `/mnt/data` is not a
    volume root, and the container must itself sit at the filesystem root, so
    an in-repo `mnt/c/` is not one. A residual false positive costs one level
    of upward walk — the pre-v2.6.0 behaviour, never the home database.
    """
    try:
        if _is_fs_root(path):
            return True
        if len(path.name) != 1 or path.name.lower() not in _DRIVE_LETTERS:
            return False
        parent = path.parent
        return _is_fs_root(parent) or (
            parent.name.lower() in _MOUNT_CONTAINERS
            and _is_fs_root(parent.parent))
    except Exception:
        # why: an undecomposable path is not a volume root; the env boundary
        # and the depth cap still apply.
        return False


def _is_profile_dir(path):
    """True when `path` is a per-user profile root by platform convention.

    That is: a direct child of a directory named `Users` or `home` **that
    itself sits at a VOLUME root** — `C:\\Users\\alice`, `/home/alice`,
    `/Users/alice`, and `/mnt/c/Users/bob`, which is the first of those seen
    from WSL. Nothing machine-specific is encoded; only the shape is. The
    volume qualifier is `_is_volume_root`, which carries the WSL measurement;
    until v2.14.0 it was `_is_fs_root` and that mounted spelling was missed.

    The volume-root qualifier is load-bearing and was missing in v2.6.0.
    Without it any in-repo directory named `users` or `home` looked like a
    profile: measured, a session in `<repo>/users/alice/sub` had its chain
    truncated to `[cwd]`, so no rung could reach the repo and the next
    UserPromptSubmit planted a stray database four levels down — the exact
    defect this module exists to prevent, produced by the guard meant to
    prevent it. A real profile's parent (`C:\\Users`, `/home`) is always a
    child of the filesystem root; an application's `users/` table directory
    never is.

    `_home_dirs()` alone is not enough: every entry in it comes from the
    ENVIRONMENT. Redirect HOME/USERPROFILE — which containers, CI, sudo and
    this project's own test sandbox all do — and the real profile stops being
    recognised while still sitting on the walk. Measured: with HOME pointed
    into a sandbox, the walk climbed seven levels out of a temp fixture,
    reached the real profile, found the `memory/memory.db` that one session
    run in the home directory had left there, and pointed a brand-new project
    at it. Structure survives that redirection; environment does not.

    A machine that keeps projects directly under `/home` (some servers do)
    loses upward resolution for them and falls back to the pre-v2.6.0
    behaviour — `.ccm-root` is the escape hatch for that layout.
    """
    try:
        parent = path.parent
        return (parent != path and bool(path.name)
                and parent.name.lower() in _PROFILE_PARENTS
                and _is_volume_root(parent.parent))
    except Exception:
        # why: an exotic path that cannot be decomposed is simply not a
        # profile dir; the env boundary and the depth cap still apply.
        return False


def _chain(start):
    """`start` plus every ancestor that may be a project root, inner-first.

    Stops BELOW any home directory and below the filesystem root, and at (and
    including) a `.ccm-root` pin. `start` itself is always element 0 even when
    it is a home directory — a project the user genuinely started in `~` must
    keep working.

    "Any home directory" is deliberately two independent tests: the set the
    environment reports (`_home_dirs`) and the platform-conventional shape
    (`_is_profile_dir`). Either one alone has a documented blind spot; see
    `_is_profile_dir` for the case that got past the environment test.
    """
    out = [start]
    if _exists(start / PIN_MARKER):
        return out
    stops = _home_dirs()
    cur = start
    for _ in range(_MAX_DEPTH):
        parent = cur.parent
        if _norm(parent) == _norm(cur):
            break  # filesystem / drive root
        if _norm(parent) in stops or _is_profile_dir(parent):
            break  # never ascend into a home directory
        out.append(parent)
        if _exists(parent / PIN_MARKER):
            break
        cur = parent
    return out


def _has_db(directory):
    """`<dir>/.ccm/memory.db` — or the pre-v2.13.0 `<dir>/memory/memory.db` —
    exists, AND neither component of the one that answered is a symlink.

    BOTH spellings, because this predicate is what makes an UNMIGRATED project
    still a project. `core/layout.memory_dir` renames `memory/` to `.ccm/` on
    the first surface that asks, but the rename can be refused — a handle open
    inside the directory is enough on Windows (measured) — and resolution runs
    BEFORE anything asks. If rung 0 and rung 1 knew only the new name, a
    project whose move had not happened yet would stop being recognised as a
    root, and the marker rung would answer for it instead: the stray-database
    shape this whole module exists to prevent, reintroduced by the rename.
    `core/layout.state_dir_candidates` is the single place the two names live.

    Each candidate is judged INDEPENDENTLY: a link on `.ccm` does not
    invalidate a real `memory/` beside it. Adopting a directory through its
    real legacy database is safe, because the write side fails closed on its
    own — `core/progress.ensure_memory_dir` refuses a linked state directory
    whatever the resolver decided.

    The link check is fail-closed identity hygiene (register Y1): a symlinked
    state directory redirected every write to wherever the link pointed — this
    predicate returned True THROUGH the link, rung 0 adopted the directory as
    a project root, and memory.db landed at the link's target, outside the
    project and outside every reporting path. `is_symlink()` is an lstat, the
    portable guard core/markers already uses (O_NOFOLLOW is 0 on Windows and
    an fstat after open describes the TARGET). A deliberately linked layout
    is refused as project IDENTITY — `.ccm-root` in a real directory is the
    supported spelling for exotic layouts. Note the module docstring's
    symlink support is for the PROJECT directory itself, which stays intact:
    the probe below never resolves `directory`.
    """
    for mem in state_dir_candidates(directory):
        try:
            # core.markers._is_link, not bare is_symlink(): S_ISLNK is False
            # for a Windows junction, and the symlink-only probe returned True
            # THROUGH a junctioned state directory — rung 0 then adopted the
            # directory as a project root (measured; the exact hole this guard
            # exists to close, open on the primary platform).
            if _markers_is_link(mem) or _markers_is_link(mem / DB_FILENAME):
                continue
        except OSError:
            # why: a probe that cannot even lstat proves nothing — treat as no
            # database, same degradation as _exists on an unreadable ancestor
            continue
        if _exists(mem / DB_FILENAME):
            return True
    return False


def _has_marker(directory):
    for marker in _MARKERS:
        if _exists(directory / marker):
            return True
    return False


def _is_vcs_root(directory):
    """A repository root — used as a CEILING, never as a requirement."""
    for marker in _VCS_MARKERS:
        if _exists(directory / marker):
            return True
    return False


def _is_container(directory):
    """True when `directory` holds several projects rather than being one.

    Consulted for EVERY candidate, on every rung — see `_candidates`. In
    v2.6.0 it had exactly one call site, the marker rung's extension loop,
    and that single-point wiring was the shared root cause of three separate
    data-integrity defects: the database rung never asked (so a `memory/`
    created by one session in a projects folder captured every uninitialised
    project under it), the marker rung never asked about the FIRST marker it
    found (so one stray `package.json` there did the same), and nothing asked
    on behalf of a marker-less cwd. Measured on the reporting machine:
    `D:\\Projects` has 27 project-shaped children.

    A directory that is itself a VCS ROOT is never a container, however many
    project-shaped children it has: a repository is an unambiguous statement
    of "I am one project", and without that clause `Claude-Code-Local` —
    which carries its own `.git` plus three nested project databases — would
    be refused and a new subdirectory of it could no longer resolve to it.

    A `.ccm-root` pin is exempt too, but NOT here: `_candidates` short-circuits
    on it before this function is reached (v2.14.0), because the exemption was
    the thing that kept being forgotten by whichever rule was written last.

    Owning a `memory/` is deliberately NOT such a statement. A container that
    has acquired a stray database is exactly the damage shape being guarded
    against: measured, a projects folder with a stray `memory/memory.db`
    and five repository children captured every one of them.

    The two triggers are therefore asymmetric:

      * >= N children that are VCS roots — always decisive; this is the real
        projects-folder shape (27 such children on the reporting machine);
      * >= N children that merely own a database — only when this directory
        owns none itself. A project whose own database sits alongside two
        nested ones is a legitimate, observed layout, not a container.

    Reads the directory once and counts both; an unreadable directory is not
    a container. The read is BOUNDED by `_CONTAINER_SCAN_CAP` subdirectories
    (v2.12.2): past the cap the verdict falls through to what was seen.
    """
    if _is_vcs_root(directory):
        return False
    vcs_children = 0
    db_children = 0
    examined = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    # why: a race or a broken junction on one entry must not
                    # decide the verdict for the whole directory
                    continue
                examined += 1
                if examined > _CONTAINER_SCAN_CAP:
                    # Bounded, not exhaustive — see _CONTAINER_SCAN_CAP. The
                    # database verdict below still counts what was seen.
                    break
                child = Path(entry.path)
                if _is_vcs_root(child):
                    vcs_children += 1
                elif _has_db(child):
                    db_children += 1
                if vcs_children >= _CONTAINER_CHILDREN:
                    return True  # decisive; no need to read the rest
    except Exception:
        # why: an unreadable directory cannot be shown to be a container, and
        # the VCS ceiling plus _MARKER_MAX_RISE still bound the walk.
        return False
    return db_children >= _CONTAINER_CHILDREN and not _has_db(directory)


def _is_pinned(directory):
    """The user's `.ccm-root` declaration. The escape hatch, tested ONCE."""
    return _exists(directory / PIN_MARKER)


def _dependency_cut(chain):
    """Outermost index in `chain` that is somebody ELSE'S code, or -1.

    A dependency directory is recognised by NAME, and a name is a guess. It is
    overruled by either declaration this module already honours — a `.ccm-root`
    pin, or the directory's own database. Without that, a project CALLED
    `external` (or living under `~/work/external/`) had itself and everything
    below the cut dropped, `_nearest` never saw its own `.ccm/memory.db`, and
    EVERY subdirectory cwd resolved to itself: measured on all four of
    `external` / `vendor` / `deps` / `third_party`, and on the own-name layout
    even when pinned AND already initialised. That is the stray-database shape
    this module exists to prevent, produced by one of its own guards.
    """
    cut = -1
    for i, directory in enumerate(chain):
        try:
            dep = directory.name.lower() in _DEPENDENCY_DIRS
        except Exception:
            # why: an undecomposable name is not a dependency marker; the
            # remaining exclusions still apply to it
            continue
        if dep and not (_is_pinned(directory) or _has_db(directory)):
            cut = i
    return cut


def _candidates(chain):
    """The entries of `chain` that may legitimately BE a project root.

    Exclusions applied ONCE so that every rung inherits them — v2.6.0 attached
    its guards to one rung's inner loop and each rung that did not inherit them
    became its own defect. The PIN EXEMPTION is the same disease one level up:
    it was bolted onto `_is_container` and onto the volume-root rule
    individually, and the dependency-name rule never got one at all, so the
    documented escape hatch could not rescue a project whose own name was in
    `_DEPENDENCY_DIRS`. A `.ccm-root` is the user overruling the heuristics; it
    short-circuits ALL of them here, and nothing below re-tests it.

    1. Anything at or inside a DEPENDENCY directory — see `_dependency_cut`.
       Reading a file under `node_modules/` must anchor memory on the project
       that DEPENDS on the package, not on the package: measured, a cwd of
       `<repo>/node_modules/left-pad` resolved to `left-pad` itself (it has a
       `package.json`, so the marker rung accepted it) and planted a database
       where `nested_databases` does not look, so it could never even be
       reported. A directory owning a database survives the cut regardless,
       and that is a DELIBERATE verdict, not a side effect: when a dependency
       directory (or something inside one) owns a database, the DATABASE wins
       and the host repository does not. Two reasons. First, rung 0 already
       decides it that way and always has — `project_root` returns `cwd`
       whenever `cwd` itself owns a database, before this function is even
       called — so through v2.13.2 a cwd of `<repo>/node_modules/left-pad`
       resolved to `left-pad` while `<repo>/node_modules/left-pad/lib`
       resolved to `<repo>`, and one `cd` flipped which database the session
       wrote to. Second, the alternative is the one thing the module docstring
       refuses in capitals: overriding an existing database orphans whatever
       is in it, and a stray is byte-for-byte indistinguishable from a
       deliberate nested project. Nothing is lost by preferring it — no new
       stray is planted, and `nested_databases` reports it (v2.13.0 removed
       `vendor` and `node_modules` from that walker's skip set for exactly
       this case). What the cut still prevents is the case it was written for:
       a dependency that declares NOTHING is dependency internals, and a
       database is never CREATED down there.
    2. Containers of projects — see `_is_container`.
    3. A VOLUME ROOT itself. `_chain` documents that it stops "below the
       filesystem root" and never did: it appends each parent and only breaks
       once the parent equals the child, so `D:\\` was the last element of
       every chain on that drive. It is not a container either (a drive root
       rarely holds two VCS-root or two database-owning children), so it
       survived every rung — and a single `D:\\memory\\memory.db`, which any one
       mis-anchored session could have created, would then have been the
       nearest-database answer for EVERY project on the drive. `_is_volume_root`
       rather than `_is_fs_root` so `/mnt/c` is refused on the same terms as
       the `C:\\` it projects. ONE exemption beyond the pin: `start` itself, so
       a session genuinely opened at `D:\\` still resolves to itself through the
       unresolved rung.

    Filtering rather than truncating is deliberate: the walk must continue
    PAST a dependency directory to reach the project that owns it.
    """
    cut = _dependency_cut(chain)
    out = []
    for i, directory in enumerate(chain):
        if _is_pinned(directory):
            out.append(directory)     # the escape hatch overrules rules 1-3
            continue
        if i <= cut and not _has_db(directory):
            continue
        if i != 0 and _is_volume_root(directory):
            continue
        if not _is_container(directory):
            out.append(directory)
    return out


def _nearest(chain, predicate):
    for directory in chain:
        if predicate(directory):
            return directory
    return None


def _marker_root(chain):
    """Nearest marker, extended outward to the enclosing repository, or None.

    `chain` is already filtered by `_candidates`, so containers and
    dependency internals are simply not present — that is what fixes
    v2.6.0's hole where only the EXTENSION candidates were checked and the
    seed was accepted unconditionally.

    Two ceilings remain:

      * a VCS root ENDS the walk inclusively — a repository is the outermost
        thing that can still be one project. Using `.git` as a stop signal
        never *requires* git, so projects without any VCS keep working;
      * `_MARKER_MAX_RISE`, so a guess cannot travel far.

    The extension deliberately does NOT require a contiguous run of markers.
    v2.6.0 broke the run at the first marker-less ancestor, which is exactly
    what `packages/`, `apps/`, `crates/` and `libs/` are — so the standard
    monorepo layout resolved to the package and re-created the very stray
    database this module exists to prevent, while two docstrings claimed the
    opposite. Climbing to the enclosing repository is the whole point; the
    VCS ceiling is what bounds it.
    """
    start = None
    for i, directory in enumerate(chain[:_MARKER_MAX_RISE + 1]):
        if _has_marker(directory):
            start = i
            break
    if start is None:
        return None
    best = chain[start]
    if _is_vcs_root(best):
        return best
    for directory in chain[start + 1:_MARKER_MAX_RISE + 1]:
        if _is_vcs_root(directory):
            return directory  # the enclosing repository wins outright
        if _has_marker(directory):
            best = directory
    return best


def _from_env(chain):
    """`CLAUDE_PROJECT_DIR`, but only if it names a directory in the chain.

    The containment test is the whole point: an exported value left over from
    another project would otherwise redirect this project's memory into it.
    """
    raw = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if not raw:
        return None
    try:
        target = _norm(Path(raw).expanduser().resolve())
    except Exception:
        # why: an unusable value is not worth failing over — the remaining
        # rungs resolve this cwd on their own.
        return None
    for directory in chain:
        if _norm(directory) == target:
            return directory
    return None


def project_root(cwd, log=None):
    """Canonical project root for a hook payload's `cwd`.

    Returns the ORIGINAL `cwd` (as a Path, unresolved) whenever the answer is
    `cwd` itself. That keeps the common case — a session whose cwd already is
    the project root — byte-identical to the pre-v2.6.0 behaviour, including
    for symlinked project directories, where returning the resolved path
    would have moved `memory/` to the link target.

    `log`, when given, is a `core.logger` instance; it is used ONLY to report
    a redirection, and only from hooks rare enough that a line per event is
    not a line per turn.

    NEVER RAISES — including for a `cwd` that is not a path at all. v2.6.0
    claimed this and did not deliver it: the handler's own `return Path(cwd)`
    re-raised the TypeError it was catching, so a payload carrying
    `{"cwd": 123}` took the hook to rc=1 with a traceback on stderr, which
    Claude Code renders as an error. A non-path `cwd` now yields `Path(".")`,
    whose `.ccm/memory.db` check fails in every caller, so the hook exits
    quietly the way a malformed payload always should.
    """
    try:
        start = Path(cwd).resolve()
        if _has_db(start):
            return Path(cwd)  # rung 0: an existing database is terminal
        # Every rung reads the FILTERED chain: containers and dependency
        # internals are not project roots no matter which rung is asking.
        chain = _candidates(_chain(start))
        root = (_nearest(chain, _has_db)
                or _from_env(chain)
                or _marker_root(chain))
        if root is None or _norm(root) == _norm(start):
            return Path(cwd)
        if log is not None:
            try:
                log.info(f"project root resolved: {cwd} -> {root}")
            except Exception:
                # why: a diagnostic must never be what breaks the hook it is
                # describing.
                pass
        return root
    except Exception:
        # why: hook safety outranks correctness here. An unresolvable cwd
        # falls back to exactly what every hook did before this module
        # existed, so the worst case is the old behaviour, never a crash.
        return _safe_path(cwd)


def anchor_project(raw, announce=None):
    """Anchor a caller-supplied project path, for every non-hook entry point.

    Hooks call `project_root` directly; everything a *user* can point at a
    directory calls this. It exists because v2.7.0 anchored `mem.py` alone and
    left the other two surfaces behind, which is the same "the guard hung off
    one call site" mistake the resolver itself was rewritten to end:

      * `plan.py --project <subdir>` created `<subdir>/memory/memory.db` on
        the spot — even for the READ-ONLY `list` — and rung 0 (an existing
        database is terminal) then pinned all six hooks <!--ce:hooks:asof-->
        to that stray.
      * `mcp/server.py` fed raw `os.getcwd()` to three tools, so the one
        model-facing write surface wrote wherever the server happened to sit.

    `announce`, when given, is called with a one-line message. It is a
    parameter and not a `print` because the MCP server speaks JSON-RPC on
    stdout — printing there would corrupt the protocol, so that caller passes
    a logger instead. A redirection is never silent for a human-facing CLI: an
    explicit `--project` is an instruction, and quietly substituting something
    else would be worse than the bug this fixes.

    NEVER RAISES, for the same reason `project_root` does not.
    """
    try:
        root = project_root(raw)
        # BOTH sides resolved. `project_root` returns the ORIGINAL, UNRESOLVED
        # value whenever the answer is the input itself (see its docstring —
        # that is what keeps symlinked project directories working), so
        # comparing it against a resolved `raw` can never match for a relative
        # spelling. `--project .` is the documented primary invocation
        # (commands/cc-mem.md tells the wrapper to pass exactly that), so the
        # one-sided comparison announced ". is inside a project rooted at ."
        # on 100% of /cc-mem calls — inverting this function's own promise that
        # an announcement means a redirection actually happened.
        if announce is not None and \
                _norm(Path(root).resolve()) != _norm(Path(raw).resolve()):
            # An empty --project is legal and resolves to the caller's cwd;
            # echoing it verbatim produced "  is inside a project rooted at X",
            # a redirection notice that never said what it redirected FROM.
            shown = raw if str(raw).strip() else (
                f"{Path(raw).resolve()} (empty --project, so the current "
                f"directory)")
            announce(f"{shown} is inside a project rooted at {root} — using "
                     f"that root, so this command and the hooks share one "
                     f"database.")
        return str(root)
    except Exception as exc:
        if announce is not None:
            announce(f"project-root anchoring unavailable ({exc}); "
                     f"using {raw} as given")
        # why: an entry point must keep working even if anchoring cannot; the
        # raw value is exactly the pre-v2.7.0 behaviour, never a crash
        return raw


def _safe_path(cwd):
    """`Path(cwd)` that cannot itself raise — the fallback's fallback."""
    try:
        return Path(cwd)
    except Exception:
        # why: `cwd` was not path-like at all (a number, a list). Returning
        # the current directory keeps the contract "never raises" true; every
        # caller then fails its .ccm/memory.db existence check and exits 0.
        return Path(".")


def nested_databases(root, max_depth=3):
    """Every nested `memory.db` strictly BELOW `root`, for `mem.py status`.

    Looks under BOTH state-directory names, `.ccm` and the pre-v2.13.0
    `memory` — a stray is reported for the name it actually has on disk, and
    an unmigrated one is exactly the kind most likely to have been forgotten.
    A directory holding both spellings is reported ONCE: the answer is the
    owning project, and naming it twice would read as two strays.

    Resolution never merges or moves one of these — an existing database is a
    declaration of identity (see the module docstring). But a stray born
    before v2.6.0 is otherwise invisible: nothing reads it and nothing says
    so. This is the reporting half, deliberately on an explicit CLI command
    rather than in a hook, because it walks the tree.

    `max_depth` counts DIRECTORY LEVELS below `root` that may own a database,
    so `max_depth=3` reports `a`, `a/b` and `a/b/c`. v2.6.0 started the walk
    at depth 1 against the same guard and reported only two of those three:
    a directory's own `memory/` is discovered while scanning that directory,
    so the deepest level was cut off before it was ever read.

    The skip set is now only the two names that structurally cannot hold a
    project's memory directory. v2.6.0 also skipped `vendor`, `node_modules`,
    `.venv` and five more — the exact places the resolver could plant a
    database, which made the one tool meant to surface a stray blind to the
    strays most likely to exist.
    """
    out = []
    try:
        base = Path(root).resolve()
    except Exception:
        # why: an unresolvable root has nothing to report on
        return out
    skip = {".git", "__pycache__"}
    # Both spellings, taken from the one module that owns them. A relative
    # probe root gives `state_dir_candidates` nothing to join against, so the
    # NAMES are what is read out of it, never the paths.
    state_names = {c.name for c in state_dir_candidates(".")}
    seen = set()

    def walk(directory, depth):
        if depth > max_depth:
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except Exception:
            # why: one unreadable subtree must not abort the report
            return
        for entry in entries:
            try:
                if not entry.is_dir() or entry.name in skip:
                    continue
            except OSError:
                continue
            child = Path(entry.path)
            if entry.name in state_names:
                owner = child.parent
                if _exists(child / DB_FILENAME) and owner != base \
                        and _norm(owner) not in seen:
                    seen.add(_norm(owner))
                    out.append(owner)
                continue
            walk(child, depth + 1)

    walk(base, 0)
    return out
