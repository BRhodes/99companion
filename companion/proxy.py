"""Passive UDP relay for any number of clients.

Every client connects to the same listen address; each distinct client address gets its own upstream
socket, because the server identifies a player by source address. Upstream ports come from a fixed
pool so that a restarted relay can rebind them and the server keeps seeing the same peers.

Every datagram is forwarded unchanged before anything else happens; `on_packet` gets a copy. (The one exception,
off unless asked for: `rewrite_s2c`, which may hand back a server datagram changed -- relay --tint-store. The
journal still gets what the server sent.)
The client talks to the server's port and port+1, so the relay listens on both and forwards each to
the matching upstream port; replies go back out of the listen socket matching the server's port.
"""

import asyncio
import logging
import socket
import struct
import time

log = logging.getLogger(__name__)

DISCONNECT = 0x8000     # packet header flag: the client is closing the connection
ERROR_SUMMARY = 600.0   # seconds between summaries of a repeating socket error
C2S = "c2s"
S2C = "s2c"


class _Udp:
    """A UDP socket with its own receive loop, standing in for asyncio's datagram transport. On Windows,
    asyncio's transport stops receiving for good after any receive error but "port unreachable" (one
    WinError 1450, say): a listen port then goes deaf, every client's packets to it are lost, and they can't
    connect until the relay restarts. Here an error is reported (protocol.error_received) and the loop
    carries on."""

    def __init__(self, loop, sock, protocol):
        self.loop, self.sock, self.protocol = loop, sock, protocol
        self.closed = False
        self.task = loop.create_task(self._read())

    async def _read(self):
        while not self.closed:
            try:
                data, addr = await self.loop.sock_recvfrom(self.sock, 65536)
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                if self.closed:
                    return
                self.protocol.error_received(exc)
                await asyncio.sleep(0.01)   # don't spin if it keeps failing at once
                continue
            if data:
                try:
                    self.protocol.datagram_received(data, addr)
                except Exception:           # one bad packet's handling mustn't stop the socket
                    log.exception("handling a datagram from %s failed", addr)

    def sendto(self, data, addr):
        if self.closed:
            return
        try:
            self.sock.sendto(data, addr)
        except OSError as exc:            # a full buffer, an unreachable port: that datagram is lost, as UDP's are
            self.protocol.error_received(exc)

    def get_extra_info(self, name, default=None):
        return self.sock.getsockname() if name == "sockname" else default

    def is_closing(self):
        return self.closed

    def close(self):
        if not self.closed:
            self.closed = True
            self.task.cancel()
            self.sock.close()


