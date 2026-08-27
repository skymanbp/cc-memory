import json
import os
import tempfile
import unittest

from tally.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "tally.json")

    def test_add_and_total(self):
        s = Store(self.path)
        s.add(10, "a")
        s.add(2.5, "b")
        self.assertEqual(s.total(), 12.5)

    def test_persists_between_instances(self):
        Store(self.path).add(3, "x")
        self.assertEqual(Store(self.path).total(), 3.0)

    def test_export_json_is_a_list_of_entries(self):
        s = Store(self.path)
        s.add(1, "one")
        out = s.export_json(os.path.join(self.dir, "out.json"))
        data = json.loads(open(out, encoding="utf-8").read())
        self.assertEqual(data, [{"amount": 1.0, "note": "one"}])


if __name__ == "__main__":
    unittest.main()
