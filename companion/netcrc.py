"""Packet checksums of the 1999 protocol (mirror of the server's crc.cpp).

  header  H = magic(20-byte header with the CRC field set to 0x03291975, include_size=True)
  control packets (GenericCRC):     crc = H + magic(body, False)
  sequenced packets (BlobCRC):      crc = H + (payload ^ keystream(seq))
      payload = magic(option bytes, True) + sum over fragments of magic(frag header, True) + magic(frag body, True)

The client verifies server checksums, so a server packet the relay rewrites must get a fresh one. The
ISAAC keystream word never needs computing: it is recovered from the original packet as
(crc - H) ^ payload, and is the same for any packet with that sequence number.
"""

import struct

CRC_SEED = 0x03291975
MASK = 0xFFFFFFFF

ACK_SEQUENCE = 0x4000
TIME_UPDATE = 0x1000000
ECHO_REQUEST = 0x2000000
ECHO_RESPONSE = 0x4000000
FLOW = 0x8000000
FRAGMENTS = 0x4
REFERRAL = 0x800
OPTION_SIZES = ((ACK_SEQUENCE, 4), (TIME_UPDATE, 8), (ECHO_REQUEST, 4), (ECHO_RESPONSE, 8), (FLOW, 6))


def magic(buf, include_size):
    """GetMagicNumber: sum of little-endian dwords, tail bytes high-first, optionally + (len << 16)."""
    total = (len(buf) << 16) if include_size else 0
    n = len(buf) // 4
    if n:
        total += sum(struct.unpack_from(f"<{n}I", buf, 0))
    shift = 3
    for b in buf[n * 4:]:
        total += b << (shift * 8)
        shift -= 1
    return total & MASK


def header_crc(packet):
    hdr = bytearray(packet[:20])
    struct.pack_into("<I", hdr, 12, CRC_SEED)
    return magic(bytes(hdr), True)


def control_crc(packet):
    """GenericCRC: no keystream (seq-0 control packets such as acks/NAKs)."""
    size = struct.unpack_from("<H", packet, 16)[0]
    return (header_crc(packet) + magic(packet[20:20 + size], False)) & MASK


def blob_payload(packet):
    """The BlobCRC payload term (before the keystream XOR)."""
    flags = struct.unpack_from("<I", packet, 4)[0]
    size = struct.unpack_from("<H", packet, 16)[0]
    body = packet[20:20 + size]
    option_size = sum(n for bit, n in OPTION_SIZES if flags & bit)
    total = magic(body[:option_size], True) if option_size else 0
    pos = option_size
    if flags & (FRAGMENTS | REFERRAL):
        while pos + 16 <= len(body):
            frag_size = struct.unpack_from("<H", body, pos + 10)[0]
            if frag_size < 16:
                break
            total += magic(body[pos:pos + 16], True) + magic(body[pos + 16:pos + frag_size], True)
            pos += frag_size
    elif flags & 0x200:
        total = magic(body, False)
    return total & MASK


def stored_crc(packet):
    return struct.unpack_from("<I", packet, 12)[0]


def keystream(packet):
    """Recover the keystream word a sequenced packet was checksummed with."""
    return ((stored_crc(packet) - header_crc(packet)) & MASK) ^ blob_payload(packet)


def with_crc(packet, crc):
    out = bytearray(packet)
    struct.pack_into("<I", out, 12, crc & MASK)
    return bytes(out)


def reseal_control(packet):
    return with_crc(packet, control_crc(packet))


def reseal_blob(packet, ks):
    return with_crc(packet, header_crc(packet) + (blob_payload(packet) ^ ks))
