"""The store-tint experiment: the components not on the shopping list get another item type (so background), in
place, resealed."""

import json
import struct
import tempfile
import types
import unittest
from pathlib import Path

from companion import framing, netcrc
from companion.storetint import StoreTint

STAND_IN = 0x80             # misc: 11D4
GEM = 0x800

KS = 0x1234ABCD
COMPS = {"Hyssop", "Moonstone", "Violet Taper"}


def ware(iid, name, item_type, icon):
    w = struct.pack("<II", 0xFFFFFF | 1 << 24, iid) + struct.pack("<IH", 0x7018, len(name)) + name
    w += b"\0" * (-(len(w) - 8) % 4)            # (the message so far is a multiple of 4: aligned the same)
    w += struct.pack("<HHII", 1, icon, item_type, 0) + struct.pack("<II", 25, 1) + struct.pack("<HHI", 1, 50, 0)
    return w


def stock(*wares):
    return struct.pack("<IIII", 0xF7B0, 1, 1, 0x62) + struct.pack("<IIiiiff", 7, 0, 0, 0, 0, 0.8, 1.5) \
        + struct.pack("<I", len(wares)) + b"".join(wares)


def packets(msg, cuts, counter=9):
    pieces = [msg[a:b] for a, b in zip([0] + cuts, cuts + [len(msg)])]
    out = []
    for i, p in enumerate(pieces):
        frag = struct.pack("<IIHHHH", counter, 0, len(pieces), 16 + len(p), i, 5) + p
        hdr = struct.pack("<IIIIHBB", 100 + i, 0x802, 11, 0, len(frag), 0, 0)
        out.append(netcrc.reseal_blob(hdr + frag, KS + i))
    return out


def kind(msg, at):
    """The item type of the ware whose flags are at `at`."""
    n = struct.unpack_from("<H", msg, at + 4)[0]
    pos = at + 6 + n
    pos += -pos % 4
    return struct.unpack_from("<I", msg, pos + 4)[0]


