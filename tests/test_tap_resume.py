"""The relay tap can start a session from a checkpoint's journal offset, and says where journals end."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from companion.capture import TapDecoder
from companion.relay import Journal

TAP_PORT = 29431


class TapResumeTest(unittest.TestCase):
    def test_resume_and_positions(self):
        asyncio.run(self.scenario())

    async def scenario(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        journal = Journal(Path(tmp.name) / "journals", "x")
        s = journal.new_session(("127.0.0.1", 9000))
        for i in range(10):
            s.writer.write("s2c", bytes([i]) * 8, i)
            if i == 5:
                mid = s.writer.size()               # a checkpoint covering records 0-5
        tap = await asyncio.start_server(journal.serve_consumer, "127.0.0.1", TAP_PORT)
        self.addCleanup(journal.close)
        try:
            await self.check(s, mid)
        finally:
            tap.close()

    async def check(self, s, mid):

        async def connect(first=None):
            reader, writer = await asyncio.open_connection("127.0.0.1", TAP_PORT)
            if first:
                writer.write((json.dumps(first) + "\n").encode())
                await writer.drain()
            return reader, writer

        async def records(reader, until):
            decoder, got = TapDecoder(), []
            while not until(got):
                got += decoder.feed(await asyncio.wait_for(reader.read(65536), 2))
            return got

        # resumed: the header, the marker, then records 6-9 only
        reader, writer = await connect({"cmd": "resume", "from": {s.id: mid}})
        got = await records(reader, lambda g: sum(1 for r in g if r[2] == "s2c") >= 4)
        self.assertEqual(got[0][4]["event"], "session")
        self.assertEqual(got[1][4], {"event": "resumed", "offset": mid})
        self.assertEqual([r[4][0] for r in got if r[2] == "s2c"], [6, 7, 8, 9])
        # where the journal ends, after everything already sent
        writer.write(b'{"cmd": "positions", "id": "p"}\n')
        reply = await records(reader, lambda g: any(r[2] == "control" and r[4].get("event") == "positions" for r in g))
        positions = next(r[4] for r in reply if r[2] == "control" and r[4].get("event") == "positions")
        self.assertEqual(positions["sessions"][str(s.num)], {"id": s.id, "offset": s.writer.size()})
        writer.close()

        # no resume: the whole journal, as before (after the short wait for one)
        reader, writer = await connect()
        got = await records(reader, lambda g: sum(1 for r in g if r[2] == "s2c") >= 10)
        self.assertEqual([r[4][0] for r in got if r[2] == "s2c"], list(range(10)))
        writer.close()


if __name__ == "__main__":
    unittest.main()
