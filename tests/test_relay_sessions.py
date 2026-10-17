"""Dead sessions don't linger: the server streaming to a client that's gone doesn't keep its session alive, and
a relay restart doesn't bring back one whose client stopped talking. A client's Disconnects don't end anything:
it sends a burst of them as it starts, some landing just after its login."""

import asyncio
import json
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

from companion import relay as rl
from companion import proxy
from companion.proxy import MultiRelay

CLIENT = ("127.0.0.1", 9000)


def packet(flags, seq=5):
    return struct.pack("<IIIHHHH", seq, flags, 0, 0, 0, 0, 0)


class FakeLink:
    def __init__(self, client=CLIENT):
        self.client = client
        self.info = {}


class FakeRelay:
    def __init__(self):
        self.links, self.closed = {}, []

    def close_link(self, client):
        self.closed.append(client)


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.relay = FakeRelay()
        self.j = rl.Journal(self.tmp.name, "x", self.relay)
        self.addCleanup(self.j.close)
        self.link = FakeLink()
        self.j.on_link(self.link)
        self.s = self.link.info["session"]

    def test_the_servers_stream_alone_doesnt_keep_a_session_alive(self):
        self.j.on_packet(self.link, "c2s", packet(0x2), 0)
        self.s.last_client -= rl.IDLE_TIMEOUT + 1
        self.j.on_packet(self.link, "s2c", packet(0xA), 0)       # the server still talking to a dead client
        self.j.reap()
        self.assertIsNotNone(self.s.ended)
        self.assertEqual(self.relay.closed, [CLIENT])

    def test_a_client_heard_from_stays(self):
        self.s.last_client -= rl.IDLE_TIMEOUT - 30
        self.j.on_packet(self.link, "c2s", packet(0x2), 0)
        self.j.reap()
        self.assertIsNone(self.s.ended)

    def test_disconnects_just_after_a_login_leave_the_session_alone(self):
        self.j.on_packet(self.link, "c2s", packet(0x2, seq=1), 0)
        self.j.on_packet(self.link, "c2s", packet(0x8002, seq=0), 1)
        self.assertIsNone(self.s.ended)
        self.assertEqual(self.relay.closed, [])

    def test_a_restart_skips_a_session_whose_client_went_quiet(self):
        now = time.time()
        live = dict(self.s.state(9120), num=1, client=["127.0.0.1", 9000], last_ts=now, last_client=now - 5)
        dead = dict(self.s.state(9122), num=2, client=["127.0.0.1", 59991], last_ts=now,      # server still sends
                    last_client=now - rl.RESUME_WINDOW - 1)
        Path(self.tmp.name, "current.json").write_text(json.dumps({"next_num": 3, "sessions": [live, dead]}))
        j2 = rl.Journal(self.tmp.name, "x", None)
        self.addCleanup(j2.close)
        asyncio.run(j2.resume())
        self.assertEqual(sorted(j2.by_client), [("127.0.0.1", 9000)])


class ProxyTest(unittest.TestCase):
    def test_a_disconnect_from_an_unknown_client_opens_nothing(self):
        r = MultiRelay("127.0.0.1", 0, "127.0.0.1", 1)
        seen = []
        r.on_link = seen.append
        r.from_client(0, packet(0x8002, seq=0), CLIENT)
        self.assertEqual((r.links, seen), ({}, []))


class UdpTest(unittest.TestCase):
    def test_a_receive_error_doesnt_stop_the_socket(self):
        asyncio.run(self.scenario())

    async def scenario(self):
        loop = asyncio.get_running_loop()
        real, fails = loop.sock_recvfrom, [OSError(1450, "insufficient system resources")]

        async def flaky(sock, n):
            if fails:
                raise fails.pop()
            return await real(sock, n)
        loop.sock_recvfrom = flaky

        class P(proxy._Protocol):
            got = []

            def datagram_received(self, data, addr):
                if data == b"boom":
                    raise ValueError("a bad packet")
                self.got.append(data)
        transport, p = await proxy.open_udp(loop, P(), ("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for data in (b"boom", b"hello"):
            s.sendto(data, ("127.0.0.1", port))
        for _ in range(100):
            if p.got:
                break
            await asyncio.sleep(0.01)
        transport.close()
        s.close()
        self.assertEqual(p.got, [b"hello"])
        self.assertEqual(sum(1 for _ in p.errors), 1)     # the receive error, reported (the loop carried on)


if __name__ == "__main__":
    unittest.main()
