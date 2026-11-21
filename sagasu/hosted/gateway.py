"""One address for every copy on this server.

Hosted path only (see SETUP.md, "Hosting for a group"): it runs as its own
service (python -m sagasu.gateway) in front of the copies.

Each person has their own copy of Sagasu (own account, browser, saved sign-in),
each listening on its own private socket. The gateway publishes all of them at
one address, https://HOST/mcp, and sends each request to the copy of the person
whose key it carries:

- A request with an API key (Authorization: Bearer or X-API-Key) goes to the
  copy that key belongs to, unchanged. The copy checks the key again, so the
  gateway alone cannot get anyone into a copy with a key.
- An app that signed in through OAuth (ChatGPT, Claude's "Sign in") holds a
  token from the gateway's own sign-in page, where the person typed their key
  once. Its requests go to that key's copy, presented with the gateway's own
  key for that copy and the person's key named by its SHA-256 digest
  (X-Sagasu-Caller). The copy decides: only one of its own keys, with its own
  OAuth switched on, counts (remote.KeyGate).

The routing table (which key digest belongs to which copy, each copy's socket
and host name, and the gateway's key for each copy) is written by
`sagasu-ctl gateway-sync` as root and re-read when it changes. It holds no
person's key, only digests. The gateway runs as its own account with no
browser; it can reach the copies' sockets, and nothing else of theirs.

The SMU sign-in page stays with each copy: links an agent gives a person open
the page on that person's own copy address.

    python -m sagasu.gateway --config /etc/sagasu/gateway.env
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path

from ..observability import event
from .oauth import CLIENT_ADDRESS, PUBLIC_PATHS
from .remote import ALTERNATIVE_KEY_HEADER, MCP_PATH, check_key

CALLER_HEADER = "x-sagasu-caller"
GATEWAY_LABEL = "gateway"
BODY_LIMIT = 4 * 1024 * 1024
MAX_IN_FLIGHT = 64
# Below a copy's own limit (remote.MAX_IN_FLIGHT), so one copy's stall never
# uses up the shared allowance.
MAX_IN_FLIGHT_PER_COPY = 16
DISCONNECTED = object()
CONNECT_SECONDS = 10
# A copy's own waits last up to 30 seconds (wait_operation) or 25 (login).
RESPONSE_SECONDS = 110
RELOAD_INTERVAL = 1.0
COPY_NAME = re.compile(r"[a-z][a-z0-9]{1,15}")
# Request headers a copy needs; everything else stays at the gateway.
FORWARDED = frozenset(
    {
        b"content-type",
        b"accept",
        b"accept-encoding",
        b"mcp-protocol-version",
        b"mcp-session-id",
        b"last-event-id",
        b"user-agent",
    }
)
HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
        b"content-length",
    }
)


class Routes:
    """The routing table, re-read whenever its file changes."""

    def __init__(self, path, clock=time.monotonic):
        self.path = Path(path)
        self.clock = clock
        self.copies = {}  # name -> {"socket", "host", "key" or None, "oauth"}
        self.keys = {}  # sha256 digest of a person's key -> "copy/label"
        # The keys apps may sign in with here: those of copies with OAuth on
        # that the gateway can present itself to. The OAuth server holds this
        # dict; it is updated in place.
        self.oauth_keys = {}
        self.digests = {}  # "copy/label" -> digest
        self.signature = None
        self.checked = None
        self.listeners = []  # called with the new oauth_keys after a change
        self.reload(force=True)

    def reload(self, force=False):
        now = self.clock()
        if (
            not force
            and self.checked is not None
            and now - self.checked < RELOAD_INTERVAL
        ):
            return
        self.checked = now
        try:
            info = self.path.stat()
            signature = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            signature = None
        if not force and signature == self.signature:
            return
        self.signature = signature
        copies, keys = {}, {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError()
            for name, copy in (data.get("copies") or {}).items():
                if (
                    COPY_NAME.fullmatch(name)
                    and isinstance(copy, dict)
                    and isinstance(copy.get("socket"), str)
                    and copy["socket"].startswith("/")
                    and isinstance(copy.get("host"), str)
                ):
                    key = copy.get("key")
                    usable = isinstance(key, str) and 20 <= len(key) <= 512
                    copies[name] = {
                        "socket": copy["socket"],
                        "host": copy["host"],
                        "key": key if usable else None,
                        # Apps may sign in for this copy here only if the copy
                        # allows OAuth and the gateway can present itself.
                        "oauth": copy.get("oauth") is True and usable,
                    }
            seen = {}
            for digest, owner in (data.get("keys") or {}).items():
                if (
                    isinstance(owner, dict)
                    and re.fullmatch(r"[0-9a-f]{64}", digest or "")
                    and owner.get("copy") in copies
                    and isinstance(owner.get("label"), str)
                    and owner["label"] != GATEWAY_LABEL
                ):
                    seen[digest] = f"{owner['copy']}/{owner['label']}"
            keys = seen
            if not copies:
                # sagasu-ctl never writes a table without the owner's copy.
                raise ValueError("no copies")
        except (OSError, ValueError, TypeError, AttributeError):
            # Unreadable (or caught mid-write): keep the last good table. An
            # empty one would read as "every key removed" and sign every app
            # out for good.
            event("gateway.routes_unreadable", level=logging.WARNING)
            self.signature = None  # try again on the next check
            return
        oauth_keys = {
            digest: label
            for digest, label in keys.items()
            if copies[label.partition("/")[0]]["oauth"]
        }
        self.copies = copies
        self.keys.clear()
        self.keys.update(keys)
        self.digests = {label: digest for digest, label in keys.items()}
        if oauth_keys != self.oauth_keys:
            self.oauth_keys.clear()
            self.oauth_keys.update(oauth_keys)
            for listener in self.listeners:
                listener(self.oauth_keys)
        event(
            "gateway.routes_loaded",
            copies=len(copies),
            keys=len(keys),
            oauth_keys=len(oauth_keys),
        )

    def copy_of(self, label):
        name = label.partition("/")[0]
        return name, self.copies.get(name)


def _credentials(routes, oauth, headers):
    """(label, by_token) for a request, or (None, reason)."""
    bearer = [value for name, value in headers if name == b"authorization"]
    alternative = [
        value for name, value in headers if name == ALTERNATIVE_KEY_HEADER.encode()
    ]
    if len(bearer) > 1 or len(alternative) > 1:
        return None, "several_key_headers"
    found = []
    if bearer:
        value = bearer[0].decode("latin-1")
        label, reason = check_key(routes.keys, {"authorization": value})
        by_token = False
        if label is None and reason == "unknown_key" and oauth is not None:
            label, reason = oauth.verify(value.strip().partition(" ")[2].strip())
            by_token = label is not None
        if label is None:
            return None, reason
        found.append((label, by_token))
    if alternative:
        label, reason = check_key(
            routes.keys, {ALTERNATIVE_KEY_HEADER: alternative[0].decode("latin-1")}
        )
        if label is None:
            return None, reason
        found.append((label, False))
    if not found:
        return None, "no_key_header"
    if len({label for label, _ in found}) > 1:
        return None, "keys_disagree"
    return found[0][0], any(by_token for _, by_token in found)


async def forward(socket_path, method, target, headers, body):
    """One HTTP/1.1 request to a copy's socket; (status, headers, body)."""
    import h11

    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(socket_path, limit=BODY_LIMIT + 65536),
        CONNECT_SECONDS,
    )
    try:
        connection = h11.Connection(h11.CLIENT)
        writer.write(
            connection.send(
                h11.Request(
                    method=method,
                    target=target,
                    headers=headers + [(b"content-length", str(len(body)).encode())],
                )
            )
            + connection.send(h11.Data(data=body))
            + connection.send(h11.EndOfMessage())
        )
        await writer.drain()
        response, chunks, size = None, [], 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESPONSE_SECONDS
        while True:
            item = connection.next_event()
            if item is h11.NEED_DATA:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                data = await asyncio.wait_for(reader.read(65536), remaining)
                connection.receive_data(data)
                continue
            if isinstance(item, h11.Response):
                response = item
            elif isinstance(item, h11.Data):
                size += len(item.data)
                if size > BODY_LIMIT:
                    raise ValueError("response too large")
                chunks.append(bytes(item.data))
            elif isinstance(item, (h11.EndOfMessage, h11.ConnectionClosed)):
                break
        if response is None:
            raise ValueError("no response")
        return response.status_code, list(response.headers), b"".join(chunks)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


