#!/usr/bin/env python3
"""
cc-memory/cli/plan.py -- Plan Queue CLI (owns the `plans` table)
================================================================
This module owns exactly ONE table: **`plans`** -- a manual, user-driven work
queue (add -> evaluate -> approve -> exec -> done) that predates v2.2.

It is NOT the v2.2 live plan anchor, despite the similar name. That is a
separate system: the **`plan_active`** table (one row per project) backs
`memory/PLAN.md` and is driven by `/cc-mem plan-*` (`cc_memory/cli/mem.py`)
together with `cc_memory/core/plan.py` and the `plan-refiner` / `plan-guardian`
subagents. The two systems share no rows, no files and no code path -- only a
word. See `docs/CONTRACTS.md#plan-contract` for the live plan anchor.

Console entry point: `cc-memory-plan` (`[project.scripts]` in pyproject.toml)
resolves to `main()` below.

Workflow (this file):
  1. User adds plans:     plan.py add "Do X" "Do Y" "Do Z"
  2. Claude evaluates:    plan.py list  (Claude reads + evaluates via Agent)
  3. User approves:       plan.py approve [--all | ID...]
  4. User triggers exec:  plan.py exec [--next | --all | ID]
  5. Cleanup:             plan.py clear

Status flow: draft -> evaluating -> ready -> executing -> done/failed/skipped
"""
import argparse, sys, textwrap
from pathlib import Path

_HERE = Path(__file__).resolve().parent     # cc_memory/cli/
_PKG_ROOT = _HERE.parent                     # cc_memory/
sys.path.insert(0, str(_PKG_ROOT))

# Force UTF-8 on stdio: plan content is arbitrary user text and a gbk console
# otherwise kills `add` / `list` with UnicodeEncodeError AFTER the row is
# already written. Invoked from main() so importing this module stays inert.
from core.encoding_setup import enable_utf8_io

from core.db import MemoryDB
from core.layout import DB_FILENAME, memory_dir as resolve_memory_dir

STATUS_ICONS = {
    "draft": "[ ]", "evaluating": "[~]", "ready": "[*]",
    "executing": "[>]", "done": "[v]", "failed": "[X]", "skipped": "[-]",
}


def _anchor(project):
    """Anchor `--project` before anything derives a path from it.

    Until this existed, `plan.py --project <subdir>` built the scaffold in the
    subdirectory — `_get_db` below mkdirs and MemoryDB creates the file, so
    even the read-only `list` planted `<subdir>/.ccm/memory.db`. That is the
    exact stray the hooks have refused to create since v2.6.0, and because an
    existing database is a terminal rung, planting one there pinned all six
    hooks <!--ce:hooks:asof--> to it permanently. Announces through print:
    this CLI's stdout is already a human report, unlike the MCP server's.
    """
    try:
        from core.roots import anchor_project
    except Exception as exc:
        # why: the plan CLI must keep working even if the resolver cannot
        # load; the raw path is exactly the pre-v2.7.1 behaviour
        print(f"[cc-memory] project-root anchoring unavailable ({exc}); "
              f"using {project} as given")
        return project
    return anchor_project(project, announce=lambda m: print(f"[cc-memory] {m}"))


def _resolve(project):
    p = Path(project).resolve()
    return resolve_memory_dir(p) / DB_FILENAME, p.name


def _get_db(project, create=True):
    """Open the project's database; `create=False` refuses to conjure one.

    `MemoryDB.__init__` mkdirs and creates, so before v2.8.0 the READ-ONLY
    `list` and `status` fabricated a 140 KB empty database (without even the
    .ccm/.gitignore that every other creator writes — the omission that let
    a stray ride into version control) merely for asking what was in the
    queue. cli/mem.py has always printed "no memory database at X" instead,
    and two halves of one CLI pair must not disagree about that.
    """
    db_path, name = _resolve(project)
    if not db_path.exists():
        if not create:
            _die(f"no memory database at {db_path}\n"
                 f"       Run /ccm-load in that project, or a plan command "
                 f"that writes (e.g. add), to create one.")
        # ensure_memory_dir also writes memory/.gitignore. This creator did not,
        # and a database created without it is exactly how a 184 KB memory.db
        # rode into three commits of a sibling repository. A vanished project
        # directory raises FileNotFoundError, which _die reports rather than
        # recreating the project as an empty shell.
        try:
            from core.progress import ensure_memory_dir
            ensure_memory_dir(db_path.parent)
        except FileNotFoundError:
            _die(f"project directory no longer exists: {db_path.parent.parent}")
        except Exception as exc:
            print(f"[cc-memory] could not prepare {db_path.parent}"
                  f" ({exc}); add memory/ to .gitignore yourself")
    db = MemoryDB(db_path)
    pid = db.upsert_project(project)
    return db, pid, name


