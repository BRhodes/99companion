import os
import unittest

from companion.burn import BurnModel, personal_formula
from companion.dat import DatFile, compute_hash, load_components, load_spells
from companion.ledger import FAILED, FIZZLED, SUCCESS, ComponentLedger
from companion.messages import decode
from tests.wire import Msg

PORTAL = r"C:\Program Files (x86)\Microsoft Games\Asheron's Call\portal.dat"

COMPONENTS = {
    2: {"name": "Iron Scarab", "cdm": 0.1},
    14: {"name": "Hyssop", "cdm": 1.0},
    34: {"name": "Powdered Agate", "cdm": 1.0},
    46: {"name": "Ash Wood", "cdm": 1.0},
    62: {"name": "Lightning Taper", "cdm": 1.0},
    **{63 + i: {"name": f"Taper {63 + i}", "cdm": 1.0} for i in range(12)},
}
SPELLS = {1122: {"name": "Blade Vulnerability Self II", "power": 75, "component_loss": 0.05,
                 "formula": [2, 67, 14, 34, 46, 62, 0, 0]}}


class FormulaTest(unittest.TestCase):
    def test_hash_matches_pstring(self):
        self.assertEqual(compute_hash(""), 0)
        self.assertEqual(compute_hash("a"), 97)
        self.assertEqual(compute_hash("ab"), 97 * 16 + 98)

    def test_five_component_formulas_are_not_personalised(self):
        base = [1, 7, 33, 44, 49, 0, 0, 0]
        self.assertEqual(personal_formula(base, "anyone"), base)

    def test_six_component_formula_replaces_taper_slot(self):
        out = personal_formula(SPELLS[1122]["formula"], "zorn")
        self.assertEqual(out[0], 2)
        self.assertEqual(out[2:], [14, 34, 46, 62, 0, 0])
        self.assertTrue(63 <= out[1] <= 74)
        self.assertEqual(out, personal_formula(SPELLS[1122]["formula"], "zorn"))  # deterministic


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.model = BurnModel(SPELLS, COMPONENTS)
        self.model.set_account("zorn")

    def test_expected_burn(self):
        burn = self.model.expected_burn(1122)
        self.assertAlmostEqual(burn["Hyssop"], 0.05)
        self.assertAlmostEqual(burn["Iron Scarab"], 0.005)
        self.assertAlmostEqual(self.model.expected_burn(1122, fizzled=True)["Hyssop"], 0.075)

    def test_casts_left(self):
        need = self.model.required(1122)
        inv = {name: 50 for name in need}
        inv["Hyssop"] = 5
        left, limiting = self.model.casts_left(1122, inv)
        self.assertEqual(limiting, "Hyssop")
        self.assertAlmostEqual(left, 5 / 0.05)
        inv["Ash Wood"] = 0
        self.assertEqual(self.model.casts_left(1122, inv), (0, "Ash Wood"))

    def test_ledger(self):
        ledger = ComponentLedger(self.model)
        casts = [{"spell_id": 1122, "components": []} for _ in range(3)]
        ledger.burned(casts[0], ["Hyssop"])
        ledger.settle(casts[0], SUCCESS)
        ledger.settle(casts[1], FIZZLED)
        ledger.settle(casts[2], FAILED)
        ledger.settle(casts[2], SUCCESS)  # settling twice is ignored
        self.assertEqual(ledger.observed["Hyssop"], 1)
        self.assertAlmostEqual(ledger.expected["Hyssop"], 0.05 + 0.075)
        snap = ledger.snapshot({}, 1.0, {1122: "Blade Vulnerability Self II"})
        spell = snap["spells"][0]
        self.assertEqual((spell["casts"], spell["fizzled"], spell["failed"]), (3, 1, 1))


class AccountTest(unittest.TestCase):
    def test_character_list_tail(self):
        m = Msg(0xF658).u32(1).u32(0).u32(0).u32(0x50000001).string("Zorn").u32(0)
        m.b += b"\x00" * 4 + b"zornaccount".ljust(20, b"\x00") + b"\x00" * 4  # [u32 0][name 20][u32 0]
        self.assertEqual(decode("s2c", 4, m.bytes()), [{"type": "account", "account": "zornaccount"}])


@unittest.skipUnless(os.path.exists(PORTAL), "needs the AC install's portal.dat")
class RealDatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dat = DatFile(PORTAL)
        cls.spells, cls.components = load_spells(dat), load_components(dat)

    def test_every_formula_decrypts_to_known_components(self):
        self.assertEqual(len(self.spells), 1635)
        self.assertEqual(len(self.components), 74)
        for sid, spell in self.spells.items():
            for c in spell["formula"]:
                self.assertTrue(c == 0 or c in self.components, (sid, spell["name"], spell["formula"]))

    def test_personalised_tapers_are_tapers(self):
        tapers = {k for k, v in self.components.items() if v["name"].endswith("Taper")}
        for sid, spell in self.spells.items():
            base = spell["formula"]
            out = personal_formula(base, "zorn")
            for slot in (1, 3, 6):
                if out[slot] != base[slot]:
                    self.assertIn(out[slot], tapers, (spell["name"], slot))


if __name__ == "__main__":
    unittest.main()