class Gateway:
    """ASGI app: OAuth paths to the gateway's own sign-in server, /mcp to the
    copy of the person whose key a request carries, and nothing else."""

    def __init__(self, routes, host, oauth=None, oauth_app=None, forwarder=forward):
        self.routes = routes
        self.host = host
        self.oauth = oauth
        self.oauth_app = oauth_app
        self.forward = forwarder
        self.active = 0
        self.active_by_copy = {}
        self.rejected = {}
        self.reported_at = 0.0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            if self.oauth_app is not None:
                await self.oauth_app(scope, receive, send)
            else:
                await _lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        self.routes.reload()
        headers = [(name.lower(), value) for name, value in scope.get("headers") or ()]
        host = next((value for name, value in headers if name == b"host"), b"")
        if host.decode("latin-1").lower().rsplit(":", 1)[0] != self.host:
            await reply(send, 421, "misdirected", "Unknown host.")
            return
        path = scope.get("path") or ""
        address = _address(scope, headers)
        if self.oauth_app is not None and path in PUBLIC_PATHS:
            token = CLIENT_ADDRESS.set(address)
            try:
                await self.oauth_app(scope, receive, send)
            finally:
                CLIENT_ADDRESS.reset(token)
            return
        if path != MCP_PATH:
            await reply(send, 404, "not_found", "Not found.")
            return
        label, by_token = _credentials(self.routes, self.oauth, headers)
        if label is None:
            await self._refuse(send, by_token)
            return
        name, copy = self.routes.copy_of(label)
        if copy is None or (by_token and not copy["oauth"]):
            await self._refuse(send, "unknown_key")
            return
        if (
            self.active >= MAX_IN_FLIGHT
            or self.active_by_copy.get(name, 0) >= MAX_IN_FLIGHT_PER_COPY
        ):
            # Per copy too: one person's stalled requests never hold up
            # everyone else's.
            await reply(
                send,
                503,
                "busy",
                "Too many requests are in progress. Retry shortly.",
                (b"retry-after", b"1"),
            )
            return
        # Taken now: the table may change while the body is still arriving.
        caller = self.routes.digests.get(label) if by_token else None
        if by_token and caller is None:
            await self._refuse(send, "key_removed")
            return
        self.active += 1
        self.active_by_copy[name] = self.active_by_copy.get(name, 0) + 1
        try:
            await self._forward(
                scope,
                receive,
                send,
                headers,
                path,
                address,
                label,
                by_token,
                name,
                copy,
                caller,
            )
        finally:
            self.active -= 1
            self.active_by_copy[name] -= 1

    async def _forward(
        self,
        scope,
        receive,
        send,
        headers,
        path,
        address,
        label,
        by_token,
        name,
        copy,
        caller,
    ):
        body = await _read_body(receive)
        if body is DISCONNECTED:
            return  # the client went away mid-request: send nothing on
        if body is None:
            await reply(send, 413, "too_large", "The request is too large.")
            return
        # Every Mcp-* header too: from protocol 2026-07-28 a copy checks that
        # Mcp-Method, Mcp-Name and Mcp-Param-* match the body (else 400).
        sent = [
            (name_, value)
            for name_, value in headers
            if name_ in FORWARDED or name_.startswith(b"mcp-")
        ]
        sent += [
            (b"host", copy["host"].encode()),
            (b"x-forwarded-for", address.encode("latin-1", "replace")),
            (b"x-forwarded-proto", b"https"),
        ]
        if by_token:
            # The copy does not know the gateway's tokens: present the
            # gateway's own key for it, and name the person's key by its
            # digest. The copy decides: only one of its own keys counts.
            sent += [
                (b"authorization", b"Bearer " + copy["key"].encode()),
                (CALLER_HEADER.encode(), caller.encode()),
            ]
        else:
            # The person's own key, unchanged: the copy checks it again.
            sent += [
                (name_, value)
                for name_, value in headers
                if name_ in {b"authorization", ALTERNATIVE_KEY_HEADER.encode()}
            ]
        target = scope.get("raw_path") or path.encode()
        if scope.get("query_string"):
            target += b"?" + scope["query_string"]
        started = time.monotonic()
        try:
            status, answer, content = await self.forward(
                copy["socket"], scope.get("method", "POST"), target, sent, body
            )
        except TimeoutError:
            # Before OSError, of which it is a kind. The copy is running and
            # may still be carrying the request out: never "retry".
            event("gateway.copy_timeout", level=logging.WARNING, copy=name)
            await reply(
                send,
                504,
                "copy_timeout",
                "The copy did not answer in time. Check the result before repeating anything.",
            )
            return
        except OSError:
            event("gateway.copy_unavailable", level=logging.WARNING, copy=name)
            await reply(
                send,
                503,
                "copy_unavailable",
                "This person's Sagasu copy is not running. Retry in a minute.",
                (b"retry-after", b"30"),
            )
            return
        except Exception as error:
            event(
                "gateway.copy_failed",
                level=logging.WARNING,
                copy=name,
                error_type=type(error).__name__,
            )
            await reply(
                send, 502, "copy_failed", "The copy's answer could not be read."
            )
            return
        details = {}
        if status >= 400 and status != 401:
            # Why the copy refused it: its own error text, the request's
            # header names (never values) and its JSON-RPC method.
            details = {
                "copy_error": _error_text(content),
                "header_names": sorted(
                    {name_.decode("latin-1") for name_, _ in headers}
                ),
                "rpc": _rpc_method(body),
                "content_type": next(
                    (
                        value.decode("latin-1")[:60]
                        for name_, value in headers
                        if name_ == b"content-type"
                    ),
                    None,
                ),
            }
        event(
            "gateway.forwarded",
            copy=name,
            key=label.partition("/")[2],
            oauth=by_token,
            status=status,
            ms=int((time.monotonic() - started) * 1000),
            **details,
        )
        if status == 401:
            # Stale routes (a key removed a moment ago): the gateway's own
            # challenge, which points apps at the gateway's sign-in.
            await self._refuse(send, "unknown_key")
            return
        kept = [
            (name_.lower(), value)
            for name_, value in answer
            if name_.lower() not in HOP_BY_HOP and name_.lower() != b"www-authenticate"
        ]
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": kept + [(b"content-length", str(len(content)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": content})

    async def _refuse(self, send, reason):
        reason = reason if isinstance(reason, str) else "unknown"
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        now = time.monotonic()
        if now - self.reported_at >= 60:
            # At most one line a minute; why keys were refused, never what.
            event(
                "gateway.unauthorized",
                level=logging.WARNING,
                requests=sum(self.rejected.values()),
                reasons=dict(self.rejected),
            )
            self.reported_at, self.rejected = now, {}
        challenge = 'Bearer realm="sagasu"'
        if self.oauth is not None:
            challenge += f', resource_metadata="{self.oauth.resource_metadata_url}"'
            if reason in {"unknown_token", "expired_token", "key_removed"}:
                challenge += ', error="invalid_token"'
        await reply(
            send,
            401,
            "unauthorized",
            "Send one valid API key: Authorization: Bearer <key>, or X-API-Key: <key>.",
            (b"www-authenticate", challenge.encode()),
        )


