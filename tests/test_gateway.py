"""The shared address (gateway.py): one https://HOST/mcp for every copy.

Each stand-in copy is a real uvicorn server on its own temporary unix socket,
behind the real KeyGate with its own keys (and its "gateway" key) and, when its
OAuth is on, its own SagasuOAuth built as RemoteHTTP builds it, in front of a
small app that reports what reached it. One copy is also started the way the
service starts it (RemoteHTTP). The gateway is built from its settings and
called in-process; the routing table is also written by the real
`sagasu-ctl gateway-sync` (its paths moved into a temporary folder). Never
contacts any outside service.
"""

import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import math
import os
import shutil
import socket
import tempfile
import time
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import uvicorn
from mcp.shared.auth import OAuthClientInformationFull

from sagasu import gateway
from sagasu.config import RemoteListener
from sagasu.console import ContextFilter, EventFormatter, Redact
from sagasu.oauth import SagasuOAuth, digest
from sagasu.observability import event as log_event
from sagasu.remote import KeyGate, RemoteHTTP, bind_unix, person_keys
from tests.test_oauth import CHATGPT, pkce
from tests.test_remote import DIGEST, KEY

KIT = Path(__file__).resolve().parents[1] / "deploy" / "server"
HOST = "sagasu.test"
METADATA = (
    f'resource_metadata="https://{HOST}/.well-known/oauth-protected-resource/mcp"'
)
ALEX_KEY = "sgs_alex_phone_Vq3Lw8Zr1Tn6Yb0Kc5Jm2Hx7Pd4Gs9Fe"
SPARE_KEY = "sgs_alex_spare_Rk2Wn7Bq4Zt9Lc1Xm6Jh3Py8Dv5Gs0Ua"
BLAIR_KEY = "sgs_blair_tablet_Ne5Qa2Wz8Kr3Tm7Xc1Lp6Hy4Bv9Jd0"
OWNER_SETUP_KEY = "sgs_owner_setup_Zq8Wc3Nv6Lb1Ht4Kx9Pm2Rd7Gy5Js0"
DANA_KEY = "sgs_dana_laptop_Bw6Mz1Qx4Kd9Lr3Tn8Vc2Hp7Jy5Gs0F"
ERIN_KEY = "sgs_erin_laptop_Tc4Hq9Zw2Lm7Rx1Kv6Bn3Pd8Js5Gy0A"
UNKNOWN = "sgs_nobody_Ho4Zs9Mx2Qc7Wa1Rk6Tb3Yn8Lv5Jp0Gd"
CALL = b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'
FORM = ("content-type", "application/x-www-form-urlencoded")


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def bearer(key):
    return ("authorization", "Bearer " + key)


def api_key(key):
    return ("x-api-key", key)


class Reply:
    def __init__(self, status, raw, body):
        self.status, self.raw, self.body = status, raw, body
        self.headers = {}
        for name, value in raw:
            self.headers.setdefault(name.decode().lower(), value.decode())

    def json(self):
        return json.loads(self.body)


def scope_for(path, headers=(), method="POST", host=HOST):
    path, _, query = path.partition("?")
    sent = [(b"host", host.encode())] if host else []
    sent += [(name.lower().encode(), value.encode()) for name, value in headers]
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": sent,
        "client": ("192.0.2.1", 50000),
        "server": (HOST, 80),
    }


async def ask(app, path, headers=(), body=b"", method="POST", host=HOST):
    """One request to an ASGI app in-process; headers may repeat."""
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    replies = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        replies.append(message)

    await app(scope_for(path, headers, method, host), receive, send)
    body = b"".join(message.get("body", b"") for message in replies[1:])
    return Reply(replies[0]["status"], list(replies[0]["headers"]), body)


class Pending:
    """A request in progress whose body the test hands over piece by piece."""

    def __init__(self, app, headers=(), path="/mcp", host=HOST):
        self.queue = asyncio.Queue()
        self.replies = []
        self.reads = 0  # how often the app has asked for (more of) the body
        self.task = asyncio.create_task(
            app(scope_for(path, headers, "POST", host), self.receive, self.send)
        )

    async def receive(self):
        self.reads += 1
        return await self.queue.get()

    async def send(self, message):
        self.replies.append(message)

    def body(self, data, more=False):
        self.queue.put_nowait({"type": "http.request", "body": data, "more_body": more})

    def disconnect(self):
        self.queue.put_nowait({"type": "http.disconnect"})

    async def reply(self):
        """The answer, or None when the gateway answered nothing."""
        await asyncio.wait_for(self.task, 10)
        if not self.replies:
            return None
        body = b"".join(message.get("body", b"") for message in self.replies[1:])
        return Reply(self.replies[0]["status"], list(self.replies[0]["headers"]), body)


async def until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.005)


async def open_sign_in(test, app):
    """Register an app and open its sign-in page, as ChatGPT does:
    (client_id, code verifier, sign-in request id)."""
    client = {
        "redirect_uris": [CHATGPT],
        "client_name": "ChatGPT",
        "token_endpoint_auth_method": "none",
    }
    reply = await ask(
        app,
        "/register",
        [("content-type", "application/json")],
        json.dumps(client).encode(),
    )
    test.assertEqual(reply.status, 201, reply.body)
    client_id = reply.json()["client_id"]
    verifier, challenge = pkce()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CHATGPT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "state-1",
            "resource": f"https://{HOST}/mcp",
        }
    )
    reply = await ask(app, "/authorize?" + query, method="GET")
    test.assertEqual(reply.status, 302, reply.body)
    page = reply.headers["location"]
    test.assertTrue(page.startswith(f"https://{HOST}/oauth/sign-in?"), page)
    return client_id, verifier, parse_qs(urlsplit(page).query)["request"][0]


async def post_key(app, request_id, typed):
    fields = {"request": request_id, "key": typed, "action": "allow"}
    return await ask(
        app,
        "/oauth/sign-in",
        [FORM, ("origin", f"https://{HOST}")],
        urlencode(fields).encode(),
    )


async def sign_in(test, app, key, refused=()):
    """The whole OAuth sign-in, as ChatGPT runs it; the keys in `refused` are
    typed first and must each be turned away."""
    client_id, verifier, request_id = await open_sign_in(test, app)
    for wrong in refused:
        reply = await post_key(app, request_id, wrong)
        test.assertEqual(reply.status, 400, reply.body)
        test.assertIn(b"not accepted", reply.body)
    reply = await post_key(app, request_id, key)
    test.assertEqual(reply.status, 303, reply.body)
    test.assertTrue(reply.headers["location"].startswith(CHATGPT))
    code = parse_qs(urlsplit(reply.headers["location"]).query)["code"][0]
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CHATGPT,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    reply = await ask(app, "/token", [FORM], urlencode(fields).encode())
    test.assertEqual(reply.status, 200, reply.body)
    return reply.json()


