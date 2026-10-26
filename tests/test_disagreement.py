"""Tests for disagreement detection signatures (app._dim_signature).

Run: PYTHONPATH=. python -m unittest tests.test_disagreement -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import _dim_signature, _sig_to_json  # noqa: E402


class SignatureTests(unittest.TestCase):
    def sig(self, payload, dim):
        return _dim_signature(payload, dim)

    # ---- D1/D2: label-set comparison, order-independent -------------------
    def test_d1_order_independent(self):
        a = self.sig({"D1": {"labels": ["1.1", "1.2"]}}, "D1")
        b = self.sig({"D1": {"labels": ["1.2", "1.1"]}}, "D1")
        self.assertEqual(a, b)

    def test_d1_differs(self):
        a = self.sig({"D1": {"labels": ["1.3"]}}, "D1")
        b = self.sig({"D1": {"labels": ["1.2"]}}, "D1")
        self.assertNotEqual(a, b)

    def test_d1_unrated_is_none(self):
        self.assertIsNone(self.sig({"D1": {"labels": [], "primary": ""}}, "D1"))
        self.assertIsNone(self.sig({}, "D1"))

    def test_d1_primary_only_counts(self):
        self.assertEqual(self.sig({"D1": {"labels": [], "primary": "1.3"}}, "D1"),
                         frozenset({"1.3"}))

    # ---- D3: type + extent tuple -----------------------------------------
    def test_d3_same_type_diff_extent_disagrees(self):
        a = self.sig({"D3": {"type": "3.3", "extent": "2"}}, "D3")
        b = self.sig({"D3": {"type": "3.3", "extent": "3"}}, "D3")
        self.assertNotEqual(a, b)

    def test_d3_same(self):
        a = self.sig({"D3": {"type": "3.3", "extent": "3"}}, "D3")
        b = self.sig({"D3": {"type": "3.3", "extent": "3"}}, "D3")
        self.assertEqual(a, b)

    # ---- D4: polarity/arousal/toxicity triple ----------------------------
    def test_d4_polarity_differs(self):
        a = self.sig({"D4": {"polarity": "0", "toxicity": "0"}}, "D4")
        b = self.sig({"D4": {"polarity": "-1", "toxicity": "0"}}, "D4")
        self.assertNotEqual(a, b)

    def test_d4_all_unrated(self):
        self.assertIsNone(self.sig({"D4": {}}, "D4"))

    # ---- serialisation ---------------------------------------------------
    def test_sig_to_json_frozenset(self):
        self.assertEqual(_sig_to_json(frozenset({"1.3", "1.2"})), ["1.2", "1.3"])

    def test_sig_to_json_tuple(self):
        self.assertEqual(_sig_to_json(("3.3", "3")), ["3.3", "3"])


if __name__ == "__main__":
    unittest.main()