def _error_text(content):
    """A copy's error message, short; never more than the copy itself says."""
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            error = data.get("error")
            text = (
                error.get("message") if isinstance(error, dict) else data.get("message")
            )
            return str(text or "")[:200]
    except ValueError:
        pass
    return content[:120].decode("utf-8", "replace")


def _rpc_method(body):
    try:
        data = json.loads(body)
    except ValueError:
        return f"unparsable ({len(body)} bytes)"
    if isinstance(data, list):
        return f"batch of {len(data)}"
    if isinstance(data, dict):
        return str(data.get("method") or "response")[:60]
    return type(data).__name__


def _address(scope, headers):
    forwarded = next(
        (value for name, value in headers if name == b"x-forwarded-for"), b""
    )
    # Caddy replaces X-Forwarded-For with the address it saw.
    address = forwarded.decode("latin-1").split(",")[0].strip()
    return address or (scope.get("client") or ("unknown",))[0]


async def _read_body(receive):
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            # Never forward part of a request as if it were whole.
            return DISCONNECTED
        size += len(message.get("body", b""))
        if size > BODY_LIMIT:
            return None
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(chunks)


async def _lifespan(receive, send):
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def reply(send, status, error, message, header=None):
    body = json.dumps({"error": error, "message": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                *([header] if header else []),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build(settings):
    """The gateway app from its settings (a dict of SAGASU_GATEWAY_* values)."""
    host = (settings.get("SAGASU_GATEWAY_HOST") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9.-]{1,253}", host):
        raise SystemExit("SAGASU_GATEWAY_HOST must be the public host name.")
    routes = Routes(
        settings.get("SAGASU_GATEWAY_ROUTES") or "/etc/sagasu/gateway-routes.json"
    )
    if not routes.copies:
        # Never start on an empty or unreadable table: the OAuth server would
        # take it as every key removed. systemd tries again shortly.
        raise SystemExit(
            "The routing table is missing or unreadable: run sagasu-ctl gateway-sync."
        )
    oauth = oauth_app = None
    if (settings.get("SAGASU_GATEWAY_OAUTH") or "on").strip().lower() == "on":
        from starlette.applications import Starlette

        from ..config import DEFAULT_OAUTH_REDIRECT_HOSTS
        from .oauth import SagasuOAuth

        state = Path(settings.get("SAGASU_GATEWAY_STATE") or ".")
        redirect_hosts = [
            item.strip().lower()
            for item in (
                settings.get("SAGASU_OAUTH_REDIRECT_HOSTS")
                or DEFAULT_OAUTH_REDIRECT_HOSTS
            ).split(",")
            if item.strip()
        ]
        # Only keys of copies that allow OAuth: switching OAuth off on a copy,
        # or removing a key, signs out every app that signed in with it here.
        oauth = SagasuOAuth(
            state / "oauth.sqlite3", routes.oauth_keys, host, redirect_hosts
        )
        routes.listeners.append(lambda keys: oauth.store.keep_only_keys(keys))
        oauth_app = Starlette(routes=oauth.routes())
    return Gateway(routes, host, oauth=oauth, oauth_app=oauth_app), oauth


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m sagasu.gateway")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    from dotenv import dotenv_values

    settings = {
        key: value
        for key, value in dotenv_values(args.config, interpolate=False).items()
        if value is not None
    }
    from ..console import ContextFilter, EventFormatter, Redact

    # The host's own log format, so each line keeps its fields (copy, status,
    # why a key was refused); never a key or a token.
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.addFilter(Redact())
    handler.setFormatter(EventFormatter(structured=False))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    app, oauth = build(settings)
    socket_path = (
        settings.get("SAGASU_GATEWAY_SOCKET") or "/run/sagasu-gateway/http.sock"
    )

    import uvicorn

    from .remote import bind_unix, throttle_third_party_logs

    for name in ("uvicorn", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.ERROR)
    throttle_third_party_logs()
    listener = bind_unix(socket_path)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="on",
            proxy_headers=False,
            server_header=False,
            ws="none",
            timeout_graceful_shutdown=5,
        )
    )
    event("gateway.listening", host=app.host, oauth=oauth is not None)
    try:
        server.run(sockets=[listener])
    finally:
        if oauth is not None:
            oauth.close()
        with contextlib.suppress(OSError):
            os.unlink(socket_path)


if __name__ == "__main__":
    main()