def _prog():
    """The name this process was actually invoked as, for every usage string.

    This module has TWO honest names and the usage text has to be right under
    both: the `cc-memory-plan` console script (`[project.scripts]` in
    pyproject.toml, which is what README's Plan Queue walkthrough tells users
    to run) and `python <install>/cli/plan.py`, which is what README documents
    for a standalone install. A hardcoded `prog="plan.py"` named a file that is
    not on a console-script user's PATH at all — `cc-memory-plan --help`
    printed `usage: plan.py ...` and every argparse failure began
    `plan.py: error:`.

    argparse's own default, basename(sys.argv[0]), is correct for both, so
    derive from that; the only adjustment is dropping the `.exe` that a Windows
    console-script wrapper carries, because `cc-memory-plan` is what the user
    types (PATHEXT supplies the suffix). `cli/mem.py:1291` solves the same
    problem by hardcoding `prog="cc-memory"`, which is right for the console
    script and wrong for `python .../cli/mem.py`.
    """
    name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name or "cc-memory-plan"


def _die(msg):
    """Print an error and exit non-zero (CLI failure paths must not exit 0)."""
    print(msg)
    sys.exit(1)


def _require_plans(db, pid, ids, statuses=None):
    """Resolve plan IDs *within this project*, refusing unknown / wrong status.

    First line of defence: it turns a typo'd or foreign id into an explicit
    "not found" + exit 1 instead of a silent no-op (or, before the SQL
    predicate below existed, another project's row) reported as success.

    Second line of defence, independent of this pre-flight: `_update_checked`
    passes `project_id` down to `db.update_plan_status`, which then carries
    `AND project_id = ?` in the UPDATE itself.
    """
    existing = {p["id"]: p for p in db.get_plans(pid)}
    missing = [i for i in ids if i not in existing]
    if missing:
        for i in missing:
            print(f"Plan #{i} not found.")
        sys.exit(1)
    if statuses:
        bad = [(i, existing[i]["status"]) for i in ids
               if existing[i]["status"] not in statuses]
        if bad:
            for i, st in bad:
                print(f"Plan #{i} is '{st}', expected: {', '.join(statuses)}.")
            sys.exit(1)
    return [existing[i] for i in ids]


def _update_checked(db, pid, plan_id, status, notes=None, field="feasibility"):
    """Update one plan and CONSUME the rowcount `db.update_plan_status` returns.

    Older builds of `core.db` return None here; an int is the post-v2.5
    contract. Both are handled, so a 0-row UPDATE can never be reported as
    success.

    `pid` goes through as `project_id`, which makes `db.update_plan_status`
    append `AND project_id = ?` to the UPDATE. `plans.id` is global to the DB
    FILE, not to a project, and one memory.db can hold several projects
    (ui/dashboard.py switches between them), so an unscoped UPDATE rewrites
    whatever row owns that id — including another project's. This CLI is
    already safe in practice because `_require_plans` pre-resolves every id
    inside the project; passing it anyway moves the guarantee into SQL, where
    it holds even for a future call site that forgets the pre-flight, and turns
    a foreign id into the 0 rowcount this function already reports as
    "not found".
    """
    rowcount = db.update_plan_status(plan_id, status, notes, field=field,
                                     project_id=pid)
    if isinstance(rowcount, int) and rowcount == 0:
        _die(f"Plan #{plan_id} not found.")


