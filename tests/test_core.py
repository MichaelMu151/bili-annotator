"""Regression tests for the annotation pipeline's statistical core.

Run:  PYTHONPATH=. python -m unittest tests.test_core -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compute_iaa import cohens_kappa, krippendorff_alpha  # noqa: E402


class AlphaTests(unittest.TestCase):
    """Values cross-validated against the `krippendorff` PyPI package (v0.6+)."""

    # Krippendorff (2011) canonical example, '.' = missing
    ROWS = [
        "1 2 3 3 2 1 4 1 2 . . .",
        "1 2 3 3 2 2 4 1 2 5 . .",
        "1 2 3 3 2 3 4 2 2 5 1 .",
        "1 2 3 3 2 4 4 1 2 5 1 .",
    ]

    def _units(self):
        grid = [r.split() for r in self.ROWS]
        units = []
        for u in range(12):
            units.append([grid[o][u] for o in range(4) if grid[o][u] != "."])
        return units

    def test_nominal_matches_reference(self):
        self.assertAlmostEqual(krippendorff_alpha(self._units(), "nominal"),
                               0.811024, places=6)

    def test_ordinal_matches_reference(self):
        self.assertAlmostEqual(
            krippendorff_alpha(self._units(), "ordinal", order=["1", "2", "3", "4", "5"]),
            0.843286, places=6)

    def test_perfect_agreement(self):
        self.assertEqual(krippendorff_alpha([["1", "1"], ["2", "2"], ["1", "1"]]), 1.0)

    def test_systematic_disagreement_is_negative(self):
        a = krippendorff_alpha([["1", "2"], ["2", "1"], ["1", "2"], ["2", "1"]])
        self.assertLess(a, 0)

    def test_no_overlap_returns_none(self):
        self.assertIsNone(krippendorff_alpha([["1"], ["2"]]))

    def test_single_category_returns_none(self):
        self.assertIsNone(krippendorff_alpha([["1", "1"], ["1", "1"]]))


class KappaTests(unittest.TestCase):
    def test_perfect(self):
        self.assertEqual(cohens_kappa(["a", "b", "a"], ["a", "b", "a"]), 1.0)

    def test_all_different(self):
        self.assertEqual(cohens_kappa(["a", "b"], ["b", "a"]), -1.0)

    def test_length_mismatch_returns_none(self):
        self.assertIsNone(cohens_kappa(["a"], ["a", "b"]))


if __name__ == "__main__":
    unittest.main()
