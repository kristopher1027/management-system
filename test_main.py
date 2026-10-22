"""Focused tests for validation and report tie behavior."""
import os
import tempfile
import unittest
from unittest.mock import patch

import main


class ResourceManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "test.db")
        self.patcher = patch.object(main, "DB_FILE", self.db_path)
        self.patcher.start()
        self.cwd_patcher = patch("os.chdir", side_effect=os.chdir)
        self.cwd_patcher.start()
        self.previous_cwd = os.getcwd()
        os.chdir(self.temp.name)
        main.init_db()

    def tearDown(self):
        self.patcher.stop()
        os.chdir(self.previous_cwd)
        self.cwd_patcher.stop()
        self.temp.cleanup()

    def test_rejected_borrow_changes_nothing(self):
        before = main.find_resource("R003")["available"]
        ok, _ = main.borrow("F001", "R003", 4)
        self.assertFalse(ok)
        self.assertEqual(main.find_resource("R003")["available"], before)
        self.assertEqual(main.outstanding("F001", "R003"), 0)

    def test_over_return_changes_nothing(self):
        self.assertTrue(main.borrow("F001", "R001", 1)[0])
        before = main.find_resource("R001")["available"]
        ok, _ = main.return_item("F001", "R001", 2)
        self.assertFalse(ok)
        self.assertEqual(main.find_resource("R001")["available"], before)
        self.assertEqual(main.outstanding("F001", "R001"), 1)

    def test_most_borrowed_reports_ties(self):
        main.borrow("F001", "R001", 1)
        main.borrow("F001", "R002", 1)
        report = main.report_data()
        self.assertEqual({r["id"] for r in report["most_borrowed"]}, {"R001", "R002"})



class AssetPassportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "assets.db")
        self.patcher = patch.object(main, "DB_FILE", self.db_path)
        self.patcher.start()
        self.previous_cwd = os.getcwd()
        os.chdir(self.temp.name)
        main.init_db()

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.patcher.stop()
        self.temp.cleanup()

    def test_creating_passport_does_not_change_inventory_quantity(self):
        before = main.find_resource("R001")["total"]
        ok, message, asset = main.create_asset_passport(
            "R001", serial_number="SN-001", location="Lagos office", actor="owner"
        )
        self.assertTrue(ok, message)
        self.assertTrue(asset["asset_tag"].startswith("RH-"))
        self.assertEqual(main.find_resource("R001")["total"], before)
        self.assertEqual(main.find_resource("R001")["available"], before)

    def test_passport_records_updates_in_history(self):
        ok, _, asset = main.create_asset_passport("R001", condition="good", actor="owner")
        self.assertTrue(ok)
        ok, message = main.update_asset_passport(
            asset["asset_tag"], condition="fair", location="Room 2", actor="owner"
        )
        self.assertTrue(ok, message)
        saved = main.get_asset_passport(asset["asset_tag"])
        self.assertEqual(saved["condition"], "fair")
        self.assertEqual(saved["location"], "Room 2")
        self.assertEqual([event["event_type"] for event in saved["events"]][:2], ["location", "condition"])

    def test_passport_rejects_unknown_resource_and_invalid_status(self):
        ok, _, asset = main.create_asset_passport("NOT-REAL")
        self.assertFalse(ok)
        self.assertIsNone(asset)
        ok, _ = main.create_asset_passport("R001")
        self.assertTrue(ok)
        changed, message = main.update_asset_passport(asset_tag=main.list_asset_passports()[0]["asset_tag"], status="unknown")
        self.assertFalse(changed)
        self.assertIn("status", message.lower())

if __name__ == "__main__":
    unittest.main()
