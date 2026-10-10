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
        main.borrow("F001", "R001", 2)
        main.borrow("F001", "R002", 2)
        report = main.report_data()
        self.assertEqual({r["id"] for r in report["most_borrowed"]}, {"R001", "R002"})


if __name__ == "__main__":
    unittest.main()
