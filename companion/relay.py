"""The long-lived half: UDP relay + per-character session journals + local tap.

    python -m companion.relay [--server host:port] [--listen host:port] [--tap host:port]

Keep this running while you play. Any number of clients can connect to the same listen address;
each gets its own upstream socket (the server tells players apart by address), its own session
journal (password redacted) and a session number on the tap.

A consumer (python -m companion) that connects is sent every active and recently ended session
from its start, then live records, so the dashboard can be restarted at any time without losing
state. The relay never waits on consumers: one that falls behind is disconnected and reconnects.

A restart of the relay itself is survivable: upstream ports come from a fixed pool and each session
records its client address and port in journals/current.json, which a restarted relay rebinds.
"""

import argparse
import asyncio
import json
import logging
import os
import struct
import time
from pathlib import Path

from .capture import CaptureWriter, StreamDecoder, encode, encode_control, tag
from .proxy import MultiRelay
from .storetint import BACKGROUNDS, StoreTint

log = logging.getLogger("relay")

DEFAULT_SERVER = "ac1999.cobradragon.com:9110"
ACCOUNT_MARKER = b"\xef\xbe\xef\xbe"  # 0xBEEFBEEF precedes the "user:pass" string in the login request
RESUME_WINDOW = 600.0                 # seconds: a relay restart within this resumes sessions
NEW_LOGIN_GAP = 30.0                  # a login request this long after the last one starts a new session
IDLE_TIMEOUT = 120.0                  # a client silent this long has quit; its session ends (the server
                                      # keeps streaming to a dead one: only the client's packets count)
RESUME_GRACE = 30.0                   # a resumed session's client has this long to be heard again
KEEP_ENDED = 900.0                    # ended sessions are still replayed to new consumers for this long
DEFAULT_JOURNAL_CAP = 10 * 1024 ** 3   # bytes: past this, the oldest finished journals are deleted
CAP_CHECK_EVERY = 60.0                 # seconds between journal-size checks
CONSUMER_BUFFER_LIMIT = 8 * 1024 * 1024
RESUME_WAIT = 0.3                     # seconds a new consumer has to say where it resumes from
CHUNK = 256 * 1024


def redact_login(direction, data):
    """Blank out the password in any client packet carrying the login account string."""
    if direction != "c2s":
        return data
    i = data.find(ACCOUNT_MARKER)
    if i < 0:
        return data
    buf = bytearray(data)
    window = buf[i + 4:i + 4 + 64]
    colon = window.find(b":")
    if colon >= 0:
        j = i + 4 + colon + 1
        while j < len(buf) and buf[j] != 0:
            buf[j] = ord("*")
            j += 1
    return bytes(buf)


def is_login_request(data):
    if len(data) < 20:
        return False
    flags, recipient = struct.unpack_from("<II", data, 4)
    return flags == 0x2 and recipient == 0 and ACCOUNT_MARKER in data


def hostport(text, default_port):
    host, _, port = text.rpartition(":")
    return (host, int(port)) if host else (text, default_port)


class Session:
    def __init__(self, num, sid, path, client, server_label):
        self.num = num
        self.id = sid
        self.client = client
        self.server_label = server_label
        self.writer = CaptureWriter(path, redact=redact_login)
        self.last_ts = time.time()
        self.last_client = self.last_ts   # the client's last packet: what says it's still there
        self.last_login = 0.0
        self.ended = None
        self.upstream_port = None     # last known, kept for the saved state once the link is closed

    @property
    def path(self):
        return self.writer.path

    def state(self, upstream_port):
        return {"num": self.num, "id": self.id, "path": str(self.path), "client": list(self.client),
                "upstream_port": upstream_port, "last_ts": self.last_ts, "last_client": self.last_client}


class Consumer:
    def __init__(self, writer):
        self.writer = writer
        self.live = set()  # session numbers this consumer is caught up on
        self.peer = writer.get_extra_info("peername")

    def send(self, data):
        if self.writer.is_closing():
            return
        self.writer.write(data)
        if self.writer.transport.get_write_buffer_size() > CONSUMER_BUFFER_LIMIT:
            log.warning("consumer %s fell behind; disconnecting it", self.peer)
            self.writer.close()


