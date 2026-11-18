"""Signing in from chat: the job runner and MCP surface for a saved SMU sign-in.

A stand-in backend plays the browser client: it takes the permit the job runner
handed the job, shows an Authenticator number through notify, and ends the
permit with the outcome the test chooses. The real store encrypts to a temp
directory. Nothing contacts FBS, Microsoft or any other service.
"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sagasu import errors
from sagasu.errors import REASONS
from sagasu.fbs_client import SAVED_SIGN_IN_REASONS, AuthenticationRequired, FBSClient
from sagasu.mcp_service import (
    HOLDS,
    NUMBER_GRACE_SECONDS,
    PERSON_NEEDED,
    AgentService,
    Backend,
)
from sagasu.origin import is_remote
from sagasu.saved_sign_in import (
    PASSWORDS_PER_DAY,
    SAVED_CREDENTIALS,
    SIGN_INS_PER_HOUR,
    Permit,
    SavedSignIn,
    make_key_file,
)
from tests.test_remote import remote_origin

try:
    from mcp import Client
    from sagasu.mcp_server import RemoteAccess, create_server
except ImportError:
    Client = None

EMAIL = "fixture.person@smu.edu.sg"
PASSWORD = "Fixture pass-word #1"
NUMBER = "42"
NEW_REASONS = (
    "sign_in_setup_needed",
    "saved_password_rejected",
    "sign_in_not_approved",
    "sign_in_unfinished",
    "sign_in_needs_page",
    "sign_in_blocked",
    "sign_in_attempts_exceeded",
    "sign_in_needed",
)


class SignInBackend:
    """Plays FBSClient around a lapsed SMU session.

    With a permit it signs in as the client does: takes the permit, counts the
    password, shows a number, waits for the test's answer and finishes the
    permit with it. Without one, a remote job fails as silent renewal does.
    """

    def __init__(self):
        self.calls = []  # (kind, permit) each time the browser was opened
        self.expired = True
        self.number = NUMBER
        self.notify = None
        self.answer = None

    async def run(self, kind, payload, progress, notify):
        permit = SAVED_CREDENTIALS.get()
        self.calls.append((kind, permit))
        await progress("Opening FBS")
        if self.expired:
            if not is_remote():
                await self.ask(notify)  # the person signs in at the host
            elif permit is None:
                raise AuthenticationRequired(
                    "SMU sign-in is required on the host.",
                    reason="sign_in_required_on_host",
                )
            elif kind == "session":
                # As FBSClient: a check that promises not to sign in.
                raise AuthenticationRequired("Session ended.", reason="sign_in_needed")
            else:
                if not permit.take():
                    raise AuthenticationRequired("Once.", reason="sign_in_unfinished")
                permit.typing_password()
                outcome = await self.ask(notify)
                permit.finish(outcome)
                if outcome != "signed_in":
                    raise AuthenticationRequired("Did not finish.", reason=outcome)
            await progress("Reading FBS")
            self.expired = False
        if kind == "tasks":
            return {"tasks": [], "total": 0, "complete": True}
        return {"authenticated": True}

    async def ask(self, notify):
        self.notify = notify
        self.answer = asyncio.get_running_loop().create_future()
        shown = f"number {self.number}" if self.number else "the request"
        await notify(f"Open Microsoft Authenticator. Enter {shown}. Then approve.")
        return await self.answer


class ChatSignInCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.key = make_key_file(self.root / "sign-in.key")
        self.now = 1000.0
        self.store = self.make_store()
        self.store.save(EMAIL, PASSWORD)
        self.backend = SignInBackend()
        self.service = self.make_service()

    def make_store(self):
        return SavedSignIn(self.root, self.key, clock=lambda: self.now)

    def make_service(self):
        service = AgentService(backend=self.backend, clock=lambda: self.now)
        service.saved_sign_in = self.make_store()
        self.addAsyncCleanup(self.close, service)
        return service

    async def close(self, service):
        if self.backend.answer is not None and not self.backend.answer.done():
            self.backend.answer.cancel()
        await service.close()

    def remote(self, kind="tasks", service=None):
        with remote_origin():
            return (service or self.service).start(kind)["operation_id"]

    async def finish(self, operation_id, service=None):
        service = service or self.service
        await service.get(operation_id).task
        return service.get(operation_id)

    async def until(self, condition):
        for _ in range(400):
            if condition():
                return
            await asyncio.sleep(0.005)
        self.fail("timed out waiting for the stand-in")

    async def until_asked(self):
        """Until a sign-in shows its number and waits for the phone."""
        await self.until(
            lambda: self.backend.answer is not None
            and not self.backend.answer.done()
        )

    async def sign_in(self, outcome, kind="tasks"):
        operation_id = self.remote(kind)
        await self.until_asked()
        self.backend.answer.set_result(outcome)
        return await self.finish(operation_id)

    async def wait(self, operation_id, timeout=5):
        with remote_origin():
            return await self.service.wait(operation_id, timeout)

    def ledger(self):
        try:
            return json.loads(self.store.ledger_path.read_text())
        except FileNotFoundError:
            return {}

    def assert_final(self, job, reason):
        """Only the person can act: never retry, and go where REASONS says."""
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error["reason"], reason)
        self.assertFalse(job.error["retry_safe"])
        self.assertEqual(job.error["next_tool"], REASONS[reason][2])
        self.assertEqual(job.public()["next_tool"], REASONS[reason][2])


class PermitTests(ChatSignInCase):
    async def test_only_a_remote_job_with_a_saved_password_gets_a_permit(self):
        self.backend.expired = False
        first = await self.finish(self.remote())
        self.assertEqual(first.status, "succeeded")
        permit = self.backend.calls[-1][1]
        self.assertIsInstance(permit, Permit)
        self.assertEqual(permit.credentials.email, EMAIL)
        self.assertTrue(permit.saved)
        self.assertFalse(permit.used)  # the session was still valid
        await self.finish(self.remote())
        self.assertIsNot(self.backend.calls[-1][1], permit)  # one per job
        await self.finish(self.service.start("tasks")["operation_id"])  # at the host
        self.assertIsNone(self.backend.calls[-1][1])
        self.store.forget_password()
        await self.finish(self.remote())
        self.assertIsNone(self.backend.calls[-1][1])
        self.assertIsNone(SAVED_CREDENTIALS.get())  # never leaks out of a job
        self.assertEqual(self.ledger().get("starts", []), [])

    async def test_without_a_store_remote_sign_in_stays_on_the_host(self):
        self.service.saved_sign_in = None
        failed = await self.finish(self.remote())
        self.assertIsNone(self.backend.calls[-1][1])
        self.assertEqual(failed.error["reason"], "sign_in_required_on_host")
        self.assertTrue(self.service.host_sign_in_required)
        calls = len(self.backend.calls)
        refused = await self.finish(self.remote())
        self.assertEqual(refused.error["reason"], "sign_in_required_on_host")
        self.assertEqual(len(self.backend.calls), calls)


class NumberInChatTests(ChatSignInCase):
    async def test_the_number_reaches_remote_results_and_nowhere_else(self):
        operation_id = self.remote()
        await self.until_asked()
        started = time.monotonic()
        view = await self.wait(operation_id, 10)
        self.assertLess(time.monotonic() - started, 2)  # returned at once
        self.assertEqual(view["status"], "awaiting_mfa")
        sign_in = view["sign_in"]
        self.assertEqual(
            set(sign_in),
            {"number", "tell_user", "agent_action", "started_by_this_operation"},
        )
        self.assertEqual(sign_in["number"], NUMBER)
        self.assertIn(NUMBER, sign_in["tell_user"])
        self.assertIn("done", sign_in["tell_user"])
        self.assertIn("wait_operation", sign_in["agent_action"])
        self.assertTrue(sign_in["started_by_this_operation"])
        self.assertIsNone(view["poll_after_ms"])
        self.assertEqual(view["next_tool"], "wait_operation")
        job = self.service.get(operation_id)
        with remote_origin():
            for output in (self.service.status_view(job), self.service.result(operation_id)):
                self.assertEqual(output["sign_in"]["number"], NUMBER)
        # Not in the job's own record, nor for the owner at the host.
        for output in (job.public(), self.service.result(operation_id)):
            self.assertNotIn("sign_in", output)
            text = json.dumps(output).replace(operation_id, "")
            self.assertNotIn(NUMBER, text)
            self.assertNotIn("Enter number", text)
        self.backend.answer.set_result("signed_in")
        done = await self.finish(operation_id)
        self.assertEqual(done.status, "succeeded")
        with remote_origin():
            self.assertNotIn("sign_in", self.service.result(operation_id))
        self.assertEqual(len(self.ledger()["starts"]), 1)
        self.assertEqual(len(self.ledger()["passwords"]), 1)
        self.assertTrue(self.store.summary()["verified"])
        self.assertIsNone(self.store.held())

    async def test_a_second_wait_blocks_until_a_new_number_or_the_end(self):
        operation_id = self.remote()
        await self.until_asked()
        self.assertEqual((await self.wait(operation_id))["sign_in"]["number"], NUMBER)
        # The person has the number; the agent waits after they reply "done".
        second = asyncio.create_task(self.wait(operation_id, 5))
        await asyncio.sleep(0.1)
        self.assertFalse(second.done())
        await self.backend.notify("Open Microsoft Authenticator. Enter number 57.")
        view = await asyncio.wait_for(second, 2)
        self.assertEqual(view["sign_in"]["number"], "57")
        third = asyncio.create_task(self.wait(operation_id, 5))
        await asyncio.sleep(0.1)
        self.assertFalse(third.done())
        self.backend.answer.set_result("signed_in")
        view = await asyncio.wait_for(third, 2)
        self.assertEqual(view["status"], "succeeded")
        self.assertNotIn("sign_in", view)

    async def test_a_job_queued_behind_the_sign_in_sees_the_same_number(self):
        first = self.remote()
        await self.until_asked()
        second = self.remote()
        view = await self.wait(second, 5)
        self.assertEqual(view["status"], "queued")
        self.assertEqual(view["sign_in"]["number"], NUMBER)
        self.assertFalse(view["sign_in"]["started_by_this_operation"])
        self.assertIsNone(view["poll_after_ms"])
        self.assertEqual(view["next_tool"], "wait_operation")
        self.assertTrue((await self.wait(first, 5))["sign_in"]["started_by_this_operation"])
        self.backend.answer.set_result("signed_in")
        self.assertEqual((await self.finish(first)).status, "succeeded")
        self.assertEqual((await self.finish(second)).status, "succeeded")
        self.assertFalse(self.backend.calls[-1][1].used)
        self.assertEqual(len(self.ledger()["starts"]), 1)  # one phone request

    async def test_a_sign_in_at_the_host_never_reaches_a_remote_caller(self):
        local = self.service.start("login")
        await self.until(
            lambda: self.service.get(local["operation_id"]).status == "awaiting_mfa"
        )
        operation_id = self.remote()
        view = await self.wait(operation_id, 0.2)
        self.assertNotIn("sign_in", view)
        self.assertNotIn(NUMBER, json.dumps(view).replace(operation_id, ""))
        self.backend.answer.set_result("signed_in")
        self.assertEqual((await self.finish(operation_id)).status, "succeeded")

    async def test_without_a_number_the_person_is_told_to_approve_after_a_grace(self):
        self.backend.number = None
        operation_id = self.remote()
        await self.until_asked()
        view = await self.wait(operation_id, 0.2)
        self.assertNotIn("sign_in", view)  # the number may still be on its way
        self.assertEqual(view["next_tool"], "wait_operation")
        self.now += NUMBER_GRACE_SECONDS
        view = await self.wait(operation_id, 5)
        self.assertIsNone(view["sign_in"]["number"])
        self.assertIn("approve", view["sign_in"]["tell_user"])
        self.backend.answer.set_result("signed_in")
        await self.finish(operation_id)


class HoldTests(ChatSignInCase):
    async def test_each_failed_sign_in_holds_and_refuses_without_the_browser(self):
        for outcome in sorted(set(HOLDS) - {"saved_password_rejected"}):
            with self.subTest(outcome=outcome):
                self.store.forget_ledger()
                self.backend.expired = True
                failed = await self.sign_in(outcome)
                self.assert_final(failed, outcome)
                held = self.store.held()
                self.assertEqual(held["reason"], outcome)
                self.assertEqual(held["page_only"], HOLDS[outcome])
                self.assertIsNotNone(self.store.load())  # the password is kept
                calls = len(self.backend.calls)
                refused = await self.finish(self.remote())
                self.assert_final(refused, outcome)
                self.assertEqual(len(self.backend.calls), calls)

    async def test_a_rejected_password_is_forgotten_and_held_for_the_page(self):
        failed = await self.sign_in("saved_password_rejected")
        self.assert_final(failed, "saved_password_rejected")
        self.assertIsNone(self.store.load())
        summary = self.store.summary()
        self.assertFalse(summary["saved"])
        self.assertIsNotNone(summary["email"])  # the masked email is kept
        self.assertEqual(
            self.store.held(),
            {"reason": "saved_password_rejected", "page_only": True, "at": self.now},
        )

    # Regression (fixed): _remote_permit loads the credentials before it checks the hold, so
    # once a rejected password is forgotten the next remote job reopens the
    # browser and reports sign_in_setup_needed instead of the held reason
    # (contrary to HOLDS and the "say so without opening the browser" comment).
    async def test_after_a_rejected_password_the_next_job_is_refused_unopened(self):
        await self.sign_in("saved_password_rejected")
        calls = len(self.backend.calls)
        refused = await self.finish(self.remote())
        self.assertEqual(len(self.backend.calls), calls)
        self.assert_final(refused, "saved_password_rejected")

    async def test_a_remote_login_lifts_an_earlier_chat_hold(self):
        await self.sign_in("sign_in_not_approved")
        self.now += 60
        refused = await self.finish(self.remote())
        self.assert_final(refused, "sign_in_not_approved")
        login = self.remote("login")
        await self.until_asked()
        kind, permit = self.backend.calls[-1]
        self.assertEqual(kind, "login")
        self.assertIsInstance(permit, Permit)
        self.backend.answer.set_result("signed_in")
        self.assertEqual((await self.finish(login)).status, "succeeded")
        self.assertIsNone(self.store.held())

    async def test_a_remote_login_never_lifts_a_page_only_hold(self):
        for outcome in ("sign_in_blocked", "sign_in_needs_page"):
            with self.subTest(outcome=outcome):
                self.store.forget_ledger()
                self.backend.expired = True
                await self.sign_in(outcome)
                self.now += 60
                calls = len(self.backend.calls)
                refused = await self.finish(self.remote("login"))
                self.assert_final(refused, outcome)
                self.assertEqual(len(self.backend.calls), calls)
                self.assertEqual(self.store.held()["reason"], outcome)

    async def test_a_hold_set_after_the_login_was_asked_for_stays(self):
        first = self.remote()
        await self.until_asked()
        self.now = 1010
        login = self.remote("login")  # queued behind the sign-in
        self.now = 1020
        self.backend.answer.set_result("sign_in_not_approved")
        self.assert_final(await self.finish(first), "sign_in_not_approved")
        # The person's answer came after the ask: it stands.
        self.assert_final(await self.finish(login), "sign_in_not_approved")
        self.assertEqual([kind for kind, _ in self.backend.calls], ["tasks"])
        self.assertEqual(self.store.held()["reason"], "sign_in_not_approved")
        self.now = 1030
        again = self.remote("login")
        await self.until_asked()
        self.backend.answer.set_result("signed_in")
        self.assertEqual((await self.finish(again)).status, "succeeded")


class ReviewRegressionTests(ChatSignInCase):
    """Cases found by the independent reviews of this change."""

    async def test_a_number_already_shown_is_not_shown_again_elsewhere(self):
        first = self.remote()
        await self.until_asked()
        second = self.remote()
        # The agent reads the second request first and shows the number.
        with remote_origin():
            shown = self.service.result(second)
        self.assertEqual(shown["sign_in"]["number"], NUMBER)
        self.assertFalse(shown["sign_in"]["started_by_this_operation"])
        # "done": a wait on the first must not hand back the same number.
        waiting = asyncio.create_task(self.wait(first, 5))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        self.backend.answer.set_result("signed_in")
        result = await waiting
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("sign_in", result)

    async def test_a_password_mistyped_on_the_page_does_not_hold_chat(self):
        permit = self.service.sign_in_permit(
            self.store.load(), saved=False, how="page", by="laptop"
        )
        permit.take()
        permit.typing_password()
        permit.finish("saved_password_rejected")
        self.assertIsNone(self.store.held())
        self.assertIsNotNone(self.store.load())  # the saved one still works
        self.assertEqual(self.store.recent()[-1]["outcome"], "saved_password_rejected")

    async def test_a_renewal_that_times_out_with_nothing_saved_asks_to_save(self):
        # Live finding: with prompts blocked, Microsoft's page may never settle,
        # so the client reports a plain expiry rather than a sign-in prompt.
        self.store.erase()
        self.backend.run = AsyncMock(
            side_effect=AuthenticationRequired("FBS session expired")
        )
        job = await self.finish(self.remote())
        self.assertEqual(job.error["reason"], "sign_in_setup_needed")
        self.assertFalse(job.error["retry_safe"])

    async def test_a_write_stopped_while_signing_in_is_known_not_submitted(self):
        with remote_origin():
            operation = self.service.start("cancel", ({"reference_number": "BK-1"}, "x"))[
                "operation_id"
            ]
        await self.until_asked()
        job = self.service.get(operation)
        job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)
        self.assertEqual(job.status, "cancelled")


class LimitTests(ChatSignInCase):
    async def test_the_hourly_limit_refuses_with_minutes_to_wait(self):
        for _ in range(SIGN_INS_PER_HOUR):
            self.store.record("starts")
        self.now += 600
        refused = await self.finish(self.remote())
        self.assert_final(refused, "sign_in_attempts_exceeded")
        self.assertIsNone(self.backend.calls[-1][1])  # no permit was handed out
        self.assertIn(refused.error["details"]["retry_after_minutes"], (50, 51))
        self.assertEqual(len(self.ledger()["starts"]), SIGN_INS_PER_HOUR)
        self.assertFalse(self.service.host_sign_in_required)
        # A session that is still valid is used as before.
        self.backend.expired = False
        self.assertEqual((await self.finish(self.remote())).status, "succeeded")
        # An hour after the first, the limit has passed.
        self.now = 1000 + 3600 + 1
        self.backend.expired = True
        operation_id = self.remote()
        await self.until_asked()
        self.backend.answer.set_result("signed_in")
        self.assertEqual((await self.finish(operation_id)).status, "succeeded")

    async def test_the_daily_password_limit_refuses_a_login_too(self):
        for _ in range(PASSWORDS_PER_DAY):
            self.store.record("passwords")
        self.now += 7200
        refused = await self.finish(self.remote("login"))
        self.assert_final(refused, "sign_in_attempts_exceeded")
        self.assertIn(refused.error["details"]["retry_after_minutes"], (1320, 1321))

    async def test_holds_and_limits_survive_a_new_service(self):
        await self.sign_in("sign_in_not_approved")
        calls = len(self.backend.calls)
        restarted = self.make_service()
        refused = await self.finish(self.remote(service=restarted), restarted)
        self.assert_final(refused, "sign_in_not_approved")
        self.assertEqual(len(self.backend.calls), calls)
        self.store.forget_ledger()
        for _ in range(SIGN_INS_PER_HOUR):
            self.store.record("starts")
        restarted = self.make_service()
        refused = await self.finish(self.remote(service=restarted), restarted)
        self.assert_final(refused, "sign_in_attempts_exceeded")


class SetupAndReasonTests(ChatSignInCase):
    def link_provider(self):
        links = []

        def link():
            links.append(len(links) + 1)
            return f"https://sagasu.test/sign-in?once={links[-1]}"

        errors.set_sign_in_url("https://sagasu.test/sign-in", link)
        self.addCleanup(errors.set_sign_in_url, None)
        return links

    async def test_with_nothing_saved_the_user_gets_a_one_time_link(self):
        links = self.link_provider()
        self.store.erase()
        failed = await self.finish(self.remote())
        self.assert_final(failed, "sign_in_setup_needed")
        self.assertEqual(failed.error["sign_in_url"], "https://sagasu.test/sign-in?once=1")
        calls = len(self.backend.calls)
        refused = await self.finish(self.remote())
        self.assert_final(refused, "sign_in_setup_needed")
        self.assertEqual(refused.error["sign_in_url"], "https://sagasu.test/sign-in?once=2")
        self.assertEqual(len(self.backend.calls), calls)
        self.assertEqual(links, [1, 2])
        # Saved on the page, then asked for from chat: the pause is lifted.
        self.store.save(EMAIL, PASSWORD)
        login = self.remote("login")
        await self.until_asked()
        self.backend.answer.set_result("signed_in")
        self.assertEqual((await self.finish(login)).status, "succeeded")
        self.assertFalse(self.service.host_sign_in_required)

    async def test_a_failing_link_provider_falls_back_to_the_page(self):
        errors.set_sign_in_url("https://sagasu.test/sign-in", Mock(side_effect=OSError))
        self.addCleanup(errors.set_sign_in_url, None)
        self.assertEqual(errors.sign_in_url(), "https://sagasu.test/sign-in")

    async def test_a_session_check_never_signs_in(self):
        """The real Backend passes interactive=False for session checks."""
        clients = []

        class PagelessClient(FBSClient):
            async def __aenter__(self):
                self.page = Mock(goto=AsyncMock(), wait_for_function=AsyncMock())
                self.page.locator.return_value.count = AsyncMock(return_value=0)
                self._await_silent_renewal = AsyncMock(
                    side_effect=AuthenticationRequired(
                        "x", reason="sign_in_required_on_host"
                    )
                )

                async def ask(view, **options):
                    await self.auth_notify("Enter number 42 in Authenticator.")
                    raise AuthenticationRequired("x", reason="sign_in_not_approved")

                self._interactive_authentication = AsyncMock(side_effect=ask)
                clients.append(self)
                return self

            async def __aexit__(self, *exc):
                return None

        self.service.backend = Backend()
        with patch("sagasu.mcp_service.FBSClient", PagelessClient):
            check = await self.finish(self.remote("session"))
            self.assert_final(check, "sign_in_needed")
            self.assertFalse(clients[-1].may_sign_in)
            self.assertIsInstance(clients[-1].sign_in_permit, Permit)
            self.assertFalse(clients[-1].sign_in_permit.used)
            clients[-1]._interactive_authentication.assert_not_awaited()
            self.assertEqual(self.ledger().get("starts", []), [])
            self.assertIsNone(self.store.held())
            self.assertFalse(self.service.host_sign_in_required)
            # A login through the same client does sign in, once.
            login = await self.finish(self.remote("login"))
            self.assert_final(login, "sign_in_not_approved")
            clients[-1]._interactive_authentication.assert_awaited_once()
            self.assertEqual(len(self.ledger()["starts"]), 1)
            self.assertEqual(self.store.held()["reason"], "sign_in_not_approved")

    async def test_every_new_reason_is_final_and_follows_its_table_entry(self):
        self.assertLessEqual(set(SAVED_SIGN_IN_REASONS), set(NEW_REASONS))
        seen = {}
        for outcome in sorted(SAVED_SIGN_IN_REASONS):
            self.store.forget_ledger()
            self.store.save(EMAIL, PASSWORD)  # a rejected one was forgotten
            self.backend.expired = True
            seen[outcome] = await self.sign_in(outcome)
        self.store.forget_ledger()
        self.store.save(EMAIL, PASSWORD)
        seen["sign_in_needed"] = await self.finish(self.remote("session"))
        for _ in range(SIGN_INS_PER_HOUR):
            self.store.record("starts")
        seen["sign_in_attempts_exceeded"] = await self.finish(self.remote())
        self.store.forget_ledger()
        self.store.erase()
        seen["sign_in_setup_needed"] = await self.finish(self.remote())
        self.assertEqual(set(seen), set(NEW_REASONS))
        for reason, job in seen.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, PERSON_NEEDED)
                self.assert_final(job, reason)
                self.assertEqual(job.error["message"], REASONS[reason][0])


@unittest.skipIf(Client is None, "Install Sagasu for MCP runtime tests")
class ChatSignInToolTests(ChatSignInCase):
    def server(self, **options):
        return Client(create_server(self.service, **options))

    async def tools(self, client):
        return {tool.name for tool in (await client.list_tools()).tools}

    async def status(self, client):
        return (await client.call_tool("server_status", {})).structured_content

    async def test_saved_sign_in_offers_login_and_signs_in_from_chat(self):
        async with self.server(remote=RemoteAccess()) as client:
            plain = await self.tools(client)
        async with self.server(remote=RemoteAccess(saved_sign_in=True)) as client:
            tools = await self.tools(client)
            self.assertEqual(tools, plain | {"login"})
            self.assertNotIn("authentication_status", tools)
            self.assertIn("SMU SIGN-IN HAPPENS IN THIS CHAT", client.instructions)
            started = time.monotonic()
            view = (await client.call_tool("login", {})).structured_content
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual(view["status"], "awaiting_mfa")
            self.assertEqual(view["sign_in"]["number"], NUMBER)
            self.assertTrue(view["sign_in"]["started_by_this_operation"])
            self.assertIsNone(view["poll_after_ms"])
            self.assertEqual(view["next_tool"], "wait_operation")
            operation_id = view["operation_id"]
            polled = await client.call_tool("operation_status", {"operation_id": operation_id})
            self.assertEqual(polled.structured_content["sign_in"]["number"], NUMBER)
            status = await self.status(client)
            self.assertEqual(
                status["remote_access"]["sign_in"],
                {"saved": True, "paused_until_user_acts": None},
            )
            [operation] = status["operations"]
            self.assertNotIn(NUMBER, json.dumps(operation).replace(operation_id, ""))
            self.assertNotIn("Enter number", json.dumps(status))
            self.backend.answer.set_result("signed_in")
            done = await client.call_tool(
                "wait_operation", {"operation_id": operation_id, "timeout": 5}
            )
            self.assertEqual(done.structured_content["status"], "succeeded")
            self.assertNotIn("sign_in", done.structured_content)

    # Regression (fixed): server_status lists a remote job awaiting approval with job.public(),
    # whose next_tool is authentication_status: a tool hidden from remote
    # callers. status_view points them at wait_operation instead.
    async def test_server_status_never_points_a_remote_agent_at_a_hidden_tool(self):
        operation_id = self.remote()
        await self.until_asked()
        async with self.server(remote=RemoteAccess(saved_sign_in=True)) as client:
            hidden = set((await self.status(client))["remote_access"]["unavailable_tools"])
            [operation] = (await self.status(client))["operations"]
        self.assertEqual(operation["operation_id"], operation_id)
        self.assertEqual(operation["status"], "awaiting_mfa")
        self.assertNotIn(operation["next_tool"], hidden)

    async def test_server_status_reports_a_pause_until_the_user_acts(self):
        async with self.server(remote=RemoteAccess(saved_sign_in=True)) as client:
            await self.sign_in("sign_in_not_approved")
            self.assertEqual(
                (await self.status(client))["remote_access"]["sign_in"],
                {"saved": True, "paused_until_user_acts": "sign_in_not_approved"},
            )
            self.now += 60
            await self.sign_in("saved_password_rejected", kind="login")
            self.assertEqual(
                (await self.status(client))["remote_access"]["sign_in"],
                {"saved": False, "paused_until_user_acts": "saved_password_rejected"},
            )

    async def test_without_saved_sign_in_the_remote_surface_is_unchanged(self):
        async with self.server(remote=RemoteAccess()) as client:
            tools = await self.tools(client)
            self.assertNotIn("login", tools)
            self.assertNotIn("authentication_status", tools)
            self.assertNotIn("SMU SIGN-IN HAPPENS IN THIS CHAT", client.instructions)
            self.assertIn("You cannot sign in or approve MFA from here", client.instructions)
            status = await self.status(client)
            self.assertEqual(status["remote_access"]["sign_in"], "not_known_to_be_required")
            self.assertIn("login", status["remote_access"]["unavailable_tools"])
            try:
                rejected = (await client.call_tool("login", {})).is_error
            except Exception:
                rejected = True
            self.assertTrue(rejected)
        self.assertEqual(self.backend.calls, [])


if __name__ == "__main__":
    unittest.main()
