"""Regressions from a real session on the live server (byte layouts and message order as observed)."""

import unittest

from companion.messages import decode
from companion.pipeline import Pipeline
from companion.tracker import Tracker
from tests.wire import Conversation, Msg

PLAYER = 0x500000C9
SPELLS = {528: {"name": "Acid Vulnerability Self II", "school": 2, "power": 75, "mana": 30,
                "component_loss": 0.05, "formula": [2, 63, 14, 34, 42, 62, 0, 0]}}
COMPONENTS = {2: {"name": "Iron Scarab", "cdm": 0.1}, 63: {"name": "Red Taper", "cdm": 1.0},
              14: {"name": "Ginseng", "cdm": 1.0}, 34: {"name": "Powdered Onyx", "cdm": 1.0},
              42: {"name": "Gypsum", "cdm": 1.0}, 62: {"name": "Cedar Talisman", "cdm": 0.1}}

# A stack of 5 Mandrake in the player's pack, exactly as the server sent it.
MANDRAKE = bytes.fromhex(
    "45f70000943404901101010 1ef0bef0b0000009 00f200e009a020000 8118020014040000"
    "65000000140000202b00003481010002cdcccc3e00000000000000000000000000000000"
    "0100000018700000090 04d616e6472616b65000007030b1400100000100000003200000"
    "0010000000500320 0c9000050".replace(" ", ""))

# The character list: one character, then [u32 0][20-byte account][u32 0].
CHARLIST = (bytes.fromhex("58f60000" "01000000" "00000000" "00000000" "c9000050")
            + b"\x05\x00Zorn\x00\x00" + bytes(4)                   # AcString + deleteTimeout
            + bytes(4) + b"zorn".ljust(20, b"\x00") + bytes(4))    # [u32 0][account 20][u32 0]


class LiveLayouts(unittest.TestCase):
    def test_component_stack_and_container(self):
        (obj,) = decode("s2c", 3, MANDRAKE)
        self.assertEqual((obj["name"], obj["wcid"], obj["stack"], obj["max_stack"], obj["container"], obj["value"]),
                         ("Mandrake", 775, 5, 50, PLAYER, 50))

    def test_account_from_character_list(self):
        self.assertEqual(decode("s2c", 4, CHARLIST), [{"type": "account", "account": "zorn"}])


class BusyClicks(unittest.TestCase):
    """Clicking again mid-cast: the new request is refused, the cast in progress resolves later."""

    def setUp(self):
        self.tracker = Tracker(SPELLS, clock=lambda: 0.0, components=COMPONENTS)
        self.conv = Conversation(Pipeline(self.tracker))
        self.conv.server(Msg(0xF746).u32(PLAYER))

    def cast(self):
        self.conv.client(Msg(0xF7B1).u32(1).u32(0x48).u32(528))

    def server(self, msg):
        self.conv.server(msg)

    def test_refusal_goes_to_the_newest_request_and_outcome_to_the_oldest(self):
        # observed: CAST, CAST, err 0x1D, done, [burn text], err 0x402, done
        self.cast()
        self.cast()
        self.server(Msg(0x1C).i32(0x1D))
        self.server(Msg(0xF7B0).u32(PLAYER).u32(1).u32(0x1C7).i32(0))
        self.server(Msg(0xF62C).string("The spell consumed the following components: Red Taper").i32(7))
        self.server(Msg(0x1C).i32(0x402))
        self.server(Msg(0xF7B0).u32(PLAYER).u32(2).u32(0x1C7).i32(0))
        newest, oldest = list(self.tracker.casts)
        self.assertEqual(newest["result"], "rejected: busy")
        self.assertEqual((oldest["result"], oldest["components"]), ("fizzled", ["Red Taper"]))
        spell = self.tracker.snapshot()["burn"]["spells"][0]
        self.assertEqual((spell["casts"], spell["fizzled"], spell["used"]), (1, 1, 1))  # the refusal isn't a cast
        self.assertAlmostEqual(spell["expected"], 4 * 0.075 + 2 * 0.0075, delta=0.01)

    def test_success_text_then_done(self):
        self.cast()
        self.server(Msg(0xF62C).string("You cast Acid Vulnerability Self II on yourself").i32(7))
        self.server(Msg(0xF7B0).u32(PLAYER).u32(1).u32(0x1C7).i32(0))
        self.cast()  # the next cast is unaffected by the previous UseDone
        self.server(Msg(0x1C).i32(0x401))
        self.server(Msg(0xF7B0).u32(PLAYER).u32(2).u32(0x1C7).i32(0))
        self.assertEqual([c["result"] for c in self.tracker.casts], ["rejected: not enough mana", "cast"])

    def test_casts_left_counts_carried_components(self):
        for guid, name, wcid, stack in ((1, "Iron Scarab", 690, 4), (2, "Red Taper", 1650, 1), (3, "Ginseng", 1, 9),
                                         (4, "Powdered Onyx", 2, 9), (5, "Gypsum", 3, 9), (6, "Cedar Talisman", 4, 5)):
            m = Msg(0xF745).u32(guid).u8(0x11).u8(0).u8(0).u8(0).u32(0).u32(0)
            for _ in range(9):
                m.u16(0)
            m.align().u32(0x1000 | 0x4000).string(name).u16(wcid).u16(0).u32(0x1000).u32(0).u16(stack).u32(PLAYER).align()
            self.conv.server(m, group=3)
        self.tracker.known_spells = {528: 1.0}
        status = self.tracker.snapshot()["spellbook"]["status"][528]
        self.assertEqual(status["limiting"], "Red Taper")  # 1 taper at 5% per cast, not the scarabs
        self.assertEqual(status["casts_left"], 20)


if __name__ == "__main__":
    unittest.main()


