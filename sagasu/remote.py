"""The host's optional remote listener: MCP over streamable HTTP, on loopback only.

A TLS reverse proxy on the same machine publishes it. Every request must carry
an API key; only SHA-256 digests of keys are configured, so the host's
configuration never contains a usable key. With SAGASU_HTTP_OAUTH=on an app may
instead present an OAuth access token, obtained by typing a key once on this
server's sign-in page (see oauth.py). SMU sign-in stays with the person at the
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
from pathlib import Path

from .config import ConfigError
from .oauth import CLIENT_ADDRESS, PUBLIC_PATHS
from .observability import event

MCP_PATH = "/mcp"
# Requests being served at once, counted only after the key has matched, so
# callers without a key cannot use up the allowance.
MAX_IN_FLIGHT = 32


# Some agent platforms reserve Authorization for their own use and only allow
# custom headers. They may send the key, on its own, in this header instead.
ALTERNATIVE_KEY_HEADER = "x-api-key"
# Set by the gate for the MCP server's per-call log line. A client's own copy of
# this header is always removed first.
LABEL_HEADER = "x-sagasu-key-label"
TOKEN_REASONS = frozenset({"unknown_token", "expired_token", "key_removed"})


def check_key(keys, headers):
    """Return (label, None) for a configured API key, else (None, why not).

    The key is either `Authorization: Bearer <key>` or `X-API-Key: <key>`. The
    reason is a fixed category for the private log; it never includes a value.
    """
    try:
        value = headers.get("authorization") or ""
        alternative = headers.get(ALTERNATIVE_KEY_HEADER) or ""
    except AttributeError:
        return None, "no_key_header"
    if value and alternative:
        return None, "several_key_headers"
    if not value and not alternative:
        return None, "no_key_header"
    scheme, _, token = (value or alternative).strip().partition(" ")
    if scheme.lower() != "bearer":
        if value:
            return None, "not_a_bearer_key"
        # X-API-Key normally carries the key alone; "Bearer <key>" pasted
        # there by habit means the same thing.
        token = alternative
    token = token.strip()
    if not 20 <= len(token) <= 512:
        return None, "wrong_length"
    digest = hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()
    found = None
    for known, label in keys.items():
        # Compare against every configured digest; no early exit.
        if hmac.compare_digest(known, digest):
            found = label
    return found, None if found else "unknown_key"


def key_label(keys, headers):
    """Return the label of the API key a request carries, else None."""
    return check_key(keys, headers)[0]


class KeyGate:
    """ASGI wrapper: nothing behind it runs without a configured API key.

    With OAuth on, an access token from the sign-in flow counts as the key it
    was issued for, and the OAuth paths themselves are served without one.
    """

    def __init__(self, app, keys, oauth=None, allowed_hosts=()):
        self.app = app
        self.keys = dict(keys)
        self.oauth = oauth
        self.allowed_hosts = frozenset(allowed_hosts)
        self.active = 0
        self.rejected = {}
        self.reported_at = 0.0

    def credentials(self, headers):
        """Return (label, None) or (None, reason) for a request's headers."""
        bearer = [value for name, value in headers if name == b"authorization"]
        alternative = [
            value for name, value in headers if name == ALTERNATIVE_KEY_HEADER.encode()
        ]
        if len(bearer) > 1 or len(alternative) > 1:
            return None, "several_key_headers"
        labels = []
        if bearer:
            value = bearer[0].decode("latin-1")
            label, reason = check_key(self.keys, {"authorization": value})
            if label is None and reason == "unknown_key" and self.oauth is not None:
                label, reason = self.oauth.verify(
                    value.strip().partition(" ")[2].strip()
                )
            if label is None:
                return None, reason
            labels.append(label)
        if alternative:
            label, reason = check_key(
                self.keys, {ALTERNATIVE_KEY_HEADER: alternative[0].decode("latin-1")}
            )
            if label is None:
                return None, reason
            labels.append(label)
        if not labels:
            return None, "no_key_header"
        if len(labels) > 1 and self.oauth is None:
            return None, "several_key_headers"
        # With OAuth an app may send its token alongside a key header, but only
        # when both name the same key, so the log is never ambiguous.
        if len(set(labels)) > 1:
            return None, "keys_disagree"
        return labels[0], None

    def host_allowed(self, headers):
        host = next((value for name, value in headers if name == b"host"), b"")
        name = host.decode("latin-1").lower().rsplit(":", 1)[0] if host else ""
        return name in self.allowed_hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = [(name.lower(), value) for name, value in scope.get("headers") or ()]
        if self.oauth is not None and scope.get("path") in PUBLIC_PATHS:
            if not self.host_allowed(headers):
                await self.reply(
                    send,
                    421,
                    "misdirected",
                    "Unknown host.",
                    (b"cache-control", b"no-store"),
                )
                return
            forwarded = next(
                (value for name, value in headers if name == b"x-forwarded-for"), b""
            )
            # Caddy replaces X-Forwarded-For with the address it saw.
            address = forwarded.decode("latin-1").split(",")[0].strip()
            token = CLIENT_ADDRESS.set(
                address or (scope.get("client") or ("unknown",))[0]
            )
            try:
                await self.app(scope, receive, send)
            finally:
                CLIENT_ADDRESS.reset(token)
            return
        label, reason = self.credentials(headers)
        if label is None:
            self.rejected[reason] = self.rejected.get(reason, 0) + 1
            now = time.monotonic()
            if now - self.reported_at >= 60:
                # At most one line a minute, so probing cannot flood the log.
                # It says why keys were refused, never what was sent.
                event(
                    "remote.unauthorized",
                    level=logging.WARNING,
                    requests=sum(self.rejected.values()),
                    reasons=dict(self.rejected),
                )
                self.reported_at, self.rejected = now, {}
            challenge = 'Bearer realm="sagasu"'
            if self.oauth is not None:
                challenge += f', resource_metadata="{self.oauth.resource_metadata_url}"'
                if reason in TOKEN_REASONS:
                    challenge += ', error="invalid_token"'
            await self.reply(
                send,
                401,
                "unauthorized",
                "Send one valid API key: Authorization: Bearer <key>, or X-API-Key: <key>.",
                (b"www-authenticate", challenge.encode()),
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
            scope = dict(
                scope,
                headers=[
                    (name, value)
                    for name, value in scope.get("headers") or ()
                    if name.lower() != LABEL_HEADER.encode()
                ]
                + [(LABEL_HEADER.encode(), label.encode())],
            )
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

    def __init__(self, listener, server, oauth_store=None):
        self.listener = listener
        self.mcp_server = server
        self.oauth_store = oauth_store
        self.oauth = None
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
        # Everything the web server logs about a request before the key is
        # checked is a warning. None of it is needed, and a caller without a
        # key must not be able to write to the private log at all.
        logging.getLogger("uvicorn.error").setLevel(logging.ERROR)
        throttle_third_party_logs()
        if (
            not self.listener.oauth
            and self.oauth_store
            and Path(self.oauth_store).exists()
        ):
            # Turning OAuth off signs every app out, so turning it on later
            # does not bring old sign-ins back.
            from .oauth import OAuthStore

            store = OAuthStore(self.oauth_store)
            store.clear()
            store.close()
        if self.listener.oauth:
            from .oauth import SagasuOAuth

            self.oauth = SagasuOAuth(
                self.oauth_store,
                self.listener.keys,
                self.listener.allowed_hosts[0],
                self.listener.oauth_redirect_hosts,
            )
            # The SDK serves extra routes from this list alongside /mcp.
            self.mcp_server._custom_starlette_routes.extend(self.oauth.routes())
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
                KeyGate(
                    app,
                    self.listener.keys,
                    oauth=self.oauth,
                    allowed_hosts=self.listener.allowed_hosts,
                ),
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
                if self.oauth is not None:
                    self.oauth.close()
                raise ConfigError("The remote listener could not start.")
            await asyncio.sleep(0.02)
        event(
            "remote.listening",
            port=self.listener.port,
            writes=self.listener.writes,
            keys=len(self.listener.keys),
            oauth=self.oauth is not None,
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
            if self.oauth is not None:
                self.oauth.close()
                self.oauth = None
        event("remote.stopped")