async def open_udp(loop, protocol, local_addr):
    """Bind a UDP socket at local_addr (OSError if it can't) and start its receive loop: (transport, protocol)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        sock.bind(local_addr)
    except OSError:
        sock.close()
        raise
    transport = _Udp(loop, sock, protocol)
    protocol.connection_made(transport)
    return transport, protocol


class _Protocol:
    """Socket errors: logged the first time, then counted and summed up at most every ERROR_SUMMARY seconds
    (Windows sometimes fails one overlapped receive or send, e.g. WinError 1450, "insufficient system
    resources", and the socket carries on: noise, unless it keeps coming)."""

    def __init__(self):
        self.transport = None
        self.errors = {}        # error code -> {count since last logged, when last logged}

    def connection_made(self, transport):
        self.transport = transport

    def _where(self):
        return ""

    def error_received(self, exc):
        now = time.monotonic()
        key = getattr(exc, "winerror", None) or getattr(exc, "errno", None) or type(exc).__name__
        seen = self.errors.setdefault(key, {"count": 0, "logged": None})
        seen["count"] += 1
        if seen["logged"] is None:
            log.warning("%s socket error: %s (repeats are counted, not logged)", self._where(), exc)
            seen["logged"], seen["count"] = now, 0
        elif now - seen["logged"] >= ERROR_SUMMARY:
            log.info("%s socket error: %s, %d more in the last %.0f min", self._where(), exc,
                     seen["count"], (now - seen["logged"]) / 60)
            seen["logged"], seen["count"] = now, 0


class _ListenProtocol(_Protocol):
    def __init__(self, relay, index):
        super().__init__()
        self.relay = relay
        self.index = index

    def _where(self):
        return f"listen port {self.relay.listen_port + self.index}"

    def datagram_received(self, data, addr):
        self.relay.from_client(self.index, data, addr)


class _UpstreamProtocol(_Protocol):
    def __init__(self, relay, link):
        super().__init__()
        self.relay = relay
        self.link = link

    def _where(self):
        return "upstream for %s:%d" % self.link.client

    def datagram_received(self, data, addr):
        self.relay.from_server(self.link, data, addr)


class Link:
    """One client <-> server pairing. `info` is free for the owner (journal, session id, ...)."""

    def __init__(self, client, upstream_port):
        self.client = client
        self.upstream_port = upstream_port
        self.transport = None
        self.pending = []      # client datagrams that arrived before the upstream socket was ready
        self.last_seen = 0.0
        self.info = {}

    @property
    def ready(self):
        return self.transport is not None


class MultiRelay:
    def __init__(self, listen_host, listen_port, server_host, server_port, port_count=2,
                 upstream_ports=(), on_packet=None, on_link=None):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.server_host = server_host
        self.server_port = server_port
        self.port_count = port_count
        self.upstream_ports = list(upstream_ports)  # empty = random ports
        self.on_packet = on_packet  # (link, direction, data, index)
        self.on_link = on_link      # (link) when a new client appears
        self.rewrite_s2c = None     # (link, data) -> the datagram to send the client instead (storetint)
        self.server_ip = None
        self.listeners = []
        self.links = {}
        self.loop = None

    async def start(self):
        await self.prepare()
        await self.listen()

    async def prepare(self):
        """Resolve the server; links can be opened (resumed) from here on, before any client is heard."""
        self.loop = asyncio.get_running_loop()
        infos = await self.loop.getaddrinfo(self.server_host, self.server_port, family=socket.AF_INET,
                                            type=socket.SOCK_DGRAM)
        self.server_ip = infos[0][4][0]

    async def listen(self):
        for i in range(self.port_count):
            _t, proto = await open_udp(self.loop, _ListenProtocol(self, i), (self.listen_host, self.listen_port + i))
            self.listeners.append(proto)
        log.info("relaying %s:%d-%d -> %s (%s):%d-%d", self.listen_host, self.listen_port,
                 self.listen_port + self.port_count - 1, self.server_host, self.server_ip,
                 self.server_port, self.server_port + self.port_count - 1)

    async def open_link(self, client, upstream_port=None):
        """Create (or resume) the link for a client, binding its upstream socket."""
        link = self.links.get(client)
        if link is None:
            link = self.links[client] = self._new_link(client, upstream_port)
        await self._bind(link, upstream_port)
        return link

    def _new_link(self, client, upstream_port=None):
        return Link(client, upstream_port)

    async def _bind(self, link, preferred=None):
        candidates = [preferred] if preferred else []
        used = {l.upstream_port for l in self.links.values() if l is not link and l.ready}
        candidates += [p for p in self.upstream_ports if p not in used and p != preferred]
        for port in candidates + [0]:
            try:
                _t, proto = await open_udp(self.loop, _UpstreamProtocol(self, link), ("0.0.0.0", port))
            except OSError:
                continue
            link.transport = proto.transport
            link.upstream_port = proto.transport.get_extra_info("sockname")[1]
            if port == 0 and self.upstream_ports:
                log.warning("no pooled upstream port free for %s; using random port %d (a relay restart "
                            "would drop this game session)", link.client, link.upstream_port)
            break
        for index, data in link.pending:
            self._forward(link, index, data)
        link.pending.clear()

    def close_link(self, client):
        link = self.links.pop(client, None)
        if link and link.transport:
            link.transport.close()

    def close(self):
        for p in self.listeners:
            if p.transport:
                p.transport.close()
        for client in list(self.links):
            self.close_link(client)

    def from_client(self, index, data, addr):
        link = self.links.get(addr)
        if link is None:
            if len(data) >= 20 and struct.unpack_from("<I", data, 4)[0] & DISCONNECT:
                return          # a disconnect from a client with no connection here: nothing to close
            link = self.links[addr] = self._new_link(addr)
            log.info("new client %s:%d", *addr)
            self._safe(self.on_link, link)
            self.loop.create_task(self._bind(link))
        link.last_seen = self.loop.time()
        if link.ready:
            self._forward(link, index, data)
        else:
            link.pending.append((index, data))

    def _forward(self, link, index, data):
        link.transport.sendto(data, (self.server_ip, self.server_port + index))
        self._safe(self.on_packet, link, C2S, data, index)

    def from_server(self, link, data, addr):
        if self.links.get(link.client) is not link:
            return
        index = addr[1] - self.server_port
        if not 0 <= index < self.port_count:
            index = 0
        link.last_seen = self.loop.time()
        out = data
        if self.rewrite_s2c is not None:
            try:
                out = self.rewrite_s2c(link, data)
            except Exception:
                log.exception("rewriting a server datagram failed; sent as it came")
                out = data
        self.listeners[index].transport.sendto(out, link.client)
        self._safe(self.on_packet, link, S2C, data, index)

    @staticmethod
    def _safe(fn, *args):
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # bookkeeping problems must never break the relay
            log.exception("relay callback failed")
