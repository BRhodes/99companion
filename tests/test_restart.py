"""The relay keeps every character's game flowing while the companion (or the relay) restarts."""

import asyncio
import socket
import tempfile
import time
import unittest
from pathlib import Path

from companion.__main__ import Companion
from companion.proxy import MultiRelay
from companion.relay import IDLE_TIMEOUT, Journal
from companion.store import Store
from tests.test_decoder import LIFE, SPELLS, player_description, skill_update
from tests.wire import Msg, c2s, fragments, s2c

SERVER_PORT, LISTEN_PORT, TAP_PORT = 19310, 29310, 29319


class FakeServer(asyncio.DatagramProtocol):
    def connection_made(self, t):
        self.t = t
        self.last_peer = None
        self.got = 0

    def datagram_received(self, data, addr):
        self.last_peer = addr
        self.got += 1


class Player:
    """A fake client plus the server's view of it."""

    counter = 100

    def __init__(self, server, guid, listen_port):
        self.server = server
        self.guid = guid
        self.listen_port = listen_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(("127.0.0.1", 0))
        self.seq = {"c2s": 1, "s2c": 1}
        self.upstream = None
        self.received = []

    async def client_send(self, msg):
        self.seq["c2s"] += 1
        Player.counter += 1
        self.sock.sendto(c2s(self.seq["c2s"], *fragments(Player.counter, 5, msg.bytes())), ("127.0.0.1", self.listen_port))
        await asyncio.sleep(0.05)
        self.upstream = self.server.last_peer

    async def server_send(self, msg, group=4):
        Player.counter += 1
        for f in fragments(Player.counter, group, msg.bytes()):
            self.seq["s2c"] += 1
            self.server.t.sendto(s2c(self.seq["s2c"], f), self.upstream)
        await self.pump()

    async def pump(self):
        await asyncio.sleep(0.05)
        while True:
            try:
                self.received.append(self.sock.recvfrom(4096)[0])
            except BlockingIOError:
                return

    def xp(self, key, value):
        return Msg(0x0E).u32(self.guid).u32(key).i32(value)

    async def login(self, name, total, unassigned, skills):
        await self.client_send(Msg(0xF7B1).u32(1).u32(0x36).u32(1))
        await self.server_send(Msg(0xF746).u32(self.guid))
        await self.server_send(player_description(name, total, unassigned, skills))

    def close(self):
        self.sock.close()


def by_character(app):
    return {v.tracker.character: v for v in app.sessions.values()}


