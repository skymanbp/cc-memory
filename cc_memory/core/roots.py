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


def _is_fs_root(path):
    """True for `C:\\`, `/`, `\\\\server\\share` — a path that is its own parent."""
    try:
        return path.parent == path
    except Exception:
        # why: an undecomposable path is not a filesystem root; the depth cap
        # still bounds the walk
        return False


def _is_profile_dir(path):
    """True when `path` is a per-user profile root by platform convention.

    That is: a direct child of a directory named `Users` or `home` **that
    itself sits at the filesystem root** — `C:\\Users\\alice`, `/home/alice`,
    `/Users/alice`. Nothing machine-specific is encoded; only the shape is.

    The filesystem-root qualifier is load-bearing and was missing in v2.6.0.
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
                and _is_fs_root(parent.parent))
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
    """memory/memory.db exists AND neither component is a symlink.

    The link check is fail-closed identity hygiene (register Y1): a symlinked
    memory/ redirected every write to wherever the link pointed — this
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
    mem = directory / "memory"
    try:
        if mem.is_symlink() or (mem / "memory.db").is_symlink():
            return False
    except OSError:
        # why: a probe that cannot even lstat proves nothing — treat as no
        # database, same degradation as _exists on an unreadable ancestor
        return False
    return _exists(mem / "memory.db")


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

    A `.ccm-root` pin is exempt for the same reason, only more so: it is the
    documented escape hatch for precisely the case where the heuristics get it
    wrong, so letting a heuristic overrule it is self-defeating. Measured
    before this clause: a pinned directory with three repository children was
    judged a container, dropped from the candidate set, and a plain
    subdirectory under it resolved to ITSELF — the pin silently did nothing.

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
    a container.
    """
    if _is_vcs_root(directory) or _exists(directory / PIN_MARKER):
        return False
    vcs_children = 0
    db_children = 0
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


def _candidates(chain):
    """The entries of `chain` that may legitimately BE a project root.

    Two exclusions, applied once so that every rung inherits them. v2.6.0
    attached its guards to one rung's inner loop instead, and each rung that
    did not inherit them became its own defect.

    1. Anything at or inside a DEPENDENCY directory. Reading a file under
       `node_modules/`, `vendor/` or `site-packages/` must anchor memory on
       the project that DEPENDS on the package, not on the package: measured,
       a cwd of `<repo>/node_modules/left-pad` resolved to `left-pad` itself
       (it has a `package.json`, so the marker rung accepted it) and planted
       a database inside the dependency tree — where `nested_databases` does
       not look, so it could never even be reported. The cut is by outermost
       occurrence: everything nearer the cwd than a dependency directory is
       that dependency's internals.
    2. Containers of projects — see `_is_container`.
    3. The FILESYSTEM ROOT itself. `_chain` documents that it stops "below the
       filesystem root" and never did: it appends each parent and only breaks
       once the parent equals the child, so `D:\\` was the last element of
       every chain on that drive. It is not a container either (a drive root
       rarely holds two VCS-root or two database-owning children), so it
       survived every rung — and a single `D:\\memory\\memory.db`, which any
       one mis-anchored session could have created, would then have been the
       nearest-database answer for EVERY project on the drive. A drive root is
       never a project; excluding it here is the contract the docstring
       already promised. TWO exemptions: `start` itself (a session genuinely
       opened at `D:\\` still resolves to itself through the unresolved rung),
       and a root carrying `.ccm-root`. Without the second, this rule silently
       overruled the pin exemption added to `_is_container` in the same
       change — a repository living AT a volume root (a dedicated drive, a
       `net use` share, a USB stick) could not be pinned at all, so a cwd one
       level down lost every upward rung and resolved to itself.

    Filtering rather than truncating is deliberate: the walk must continue
    PAST a dependency directory to reach the project that owns it.
    """
    dep_cut = -1
    for i, directory in enumerate(chain):
        try:
            if directory.name.lower() in _DEPENDENCY_DIRS:
                dep_cut = i
        except Exception:
            # why: an undecomposable name is not a dependency marker; the
            # remaining exclusions still apply to it
            continue
    return [d for i, d in enumerate(chain)
            if i > dep_cut
            and (i == 0 or not _is_fs_root(d) or _exists(d / PIN_MARKER))
            and not _is_container(d)]


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
    whose `memory/memory.db` check fails in every caller, so the hook exits
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
        # caller then fails its memory/memory.db existence check and exits 0.
        return Path(".")


def nested_databases(root, max_depth=3):
    """Every `memory/memory.db` strictly BELOW `root`, for `mem.py status`.

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

    walk(base, 0)
    return out
