"""OAuth sign-in for the remote listener: discovery, registration, the sign-in
page, tokens, refresh, revocation and how they relate to API keys.

Runs a real host with a fake FBS backend and a standard-library HTTP client.
Never contacts FBS, Microsoft or any outside service.
"""

import asyncio
import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from sagasu.config import Settings, parse_remote_listener
from sagasu.host import serve_host
from sagasu.mcp_service import AgentService
from sagasu.oauth import OAuthStore, SagasuOAuth, digest, redirect_allowed
from tests.test_mcp import FakeBackend
from tests.test_remote import DIGEST, KEY, VALID, OriginBackend, free_port

OTHER_KEY = "sagasu-other-key-" + "o" * 32
OTHER_DIGEST = hashlib.sha256(OTHER_KEY.encode()).hexdigest()
HOST = "sagasu.test"
CHATGPT = "https://chatgpt.com/connector/oauth/abc123"


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


class SettingsTests(unittest.TestCase):
    def test_oauth_is_off_unless_asked_for(self):
        listener, problems = parse_remote_listener(VALID)
        self.assertEqual(problems, [])
        self.assertFalse(listener.oauth)
        self.assertIn("chatgpt.com", listener.oauth_redirect_hosts)
        self.assertIn("claude.ai", listener.oauth_redirect_hosts)

    def test_oauth_settings_are_validated(self):
        listener, _ = parse_remote_listener(
            {
                **VALID,
                "SAGASU_HTTP_OAUTH": "on",
                "SAGASU_OAUTH_REDIRECT_HOSTS": "chatgpt.com, Example.org",
            }
        )
        self.assertTrue(listener.oauth)
        self.assertEqual(listener.oauth_redirect_hosts, ("chatgpt.com", "example.org"))
        for change in (
            {"SAGASU_HTTP_OAUTH": "yes"},
            {"SAGASU_OAUTH_REDIRECT_HOSTS": "https://chatgpt.com"},
            {"SAGASU_OAUTH_REDIRECT_HOSTS": "chatgpt.com/path"},
        ):
            with self.subTest(change=change):
                listener, problems = parse_remote_listener({**VALID, **change})
                self.assertIsNone(listener)
                self.assertTrue(problems)

    def test_redirects_go_only_to_allowed_hosts(self):
        hosts = {"chatgpt.com", "claude.ai", "localhost", "127.0.0.1"}
        for uri in (
            CHATGPT,
            "https://claude.ai/api/mcp/auth_callback",
            "http://127.0.0.1:33418/callback",
            "http://localhost:8080/cb",
        ):
            self.assertTrue(redirect_allowed(uri, hosts), uri)
        for uri in (
            "http://chatgpt.com/connector/oauth/abc",  # not https
            "https://chatgpt.com:8443/cb",
            "https://evil.chatgpt.com/cb",
            "https://chatgpt.com.evil.example/cb",
            "https://user@chatgpt.com/cb",  # ggignore: user info in a redirect URI must be refused
            "https://chatgpt.com/cb#fragment",
            "https://example.org/cb",
            "javascript:alert(1)",
            "not a url",
        ):
            self.assertFalse(redirect_allowed(uri, hosts), uri)


class NameTests(unittest.TestCase):
    def test_app_names_lose_control_and_direction_characters(self):
        from sagasu.oauth import display_name

        self.assertEqual(display_name("ChatGPT"), "ChatGPT")
        self.assertEqual(display_name("\r\x1b[2Kowner  laptop"), "[2Kowner  laptop")
        self.assertEqual(display_name("evil\u202emoc.tpgtahc"), "evilmoc.tpgtahc")
        self.assertEqual(display_name("a\u2066b\u2069c"), "abc")
        self.assertEqual(display_name("x" * 200), "x" * 80)
        self.assertIsNone(display_name("\x00\x1b"))
        self.assertIsNone(display_name(None))


