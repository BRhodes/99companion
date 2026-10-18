import asyncio
import json
import os
import tempfile
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from companion import research
from companion.__main__ import Companion, spell_catalog
from companion.dat import DatFile
from companion.layouts import SLOTS_PER_PAGE, LayoutStore
from companion.messages import decode
from companion.pipeline import Pipeline
from companion.tracker import Tracker
from companion.web import WebServer
from tests.test_decoder import PLAYER, SPELLS
from tests.wire import Conversation, Msg

PORTAL = r"C:\Program Files (x86)\Microsoft Games\Asheron's Call\portal.dat"


def description_with_spells(spells):
    m = Msg(0x13).u32(0x10).i32(1)
    m.u16(1).u16(16).u32(1).string("Zorn")
    m.u32(0x2 | 0x100).u32(1)
    m.u16(0).u16(32)                      # no skills
    m.u16(len(spells)).u16(64)
    for sid, likelihood in spells.items():
        m.u32(sid)
        m.b += bytes(8)                   # charge, unknown
        m.b += __import__("struct").pack("<f", likelihood)
    return m


class SpellbookDecodeTest(unittest.TestCase):
    def test_login_spellbook_and_updates(self):
        tracker = Tracker(SPELLS, clock=lambda: 0.0)
        conv = Conversation(Pipeline(tracker))
        conv.server(Msg(0xF746).u32(PLAYER))
        conv.server(description_with_spells({1122: 1.0, 2: 0.5}))
        self.assertEqual(sorted(tracker.known_spells), [2, 1122])
        conv.server(Msg(0x4C).u32(6))
        conv.server(Msg(0xF7B0).u32(PLAYER).u32(1).u32(0x4D).u32(2))  # wrapped form works too
        self.assertEqual(tracker.snapshot()["spellbook"]["known"], [6, 1122])

    def test_spell_messages(self):
        self.assertEqual(decode("s2c", 4, Msg(0x4C).u32(77).bytes()), [{"type": "spell_learned", "spell": 77}])


class RoutesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dat = DatFile(PORTAL) if os.path.exists(PORTAL) else None
        spells = {1122: dict(SPELLS[1122], school_name="Life", icon=0x060013BC)}
        self.app = Companion(spells, None, {}, dat=dat, layouts=LayoutStore(Path(tmp.name) / "layouts.json"))
        self.dat = dat

    def test_the_shopping_list_goes_to_the_relay_with_the_backgrounds(self):
        sent = []
        self.app.writer = types.SimpleNamespace(is_closing=lambda: False, write=sent.append)
        self.app.sessions[1] = types.SimpleNamespace(info={"id": "s1"}, ended=None, tracker=types.SimpleNamespace(character="Zorn"))
        self.app.route("POST", "/api/sessions/s1/shoplist", b'{"amounts": {"Hyssop": 12}}')
        self.assertEqual(json.loads(sent[-1]), {"cmd": "shoplist", "session": "s1", "amounts": {"Hyssop": 12}, "buy": 0x2,
                                                "other": 0x1000})
        self.app.route("PUT", "/api/prefs/Zorn", json.dumps({"restock": {"tint_buy": 0x800, "tint_other": 0x10}}).encode())
        self.assertEqual(json.loads(sent[-1])["buy"], 0x800)                    # a new pick goes at once
        self.assertEqual(json.loads(sent[-1])["other"], 0x1000)                 # (creature isn't offered: the default)

    def test_a_route_from_a_starting_point_gets_an_answer(self):
        # (the routing module went missing from the imports once: every route request died without a response)
        view = types.SimpleNamespace(info={"id": "s1"}, ended=None, tracker=types.SimpleNamespace(position=None, level=10))
        self.app.sessions[1] = view
        body = json.dumps({"ns": 33.3, "ew": 56.7, "from": {"ns": 999, "ew": 999}}).encode()
        status, _, out = asyncio.run(self.app.route("POST", "/api/sessions/s1/route", body))
        self.assertEqual((status, json.loads(out)["error"]), (400, "that starting point is off the map"))

    def test_catalog(self):
        cat = spell_catalog({1122: dict(SPELLS[1122], school_name="Life", icon=0x060013BC)})
        self.assertEqual(cat[1122], {"name": "Blade Vulnerability Self II", "school": "Life", "power": 75,
                                     "mana": 30, "level": 2, "target": "self", "icon": "060013bc",
                                     "category": SPELLS[1122].get("category"),
                                     "beneficial": bool(SPELLS[1122].get("bitfield", 0) & 0x4),
                                 "target_type": research.target_type(SPELLS[1122])})

    def test_layout_round_trip_and_validation(self):
        status, _, body = self.app.route("GET", "/api/layout/Zorn", b"")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["pages"][0]["slots"]), SLOTS_PER_PAGE)
        layout = {"pages": [{"name": "Buffs", "slots": [1122, None, "junk", -5]},
                            {"name": "x" * 99, "slots": []}]}
        status, _, body = self.app.route("PUT", "/api/layout/Zorn", json.dumps(layout).encode())
        saved = json.loads(body)
        self.assertEqual(saved["pages"][0]["slots"][:4], [1122, None, None, None])
        self.assertEqual(len(saved["pages"][1]["name"]), 40)
        self.assertEqual(json.loads(self.app.route("GET", "/api/layout/Zorn", b"")[2]), saved)
        self.assertEqual(self.app.route("PUT", "/api/layout/Zorn", b"not json")[0], 400)

    def test_prefs_round_trip(self):
        empty = {"hidden_skills": [], "buff_profiles": [], "active_profile": None, "items": {"weapons": [], "shields": []},
                 "goals": [], "goal_tracked": None,
                 "restock": {"target_bu": 5000, "min": {"Scarabs": 10, "Herbs": 10, "Powders": 10, "Potions": 10,
                                                        "Talismans": 10, "Tapers": 20}, "since": None, "target_mode": "bu", "target_pct": 50,
                                                        "tint_buy": 0x2, "tint_other": 0x1000,
                             "override": {}, "adjust": {}, "exclude": []}}
        get = lambda who: json.loads(self.app.route("GET", f"/api/prefs/{who}", b"")[2])
        self.assertEqual(get("Zorn"), empty)
        status, _, body = self.app.route("PUT", "/api/prefs/Zorn", b'{"hidden_skills": [22, 24, 22, "x", -1]}')
        self.assertEqual((status, json.loads(body)), (200, dict(empty, hidden_skills=[22, 24])))
        self.assertEqual(get("Zorn")["hidden_skills"], [22, 24])
        self.assertEqual(get("Alt"), empty)
        # layouts and prefs share the file without clobbering each other
        self.app.route("PUT", "/api/layout/Zorn", b'{"pages": [{"name": "A", "slots": [1122]}]}')
        self.assertEqual(get("Zorn")["hidden_skills"], [22, 24])

    def test_buff_profiles(self):
        prefs = {"hidden_skills": [], "active_profile": "Hunt", "buff_profiles": [
            {"name": "Hunt", "buffs": [{"category": 1, "spell": 2, "name": "Strength Self I", "critical": 1},
                                       {"category": 1, "spell": 3, "name": "duplicate kind"},     # dropped
                                       {"category": "bad"}]},                                      # dropped
            {"name": "  ", "buffs": []}]}                                                         # no name: dropped
        saved = json.loads(self.app.route("PUT", "/api/prefs/Zorn", json.dumps(prefs).encode())[2])
        self.assertEqual(saved["buff_profiles"], [{"name": "Hunt", "max_chance": 0.3, "crit_memory": [], "bane_targets": ["armor"], "buffs": [
            {"category": 1, "spell": 2, "name": "Strength Self I", "section": "", "critical": True, "level": None}]}])
        prefs["buff_profiles"][0]["buffs"][0]["level"] = "max"
        prefs["buff_profiles"][0]["max_chance"] = 5
        saved = json.loads(self.app.route("PUT", "/api/prefs/Zorn", json.dumps(prefs).encode())[2])
        self.assertEqual((saved["buff_profiles"][0]["buffs"][0]["level"], saved["buff_profiles"][0]["max_chance"]), ("max", 0.99))
        # a crit mark remembered for a buff taken out (category 5); one that's kept again (1) drops out of it
        prefs["buff_profiles"][0]["crit_memory"] = [5, 1, "x"]
        saved = json.loads(self.app.route("PUT", "/api/prefs/Zorn", json.dumps(prefs).encode())[2])
        self.assertEqual(saved["buff_profiles"][0]["crit_memory"], [5])
        # the weapons and shields buffs go on
        prefs["items"] = {"weapons": [{"id": 7, "name": "Longbow"}, {"id": "x"}], "shields": [{"id": 9}]}
        saved = json.loads(self.app.route("PUT", "/api/prefs/Zorn", json.dumps(prefs).encode())[2])
        self.assertEqual(saved["items"], {"weapons": [{"id": 7, "name": "Longbow"}], "shields": [{"id": 9, "name": ""}]})
        self.assertEqual(saved["active_profile"], "Hunt")
        prefs["active_profile"] = "Gone"
        self.assertIsNone(json.loads(self.app.route("PUT", "/api/prefs/Zorn", json.dumps(prefs).encode())[2])["active_profile"])

    @unittest.skipUnless(os.path.exists(PORTAL), "needs the AC install's portal.dat")
    def test_icon(self):
        status, ctype, png = self.app.route("GET", "/icon/060013bc.png", b"")
        self.assertEqual((status, ctype, png[:8]), (200, "image/png", b"\x89PNG\r\n\x1a\n"))
        self.assertIsNone(self.app.route("GET", "/icon/0e00000e.png", b""))  # only textures

    def test_writes_need_the_companion_header(self):
        async def scenario():
            web = WebServer("127.0.0.1", 28199, lambda: {})
            web.router = self.app.route
            await web.start()
            loop = asyncio.get_running_loop()

            def put(headers):
                req = urllib.request.Request("http://127.0.0.1:28199/api/layout/Zorn", data=b'{"pages": []}',
                                             method="PUT", headers=headers)
                try:
                    return urllib.request.urlopen(req).status
                except urllib.error.HTTPError as e:
                    e.close()
                    return e.code

            without = await loop.run_in_executor(None, put, {"Content-Type": "application/json"})
            with_header = await loop.run_in_executor(None, put, {"Content-Type": "application/json", "X-Companion": "1"})
            web.close()
            return without, with_header

        self.assertEqual(asyncio.run(scenario()), (403, 200))

    def test_only_our_own_host_names_are_answered(self):
        async def scenario():
            web = WebServer("127.0.0.1", 28198, lambda: {"ok": 1})
            await web.start()
            loop = asyncio.get_running_loop()

            def get(host):
                req = urllib.request.Request("http://127.0.0.1:28198/api/state", headers={"Host": host})
                try:
                    return urllib.request.urlopen(req).status
                except urllib.error.HTTPError as e:
                    e.close()
                    return e.code

            got = [await loop.run_in_executor(None, get, h)
                   for h in ("127.0.0.1:28198", "localhost:28198", "evil.example:28198", "localhost.evil.example:28198")]
            web.close()
            return got

        self.assertEqual(asyncio.run(scenario()), [200, 200, 403, 403])


if __name__ == "__main__":
    unittest.main()
