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

STATUS_ICONS = {
    "draft": "[ ]", "evaluating": "[~]", "ready": "[*]",
    "executing": "[>]", "done": "[v]", "failed": "[X]", "skipped": "[-]",
}


def _resolve(project):
    p = Path(project).resolve()
    return p / "memory" / "memory.db", p.name


def _get_db(project):
    db_path, name = _resolve(project)
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
    db = MemoryDB(db_path)
    pid = db.upsert_project(project)
    return db, pid, name


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
    db, pid, name = _get_db(args.project)
    if args.all:
        plans = db.get_plans(pid)
        _print_plans(plans, f"All Plans ({name})")
    else:
        plans = db.get_active_plans(pid)
        _print_plans(plans, f"Active Plans ({name})")


def cmd_status(args):
    """Show plan queue status summary."""
    db, pid, name = _get_db(args.project)
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
    print(f"  python plan.py --project {args.project} set-eval <ID> <status> \"<notes>\"")


def cmd_set_eval(args):
    """Set evaluation result for a plan."""
    db, pid, name = _get_db(args.project)
    status = args.status  # 'ready' or 'skipped'
    _require_plans(db, pid, [args.id])
    _update_checked(db, pid, args.id, status, args.notes, field="feasibility")
    print(f"Plan #{args.id} -> {status}" + (f": {args.notes}" if args.notes else ""))


def cmd_approve(args):
    """Mark plans as 'ready' for execution."""
    db, pid, name = _get_db(args.project)

    if args.all:
        plans = db.get_plans(pid, statuses=["draft", "evaluating"])
        if not plans:
            print("No draft/evaluating plans to approve.")
            return
    elif args.ids:
        plans = _require_plans(db, pid, args.ids)
    else:
        _die("Specify --all or plan IDs to approve.\n"
             "  usage: plan.py --project <path> approve --all\n"
             "         plan.py --project <path> approve 1 2 3")

    for p in plans:
        _update_checked(db, pid, p["id"], "ready")
        print(f"  Plan #{p['id']} -> ready")
    print(f"\n{len(plans)} plan(s) approved.")


def cmd_exec(args):
    """Mark a plan as executing (Claude should then execute it)."""
    db, pid, name = _get_db(args.project)

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
        _die("Specify --next, --all, or a plan ID.\n"
             "  usage: plan.py --project <path> exec --next\n"
             "         plan.py --project <path> exec --all\n"
             "         plan.py --project <path> exec 3")

    print(f"\n=== EXECUTE PLANS ({name}) ===\n")
    for p in plans:
        _update_checked(db, pid, p["id"], "executing")
        print(f"EXECUTE #{p['id']} (order {p['exec_order']}):")
        print(f"  {p['content']}")
        print()
    print("=== END ===")
    print("\nClaude: Execute these plans in order, then mark done/failed via:")
    print(f"  python plan.py --project {args.project} done <ID> \"<result>\"")


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
    p = argparse.ArgumentParser(
        prog="plan.py", description="cc-memory Plan Queue",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Workflow:
              plan.py add "Task 1" "Task 2" "Task 3"   # Add plans
              plan.py list                               # View plans
              plan.py evaluate                           # Start evaluation
              plan.py set-eval 1 ready "Looks feasible"  # Set eval result
              plan.py approve --all                      # Approve all
              plan.py exec --next                        # Execute next plan
              plan.py done 1 "Completed successfully"    # Mark done
              plan.py clear                              # Clean up

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
