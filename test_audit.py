import os
import sqlite3
import tempfile
import unittest

import audit


class FakeInventory:
    """Stand-in for main.py: one SQLite file per 'shop', rows readable as dicts."""

    def __init__(self, path):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.shop_a = FakeInventory(os.path.join(self.tmp.name, "a.db"))
        self.shop_b = FakeInventory(os.path.join(self.tmp.name, "b.db"))

    def test_entries_are_newest_first_with_all_fields(self):
        audit.record(self.shop_a, "amina", "owner", "item_added", "P001", "Added Laptop", "10.0.0.1")
        audit.record(self.shop_a, "tunde", "staff", "sale", "P001", "Sold 1", "10.0.0.2")
        rows = audit.entries(self.shop_a)
        self.assertEqual([r["action"] for r in rows], ["sale", "item_added"])
        self.assertEqual(rows[0]["actor"], "tunde")
        self.assertEqual(rows[0]["role"], "staff")
        self.assertEqual(rows[0]["ip"], "10.0.0.2")
        self.assertTrue(rows[0]["created_at"].endswith("Z"))

    def test_shops_never_share_entries(self):
        audit.record(self.shop_a, "amina", "owner", "sale", "P001", "A only")
        self.assertEqual(audit.entries(self.shop_b), [])

    def test_search_is_case_insensitive_and_matches_detail(self):
        audit.record(self.shop_a, "amina", "owner", "sale", "P001", "Sold to Chidi")
        audit.record(self.shop_a, "amina", "owner", "stock_in", "P002", "Received boxes")
        self.assertEqual(len(audit.entries(self.shop_a, "CHIDI")), 1)
        self.assertEqual(len(audit.entries(self.shop_a, "p00")), 2)

    def test_wildcards_in_search_are_literal(self):
        audit.record(self.shop_a, "amina", "owner", "sale", "P001", "50% off")
        audit.record(self.shop_a, "amina", "owner", "sale", "P002", "plain")
        self.assertEqual(len(audit.entries(self.shop_a, "%")), 1)
        self.assertEqual(len(audit.entries(self.shop_a, "_")), 0)

    def test_record_never_raises_when_database_is_unavailable(self):
        broken = FakeInventory(os.path.join(self.tmp.name, "missing-dir", "x.db"))
        self.assertFalse(audit.record(broken, "amina", "owner", "sale"))

    def test_long_values_are_truncated(self):
        audit.record(self.shop_a, "a" * 500, "owner", "sale", "t" * 500, "d" * 5000)
        row = audit.entries(self.shop_a)[0]
        self.assertEqual(len(row["actor"]), 80)
        self.assertEqual(len(row["target"]), 100)
        self.assertEqual(len(row["detail"]), 400)

    def test_csv_neutralizes_formulas_and_quotes_commas(self):
        audit.record(self.shop_a, "=HYPERLINK(\"x\")", "owner", "sale", "P1", "a, b")
        text = audit.to_csv(self.shop_a)
        self.assertIn("'=HYPERLINK", text)
        self.assertIn('"a, b"', text)
        self.assertTrue(text.startswith("id,time_utc,actor"))

    def test_describe_item_changes_reports_only_real_changes(self):
        before = [{"id": "P001", "price": 5, "sku": "A1", "supplier": "Acme", "reorder_level": 3}]
        data = {"resource_id": "p001", "price": 5.0, "sku": " A1 ",
                "supplier": "Zenith", "reorder_level": 4}
        self.assertEqual(audit.describe_item_changes(before, data),
                         "supplier: Acme → Zenith; reorder_level: 3 → 4")

    def test_describe_item_changes_unknown_item_or_no_change(self):
        before = [{"id": "P001", "price": 5, "sku": "", "supplier": "", "reorder_level": 3}]
        self.assertEqual(audit.describe_item_changes(before, {"resource_id": "NOPE", "price": 9}), "")
        self.assertEqual(audit.describe_item_changes(before, {"resource_id": "P001", "price": 5}), "")


if __name__ == "__main__":
    unittest.main()