class Quiet(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # the test's loop owns signals
        yield


class Copy:
    """One person's copy: the real KeyGate in front of an app that reports
    the label the gate set, the Host, the headers and the body it got."""

    def __init__(self, name, host, directory, keys, oauth=True):
        self.name, self.host, self.keys = name, host, keys  # keys: label -> key
        self.socket = os.path.join(directory, f"{name}.sock")
        self.store = os.path.join(directory, f"{name}-oauth.sqlite3")
        self.gateway_key = f"sgs_gateway_{name}_" + sha(name)[:32]
        self.oauth_on = oauth  # the copy's own SAGASU_HTTP_OAUTH
        self.seen = []
        self.release = asyncio.Event()
        self.server = self.task = self.gate = self.oauth = None

    async def app(self, scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = {}
        for name, value in scope["headers"]:
            headers.setdefault(name.decode(), []).append(value.decode())
        seen = {
            "copy": self.name,
            "label": headers.get("x-sagasu-key-label", [None])[0],
            "host": headers.get("host", [None])[0],
            "method": scope["method"],
            "path": scope["path"],
            "query": scope["query_string"].decode(),
            "size": len(body),
            "body": body.decode() if len(body) <= 2048 else sha(body.decode()),
            "headers": headers,
        }
        self.seen.append(seen)
        if body == b"hang":
            await self.release.wait()
        payload = json.dumps(seen).encode()
        if body == b"big":
            payload = b"z" * (gateway.BODY_LIMIT + 1)
        # Like the MCP SDK's DNS-rebinding check: only its own host name.
        status = 200 if seen["host"] == self.host else 421
        headers = [
            (b"content-type", b"application/json"),
            (b"x-copy", self.name.encode()),
        ]
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": payload})

    async def start(self):
        keys = {sha(key): label for label, key in self.keys.items()}
        keys[sha(self.gateway_key)] = "gateway"
        # As RemoteHTTP builds a copy's own OAuth: never with the gateway's key.
        self.oauth = (
            SagasuOAuth(self.store, person_keys(keys), self.host, ("chatgpt.com",))
            if self.oauth_on
            else None
        )
        self.gate = KeyGate(
            self.app, keys, oauth=self.oauth, allowed_hosts=(self.host,)
        )
        # Keep every refusal's reason in gate.rejected (otherwise logged and
        # cleared at most once a minute).
        self.gate.reported_at = math.inf
        self.server = Quiet(
            uvicorn.Config(
                self.gate,
                log_config=None,
                access_log=False,
                lifespan="off",
                ws="none",
                timeout_graceful_shutdown=1,
            )
        )
        self.task = asyncio.create_task(
            self.server.serve(sockets=[bind_unix(self.socket)])
        )
        while not self.server.started:
            if self.task.done():
                raise RuntimeError(f"copy {self.name} did not start")
            await asyncio.sleep(0.01)

    async def stop(self):
        if self.task is None:
            return
        self.release.set()
        self.server.should_exit = True
        await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 10)
        self.task = None
        if self.oauth is not None:
            self.oauth.close()
            self.oauth = None

    async def restart(self, **changes):
        """Stop, change settings (keys, oauth_on, gateway_key), start again."""
        await self.stop()
        for name, value in changes.items():
            setattr(self, name, value)
        self.release = asyncio.Event()
        await self.start()


class GatewayCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Short: a unix socket path has to fit in about 100 bytes.
        self.directory = tempfile.mkdtemp(prefix="sgw", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.routes_path = os.path.join(self.directory, "gateway-routes.json")
        self.copies = {
            # The owner's copy answers to the shared host name itself.
            "sagasu": Copy("sagasu", HOST, self.directory, {"laptop": KEY}),
            "alex": Copy(
                "alex",
                "alex." + HOST,
                self.directory,
                {"phone": ALEX_KEY, "spare": SPARE_KEY},
            ),
            "blair": Copy(
                "blair", "blair." + HOST, self.directory, {"tablet": BLAIR_KEY}
            ),
        }
        for copy in self.copies.values():
            await copy.start()
            self.addAsyncCleanup(copy.stop)
        self.events = self.watch("sagasu.gateway.event")
        self.oauth_events = self.watch("sagasu.oauth.event")
        self.sent = []
        self.write_routes()
        self.app, self.oauth = self.build()

    def watch(self, target):
        patcher = patch(target)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def table(self, omit=("alex/spare",), copies=None, oauth=None):
        """The routing table as sagasu-ctl gateway-sync writes it. alex's
        "spare" key is known to alex's copy but not yet routed. `oauth`: the
        copies that allow OAuth (default: all)."""
        chosen = {name: self.copies[name] for name in (copies or self.copies)}
        allowed = set(chosen) if oauth is None else set(oauth)
        return {
            "version": 1,
            "copies": {
                name: {
                    "socket": copy.socket,
                    "host": copy.host,
                    "key": copy.gateway_key,
                    "oauth": name in allowed,
                }
                for name, copy in chosen.items()
            },
            "keys": {
                sha(key): {"copy": name, "label": label}
                for name, copy in chosen.items()
                for label, key in copy.keys.items()
                if f"{name}/{label}" not in omit
            },
        }

    def write_routes(self, table=None, raw=None):
        # Written beside and renamed into place, as sagasu-ctl does.
        text = raw if raw is not None else json.dumps(table or self.table())
        Path(self.routes_path + ".new").write_text(text)
        os.replace(self.routes_path + ".new", self.routes_path)

    def settings(self, state="state", routes=None, **extra):
        return {
            "SAGASU_GATEWAY_HOST": HOST,
            "SAGASU_GATEWAY_ROUTES": routes or self.routes_path,
            "SAGASU_GATEWAY_STATE": os.path.join(self.directory, state),
            **extra,
        }

    def build(self, state="state", **extra):
        app, oauth = gateway.build(self.settings(state, **extra))
        if oauth is not None:
            self.addCleanup(oauth.close)
        app.forward = self.record
        return app, oauth

    async def record(self, socket_path, method, target, headers, body):
        self.sent.append({"socket": socket_path, "headers": headers})
        return await gateway.forward(socket_path, method, target, headers, body)

    async def mcp(self, *headers, body=CALL, path="/mcp", app=None, **options):
        return await ask(app or self.app, path, headers, body, **options)

    def seen_counts(self):
        return {name: len(copy.seen) for name, copy in self.copies.items()}

    async def sign_in(self, key, refused=(), app=None):
        """The whole OAuth sign-in at the gateway, as ChatGPT runs it."""
        return await sign_in(self, app or self.app, key, refused)

    async def reload(self, table=None):
        """Write a table and let the gateway pick it up."""
        with patch.object(gateway, "RELOAD_INTERVAL", 0):
            self.write_routes(table)
            self.app.routes.reload()

    def assertChallenge(self, reply, invalid_token=None):
        self.assertEqual(reply.status, 401, reply.body)
        self.assertEqual(reply.json()["error"], "unauthorized")
        challenge = reply.headers["www-authenticate"]
        self.assertIn(METADATA, challenge)
        if invalid_token is not None:  # an app holding a dead token signs in again
            self.assertEqual(
                'error="invalid_token"' in challenge, invalid_token, challenge
            )


class KeyRoutingTests(GatewayCase):
    async def test_each_persons_key_reaches_only_its_own_copy_unchanged(self):
        for name, copy in self.copies.items():
            for label, key in copy.keys.items():
                if (name, label) == ("alex", "spare"):
                    continue
                for header in (bearer(key), api_key(key)):
                    with self.subTest(copy=name, header=header[0]):
                        before = self.seen_counts()
                        body = json.dumps(
                            {"id": 7, "text": f"für {name} ✓"}, ensure_ascii=False
                        ).encode()
                        reply = await self.mcp(
                            header,
                            ("content-type", "application/json"),
                            ("mcp-protocol-version", "2025-06-18"),
                            body=body,
                            path="/mcp?probe=1",
                        )
                        self.assertEqual(reply.status, 200, reply.body)
                        seen = reply.json()
                        self.assertEqual(seen["copy"], name)
                        self.assertEqual(seen["label"], label)
                        self.assertEqual(seen["host"], copy.host)
                        self.assertEqual(seen["body"], body.decode())
                        self.assertEqual(
                            (seen["path"], seen["query"]), ("/mcp", "probe=1")
                        )
                        self.assertEqual(seen["headers"][header[0]], [header[1]])
                        self.assertEqual(
                            seen["headers"]["mcp-protocol-version"], ["2025-06-18"]
                        )
                        self.assertEqual(
                            seen["headers"]["x-forwarded-proto"], ["https"]
                        )
                        self.assertNotIn("x-sagasu-caller", seen["headers"])
                        self.assertEqual(reply.headers["x-copy"], name)
                        self.assertEqual(
                            self.seen_counts(),
                            {
                                other: before[other] + (other == name)
                                for other in before
                            },
                        )

    async def test_protocol_2026_headers_reach_the_copy(self):
        # Live finding: from MCP 2026-07-28 a copy refuses (400) a request whose
        # Mcp-Method / Mcp-Name / Mcp-Param-* headers do not match its body.
        reply = await self.mcp(
            bearer(ALEX_KEY),
            ("content-type", "application/json"),
            ("mcp-protocol-version", "2026-07-28"),
            ("mcp-method", "tools/call"),
            ("mcp-name", "search_rooms"),
            ("mcp-param-date", "2026-11-20"),
        )
        self.assertEqual(reply.status, 200, reply.body)
        seen = reply.json()["headers"]
        for name, value in (
            ("mcp-protocol-version", "2026-07-28"),
            ("mcp-method", "tools/call"),
            ("mcp-name", "search_rooms"),
            ("mcp-param-date", "2026-11-20"),
        ):
            self.assertEqual(seen[name], [value], name)

    async def test_the_copy_checks_the_key_again(self):
        # A table that wrongly sends alex's key to blair gets nobody in.
        table = self.table()
        table["keys"][sha(ALEX_KEY)] = {"copy": "blair", "label": "phone"}
        with patch.object(gateway, "RELOAD_INTERVAL", 0):
            self.write_routes(table)
            reply = await self.mcp(bearer(ALEX_KEY))
        self.assertChallenge(reply)
        self.assertEqual(self.sent[-1]["socket"], self.copies["blair"].socket)
        self.assertEqual(self.seen_counts(), {"sagasu": 0, "alex": 0, "blair": 0})

    async def test_refused_requests_get_the_gateways_challenge(self):
        alex, owner = self.copies["alex"], self.copies["sagasu"]
        table = self.table()
        # Even listed by mistake, a gateway key is never routed from outside.
        table["keys"][sha(alex.gateway_key)] = {"copy": "alex", "label": "gateway"}
        cases = {
            "unknown key": [bearer(UNKNOWN)],
            "unknown x-api-key": [api_key(UNKNOWN)],
            "a gateway key": [bearer(alex.gateway_key)],
            "a gateway key in x-api-key": [api_key(alex.gateway_key)],
            "a gateway key naming a person": [
                bearer(alex.gateway_key),
                ("x-sagasu-caller", sha(ALEX_KEY)),
            ],
            "the owner's gateway key": [bearer(owner.gateway_key)],
            "a key's digest": [bearer(sha(ALEX_KEY))],
            "keys disagree": [bearer(ALEX_KEY), api_key(BLAIR_KEY)],
            "keys disagree, other way": [bearer(KEY), api_key(ALEX_KEY)],
            "two authorization headers": [bearer(ALEX_KEY), bearer(ALEX_KEY)],
            "two x-api-key headers": [api_key(ALEX_KEY), api_key(BLAIR_KEY)],
            "not bearer": [("authorization", "Basic " + ALEX_KEY)],
            "no key": [],
        }
        with patch.object(gateway, "RELOAD_INTERVAL", 0):
            self.write_routes(table)
            for name, headers in cases.items():
                with self.subTest(name):
                    reply = await self.mcp(*headers)
                    self.assertChallenge(reply)
                    self.assertEqual(reply.headers["cache-control"], "no-store")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.seen_counts(), {"sagasu": 0, "alex": 0, "blair": 0})

    async def test_a_gateway_key_routed_by_mistake_still_gets_nobody_in(self):
        # Listed under a person's label, the gateway's key does reach the copy,
        # but never with a caller: the copy refuses it.
        alex = self.copies["alex"]
        table = self.table()
        table["keys"][sha(alex.gateway_key)] = {"copy": "alex", "label": "laptop"}
        await self.reload(table)
        reply = await self.mcp(
            bearer(alex.gateway_key), ("x-sagasu-caller", sha(ALEX_KEY))
        )
        self.assertChallenge(reply)
        self.assertEqual(self.sent[-1]["socket"], alex.socket)
        self.assertNotIn(
            b"x-sagasu-caller", [name for name, _ in self.sent[-1]["headers"]]
        )
        self.assertEqual(alex.gate.rejected, {"key_removed": 1})
        self.assertEqual(alex.seen, [])

    async def test_wrong_host_gets_421_and_other_paths_404(self):
        for host in ("evil.test", "alex." + HOST, ""):
            with self.subTest(host=host):
                reply = await self.mcp(bearer(ALEX_KEY), host=host)
                self.assertEqual(reply.status, 421)
                reply = await ask(
                    self.app,
                    "/.well-known/oauth-authorization-server",
                    method="GET",
                    host=host,
                )
                self.assertEqual(reply.status, 421)
        reply = await self.mcp(bearer(ALEX_KEY), host="SAGASU.test:443")
        self.assertEqual((reply.status, reply.json()["copy"]), (200, "alex"))
        for path in (
            "/",
            "/sign-in",
            "/sign-in/start",
            "/mcp/",
            "/mcp/x",
            "/sse",
            "/MCP",
        ):
            with self.subTest(path=path):
                reply = await self.mcp(bearer(ALEX_KEY), path=path)
                self.assertEqual(reply.status, 404)
        self.assertEqual(self.seen_counts(), {"sagasu": 0, "alex": 1, "blair": 0})

    async def test_a_forged_caller_is_never_passed_on_with_a_key(self):
        for forged in (sha(SPARE_KEY), "oauth:alex/spare"):
            with self.subTest(forged=forged):
                reply = await self.mcp(
                    bearer(ALEX_KEY),
                    ("x-sagasu-caller", forged),
                    ("x-sagasu-key-label", "gateway"),
                    ("cookie", "session=1"),
                )
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.json()["label"], "phone")
                names = {name for name, _ in self.sent[-1]["headers"]}
                for dropped in (b"x-sagasu-caller", b"x-sagasu-key-label", b"cookie"):
                    self.assertNotIn(dropped, names)

    async def test_the_copy_believes_a_caller_only_with_its_gateway_key(self):
        alex, blair = self.copies["alex"], self.copies["blair"]

        async def direct(copy, *headers):
            """Straight to a copy's socket: its label for the request, or the status."""
            status, _, body = await gateway.forward(
                copy.socket,
                "POST",
                b"/mcp",
                [(b"host", copy.host.encode()), *headers],
                CALL,
            )
            if status != 200:
                return status
            seen = json.loads(body)
            self.assertNotIn("x-sagasu-caller", seen["headers"])
            return seen["label"]

        def caller(value):
            return (b"x-sagasu-caller", value.encode())

        own = (b"authorization", b"Bearer " + ALEX_KEY.encode())
        gateway_key = (b"authorization", b"Bearer " + alex.gateway_key.encode())
        # A person's own key never makes the copy read the caller header.
        self.assertEqual(await direct(alex, own, caller(sha(SPARE_KEY))), "phone")
        self.assertEqual(
            await direct(
                alex, (b"x-api-key", ALEX_KEY.encode()), caller(sha(SPARE_KEY))
            ),
            "phone",
        )
        self.assertEqual(await direct(alex, own, caller("oauth:alex/spare")), "phone")
        # The gateway's key, naming one of the copy's own keys by its digest:
        # that key's label, without the copy's name.
        self.assertEqual(
            await direct(alex, gateway_key, caller(sha(SPARE_KEY))), "oauth:spare"
        )
        self.assertEqual(
            await direct(alex, gateway_key, caller(sha(ALEX_KEY))), "oauth:phone"
        )
        self.assertEqual(alex.gate.rejected, {})
        # Anything else with the gateway's key gets nobody in.
        refused = {
            "no caller": [],
            "the old label form": [caller("oauth:alex/phone")],
            "a label": [caller("phone")],
            "an upper-case digest": [caller(sha(ALEX_KEY).upper())],
            "a digest too long": [caller(sha(ALEX_KEY) + "0")],
            "a digest cut short": [caller(sha(ALEX_KEY)[:-1])],
            "another copy's key": [caller(sha(BLAIR_KEY))],
            "an unknown key": [caller(sha(UNKNOWN))],
            "the gateway's own key": [caller(sha(alex.gateway_key))],
            "the key itself": [caller(ALEX_KEY)],
        }
        for name, headers in refused.items():
            with self.subTest(name):
                self.assertEqual(await direct(alex, gateway_key, *headers), 401)
        self.assertEqual(alex.gate.rejected, {"key_removed": len(refused)})
        # Another copy's gateway key is no key at all here.
        other = (b"authorization", b"Bearer " + blair.gateway_key.encode())
        self.assertEqual(await direct(alex, other, caller(sha(ALEX_KEY))), 401)
        # With the copy's own OAuth off, its gateway key opens nothing.
        await blair.restart(oauth_on=False)
        mine = (b"authorization", b"Bearer " + blair.gateway_key.encode())
        self.assertEqual(await direct(blair, mine, caller(sha(BLAIR_KEY))), 401)
        self.assertEqual(blair.gate.rejected, {"oauth_off": 1})
        own = (b"authorization", b"Bearer " + BLAIR_KEY.encode())
        self.assertEqual(await direct(blair, own, caller(sha(BLAIR_KEY))), "tablet")
        self.assertEqual(
            [seen["label"] for seen in alex.seen],
            ["phone", "phone", "phone", "oauth:spare", "oauth:phone"],
        )


