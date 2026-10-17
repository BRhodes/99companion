"""Picking out your shopping list in a vendor's window (an experiment; relay --tint-store): while the list has
anything on it, the spell components on it and the rest each show on the background you've picked for them (the
Order tab). The dashboard sends the list to the relay (through the companion) as it works it out, with the picks.
(Showing each one to buy as a stack of what's short was tried: the client still buys one at a time.)

A purchase is seen on its way to the server (bought(): the client's [vendor][(amount, item)...]), so the list can be
cut by it before the server answers -- the stock list it sends straight after then already shows what's left to buy.
The dashboard's next list (once the companion has seen what arrived) replaces the guess.

The client draws an item's icon on a background it picks by the item's type -- the lowest bit set, through a table
(client.exe 0x657660): spell components (0x1000) 11CD, misc (0x80) 11D4, gem (0x800) 11D3, missile weapon (0x100)
11D2... A vendor's stock (0xF7B0 event 0x62) lists each ware as an OldPublicWeenieDesc: [flags][name][type id u16]
[icon u16][item type u32]..., so the item type is swapped in place: nothing changes length, every fragment and packet
keeps its size, and only the touched packets' checksums are worked out again (netcrc: the keystream word is
recovered from the original). The name comes before the item type, so it's known by the time the type arrives:
every component gets it right, wherever a packet ends. Buying sends only the vendor, the amount and the item; what
you buy arrives as itself.

A stock list comes in many fragments, one a packet, in order; each is forwarded as it comes. Anything unexpected
-- a fragment missing, a ware that doesn't parse -- and the rest of that message is left alone.
"""

import json
import struct
from pathlib import Path

from . import framing, netcrc
from .binreader import Reader, ReadError
from .messages import _PWD_FIELDS

OP_EVENT = 0xF7B0
OP_ACTION, ACT_BUY = 0xF7B1, 0x5F   # client: [seq][0x5F][vendor][count][(amount, item)...]
EV_VENDOR_INFO = 0x62
COMPONENT = 0x1000          # spell components' own item type (background 11CD)
# item types you can pick, and the background the client draws them on (its table; creature and container left out:
# the client may treat those differently)
BACKGROUNDS = {0x1000: 0x11CD, 0x1: 0x11CB, 0x2: 0x11CF, 0x4: 0x11F3, 0x8: 0x11D5, 0x20: 0x11CC, 0x40: 0x11F4,
               0x80: 0x11D4, 0x100: 0x11D2, 0x400: 0x11D0, 0x800: 0x11D3}
DEFAULT_OTHER = 0x80        # misc: 11D4
STOCK_START = 16 + 28        # event header, then the vendor's id, buy types, min / max value, magic, two prices
SEQ_MEMORY = 512

try:                         # every spell component's name (tools/build_component_icons.py)
    COMPONENTS = frozenset(json.loads((Path(__file__).resolve().parent / "data" / "component_icons.json").read_text()))
except (OSError, ValueError):
    COMPONENTS = frozenset()


def _strict_align(r):
    pad = -r.pos % 4
    if r.pos + pad > r.end:
        raise ReadError("partial")
    r.pos += pad


def _walk(buf, start):
    """A stock entry from `start`, as far as the buffer holds it: ({field: (offset, size, value)}, where it ends or
    None while it's partial). Fields: "iid", "name", "type", "max_stack" -- each there once its bytes are all in."""
    r, got = Reader(buf, start), {}
    try:
        packed, iid = r.u32(), r.u32()
        got["iid"] = (start + 4, 4, iid)
        if not packed >> 24:
            return got, r.pos
        bits = r.u32()
        n = r.u16()
        got["name"] = (r.pos, n, r.bytes(n).split(b"\0", 1)[0].decode("latin-1"))
        _strict_align(r)
        r.skip(4)                   # type id, icon
        got["type"] = (r.pos, 4, r.u32())
        r.skip(4)                   # description flags
        for bit, _field, size in _PWD_FIELDS:
            if not bits & bit:
                continue
            if bit == 0x2000:           # max stack
                got["max_stack"] = (r.pos, 2, r.u16())
            elif size == "str":
                r.skip(r.u16())
                _strict_align(r)
            else:
                r.skip(4 if size == "f32" else size)
        _strict_align(r)
    except ReadError:
        return got, None
    return got, r.pos


class _Message:
    def __init__(self, count):
        self.count, self.next = count, 0
        self.buf = bytearray()
        self.pos = STOCK_START + 4      # where the next unread entry starts
        self.left = None                # entries still to read
        self.handled = set()            # the fields of the entry at self.pos already decided (it ends later)
        self.changed = {"backgrounds": 0}


