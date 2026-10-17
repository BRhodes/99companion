"""Tiny local HTTP server for the dashboard (stdlib only).

GET /            dashboard page
GET /api/state   current tracker snapshot (JSON)
GET /events      server-sent events: one `state` message per tracker change
"""

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)
PING_EVERY = 10.0       # seconds of quiet before the event stream says it's still there (the page's watchdog)
STATIC = Path(__file__).parent / "static"
MAX_BODY = 256 * 1024


class WebServer:
    def __init__(self, host, port, snapshot):
        self.host = host
        self.port = port
        self.snapshot = snapshot  # callable -> JSON-serialisable dict
        self.router = None        # callable (method, path, body) -> (status, ctype, bytes) or None
        self.clients = set()
        self.server = None

    @property
    def hosts(self):
        """The Host values this dashboard answers to: its own addresses, never another name."""
        return {f"{h}:{self.port}" for h in ("127.0.0.1", "localhost", "[::1]")}

    async def start(self):
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("dashboard at http://localhost:%d/", self.port)

    def close(self):
        if self.server:
            self.server.close()
        for q in self.clients:
            q.put_nowait(None)

    def publish(self):
        """Push the current snapshot to every open dashboard."""
        if not self.clients:
            return
        payload = json.dumps(self.snapshot())
        for q in list(self.clients):
            if q.qsize() > 50:  # slow tab; drop stale updates
                continue
            q.put_nowait(payload)

    async def _handle(self, reader, writer):
        try:
            request = await reader.readline()
            headers = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            parts = request.decode("latin-1").split()
            method = parts[0].upper() if parts else "GET"
            path = parts[1].split("?", 1)[0] if len(parts) > 1 else "/"
            length = min(int(headers.get("content-length") or 0), MAX_BODY)
            body = await reader.readexactly(length) if length else b""
            if headers.get("host", "").lower() not in self.hosts:
                # DNS rebinding: a web page whose name has been pointed at 127.0.0.1 counts as this dashboard's own
                # origin to the browser, but still says its own name in Host. Only our own names get an answer.
                await self._send(writer, 403, "application/json", b'{"error": "unknown Host"}')
            elif method != "GET" and headers.get("x-companion") != "1":
                # Any web page can POST to localhost; a custom header forces a CORS preflight we never
                # answer, so only this dashboard can change state or (later) trigger actions.
                await self._send(writer, 403, "application/json", b'{"error": "missing X-Companion header"}')
            elif path == "/events":
                await self._events(writer)
            elif path == "/api/state":
                await self._send(writer, 200, "application/json", json.dumps(self.snapshot()).encode())
            elif path in ("/", "/index.html"):
                await self._send(writer, 200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes())
            else:
                result = self.router(method, path, body) if self.router else None
                if asyncio.iscoroutine(result):  # handlers that wait on the relay
                    result = await result
                if result is None:
                    await self._send(writer, 404, "text/plain", b"not found")
                else:
                    await self._send(writer, *result)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("web request failed")
        finally:
            writer.close()

    @staticmethod
    async def _send(writer, status, ctype, body):
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict",
                  501: "Not Implemented", 502: "Bad Gateway", 503: "Service Unavailable"}.get(status, "")
        cache = "max-age=86400" if ctype == "image/png" else "no-store"
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                f"Cache-Control: {cache}\r\nConnection: close\r\n\r\n")
        writer.write(head.encode() + body)
        await writer.drain()

    async def _events(self, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     b"Connection: keep-alive\r\n\r\n")
        q = asyncio.Queue()
        self.clients.add(q)
        try:
            q.put_nowait(json.dumps(self.snapshot()))
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=PING_EVERY)
                except asyncio.TimeoutError:
                    writer.write(b"event: ping\ndata: {}\n\n")  # the page's watchdog: still connected
                    await writer.drain()
                    continue
                if payload is None:
                    break
                writer.write(f"event: state\ndata: {payload}\n\n".encode())
                await writer.drain()
        finally:
            self.clients.discard(q)
