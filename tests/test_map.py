"""Map coordinates, position decoding and routing."""

import struct
import unittest

from companion.messages import decode
from companion import walkmap
from companion.routing import Router
from companion.tracker import Tracker
from companion.worldmap import coord_text, coords
from tests.wire import Msg

PLAYER = 0x50000001


class CoordTest(unittest.TestCase):
    def test_known_places(self):
        self.assertEqual(coord_text(*coords(0xA9B4001F, 87.75, 147.72)), "42.7N, 33.6E")   # Holtburg start
        # Util.cpp GetCoordString: base = block*8 + (local-12)/24; coord = (base - 1024)/10 + 0.5
        ns, ew = coords(0xA7B30000, 117.6, 134.9)
        self.assertAlmostEqual(ew, (0xA7 * 8 + (117.6 - 12) / 24 - 1024) / 10 + 0.5)
        self.assertAlmostEqual(ns, (0xB3 * 8 + (134.9 - 12) / 24 - 1024) / 10 + 0.5)
        self.assertIsNone(coords(0xA9B4011B, 154.1, 133.3))                              # indoors


class PositionTest(unittest.TestCase):
    def position(self, cell, x, y, z, qw=1.0, qz=0.0):
        return struct.pack("<I3f4f", cell, x, y, z, qw, 0.0, 0.0, qz)

    def test_client_report_and_server_update(self):
        client = Msg(0xF7B1).u32(9).u32(0xF753)
        client.b += self.position(0xA9B4001F, 87.75, 147.72, 66.0)
        (ev,) = decode("c2s", 5, client.bytes())
        self.assertEqual((ev["type"], ev["source"], ev["cell"], round(ev["x"], 2)), ("position", "client", 0xA9B4001F, 87.75))
        server = Msg(0xF748).u32(PLAYER).u32(0x10 | 0x20).u32(0xC88C000B)    # qx and qy left out
        server.b += struct.pack("<3f2f", 44.2, 62.1, 22.0, -0.1, -0.995)
        server.u16(1).u16(2).u16(3).u16(4)
        (ev,) = decode("s2c", 4, server.bytes())
        self.assertEqual((ev["object"], ev["cell"], round(ev["qz"], 3)), (PLAYER, 0xC88C000B, -0.995))

    def test_tracker_keeps_only_the_players_position(self):
        tr = Tracker({}, clock=lambda: 0.0)
        tr.reset("x")
        tr.player = PLAYER
        tr.handle({"type": "position", "object": 0x50000999, "source": "server", "cell": 1, "x": 0, "y": 0, "z": 0,
                   "qw": 1, "qz": 0}, 1.0)
        self.assertIsNone(tr.position)
        tr.handle({"type": "position", "object": None, "source": "client", "cell": 0xA9B4001F, "x": 87.75,
                   "y": 147.72, "z": 66, "qw": 1, "qz": 0}, 2.0)
        snap = tr.snapshot()["position"]
        self.assertEqual((round(snap["ns"], 1), round(snap["ew"], 1), snap["indoors"]), (42.7, 33.6, False))
        self.assertEqual([round(v, 3) for v in snap["facing"]], [0.0, 1.0])            # unrotated: facing north


