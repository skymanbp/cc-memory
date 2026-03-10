#!/usr/bin/env python3
"""
cc-memory/plan.py -- Plan Queue CLI
====================================
Manage execution plans: add tasks, evaluate feasibility, execute in order.

Workflow:
  1. User adds plans:     plan.py add "Do X" "Do Y" "Do Z"
  2. Claude evaluates:    plan.py list  (Claude reads + evaluates via Agent)
  3. User approves:       plan.py approve [--all | ID...]
  4. User triggers exec:  plan.py exec [--next | --all | ID]
  5. Cleanup:             plan.py clear

Status flow: draft -> evaluating -> ready -> executing -> done/failed/skipped
"""
import argparse, json, sys, textwrap
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))
from db import MemoryDB

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

    if args.ids:
        plans = db.get_plans(pid)
        plans = [p for p in plans if p["id"] in args.ids]
    else:
        plans = db.get_plans(pid, statuses=["draft"])

    if not plans:
        print("No draft plans to evaluate.")
        return

    # Mark as evaluating
    for p in plans:
        db.update_plan_status(p["id"], "evaluating")

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
    db.update_plan_status(args.id, status, args.notes, field="feasibility")
    print(f"Plan #{args.id} -> {status}" + (f": {args.notes}" if args.notes else ""))


def cmd_approve(args):
    """Mark plans as 'ready' for execution."""
    db, pid, name = _get_db(args.project)

    if args.all:
        plans = db.get_plans(pid, statuses=["draft", "evaluating"])
    elif args.ids:
        plans = db.get_plans(pid)
        plans = [p for p in plans if p["id"] in args.ids]
    else:
        print("Specify --all or plan IDs to approve.")
        return

    for p in plans:
        db.update_plan_status(p["id"], "ready")
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
    elif args.id:
        plans = db.get_plans(pid)
        plans = [p for p in plans if p["id"] == args.id and p["status"] == "ready"]
    else:
        print("Specify --next, --all, or a plan ID.")
        return

    if not plans:
        print("No matching ready plans.")
        return

    print(f"\n=== EXECUTE PLANS ({name}) ===\n")
    for p in plans:
        db.update_plan_status(p["id"], "executing")
        print(f"EXECUTE #{p['id']} (order {p['exec_order']}):")
        print(f"  {p['content']}")
        print()
    print("=== END ===")
    print("\nClaude: Execute these plans in order, then mark done/failed via:")
    print(f"  python plan.py --project {args.project} done <ID> \"<result>\"")


def cmd_done(args):
    """Mark a plan as done with result."""
    db, pid, name = _get_db(args.project)
    db.update_plan_status(args.id, "done", args.result, field="result")
    print(f"Plan #{args.id} -> done" + (f": {args.result}" if args.result else ""))

    # Show next plan if any
    next_plan = db.get_next_plan(pid)
    if next_plan:
        print(f"\nNext ready plan: #{next_plan['id']} (order {next_plan['exec_order']})")
        print(f"  {next_plan['content']}")


def cmd_fail(args):
    """Mark a plan as failed."""
    db, pid, name = _get_db(args.project)
    db.update_plan_status(args.id, "failed", args.reason, field="result")
    print(f"Plan #{args.id} -> failed" + (f": {args.reason}" if args.reason else ""))


def cmd_skip(args):
    """Skip a plan."""
    db, pid, name = _get_db(args.project)
    db.update_plan_status(args.id, "skipped", args.reason, field="result")
    print(f"Plan #{args.id} -> skipped")


def cmd_clear(args):
    """Clear completed/failed/skipped plans."""
    db, pid, name = _get_db(args.project)
    n = db.clear_done_plans(pid)
    print(f"Cleared {n} completed plans from {name}.")


def cmd_reorder(args):
    """Reorder plans by providing new sequence of IDs."""
    db, pid, name = _get_db(args.project)
    db.reorder_plans(pid, args.ids)
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

    pe = sub.add_parser("evaluate", help="Start plan evaluation")
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


if __name__ == "__main__":
    args = make_parser().parse_args()
    dispatch = {
        "add": cmd_add, "list": cmd_list, "status": cmd_status,
        "evaluate": cmd_evaluate, "set-eval": cmd_set_eval,
        "approve": cmd_approve, "exec": cmd_exec,
        "done": cmd_done, "fail": cmd_fail, "skip": cmd_skip,
        "clear": cmd_clear, "reorder": cmd_reorder,
    }
    dispatch[args.command](args)
