"""Transport framing for the 1999 protocol: packet header, fragments, blob reassembly.

Layout (see the wire spec in README):
  header  u32 seq | u32 flags | u32 recipient | u32 crc | u16 size | u8 time | u8 table
  0x800 (Referral) marks application data. `size` counts only the fragment bytes, which always sit
  at the end of the datagram: server packets have none before them, client packets carry a 4-byte
  prefix that `size` does not include. So fragments = datagram[len - size:].
  fragment u32 counter | u32 channel | u16 count | u16 size (incl. 16-byte header) | u16 index | u16 group
"""

import struct
import time
from collections import OrderedDict

HEADER = struct.Struct("<IIIIHBB")
FRAG = struct.Struct("<IIHHHH")
FLAG_APP_DATA = 0x800

BLOB_TIMEOUT = 30.0
SEQ_MEMORY = 512


class Packet:
    __slots__ = ("seq", "flags", "recipient", "size")

    def __init__(self, seq, flags, recipient, size):
        self.seq, self.flags, self.recipient, self.size = seq, flags, recipient, size


def parse_header(data):
    if len(data) < HEADER.size:
        return None
    seq, flags, recipient, _crc, size, _time, _table = HEADER.unpack_from(data, 0)
    return Packet(seq, flags, recipient, size)


def iter_fragments(data, pkt):
    """Yield (counter, channel, count, index, group, body) for each fragment in an app-data packet."""
    start = len(data) - pkt.size
    if pkt.size < FRAG.size or start < HEADER.size:
        return
    pos = start
    while len(data) - pos >= FRAG.size:
        counter, channel, count, fsize, index, group = FRAG.unpack_from(data, pos)
        if fsize < FRAG.size or pos + fsize > len(data) or count == 0 or index >= count:
            return
        yield counter, channel, count, index, group, data[pos + FRAG.size:pos + fsize]
        pos += fsize


class Reassembler:
    """Turns datagrams from one direction into complete (group, message bytes) blobs."""

    def __init__(self):
        self.partial = {}
        self.seen = OrderedDict()

    def reset(self):
        self.partial.clear()
        self.seen.clear()

    def feed(self, data, now=None):
        now = time.time() if now is None else now
        pkt = parse_header(data)
        if pkt is None or not pkt.flags & FLAG_APP_DATA:
            return []
        if pkt.seq in self.seen:  # retransmissions repeat identical bytes under the same sequence
            return []
        self.seen[pkt.seq] = True
        if len(self.seen) > SEQ_MEMORY:
            self.seen.popitem(last=False)

        out = []
        for counter, channel, count, index, group, body in iter_fragments(data, pkt):
            if count == 1:
                out.append((group, body))
                continue
            key = (channel, counter)
            entry = self.partial.get(key)
            if entry is None or entry["count"] != count:
                entry = self.partial[key] = {"count": count, "pieces": {}, "group": group, "t": now}
            entry["pieces"][index] = body
            if len(entry["pieces"]) == count:
                del self.partial[key]
                out.append((entry["group"], b"".join(entry["pieces"][i] for i in range(count))))
        for key in [k for k, e in self.partial.items() if now - e["t"] > BLOB_TIMEOUT]:
            del self.partial[key]
        return out
