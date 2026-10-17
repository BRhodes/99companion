import unittest

from companion.framing import Reassembler
from companion.messages import decode
from companion.pipeline import Pipeline
from companion.tracker import Tracker
from tests.wire import Conversation, Msg, c2s, fragments, s2c

PLAYER = 0x50000001
LIFE, MANA_CONV, ALCHEMY = 33, 16, 38
SPELLS = {1122: {"name": "Blade Vulnerability Self II", "school": 2, "power": 75, "mana": 30}}


def player_description(name, total, unassigned, skills):
    m = Msg(0x13).u32(0x1 | 0x10).i32(1)
    m.u16(2).u16(16).u32(0x15).i32(total).u32(0x16).i32(unassigned)
    m.u16(1).u16(16).u32(1).string(name)
    m.u32(0x2).u32(1)
    m.u16(len(skills)).u16(32)
    for sid, (pp, resistance, since) in skills.items():
        m.u32(sid).skill(10, 2, pp, 50, resistance, since)
    return m


def skill_update(sid, pp, resistance, since):
    return Msg(0x28).u32(PLAYER).u32(sid).skill(11, 2, pp, 50, resistance, since)


def xp(key, value):
    return Msg(0x0E).u32(PLAYER).u32(key).i32(value)


def create_object(guid, name, wcid, stack, container):
    m = Msg(0xF745).u32(guid)
    m.u8(0x11).u8(0).u8(0).u8(0)                       # ObjDesc, no parts
    m.u32(0).u32(0)                                    # PhysicsDesc bitfield + state
    for _ in range(9):
        m.u16(0)
    m.align()
    m.u32(0x1000 | 0x4000).string(name).u16(wcid).u16(0).u32(0x80).u32(0)   # presence flags lead
    m.u16(stack).u32(container).align()
    return m