class Journal:
    """Per-client session journals plus the tap that streams them."""

    def __init__(self, directory, server_label, relay=None, cap_bytes=DEFAULT_JOURNAL_CAP):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "current.json"
        self.server_label = server_label
        self.relay = relay
        self.sessions = {}        # num -> Session (active and recently ended)
        self.by_client = {}       # client addr -> active Session
        self.shoplists = {}       # session id -> {amounts: its shopping list, buy / other: item types} (--tint-store)
        self.next_num = 1
        self.consumers = set()
        self.cap_bytes = cap_bytes
        self.last_cap_check = 0.0

    # ---- sessions ------------------------------------------------------------------------------
    def new_session(self, client):
        old = self.by_client.get(client)
        if old:
            self.end_session(old, "relogin")
        num = self.next_num
        self.next_num += 1
        sid = time.strftime("%Y%m%d-%H%M%S") + f"-{client[1]}"
        s = Session(num, sid, self.dir / f"session-{sid}.cap", client, self.server_label)
        self.sessions[num] = s
        self.by_client[client] = s
        for c in self.consumers:
            c.live.add(num)  # nothing to catch up on yet
        self._broadcast(s, s.writer.write_control(self._session_info(s)))
        log.info("session %d (%s) for client %s:%d -> %s", num, sid, client[0], client[1], s.path)
        self.save_state()
        return s

    def _session_info(self, s):
        return {"event": "session", "id": s.id, "num": s.num, "server": s.server_label,
                "client": f"{s.client[0]}:{s.client[1]}"}

    def end_session(self, s, reason):
        if s.ended:
            return
        s.ended = time.time()
        self._broadcast(s, s.writer.write_control({"event": "session_end", "id": s.id, "reason": reason}))
        s.writer.close()
        if self.by_client.get(s.client) is s:
            del self.by_client[s.client]
        log.info("session %d ended (%s)", s.num, reason)
        self.save_state()

    async def resume(self):
        """Rebind the sessions of a relay that restarted mid-game."""
        try:
            st = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        self.next_num = st.get("next_num", 1)
        now = time.time()
        for rec in st.get("sessions", []):
            heard = rec.get("last_client", rec.get("last_ts", 0))     # the client, not the server's stream
            if now - heard > RESUME_WINDOW or not Path(rec["path"]).exists():
                continue
            client = tuple(rec["client"])
            s = Session(rec["num"], rec["id"], rec["path"], client, self.server_label)
            s.last_ts = rec["last_ts"]
            s.last_client = max(heard, now - IDLE_TIMEOUT + RESUME_GRACE)   # a moment to be heard again
            s.writer.write_control({"event": "relay_restart", "id": s.id})
            self.sessions[s.num] = s
            self.by_client[client] = s
            if self.relay:
                link = await self.relay.open_link(client, rec.get("upstream_port"))
                link.info["session"] = s
            log.info("resumed session %d (%s) for client %s:%d", s.num, s.id, *client)

    def save_state(self):
        active = []
        for s in self.by_client.values():
            link = self.relay.links.get(s.client) if self.relay else None
            if link:
                s.upstream_port = link.upstream_port
            active.append(s.state(s.upstream_port))
        st = {"next_num": self.next_num, "server": self.server_label, "sessions": active}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(self.state_path)

    def reap(self, now=None):
        """End sessions whose client went quiet (whatever the server still sends it), and forget long-ended ones."""
        now = time.time() if now is None else now
        for s in list(self.by_client.values()):
            if now - s.last_client > IDLE_TIMEOUT:
                self.end_session(s, "idle")
                if self.relay:
                    self.relay.close_link(s.client)
        for num, s in list(self.sessions.items()):
            if s.ended and now - s.ended > KEEP_ENDED:
                del self.sessions[num]
        if self.cap_bytes and now - self.last_cap_check >= CAP_CHECK_EVERY:
            self.last_cap_check = now
            self.enforce_cap()

    def enforce_cap(self):
        """Delete the oldest finished journals until the folder is under the cap. A journal still in use
        (a live session, or one ended recently enough to be replayed) is never deleted, even if that
        leaves the folder over the cap. Returns the paths deleted."""
        in_use = {Path(s.path).resolve() for s in self.sessions.values()}
        files = []
        for f in self.dir.glob("session-*.cap"):
            try:
                st = f.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, f))
        total = sum(size for _, size, _ in files)
        deleted = []
        for _, size, f in sorted(files):
            if total <= self.cap_bytes:
                break
            if f.resolve() in in_use:
                continue
            try:
                f.unlink()
            except OSError as e:
                log.warning("couldn't delete old journal %s: %s", f.name, e)
                continue
            total -= size
            deleted.append(f)
            log.info("journal cap: deleted %s (%.1f MB)", f.name, size / 1024 ** 2)
        return deleted

    # ---- traffic -------------------------------------------------------------------------------
    def shoplist_for(self, client):
        """The shopping list (and backgrounds) the companion last sent for this client's session (None: none yet)."""
        s = self.by_client.get(client)
        return self.shoplists.get(s.id) if s else None

    def predict(self, link, bought):
        """A purchase the client's just sent ({name: how many}): taken off its session's shopping list until the
        companion sends the real one."""
        s = self.by_client.get(link.client)
        got = self.shoplists.get(s.id) if s else None
        if not got:
            return
        amounts = dict(got["amounts"])
        for name, n in bought.items():
            if name in amounts:
                left = amounts[name] - n
                if left > 0:
                    amounts[name] = left
                else:
                    del amounts[name]
        got["amounts"] = amounts
        self.note(link, {"event": "tint_buy", "bought": bought, "on_list": len(amounts)})

    def note(self, link, info):
        """A control record in the link's session journal (and to the companions): what the relay did (storetint)."""
        s = link.info.get("session")
        if s is not None and not s.ended:
            self._broadcast(s, s.writer.write_control(info))

    def on_link(self, link):
        link.info["session"] = self.new_session(link.client)

    def on_packet(self, link, direction, data, index):
        now = time.time()
        s = link.info.get("session")
        if s is None or s.ended:
            s = link.info["session"] = self.new_session(link.client)
        if direction == "c2s" and is_login_request(data):
            if now - s.last_login > NEW_LOGIN_GAP and s.writer.size() > 4096:
                s = link.info["session"] = self.new_session(link.client)
            s.last_login = now
        rec = s.writer.write(direction, data, index, ts=now)
        s.last_ts = now
        if direction == "c2s":
            s.last_client = now
        if rec:
            self._broadcast(s, rec)

    def _broadcast(self, s, rec):
        framed = tag(s.num, rec)
        for c in list(self.consumers):
            if s.num in c.live:
                c.send(framed)

    # ---- tap -----------------------------------------------------------------------------------
    async def serve_consumer(self, reader, writer):
        c = Consumer(writer)
        self.consumers.add(c)
        log.info("consumer connected: %s", c.peer)
        try:
            # A consumer may open with {"cmd": "resume", "from": {session id: journal offset}}: it already holds
            # each of those sessions up to that offset (a checkpoint), so they're streamed from there.
            resume, first = {}, None
            try:
                first = await asyncio.wait_for(reader.readline(), RESUME_WAIT)
            except asyncio.TimeoutError:
                pass
            if first:
                try:
                    cmd = json.loads(first)
                except ValueError:
                    cmd = {}
                if cmd.get("cmd") == "resume" and isinstance(cmd.get("from"), dict):
                    resume = cmd["from"]
                    first = None
            for num in sorted(self.sessions):
                s = self.sessions[num]
                if not await self._catch_up(c, s, resume.get(s.id)):
                    return
            if first:
                self.command(c, first)
            while not writer.is_closing():
                line = await reader.readline()  # consumers send one JSON command per line
                if not line:
                    break
                self.command(c, line)
        except (ConnectionError, OSError):
            pass
        finally:
            self.consumers.discard(c)
            writer.close()
            log.info("consumer disconnected: %s", c.peer)

    def command(self, c, line):
        try:
            cmd = json.loads(line)
        except ValueError:
            return
        if cmd.get("cmd") == "shoplist" and isinstance(cmd.get("amounts"), dict):
            # {"session": its id, "amounts": {name: how many}, "buy", "other": item types}: what the
            # companion's dashboard has to buy, and how a vendor's window shows it (storetint)
            kind = lambda k: cmd.get(k) if cmd.get(k) in BACKGROUNDS else None
            amounts = {str(n): int(a) for n, a in list(cmd["amounts"].items())[:500] if isinstance(a, int) and a > 0}
            self.shoplists[str(cmd.get("session"))] = {"amounts": amounts, "buy": kind("buy"), "other": kind("other")}
            return
        if cmd.get("cmd") == "positions":
            # how far each session's journal goes right now; sent after every record already sent, so the
            # consumer has seen exactly this much when it reads the reply (a checkpoint can name the offset)
            return c.send(tag(0, encode_control({"event": "positions", "id": cmd.get("id"), "sessions": {
                str(s.num): {"id": s.id, "offset": s.writer.size()} for s in self.sessions.values()
                if not s.writer.f.closed}})))

    async def _catch_up(self, c, s, start=None):
        """Stream one session's journal from the start (or from `start`, a record boundary the consumer
        already holds everything before: then the session's header record, a "resumed" marker, and what
        follows), then switch the consumer to its live records."""
        decoder = StreamDecoder()
        with open(s.path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if isinstance(start, int) and 0 < start <= size:
                header = None
                while header is None:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    got = decoder.feed(chunk)
                    header = got[0] if got else None
                if header is not None and header[1] == "control":
                    c.writer.write(tag(s.num, encode_control(header[3], header[0])))
                    c.writer.write(tag(s.num, encode_control({"event": "resumed", "offset": start}, header[0])))
                    f.seek(start)
                else:
                    f.seek(0)
                decoder = StreamDecoder()
            while True:
                if c.writer.is_closing():
                    return False
                chunk = f.read(CHUNK)
                if not chunk:
                    # Records are written whole and flushed inside packet callbacks, and nothing awaits
                    # between this EOF check and going live, so no record is skipped or doubled.
                    c.live.add(s.num)
                    return True
                out = bytearray()
                for ts, kind, index, payload in decoder.feed(chunk):
                    rec = encode_control(payload, ts) if kind == "control" else encode(kind, payload, index, ts)
                    out += tag(s.num, rec)
                c.writer.write(bytes(out))
                await c.writer.drain()

    def close(self):
        self.save_state()
        for s in self.sessions.values():
            if not s.writer.f.closed:
                s.writer.close()
        for c in list(self.consumers):
            c.writer.close()


async def run(args):
    server_host, server_port = hostport(args.server, 9110)
    listen_host, listen_port = hostport(args.listen, 9110)
    tap_host, tap_port = hostport(args.tap, 9119)
    pool = range(args.upstream_ports[0], args.upstream_ports[1] + 1) if args.upstream_ports else ()

    relay = MultiRelay(listen_host, listen_port, server_host, server_port, upstream_ports=pool)
    journal = Journal(args.journal_dir, f"{server_host}:{server_port}", relay,
                      cap_bytes=int(args.journal_cap_gb * 1024 ** 3))
    relay.on_link = journal.on_link
    relay.on_packet = journal.on_packet
    if args.tint_store:
        def on_packet(link, direction, data, index):
            journal.on_packet(link, direction, data, index)
            tint = link.info.get("tint")
            if direction == "c2s" and tint is not None:
                got = tint.bought(data)     # a purchase: off the list now, before the server's stock list comes back
                if got:
                    journal.predict(link, got)
        relay.on_packet = on_packet
        relay.rewrite_s2c = lambda link, data: link.info.setdefault(
            "tint", StoreTint(wanted=lambda: journal.shoplist_for(link.client),
                              on_list=lambda info: journal.note(link, info))).rewrite(data)
        log.info("tint-store: in a vendor's window, the spell components on your shopping list and the rest get the "
                 "backgrounds picked on the Order tab")
    await relay.prepare()
    await journal.resume()   # rebind resumed sessions before any packet arrives
    await relay.listen()
    tap = await asyncio.start_server(journal.serve_consumer, tap_host, tap_port)
    log.info("tap for companions at %s:%d; journals in %s", tap_host, tap_port, journal.dir.resolve())
    try:
        ticks = 0
        while True:
            await asyncio.sleep(0.5)
            ticks += 1
            if ticks % 10 == 0:
                journal.reap()
                journal.save_state()
    finally:
        tap.close()
        journal.close()   # saves state while the links (their ports) are still open
        relay.close()


def port_range(text):
    if text in ("0", ""):
        return None
    lo, _, hi = text.partition("-")
    return int(lo), int(hi or lo)


def main():
    p = argparse.ArgumentParser(prog="companion.relay", description="Long-lived AC1999 UDP relay and session journals")
    p.add_argument("--server", default=DEFAULT_SERVER, help="real server host:port (default %(default)s)")
    p.add_argument("--listen", default="127.0.0.1:9110", help="address clients connect to (default %(default)s)")
    p.add_argument("--tap", default="127.0.0.1:9119", help="local address companions connect to (default %(default)s)")
    p.add_argument("--upstream-ports", type=port_range, default=(9120, 9139),
                   help="pool of fixed local UDP ports toward the server, one per client, so a relay "
                        "restart keeps every session (default 9120-9139; 0 = random)")
    p.add_argument("--tint-store", action="store_true",
                   help="experiment: while your shopping list has anything on it, the spell components in a vendor's "
                        "window get the backgrounds picked on the Order tab, for what's on it and the rest (rewrites those server packets; the journal "
                        "keeps what the server sent)")
    p.add_argument("--journal-dir", default="journals", help="where session journals go (default %(default)s)")
    p.add_argument("--journal-cap-gb", type=float, default=10.0,
                   help="delete the oldest finished journals past this many GB (default %(default)s; 0 = never)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