def _print_plans(plans, title="Plans"):
    if not plans:
        print(f"\n{title}: (empty)\n")
        return
    print(f"\n{title}:\n")
    for p in plans:
        icon = STATUS_ICONS.get(p["status"], "[?]")
        content = p["content"]
        if len(content) > 80:
            content = content[:77] + "..."
        line = f"  {icon} #{p['id']:3d}  (order {p['exec_order']})  {content}"
        if p.get("feasibility"):
            feas = p["feasibility"]
            if len(feas) > 60:
                feas = feas[:57] + "..."
            line += f"\n        Eval: {feas}"
        if p.get("result"):
            res = p["result"]
            if len(res) > 60:
                res = res[:57] + "..."
            line += f"\n        Result: {res}"
        print(line)
    print()


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_add(args):
    """Add one or more plans."""
    db, pid, name = _get_db(args.project)
    start_order = args.start_order

    for i, content in enumerate(args.content):
        order = start_order + i if start_order > 0 else 0
        plan_id = db.add_plan(pid, content, order)
        print(f"  Added plan #{plan_id}: {content}")

    print(f"\n{len(args.content)} plan(s) added to {name}.")
    plans = db.get_active_plans(pid)
    _print_plans(plans, "Active Plans")


def cmd_list(args):
    """List plans."""
    db, pid, name = _get_db(args.project, create=False)
    if args.all:
        plans = db.get_plans(pid)
        _print_plans(plans, f"All Plans ({name})")
    else:
        plans = db.get_active_plans(pid)
        _print_plans(plans, f"Active Plans ({name})")


def cmd_status(args):
    """Show plan queue status summary."""
    db, pid, name = _get_db(args.project, create=False)
    plans = db.get_plans(pid)
    counts = {}
    for p in plans:
        counts[p["status"]] = counts.get(p["status"], 0) + 1

    print(f"\nPlan Queue Status ({name}):")
    order = ["draft", "evaluating", "ready", "executing", "done", "failed", "skipped"]
    for s in order:
        if s in counts:
            icon = STATUS_ICONS[s]
            print(f"  {icon} {s:<12} {counts[s]}")
    total = sum(counts.values())
    print(f"\n  Total: {total} plans")


def cmd_evaluate(args):
    """Mark plans as 'evaluating' and print them for Claude to assess.
    Claude reads this output and uses Agent tool to evaluate each plan."""
    db, pid, name = _get_db(args.project)

    # Explicit IDs obey the SAME status predicate as the no-ID branch: only a
    # draft plan may enter evaluation. Previously `evaluate <id>` filtered with
    # no predicate at all and could drag an `executing` plan back to
    # `evaluating`, bypassing the state machine.
    if args.ids:
        plans = _require_plans(db, pid, args.ids, statuses=["draft"])
    else:
        plans = db.get_plans(pid, statuses=["draft"])

    if not plans:
        print("No draft plans to evaluate.")
        return

    # Mark as evaluating
    for p in plans:
        _update_checked(db, pid, p["id"], "evaluating")

    # Output structured format for Claude to read
    print(f"\n=== PLANS FOR EVALUATION ({name}) ===\n")
    for p in plans:
        print(f"PLAN #{p['id']} (order {p['exec_order']}):")
        print(f"  {p['content']}")
        print()
    print("=== END PLANS ===")
    print("\nClaude: Please evaluate each plan's feasibility, then update via:")
    print(f"  {_prog()} --project {args.project} set-eval <ID> <status> \"<notes>\"")


# The states a plan may be moved OUT OF by an approval-side command. `done`
# and `failed` are terminal: a plan that finished must not re-enter the ready
# queue, where `exec --next` would hand it back to Claude to run again.
# `cmd_evaluate` has filtered on its own predicate since the twin defect was
# fixed there; these two took explicit ids with NO predicate at all, so
# `approve 1` and `set-eval 1 ready` both walked a `done` plan backwards
# (measured: status `done` -> `ready` for each).
_APPROVABLE_STATUSES = ["draft", "evaluating"]


def cmd_set_eval(args):
    """Set evaluation result for a plan."""
    db, pid, name = _get_db(args.project)
    status = args.status  # 'ready' or 'skipped'
    _require_plans(db, pid, [args.id], statuses=_APPROVABLE_STATUSES)
    _update_checked(db, pid, args.id, status, args.notes, field="feasibility")
    print(f"Plan #{args.id} -> {status}" + (f": {args.notes}" if args.notes else ""))


def cmd_approve(args):
    """Mark plans as 'ready' for execution."""
    db, pid, name = _get_db(args.project)

    if args.all:
        plans = db.get_plans(pid, statuses=_APPROVABLE_STATUSES)
        if not plans:
            print("No draft/evaluating plans to approve.")
            return
    elif args.ids:
        # The SAME predicate as the --all branch above. It had none, so the
        # two spellings of one command disagreed about the state machine.
        plans = _require_plans(db, pid, args.ids,
                               statuses=_APPROVABLE_STATUSES)
    else:
        prog = _prog()
        _die(f"Specify --all or plan IDs to approve.\n"
             f"  usage: {prog} --project <path> approve --all\n"
             f"         {prog} --project <path> approve 1 2 3")

    for p in plans:
        _update_checked(db, pid, p["id"], "ready")
        print(f"  Plan #{p['id']} -> ready")
    print(f"\n{len(plans)} plan(s) approved.")