class StoreTint:
    """One per client link, fed every server datagram in order: rewrite(datagram) -> the datagram to send.
    wanted: () -> {"amounts": the shopping list {name: how many}, "buy": item type for those, "other": for the other
    components (a type None or COMPONENT: left as it is)}, or None.
    Nothing on the list: nothing's changed."""

    def __init__(self, wanted, components=COMPONENTS, on_list=None):
        self.wanted, self.components = wanted, components
        self.on_list = on_list   # ({event: "tint", ...}) when a stock list has gone through: what was changed in it
        self.messages = {}       # (channel, counter) -> _Message, while its fragments arrive
        self.done = {}           # (counter, index) -> [(offset in the fragment's body, new bytes)]: retransmits
        self.ware_names = {}     # item id -> name, from the stock lists seen (what a purchase names)
        self.buys_seen = []      # the client packets' sequence numbers a purchase came in (a resend's not counted twice)
        self.swapped = 0

    def rewrite(self, data):
        try:
            return self._rewrite(data)
        except Exception:        # never let the experiment cost the client a packet
            return data

    def _rewrite(self, data):
        pkt = framing.parse_header(data)
        if pkt is None or not pkt.flags & framing.FLAG_APP_DATA:
            return data
        edits = []               # (offset in the datagram, new bytes)
        pos = len(data) - pkt.size
        for counter, channel, count, index, _group, body in framing.iter_fragments(data, pkt):
            body_at = pos + framing.FRAG.size
            pos += framing.FRAG.size + len(body)
            if (counter, index) in self.done:
                edits += [(body_at + off, b) for off, b in self.done[(counter, index)]]
                continue
            found = self._fragment(channel, counter, count, index, body)
            if found:
                self.done[(counter, index)] = found
                while len(self.done) > SEQ_MEMORY:
                    self.done.pop(next(iter(self.done)))
                edits += [(body_at + off, b) for off, b in found]
        if not edits:
            return data
        ks = netcrc.keystream(data)
        out = bytearray(data)
        for at, b in edits:
            out[at:at + len(b)] = b
        return netcrc.reseal_blob(bytes(out), ks)

    def _fragment(self, channel, counter, count, index, body):
        """The edits for one fragment of a stock list ([(offset in body, bytes)]), and the list's state kept."""
        key = (channel, counter)
        if index == 0:
            if len(body) < 16 or struct.unpack_from("<II", body, 0)[0] != OP_EVENT \
                    or struct.unpack_from("<I", body, 12)[0] != EV_VENDOR_INFO:
                return None
            self.messages[key] = _Message(count)
        m = self.messages.get(key)
        if m is None or index < m.next:
            return None             # not a stock list, or a retransmission of one that had nothing to change
        if index > m.next:          # one's missing: leave the rest alone
            self.messages.pop(key, None)
            return None
        m.next += 1
        base = len(m.buf)
        m.buf += body
        edits = self._scan(m, base)
        if m.next == m.count:
            self.messages.pop(key, None)
            if self.on_list:
                want = self.wanted() or {}
                self.on_list(dict({"event": "tint", "vendor": struct.unpack_from("<I", m.buf, 16)[0],
                                   "on_list": len(want.get("amounts") or {})},
                                  **m.changed))
        return edits

    def _scan(self, m, base):
        """Read the entries the buffer now holds, deciding each field as soon as it's all in (in [base, end) then:
        the name, before them all, is in by then)."""
        edits = []
        if m.left is None:
            if len(m.buf) < STOCK_START + 4:
                return edits
            m.left = struct.unpack_from("<I", m.buf, STOCK_START)[0]
        want = self.wanted() or {}
        amounts = want.get("amounts") or {}
        buf = bytes(m.buf)
        while m.left:
            fields, end = _walk(buf, m.pos)
            name = fields.get("name", (0, 0, None))[2]
            for key, (at, size, value) in fields.items():
                if key == "name" and fields.get("iid"):
                    self.ware_names[fields["iid"][2]] = value
                if key in ("name", "iid") or key in m.handled:
                    continue
                m.handled.add(key)
                if key == "max_stack":
                    # (an experiment) one on the list shows a max stack of what it's short; the server keeps its
                    # own: a purchase's stacks are made from that
                    n = amounts.get(name) if name in self.components else None
                    new = min(n, 0xFFFF) if n and n > 0 else None
                else:
                    new = self._decide(name, amounts, want) if amounts else None
                if new is not None and new != value and at >= base:
                    edits.append((at - base, new.to_bytes(size, "little")))
                    if key == "max_stack":
                        m.changed.setdefault("max_stack", {})[name] = new
                    else:
                        self.swapped += 1
                        m.changed["backgrounds"] += 1
            if end is None:
                break               # the rest of it hasn't come yet
            m.pos, m.left = end, m.left - 1
            m.handled = set()
        return edits

    def bought(self, data):
        """A client datagram: {component name: how many} if it's a purchase from a vendor whose list went through here
        (else {}); a resend of one already counted gives {}."""
        try:
            pkt = framing.parse_header(data)
            if pkt is None or not pkt.flags & framing.FLAG_APP_DATA or pkt.seq in self.buys_seen:
                return {}
            got = {}
            for _counter, _channel, count, _index, _group, body in framing.iter_fragments(data, pkt):
                if count != 1 or len(body) < 20 or struct.unpack_from("<I", body, 0)[0] != OP_ACTION \
                        or struct.unpack_from("<I", body, 8)[0] != ACT_BUY:
                    continue
                n = struct.unpack_from("<I", body, 16)[0]
                for k in range(min(n, 300)):
                    if 20 + 8 * k + 8 > len(body):
                        break
                    amount, iid = struct.unpack_from("<II", body, 20 + 8 * k)
                    name = self.ware_names.get(iid)
                    if name in self.components and amount:
                        got[name] = got.get(name, 0) + amount
            if got:
                self.buys_seen = (self.buys_seen + [pkt.seq])[-64:]
            return got
        except Exception:
            return {}

    def _decide(self, name, amounts, want):
        """The item type a component's entry is shown as (None: as it is)."""
        if name not in self.components:
            return None
        kind = want.get("buy") if name in amounts else want.get("other")
        return kind if kind in BACKGROUNDS and kind != COMPONENT else None
