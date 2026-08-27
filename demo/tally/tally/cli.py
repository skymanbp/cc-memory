"""Command-line front end for tally."""
from __future__ import annotations

import argparse
import sys

from .store import Store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tally", description="Tiny expense tally")
    parser.add_argument("--file", default="tally.json", help="storage file")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="record an expense")
    p_add.add_argument("amount", type=float)
    p_add.add_argument("note", nargs="?", default="")
    sub.add_parser("total", help="print the running total")
    sub.add_parser("list", help="list every entry")
    p_exp = sub.add_parser("export", help="export entries as JSON")
    p_exp.add_argument("out")

    args = parser.parse_args(argv)
    store = Store(args.file)
    if args.cmd == "add":
        e = store.add(args.amount, args.note)
        print(f"added {e['amount']:.2f} {e['note']}".rstrip())
    elif args.cmd == "total":
        print(f"{store.total():.2f}")
    elif args.cmd == "list":
        for i, e in enumerate(store.entries(), 1):
            print(f"{i:3d}  {e['amount']:8.2f}  {e['note']}")
    elif args.cmd == "export":
        print(f"wrote {store.export_json(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
