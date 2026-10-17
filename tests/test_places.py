"""The bundled places: towns without a town-network portal are there too (from their name signs)."""

import json
import unittest
from pathlib import Path

PLACES = json.loads((Path(__file__).resolve().parent.parent / "companion" / "data" / "places.json").read_text())


class TownsTest(unittest.TestCase):
    def test_towns_with_only_a_sign_are_known(self):
        towns = {t["name"]: t for t in PLACES["towns"]}
        for name in ("Lin", "Baishi", "Glenden Wood", "Qalaba'r", "Uziz", "Holtburg",
                     "Kara", "Stonehold", "Plateau Village", "Wai Jhou", "Crater"):      # (the last: settlements)
            self.assertIn(name, towns)
        self.assertAlmostEqual(towns["Lin"]["ns"], -54.8, delta=1)
        self.assertAlmostEqual(towns["Lin"]["ew"], 73.8, delta=1)
        self.assertAlmostEqual(towns["Kara"]["ns"], -83.2, delta=1)
        self.assertAlmostEqual(towns["Stonehold"]["ew"], -21.8, delta=1)

    def test_no_town_twice_and_no_shops(self):
        names = [t["name"].lower() for t in PLACES["towns"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertFalse({"healer", "tailor", "bowyer", "grocery"} & set(names))


if __name__ == "__main__":
    unittest.main()
