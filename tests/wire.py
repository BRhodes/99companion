"""Builders for synthetic 1999 packets and messages, following the wire spec."""

import struct

HDR = struct.Struct("<IIIIHBB")
FRAG = struct.Struct("<IIHHHH")


class Msg:
    """Little-endian message builder; strings are aligned relative to the message start."""

    def __init__(self, opcode):
        self.b = bytearray(struct.pack("<I", opcode))

    def u8(self, v):
        self.b += struct.pack("<B", v)
        return self

    def u16(self, v):
        self.b += struct.pack("<H", v)
        return self

    def u32(self, v):
        self.b += struct.pack("<I", v)
        return self

    def i32(self, v):
        self.b += struct.pack("<i", v)
        return self

    def f64(self, v):
        self.b += struct.pack("<d", v)
        return self

    def align(self):
        self.b += b"\x00" * ((-len(self.b)) % 4)
        return self

    def string(self, s):
        raw = s.encode("latin-1") + b"\x00"
        self.u16(len(raw))
        self.b += raw
        return self.align()

    def skill(self, level, sac, pp, init_level, resistance, since):
        self.u32(level).i32(sac).u32(pp).u32(init_level).i32(resistance)
        return self.f64(since)

    def bytes(self):
        return bytes(self.b)


def fragments(counter, group, payload, chunk=448):
    pieces = [payload[i:i + chunk] for i in range(0, len(payload), chunk)] or [b""]
    return [FRAG.pack(counter, 0x80000000, len(pieces), FRAG.size + len(p), i, group) + p
            for i, p in enumerate(pieces)]


def s2c(seq, *frags):
    body = b"".join(frags)
    return HDR.pack(seq, 0x802, 11, 0, len(body), 0, 0) + body


def c2s(seq, *frags):
    body = b"".join(frags)
    return HDR.pack(seq, 0x802, 1, 0, len(body), 0, 0) + struct.pack("<I", seq) + body


class Conversation:
    """Numbers packets and fragment counters so tests can just send messages."""

    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.seq = {"c2s": 1, "s2c": 1}
        self.counter = 1
        self.t = 1000.0

    def send(self, direction, msg, group=4, dt=0.05):
        self.t += dt
        frags = fragments(self.counter, group, msg.bytes() if isinstance(msg, Msg) else msg)
        self.counter += 1
        build = s2c if direction == "s2c" else c2s
        for f in frags:
            self.seq[direction] += 1
            self.pipeline.feed(direction, build(self.seq[direction], f), self.t)

    def server(self, msg, **kw):
        self.send("s2c", msg, **kw)

    def client(self, msg, **kw):
        self.send("c2s", msg, group=5, **kw)