class CharacterSwitch(unittest.TestCase):
    """Observed on account 2: logoff to character select, then another character, same connection."""

    def test_switching_characters_resets_character_state(self):
        from tests.test_decoder import player_description
        tracker = Tracker({}, clock=lambda: 0.0)
        conv = Conversation(Pipeline(tracker))
        zornule, alt = 0x500000B0, 0x500000F5

        def enter(guid, name, skills):
            conv.server(Msg(0xF746).u32(guid))
            m = Msg(0xF745).u32(guid).u8(0x11).u8(0).u8(0).u8(0).u32(0).u32(0)
            for _ in range(9):
                m.u16(0)
            m.align().u32(0).string(name).u16(1).u16(0).u32(0x10).u32(0).align()
            conv.server(m, group=3)
            conv.server(player_description(name, 100, 50, skills))

        enter(zornule, "Zornule", {33: (500, 75, 10.0)})
        self.assertEqual((tracker.character, sorted(tracker.skills)), ("Zornule", [33]))
        for _ in range(3):  # the client sends logoff repeatedly
            conv.client(Msg(0xF653))
        snap = tracker.snapshot()
        self.assertEqual((snap["status"]["character"], snap["status"]["in_world"], snap["skills"]), (None, False, []))
        enter(alt, "Zorn Alt", {38: (200, 0, 900.0)})
        self.assertEqual((tracker.character, sorted(tracker.skills)), ("Zorn Alt", [38]))
        # entering again without a logoff in between (character switch) also starts fresh
        enter(zornule, "Zornule", {33: (500, 75, 10.0)})
        self.assertEqual((tracker.character, sorted(tracker.skills)), ("Zornule", [33]))


class Vitals(unittest.TestCase):
    def test_vital_and_attribute_updates(self):
        vital = Msg(0x29).u32(PLAYER).u32(4).u32(3).u32(0).u32(0).u32(7)
        self.assertEqual(decode("s2c", 4, vital.bytes()),
                         [{"type": "vital", "object": PLAYER, "key": 4, "raises": 3, "init": 0, "cp": 0, "current": 7}])
        attr = Msg(0x26).u32(PLAYER).u32(2).u32(55).u32(0).u32(110)
        self.assertEqual(decode("s2c", 4, attr.bytes()), [{"type": "attribute", "object": PLAYER, "key": 2, "value": 55, "cp": 110}])

    def test_login_attribute_cache(self):
        m = Msg(0x13).u32(0).i32(1).u32(0x1).u32(1).u32(0x1FF)
        for value in (100, 10, 10, 102, 101, 10):       # Str End Quick Coord Focus Self, as observed for Zornule
            m.u32(value).u32(0).u32(0)
        for current in (5, 10, 10):                      # Health Stamina Mana
            m.u32(0).u32(0).u32(0).u32(current)
        (pd,) = decode("s2c", 4, m.bytes())
        self.assertEqual(pd["attributes"], {1: 100, 2: 10, 3: 10, 4: 102, 5: 101, 6: 10})
        self.assertEqual({k: v["current"] for k, v in pd["vitals"].items()}, {2: 5, 4: 10, 6: 10})

    def test_tracker_vitals_and_max(self):
        tracker = Tracker({}, clock=lambda: 0.0)
        conv = Conversation(Pipeline(tracker))
        conv.server(Msg(0xF746).u32(PLAYER))
        m = Msg(0x13).u32(0).i32(1).u32(0x1).u32(1).u32(0x1FF)
        for value in (100, 11, 10, 102, 101, 40):
            m.u32(value).u32(0).u32(0)
        for raises, current in ((2, 4), (0, 11), (5, 45)):
            m.u32(raises).u32(0).u32(0).u32(current)
        conv.server(m)
        vitals = {v["name"]: (v["current"], v["max"]) for v in tracker.snapshot()["vitals"]}
        # Health = round(11/2)=6 (+2 raises) = 8; Stamina = 11; Mana = Self 40 + 5 raises = 45
        self.assertEqual(vitals, {"Health": (4, 8), "Stamina": (11, 11), "Mana": (45, 45)})
        conv.server(Msg(0x29).u32(PLAYER).u32(4).u32(0).u32(0).u32(0).u32(3))   # stamina drained
        conv.server(Msg(0x26).u32(PLAYER).u32(2).u32(20).u32(0).u32(0))         # endurance raised to 20
        vitals = {v["name"]: (v["current"], v["max"]) for v in tracker.snapshot()["vitals"]}
        self.assertEqual(vitals["Stamina"], (3, 20))
        self.assertEqual(vitals["Health"], (4, 12))


class VendorPurchase(unittest.TestCase):
    """Observed: a bought item's 0x22 'put in pack' (in 0xF7B0) arrives before its CreateObject,
    and the CreateObject carries no container."""

    def test_bought_item_counts_as_carried(self):
        tracker = Tracker({}, clock=lambda: 0.0)
        conv = Conversation(Pipeline(tracker))
        conv.server(Msg(0xF746).u32(PLAYER))
        bought = 0x90043A58
        conv.server(Msg(0xF7B0).u32(PLAYER).u32(5).u32(0x22).u32(bought).u32(PLAYER).u32(0).u32(0))
        m = Msg(0xF745).u32(bought).u8(0x11).u8(0).u8(0).u8(0).u32(0).u32(0)
        for _ in range(9):
            m.u16(0)
        m.align().u32(0x1000).string("Hyssop").u16(7).u16(0).u32(0x1000).u32(0).u16(10).align()  # stack, no container
        conv.server(m, group=3)
        self.assertEqual(tracker.inventory_counts()["Hyssop"], 10)
        # a later re-send of the same item without a container keeps it carried
        conv.server(m, group=3)
        self.assertEqual(tracker.inventory_counts()["Hyssop"], 10)
