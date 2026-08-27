"""JSON-backed storage for tally entries.

Known bug (deliberate, for the demo): `add()` accepts a negative amount
without complaint, so `total()` can silently go down.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List


class Store:
    def __init__(self, path: str | Path = "tally.json") -> None:
        self.path = Path(path)
        self._entries: List[Dict] = []
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))

    def add(self, amount: float, note: str = "") -> Dict:
        entry = {"amount": float(amount), "note": note}
        self._entries.append(entry)
        self._save()
        return entry

    def entries(self) -> List[Dict]:
        return list(self._entries)

    def total(self) -> float:
        return round(sum(e["amount"] for e in self._entries), 2)

    def export_json(self, out_path: str | Path) -> Path:
        """Write all entries as a JSON array. The reporting script reads this."""
        out = Path(out_path)
        out.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")
        return out

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")
