"""Tests for ResourceHub QR asset labels."""
import unittest

from asset_labels import make_qr_png


class AssetLabelTests(unittest.TestCase):
    def test_generates_png_for_asset_tag(self):
        result = make_qr_png("ASSET-001")
        self.assertTrue(result.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(result), 100)

    def test_rejects_empty_tag(self):
        with self.assertRaises(ValueError):
            make_qr_png(" ")

    def test_rejects_oversized_tag(self):
        with self.assertRaises(ValueError):
            make_qr_png("A" * 81)

    def test_rejects_non_text_tag(self):
        with self.assertRaises(ValueError):
            make_qr_png(None)


if __name__ == "__main__":
    unittest.main()
