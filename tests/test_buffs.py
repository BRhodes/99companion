"""Enchantments on the player: decoding, expiry times, stacking and the spell to recast."""

import struct
import unittest

from companion.messages import decode
from companion.tracker import Tracker
from tests.wire import Msg

PLAYER = 0x500000C9
SPELLS = {
    213: {"name": "Mana Renewal Self II", "school": 2, "school_name": "Life", "category": 14, "power": 50, "icon": 0x06001F55},
    214: {"name": "Mana Renewal Self III", "school": 2, "school_name": "Life", "category": 14, "power": 100, "icon": 0x06001F55},
    218: {"name": "Mana Renewal Other I", "school": 2, "school_name": "Life", "category": 14, "power": 150, "icon": 0x06001F55},
    527: {"name": "Acid Vulnerability Self I", "school": 2, "school_name": "Life", "category": 96, "power": 25},
}


def enchantment(m, spell, power, start, duration, caster=PLAYER, layer=0, category=14):
    """Append one Enchantment::Pack (60 bytes)."""
    m.u32(spell | layer << 16).u32(category).i32(power).f64(start).f64(duration).u32(caster)
    m.b += struct.pack("<ff", 0.0, -666.0)
    m.f64(-1000.0).u32(0x5008).u32(4).b += struct.pack("<f", 1.25)
    return m


def event(op):
    return Msg(0xF7B0).u32(PLAYER).u32(1).u32(op)


def feed(tracker, msg, now):
    for ev in decode("s2c", 4, msg.bytes()):
        tracker.handle(ev, now)


class BuffTest(unittest.TestCase):
    def tracker(self, known=(213, 214)):
        tr = Tracker(SPELLS, clock=lambda: 1000.0)
        tr.reset("x")
        tr.player = PLAYER
        tr.known_spells = {s: 1.0 for s in known}
        return tr

    def test_decode_matches_live_bytes(self):
        live = bytes.fromhex("b0f70000c9000050080000004e0000000f020000600000001900000000000000000000000000000000707740"
                             "c900005000000000008026c43c5c353fdb7dc9c10850000045000000cdcc8c3f")
        (ev,) = decode("s2c", 4, live)
        e = ev["list"][0]
        self.assertEqual((e["spell"], e["layer"], e["category"], e["power"], e["start"], e["duration"]),
                         (527, 0, 96, 25, 0.0, 375.0))
        self.assertEqual((e["stat"]["key"], round(e["stat"]["value"], 2)), (69, 1.1))
        (ev,) = decode("s2c", 4, bytes.fromhex("b0f70000c900005007000000a601000001000000ae010000"))
        self.assertEqual((ev["type"], ev["ids"], ev["why"]), ("enchantments_removed", [0x1AE], "expired"))

    def test_expiry_and_removal(self):
        tr = self.tracker()
        feed(tr, enchantment(event(0x4E), 214, 100, start=-50.0, duration=450.0), now=1000.0)
        (b,) = tr.snapshot()["buffs"]
        self.assertEqual((b["name"], b["expires"], b["active"], b["self_cast"]), ("Mana Renewal Self III", 1400.0, True, True))
        self.assertEqual((b["recast"], b["recast_weaker"]), (214, False))
        feed(tr, event(0x1A6).u32(1).u32(214), now=1400.0)
        self.assertEqual(tr.snapshot()["buffs"], [])

    def test_stacking_and_recast_choice(self):
        tr = self.tracker(known=(213,))
        m = enchantment(event(0x1A5).u32(2), 213, 50, start=0.0, duration=300.0)
        enchantment(m, 218, 150, start=0.0, duration=900.0, caster=0x50000999, layer=1)
        feed(tr, m, now=1000.0)
        buffs = {b["spell_id"]: b for b in tr.snapshot()["buffs"]}
        self.assertFalse(buffs[213]["active"])                 # the stronger Other I wins the category
        self.assertTrue(buffs[218]["active"])
        self.assertFalse(buffs[218]["self_cast"])
        self.assertEqual((buffs[218]["recast"], buffs[218]["recast_weaker"]), (213, True))  # only II is known
        self.assertEqual([b["spell_id"] for b in tr.snapshot()["buffs"]], [213, 218])     # soonest first

    def test_login_registry_replaces_state(self):
        tr = self.tracker()
        feed(tr, enchantment(event(0x4E), 527, 25, start=0.0, duration=375.0, category=96), now=1000.0)
        desc = Msg(0x13).u32(0).i32(1).u32(0x200).u32(0)       # no base tables; ac flags: registry only
        desc.u32(0x2 | 0x4).u32(1)                               # additive list of one, plus vitae
        enchantment(desc, 214, 100, start=-10.0, duration=450.0)
        enchantment(desc, 666, 0, start=0.0, duration=-1.0, category=204)
        feed(tr, desc, now=1000.0)
        buffs = {b["spell_id"]: b for b in tr.snapshot()["buffs"]}
        self.assertEqual(set(buffs), {214, 666})
        self.assertIsNone(buffs[666]["expires"])                 # permanent (vitae) sorts last
        self.assertEqual(tr.snapshot()["buffs"][-1]["spell_id"], 666)

    def test_character_change_forgets_buffs(self):
        tr = self.tracker()
        feed(tr, enchantment(event(0x4E), 214, 100, start=0.0, duration=450.0), now=1000.0)
        tr.reset_character()
        self.assertEqual(tr.snapshot()["buffs"], [])


if __name__ == "__main__":
    unittest.main()
