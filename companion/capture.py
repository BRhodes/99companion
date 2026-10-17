"""Record format shared by session journals, the relay tap stream and offline replay.

Each record: f64 timestamp | u8 kind | u8 port index | u16 length | payload
  kind 0 = client->server datagram, 1 = server->client datagram,
  kind 2 = control: UTF-8 JSON, e.g. {"event": "session", "id": ..., "server": ...}
A record of any other kind is skipped.
"""

import json
import struct
import time

REC = struct.Struct("<dBBH")
KINDS = ("c2s", "s2c", "control")


def encode(kind, payload, index=0, ts=None):
    return REC.pack(time.time() if ts is None else ts, KINDS.index(kind), index, len(payload)) + payload


def encode_control(info, ts=None):
    return encode("control", json.dumps(info).encode(), ts=ts)


class StreamDecoder:
    """Incrementally split a byte stream into (ts, kind, index, payload) records."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk):
        self.buf += chunk
        out = []
        pos = 0
        while len(self.buf) - pos >= REC.size:
            ts, kind, index, n = REC.unpack_from(self.buf, pos)
            if len(self.buf) - pos - REC.size < n:
                break
            payload = bytes(self.buf[pos + REC.size:pos + REC.size + n])
            pos += REC.size + n
            if kind < len(KINDS):
                out.append((ts, KINDS[kind], index, json.loads(payload) if kind == 2 else payload))
        del self.buf[:pos]
        return out


class CaptureWriter:
    def __init__(self, path, redact=None):
        self.path = path
        self.f = open(path, "ab")
        self.redact = redact

    def write(self, kind, data, index=0, ts=None):
        if self.redact and kind != "control":
            data = self.redact(kind, data)
            if data is None:
                return None
        rec = encode(kind, data, index, ts)
        self.f.write(rec)
        self.f.flush()
        return rec

    def write_control(self, info, ts=None):
        rec = encode_control(info, ts)
        self.f.write(rec)
        self.f.flush()
        return rec

    def size(self):
        return self.f.tell()

    def close(self):
        self.f.close()


TAG = struct.Struct("<I")


def tag(num, rec):
    """Tap stream framing: the relay's session number, then a journal record."""
    return TAG.pack(num) + rec


class TapDecoder:
    """Incrementally split a tap stream into (session number, ts, kind, index, payload)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk):
        self.buf += chunk
        out = []
        pos = 0
        head = TAG.size + REC.size
        while len(self.buf) - pos >= head:
            (num,) = TAG.unpack_from(self.buf, pos)
            ts, kind, index, n = REC.unpack_from(self.buf, pos + TAG.size)
            if len(self.buf) - pos - head < n:
                break
            payload = bytes(self.buf[pos + head:pos + head + n])
            pos += head + n
            if kind < len(KINDS):
                out.append((num, ts, KINDS[kind], index, json.loads(payload) if kind == 2 else payload))
        del self.buf[:pos]
        return out


def read_capture(path):
    with open(path, "rb") as f:
        return StreamDecoder().feed(f.read())
