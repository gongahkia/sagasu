"""The host's optional remote listener: MCP over streamable HTTP, on loopback only.

A TLS reverse proxy on the same machine publishes it. Every request must carry
an API key; only SHA-256 digests of keys are configured, so the host's
configuration never contains a usable key. Sign-in stays with the person at the
host: see RemoteAccess in mcp_server and CALL_ORIGIN in origin.

Requests are independent POSTs answered with JSON. There are no sessions to
expire or to lose in a restart, and no event stream: nothing here needs the
server to call a remote client back.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import socket
import time

from .config import ConfigError
from .observability import event

MCP_PATH = "/mcp"
# Requests being served at once, counted only after the key has matched, so
# callers without a key cannot use up the allowance.
MAX_IN_FLIGHT = 32


def key_label(keys, headers):
    """Return the label of the API key in an Authorization header, else None."""
    try:
        value = headers.get("authorization") or ""
    except AttributeError:
        return None
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not 20 <= len(token) <= 512:
        return None
    digest = hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()
    found = None
    for known, label in keys.items():
        # Compare against every configured digest; no early exit.
        if hmac.compare_digest(known, digest):
            found = label
    return found


class KeyGate:
    """ASGI wrapper: nothing behind it runs without a configured API key."""

    def __init__(self, app, keys):
        self.app = app
        self.keys = dict(keys)
        self.active = 0
        self.rejected = 0
        self.reported_at = 0.0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = [
            value.decode("latin-1")
            for name, value in scope.get("headers") or ()
            if name.lower() == b"authorization"
        ]
        # Exactly one credential, so the check here and the label in the log
        # can never be about different headers.
        if (
            len(supplied) != 1
            or key_label(self.keys, {"authorization": supplied[0]}) is None
        ):
            self.rejected += 1
            now = time.monotonic()
            if now - self.reported_at >= 60:
                # At most one line a minute, so probing cannot flood the log.
                event(
                    "remote.unauthorized", level=logging.WARNING, requests=self.rejected
                )
                self.reported_at, self.rejected = now, 0
            await self.reply(
                send,
                401,
                "unauthorized",
                "Send one valid API key: Authorization: Bearer <key>.",
                (b"www-authenticate", b'Bearer realm="sagasu"'),
            )
        elif scope.get("method") != "POST":
            # No event stream to open and no session to end. The MCP transport
            # lets a server answer 405 to both, and an idle stream would only
            # hold a connection open.
            await self.reply(
                send,
                405,
                "method_not_allowed",
                "This endpoint answers POST requests only.",
                (b"allow", b"POST"),
            )
        elif self.active >= MAX_IN_FLIGHT:
            await self.reply(
                send,
                503,
                "busy",
                "Too many requests are in progress. Retry shortly.",
                (b"retry-after", b"1"),
            )
        else:
            self.active += 1
            try:
                await self.app(scope, receive, send)
            finally:
                self.active -= 1

    @staticmethod
    async def reply(send, status, error, message, header):
        body = json.dumps({"error": error, "message": message}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    header,
                    (b"cache-control", b"no-store"),
                    # Do not wait for the rest of a refused request's body.
                    (b"connection", b"close"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class ThirdPartyThrottle(logging.Filter):
    """Let only a few lines a minute from other libraries into the private log.

    The web server logs some malformed requests before the key is checked. Left
    alone, a caller without a key could rotate the per-call record out of the
    log files.
    """

    def __init__(self, limit=20, window=60):
        super().__init__()
        self.limit, self.window = limit, window
        self.started, self.count = 0.0, 0

    def filter(self, record):
        if record.name.startswith("sagasu"):
            return True
        now = time.monotonic()
        if now - self.started >= self.window:
            self.started, self.count = now, 0
        self.count += 1
        return self.count <= self.limit


def throttle_third_party_logs():
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, ThirdPartyThrottle) for item in handler.filters):
            handler.addFilter(ThirdPartyThrottle())


def bind_loopback(host, port):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.setblocking(False)
    except OSError:
        listener.close()
        raise ConfigError(
            "The remote listener could not bind its loopback port. Check SAGASU_HTTP_PORT and that no other Sagasu host is running."
        ) from None
    return listener


class RemoteHTTP:
    """Serve one remote MCP server inside the host's event loop."""

    def __init__(self, listener, server):
        self.listener = listener
        self.mcp_server = server
        self.server = None
        self.task = None

    async def start(self):
        import uvicorn
        from mcp.server.transport_security import TransportSecuritySettings

        class Server(uvicorn.Server):
            # The host owns signals and shutdown order; uvicorn must not.
            @contextlib.contextmanager
            def capture_signals(self):
                yield

        for name in ("uvicorn", "mcp"):
            logging.getLogger(name).setLevel(logging.WARNING)
        throttle_third_party_logs()
        app = self.mcp_server.streamable_http_app(
            streamable_http_path=MCP_PATH,
            json_response=True,
            stateless_http=True,
            host=self.listener.bind,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[
                    value
                    for host in self.listener.allowed_hosts
                    for value in (host, host + ":*")
                ],
                # No browser origin is allowed; agents do not send Origin.
                allowed_origins=[],
            ),
        )
        bound = bind_loopback(self.listener.bind, self.listener.port)
        self.server = Server(
            uvicorn.Config(
                KeyGate(app, self.listener.keys),
                log_config=None,
                access_log=False,
                lifespan="on",
                proxy_headers=False,
                server_header=False,
                ws="none",
                # Requests still running after this are cancelled, so a stop
                # finishes well inside the time local commands wait for it.
                timeout_graceful_shutdown=2,
            )
        )
        self.task = asyncio.create_task(self.server.serve(sockets=[bound]))
        while not self.server.started:
            if self.task.done():
                bound.close()
                await asyncio.gather(self.task, return_exceptions=True)
                raise ConfigError("The remote listener could not start.")
            await asyncio.sleep(0.02)
        event(
            "remote.listening",
            port=self.listener.port,
            writes=self.listener.writes,
            keys=len(self.listener.keys),
        )

    async def stop(self):
        if self.task is None:
            return
        self.server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 20)
        except BaseException:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        finally:
            self.task = None
        event("remote.stopped")