def pos(ns, ew):
    """An outdoor position at map coordinates (inverse of coords; cell index left 0)."""
    gx, gy = (ew + 101.95) * 240, (ns + 101.95) * 240
    bx, by = int(gx // 192), int(gy // 192)
    return {"cell": (bx << 24) | (by << 16) | 1, "x": gx - bx * 192, "y": gy - by * 192}


class RouteTest(unittest.TestCase):
    PLACES = {"portals": [
        {"name": "Portal to Far Town", "at": dict(pos(0, 1), ns=0, ew=1), "to": dict(pos(40, 40), ns=40, ew=40),
         "min_level": None, "max_level": None, "quest": False},
        {"name": "High Portal", "at": dict(pos(0, 0.5), ns=0, ew=0.5), "to": dict(pos(40, 40), ns=40, ew=40),
         "min_level": 50, "max_level": None, "quest": False},
    ], "lifestones": [], "towns": []}

    def test_takes_the_portal_when_faster(self):
        r = Router(self.PLACES).route(pos(0, 0), (40, 41), level=10)
        self.assertEqual([s["kind"] for s in r["steps"]], ["run", "portal", "run"])
        self.assertEqual(r["steps"][1]["name"], "Portal to Far Town")          # the closer one is level 50+
        self.assertLess(r["seconds"], 100)

    def test_runs_when_close(self):
        r = Router(self.PLACES).route(pos(0, 0), (0.5, 0.5), level=10)
        self.assertEqual([s["kind"] for s in r["steps"]], ["run"])

    def test_level_gate(self):
        r = Router(self.PLACES).route(pos(0, 0), (40, 41), level=60)
        self.assertEqual(r["steps"][1]["name"], "High Portal")

    def test_skipped_portal_is_left_out(self):
        router = Router(self.PLACES)
        far = next(p for p in router.places["portals"] if p["name"] == "Portal to Far Town")
        r = router.route(pos(0, 0), (40, 41), level=10, skip=[far["id"]])
        self.assertNotIn("Portal to Far Town", [s.get("name") for s in r["steps"]])
        self.assertEqual([s["kind"] for s in r["steps"]], ["run"])            # High Portal is level 50+: run it all


class SlowGround:
    """A walk map where crossing the band NS 10..20 on foot takes three times the distance."""
    def __init__(self):
        self.walked = []

    def walk(self, a, b):
        self.walked.append((a, b))
        d = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
        crosses = min(a[0], b[0]) < 20 and max(a[0], b[0]) > 10
        return {"distance": d * (3 if crosses else 1), "points": [a, b]}


class WalkRouteTest(unittest.TestCase):
    PLACES = {"portals": [
        {"name": "Over the Mountains", "at": dict(pos(0, 30), ns=0, ew=30), "to": dict(pos(30, 0), ns=30, ew=0),
         "min_level": None, "max_level": None, "quest": False},
    ], "lifestones": [], "towns": []}

    def test_a_long_walk_makes_the_portal_worth_it(self):
        dest = (30, 1)
        straight = Router(self.PLACES).route(pos(0, 0), dest)
        self.assertEqual([s["kind"] for s in straight["steps"]], ["run"])     # 30 units straight beats 30 + 1 via the portal
        ground = SlowGround()
        walked = Router(self.PLACES, walk=ground).route(pos(0, 0), dest)
        self.assertEqual([s["kind"] for s in walked["steps"]], ["run", "portal", "run"])
        self.assertTrue(walked["terrain"])
        self.assertGreater(walked["walked"], 0)
        self.assertLess(walked["walked"], 10)                                   # only the runs the search reached

    def test_unwalkable_leg_is_dropped(self):
        class NoWay(SlowGround):
            def walk(self, a, b):
                return None if abs(b[0] - 30) < 0.01 and abs(b[1] - 1) < 0.01 and abs(a[0]) < 0.01 else super().walk(a, b)
        r = Router(self.PLACES, walk=NoWay()).route(pos(0, 0), (30, 1))
        self.assertEqual([s["kind"] for s in r["steps"]], ["run", "portal", "run"])


class WalkMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        n = walkmap.CELLS
        grid = bytearray(b"" * (n * n))
        for y in range(900, 1200):                     # a wall at x = 1000, open only at its south end
            grid[y * n + 1000] = 0
        cls.w = walkmap.WalkMap(bytes(grid), [1] * (n * n))

    def test_straight_when_clear(self):
        a, b = walkmap.WalkMap.centre(950, 800), walkmap.WalkMap.centre(1050, 800)
        r = self.w.walk(a, b)
        self.assertEqual(len(r["points"]), 2)
        self.assertAlmostEqual(r["distance"], 10.0, places=3)

    def test_goes_around_a_wall(self):
        a, b = walkmap.WalkMap.centre(990, 1000), walkmap.WalkMap.centre(1010, 1000)
        r = self.w.walk(a, b)
        self.assertGreater(len(r["points"]), 2)
        self.assertGreater(r["distance"], 20.0)        # down to y < 900 and back up, rather than 2 units across
        for p, q in zip(r["points"], r["points"][1:]):  # every stretch stays on open ground
            self.assertTrue(self.w.clear(walkmap.WalkMap.cell(*p), walkmap.WalkMap.cell(*q)))

    def test_split_matches_the_server(self):
        # FSplitNESW: the top bit of the 32-bit hash picks the diagonal
        v = lambda x, y: (x * y * 0x0CCAC033 - x * 0x421BE3BD + y * 0x6C1AC587 - 0x519B8F25) & 0xFFFFFFFF
        for x, y in ((0, 0), (5, 7), (1000, 1234)):
            self.assertEqual(walkmap.split_ne(x, y), v(x, y) >= 0x80000000)


class RouteBudgetTest(unittest.TestCase):
    def test_a_passed_deadline_leaves_runs_straight(self):
        import time
        from companion import routing
        calls = []

        class Walk:
            sea = False

            def walk(self, a, b):
                calls.append((a, b))
                return {"distance": 2 * routing.distance(a, b), "points": [list(a), list(b)]}

            def crosses_blocked(self, a, b):
                return self.sea
        r = routing.Router(places={"portals": []}, walk=Walk())
        start = routing.outdoor_position(10.0, 10.0)
        walked = r.route(start, (12.0, 10.0))
        self.assertTrue(calls and walked["steps"][0].get("points"))
        calls.clear()
        late = r.route(start, (12.0, 10.0), deadline=time.monotonic() - 1)
        self.assertEqual(calls, [])                                      # out of time: nothing walked
        self.assertTrue(late["cut_short"])
        self.assertAlmostEqual(late["steps"][0]["distance"], 2.0, places=3)   # its straight line
        flat = r.route(start, (12.0, 10.0), walk=False)
        self.assertFalse(flat["terrain"])
        r.walk.sea = True                                                # a straight line over open sea: no run
        self.assertIsNone(r.route(start, (12.0, 10.0), walk=False))

    def test_the_sea_check_on_the_real_grid(self):
        from companion import walkmap
        grid = bytearray(b"" * walkmap.CELLS * walkmap.CELLS)
        for y in range(800, 816):                                        # two landblocks of sea, east-west
            for x in range(0, walkmap.CELLS):
                grid[y * walkmap.CELLS + x] = 0
        w = walkmap.WalkMap(bytes(grid), None)
        y0 = 800 * walkmap.UNIT - 101.95                                 # its southern edge, in map units
        self.assertTrue(w.crosses_blocked((y0 - 5, 0.0), (y0 + 5, 0.0)))  # straight across it
        self.assertFalse(w.crosses_blocked((y0 - 5, 0.0), (y0 - 2, 10.0)))  # along the shore


class StartPointTest(unittest.TestCase):
    def test_a_map_coordinate_as_an_outdoor_start(self):
        from companion import routing
        for ns, ew in [(31.0, -14.0), (33.3, 56.7), (-54.8, 73.8)]:
            p = routing.outdoor_position(ns, ew)
            self.assertLess(p["cell"] & 0xFFFF, 0x100)                  # outdoors
            got = routing.coords(p["cell"], p["x"], p["y"])
            self.assertAlmostEqual(got[0], ns, places=6)
            self.assertAlmostEqual(got[1], ew, places=6)
        self.assertIsNone(routing.outdoor_position(150.0, 0.0))         # off the map


class NearestTest(unittest.TestCase):
    def test_an_unreachable_spot_routes_to_the_nearest_you_can_reach(self):
        from array import array
        from companion import routing, walkmap
        n = walkmap.CELLS
        grid = bytearray(b"" * n * n)
        for y in range(1000, 1040):                                      # a strait of sea, east-west
            for x in range(n):
                grid[y * n + x] = 0
        w = walkmap.WalkMap(bytes(grid), walkmap.label(bytes(grid)))
        south = walkmap.WalkMap.centre(500, 990)                          # the shore you're on
        north = walkmap.WalkMap.centre(500, 1060)                         # across the water
        r = routing.Router(places={"portals": []}, walk=w)
        start = routing.outdoor_position(*south)
        self.assertIsNone(r.route(start, north))
        got = r.route_nearest(start, north)
        self.assertIsNotNone(got)
        end = got["partial"]
        self.assertAlmostEqual(end["ns"], walkmap.WalkMap.centre(500, 999)[0], places=6)   # the strait's near shore
        self.assertGreater(end["short"], 5.0)


if __name__ == "__main__":
    unittest.main()