class StoreTintTest(unittest.TestCase):
    def setUp(self):
        self.wares = [ware(1, b"Hyssop", 0x1000, 0x11), ware(2, b"Book", 0x2000, 0x22),
                      ware(3, b"Moonstone", 0x1000, 0x33), ware(4, b"Violet Taper", 0x1000, 0x44)]
        self.msg = stock(*self.wares)
        self.at = [48 + 8 + sum(len(w) for w in self.wares[:i]) for i in range(len(self.wares))]   # each ware's flags

    def rebuild(self, pkts):
        ra = framing.Reassembler()
        return [m for p in pkts for _g, m in ra.feed(p)][0]

    def run_list(self, cuts, want=("Moonstone",), buy=None, other=STAND_IN):
        t = StoreTint(wanted=lambda: {"amounts": {n: 1 for n in want}, "buy": buy, "other": other}, components=COMPS)
        sent = [t.rewrite(p) for p in packets(self.msg, cuts)]
        return t, sent, self.rebuild(sent)

    def test_the_rest_of_the_components_get_the_stand_in(self):
        _t, sent, out = self.run_list([])
        self.assertEqual(len(out), len(self.msg))
        self.assertEqual([kind(out, a) for a in self.at], [STAND_IN, 0x2000, 0x1000, STAND_IN])   # Book's not a component
        diff = [i for i in range(len(out)) if out[i] != self.msg[i]]
        self.assertTrue(all(kind(self.msg, a) == 0x1000 for a in (self.at[0], self.at[3])))
        self.assertEqual(len(diff), 5)          # (two bytes of each type, Moonstone's max stack: 1) nothing else changed
        self.assertEqual(netcrc.keystream(sent[0]), KS)              # still checksums right

    def test_wherever_a_packet_ends(self):
        for cut in range(self.at[2], self.at[3] + 32, 4):            # through Moonstone into Violet Taper's type
            _t, sent, out = self.run_list([cut], want=("Hyssop",))
            self.assertEqual([kind(out, a) for a in self.at], [0x1000, 0x2000, STAND_IN, STAND_IN], cut)
            for i, p in enumerate(sent):
                self.assertEqual(netcrc.keystream(p), KS + i)

    def test_the_lists_own_background(self):
        _t, _sent, out = self.run_list([], buy=GEM, other=None)       # the rest left as they are
        self.assertEqual([kind(out, a) for a in self.at], [0x1000, 0x2000, GEM, 0x1000])
        _t, _sent, out = self.run_list([], buy=GEM, other=STAND_IN)
        self.assertEqual([kind(out, a) for a in self.at], [STAND_IN, 0x2000, GEM, STAND_IN])
        _t, _sent, out = self.run_list([], buy=0x10, other=0x200)     # creature, container: not offered, not sent
        self.assertEqual([kind(out, a) for a in self.at], [0x1000, 0x2000, 0x1000, 0x1000])

    def test_each_list_is_noted(self):
        notes = []
        want = {"amounts": {"Moonstone": 7}, "buy": GEM, "other": STAND_IN}
        t = StoreTint(wanted=lambda: want, components=COMPS, on_list=notes.append)
        for p in packets(self.msg, [self.at[2]]):
            t.rewrite(p)
        self.assertEqual(notes, [{"event": "tint", "vendor": 7, "on_list": 1, "backgrounds": 3, "max_stack": {"Moonstone": 7}}])

    def test_a_purchase_comes_off_the_list_before_the_server_answers(self):
        t = StoreTint(wanted=lambda: {"amounts": {"Moonstone": 3}}, components=COMPS)
        for p in packets(self.msg, []):
            t.rewrite(p)                                               # the stock list: item ids to names
        def buy(seq, *pairs):
            body = struct.pack("<IIIII", 0xF7B1, 4, 0x5F, 7, len(pairs)) + b"".join(struct.pack("<II", a, i) for a, i in pairs)
            frag = struct.pack("<IIHHHH", 3, 0, 1, 16 + len(body), 0, 5) + body
            return struct.pack("<IIIIHBB", seq, 0x800, 11, 0, len(frag), 0, 0) + frag
        self.assertEqual(t.bought(buy(50, (2, 3), (1, 2))), {"Moonstone": 2})   # Book (iid 2) isn't a component
        self.assertEqual(t.bought(buy(50, (2, 3))), {})                          # the same packet again
        self.assertEqual(t.bought(buy(51, (5, 4), (1, 1))), {"Violet Taper": 5, "Hyssop": 1})
        self.assertEqual(t.bought(packets(self.msg, [])[0]), {})

    def test_a_max_stack_of_what_is_short(self):
        def max_stack(msg, at):
            n = struct.unpack_from("<H", msg, at + 4)[0]
            pos = at + 6 + n + (-(at + 6 + n) % 4)
            return struct.unpack_from("<H", msg, pos + 22)[0]
        for cut in ([], [self.at[2] + 20]):                              # (its max stack in the next packet, too)
            t = StoreTint(wanted=lambda: {"amounts": {"Moonstone": 37, "Hyssop": 100000}}, components=COMPS)
            out = self.rebuild([t.rewrite(p) for p in packets(self.msg, cut)])
            self.assertEqual([max_stack(out, a) for a in self.at], [0xFFFF, 50, 37, 50])   # Book: not a component
        t = StoreTint(wanted=lambda: None, components=COMPS)               # no list: as it is
        self.assertEqual(self.rebuild([t.rewrite(p) for p in packets(self.msg, [])]), self.msg)

    def test_the_relay_takes_it_off_the_list(self):
        from companion.relay import Journal
        with tempfile.TemporaryDirectory() as d:
            j = Journal(Path(d), "x", cap_bytes=0)
            try:
                client = ("127.0.0.1", 5000)
                s = j.new_session(client)
                link = types.SimpleNamespace(client=client, info={"session": s})
                j.command(None, json.dumps({"cmd": "shoplist", "session": s.id, "amounts": {"Moonstone": 3, "Hyssop": 1},
                                            "other": STAND_IN}).encode())
                j.predict(link, {"Moonstone": 2, "Hyssop": 1})
                self.assertEqual(j.shoplist_for(client)["amounts"], {"Moonstone": 1})
                j.command(None, json.dumps({"cmd": "shoplist", "session": s.id, "amounts": {"Moonstone": 1}}).encode())
                self.assertEqual(j.shoplist_for(client)["amounts"], {"Moonstone": 1})   # the companion's own, from then on
            finally:
                j.close()

    def test_a_retransmission_gets_the_same(self):
        orig = packets(self.msg, [self.at[3] + 4])                   # Violet Taper's name in the second
        t = StoreTint(wanted=lambda: {"amounts": {"Moonstone": 1}, "other": STAND_IN}, components=COMPS)
        sent = [t.rewrite(p) for p in orig]
        self.assertNotEqual(sent[1], orig[1])
        self.assertEqual(t.rewrite(orig[1]), sent[1])

    def test_an_empty_list_changes_nothing(self):
        for want in (None, {"amounts": {}, "buy": GEM, "other": STAND_IN}):
            p = packets(self.msg, [])[0]
            self.assertIs(StoreTint(wanted=lambda: want, components=COMPS).rewrite(p), p)

    def test_the_relay_keeps_each_sessions_list(self):
        from companion.relay import Journal
        with tempfile.TemporaryDirectory() as d:
            j = Journal(Path(d), "x", cap_bytes=0)
            try:
                s = j.new_session(("127.0.0.1", 5000))
                self.assertIsNone(j.shoplist_for(("127.0.0.1", 5000)))
                j.command(None, json.dumps({"cmd": "shoplist", "session": s.id, "amounts": {"Moonstone": 7, "Bad": "x"},
                                            "buy": GEM, "other": 0x10}).encode())
                self.assertEqual(j.shoplist_for(("127.0.0.1", 5000)),
                                 {"amounts": {"Moonstone": 7}, "buy": GEM, "other": None})
                self.assertIsNone(j.shoplist_for(("127.0.0.1", 5001)))
            finally:
                j.close()

    def test_other_messages_pass_untouched(self):
        other = struct.pack("<IIII", 0xF7B0, 1, 1, 0xC9) + self.msg[16:]
        p = packets(other, [])[0]
        self.assertIs(StoreTint(wanted=lambda: {"amounts": {"Moonstone": 1}, "other": STAND_IN}, components=COMPS).rewrite(p), p)
        self.assertEqual(StoreTint(wanted=lambda: {"amounts": {"x": 1}}).rewrite(b"junk"), b"junk")


if __name__ == "__main__":
    unittest.main()