def cmd_exec(args):
    """Mark a plan as executing (Claude should then execute it)."""
    db, pid, name = _get_db(args.project)

    # REFUSE a contradictory invocation instead of silently picking one. The
    # branch order was `--next`, then `--all`, then the positional id, and a
    # supplied id was never validated or even mentioned when a flag was also
    # present: `exec --next 3` exited 0 and executed plan #1 (measured). The
    # installer already sets the precedent of refusing arguments it cannot
    # honour rather than guessing.
    if args.id is not None and (args.next or args.all):
        flag = "--next" if args.next else "--all"
        _die(f"`exec {flag} {args.id}` is contradictory: {flag} picks the "
             f"plan(s) itself. Drop the id, or drop {flag} to execute #{args.id}.")

    if args.next:
        plan = db.get_next_plan(pid)
        if not plan:
            print("No ready plans to execute.")
            return
        plans = [plan]
    elif args.all:
        plans = db.get_plans(pid, statuses=["ready"])
        if not plans:
            print("No ready plans to execute.")
            return
    elif args.id is not None:
        plans = _require_plans(db, pid, [args.id], statuses=["ready"])
    else:
        prog = _prog()
        _die(f"Specify --next, --all, or a plan ID.\n"
             f"  usage: {prog} --project <path> exec --next\n"
             f"         {prog} --project <path> exec --all\n"
             f"         {prog} --project <path> exec 3")

    print(f"\n=== EXECUTE PLANS ({name}) ===\n")
    for p in plans:
        _update_checked(db, pid, p["id"], "executing")
        print(f"EXECUTE #{p['id']} (order {p['exec_order']}):")
        print(f"  {p['content']}")
        print()
    print("=== END ===")
    print("\nClaude: Execute these plans in order, then mark done/failed via:")
    print(f"  {_prog()} --project {args.project} done <ID> \"<result>\"")


def cmd_done(args):
    """Mark a plan as done with result."""
    db, pid, name = _get_db(args.project)
    _require_plans(db, pid, [args.id])
    _update_checked(db, pid, args.id, "done", args.result, field="result")
    print(f"Plan #{args.id} -> done" + (f": {args.result}" if args.result else ""))

    # Show next plan if any
    next_plan = db.get_next_plan(pid)
    if next_plan:
        print(f"\nNext ready plan: #{next_plan['id']} (order {next_plan['exec_order']})")
        print(f"  {next_plan['content']}")


def cmd_fail(args):
    """Mark a plan as failed."""
    db, pid, name = _get_db(args.project)
    _require_plans(db, pid, [args.id])
    _update_checked(db, pid, args.id, "failed", args.reason, field="result")
    print(f"Plan #{args.id} -> failed" + (f": {args.reason}" if args.reason else ""))


def cmd_skip(args):
    """Skip a plan."""
    db, pid, name = _get_db(args.project)
    _require_plans(db, pid, [args.id])
    _update_checked(db, pid, args.id, "skipped", args.reason, field="result")
    print(f"Plan #{args.id} -> skipped")


def cmd_clear(args):
    """Clear completed/failed/skipped plans."""
    db, pid, name = _get_db(args.project)
    n = db.clear_done_plans(pid)
    print(f"Cleared {n} completed plans from {name}.")


def cmd_reorder(args):
    """Reorder plans by providing new sequence of IDs."""
    db, pid, name = _get_db(args.project)
    _require_plans(db, pid, args.ids)
    rowcount = db.reorder_plans(pid, args.ids)
    if isinstance(rowcount, int) and rowcount == 0:
        _die("No plans were reordered.")
    plans = db.get_active_plans(pid)
    _print_plans(plans, f"Reordered Plans ({name})")


# ── Parser ───────────────────────────────────────────────────────────────────

