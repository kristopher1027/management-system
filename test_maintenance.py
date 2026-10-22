"""Tests for the ResourceHub maintenance and repair log."""
import os
import tempfile
import unittest
from unittest.mock import patch

import main


class MaintenanceRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "maintenance.db")
        self.patcher = patch.object(main, "DB_FILE", self.db_path)
        self.patcher.start()
        self.previous_cwd = os.getcwd()
        os.chdir(self.temp.name)
        main.init_db()
        ok, message, self.asset = main.create_asset_passport(
            "R001", serial_number="MAINT-SN-01", actor="owner"
        )
        self.assertTrue(ok, message)
        self.inventory_total = main.find_resource("R001")["total"]

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.patcher.stop()
        self.temp.cleanup()

    def test_create_record_and_preserve_inventory(self):
        ok, message, record = main.create_maintenance_record(
            self.asset["asset_tag"],
            "Screen is cracked",
            action_taken="Screen replacement",
            cost=125.50,
            technician="Service team",
            status="completed",
            reported_by="owner",
        )
        self.assertTrue(ok, message)
        self.assertEqual(record["asset_tag"], self.asset["asset_tag"])
        self.assertEqual(record["cost"], 125.50)
        self.assertIsNotNone(record["completed_at"])
        self.assertEqual(main.find_resource("R001")["total"], self.inventory_total)
        saved_asset = main.get_asset_passport(self.asset["asset_tag"])
        self.assertEqual(saved_asset["events"][0]["event_type"], "maintenance")

    def test_reject_invalid_problem_cost_status_and_asset(self):
        cases = [
            ("", 0, "open", self.asset["asset_tag"]),
            ("Broken", -1, "open", self.asset["asset_tag"]),
            ("Broken", float("inf"), "open", self.asset["asset_tag"]),
            ("Broken", 1, "unknown", self.asset["asset_tag"]),
            ("Broken", 1, "open", "MISSING-ASSET"),
        ]
        for problem, cost, status, asset_tag in cases:
            with self.subTest(problem=problem, cost=cost, status=status, asset=asset_tag):
                ok, message, *_ = main.create_maintenance_record(
                    asset_tag, problem, cost=cost, status=status
                )
                self.assertFalse(ok)
                self.assertTrue(message)

        self.assertEqual(main.list_maintenance_records(), [])

    def test_filter_and_update_record(self):
        ok, message, record = main.create_maintenance_record(
            self.asset["asset_tag"], "Battery failing"
        )
        self.assertTrue(ok, message)
        ok, message = main.update_maintenance_record(
            record["id"], action_taken="Battery replaced",
            cost=75, technician="Technician A", status="completed"
        )
        self.assertTrue(ok, message)
        results = main.list_maintenance_records(
            asset_tag=self.asset["asset_tag"], status="completed"
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["action_taken"], "Battery replaced")
        self.assertEqual(results[0]["cost"], 75)
        self.assertIsNotNone(results[0]["completed_at"])

    def test_update_rejects_invalid_values(self):
        ok, _, record = main.create_maintenance_record(
            self.asset["asset_tag"], "Keyboard not working"
        )
        self.assertTrue(ok)
        ok, message = main.update_maintenance_record(record["id"], cost=-10)
        self.assertFalse(ok)
        self.assertIn("cost", message.lower())
        ok, message = main.update_maintenance_record(record["id"], status="unknown")
        self.assertFalse(ok)
        self.assertIn("status", message.lower())


if __name__ == "__main__":
    unittest.main()