class RestartTest(unittest.TestCase):
    def test_two_characters_survive_companion_restart(self):
        asyncio.run(self.scenario())

    async def scenario(self):
        loop = asyncio.get_running_loop()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        srv_t, srv = await loop.create_datagram_endpoint(FakeServer, local_addr=("127.0.0.1", SERVER_PORT))
        relay = MultiRelay("127.0.0.1", LISTEN_PORT, "127.0.0.1", SERVER_PORT, port_count=1)
        journal = Journal(Path(tmp.name) / "journals", f"127.0.0.1:{SERVER_PORT}", relay)
        relay.on_link, relay.on_packet = journal.on_link, journal.on_packet
        await relay.start()
        tap = await asyncio.start_server(journal.serve_consumer, "127.0.0.1", TAP_PORT)
        db = Store(str(Path(tmp.name) / "events.sqlite3"))
        self.addCleanup(db.close)

        zorn = Player(srv, 0x50000001, LISTEN_PORT)
        alt = Player(srv, 0x50000002, LISTEN_PORT)
        # each character's server packets must reach only that character's client
        await zorn.login("Zorn", 11580, 2943, {LIFE: (1000, 50, 300.0)})
        await alt.login("Alt", 500, 100, {LIFE: (10, 0, 9999.0)})
        self.assertNotEqual(zorn.upstream, alt.upstream)
        await zorn.client_send(Msg(0xF7B1).u32(2).u32(0x48).u32(1122))
        await zorn.server_send(skill_update(LIFE, 1086, 50, 300.0))  # helper uses PLAYER == zorn.guid
        await zorn.server_send(zorn.xp(0x16, 2943 + 86))
        await zorn.server_send(zorn.xp(0x16, 2943 + 93))
        self.assertEqual(len(alt.received), 2, "alt received another character's packets")

        async def start_companion():
            app = Companion(SPELLS, db)
            task = asyncio.create_task(app.follow("127.0.0.1", TAP_PORT))
            for _ in range(100):
                await asyncio.sleep(0.02)
                chars = by_character(app)
                if "Zorn" in chars and "Alt" in chars and chars["Zorn"].tracker.grants:
                    break
            return app, task

        # --- a companion started mid-game catches up on both characters
        app1, task1 = await start_companion()
        chars = by_character(app1)
        self.assertEqual(set(chars), {"Zorn", "Alt"})
        self.assertEqual([g["difficulty"] for g in chars["Zorn"].tracker.grants], [75])
        self.assertEqual(len(chars["Alt"].tracker.grants), 0)
        self.assertEqual(len(app1.snapshot()["sessions"]), 2)

        # --- stop it; both games keep flowing
        task1.cancel()
        await asyncio.sleep(0.05)
        before = len(alt.received)
        await alt.server_send(alt.xp(0x16, 900))
        self.assertEqual(len(alt.received), before + 1)

        # --- a fresh companion rebuilds both, including what happened while it was down
        app2, task2 = await start_companion()
        chars = by_character(app2)
        self.assertEqual(chars["Alt"].tracker.xp_unassigned, 900)
        self.assertEqual([g["difficulty"] for g in chars["Zorn"].tracker.grants], [75])
        rows = db.db.execute("SELECT COUNT(*) FROM events WHERE kind = 'grant'").fetchone()[0]
        self.assertEqual(rows, 1, "replayed grant was stored twice")

        # --- a client that goes quiet is ended; its tab is marked logged out
        journal.sessions[1].last_client -= IDLE_TIMEOUT + 1
        journal.reap()
        await asyncio.sleep(0.1)
        ended = {c: v.ended is not None for c, v in by_character(app2).items()}
        self.assertEqual(ended, {"Zorn": True, "Alt": False})

        task2.cancel()
        await asyncio.sleep(0.05)
        zorn.close()
        alt.close()
        tap.close()
        relay.close()
        journal.close()
        srv_t.close()


class RelayRestartTest(unittest.TestCase):
    def test_relay_restart_resumes_every_session(self):
        asyncio.run(self.scenario())

    async def scenario(self):
        loop = asyncio.get_running_loop()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        srv_t, srv = await loop.create_datagram_endpoint(FakeServer, local_addr=("127.0.0.1", SERVER_PORT + 5))
        jdir = Path(tmp.name) / "journals"
        pool = range(29390, 29394)

        async def start_relay():
            relay = MultiRelay("127.0.0.1", LISTEN_PORT + 5, "127.0.0.1", SERVER_PORT + 5, port_count=1,
                               upstream_ports=pool)
            journal = Journal(jdir, "x", relay)
            relay.on_link, relay.on_packet = journal.on_link, journal.on_packet
            await relay.start()
            await journal.resume()
            return relay, journal

        relay, journal = await start_relay()
        a = Player(srv, 1, LISTEN_PORT + 5)
        b = Player(srv, 2, LISTEN_PORT + 5)
        await a.client_send(Msg(0xF7B1).u32(1).u32(0x36).u32(1))
        await b.client_send(Msg(0xF7B1).u32(1).u32(0x36).u32(2))
        sessions = {s.client: s.id for s in journal.by_client.values()}
        journal.save_state()
        relay.close()
        journal.close()
        await asyncio.sleep(0.05)

        relay, journal = await start_relay()  # restarted mid-game
        self.assertEqual({s.client: s.id for s in journal.by_client.values()}, sessions)
        # the server keeps talking to the same addresses, and each reply reaches the right client
        srv_t.sendto(b"for-a", a.upstream)
        srv_t.sendto(b"for-b", b.upstream)
        await a.pump()
        await b.pump()
        self.assertEqual((a.received, b.received), ([b"for-a"], [b"for-b"]))
        old = (a.upstream, b.upstream)
        await a.client_send(Msg(0xF7B1).u32(2).u32(0x36).u32(1))
        await b.client_send(Msg(0xF7B1).u32(2).u32(0x36).u32(2))
        self.assertEqual((a.upstream, b.upstream), old, "server saw a different source address after the restart")

        relay.close()
        journal.close()
        srv_t.close()
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