class Session(unittest.TestCase):
    def setUp(self):
        self.clock = {"t": 0.0}
        self.tracker = Tracker(SPELLS, clock=lambda: self.clock["t"])
        self.pipe = Pipeline(self.tracker)
        self.conv = Conversation(self.pipe)
        self.conv.server(Msg(0xF746).u32(PLAYER))
        self.conv.server(player_description("Zorn", 11580, 2943, {
            LIFE: (1000, 50, 300.0), MANA_CONV: (500, 63, 30.0), ALCHEMY: (200, 0, 5000.0)}))
        self.clock["t"] = self.conv.t

    def grant(self, sid, new_pp, old_resistance, since, steps):
        self.conv.server(skill_update(sid, new_pp, old_resistance, since))
        for step in steps:
            unassigned = self.tracker.xp_unassigned + step
            self.conv.server(xp(0x16, unassigned))
            self.conv.server(xp(0x15, self.tracker.xp_total + step))

    def finish(self):
        self.clock["t"] = self.conv.t + 5
        self.tracker.tick()

    def test_login_state(self):
        snap = self.tracker.snapshot()
        self.assertEqual(snap["status"]["character"], "Zorn")
        self.assertEqual((snap["xp"]["total"], snap["xp"]["unassigned"]), (11580, 2943))
        life = next(s for s in snap["skills"] if s["id"] == LIFE)
        self.assertEqual(life["stored_difficulty"], 50)
        self.assertAlmostEqual(self.clock["t"] - life["last_grant_ts"], 300.0, delta=1)

    def test_cast_grants_life_and_mana_conversion(self):
        # The screenshot case: Blade Vulnerability Self II (power 75) -> Life 86 + MC 95, 196 unassigned.
        self.conv.client(Msg(0xF7B1).u32(7).u32(0x48).u32(1122))
        self.grant(LIFE, 1086, 50, 300.0, [86, 7])
        self.grant(MANA_CONV, 595, 63, 30.0, [95, 8])
        self.finish()
        snap = self.tracker.snapshot()
        grants = {g["skill_id"]: g for g in snap["grants"]}
        self.assertEqual(grants[LIFE]["difficulty"], 75)
        self.assertEqual(grants[MANA_CONV]["difficulty"], 88)
        self.assertEqual(snap["xp"]["unassigned"], 2943 + 196)
        self.assertEqual(snap["xp"]["grant_gain"], 196)
        life = next(s for s in snap["skills"] if s["id"] == LIFE)
        self.assertEqual((life["grants"], life["skill_xp"], life["stored_difficulty"]), (1, 86, 75))
        self.assertEqual(life["last_grant_ts"], grants[LIFE]["ts"])

    def test_next_skill_record_confirms_difficulty(self):
        self.grant(LIFE, 1086, 50, 300.0, [86, 7])  # no cause: only the 70-79 range is known
        self.finish()
        self.assertIsNone(self.tracker.grants[0]["difficulty"])
        self.conv.server(skill_update(LIFE, 1186, 75, 61.0))  # next record reports the stored 75
        self.assertEqual(self.tracker.grants[-1]["difficulty"], 75)

    def test_spending_xp_is_not_a_grant(self):
        self.conv.server(skill_update(LIFE, 1400, 50, 300.0))
        self.conv.server(xp(0x16, 2943 - 400))
        self.finish()
        self.assertEqual(len(self.tracker.grants), 0)
        self.assertEqual(self.tracker.skills[LIFE]["pp"], 1400)

    def test_craft_uses_recipe_difficulty(self):
        self.conv.server(create_object(0x70001, "Mana Infusion", 5326, 3, PLAYER), group=3)
        self.conv.server(create_object(0x70002, "Aqua Incanta", 4748, 5, PLAYER), group=3)
        self.conv.client(Msg(0xF7B1).u32(8).u32(0x35).u32(0x70001).u32(0x70002))
        self.grant(ALCHEMY, 290, 0, 5000.0, [90, 9])
        self.finish()
        g = self.tracker.grants[0]
        self.assertEqual((g["skill"], g["difficulty"], g["cause"]), ("Alchemy", 90, "Mana Infusion on Aqua Incanta"))
        self.assertEqual(self.tracker.inventory_counts()["Aqua Incanta"], 5)
        self.conv.server(Msg(0x197).u32(0x70002).u32(4).u32(20))
        self.assertEqual(self.tracker.inventory_counts()["Aqua Incanta"], 4)

    def test_components_and_fizzle(self):
        self.conv.client(Msg(0xF7B1).u32(9).u32(0x48).u32(1122))
        self.conv.server(Msg(0xF62C).string("The spell consumed the following components: Scarab, Hyssop, Hyssop").i32(7))
        self.conv.server(Msg(0x1C).i32(0x402))
        snap = self.tracker.snapshot()
        used = {c["name"]: c["used"] for c in snap["burn"]["components"] if c["used"]}
        self.assertEqual(used, {"Hyssop": 2, "Scarab": 1})
        self.assertEqual(snap["casts"][0]["result"], "fizzled")
        self.assertEqual(snap["casts"][0]["components"], ["Scarab", "Hyssop", "Hyssop"])


class Framing(unittest.TestCase):
    def test_multi_fragment_and_retransmit(self):
        text = "x" * 1000
        msg = Msg(0xF62C).string(text).i32(0).bytes()
        frags = fragments(5, 4, msg)
        self.assertGreater(len(frags), 2)
        r = Reassembler()
        out = []
        for i, f in reversed(list(enumerate(frags))):  # out of order
            out += r.feed(s2c(10 + i, f))
        out += r.feed(s2c(10, frags[0]))  # duplicate sequence is ignored
        self.assertEqual(len(out), 1)
        self.assertEqual(decode("s2c", *out[0])[0]["text"], text)

    def test_client_prefix_not_in_size(self):
        msg = Msg(0xF7B1).u32(1).u32(0x4A).u32(0x1234).u32(1122).bytes()
        out = Reassembler().feed(c2s(2, *fragments(1, 5, msg)))
        self.assertEqual(decode("c2s", *out[0]), [{"type": "cast", "spell": 1122, "target": 0x1234, "aseq": 1}])

    def test_wrapped_event(self):
        msg = Msg(0xF7B0).u32(PLAYER).u32(3).u32(0x1C7).i32(0).bytes()
        self.assertEqual(decode("s2c", 4, msg), [{"type": "use_done", "code": 0}])


if __name__ == "__main__":
    unittest.main()