def make_parser():
    # `prog=_prog()` instead of a hardcoded name, and `%(prog)s` in the epilog
    # (argparse expands it there, RawDescriptionHelpFormatter included) so the
    # workflow block names the command the reader actually ran. The subcommands
    # are listed bare rather than repeated after the prog name, which keeps the
    # comment column aligned whatever length that name turns out to be.
    p = argparse.ArgumentParser(
        prog=_prog(), description="cc-memory Plan Queue",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Workflow -- each line below runs as
            `%(prog)s --project <path> <the line>`:

              add "Task 1" "Task 2" "Task 3"     # Add plans
              list                               # View plans
              evaluate                           # Start evaluation
              set-eval 1 ready "Looks feasible"  # Set eval result
              approve --all                      # Approve all
              exec --next                        # Execute next plan
              done 1 "Completed successfully"    # Mark done
              clear                              # Clean up

            This CLI owns the `plans` queue table only. The v2.2 live plan
            anchor (memory/PLAN.md, `plan_active`) is `/cc-mem plan-*`.
        """))
    p.add_argument("--project", required=True, help="Project root path")
    sub = p.add_subparsers(dest="command", required=True)

    pa = sub.add_parser("add", help="Add plans")
    pa.add_argument("content", nargs="+", help="Plan descriptions")
    pa.add_argument("--start-order", type=int, default=0,
                    help="Starting order number (0=auto)")

    pl = sub.add_parser("list", help="List plans")
    pl.add_argument("--all", action="store_true", help="Include done/failed")

    sub.add_parser("status", help="Queue status summary")

    pe = sub.add_parser("evaluate", help="Start plan evaluation (draft plans only)")
    pe.add_argument("ids", nargs="*", type=int, help="Specific plan IDs")

    ps = sub.add_parser("set-eval", help="Set evaluation result")
    ps.add_argument("id", type=int)
    ps.add_argument("status", choices=["ready", "skipped"])
    ps.add_argument("notes", nargs="?", default="")

    pv = sub.add_parser("approve", help="Approve plans")
    pv.add_argument("ids", nargs="*", type=int)
    pv.add_argument("--all", action="store_true")

    px = sub.add_parser("exec", help="Execute plans")
    px.add_argument("--next", action="store_true", help="Execute next ready plan")
    px.add_argument("--all", action="store_true", help="Execute all ready plans")
    px.add_argument("id", nargs="?", type=int, help="Specific plan ID")

    pd = sub.add_parser("done", help="Mark plan done")
    pd.add_argument("id", type=int)
    pd.add_argument("result", nargs="?", default="")

    pf = sub.add_parser("fail", help="Mark plan failed")
    pf.add_argument("id", type=int)
    pf.add_argument("reason", nargs="?", default="")

    pk = sub.add_parser("skip", help="Skip a plan")
    pk.add_argument("id", type=int)
    pk.add_argument("reason", nargs="?", default="")

    sub.add_parser("clear", help="Clear done/failed/skipped plans")

    pr = sub.add_parser("reorder", help="Reorder plans")
    pr.add_argument("ids", nargs="+", type=int, help="Plan IDs in new order")

    return p


def main():
    """Console entry point (`cc-memory-plan`, pyproject [project.scripts])."""
    enable_utf8_io()
    args = make_parser().parse_args()
    # Anchor ONCE, here, before any of the 13 `_get_db(args.project)` call
    # sites or the two help lines that echo the path back. Anchoring inside
    # `_resolve` instead would have placed the database at the root while
    # `_get_db` still passed the RAW path to `upsert_project` — one file, two
    # project rows, which is worse than the stray it replaced.
    # `is not None`, not truthiness: `--project ""` must anchor too.
    if getattr(args, "project", None) is not None:
        # Opt-out BEFORE anchoring, exactly like the hooks: anchoring first
        # would resolve an excluded SUBDIRECTORY up to its unexcluded parent.
        try:
            from core.modes import cli_opt_out_notice
            notice = cli_opt_out_notice(args.project)
            if notice:
                print(f"[cc-memory] {notice}")
                return
        except ImportError:
            # why: a plan CLI that cannot load the opt-out check must still
            # work; hooks and the MCP server enforce it independently
            pass
        args.project = _anchor(args.project)
    dispatch = {
        "add": cmd_add, "list": cmd_list, "status": cmd_status,
        "evaluate": cmd_evaluate, "set-eval": cmd_set_eval,
        "approve": cmd_approve, "exec": cmd_exec,
        "done": cmd_done, "fail": cmd_fail, "skip": cmd_skip,
        "clear": cmd_clear, "reorder": cmd_reorder,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
