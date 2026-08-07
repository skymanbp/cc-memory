r"""Project-root resolution for the `cwd` a Claude Code hook is handed.

RAW docstring on purpose: this module's whole subject is Windows paths, and a
plain one turns `D:\Projects` into the invalid escape `\P`. Python answers
that with a SyntaxWarning on **stderr**, and Claude Code renders any hook
stderr as an error — so the prose below would have broken all six hooks the
first time they were compiled from a fresh copy. Caught by test_surfaces §4.

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

That stray then self-sustains: the other five hooks gate on
`memory/memory.db` merely EXISTING at `cwd`, so once born it keeps being
written. Measured on the reporting machine before the fix: 27 memories, its
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

  0. `cwd` itself has `memory/memory.db` → `cwd`, terminal, before anything
     else is even consulted.
  1. The NEAREST ancestor with `memory/memory.db`. No outward extension: see
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
    try:
        out.add(_norm(Path.home()))
    except Exception:
        # why: no resolvable home (bare container, no passwd entry). The env
        # vars below may still name one; if none does, `_is_profile_dir` and
        # the depth cap are what remain.
        pass
    for var in ("USERPROFILE", "HOME"):
        val = os.environ.get(var, "")
        if val:
            out.add(_norm(Path(val)))
    out.discard("")
    return out


# Directory names that CONTAIN home directories. Their children are profile
# roots by platform convention, whatever the environment claims. These are
# fixed OS conventions, not machine paths: no user name, no drive letter.
_PROFILE_PARENTS = ("users", "home")


def _is_profile_dir(path):
    """True when `path` is a per-user profile root by platform convention.

    That is: any direct child of a directory named `Users` or `home` — the
    shape a home directory has on Windows, Linux and macOS alike. Nothing
    machine-specific is encoded; only the container name is matched.

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
                and parent.name.lower() in _PROFILE_PARENTS)
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
    return _exists(directory / "memory" / "memory.db")


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

    Checked before every upward step of the marker rung. Without it, one
    stray marker in a projects folder captures every project under it at
    once: the reporting machine's `D:\\Projects` has 27 project-shaped
    children, so a single `package.json` dropped there — or a `memory/`
    created by one session run in that folder — would have collapsed all of
    them into one database.

    Scans at most a bounded prefix of the directory: it stops as soon as the
    threshold is reached, and a directory it cannot read is not a container.
    """
    try:
        hits = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    # why: a race or a broken junction on one entry must not
                    # decide the verdict for the whole directory
                    continue
                child = Path(entry.path)
                if _is_vcs_root(child) or _has_db(child):
                    hits += 1
                    if hits >= _CONTAINER_CHILDREN:
                        return True
        return False
    except Exception:
        # why: an unreadable directory cannot be shown to be a container, and
        # the caller's other ceiling (VCS root) plus _MARKER_MAX_RISE still
        # bound the walk.
        return False


def _nearest(chain, predicate):
    for directory in chain:
        if predicate(directory):
            return directory
    return None


def _marker_root(chain):
    """Nearest marker, extended outward under two ceilings, or None.

    The extension is what makes a Cargo workspace member or a monorepo
    package resolve to its workspace instead of to itself. The ceilings are
    what keep it from walking out of the project entirely:

      * a VCS root ENDS the walk inclusively — a repository is the outermost
        thing that can still be one project. Using `.git` as a stop signal
        never *requires* git, so projects without any VCS keep working;
      * a container directory is refused and stops the walk BELOW itself.
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
        if not _has_marker(directory) or _is_container(directory):
            break
        best = directory
        if _is_vcs_root(directory):
            break
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
    not a line per turn. Never raises: any failure returns `Path(cwd)`.
    """
    try:
        start = Path(cwd).resolve()
        if _has_db(start):
            return Path(cwd)  # rung 0: an existing database is terminal
        chain = _chain(start)
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
        return Path(cwd)


def nested_databases(root, max_depth=3):
    """Every `memory/memory.db` strictly BELOW `root`, for `mem.py status`.

    Resolution never merges or moves one of these — an existing database is a
    declaration of identity (see the module docstring). But a stray born
    before v2.6.0 is otherwise invisible: nothing reads it and nothing says
    so. This is the reporting half, deliberately on an explicit CLI command
    rather than in a hook, because it walks the tree.
    """
    out = []
    try:
        base = Path(root).resolve()
    except Exception:
        # why: an unresolvable root has nothing to report on
        return out
    skip = {".git", "node_modules", "target", "dist", "build", ".venv",
            "__pycache__", ".tox", "vendor"}

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
            if entry.name == "memory":
                if _exists(child / "memory.db") and child.parent != base:
                    out.append(child.parent)
                continue
            walk(child, depth + 1)

    walk(base, 1)
    return out