class StoreTests(unittest.IsolatedAsyncioTestCase):
    """Token lifetimes and key removal, with a controllable clock."""

    def make(self, keys, path=None, now=None):
        self.now = now or self.now
        oauth = SagasuOAuth(
            path or self.path,
            keys,
            HOST,
            ("chatgpt.com",),
            clock=lambda: self.now,
        )
        self.addCleanup(oauth.close)
        return oauth

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state" / "oauth.sqlite3"
        self.now = 1_000_000.0

    async def grant(self, oauth, key_digest=DIGEST):
        from mcp.shared.auth import OAuthClientInformationFull

        info = OAuthClientInformationFull(
            client_id="client-1",
            redirect_uris=[CHATGPT],
            token_endpoint_auth_method="none",
        )
        await oauth.register_client(info)
        client = await oauth.get_client("client-1")
        grant_id = oauth.store.add_grant(
            "client-1", key_digest, ["sagasu"], self.now, "App", "chatgpt.com"
        )
        return client, oauth._issue(grant_id, ["sagasu"], self.now, self.now)

    async def test_store_file_is_private_and_holds_no_tokens(self):
        oauth = self.make({DIGEST: "laptop"})
        _, tokens = await self.grant(oauth)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        raw = self.path.read_bytes()
        self.assertNotIn(tokens.access_token.encode(), raw)
        self.assertNotIn(tokens.refresh_token.encode(), raw)
        self.assertIn(digest(tokens.access_token).encode(), raw)

    async def test_tokens_survive_a_restart_and_end_with_their_key(self):
        oauth = self.make({DIGEST: "laptop", OTHER_DIGEST: "phone"})
        client, tokens = await self.grant(oauth)
        self.assertEqual(oauth.verify(tokens.access_token), ("laptop", None))
        oauth.close()
        again = self.make({DIGEST: "laptop"})
        self.assertEqual(again.verify(tokens.access_token), ("laptop", None))
        self.assertIsNotNone(
            await again.load_refresh_token(client, tokens.refresh_token)
        )
        removed = self.make({OTHER_DIGEST: "phone"})
        self.assertIsNone(removed.verify(tokens.access_token)[0])
        self.assertIsNone(
            await removed.load_refresh_token(client, tokens.refresh_token)
        )
        # Restoring the key does not bring its old sign-ins back.
        restored = self.make({DIGEST: "laptop"})
        self.assertEqual(restored.verify(tokens.access_token), (None, "unknown_token"))
        self.assertIsNone(
            await restored.load_refresh_token(client, tokens.refresh_token)
        )

    async def test_a_key_removed_while_running_stops_its_tokens_at_once(self):
        keys = {DIGEST: "laptop"}
        oauth = self.make(keys)
        _, tokens = await self.grant(oauth)
        keys.clear()
        self.assertEqual(oauth.verify(tokens.access_token), (None, "key_removed"))

    async def test_access_tokens_expire_and_refresh_tokens_rotate(self):
        oauth = self.make({DIGEST: "laptop"})
        client, tokens = await self.grant(oauth)
        self.now += 3601
        self.assertEqual(oauth.verify(tokens.access_token), (None, "expired_token"))
        refresh = await oauth.load_refresh_token(client, tokens.refresh_token)
        fresh = await oauth.exchange_refresh_token(client, refresh, refresh.scopes)
        self.assertEqual(oauth.verify(fresh.access_token), ("laptop", None))
        self.assertNotEqual(fresh.refresh_token, tokens.refresh_token)

    async def test_reusing_a_rotated_refresh_token_ends_the_grant(self):
        oauth = self.make({DIGEST: "laptop"})
        client, tokens = await self.grant(oauth)
        refresh = await oauth.load_refresh_token(client, tokens.refresh_token)
        fresh = await oauth.exchange_refresh_token(client, refresh, refresh.scopes)
        with self.assertLogs("sagasu.observability", level="WARNING") as logs:
            self.assertIsNone(
                await oauth.load_refresh_token(client, tokens.refresh_token)
            )
        self.assertIn("remote.oauth_refresh_reused", logs.output[0])
        self.assertEqual(oauth.verify(fresh.access_token), (None, "unknown_token"))
        self.assertIsNone(await oauth.load_refresh_token(client, fresh.refresh_token))

    async def test_a_grant_ends_after_ninety_days_however_often_it_refreshes(self):
        oauth = self.make({DIGEST: "laptop"})
        client, tokens = await self.grant(oauth)
        refresh_token = tokens.refresh_token
        for _ in range(4):
            self.now += 25 * 86400
            refresh = await oauth.load_refresh_token(client, refresh_token)
            if refresh is None:
                break
            refresh_token = (
                await oauth.exchange_refresh_token(client, refresh, refresh.scopes)
            ).refresh_token
        self.assertIsNone(refresh)
        self.assertGreater(self.now - 1_000_000.0, 90 * 86400)

    async def test_refresh_tokens_belong_to_their_app(self):
        oauth = self.make({DIGEST: "laptop"})
        _, tokens = await self.grant(oauth)
        from mcp.shared.auth import OAuthClientInformationFull

        other = OAuthClientInformationFull(
            client_id="client-2", redirect_uris=[CHATGPT]
        )
        self.assertIsNone(await oauth.load_refresh_token(other, tokens.refresh_token))
        self.assertIsNone(await oauth.load_refresh_token(other, tokens.access_token))

    async def test_registration_is_capped(self):
        oauth = self.make({DIGEST: "laptop"})
        from mcp.server.auth.provider import RegistrationError
        from mcp.shared.auth import OAuthClientInformationFull

        with self.assertRaises(RegistrationError):
            for number in range(25):
                await oauth.register_client(
                    OAuthClientInformationFull(
                        client_id=f"c{number}", redirect_uris=[CHATGPT]
                    )
                )
        with patch("sagasu.oauth.MAX_CLIENTS", 3):
            store = OAuthStore(Path(self.directory.name) / "cap.sqlite3")
            self.addCleanup(store.close)
            for number in range(3):
                self.assertTrue(store.add_client(f"x{number}", "{}", self.now))
            self.assertFalse(store.add_client("x3", "{}", self.now))
            # Apps that never signed in make room after a day.
            self.assertTrue(store.add_client("x4", "{}", self.now + 2 * 86400))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    """The whole flow through the real listener, as ChatGPT would run it."""

    oauth_setting = "on"

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.port = free_port()
        env = Path(self.directory.name) / ".env"
        env.write_text(
            "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\n"
            f"SAGASU_HTTP_PORT={self.port}\nSAGASU_HTTP_ALLOWED_HOSTS={HOST}\n"
            f"SAGASU_HTTP_KEYS=laptop:{DIGEST},phone:{OTHER_DIGEST}\n"
            f"SAGASU_HTTP_OAUTH={self.oauth_setting}\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            self.settings = Settings.load(env)
        self.backend = OriginBackend()
        await self.start()

    async def start(self):
        self.host = asyncio.create_task(
            serve_host(self.settings, service=AgentService(backend=self.backend))
        )
        self.addAsyncCleanup(self.stop_host)
        for _ in range(300):
            if self.host.done():
                break
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.01)
        self.assertFalse(self.host.done(), "host exited during startup")

    async def stop_host(self):
        if not self.host.done():
            self.host.cancel()
        await asyncio.gather(self.host, return_exceptions=True)

    async def request(self, method, path, body=None, headers=None, form=None):
        def call():
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
            try:
                sent = {"Host": HOST, **(headers or {})}
                payload = body
                if form is not None:
                    payload = urlencode(form)
                    sent["Content-Type"] = "application/x-www-form-urlencoded"
                elif isinstance(body, (dict, list)):
                    payload = json.dumps(body)
                    sent.setdefault("Content-Type", "application/json")
                connection.request(method, path, body=payload, headers=sent)
                response = connection.getresponse()
                return (
                    response.status,
                    {k.lower(): v for k, v in response.getheaders()},
                    response.read(),
                )
            finally:
                connection.close()

        return await asyncio.to_thread(call)

    async def mcp(self, method, params=None, token=None, headers=None):
        packet = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            packet["params"] = params
        sent = {
            "Accept": "application/json, text/event-stream",
            **({"Authorization": "Bearer " + token} if token else {}),
            **(headers or {}),
        }
        status, response_headers, body = await self.request(
            "POST", "/mcp", packet, sent
        )
        return status, response_headers, (json.loads(body) if status == 200 else body)

    async def register(self, redirect=CHATGPT, **extra):
        status, _, body = await self.request(
            "POST",
            "/register",
            {
                "redirect_uris": [redirect],
                "client_name": "ChatGPT",
                "token_endpoint_auth_method": "none",
                **extra,
            },
        )
        return status, json.loads(body)

    async def start_sign_in(self, client_id, challenge, redirect=CHATGPT, **extra):
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "state-123",
                "resource": f"https://{HOST}/mcp",
                **extra,
            }
        )
        status, headers, _ = await self.request("GET", "/authorize?" + query)
        return status, headers.get("location", "")

    async def through_desk(self, headers):
        """After the key: the SMU sign-in page, driven as its script drives it.

        The fake backend signs in at once, so the page sends the app back."""
        self.assertEqual(headers["location"], "/sign-in")
        cookie = headers["set-cookie"].split(";", 1)[0]
        status, _, body = await self.request(
            "POST",
            "/sign-in/start",
            {},
            {"Cookie": cookie, "Origin": f"https://{HOST}"},
        )
        self.assertEqual(status, 200, body)
        state = None
        for _ in range(250):
            status, _, body = await self.request(
                "GET", "/sign-in/status", headers={"Cookie": cookie}
            )
            self.assertEqual(status, 200, body)
            state = json.loads(body)
            if state["next"]:
                return state["next"]
            await asyncio.sleep(0.02)
        self.fail(f"the SMU sign-in page never sent the app back: {state}")

    async def sign_in(self, key=KEY, redirect=CHATGPT):
        _, client = await self.register(redirect)
        verifier, challenge = pkce()
        _, page_url = await self.start_sign_in(client["client_id"], challenge, redirect)
        request_id = parse_qs(urlsplit(page_url).query)["request"][0]
        status, headers, _ = await self.request(
            "POST",
            "/oauth/sign-in",
            form={"request": request_id, "key": key, "action": "allow"},
            headers={"Origin": f"https://{HOST}"},
        )
        self.assertEqual(status, 303)
        location = await self.through_desk(headers)
        self.assertTrue(location.startswith(redirect), location)
        code = parse_qs(urlsplit(location).query)["code"][0]
        status, _, body = await self.request(
            "POST",
            "/token",
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect,
                "client_id": client["client_id"],
                "code_verifier": verifier,
            },
        )
        self.assertEqual(status, 200, body)
        return client, json.loads(body)

    async def test_discovery_points_apps_at_the_sign_in(self):
        status, headers, _ = await self.mcp("tools/list")
        self.assertEqual(status, 401)
        self.assertIn(
            f'resource_metadata="https://{HOST}/.well-known/oauth-protected-resource/mcp"',
            headers["www-authenticate"],
        )
        for path in (
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-protected-resource",
        ):
            status, _, body = await self.request("GET", path)
            self.assertEqual(status, 200, path)
            about = json.loads(body)
            self.assertEqual(about["resource"], f"https://{HOST}/mcp")
            server = about["authorization_servers"][0]
        status, _, body = await self.request(
            "GET", "/.well-known/oauth-authorization-server"
        )
        self.assertEqual(status, 200)
        metadata = json.loads(body)
        self.assertEqual(metadata["issuer"], server)
        self.assertEqual(
            metadata["authorization_endpoint"], f"https://{HOST}/authorize"
        )
        self.assertEqual(metadata["registration_endpoint"], f"https://{HOST}/register")
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])
        self.assertIn("none", metadata["token_endpoint_auth_methods_supported"])
        # Other host names get nothing, even on the public paths.
        status, _, _ = await self.request(
            "GET",
            "/.well-known/oauth-authorization-server",
            headers={"Host": "evil.test"},
        )
        self.assertEqual(status, 421)

    async def test_registration_accepts_only_known_app_redirects(self):
        status, client = await self.register()
        self.assertEqual(status, 201)
        self.assertIsNone(client.get("client_secret"))
        status, _ = await self.register("http://127.0.0.1:33418/callback")
        self.assertEqual(status, 201)
        status, refused = await self.register("https://evil.example/callback")
        self.assertEqual(status, 400)
        self.assertEqual(refused["error"], "invalid_redirect_uri")
        # Unknown scopes do not stop an app registering; they grant nothing.
        status, _ = await self.register(scope="openid email offline_access")
        self.assertEqual(status, 201)

    async def test_full_sign_in_then_calls_count_as_the_key(self):
        client, tokens = await self.sign_in()
        self.assertTrue(tokens["access_token"].startswith("sgt_"))
        self.assertEqual(tokens["token_type"], "Bearer")
        status, _, result = await self.mcp("tools/list", token=tokens["access_token"])
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in result["result"]["tools"]}
        self.assertIn("search_rooms", names)
        self.assertNotIn("login", names)
        with self.assertLogs("sagasu.observability", level="INFO") as logs:
            status, _, _ = await self.mcp(
                "tools/call",
                {"name": "server_status", "arguments": {}},
                token=tokens["access_token"],
                # A client cannot choose the name the log records.
                headers={"x-sagasu-key-label": "phone"},
            )
        self.assertEqual(status, 200)
        calls = [r.fields for r in logs.records if r.getMessage() == "remote.tool_call"]
        self.assertEqual(calls[0]["key"], "laptop")
        # The phone key signs in as phone.
        _, phone = await self.sign_in(OTHER_KEY)
        with self.assertLogs("sagasu.observability", level="INFO") as logs:
            await self.mcp(
                "tools/call",
                {"name": "server_status", "arguments": {}},
                token=phone["access_token"],
            )
        calls = [r.fields for r in logs.records if r.getMessage() == "remote.tool_call"]
        self.assertEqual(calls[0]["key"], "phone")

    async def test_the_sign_in_page_names_the_app_and_escapes_it(self):
        _, client = await self.register()
        status, client = await self.register(client_name='<script>alert("x")</script>')
        verifier, challenge = pkce()
        status, page_url = await self.start_sign_in(client["client_id"], challenge)
        self.assertEqual(status, 302)
        self.assertTrue(page_url.startswith(f"https://{HOST}/oauth/sign-in?request="))
        status, headers, body = await self.request(
            "GET", urlsplit(page_url).path + "?" + urlsplit(page_url).query
        )
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("chatgpt.com", page)
        self.assertIn('name="viewport"', page)
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["cache-control"], "no-store")

    async def test_wrong_keys_are_refused_and_limited(self):
        _, client = await self.register()
        verifier, challenge = pkce()
        _, page_url = await self.start_sign_in(client["client_id"], challenge)
        request_id = parse_qs(urlsplit(page_url).query)["request"][0]

        async def attempt(key, request=request_id, address="203.0.113.7"):
            return await self.request(
                "POST",
                "/oauth/sign-in",
                form={"request": request, "key": key, "action": "allow"},
                headers={"X-Forwarded-For": address},
            )

        status, headers, body = await attempt("wrong-key-" + "w" * 40)
        self.assertEqual(status, 400)
        self.assertNotIn("location", headers)
        self.assertIn(b"not accepted", body)
        for _ in range(3):
            await attempt("wrong-key-" + "w" * 40)
        status, _, body = await attempt("wrong-key-" + "w" * 40)
        self.assertIn(b"Too many wrong keys for this sign-in", body)
        # That sign-in is gone, even with the right key now.
        status, headers, _ = await attempt(KEY)
        self.assertEqual(status, 400)
        self.assertNotIn("location", headers)
        # Per address: ten wrong keys across sign-ins, then a wait.
        for _ in range(2):
            _, page_url = await self.start_sign_in(client["client_id"], challenge)
            fresh = parse_qs(urlsplit(page_url).query)["request"][0]
            for _ in range(3):
                await attempt("wrong-key-" + "w" * 40, fresh)
        _, page_url = await self.start_sign_in(client["client_id"], challenge)
        fresh = parse_qs(urlsplit(page_url).query)["request"][0]
        status, headers, body = await attempt(KEY, fresh)
        self.assertEqual(status, 429)
        self.assertIn(b"Wait ten minutes", body)
        # Another address is not affected.
        status, headers, _ = await attempt(KEY, fresh, "198.51.100.9")
        self.assertEqual(status, 303)

    async def test_browsers_own_form_posts_are_accepted(self):
        _, client = await self.register()
        for origin in ("null", f"https://{HOST}", f"http://{HOST}:8080", None):
            with self.subTest(origin=origin):
                _, challenge = pkce()
                _, page_url = await self.start_sign_in(client["client_id"], challenge)
                request_id = parse_qs(urlsplit(page_url).query)["request"][0]
                status, _, _ = await self.request(
                    "POST",
                    "/oauth/sign-in",
                    form={"request": request_id, "key": KEY, "action": "allow"},
                    headers={"Origin": origin} if origin else {},
                )
                self.assertEqual(status, 303)
        _, challenge = pkce()
        _, page_url = await self.start_sign_in(client["client_id"], challenge)
        status, headers, _ = await self.request("GET", page_url.split(HOST, 1)[1])
        self.assertEqual(headers.get("referrer-policy"), "same-origin")

    async def test_one_address_cannot_block_sign_ins_for_everyone(self):
        flood = {"X-Forwarded-For": "203.0.113.50"}
        for number in range(20):
            status, _, _ = await self.request(
                "POST",
                "/register",
                {"redirect_uris": [CHATGPT], "token_endpoint_auth_method": "none"},
                headers=flood,
            )
            self.assertEqual(status, 201)
        status, _, _ = await self.request(
            "POST", "/register", {"redirect_uris": [CHATGPT]}, headers=flood
        )
        self.assertEqual(status, 400)
        status, client = await self.register()  # another address
        self.assertEqual(status, 201)
        _, challenge = pkce()

        def query():
            return "/authorize?" + urlencode(
                {
                    "response_type": "code",
                    "client_id": client["client_id"],
                    "redirect_uri": CHATGPT,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )

        for _ in range(20):
            status, headers, _ = await self.request("GET", query(), headers=flood)
            self.assertIn("/oauth/sign-in", headers["location"])
        status, headers, _ = await self.request("GET", query(), headers=flood)
        self.assertIn("error=temporarily_unavailable", headers["location"])
        status, headers, _ = await self.request(
            "GET", query(), headers={"X-Forwarded-For": "198.51.100.20"}
        )
        self.assertIn("/oauth/sign-in", headers["location"])
        # Many wrong keys from many addresses do not lock out the right key.
        for number in range(12):
            _, page_url = await self.start_sign_in(client["client_id"], challenge)
            request_id = parse_qs(urlsplit(page_url).query)["request"][0]
            for _ in range(4):
                await self.request(
                    "POST",
                    "/oauth/sign-in",
                    form={"request": request_id, "key": "w" * 40, "action": "allow"},
                    headers={"X-Forwarded-For": f"192.0.2.{number}"},
                )
        _, tokens = await self.sign_in()
        self.assertTrue(tokens["access_token"])

    async def test_cancel_and_foreign_forms_and_expired_links(self):
        _, client = await self.register()
        verifier, challenge = pkce()
        _, page_url = await self.start_sign_in(client["client_id"], challenge)
        request_id = parse_qs(urlsplit(page_url).query)["request"][0]
        status, _, _ = await self.request(
            "POST",
            "/oauth/sign-in",
            form={"request": request_id, "key": KEY, "action": "allow"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        status, headers, _ = await self.request(
            "POST", "/oauth/sign-in", form={"request": request_id, "action": "cancel"}
        )
        self.assertEqual(status, 303)
        returned = parse_qs(urlsplit(headers["location"]).query)
        self.assertEqual(returned["error"], ["access_denied"])
        self.assertEqual(returned["state"], ["state-123"])
        status, _, body = await self.request(
            "GET", "/oauth/sign-in?request=" + request_id
        )
        self.assertEqual(status, 400)
        self.assertIn(b"expired", body)
        status, _, _ = await self.request("GET", "/oauth/sign-in?request=made-up")
        self.assertEqual(status, 400)

    async def test_codes_need_the_verifier_and_work_once(self):
        _, client = await self.register()
        verifier, challenge = pkce()
        _, page_url = await self.start_sign_in(client["client_id"], challenge)
        request_id = parse_qs(urlsplit(page_url).query)["request"][0]
        _, headers, _ = await self.request(
            "POST",
            "/oauth/sign-in",
            form={"request": request_id, "key": KEY, "action": "allow"},
        )
        code = parse_qs(urlsplit(await self.through_desk(headers)).query)["code"][0]
        exchange = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CHATGPT,
            "client_id": client["client_id"],
        }
        status, _, body = await self.request(
            "POST", "/token", form={**exchange, "code_verifier": "x" * 50}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid_grant")
        status, _, _ = await self.request(
            "POST", "/token", form={**exchange, "code_verifier": verifier}
        )
        self.assertEqual(status, 200)
        status, _, body = await self.request(
            "POST", "/token", form={**exchange, "code_verifier": verifier}
        )
        self.assertEqual(status, 400)
        # Another app cannot use the code either.
        _, other = await self.register()
        status, _, _ = await self.request(
            "POST",
            "/token",
            form={
                **exchange,
                "client_id": other["client_id"],
                "code_verifier": verifier,
            },
        )
        self.assertEqual(status, 400)

    async def test_other_resources_and_redirects_are_refused(self):
        _, client = await self.register()
        _, challenge = pkce()
        status, location = await self.start_sign_in(
            client["client_id"], challenge, resource="https://other.example/mcp"
        )
        self.assertEqual(status, 302)
        self.assertEqual(
            parse_qs(urlsplit(location).query)["error"], ["invalid_target"]
        )
        status, _ = await self.start_sign_in(
            client["client_id"], challenge, redirect="https://chatgpt.com/other"
        )
        self.assertEqual(status, 400)

    async def test_refresh_naming_the_scope_works_without_one_at_sign_in(self):
        client, tokens = await self.sign_in()
        self.assertEqual(tokens.get("scope"), "sagasu")
        status, _, body = await self.request(
            "POST",
            "/token",
            form={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
                "scope": "sagasu",
            },
        )
        self.assertEqual(status, 200, body)

    async def test_refresh_and_revoke(self):
        client, tokens = await self.sign_in()
        status, _, body = await self.request(
            "POST",
            "/token",
            form={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        self.assertEqual(status, 200)
        fresh = json.loads(body)
        self.assertEqual(
            (await self.mcp("tools/list", token=fresh["access_token"]))[0], 200
        )
        status, _, _ = await self.request(
            "POST",
            "/revoke",
            form={"token": fresh["access_token"], "client_id": client["client_id"]},
        )
        self.assertEqual(status, 200)
        status, headers, _ = await self.mcp("tools/list", token=fresh["access_token"])
        self.assertEqual(status, 401)
        self.assertIn('error="invalid_token"', headers["www-authenticate"])

    async def test_tokens_and_key_headers_together(self):
        _, tokens = await self.sign_in()
        same = {"X-API-Key": KEY}
        self.assertEqual(
            (await self.mcp("tools/list", token=tokens["access_token"], headers=same))[
                0
            ],
            200,
        )
        other = {"X-API-Key": OTHER_KEY}
        self.assertEqual(
            (await self.mcp("tools/list", token=tokens["access_token"], headers=other))[
                0
            ],
            401,
        )
        # A token is not accepted where only keys go.
        status, _, _ = await self.mcp(
            "tools/list", headers={"X-API-Key": tokens["access_token"]}
        )
        self.assertEqual(status, 401)
        # Keys keep working exactly as before.
        self.assertEqual((await self.mcp("tools/list", token=KEY))[0], 200)
        self.assertEqual(
            (await self.mcp("tools/list", headers={"X-API-Key": KEY}))[0], 200
        )

    async def test_tokens_survive_a_service_restart(self):
        client, tokens = await self.sign_in()
        await self.stop_host()
        await self.start()
        self.assertEqual(
            (await self.mcp("tools/list", token=tokens["access_token"]))[0], 200
        )
        store = Path(self.directory.name) / "oauth.sqlite3"
        self.assertTrue(store.exists())
        self.assertEqual(store.stat().st_mode & 0o777, 0o600)

    async def test_a_real_browser_completes_the_sign_in_page(self):
        from playwright.async_api import async_playwright

        _, client = await self.register()
        verifier, challenge = pkce()
        _, page_url = await self.start_sign_in(client["client_id"], challenge)
        local = f"http://{HOST}:{self.port}" + page_url.split(HOST, 1)[1]
        landed = []
        async with async_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("Install Playwright Chromium to run browser tests")
            browser = await playwright.chromium.launch(
                headless=True,
                # The browser can reach the test listener and nothing else.
                args=[
                    f"--host-resolver-rules=MAP {HOST} 127.0.0.1, MAP * ~NOTFOUND",
                    "--no-proxy-server",
                ],
            )
            try:
                page = await browser.new_page()
                page.on(
                    "request",
                    lambda request: (
                        landed.append(request.url)
                        if not request.url.startswith(f"http://{HOST}:")
                        else None
                    ),
                )
                await page.goto(local)
                await page.fill("#key", KEY)
                try:
                    await page.click("button[value=allow]")
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass  # the app's address cannot load here, by design
            finally:
                await browser.close()
        self.assertEqual(len(landed), 1, landed)
        self.assertTrue(landed[0].startswith(CHATGPT + "?"), landed[0])
        returned = parse_qs(urlsplit(landed[0]).query)
        self.assertEqual(returned["state"], ["state-123"])
        status, _, body = await self.request(
            "POST",
            "/token",
            form={
                "grant_type": "authorization_code",
                "code": returned["code"][0],
                "redirect_uri": CHATGPT,
                "client_id": client["client_id"],
                "code_verifier": verifier,
            },
        )
        self.assertEqual(status, 200, body)

    async def test_turning_oauth_off_signs_every_app_out(self):
        _, tokens = await self.sign_in()
        env = self.settings.env_file
        for setting in ("off", "on"):
            await self.stop_host()
            text = env.read_text()
            env.write_text(
                text.replace(
                    "SAGASU_HTTP_OAUTH=on", f"SAGASU_HTTP_OAUTH={setting}"
                ).replace("SAGASU_HTTP_OAUTH=off", f"SAGASU_HTTP_OAUTH={setting}")
            )
            with patch.dict(os.environ, {}, clear=True):
                self.settings = Settings.load(env)
            await self.start()
        status, _, _ = await self.mcp("tools/list", token=tokens["access_token"])
        self.assertEqual(status, 401)

    async def test_the_smu_sign_in_page_is_served_without_a_key_header(self):
        from sagasu import errors

        status, headers, body = await self.request("GET", "/sign-in")
        self.assertEqual(status, 200)
        self.assertIn(b'name="key"', body)
        status, _, _ = await self.request("GET", "/sign-in/status")
        self.assertEqual(status, 401)  # the page's own refusal: no visit
        status, _, _ = await self.request(
            "GET", "/sign-in", headers={"Host": "other.example"}
        )
        self.assertEqual(status, 421)
        status, _, _ = await self.request("GET", "/sign-inx")
        self.assertEqual(status, 401)  # the gate's: not the page's path
        # Agents are told where to send a person to sign in.
        self.assertEqual(errors.SIGN_IN_URL, f"https://{HOST}/sign-in")
        await self.stop_host()
        self.assertIsNone(errors.SIGN_IN_URL)

    async def test_a_key_opens_the_sign_in_page_without_any_app(self):
        status, headers, _ = await self.request(
            "POST",
            "/sign-in",
            form={"key": KEY},
            headers={"Origin": f"https://{HOST}"},
        )
        self.assertEqual((status, headers["location"]), (303, "/sign-in"))
        cookie = headers["set-cookie"].split(";", 1)[0]
        status, _, body = await self.request(
            "POST",
            "/sign-in/start",
            {},
            {"Cookie": cookie, "Origin": f"https://{HOST}"},
        )
        self.assertEqual(status, 200, body)
        for _ in range(250):
            _, _, body = await self.request(
                "GET", "/sign-in/status", headers={"Cookie": cookie}
            )
            state = json.loads(body)
            if state["phase"] == "signed_in":
                break
            await asyncio.sleep(0.02)
        self.assertEqual((state["phase"], state["next"]), ("signed_in", None))
        self.assertIn(("login", None), self.backend.calls)

    async def test_other_paths_still_need_a_key(self):
        for path in ("/", "/oauth", "/admin", "/.well-known/openid-configuration"):
            status, _, _ = await self.request("GET", path)
            self.assertEqual(status, 401, path)


class OAuthOffTests(FlowTests):
    """With OAuth off nothing new is served and the challenge stays plain."""

    oauth_setting = "off"

    async def test_discovery_points_apps_at_the_sign_in(self):
        status, headers, _ = await self.mcp("tools/list")
        self.assertEqual(status, 401)
        self.assertEqual(headers["www-authenticate"], 'Bearer realm="sagasu"')
        for path in (
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-authorization-server",
            "/oauth/sign-in?request=x",
        ):
            status, _, _ = await self.request("GET", path)
            self.assertEqual(status, 401, path)
        status, _, _ = await self.request(
            "POST", "/register", {"redirect_uris": [CHATGPT]}
        )
        self.assertEqual(status, 401)
        self.assertFalse((Path(self.directory.name) / "oauth.sqlite3").exists())

    def _skip(self):
        self.skipTest("needs OAuth on")

    test_registration_accepts_only_known_app_redirects = _skip
    test_full_sign_in_then_calls_count_as_the_key = _skip
    test_the_sign_in_page_names_the_app_and_escapes_it = _skip
    test_wrong_keys_are_refused_and_limited = _skip
    test_cancel_and_foreign_forms_and_expired_links = _skip
    test_codes_need_the_verifier_and_work_once = _skip
    test_other_resources_and_redirects_are_refused = _skip
    test_refresh_and_revoke = _skip
    test_tokens_and_key_headers_together = _skip
    test_tokens_survive_a_service_restart = _skip
    test_browsers_own_form_posts_are_accepted = _skip
    test_one_address_cannot_block_sign_ins_for_everyone = _skip
    test_refresh_naming_the_scope_works_without_one_at_sign_in = _skip
    test_a_real_browser_completes_the_sign_in_page = _skip
    test_turning_oauth_off_signs_every_app_out = _skip


if __name__ == "__main__":
    unittest.main()
