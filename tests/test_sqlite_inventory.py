import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "scripts" / "sqlite_inventory.py"
SPEC = importlib.util.spec_from_file_location("sqlite_inventory", PATH)
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


class SQLiteInventoryTest(unittest.TestCase):
    def test_ratchet_allows_reduction_but_blocks_growth(self):
        baseline = {"files": [{"path": "server/a.py", "connect_calls": 2, "pragma": 1,
                               "begin_immediate": 1, "insert_or": 0,
                               "autoincrement": 0, "last_insert_rowid": 0}]}
        reduced = {"files": [{"path": "server/a.py", "connect_calls": 1, "pragma": 0,
                              "begin_immediate": 0, "insert_or": 0,
                              "autoincrement": 0, "last_insert_rowid": 0}]}
        self.assertEqual(inventory.ratchet_violations(reduced, baseline), [])

        increased = {"files": [dict(reduced["files"][0], connect_calls=3),
                                dict(reduced["files"][0], path="server/new.py")]}
        violations = inventory.ratchet_violations(increased, baseline)
        self.assertTrue(any("usage increased" in item for item in violations))
        self.assertTrue(any("new SQLite-dependent file" in item for item in violations))
