"""Remote listener: API-key gate, restricted tools, and sign-in that stays on the host.

Uses a fake backend and a stdlib HTTP client. Never contacts FBS or Microsoft.
"""

import asyncio
import contextlib
import hashlib
import http.client
import json
import logging
import os
import signal
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sagasu.config import Settings, parse_remote_listener
from sagasu.fbs_client import AuthenticationRequired, FBSClient
from sagasu.host import (
    HostError,
    client_session,
    connect,
    control,
    identity_parts,
    serve_host,
    socket_path,
)
from sagasu.mcp_service import SIGN_IN_RETRY_SECONDS, AgentError, AgentService
from sagasu.origin import CALL_ORIGIN, is_remote
from tests.test_mcp import CONFIG, ROOM, FakeBackend, ServiceCase

try:
    from mcp import Client
    from mcp_types import ElicitResult
    from sagasu.mcp_server import (
        REMOTE_HIDDEN_TOOLS,
        REMOTE_WRITE_TOOLS,
        RemoteAccess,
        create_server,
    )
except ImportError:
    Client = None

KEY = "sagasu-test-key-" + "k" * 32
DIGEST = hashlib.sha256(KEY.encode()).hexdigest()
VALID = {
    "SAGASU_HTTP_PORT": "8765",
    "SAGASU_HTTP_ALLOWED_HOSTS": "sagasu.test",
    "SAGASU_HTTP_KEYS": "laptop:" + DIGEST,
}


@contextlib.contextmanager
def remote_origin():
    token = CALL_ORIGIN.set("remote")
    try:
        yield
    finally:
        CALL_ORIGIN.reset(token)


class OriginBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.origins = []

    async def run(self, kind, payload, progress, notify):
        self.origins.append(is_remote())
        return await super().run(kind, payload, progress, notify)


