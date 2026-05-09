"""Regression tests for scraper._walk_for_price.

Run from the `pricing manager/` directory:
    python -m unittest tests.test_walker -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Make the package importable when run from the pricing-manager dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import ScrapeError, _walk_for_price  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


class TestWalker(unittest.TestCase):

    def test_listing_response_extracts_before_tax_total(self):
        br = _walk_for_price(load("weekly_listing_response.json"), nights=7)
        self.assertEqual(br.total, 1370.72)
        self.assertEqual(br.currency, "USD")
        self.assertEqual(br.total_qualifier, "before taxes")
        self.assertIn(("7 nights x $237.63", 1663.40), br.line_items)
        # Discount item has no description; walker labels it.
        self.assertIn(("Discount", -292.68), br.line_items)

    def test_checkout_response_extracts_after_tax_total_with_taxes(self):
        br = _walk_for_price(load("checkout_with_taxes_response.json"), nights=30)
        self.assertEqual(br.total, 5720.00)
        # No "before taxes" qualifier on the after-tax response — accessibility
        # label was just "$5,720.00 total".
        self.assertEqual(br.total_qualifier, "")
        labels = [l for l, _ in br.line_items]
        self.assertIn("Cleaning fee", labels)
        self.assertIn("Taxes", labels)

    def test_falls_back_to_primary_line_when_no_total_acc_label(self):
        # No accessibilityLabel containing "total"; should use discountedPrice.
        sdp = {
            "structuredDisplayPrice": {
                "primaryLine": {"discountedPrice": "$999"},
                "explanationData": {"priceDetails": [
                    {"items": [{"description": "5n x $200", "priceString": "$1,000"}]},
                ]},
            }
        }
        br = _walk_for_price({"x": sdp}, nights=5)
        self.assertEqual(br.total, 999.0)

    def test_raises_when_no_priceItems_anywhere(self):
        with self.assertRaises(ScrapeError):
            _walk_for_price({"data": {"unrelated": "shape"}}, nights=7)


if __name__ == "__main__":
    unittest.main()