class OAuthTests(GatewayCase):
    async def test_a_token_reaches_its_keys_copy_through_the_gateway_key(self):
        alex, blair = self.copies["alex"], self.copies["blair"]
        tokens = await self.sign_in(ALEX_KEY, refused=[alex.gateway_key, UNKNOWN])
        self.assertEqual([row[0] for row in self.oauth.store.grants()], [sha(ALEX_KEY)])
        reply = await self.mcp(
            bearer(tokens["access_token"]),
            ("x-sagasu-caller", sha(BLAIR_KEY)),
            ("x-sagasu-key-label", "oauth:tablet"),
            body=b'{"hello": "alex"}',
        )
        self.assertEqual(reply.status, 200, reply.body)
        seen = reply.json()
        self.assertEqual((seen["copy"], seen["label"]), ("alex", "oauth:phone"))
        self.assertEqual((seen["host"], seen["body"]), (alex.host, '{"hello": "alex"}'))
        self.assertEqual(
            seen["headers"]["authorization"], ["Bearer " + alex.gateway_key]
        )
        sent = self.sent[-1]["headers"]
        self.assertEqual(
            [value for name, value in sent if name == b"authorization"],
            [b"Bearer " + alex.gateway_key.encode()],
        )
        # The person's key named by its SHA-256 digest, once; never a label.
        self.assertEqual(
            [value for name, value in sent if name == b"x-sagasu-caller"],
            [sha(ALEX_KEY).encode()],
        )
        self.assertNotIn(b"x-sagasu-key-label", [name for name, _ in sent])
        self.assertNotIn(
            tokens["access_token"].encode(), b"".join(value for _, value in sent)
        )
        self.assertEqual(blair.seen, [])

    async def test_a_token_with_its_own_key_alongside_sends_the_copy_one_credential(
        self,
    ):
        await self.reload(self.table(omit=()))
        tokens = await self.sign_in(ALEX_KEY)
        reply = await self.mcp(bearer(tokens["access_token"]), api_key(ALEX_KEY))
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.json()["label"], "oauth:phone")
        names = [name for name, _ in self.sent[-1]["headers"]]
        self.assertEqual(names.count(b"authorization"), 1)
        self.assertNotIn(b"x-api-key", names)
        sent = len(self.sent)
        for other in (BLAIR_KEY, SPARE_KEY):  # another person's, or another of alex's
            with self.subTest(other=other[:10]):
                self.assertChallenge(
                    await self.mcp(bearer(tokens["access_token"]), api_key(other))
                )
        self.assertEqual(len(self.sent), sent)

    async def test_the_sign_in_page_takes_any_copys_key_and_binds_the_grant_to_it(self):
        for name, label, key in (
            ("sagasu", "laptop", KEY),
            ("blair", "tablet", BLAIR_KEY),
        ):
            with self.subTest(copy=name):
                tokens = await self.sign_in(key)
                found = self.oauth.store.token(digest(tokens["access_token"]))
                self.assertEqual(found["key_digest"], sha(key))
                self.assertEqual(
                    self.oauth.verify(tokens["access_token"]), (f"{name}/{label}", None)
                )
                reply = await self.mcp(bearer(tokens["access_token"]))
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(reply.json()["copy"], name)
                self.assertEqual(reply.json()["label"], f"oauth:{label}")
        self.assertEqual(self.copies["alex"].seen, [])

    async def test_the_copy_decides_whether_an_app_may_act_for_its_key(self):
        # The table still allows it; the copy has the last word, at once.
        alex = self.copies["alex"]
        tokens = await self.sign_in(ALEX_KEY)
        await alex.restart(oauth_on=False)
        self.assertChallenge(await self.mcp(bearer(tokens["access_token"])))
        self.assertEqual(alex.gate.rejected, {"oauth_off": 1})
        self.assertEqual((await self.mcp(bearer(ALEX_KEY))).json()["label"], "phone")
        # OAuth back on at the copy, but that key removed there.
        await alex.restart(oauth_on=True, keys={"spare": SPARE_KEY})
        self.assertChallenge(await self.mcp(bearer(tokens["access_token"])))
        self.assertEqual(alex.gate.rejected, {"key_removed": 1})
        self.assertEqual([seen["label"] for seen in alex.seen], ["phone"])
        self.assertEqual(self.sent[-1]["socket"], alex.socket)

    async def test_apps_signed_in_at_the_owners_copy_stay_signed_in(self):
        # install.sh moves the owner's copy's OAuth store to the gateway once.
        state = Path(self.directory, "moved")
        owner = SagasuOAuth(
            state / "oauth.sqlite3", {DIGEST: "laptop"}, HOST, ("chatgpt.com",)
        )
        await owner.register_client(
            OAuthClientInformationFull(
                client_id="client-1",
                redirect_uris=[CHATGPT],
                token_endpoint_auth_method="none",
            )
        )
        now = time.time()
        grant = owner.store.add_grant(
            "client-1", DIGEST, ["sagasu"], now, "App", "chatgpt.com"
        )
        tokens = owner._issue(grant, ["sagasu"], now, now)
        owner.close()
        app, _ = self.build(state="moved")
        reply = await self.mcp(bearer(tokens.access_token), app=app)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(
            (reply.json()["copy"], reply.json()["label"]), ("sagasu", "oauth:laptop")
        )
        fields = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": "client-1",
        }
        reply = await ask(app, "/token", [FORM], urlencode(fields).encode())
        self.assertEqual(reply.status, 200, reply.body)
        reply = await self.mcp(bearer(reply.json()["access_token"]), app=app)
        self.assertEqual(reply.json()["label"], "oauth:laptop")

    async def test_without_oauth_only_keys_work(self):
        app, oauth = self.build(state="off", SAGASU_GATEWAY_OAUTH="off")
        self.assertIsNone(oauth)
        self.assertEqual(
            (
                await ask(app, "/.well-known/oauth-authorization-server", method="GET")
            ).status,
            404,
        )
        self.assertEqual((await self.mcp(bearer(ALEX_KEY), app=app)).status, 200)
        reply = await self.mcp(app=app)
        self.assertEqual(
            (reply.status, reply.headers["www-authenticate"]),
            (401, 'Bearer realm="sagasu"'),
        )