class RemoteConfigurationTests(unittest.TestCase):
    def test_listener_is_off_unless_a_port_is_set(self):
        self.assertEqual(parse_remote_listener({}), (None, []))
        self.assertEqual(
            parse_remote_listener({"SAGASU_HTTP_KEYS": "a:" + DIGEST}), (None, [])
        )

    def test_valid_listener_defaults_to_loopback_and_read_only(self):
        listener, problems = parse_remote_listener(VALID)
        self.assertEqual(problems, [])
        self.assertEqual(
            (listener.bind, listener.port, listener.allowed_hosts, listener.writes),
            ("127.0.0.1", 8765, ("sagasu.test",), False),
        )
        self.assertEqual(listener.keys, {DIGEST: "laptop"})
        self.assertNotIn(DIGEST, repr(listener))

    def test_each_unsafe_or_partial_setting_keeps_the_listener_off(self):
        for change in (
            {"SAGASU_HTTP_PORT": "80"},
            {"SAGASU_HTTP_PORT": "eight"},
            {"SAGASU_HTTP_BIND": "0.0.0.0"},
            {"SAGASU_HTTP_BIND": "10.0.0.5"},
            {"SAGASU_HTTP_BIND": "localhost"},
            {"SAGASU_HTTP_ALLOWED_HOSTS": ""},
            {"SAGASU_HTTP_ALLOWED_HOSTS": "https://sagasu.test"},
            {"SAGASU_HTTP_ALLOWED_HOSTS": "sagasu.test:443"},
            {"SAGASU_HTTP_ALLOWED_HOSTS": "*"},
            {"SAGASU_HTTP_KEYS": ""},
            {"SAGASU_HTTP_KEYS": "laptop:" + KEY},  # a raw key, not its digest
            {"SAGASU_HTTP_KEYS": "Laptop!:" + DIGEST},
            {"SAGASU_HTTP_KEYS": f"a:{DIGEST},a:{'0' * 64}"},
            {"SAGASU_HTTP_KEYS": f"a:{DIGEST},b:{DIGEST}"},
            {"SAGASU_HTTP_WRITES": "yes"},
            {"SAGASU_HTTP_WRITES": "on", "SAGASU_MCP_CONFIRMATION": "human"},
            {"SMU_LOGIN_PASSWORD": "fixture-password"},
        ):
            with self.subTest(change=change):
                listener, problems = parse_remote_listener({**VALID, **change})
                self.assertIsNone(listener)
                self.assertTrue(problems)
                self.assertNotIn("fixture-password", " ".join(problems))

    def test_writes_are_an_explicit_host_choice(self):
        listener, _ = parse_remote_listener({**VALID, "SAGASU_HTTP_WRITES": "on"})
        self.assertTrue(listener.writes)

    def test_settings_report_labels_but_never_digests_and_block_on_problems(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("".join(f"{key}={value}\n" for key, value in VALID.items()))
            with patch.dict(os.environ, {}, clear=True):
                settings = Settings.load(env)
                self.assertEqual(settings.issues(), [])
                public = json.dumps(settings.public())
                self.assertNotIn(DIGEST, public)
                self.assertEqual(
                    settings.public()["remote_listener"]["key_labels"], ["laptop"]
                )
                self.assertTrue(settings.remote_listener_requested)
            env.write_text("SAGASU_HTTP_PORT=8765\nSAGASU_SESSION_COOKIES=some\n")
            with patch.dict(os.environ, {}, clear=True):
                broken = Settings.load(env)
                self.assertIsNone(broken.http)
                self.assertTrue(broken.remote_listener_requested)
                names = {issue.name for issue in broken.issues()}
                self.assertEqual(
                    names, {"config.remote_listener", "config.session_cookies"}
                )
                self.assertTrue(all(issue.blocks_startup for issue in broken.issues()))
                self.assertFalse(broken.public()["remote_listener"]["enabled"])


class UpgradeCompatibilityTests(unittest.TestCase):
    def load(self, directory, text):
        env = Path(directory) / ".env"
        env.write_text(text)
        with patch.dict(os.environ, {}, clear=True):
            return Settings.load(env)

    def test_unchanged_local_configuration_keeps_its_earlier_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.load(directory, "SAGASU_BROWSER_PROFILE=browser\n")
            # What a host started by the version before this feature hashed.
            earlier = settings.public()
            del earlier["session_cookies"], earlier["remote_listener"]
            earlier["configuration_issues"] = []
            earlier.update(
                SMU_LOGIN_EMAIL="",
                SMU_LOGIN_PASSWORD="",
                SAGASU_STAY_SIGNED_IN="true",
                SAGASU_AUTH_ARTIFACT_DIR="",
            )
            digest = hashlib.sha256(
                json.dumps(earlier, sort_keys=True).encode()
            ).hexdigest()
            self.assertEqual(identity_parts(settings)["configuration"], digest)

    def test_every_new_setting_changes_the_digest_when_used(self):
        with tempfile.TemporaryDirectory() as directory:
            base = "SAGASU_BROWSER_PROFILE=browser\n"
            listener = base + "".join(f"{k}={v}\n" for k, v in VALID.items())
            digests = {
                identity_parts(self.load(directory, text))["configuration"]
                for text in (
                    base,
                    base + "SAGASU_SESSION_COOKIES=fbs\n",
                    listener,
                    listener + "SAGASU_HTTP_WRITES=on\n",
                    listener.replace(DIGEST, "0" * 64),
                )
            }
            self.assertEqual(len(digests), 5)


class LogThrottleTests(unittest.TestCase):
    def test_other_libraries_cannot_flood_out_the_call_record(self):
        from sagasu.remote import ThirdPartyThrottle

        def record(name):
            return logging.LogRecord(name, logging.WARNING, "", 0, "x", (), None)

        throttle, clock = ThirdPartyThrottle(limit=20, window=60), [1000.0]
        with patch("sagasu.remote.time.monotonic", lambda: clock[0]):
            passed = sum(throttle.filter(record("uvicorn.error")) for _ in range(500))
            self.assertEqual(passed, 20)
            self.assertTrue(throttle.filter(record("sagasu.observability")))
            self.assertFalse(throttle.filter(record("mcp.server")))
            clock[0] += 61
            self.assertTrue(throttle.filter(record("uvicorn.error")))


class SignInStaysOnHostTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        with remote_origin():
            client = FBSClient(interactive=True, headless=True)
        client.page = Mock(
            url="https://login.microsoftonline.com/common/oauth2/authorize",
            wait_for_timeout=AsyncMock(),
            goto=AsyncMock(),
            wait_for_function=AsyncMock(),
        )
        client.page.locator.return_value.count = AsyncMock(return_value=0)
        return client

    def assert_nothing_entered(self, client):
        locator = client.page.locator.return_value
        for action in (locator.fill, locator.click, locator.press, locator.type):
            action.assert_not_called()
        client.page.keyboard.press.assert_not_called()
        client.page.get_by_text.assert_not_called()

    async def test_remote_origin_can_never_be_interactive(self):
        self.assertTrue(FBSClient(interactive=True).interactive)
        self.assertFalse(FBSClient(interactive=True).silent_renewal)
        with remote_origin():
            client = FBSClient(interactive=True)
        self.assertFalse(client.interactive)
        self.assertTrue(client.silent_renewal)

    async def test_live_microsoft_session_renews_without_input(self):
        client = self.client()
        client.page.locator.return_value.count = AsyncMock(side_effect=[0, 0, 1])
        client._classify_auth_state = AsyncMock(return_value=("sign_in", "waiting"))
        self.assertTrue(await client._await_silent_renewal())
        self.assert_nothing_entered(client)
        client.page.goto.assert_not_awaited()

    async def test_any_prompt_for_a_person_stops_and_names_the_host(self):
        for state in ("email", "password", "account", "authenticator", "manual"):
            with self.subTest(state=state):
                client = self.client()
                client._classify_auth_state = AsyncMock(return_value=(state, "prompt"))
                with self.assertRaises(AuthenticationRequired) as caught:
                    await client._await_silent_renewal()
                self.assertEqual(caught.exception.reason, "sign_in_required_on_host")
                # One reading can be a page in mid-redirect; two are a prompt.
                self.assertEqual(client._classify_auth_state.await_count, 2)
                self.assert_nothing_entered(client)

    async def test_unfamiliar_origin_must_persist_before_it_counts_as_a_prompt(self):
        client = self.client()
        client.page.url = "https://example.invalid/sign-in"
        client._classify_auth_state = AsyncMock()
        with self.assertRaises(AuthenticationRequired) as caught:
            await client._await_silent_renewal()
        self.assertEqual(caught.exception.reason, "sign_in_required_on_host")
        self.assertEqual(client.page.wait_for_timeout.await_count, 7)  # 8 readings
        client._classify_auth_state.assert_not_awaited()
        self.assert_nothing_entered(client)

        # Passing through one during a redirect is not a prompt.
        client = self.client()
        client.page.url = "https://example.invalid/hop"
        client.page.locator.return_value.count = AsyncMock(side_effect=[0, 0, 0, 1])
        self.assertTrue(await client._await_silent_renewal())
        self.assertEqual(client.page.wait_for_timeout.await_count, 3)

    async def test_error_pages_are_not_mistaken_for_a_sign_in_prompt(self):
        for url, state in (
            ("chrome-error://chromewebdata/", None),
            ("about:blank", None),
            ("https://login.microsoftonline.com/common/reprocess", "provider_error"),
            ("https://login.microsoftonline.com/common/reprocess", "invalid_context"),
        ):
            with self.subTest(url=url, state=state):
                client = self.client()
                client.page.url = url
                client._classify_auth_state = AsyncMock(return_value=(state, "text"))
                # Not renewed, and not a reason to pause every other request.
                self.assertFalse(await client._await_silent_renewal())
                self.assertEqual(client.page.wait_for_timeout.await_count, 1)
                self.assert_nothing_entered(client)

    async def test_a_blocked_sign_in_needs_the_owner(self):
        client = self.client()
        client._classify_auth_state = AsyncMock(return_value=("failed", "blocked"))
        with self.assertRaises(AuthenticationRequired) as caught:
            await client._await_silent_renewal()
        self.assertEqual(caught.exception.reason, "sign_in_required_on_host")

    async def test_slow_redirect_times_out_without_blocking_later_requests(self):
        client = self.client()
        client._classify_auth_state = AsyncMock(return_value=("sign_in", "waiting"))

        async def pause(_):
            await asyncio.sleep(0.01)

        client.page.wait_for_timeout = AsyncMock(side_effect=pause)
        with patch("sagasu.fbs_client.SILENT_RENEWAL_SECONDS", 0.05):
            self.assertFalse(await client._await_silent_renewal())
        self.assert_nothing_entered(client)

    async def test_ensure_authenticated_uses_renewal_and_never_the_login_flow(self):
        client = self.client()
        client._complete_interactive_authentication = AsyncMock()
        client._save_session = AsyncMock()
        frame = Mock(wait_for_selector=AsyncMock())
        client._wait_for_frame = AsyncMock(return_value=frame)
        client._await_silent_renewal = AsyncMock(return_value=True)
        self.assertEqual(await client.ensure_authenticated(), (client.page, frame))
        client._save_session.assert_awaited_once()
        client._await_silent_renewal = AsyncMock(return_value=False)
        with self.assertRaises(AuthenticationRequired) as caught:
            await client.ensure_authenticated()
        self.assertEqual(caught.exception.reason, "session_expired")
        client._complete_interactive_authentication.assert_not_awaited()

    async def test_local_non_interactive_checks_do_not_wait_for_renewal(self):
        client = FBSClient(interactive=False, headless=True)
        client.page = Mock(goto=AsyncMock(), wait_for_function=AsyncMock())
        client.page.locator.return_value.count = AsyncMock(return_value=0)
        client._await_silent_renewal = AsyncMock(return_value=True)
        with self.assertRaises(AuthenticationRequired):
            await client.ensure_authenticated()
        client._await_silent_renewal.assert_not_awaited()

    async def test_saved_session_can_be_limited_to_cookies_fbs_receives(self):
        self.assertTrue(FBSClient._sent_to_fbs("fbs.intranet.smu.edu.sg"))
        self.assertTrue(FBSClient._sent_to_fbs(".intranet.smu.edu.sg"))
        self.assertTrue(FBSClient._sent_to_fbs(".smu.edu.sg"))
        for domain in (
            "login.microsoftonline.com",
            ".login.microsoftonline.com",
            "login.smu.edu.sg",
            "evilfbs.intranet.smu.edu.sg",
            "xsmu.edu.sg",
            ".edu.sg",
            "sg",
            "",
            None,
        ):
            self.assertFalse(FBSClient._sent_to_fbs(domain), domain)
        cookies = [
            {"name": "fbs", "domain": "fbs.intranet.smu.edu.sg", "expires": -1},
            {"name": "sso", "domain": ".smu.edu.sg", "expires": -1},
            {"name": "ESTSAUTH", "domain": ".login.microsoftonline.com", "expires": -1},
            {
                "name": "kept-by-chromium",
                "domain": ".smu.edu.sg",
                "expires": 4102444800,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            client = FBSClient(profile_path=Path(directory))
            client.context = Mock(cookies=AsyncMock(return_value=cookies))
            with patch.dict(os.environ, {"SAGASU_SESSION_COOKIES": "fbs"}):
                await client._save_session()
            saved = json.loads(client.session_path.read_text())
            self.assertEqual([cookie["name"] for cookie in saved], ["fbs", "sso"])
            with patch.dict(os.environ, {"SAGASU_SESSION_COOKIES": "all"}):
                await client._save_session()
            saved = json.loads(client.session_path.read_text())
            self.assertEqual(
                [cookie["name"] for cookie in saved], ["fbs", "sso", "ESTSAUTH"]
            )


class RemoteJobTests(ServiceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.backend = OriginBackend()
        self.service.backend = self.backend

    async def remote(self, kind="bookings"):
        with remote_origin():
            job = self.service.start(kind)
        return await self.finish(job)

    async def test_jobs_inherit_the_origin_of_the_call_that_started_them(self):
        await self.remote()
        await self.finish(self.service.start("bookings"))
        self.assertEqual(self.backend.origins, [True, False])

    async def test_host_sign_in_failure_pauses_remote_requests_only(self):
        self.backend.error = AuthenticationRequired(
            "fixture", reason="sign_in_required_on_host"
        )
        failed = await self.remote()
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error["reason"], "sign_in_required_on_host")
        self.assertIsNone(failed.error["next_tool"])
        self.assertFalse(failed.error["retry_safe"])
        self.assertNotIn("Run login", json.dumps(failed.error))
        self.assertTrue(self.service.host_sign_in_required)

        self.backend.error = None
        calls = len(self.backend.calls)
        refused = await self.remote()
        self.assertEqual(refused.error["reason"], "sign_in_required_on_host")
        self.assertEqual(len(self.backend.calls), calls)  # browser never opened

        # The owner's own login on the host is not paused, and lifts the pause.
        done = await self.finish(self.service.start("login"))
        self.assertEqual(done.status, "succeeded")
        self.assertFalse(self.service.host_sign_in_required)
        self.assertEqual((await self.remote()).status, "succeeded")

    async def test_pause_expires_even_while_agents_keep_asking(self):
        self.backend.error = AuthenticationRequired(
            "fixture", reason="sign_in_required_on_host"
        )
        await self.remote()
        self.backend.error = None  # the session has recovered meanwhile
        calls = len(self.backend.calls)
        for _ in range(4):  # a request every 200 s: all inside the pause
            self.now += 200
            refused = await self.remote()
            self.assertEqual(refused.error["reason"], "sign_in_required_on_host")
        self.assertEqual(len(self.backend.calls), calls)
        self.now += 200  # 1000 s after the detection, not after the last refusal
        self.assertFalse(self.service.host_sign_in_required)
        self.assertEqual((await self.remote()).status, "succeeded")

    async def test_remote_caller_cannot_see_or_touch_what_the_owner_started(self):
        self.backend.gate = asyncio.Event()
        login = self.service.start("login")  # the owner, signing in at the host
        queued = self.service.start("bookings")
        await asyncio.sleep(0.01)
        with remote_origin():
            mine = self.service.start("tasks")
            self.assertEqual(
                [job.id for job in self.service.visible_jobs()], [mine["operation_id"]]
            )
            for operation in (login, queued):
                for call in (
                    self.service.get,
                    self.service.result,
                    self.service.authentication_status,
                    self.service.reconcile_operation,
                ):
                    with self.subTest(kind=operation["kind"], call=call.__name__):
                        with self.assertRaises(AgentError):
                            call(operation["operation_id"])
                with self.assertRaises(AgentError):
                    await self.service.wait(operation["operation_id"], 0)
                with self.assertRaises(AgentError):
                    await self.service.cancel(operation["operation_id"])
        self.assertFalse(self.service.get(login["operation_id"]).terminal)
        self.assertFalse(self.service.get(queued["operation_id"]).terminal)
        # The owner sees and controls everything, remote work included.
        self.assertEqual(len(self.service.visible_jobs()), 3)
        stopped = await self.service.cancel(mine["operation_id"])
        self.assertEqual(stopped["status"], "cancelled")
        self.backend.gate.set()

    async def test_remote_caller_acts_only_on_reviews_it_prepared(self):
        local = await self.review()  # prepared at the host
        with remote_origin():
            self.assertEqual(self.service.visible_reviews(), [])
            for call in (
                self.service.review,
                self.service.validate,
                self.service.confirm,
                self.service.submit,
                self.service.discard,
            ):
                with self.subTest(call=call.__name__):
                    with self.assertRaises(AgentError):
                        call(local["review_id"])
            search = await self.finish(self.service.search(CONFIG))
            mine = self.service.review_booking(
                search.id, ROOM["name"], "Remote meeting", "friend@scis.smu.edu.sg"
            )
            self.assertNotIn("remote", mine)
            self.assertEqual(
                [r["review_id"] for r in self.service.visible_reviews()],
                [mine["review_id"]],
            )
            # Kinds whose tools are never offered remotely are refused outright.
            for kind in ("approve_task", "batch"):
                self.service.reviews[mine["review_id"]]["kind"] = kind
                with self.subTest(kind=kind), self.assertRaises(AgentError):
                    self.service.submit(mine["review_id"])
            self.service.reviews[mine["review_id"]]["kind"] = "book"
            job = await self.finish(self.service.submit(mine["review_id"]))
            self.assertEqual(job.status, "succeeded")
        self.assertFalse(any(k == "approve_task" for k, _ in self.backend.calls))
        self.assertIn(local["review_id"], self.service.reviews)
        self.assertEqual(len(self.service.visible_reviews()), 1)

    async def test_remote_caller_cannot_replace_verified_evidence(self):
        with remote_origin():
            search = await self.finish(self.service.search(CONFIG))
            review = self.service.review_booking(
                search.id, ROOM["name"], "Remote meeting", "friend@scis.smu.edu.sg"
            )
            booked = await self.finish(self.service.submit(review["review_id"]))
            booked.reconciliation = {"action_outcome_verified": True}
            with self.assertRaises(AgentError):
                self.service.reconcile_operation(booked.id)
        self.assertEqual(
            self.service.reconcile_operation(booked.id)["kind"], "reconcile"
        )

    async def test_planning_jobs_stop_at_the_first_date_that_needs_the_owner(self):
        from sagasu.alternatives import candidates, find_alternatives
        from sagasu.repeat import repeat_plan
        from sagasu.room_plans import find_room_plans, prepare_plan
        from sagasu.room_shortlists import refresh_shortlist
        from tests.test_room_plans import CONFIG as PLAN
        from tests.test_room_shortlists import NOW, payload as shortlist

        needs_owner = AuthenticationRequired("x", reason="sign_in_required_on_host")
        source = (await self.finish(self.service.search(CONFIG))).result
        sources = [
            {"reference_number": name, "room": "A", "date": "01-Nov-2099"}
            for name in ("BK-1", "BK-2")
        ]
        cases = {
            "plan": lambda read: find_room_plans(
                prepare_plan(PLAN, dates=["2099-11-01", "2099-11-02"]), read
            ),
            "alternatives": lambda read: find_alternatives(
                None,
                {"source": source, "configs": candidates(source, 30)[:2]},
                search=read,
            ),
            "shortlist": lambda read: refresh_shortlist(shortlist(), read, now=NOW),
        }
        for name, run in cases.items():
            with self.subTest(job=name):
                read = AsyncMock(side_effect=needs_owner)
                with remote_origin(), self.assertRaises(AuthenticationRequired):
                    await run(read)
                self.assertEqual(read.await_count, 1)
                # At the host the behaviour is unchanged: recorded, and on to the next.
                read = AsyncMock(side_effect=AuthenticationRequired("expired"))
                result = await run(read)
                self.assertGreaterEqual(read.await_count, 1)
                self.assertIn("failed", json.dumps(result))
        with patch(
            "sagasu.repeat.read_booking_details", side_effect=needs_owner
        ) as read:
            payload = {"date": "02-Nov-2099", "sources": sources}
            with remote_origin(), self.assertRaises(AuthenticationRequired):
                await repeat_plan(None, payload)
            self.assertEqual(read.await_count, 1)
            self.assertTrue((await repeat_plan(None, payload))["partial_failure"])
            self.assertEqual(read.await_count, 3)

    async def test_a_remote_plan_reports_the_host_and_pauses_like_any_other_read(self):
        from sagasu.mcp_service import Backend
        from tests.test_room_plans import CONFIG as PLAN

        self.service.backend = backend = Backend()
        backend.reads.mode = "browser"
        needs_owner = AuthenticationRequired("x", reason="sign_in_required_on_host")
        with patch("sagasu.mcp_service.FBSClient", side_effect=needs_owner) as browser:
            with remote_origin():
                job = self.service.plan_rooms(
                    PLAN, ["2099-11-01", "2099-11-02", "2099-11-03"], 60
                )
            failed = await self.finish(job)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.error["reason"], "sign_in_required_on_host")
            self.assertEqual(browser.call_count, 1)  # one page load, not one per date
            self.assertTrue(self.service.host_sign_in_required)

    async def test_remote_expiry_never_points_at_login(self):
        self.backend.error = AuthenticationRequired("FBS session expired")
        failed = await self.remote()
        self.assertEqual(failed.error["reason"], "session_renewal_pending")
        self.assertIsNone(failed.error["next_tool"])
        self.assertFalse(self.service.host_sign_in_required)
        local = await self.finish(self.service.start("bookings"))
        self.assertEqual(local.error["reason"], "session_expired")
        self.assertEqual(local.error["next_tool"], "login")


@unittest.skipIf(Client is None, "Install Sagasu for MCP runtime tests")
class RemoteToolPolicyTests(ServiceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.backend = OriginBackend()
        self.service.backend = self.backend

    async def tools(self, **options):
        async with Client(create_server(self.service, **options)) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    async def rejected(self, client, name, arguments=None):
        try:
            return (await client.call_tool(name, arguments or {})).is_error
        except Exception:
            return True

    async def test_read_only_remote_offers_reads_and_nothing_else(self):
        local = await self.tools(allow_agent_submission=True)
        # Every listed name is a real tool, so a rename cannot silently expose one.
        self.assertLessEqual(REMOTE_HIDDEN_TOOLS | REMOTE_WRITE_TOOLS, local)
        remote = await self.tools(remote=RemoteAccess())
        self.assertEqual(remote, local - REMOTE_HIDDEN_TOOLS - REMOTE_WRITE_TOOLS)
        self.assertLessEqual(
            {
                "server_status",
                "session_status",
                "search_rooms",
                "plan_rooms",
                "available_rooms",
                "list_bookings",
                "booking_details",
                "wait_operation",
                "operation_result",
            },
            remote,
        )

    async def test_write_enabled_remote_still_lacks_host_and_human_tools(self):
        local = await self.tools(allow_agent_submission=True)
        remote = await self.tools(
            allow_agent_submission=True, remote=RemoteAccess(writes=True)
        )
        self.assertEqual(remote, local - REMOTE_HIDDEN_TOOLS)
        self.assertIn("submit_review", remote)
        for name in ("login", "authentication_status", "confirm_review"):
            self.assertNotIn(name, remote)

    async def test_hidden_tools_cannot_be_called_by_name(self):
        review = await self.review()  # prepared locally, on the host
        calls = len(self.backend.calls)

        async def accept(context, params):
            return ElicitResult(action="accept", content={"confirm": True})

        async with Client(
            create_server(self.service, remote=RemoteAccess()),
            elicitation_callback=accept,
        ) as client:
            for name, arguments in (
                ("login", {}),
                ("authentication_status", {"operation_id": "x"}),
                ("recover_host", {}),
                ("browser_screenshot", {}),
                ("confirm_review", {"review_id": review["review_id"]}),
                ("submit_review", {"review_id": review["review_id"]}),
                (
                    "booking_review",
                    {
                        "search_operation_id": "x",
                        "room": ROOM["name"],
                        "purpose": "p",
                        "co_booker": "friend@scis.smu.edu.sg",
                    },
                ),
                ("save_cobooker", {"name": "A", "email": "a@smu.edu.sg"}),
            ):
                with self.subTest(tool=name):
                    self.assertTrue(await self.rejected(client, name, arguments))
        self.assertEqual(len(self.backend.calls), calls)
        self.assertFalse(any(kind == "book" for kind, _ in self.backend.calls))
        self.assertIn(review["review_id"], self.service.reviews)  # untouched

    async def test_owner_sign_in_is_invisible_and_untouchable_remotely(self):
        self.backend.gate = asyncio.Event()
        self.backend.mfa = True
        login = self.service.start("login")
        await asyncio.sleep(0.01)
        self.assertEqual(self.service.get(login["operation_id"]).status, "awaiting_mfa")
        review = await self.local_review()
        async with Client(create_server(self.service, remote=RemoteAccess())) as client:
            status = (await client.call_tool("server_status", {})).structured_content
            self.assertEqual(status["operations"], [])
            self.assertEqual(status["pending_reviews"], [])
            self.assertNotIn(login["operation_id"], json.dumps(status))
            self.assertNotIn(review["review_id"], json.dumps(status))
            for name in (
                "cancel_operation",
                "operation_status",
                "wait_operation",
                "operation_result",
                "operation_receipt",
                "support_bundle",
            ):
                with self.subTest(tool=name):
                    self.assertTrue(
                        await self.rejected(
                            client, name, {"operation_id": login["operation_id"]}
                        )
                    )
            # Its own work is listed and can be stopped.
            mine = (await client.call_tool("list_tasks", {})).structured_content
            status = (await client.call_tool("server_status", {})).structured_content
            self.assertEqual(
                [op["operation_id"] for op in status["operations"]],
                [mine["operation_id"]],
            )
            stopped = await client.call_tool(
                "cancel_operation", {"operation_id": mine["operation_id"]}
            )
            self.assertEqual(stopped.structured_content["status"], "cancelled")
        self.assertEqual(self.service.get(login["operation_id"]).status, "awaiting_mfa")
        async with Client(create_server(self.service)) as client:
            status = (await client.call_tool("server_status", {})).structured_content
            # The owner's sign-in, the search behind their review, the remote read.
            self.assertEqual(len(status["operations"]), 3)
            self.assertEqual(len(status["pending_reviews"]), 1)
            stopped = await client.call_tool(
                "cancel_operation", {"operation_id": login["operation_id"]}
            )
            self.assertEqual(stopped.structured_content["status"], "cancelled")

    async def local_review(self):
        # Prepared at the host before anything is waiting on the browser queue.
        gate, self.backend.gate = self.backend.gate, None
        search = AgentService(backend=FakeBackend(), clock=lambda: self.now)
        job = search.search(CONFIG)
        await search.get(job["operation_id"]).task
        self.service.jobs[job["operation_id"]] = search.get(job["operation_id"])
        self.backend.gate = gate
        return self.service.review_booking(
            job["operation_id"], ROOM["name"], "Local", "friend@scis.smu.edu.sg"
        )

    async def test_write_enabled_key_cannot_submit_what_the_owner_prepared(self):
        review = await self.review()
        async with Client(
            create_server(
                self.service,
                allow_agent_submission=True,
                remote=RemoteAccess(writes=True),
            )
        ) as client:
            status = (await client.call_tool("server_status", {})).structured_content
            self.assertEqual(status["pending_reviews"], [])
            for name in ("submit_review", "get_review", "discard_review"):
                with self.subTest(tool=name):
                    self.assertTrue(
                        await self.rejected(
                            client, name, {"review_id": review["review_id"]}
                        )
                    )
        self.assertFalse(any(kind == "book" for kind, _ in self.backend.calls))
        self.assertIn(review["review_id"], self.service.reviews)

    async def test_status_and_workflow_describe_the_remote_connection(self):
        async with Client(create_server(self.service, remote=RemoteAccess())) as client:
            status = (await client.call_tool("server_status", {})).structured_content
            self.assertEqual(status["transport"], "streamable-http")
            self.assertEqual(status["submission_mode"], "disabled")
            self.assertFalse(status["can_submit"])
            self.assertFalse(status["agent_submission_enabled"])
            self.assertIsNone(status["submission_tool"])
            self.assertIsNone(status["authentication_prompts"])
            self.assertTrue(status["remote_access"]["read_only"])
            self.assertEqual(
                status["remote_access"]["sign_in"], "not_known_to_be_required"
            )
            self.assertIn("login", status["remote_access"]["unavailable_tools"])
            workflow = json.loads(
                (await client.read_resource("sagasu://workflow")).contents[0].text
            )
            self.assertIsNone(workflow["submission_tool"])
            self.assertTrue(workflow["remote_access"]["read_only"])
            self.assertNotIn("login", " ".join(workflow["authentication"]).split())
            self.service.sign_in_retry_at = self.now + 60
            status = (await client.call_tool("server_status", {})).structured_content
            self.assertEqual(status["remote_access"]["sign_in"], "required_on_host")
        async with Client(
            create_server(
                self.service,
                allow_agent_submission=True,
                remote=RemoteAccess(writes=True),
            )
        ) as client:
            status = (await client.call_tool("server_status", {})).structured_content
            self.assertEqual(status["submission_mode"], "agent")
            self.assertEqual(status["submission_tool"], "submit_review")
            self.assertFalse(status["remote_access"]["read_only"])
        async with Client(create_server(self.service)) as client:
            status = (await client.call_tool("server_status", {})).structured_content
            self.assertEqual(status["transport"], "stdio")
            self.assertNotIn("remote_access", status)

    async def test_remote_calls_mark_their_jobs_and_are_logged_without_secrets(self):
        labels = []
        access = RemoteAccess(caller=lambda headers: labels.append(headers) or "x")
        async with Client(create_server(self.service, remote=access)) as client:
            with self.assertLogs("sagasu.observability", level="INFO") as logs:
                started = await client.call_tool("list_bookings", {})
                await self.finish(started.structured_content)
                await self.rejected(client, "login")
        self.assertEqual(self.backend.origins, [True])
        self.assertFalse(is_remote())  # the origin never leaks out of the call
        calls = [
            record.fields
            for record in logs.records
            if record.getMessage() == "remote.tool_call"
        ]
        self.assertEqual(
            calls,
            [
                {"tool": "list_bookings", "key": "unknown", "outcome": "ok"},
                {"tool": "unknown", "key": "unknown", "outcome": "rejected"},
            ],
        )
        async with Client(create_server(self.service)) as client:
            started = await client.call_tool("list_bookings", {})
            await self.finish(started.structured_content)
        self.assertEqual(self.backend.origins, [True, False])


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipIf(os.name == "nt" or Client is None, "Unix host with MCP runtime")
class RemoteListenerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.port = free_port()
        env = Path(self.directory.name) / ".env"
        env.write_text(
            "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\n"
            f"SAGASU_HTTP_PORT={self.port}\nSAGASU_HTTP_ALLOWED_HOSTS=sagasu.test\n"
            f"SAGASU_HTTP_KEYS=laptop:{DIGEST}\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            self.settings = Settings.load(env)
        self.backend = OriginBackend()
        self.service = AgentService(backend=self.backend)
        self.host = asyncio.create_task(
            serve_host(self.settings, service=self.service, idle_seconds=0.05)
        )
        self.addAsyncCleanup(self.stop_host)
        for _ in range(300):
            if self.host.done() or await self.listening():
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.host.done(), "host exited during startup")
        self.session = None
        self.sequence = 0

    async def listening(self):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", self.port)
        except OSError:
            return False
        writer.close()
        await writer.wait_closed()
        return True

    async def stop_host(self):
        if not self.host.done():
            self.host.cancel()
        await asyncio.gather(self.host, return_exceptions=True)

    async def http(self, method="POST", payload=None, *, key=KEY, headers=None):
        def call():
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
            try:
                sent = {
                    "Host": "sagasu.test",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    **({"Authorization": "Bearer " + key} if key else {}),
                    **({"Mcp-Session-Id": self.session} if self.session else {}),
                    **(headers or {}),
                }
                connection.request(
                    method,
                    "/mcp",
                    body=None if payload is None else json.dumps(payload),
                    headers=sent,
                )
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        return await asyncio.to_thread(call)

    async def rpc(self, method, params=None):
        self.sequence += 1
        packet = {"jsonrpc": "2.0", "id": self.sequence, "method": method}
        if params is not None:
            packet["params"] = params
        status, headers, body = await self.http(payload=packet)
        self.assertEqual(status, 200, body)
        self.session = self.session or {
            name.lower(): value for name, value in headers.items()
        }.get("mcp-session-id")
        return json.loads(body)["result"]

    async def initialize(self):
        result = await self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stdlib-http-test", "version": "1"},
            },
        )
        status, _, _ = await self.http(
            payload={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertIn(status, {200, 202})
        return result

    async def tool(self, name, arguments=None):
        result = await self.rpc(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    async def test_nothing_is_served_without_a_configured_key(self):
        packet = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        for key in (None, "wrong-key-" + "w" * 40, KEY[:-1], DIGEST, ""):
            with self.subTest(key=key):
                status, headers, body = await self.http(payload=packet, key=key)
                self.assertEqual(status, 401)
                self.assertIn(
                    "Bearer",
                    {k.lower(): v for k, v in headers.items()}["www-authenticate"],
                )
                self.assertNotIn(b"search_rooms", body)
        for name, value in (
            ("X-Api-Key", "wrong-key-" + "w" * 40),
            ("X-Api-Key", "Bearer wrong-key-" + "w" * 40),
            ("X-Api-Key", "short"),
            ("X-Sagasu-Key", KEY),
            ("Proxy-Authorization", "Bearer " + KEY),
        ):
            with self.subTest(header=name):
                status, _, _ = await self.http(
                    payload=packet, key=None, headers={name: value}
                )
                self.assertEqual(status, 401)
        self.assertEqual((await self.http("GET", key=None))[0], 401)
        self.assertEqual((await self.http("DELETE", key=None))[0], 401)
        self.assertEqual(self.backend.calls, [])

    async def raw(self, lines, body=b""):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.addAsyncCleanup(self.close, writer)
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        await writer.drain()
        return reader, writer

    @staticmethod
    async def close(writer):
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def test_exactly_one_credential_is_accepted(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        for first, second in (("x" * 30, KEY), (KEY, "x" * 30), (KEY, KEY)):
            with self.subTest(first=first == KEY, second=second == KEY):
                reader, _ = await self.raw(
                    [
                        "POST /mcp HTTP/1.1",
                        "Host: sagasu.test",
                        "Content-Type: application/json",
                        "Accept: application/json, text/event-stream",
                        f"Authorization: Bearer {first}",
                        f"Authorization: Bearer {second}",
                        f"Content-Length: {len(body)}",
                    ],
                    body,
                )
                status = await asyncio.wait_for(reader.readline(), 5)
                self.assertIn(b" 401 ", status)

    async def test_the_key_may_arrive_alone_in_x_api_key(self):
        # For agent platforms that reserve Authorization for themselves.
        packet = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        status, _, body = await self.http(
            payload=packet, key=None, headers={"x-api-key": KEY}
        )
        self.assertEqual(status, 200)
        self.assertIn(b"search_rooms", body)
        with self.assertLogs("sagasu.observability", level="INFO") as logs:
            status, _, _ = await self.http(
                payload={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "server_status", "arguments": {}},
                },
                key=None,
                headers={"X-API-Key": KEY},
            )
        self.assertEqual(status, 200)
        calls = [r.fields for r in logs.records if r.getMessage() == "remote.tool_call"]
        self.assertEqual(calls[0]["key"], "laptop")  # same label as the other header
        # "Bearer <key>" pasted into this header by habit means the same thing.
        status, _, _ = await self.http(
            payload=packet, key=None, headers={"X-API-Key": "Bearer " + KEY}
        )
        self.assertEqual(status, 200)
        # Never both: one credential, in one header.
        status, _, _ = await self.http(payload=packet, headers={"X-API-Key": KEY})
        self.assertEqual(status, 401)
        body = json.dumps(packet).encode()
        reader, _ = await self.raw(
            [
                "POST /mcp HTTP/1.1",
                "Host: sagasu.test",
                "Content-Type: application/json",
                "Accept: application/json, text/event-stream",
                f"X-API-Key: {KEY}",
                f"X-API-Key: {KEY}",
                f"Content-Length: {len(body)}",
            ],
            body,
        )
        self.assertIn(b" 401 ", await asyncio.wait_for(reader.readline(), 5))

    async def test_a_refused_request_is_dropped_without_waiting_for_its_body(self):
        reader, _ = await self.raw(
            [
                "POST /mcp HTTP/1.1",
                "Host: sagasu.test",
                "Content-Type: application/json",
                "Content-Length: 100000",
            ],
            b"{",  # the rest never arrives
        )
        answer = await asyncio.wait_for(reader.read(), 5)  # until the server closes
        self.assertTrue(answer.startswith(b"HTTP/1.1 401 "))

    async def test_keyed_requests_have_their_own_allowance(self):
        self.backend.gate = asyncio.Event()
        started = await self.tool("list_bookings")
        with patch("sagasu.remote.MAX_IN_FLIGHT", 1):
            waiting = asyncio.create_task(
                self.tool(
                    "wait_operation",
                    {"operation_id": started["operation_id"], "timeout": 10},
                )
            )
            await asyncio.sleep(0.2)
            packet = {"jsonrpc": "2.0", "id": 99, "method": "tools/list"}
            status, headers, _ = await self.http(payload=packet)
            self.assertEqual(status, 503)
            self.assertEqual(
                {k.lower(): v for k, v in headers.items()}["retry-after"], "1"
            )
            # Callers without a key do not consume it and are still told 401.
            self.assertEqual((await self.http(payload=packet, key=None))[0], 401)
            self.backend.gate.set()
            self.assertEqual((await waiting)["status"], "succeeded")
            self.assertEqual((await self.http(payload=packet))[0], 200)

    async def test_stop_completes_while_a_keyed_request_is_still_arriving(self):
        await self.raw(
            [
                "POST /mcp HTTP/1.1",
                "Host: sagasu.test",
                "Content-Type: application/json",
                "Accept: application/json, text/event-stream",
                f"Authorization: Bearer {KEY}",
                "Content-Length: 100000",
            ],
            b"{",
        )
        await asyncio.sleep(0.2)
        reply = await control(self.settings, "stop")
        self.assertTrue(reply["stopping"])
        self.assertTrue(reply["shutdown_complete"])
        await asyncio.wait_for(self.host, 10)

    async def test_command_attaches_once_the_supervisor_has_restarted_the_service(
        self,
    ):
        await control(self.settings, "stop")
        await asyncio.wait_for(self.host, 30)
        with patch("sagasu.host.subprocess.Popen") as spawn:
            attaching = asyncio.create_task(connect(self.settings))
            await asyncio.sleep(0.6)
            self.assertFalse(attaching.done())
            self.host = asyncio.create_task(
                serve_host(self.settings, service=AgentService(backend=self.backend))
            )
            _, writer, reply = await asyncio.wait_for(attaching, 15)
            self.addAsyncCleanup(self.close, writer)
        self.assertTrue(reply["ok"])
        spawn.assert_not_called()

    async def test_the_log_says_why_a_key_was_refused_but_never_what_was_sent(self):
        from sagasu.remote import check_key

        keys = {DIGEST: "laptop"}
        cases = {
            "no_key_header": {},
            "several_key_headers": {"authorization": "Bearer " + KEY, "x-api-key": KEY},
            "not_a_bearer_key": {"authorization": "Basic " + KEY},
            "wrong_length": {"x-api-key": "short"},
            "unknown_key": {"x-api-key": "wrong-key-" + "w" * 40},
        }
        for reason, headers in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(check_key(keys, headers), (None, reason))
        self.assertEqual(check_key(keys, {"x-api-key": KEY}), ("laptop", None))
        packet = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        secret = "wrong-key-" + "w" * 40
        with self.assertLogs("sagasu.observability", level="WARNING") as logs:
            await self.http(payload=packet, key=None, headers={"X-API-Key": secret})
        record = logs.records[0]
        self.assertEqual(record.fields["reasons"], {"unknown_key": 1})
        self.assertNotIn(secret, json.dumps(record.fields))

    async def test_only_post_is_served_so_no_stream_or_session_is_held(self):
        for method in ("GET", "DELETE", "PUT"):
            with self.subTest(method=method):
                status, headers, _ = await self.http(method)
                self.assertEqual(status, 405)
                self.assertEqual(
                    {k.lower(): v for k, v in headers.items()}["allow"], "POST"
                )

    async def test_requests_are_independent_and_survive_a_restart(self):
        # No handshake and no session: what a client sees after a restart.
        status = await self.tool("server_status")
        self.assertEqual(status["transport"], "streamable-http")
        self.assertIsNone(self.session)
        self.session = "session-id-from-before-a-restart"
        names = {tool["name"] for tool in (await self.rpc("tools/list"))["tools"]}
        self.assertIn("search_rooms", names)
        self.assertNotIn("login", names)

    async def test_other_host_names_and_browser_origins_are_refused(self):
        packet = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        status, _, _ = await self.http(payload=packet, headers={"Host": "evil.test"})
        self.assertEqual(status, 421)
        status, _, _ = await self.http(
            payload=packet, headers={"Host": f"127.0.0.1:{self.port}"}
        )
        self.assertEqual(status, 421)
        status, _, _ = await self.http(
            payload=packet, headers={"Origin": "https://evil.test"}
        )
        self.assertEqual(status, 403)

    async def test_a_keyed_agent_reads_through_the_restricted_server(self):
        initialized = await self.initialize()
        self.assertIn("REMOTE CONNECTION", initialized["instructions"])
        self.assertIn("READ-ONLY", initialized["instructions"])
        names = {tool["name"] for tool in (await self.rpc("tools/list"))["tools"]}
        self.assertFalse(names & (REMOTE_HIDDEN_TOOLS | REMOTE_WRITE_TOOLS))
        self.assertIn("search_rooms", names)
        status = await self.tool("server_status")
        self.assertEqual(status["transport"], "streamable-http")
        self.assertFalse(status["can_submit"])
        started = await self.tool("list_bookings")
        finished = await self.tool(
            "wait_operation", {"operation_id": started["operation_id"]}
        )
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(self.backend.origins, [True])
        refused = await self.rpc("tools/call", {"name": "login", "arguments": {}})
        self.assertTrue(refused["isError"])
        self.assertEqual(self.backend.origins, [True])

    async def test_the_sdk_client_connects_with_a_key_in_its_headers(self):
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + KEY, "Host": "sagasu.test"}
        ) as http_client:
            async with streamable_http_client(
                f"http://127.0.0.1:{self.port}/mcp", http_client=http_client
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    self.assertIn("search_rooms", names)
                    self.assertNotIn("login", names)
                    status = await session.call_tool("server_status", {})
                    self.assertFalse(status.is_error)
                    self.assertEqual(
                        status.structured_content["transport"], "streamable-http"
                    )
                    started = await session.call_tool(
                        "search_rooms", {"config": CONFIG}
                    )
                    finished = await session.call_tool(
                        "wait_operation",
                        {"operation_id": started.structured_content["operation_id"]},
                    )
                    self.assertEqual(finished.structured_content["status"], "succeeded")
        self.assertEqual(self.backend.origins, [True])

    async def test_service_never_idles_out_and_the_local_socket_stays_complete(self):
        await asyncio.sleep(0.4)  # eight idle periods
        self.assertFalse(self.host.done())
        async with client_session(self.settings) as local:
            names = {tool.name for tool in (await local.list_tools()).tools}
            self.assertIn("login", names)
            self.assertIn("submit_review", names)
            started = (await local.call_tool("login", {})).structured_content
            await local.call_tool(
                "wait_operation", {"operation_id": started["operation_id"]}
            )
        self.assertEqual(self.backend.origins, [False])
        reply = await control(self.settings, "status")
        self.assertTrue(reply["service_mode"])
        self.assertTrue(reply["remote_listener"])

    async def test_stop_closes_the_listener_and_the_host(self):
        reply = await control(self.settings, "stop")
        self.assertTrue(reply["stopping"])
        await asyncio.wait_for(self.host, 30)
        self.assertFalse(await self.listening())

    async def test_command_line_never_starts_a_second_service(self):
        await control(self.settings, "stop")
        await asyncio.wait_for(self.host, 30)
        with (
            patch("sagasu.host.subprocess.Popen") as spawn,
            patch("sagasu.host.SERVICE_WAIT_SECONDS", 0.3),
            self.assertRaises(HostError) as caught,
        ):
            await connect(self.settings)
        self.assertEqual(caught.exception.reason, "host_service_stopped")
        spawn.assert_not_called()


@unittest.skipIf(os.name == "nt" or Client is None, "Unix host with MCP runtime")
class ServiceProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_stops_the_real_service_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            port = free_port()
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\n"
                f"SAGASU_HTTP_PORT={port}\nSAGASU_HTTP_ALLOWED_HOSTS=sagasu.test\n"
                f"SAGASU_HTTP_KEYS=laptop:{DIGEST}\n"
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("SAGASU_", "SMU_"))
            }
            with patch.dict(os.environ, environment, clear=True):
                path = socket_path(Settings.load(env_file))
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "sagasu",
                "host-run",
                "--env-file",
                str(env_file),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            try:
                for _ in range(300):
                    with socket.socket() as probe:
                        probe.settimeout(0.2)
                        if probe.connect_ex(("127.0.0.1", port)) == 0:
                            break
                    self.assertIsNone(process.returncode, "service exited early")
                    await asyncio.sleep(0.05)
                else:
                    self.fail("service never listened")
                self.assertTrue(path.exists())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(await asyncio.wait_for(process.wait(), 20), 0)
                self.assertFalse(path.exists())
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()


@unittest.skipIf(os.name == "nt" or Client is None, "Unix host with MCP runtime")
class MisconfiguredListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_requested_but_invalid_listener_stays_closed_and_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            port = free_port()
            env = Path(directory) / ".env"
            env.write_text(
                "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\n"
                f"SAGASU_HTTP_PORT={port}\n"  # no keys, no host names
            )
            with patch.dict(os.environ, {}, clear=True):
                settings = Settings.load(env, diagnostic=True)
            host = asyncio.create_task(serve_host(settings, idle_seconds=0.05))
            try:
                await asyncio.sleep(0.4)
                self.assertFalse(host.done())  # supervised: stays up to be inspected
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
                reply = await control(settings, "status")
                self.assertEqual(reply["runtime_mode"], "diagnostic")
                self.assertFalse(reply["remote_listener"])
            finally:
                host.cancel()
                await asyncio.gather(host, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
