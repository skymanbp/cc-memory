"""Legacy CSV importer from the pre-JSON days.

Nobody has run this since the JSON store landed. Kept only because the
reporting team once asked for a CSV path; revisit when storage changes.
"""
import csv
import sys

from tally.store import Store


def main(path):
    s = Store()
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            s.add(float(row["amount"]), row.get("note", ""))
    print(f"imported {len(s.entries())} entries")


if __name__ == "__main__":
    main(sys.argv[1])
