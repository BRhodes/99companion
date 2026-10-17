import asyncio
import json
import socket
import unittest
import urllib.request

from companion.proxy import MultiRelay
from companion.relay import redact_login
from companion.web import WebServer


async def fake_server(loop, base_port, count=2):
    got = []

    class FakeServer(asyncio.DatagramProtocol):
        def connection_made(self, t):
            self.t = t

        def datagram_received(self, data, addr):
            got.append((data, addr))
            self.t.sendto(b"reply:" + data, addr)

    endpoints = [await loop.create_datagram_endpoint(FakeServer, local_addr=("127.0.0.1", base_port + i))
                 for i in range(count)]
    return got, endpoints


def udp_client():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    s.bind(("127.0.0.1", 0))
    return s


async def recv(sock, tries=50):
    for _ in range(tries):
        await asyncio.sleep(0.01)
        try:
            data, src = sock.recvfrom(2048)
            return data, src[1]
        except BlockingIOError:
            pass
    return None


class RelayTest(unittest.TestCase):
    def test_round_trip_both_ports(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            got, endpoints = await fake_server(loop, 19110)
            taps = []
            relay = MultiRelay("127.0.0.1", 29110, "127.0.0.1", 19110,
                               on_packet=lambda link, d, data, i: taps.append((d, i)))
            await relay.start()
            client = udp_client()
            replies = []
            for i in range(2):
                client.sendto(b"hello%d" % i, ("127.0.0.1", 29110 + i))
                replies.append(await recv(client))
            relay.close()
            for t, _ in endpoints:
                t.close()
            client.close()
            return [d for d, _ in got], replies, taps

        got, replies, taps = asyncio.run(scenario())
        self.assertEqual(got, [b"hello0", b"hello1"])
        self.assertEqual(replies, [(b"reply:hello0", 29110), (b"reply:hello1", 29111)])
        self.assertEqual(taps, [("c2s", 0), ("s2c", 0), ("c2s", 1), ("s2c", 1)])

    def test_each_client_gets_its_own_upstream_address(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            got, endpoints = await fake_server(loop, 19150, count=1)
            relay = MultiRelay("127.0.0.1", 29150, "127.0.0.1", 19150, port_count=1, upstream_ports=range(29160, 29163))
            await relay.start()
            a, b = udp_client(), udp_client()
            a.sendto(b"from-a", ("127.0.0.1", 29150))
            ra = await recv(a)
            b.sendto(b"from-b", ("127.0.0.1", 29150))
            rb = await recv(b)
            a.sendto(b"again-a", ("127.0.0.1", 29150))
            ra2 = await recv(a)
            extra_b = await recv(b, tries=5)
            relay.close()
            for t, _ in endpoints:
                t.close()
            a.close()
            b.close()
            return got, ra, rb, ra2, extra_b

        got, ra, rb, ra2, extra_b = asyncio.run(scenario())
        server_peers = {data: addr for data, addr in got}
        self.assertNotEqual(server_peers[b"from-a"], server_peers[b"from-b"])   # distinct players to the server
        self.assertEqual(server_peers[b"from-a"], server_peers[b"again-a"])     # stable per client
        self.assertEqual({server_peers[b"from-a"][1], server_peers[b"from-b"][1]}, {29160, 29161})  # pooled ports
        self.assertEqual((ra[0], rb[0], ra2[0]), (b"reply:from-a", b"reply:from-b", b"reply:again-a"))
        self.assertIsNone(extra_b)  # b never sees a's traffic

    def test_dashboard_serves_state(self):
        async def scenario():
            web = WebServer("127.0.0.1", 28099, lambda: {"relay": True, "sessions": []})
            await web.start()
            loop = asyncio.get_running_loop()
            body = await loop.run_in_executor(None, lambda: urllib.request.urlopen("http://127.0.0.1:28099/api/state").read())
            page = await loop.run_in_executor(None, lambda: urllib.request.urlopen("http://127.0.0.1:28099/").read())
            web.close()
            return json.loads(body), page

        state, page = asyncio.run(scenario())
        self.assertTrue(state["relay"])
        self.assertIn(b"99 Companion", page)


class RedactTest(unittest.TestCase):
    def test_password_blanked(self):
        pkt = b"\x00" * 24 + b"\xef\xbe\xef\xbe" + b"zorn:secret\x00\x00\x00" + b"tail"
        out = redact_login("c2s", pkt)
        self.assertNotIn(b"secret", out)
        self.assertIn(b"zorn:******", out)
        self.assertEqual(len(out), len(pkt))
        self.assertEqual(redact_login("s2c", pkt), pkt)


if __name__ == "__main__":
    unittest.main()

