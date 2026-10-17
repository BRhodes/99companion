"""Checksums against real server packets from a live session."""

import struct
import unittest

from companion import netcrc

# Captured from ac1999.cobradragon.com (server -> client).
CONTROL = bytes.fromhex("000000000a0000000b000000a01941030c000400080000000100000001000000")          # 0xA ack
ACKSEQ = bytes.fromhex("7b010000024000010b00000022b1d7420c00140006000000d3a6e3a7f47dc941")           # 0x1004002
DATA = bytes.fromhex("40000000020800000b0000003a5130861400070007000000000000800100140000000400c7f70000")  # 0x802


class RealPackets(unittest.TestCase):
    def test_control_crc(self):
        self.assertEqual(netcrc.control_crc(CONTROL), netcrc.stored_crc(CONTROL))

    def test_keystream_recovery_reproduces_the_original(self):
        for packet in (ACKSEQ, DATA):
            ks = netcrc.keystream(packet)
            self.assertEqual(netcrc.reseal_blob(packet, ks), packet)

    def test_known_isaac_words(self):
        # the seed-0 ISAAC stream starts 0x182600f3, 0x300b4a8d for sequences 2 and 3
        def fake(seq, word):
            body = struct.pack("<I", 7)
            hdr = struct.pack("<IIIIHBB", seq, 0x4002, 11, 0, len(body), 0, 0)
            return netcrc.reseal_blob(hdr + body, word)
        self.assertEqual(netcrc.keystream(fake(2, 0x182600F3)), 0x182600F3)

    def test_rewriting_the_ack_value_reseals(self):
        ks = netcrc.keystream(ACKSEQ)
        changed = bytearray(ACKSEQ)
        struct.pack_into("<I", changed, 20, 5)
        resealed = netcrc.reseal_blob(bytes(changed), ks)
        self.assertNotEqual(netcrc.stored_crc(resealed), netcrc.stored_crc(ACKSEQ))
        self.assertEqual(netcrc.keystream(resealed), ks)  # still valid under the same keystream word

    def test_resealing_a_control_packet(self):
        changed = bytearray(CONTROL)
        struct.pack_into("<I", changed, 28, 9)
        resealed = netcrc.reseal_control(bytes(changed))
        self.assertEqual(netcrc.control_crc(resealed), netcrc.stored_crc(resealed))


if __name__ == "__main__":
    unittest.main()