class RoutesFileTests(GatewayCase):
    async def test_edits_to_the_routes_file_are_picked_up(self):
        with patch.object(gateway, "RELOAD_INTERVAL", 0):
            self.write_routes(self.table(copies=("sagasu", "alex")))
            self.assertChallenge(await self.mcp(bearer(BLAIR_KEY)))
            self.assertChallenge(await self.mcp(bearer(SPARE_KEY)))
            self.write_routes(self.table(omit=()))
            reply = await self.mcp(bearer(SPARE_KEY))
            self.assertEqual(
                (reply.json()["copy"], reply.json()["label"]), ("alex", "spare")
            )
            self.assertEqual(
                (await self.mcp(bearer(BLAIR_KEY))).json()["copy"], "blair"
            )
            self.write_routes(self.table(omit=("alex/spare",)))
            self.assertChallenge(await self.mcp(bearer(SPARE_KEY)))

    async def test_each_copy_is_read_with_its_gateway_key_and_oauth_switch(self):
        owner, alex, blair = (self.copies[name] for name in ("sagasu", "alex", "blair"))
        table = self.table(oauth=("sagasu", "blair"))
        table["copies"]["blair"][
            "key"
        ] = None  # e.g. 16 keys, no room for the gateway's
        await self.reload(table)
        routes = self.app.routes
        self.assertEqual(
            routes.copies,
            {
                "sagasu": {
                    "socket": owner.socket,
                    "host": HOST,
                    "key": owner.gateway_key,
                    "oauth": True,
                },
                "alex": {
                    "socket": alex.socket,
                    "host": alex.host,
                    "key": alex.gateway_key,
                    "oauth": False,
                },
                # OAuth on in its settings, but the gateway cannot present itself.
                "blair": {
                    "socket": blair.socket,
                    "host": blair.host,
                    "key": None,
                    "oauth": False,
                },
            },
        )
        everyone = {
            sha(KEY): "sagasu/laptop",
            sha(ALEX_KEY): "alex/phone",
            sha(BLAIR_KEY): "blair/tablet",
        }
        self.assertEqual(routes.keys, everyone)
        self.assertEqual(routes.oauth_keys, {sha(KEY): "sagasu/laptop"})
        self.assertEqual(
            routes.digests, {label: key for key, label in everyone.items()}
        )
        # The OAuth server holds the oauth_keys dict itself.
        self.assertIs(self.oauth.keys, routes.oauth_keys)
        # Every person's key is still routed; apps sign in only for the owner's copy.
        for header, (name, label) in (
            (bearer(KEY), ("sagasu", "laptop")),
            (bearer(ALEX_KEY), ("alex", "phone")),
            (api_key(BLAIR_KEY), ("blair", "tablet")),
        ):
            reply = await self.mcp(header)
            self.assertEqual(
                (reply.status, reply.json()["copy"], reply.json()["label"]),
                (200, name, label),
            )
        tokens = await self.sign_in(KEY, refused=[ALEX_KEY, BLAIR_KEY])
        self.assertEqual(
            (await self.mcp(bearer(tokens["access_token"]))).json()["label"],
            "oauth:laptop",
        )

    async def test_only_a_usable_key_and_oauth_true_let_apps_sign_in(self):
        cases = {
            # name: (changes to alex's entry, key kept, oauth)
            "on": ({}, True, True),
            "off": ({"oauth": False}, True, False),
            "no switch": ({"oauth": ...}, True, False),
            "the word true": ({"oauth": "true"}, True, False),
            "one": ({"oauth": 1}, True, False),
            "no key": ({"key": None}, False, False),
            "no key entry": ({"key": ...}, False, False),
            "empty key": ({"key": ""}, False, False),
            "key too short": ({"key": "k" * 19}, False, False),
            "key too long": ({"key": "k" * 513}, False, False),
            "key not text": ({"key": 10**30}, False, False),
            "shortest key": ({"key": "k" * 20}, True, True),
            "longest key": ({"key": "k" * 512}, True, True),
        }
        for name, (changes, kept, allowed) in cases.items():
            with self.subTest(name):
                table = self.table()
                entry = table["copies"]["alex"]
                for field, value in changes.items():
                    if value is ...:
                        del entry[field]
                    else:
                        entry[field] = value
                await self.reload(table)
                routes = self.app.routes
                found = routes.copies["alex"]
                self.assertEqual(set(found), {"socket", "host", "key", "oauth"})
                self.assertIs(found["oauth"], allowed)
                self.assertEqual(found["key"], entry.get("key") if kept else None)
                self.assertEqual(routes.keys[sha(ALEX_KEY)], "alex/phone")
                self.assertEqual(sha(ALEX_KEY) in routes.oauth_keys, allowed)
                self.assertEqual(routes.copies["blair"]["oauth"], True)

    async def test_switching_oauth_off_for_a_copy_signs_its_apps_out_for_good(self):
        alex_tokens = await self.sign_in(ALEX_KEY)
        blair_tokens = await self.sign_in(BLAIR_KEY)
        heard = []
        self.app.routes.listeners.append(heard.append)
        with patch.object(gateway, "RELOAD_INTERVAL", 0):
            self.write_routes(self.table(oauth=("sagasu", "blair")))
            self.assertChallenge(
                await self.mcp(bearer(alex_tokens["access_token"])), invalid_token=True
            )
            # Listeners get the dict the OAuth server holds: only OAuth copies' keys.
            self.assertEqual(len(heard), 1)
            self.assertIs(heard[0], self.app.routes.oauth_keys)
            self.assertEqual(
                heard[0], {sha(KEY): "sagasu/laptop", sha(BLAIR_KEY): "blair/tablet"}
            )
            self.assertEqual(
                [row[0] for row in self.oauth.store.grants()], [sha(BLAIR_KEY)]
            )
            self.assertEqual(
                (await self.mcp(bearer(blair_tokens["access_token"]))).status, 200
            )
            # alex's key itself still works, and cannot sign an app in here.
            self.assertEqual(
                (await self.mcp(bearer(ALEX_KEY))).json()["label"], "phone"
            )
            await self.sign_in(BLAIR_KEY, refused=[ALEX_KEY])
            # Turning it back on does not bring the old sign-ins back.
            self.write_routes()
            self.assertChallenge(
                await self.mcp(bearer(alex_tokens["access_token"])), invalid_token=True
            )
            self.assertEqual(len(heard), 2)
            tokens = await self.sign_in(ALEX_KEY)
            self.assertEqual(
                (await self.mcp(bearer(tokens["access_token"]))).json()["label"],
                "oauth:phone",
            )

    async def test_removing_a_key_ends_its_oauth_sign_ins_for_good(self):
        alex_tokens = await self.sign_in(ALEX_KEY)
        blair_tokens = await self.sign_in(BLAIR_KEY)
        with patch.object(gateway, "RELOAD_INTERVAL", 0):
            self.write_routes(self.table(omit=("alex/spare", "alex/phone")))
            self.assertChallenge(
                await self.mcp(bearer(alex_tokens["access_token"])), invalid_token=True
            )
            self.assertEqual(
                [row[0] for row in self.oauth.store.grants()], [sha(BLAIR_KEY)]
            )
            self.assertEqual(
                (await self.mcp(bearer(blair_tokens["access_token"]))).status, 200
            )
            # Restoring the key does not bring its old sign-ins back.
            self.write_routes()
            self.assertEqual(
                (await self.mcp(bearer(ALEX_KEY))).json()["label"], "phone"
            )
            self.assertChallenge(
                await self.mcp(bearer(alex_tokens["access_token"])), invalid_token=True
            )

    async def test_an_unreadable_or_half_written_file_keeps_the_last_good_table(self):
        tokens = await self.sign_in(ALEX_KEY)
        good = json.dumps(self.table())
        path = Path(self.routes_path)
        unusable = {
            "version": 1,
            "keys": {},
            "copies": {
                "X": {
                    "socket": self.copies["alex"].socket,
                    "host": HOST,
                    "key": "k" * 30,
                    "oauth": True,
                },
                "alex": {
                    "socket": "relative.sock",
                    "host": HOST,
                    "key": "k" * 30,
                    "oauth": True,
                },
            },
        }
        damages = {
            "half-written": lambda: path.write_text(good[: len(good) // 2]),
            "empty": lambda: path.write_text(""),
            "not a table": lambda: self.write_routes(raw="[]"),
            "another version": lambda: self.write_routes(
                raw=json.dumps({**self.table(), "version": 2})
            ),
            # A table with no copies would read as "every key removed".
            "no copies": lambda: self.write_routes(
                raw=json.dumps({"version": 1, "copies": {}, "keys": {}})
            ),
            "copies missing": lambda: self.write_routes(
                raw=json.dumps({"version": 1, "keys": {}})
            ),
            "copies not a table": lambda: self.write_routes(
                raw=json.dumps({**self.table(), "copies": []})
            ),
            "no usable copy": lambda: self.write_routes(raw=json.dumps(unusable)),
            "missing": path.unlink,
        }
        with patch.object(gateway, "RELOAD_INTERVAL", 0):
            for name, damage in damages.items():
                with self.subTest(name):
                    self.write_routes()
                    self.assertEqual((await self.mcp(bearer(BLAIR_KEY))).status, 200)
                    self.events.reset_mock()
                    damage()
                    for header in (
                        bearer(ALEX_KEY),
                        bearer(tokens["access_token"]),
                        api_key(BLAIR_KEY),
                    ):
                        self.assertEqual(
                            (await self.mcp(header)).status, 200, header[0]
                        )
                    self.assertIn(
                        sha(ALEX_KEY), [row[0] for row in self.oauth.store.grants()]
                    )
                    self.assertEqual(
                        set(self.app.routes.copies), {"sagasu", "alex", "blair"}
                    )
                    self.assertIn(sha(ALEX_KEY), self.app.routes.oauth_keys)
                    names = [call.args[0] for call in self.events.call_args_list]
                    self.assertIn("gateway.routes_unreadable", names)
                    self.assertNotIn("gateway.routes_loaded", names)
            if os.geteuid() != 0:
                # A new table the gateway cannot read yet waits until it can.
                self.write_routes(self.table(omit=("alex/spare", "blair/tablet")))
                os.chmod(self.routes_path, 0)
                self.assertEqual((await self.mcp(bearer(BLAIR_KEY))).status, 200)
                os.chmod(self.routes_path, 0o644)
                self.assertChallenge(await self.mcp(bearer(BLAIR_KEY)))
        self.assertEqual(
            (await self.mcp(bearer(tokens["access_token"]))).json()["label"],
            "oauth:phone",
        )

    async def test_build_refuses_to_start_without_a_table(self):
        tokens = await self.sign_in(ALEX_KEY)
        broken = {
            "missing": None,
            "no copies": json.dumps({"version": 1, "copies": {}, "keys": {}}),
            "half-written": json.dumps(self.table())[:40],
            "another version": json.dumps({**self.table(), "version": 2}),
            "no usable copy": json.dumps(
                {
                    "version": 1,
                    "keys": {},
                    "copies": {
                        "X": {
                            "socket": "rel.sock",
                            "host": HOST,
                            "key": "k" * 30,
                            "oauth": True,
                        }
                    },
                }
            ),
        }
        for number, (name, text) in enumerate(broken.items()):
            with self.subTest(name):
                path = Path(self.directory, f"broken-{number}.json")
                if text is not None:
                    path.write_text(text)
                with self.assertRaises(SystemExit):
                    gateway.build(
                        self.settings(state=f"broken-{number}", routes=str(path))
                    )
                self.assertFalse(Path(self.directory, f"broken-{number}").exists())
        for host in ("", "https://sagasu.test", "sagasu.test/mcp"):
            with self.subTest(host=host), self.assertRaises(SystemExit):
                gateway.build(self.settings(SAGASU_GATEWAY_HOST=host))
        # A refused start signs nobody out.
        self.assertEqual(
            self.oauth.verify(tokens["access_token"]), ("alex/phone", None)
        )


class SyncedTableTests(GatewayCase):
    """The table the real `sagasu-ctl gateway-sync` writes, as the gateway reads it."""

    def load_ctl(self):
        root = Path(self.directory, "root")
        (root / "etc/sagasu/instances").mkdir(parents=True)
        loader = SourceFileLoader(
            "sagasu_ctl_for_gateway_tests", str(KIT / "sagasu-ctl")
        )
        ctl = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(loader.name, loader)
        )
        loader.exec_module(ctl)
        ctl.OWNER_ENV = str(root / "etc/sagasu/sagasu.env")
        ctl.INSTANCES = str(root / "etc/sagasu/instances")
        ctl.GATEWAY_ROUTES = str(root / "etc/sagasu/gateway-routes.json")

        class Moved(ctl.Copy):
            def __init__(self, name=None):
                super().__init__(name)
                if name:
                    self.env = str(root / f"etc/sagasu/{name}.env")

        ctl.Copy = Moved
        # Every account exists; commands (chown) are only pretended.
        ctl.pwd = types.SimpleNamespace(
            getpwnam=lambda name: types.SimpleNamespace(pw_name=name, pw_uid=None)
        )
        ctl.subprocess = types.SimpleNamespace(
            run=lambda command, **options: types.SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
        )
        return ctl

    def settings_file(self, ctl, name, socket_path, host, keys, oauth):
        copy = ctl.Copy(None if name == "sagasu" else name)
        lines = [
            f"# Sagasu copy {name}",
            f"SAGASU_HTTP_SOCKET={socket_path}",
            f"SAGASU_HTTP_ALLOWED_HOSTS={host}",
            "SAGASU_HTTP_KEYS="
            + ",".join(f"{label}:{sha(key)}" for label, key in keys.items()),
            f"SAGASU_HTTP_OAUTH={'on' if oauth else 'off'}",
        ]
        Path(copy.env).write_text("\n".join(lines) + "\n", encoding="utf-8")
        if name != "sagasu":
            Path(ctl.INSTANCES, name).write_text(f"HOST={host}\n", encoding="utf-8")

    async def test_the_gateway_routes_by_the_table_sagasu_ctl_writes(self):
        ctl = self.load_ctl()
        owner, alex, blair = (self.copies[name] for name in ("sagasu", "alex", "blair"))
        owner_keys = {
            "laptop": KEY,
            "setup": OWNER_SETUP_KEY,
        }  # "setup" is the owner's own key
        dana_keys = {
            "laptop": DANA_KEY,
            **{
                f"k{number}": f"sgs_dana_k{number}_" + sha(f"dana{number}")[:32]
                for number in range(15)
            },
        }
        dana_socket = os.path.join(self.directory, "dana.sock")  # nobody listens there
        self.settings_file(ctl, "sagasu", owner.socket, HOST, owner_keys, oauth=True)
        self.settings_file(ctl, "alex", alex.socket, alex.host, alex.keys, oauth=True)
        self.settings_file(
            ctl, "blair", blair.socket, blair.host, blair.keys, oauth=False
        )
        self.settings_file(
            ctl, "dana", dana_socket, "dana." + HOST, dana_keys, oauth=True
        )
        with contextlib.redirect_stdout(io.StringIO()):
            ctl.gateway_sync(restart_changed=False, quiet=True)
        table = json.loads(Path(ctl.GATEWAY_ROUTES).read_text(encoding="utf-8"))
        # Each copy restarts with the gateway key sync gave it.
        await owner.restart(
            keys=owner_keys, gateway_key=table["copies"]["sagasu"]["key"]
        )
        await alex.restart(gateway_key=table["copies"]["alex"]["key"])
        await blair.restart(oauth_on=False, gateway_key=table["copies"]["blair"]["key"])
        app, oauth = self.build(state="synced", routes=ctl.GATEWAY_ROUTES)
        routes = app.routes
        self.assertEqual(
            routes.copies,
            {
                "sagasu": {
                    "socket": owner.socket,
                    "host": HOST,
                    "key": owner.gateway_key,
                    "oauth": True,
                },
                "alex": {
                    "socket": alex.socket,
                    "host": alex.host,
                    "key": alex.gateway_key,
                    "oauth": True,
                },
                "blair": {
                    "socket": blair.socket,
                    "host": blair.host,
                    "key": blair.gateway_key,
                    "oauth": False,
                },
                # Sixteen keys: no room for the gateway's, so keys only.
                "dana": {
                    "socket": dana_socket,
                    "host": "dana." + HOST,
                    "key": None,
                    "oauth": False,
                },
            },
        )
        self.assertEqual(routes.copies, table["copies"])  # read exactly as written
        expected = {
            sha(KEY): "sagasu/laptop",
            sha(OWNER_SETUP_KEY): "sagasu/setup",
            sha(ALEX_KEY): "alex/phone",
            sha(SPARE_KEY): "alex/spare",
            sha(BLAIR_KEY): "blair/tablet",
            **{sha(key): f"dana/{label}" for label, key in dana_keys.items()},
        }
        self.assertEqual(routes.keys, expected)
        self.assertEqual(
            routes.oauth_keys,
            {
                key: label
                for key, label in expected.items()
                if label.split("/")[0] in {"sagasu", "alex"}
            },
        )
        self.assertEqual(
            routes.digests, {label: key for key, label in expected.items()}
        )
        self.assertIs(oauth.keys, routes.oauth_keys)
        for header, (name, label) in (
            (bearer(KEY), ("sagasu", "laptop")),
            (bearer(OWNER_SETUP_KEY), ("sagasu", "setup")),
            (api_key(SPARE_KEY), ("alex", "spare")),
            (bearer(BLAIR_KEY), ("blair", "tablet")),
        ):
            with self.subTest(copy=name, label=label):
                reply = await self.mcp(header, app=app)
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(
                    (reply.json()["copy"], reply.json()["label"]), (name, label)
                )
        reply = await self.mcp(bearer(DANA_KEY), app=app)
        self.assertEqual(
            (reply.status, reply.json()["error"]), (503, "copy_unavailable")
        )
        self.assertEqual(self.sent[-1]["socket"], dana_socket)
        # Apps sign in only for copies with OAuth on and a gateway key.
        tokens = await self.sign_in(
            ALEX_KEY, refused=[BLAIR_KEY, DANA_KEY, owner.gateway_key], app=app
        )
        reply = await self.mcp(bearer(tokens["access_token"]), app=app)
        self.assertEqual(
            (reply.json()["copy"], reply.json()["label"]), ("alex", "oauth:phone")
        )
        self.assertEqual(
            [
                value
                for name, value in self.sent[-1]["headers"]
                if name == b"x-sagasu-caller"
            ],
            [sha(ALEX_KEY).encode()],
        )
        tokens = await self.sign_in(OWNER_SETUP_KEY, app=app)
        reply = await self.mcp(bearer(tokens["access_token"]), app=app)
        self.assertEqual(
            (reply.json()["copy"], reply.json()["label"]), ("sagasu", "oauth:setup")
        )


class LimitTests(GatewayCase):
    def add_tableonly_copies(self):
        """Copies dana and erin: in the table, but nobody listens for them."""
        table = self.table()
        for name, key in (("dana", DANA_KEY), ("erin", ERIN_KEY)):
            table["copies"][name] = {
                "socket": os.path.join(self.directory, f"{name}.sock"),
                "host": f"{name}.{HOST}",
                "key": f"sgs_gateway_{name}_" + "g" * 32,
                "oauth": True,
            }
            table["keys"][sha(key)] = {"copy": name, "label": "laptop"}
        return table

    async def hold(self, count, key):
        """Requests whose body has not arrived: each holds its place."""
        held = [Pending(self.app, [bearer(key)]) for _ in range(count)]
        await until(lambda: all(pending.reads for pending in held))
        return held

    def assertBusy(self, reply):
        self.assertEqual(
            (reply.status, reply.json()["error"]), (503, "busy"), reply.body
        )
        self.assertEqual(reply.headers["retry-after"], "1")

    async def test_the_allowances_are_counted_before_the_body_is_read(self):
        self.assertEqual(
            (gateway.MAX_IN_FLIGHT, gateway.MAX_IN_FLIGHT_PER_COPY), (64, 16)
        )
        await self.reload(self.add_tableonly_copies())
        held = await self.hold(16, ALEX_KEY)
        self.assertEqual((self.app.active, self.app.active_by_copy["alex"]), (16, 16))
        # alex's share is used up; nobody else's is.
        self.assertBusy(await self.mcp(bearer(ALEX_KEY)))
        self.assertEqual((await self.mcp(bearer(BLAIR_KEY))).json()["copy"], "blair")
        # Sixteen for each of four copies use up the shared allowance.
        for key in (KEY, BLAIR_KEY, DANA_KEY):
            held += await self.hold(16, key)
        self.assertEqual(self.app.active, 64)
        self.assertBusy(await self.mcp(bearer(ERIN_KEY)))  # erin holds none
        # Callers without a valid key are refused as before, and never counted.
        self.assertChallenge(await self.mcp(bearer(UNKNOWN)))
        self.assertChallenge(await self.mcp())
        self.assertEqual(self.app.active, 64)
        self.assertEqual(len(self.sent), 1)  # blair's alone
        # A held request whose body arrives is answered, and frees its place.
        held[0].body(CALL)
        reply = await held[0].reply()
        self.assertEqual((reply.status, reply.json()["copy"]), (200, "alex"))
        self.assertEqual((self.app.active, self.app.active_by_copy["alex"]), (63, 15))
        reply = await self.mcp(bearer(ERIN_KEY))  # gets through now, to nobody
        self.assertEqual(
            (reply.status, reply.json()["error"]), (503, "copy_unavailable")
        )
        # The rest leave without finishing: nothing is forwarded for them.
        for pending in held[1:]:
            pending.disconnect()
        for pending in held[1:]:
            self.assertIsNone(await pending.reply())
        self.assertEqual(self.app.active, 0)
        self.assertEqual(set(self.app.active_by_copy.values()), {0})
        self.assertEqual(self.seen_counts(), {"sagasu": 0, "alex": 1, "blair": 1})
        self.assertEqual(len(self.sent), 3)
        self.assertEqual((await self.mcp(bearer(ALEX_KEY))).status, 200)

    async def test_a_client_that_leaves_mid_body_sends_nothing_on(self):
        tokens = await self.sign_in(ALEX_KEY)
        for name, header in (
            ("key", bearer(ALEX_KEY)),
            ("token", bearer(tokens["access_token"])),
        ):
            with self.subTest(name):
                pending = Pending(
                    self.app, [header, ("content-type", "application/json")]
                )
                pending.body(
                    b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"na',
                    more=True,
                )
                await until(lambda: pending.reads >= 2)
                pending.disconnect()
                self.assertIsNone(await pending.reply())
                self.assertEqual(
                    (self.app.active, self.app.active_by_copy["alex"]), (0, 0)
                )
        self.assertEqual(self.sent, [])
        self.assertEqual(self.seen_counts(), {"sagasu": 0, "alex": 0, "blair": 0})

    # Regression (fixed): Gateway._forward looks up self.routes.digests[label] only after the
    # request's body has arrived (`await _read_body`). If the routing table is
    # reloaded meanwhile (another request after `sagasu-ctl key-remove`) and
    # the key is gone, the lookup raises KeyError out of the ASGI app: uvicorn
    # answers 500 and logs "Exception in ASGI application", instead of the
    # gateway answering (its own 401, or forwarding with the digest it
    # verified, for the copy to decide).
    async def test_a_key_removed_while_its_request_arrives_still_gets_an_answer(self):
        tokens = await self.sign_in(ALEX_KEY)
        pending = Pending(self.app, [bearer(tokens["access_token"])])
        pending.body(b'{"jsonrpc": "2.0", ', more=True)
        await until(lambda: pending.reads >= 2)
        with patch.object(gateway, "RELOAD_INTERVAL", 0):
            self.write_routes(self.table(omit=("alex/spare", "alex/phone")))
            self.assertEqual(
                (await self.mcp(bearer(BLAIR_KEY))).status, 200
            )  # reloads the table
        pending.body(b'"id": 1, "method": "tools/list"}')
        reply = await pending.reply()
        self.assertIsNotNone(reply)
        # Either is fine: the copy's own decision, or the gateway's refusal.
        self.assertIn(reply.status, {200, 401}, reply.body)
        if reply.status == 401:
            self.assertChallenge(reply)
        self.assertEqual(self.app.active, 0)


class CopyTroubleTests(GatewayCase):
    async def test_a_stopped_copy_gives_503_and_the_others_keep_working(self):
        blair = self.copies["blair"]
        await blair.stop()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(blair.socket)
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(blair.socket)  # a socket file nobody listens on
        stale.close()
        for state in ("nobody listening", "socket missing"):
            with self.subTest(state):
                reply = await self.mcp(bearer(BLAIR_KEY))
                self.assertEqual(reply.status, 503, reply.body)
                self.assertEqual(reply.json()["error"], "copy_unavailable")
                self.assertEqual(reply.headers["retry-after"], "30")
            with contextlib.suppress(FileNotFoundError):
                os.unlink(blair.socket)
        self.assertEqual((await self.mcp(bearer(ALEX_KEY))).status, 200)

    async def test_a_hung_copy_holds_up_only_its_own_request(self):
        alex = self.copies["alex"]
        with patch.object(gateway, "RESPONSE_SECONDS", 0.5):
            started = time.monotonic()
            hung = asyncio.create_task(self.mcp(bearer(ALEX_KEY), body=b"hang"))
            while not alex.seen:
                await asyncio.sleep(0.01)
            self.assertEqual((await self.mcp(bearer(BLAIR_KEY))).status, 200)
            self.assertFalse(hung.done())
            reply = await asyncio.wait_for(hung, 5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((reply.status, reply.json()["error"]), (504, "copy_timeout"))

    # Regression (fixed): since Python 3.11 asyncio.TimeoutError is the
    # built-in TimeoutError, a subclass of OSError, so an `except OSError`
    # placed first caught the timeout and answered 503 copy_unavailable ("not
    # running"); the 504 copy_timeout branch could never run.
    async def test_a_hung_copy_gives_504(self):
        with patch.object(gateway, "RESPONSE_SECONDS", 0.3):
            reply = await asyncio.wait_for(self.mcp(bearer(ALEX_KEY), body=b"hang"), 5)
        self.assertEqual((reply.status, reply.json()["error"]), (504, "copy_timeout"))
        # Never "retry": the copy may still be carrying the request out.
        self.assertNotIn("retry-after", reply.headers)
        names = [
            (call.args[0], call.kwargs.get("copy"))
            for call in self.events.call_args_list
        ]
        self.assertIn(("gateway.copy_timeout", "alex"), names)
        self.assertNotIn("gateway.copy_unavailable", [name for name, _ in names])

    async def test_each_failure_to_reach_a_copy_gets_its_own_answer(self):
        failures = {
            # name: (raised, status, error, retry-after, event)
            "timed out": (
                TimeoutError(),
                504,
                "copy_timeout",
                None,
                "gateway.copy_timeout",
            ),
            "asyncio timeout": (
                asyncio.TimeoutError(),
                504,
                "copy_timeout",
                None,
                "gateway.copy_timeout",
            ),
            "refused": (
                ConnectionRefusedError(),
                503,
                "copy_unavailable",
                "30",
                "gateway.copy_unavailable",
            ),
            "socket missing": (
                FileNotFoundError(),
                503,
                "copy_unavailable",
                "30",
                "gateway.copy_unavailable",
            ),
            "not allowed": (
                PermissionError(),
                503,
                "copy_unavailable",
                "30",
                "gateway.copy_unavailable",
            ),
            "unreadable answer": (
                ValueError("no response"),
                502,
                "copy_failed",
                None,
                "gateway.copy_failed",
            ),
        }
        for name, (raised, status, error, retry, logged) in failures.items():
            with self.subTest(name):

                async def fail(*args, raised=raised):
                    raise raised

                self.app.forward = fail
                self.events.reset_mock()
                reply = await self.mcp(bearer(ALEX_KEY))
                self.assertEqual((reply.status, reply.json()["error"]), (status, error))
                self.assertEqual(reply.headers.get("retry-after"), retry)
                names = [
                    (call.args[0], call.kwargs.get("copy"))
                    for call in self.events.call_args_list
                ]
                self.assertIn((logged, "alex"), names)
                self.assertEqual(
                    (self.app.active, self.app.active_by_copy["alex"]), (0, 0)
                )

    async def test_a_copys_401_becomes_the_gateways_own(self):
        # The key is still in the table but its copy no longer takes it.
        alex = self.copies["alex"]
        await alex.restart(keys={"spare": SPARE_KEY})
        reply = await self.mcp(bearer(ALEX_KEY))
        self.assertChallenge(reply)
        self.assertEqual(self.sent[-1]["socket"], alex.socket)
        self.assertNotIn("connection", reply.headers)

    async def test_bodies_over_the_limit_get_413(self):
        with patch.object(gateway, "BODY_LIMIT", 4096):
            reply = await self.mcp(bearer(ALEX_KEY), body=b"x" * 4097)
            self.assertEqual((reply.status, reply.json()["error"]), (413, "too_large"))
            self.assertEqual(self.sent, [])
            reply = await self.mcp(bearer(ALEX_KEY), body=b"y" * 4096)
            self.assertEqual(reply.status, 200, reply.body)
            self.assertEqual(reply.json()["size"], 4096)
            self.assertEqual(reply.json()["body"], sha("y" * 4096))
            # A copy's answer over the limit is not passed on either.
            reply = await self.mcp(bearer(ALEX_KEY), body=b"big")
            self.assertEqual(
                (reply.status, reply.json()["error"]), (502, "copy_failed")
            )

    async def test_hop_by_hop_response_headers_are_not_passed_on(self):
        async def answer(socket_path, method, target, headers, body):
            return (
                200,
                [
                    (b"Connection", b"keep-alive"),
                    (b"Keep-Alive", b"timeout=5"),
                    (b"Proxy-Authenticate", b"Basic"),
                    (b"TE", b"trailers"),
                    (b"Trailer", b"x-check"),
                    (b"Transfer-Encoding", b"chunked"),
                    (b"Upgrade", b"h2c"),
                    (b"Content-Length", b"999"),
                    (b"WWW-Authenticate", b'Bearer realm="copy"'),
                    (b"Content-Type", b"application/json"),
                    (b"Mcp-Session-Id", b"abc"),
                    (b"Set-Cookie", b"a=1"),
                    (b"Set-Cookie", b"b=2"),
                ],
                b'{"ok": true}',
            )

        self.app.forward = answer
        reply = await self.mcp(bearer(ALEX_KEY))
        self.assertEqual(reply.status, 200)
        names = [name.decode().lower() for name, _ in reply.raw]
        for name in (
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
            "www-authenticate",
        ):
            self.assertNotIn(name, names)
        self.assertEqual(
            [v for n, v in reply.raw if n.lower() == b"content-length"], [b"12"]
        )
        self.assertEqual(reply.headers["mcp-session-id"], "abc")
        self.assertEqual(
            [v for n, v in reply.raw if n.lower() == b"set-cookie"], [b"a=1", b"b=2"]
        )
        # End to end: the copy's own 405 (with "connection: close") passes, minus that.
        self.app.forward = self.record
        reply = await self.mcp(bearer(ALEX_KEY), method="GET", body=b"")
        self.assertEqual((reply.status, reply.headers["allow"]), (405, "POST"))
        self.assertNotIn("connection", reply.headers)

    async def test_the_gateway_never_logs_a_key_or_token(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(
            f"{record.getMessage()} {record.__dict__!r}"
        )
        root = logging.getLogger()
        levels = {
            name: logging.getLogger(name).level for name in ("", "uvicorn", "mcp")
        }
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        for name, level in levels.items():
            self.addCleanup(logging.getLogger(name).setLevel, level)
        # As main() sets them.
        root.setLevel(logging.INFO)
        logging.getLogger("uvicorn").setLevel(logging.WARNING)
        logging.getLogger("mcp").setLevel(logging.WARNING)
        tokens = await self.sign_in(
            ALEX_KEY, refused=[UNKNOWN, self.copies["alex"].gateway_key]
        )
        self.assertEqual((await self.mcp(bearer(ALEX_KEY))).status, 200)
        self.assertEqual((await self.mcp(api_key(KEY))).status, 200)
        self.assertEqual((await self.mcp(bearer(tokens["access_token"]))).status, 200)
        self.assertEqual((await self.mcp(bearer(UNKNOWN))).status, 401)
        self.assertEqual((await self.mcp(bearer(tokens["refresh_token"]))).status, 401)
        with patch.object(gateway, "RESPONSE_SECONDS", 0.3):
            self.assertEqual(
                (await self.mcp(bearer(ALEX_KEY), body=b"hang")).status, 504
            )
        await self.copies["blair"].stop()
        self.assertEqual((await self.mcp(bearer(BLAIR_KEY))).status, 503)
        with patch.object(gateway, "RELOAD_INTERVAL", 0):
            Path(self.routes_path).write_text("{")
            self.assertEqual((await self.mcp(bearer(ALEX_KEY))).status, 200)
        names = [call.args[0] for call in self.events.call_args_list]
        for expected in (
            "gateway.routes_loaded",
            "gateway.forwarded",
            "gateway.unauthorized",
            "gateway.copy_timeout",
            "gateway.copy_unavailable",
            "gateway.routes_unreadable",
        ):
            self.assertIn(expected, names)
        forwarded = [
            call.kwargs
            for call in self.events.call_args_list
            if call.args[0] == "gateway.forwarded"
        ]
        self.assertIn(
            ("alex", "phone", True),
            [(f["copy"], f["key"], f["oauth"]) for f in forwarded],
        )
        logged = "\n".join(
            [repr(self.events.mock_calls), repr(self.oauth_events.mock_calls), *records]
        )
        held = [
            KEY,
            ALEX_KEY,
            SPARE_KEY,
            BLAIR_KEY,
            UNKNOWN,
            tokens["access_token"],
            tokens["refresh_token"],
            *(copy.gateway_key for copy in self.copies.values()),
        ]
        for secret in held:
            self.assertNotIn(secret, logged)
            self.assertNotIn(secret[-16:], logged)


class StandInServer:
    """What RemoteHTTP needs of the MCP server: a list for its extra routes,
    and an app serving them beside /mcp (here a reporter in place of MCP)."""

    def __init__(self, mcp):
        self._custom_starlette_routes = []
        self.mcp = mcp

    def streamable_http_app(self, **options):
        from starlette.applications import Starlette

        others = Starlette(routes=list(self._custom_starlette_routes))

        async def app(scope, receive, send):
            if scope["type"] == "http" and scope.get("path") == gateway.MCP_PATH:
                await self.mcp(scope, receive, send)
            else:
                await others(scope, receive, send)

        return app


class CopyServiceTests(unittest.IsolatedAsyncioTestCase):
    """A copy started the way the service starts it (RemoteHTTP), with OAuth
    on, its SMU sign-in page, and a gateway key among its keys."""

    async def asyncSetUp(self):
        self.directory = tempfile.mkdtemp(prefix="sgr", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.directory, True)
        for name in ("uvicorn", "uvicorn.error", "mcp"):  # RemoteHTTP sets these
            self.addCleanup(
                logging.getLogger(name).setLevel, logging.getLogger(name).level
            )
        throttle = patch("sagasu.remote.throttle_third_party_logs")
        throttle.start()
        self.addCleanup(throttle.stop)
        self.reporter = Copy("sagasu", HOST, self.directory, {"laptop": KEY})
        self.gateway_key = self.reporter.gateway_key
        self.socket = os.path.join(self.directory, "copy.sock")
        listener = RemoteListener(
            bind="127.0.0.1",
            port=0,
            allowed_hosts=(HOST,),
            keys={DIGEST: "laptop", sha(self.gateway_key): "gateway"},
            oauth=True,
            oauth_redirect_hosts=("chatgpt.com",),
            socket=self.socket,
        )
        self.remote = RemoteHTTP(
            listener,
            StandInServer(self.reporter.app),
            oauth_store=os.path.join(self.directory, "copy-oauth.sqlite3"),
            service=types.SimpleNamespace(saved_sign_in=None),
        )
        await self.remote.start()
        self.addAsyncCleanup(self.remote.stop)
        self.gate = self.remote.server.config.app

    async def test_its_own_sign_in_pages_never_take_the_gateway_key(self):
        self.assertIsInstance(self.gate, KeyGate)
        self.assertEqual(
            self.gate.keys, {DIGEST: "laptop", sha(self.gateway_key): "gateway"}
        )
        self.assertEqual(self.remote.oauth.keys, {DIGEST: "laptop"})
        self.assertEqual(self.remote.desk.keys, {DIGEST: "laptop"})
        # The SMU sign-in page.
        form = [FORM, ("origin", f"https://{HOST}")]
        reply = await ask(
            self.gate, "/sign-in", form, urlencode({"key": self.gateway_key}).encode()
        )
        self.assertEqual(reply.status, 400, reply.body)
        self.assertIn(b"not accepted", reply.body)
        reply = await ask(self.gate, "/sign-in", form, urlencode({"key": KEY}).encode())
        self.assertEqual(
            (reply.status, reply.headers["location"]), (303, "/sign-in"), reply.body
        )
        # The copy's own OAuth sign-in (next: the SMU sign-in page).
        _, _, request_id = await open_sign_in(self, self.gate)
        reply = await post_key(self.gate, request_id, self.gateway_key)
        self.assertEqual(reply.status, 400, reply.body)
        self.assertIn(b"not accepted", reply.body)
        reply = await post_key(self.gate, request_id, KEY)
        self.assertEqual(
            (reply.status, reply.headers["location"]), (303, "/sign-in"), reply.body
        )
        self.assertEqual(self.reporter.seen, [])

    async def test_the_gateway_reaches_it_for_an_app_signed_in_there(self):
        routes = os.path.join(self.directory, "gateway-routes.json")
        Path(routes).write_text(
            json.dumps(
                {
                    "version": 1,
                    "copies": {
                        "sagasu": {
                            "socket": self.socket,
                            "host": HOST,
                            "key": self.gateway_key,
                            "oauth": True,
                        }
                    },
                    "keys": {DIGEST: {"copy": "sagasu", "label": "laptop"}},
                }
            )
        )
        app, oauth = gateway.build(
            {
                "SAGASU_GATEWAY_HOST": HOST,
                "SAGASU_GATEWAY_ROUTES": routes,
                "SAGASU_GATEWAY_STATE": os.path.join(self.directory, "gw"),
            }
        )
        self.addCleanup(oauth.close)
        tokens = await sign_in(self, app, KEY, refused=[self.gateway_key])
        reply = await ask(app, "/mcp", [bearer(tokens["access_token"])], CALL)
        self.assertEqual(reply.status, 200, reply.body)
        seen = reply.json()
        self.assertEqual(seen["label"], "oauth:laptop")
        self.assertEqual(
            seen["headers"]["authorization"], ["Bearer " + self.gateway_key]
        )
        self.assertNotIn("x-sagasu-caller", seen["headers"])
        reply = await ask(app, "/mcp", [bearer(KEY), ("x-sagasu-caller", DIGEST)], CALL)
        self.assertEqual(reply.json()["label"], "laptop")


class MainTests(unittest.TestCase):
    def test_main_logs_in_the_hosts_format_without_secrets(self):
        folder = Path(tempfile.mkdtemp(prefix="sgm", dir="/tmp"))
        self.addCleanup(shutil.rmtree, folder, True)
        routes, config = folder / "gateway-routes.json", folder / "gateway.env"
        routes.write_text(
            json.dumps(
                {
                    "version": 1,
                    "keys": {DIGEST: {"copy": "sagasu", "label": "laptop"}},
                    "copies": {
                        "sagasu": {
                            "socket": "/run/sagasu/http.sock",
                            "host": HOST,
                            "key": "sgs_" + "g" * 43,
                            "oauth": True,
                        }
                    },
                }
            )
        )
        config.write_text(
            f"SAGASU_GATEWAY_HOST={HOST}\nSAGASU_GATEWAY_SOCKET={folder / 'gw.sock'}\n"
            f"SAGASU_GATEWAY_ROUTES={routes}\nSAGASU_GATEWAY_STATE={folder / 'state'}\n"
            "SAGASU_GATEWAY_OAUTH=on\n"
        )
        root = logging.getLogger()
        before = list(root.handlers)
        self.addCleanup(root.setLevel, root.level)
        for name in ("uvicorn", "uvicorn.error", "mcp"):
            self.addCleanup(
                logging.getLogger(name).setLevel, logging.getLogger(name).level
            )
        stream = io.StringIO()
        try:
            with (
                contextlib.redirect_stderr(stream),
                patch("sagasu.remote.bind_unix", return_value=object()),
                patch("sagasu.remote.throttle_third_party_logs"),
                patch("uvicorn.Server"),
            ):
                gateway.main(["--config", str(config)])
        finally:
            added = [handler for handler in root.handlers if handler not in before]
            for handler in added:
                self.addCleanup(root.removeHandler, handler)
        self.assertEqual(len(added), 1)
        handler = added[0]
        self.assertIsInstance(handler, logging.StreamHandler)
        self.assertIs(handler.stream, stream)
        self.assertTrue(
            any(isinstance(item, ContextFilter) for item in handler.filters)
        )
        self.assertTrue(any(isinstance(item, Redact) for item in handler.filters))
        self.assertIsInstance(handler.formatter, EventFormatter)
        self.assertFalse(handler.formatter.structured)
        self.assertEqual(root.level, logging.INFO)
        # Each line keeps its event's fields...
        log_event(
            "gateway.forwarded", copy="alex", key="phone", oauth=True, status=200, ms=3
        )
        log_event(
            "gateway.unauthorized",
            level=logging.WARNING,
            requests=2,
            reasons={"unknown_key": 1, "unknown_token": 1},
        )
        # ...and what other libraries write is cleaned like the host's own log.
        logging.getLogger("uvicorn.error").error(
            "Bad request from someone@example.test for https://%s/authorize?code=abc123secret",
            HOST,
        )
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("uvicorn.error").exception(
                "Exception in ASGI application"
            )
        text = stream.getvalue()
        self.assertRegex(
            text,
            r"INFO sagasu\.observability +gateway\.listening host=\"sagasu\.test\" oauth=true",
        )
        self.assertRegex(
            text,
            r"gateway\.forwarded copy=\"alex\" key=\"phone\" oauth=true status=200 ms=3",
        )
        # (Redact hides the value of any field named like "token", here a count.)
        self.assertRegex(
            text,
            r"WARNING sagasu\.observability +gateway\.unauthorized requests=2 "
            r"reasons=\{\"unknown_key\": 1, \"unknown_token\": ",
        )
        self.assertIn("<redacted-email>", text)
        self.assertIn(f"https://{HOST}/authorize?<redacted>", text)
        self.assertIn("(RuntimeError; raw traceback suppressed)", text)
        for leaked in ("someone@example.test", "abc123secret", "Traceback", "boom"):
            self.assertNotIn(leaked, text)


if __name__ == "__main__":
    unittest.main()
