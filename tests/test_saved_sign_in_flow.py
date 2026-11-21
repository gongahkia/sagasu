"""The browser client signing in by itself with a person's saved SMU sign-in.

Microsoft's pages are mocked (the classifier is replaced, as in test_auth.py);
the "Stay signed in?" buttons run in a real headless Chromium on a
set_content page. Never contacts FBS, Microsoft or any outside service.
"""

import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import Error as PlaywrightError

from sagasu import fbs_client
from sagasu.fbs_client import (
    FBS_URL,
    FINISHING,
    LOGIN_EMAIL_SELECTOR,
    LOGIN_PASSWORD_SELECTOR,
    MICROSOFT_LOGIN,
    SIGNING_IN,
    AuthenticationRequired,
    FBSClient,
)
from sagasu.saved_sign_in import SAVED_CREDENTIALS, Credentials, Permit
from tests.test_remote import remote_origin

EMAIL = "fixture.person@smu.edu.sg"
PASSWORD = "Fixture pass-word #1"
# Set in the environment to prove the saved values, not these, are typed.
ENV_EMAIL = "someone.else@smu.edu.sg"
ENV_PASSWORD = "environment-password-never-typed"
LOGIN_URL = "https://login.microsoftonline.com/tenant/oauth2/authorize"
SUBMIT = 'input[type="submit"]:visible, button[type="submit"]:visible'
YES, NO = "#idSIButton9:visible", "#idBtn_Back:visible"
COMPLETE = "Microsoft authentication complete; continuing FBS operation"

# What the real classifier returns for these pages with nothing in the env.
EMAIL_PAGE = ("email", "enter your SMU email in Chrome")
PASSWORD_PAGE = ("password", "enter your Microsoft password in Chrome")
LOADING = ("sign_in", "waiting for Microsoft sign-in")
WAITING = (
    "Open Microsoft Authenticator and approve the Sagasu SMU sign-in. The matching "
    "number will follow when Microsoft displays it. Only approve if you initiated "
    "this request."
)
NUMBER = (
    "Open Microsoft Authenticator and approve the Sagasu SMU sign-in. Enter number "
    "42. Only approve if you initiated this request."
)
FAILED = (
    "failed",
    "Microsoft sign-in failed or was denied/blocked. Check your credentials or SMU "
    "sign-in policy before retrying.",
)
MANUAL = (
    "manual",
    "Microsoft requires an interactive verification step beyond Authenticator push "
    "approval. Complete it in an interactive browser; do not send passwords or "
    "verification codes in agent chat.",
)
PROVIDER_ERROR = ("provider_error", "Microsoft returned a sign-in error page")
STAY = ("stay_signed_in", "keeping the Microsoft session signed in")
FBS = ("fbs", "")  # the FBS frame has loaded: signed in


class SavedSignInCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(
            os.environ,
            {"SMU_LOGIN_EMAIL": ENV_EMAIL, "SMU_LOGIN_PASSWORD": ENV_PASSWORD},
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)
        # A bug must not hang the suite for the real three minutes.
        deadline = patch.object(fbs_client, "SAVED_SIGN_IN_SECONDS", 10)
        deadline.start()
        self.addCleanup(deadline.stop)
        self.log = []
        self.body = None  # what Microsoft's page says, read on a failure
        self.permit = Permit(
            Credentials(EMAIL, PASSWORD),
            on_start=lambda: self.log.append("start"),
            on_password=lambda: self.log.append("counted"),
            on_end=lambda permit, outcome: self.log.append(("end", outcome)),
        )

    def make_client(self, *, notify=True, **options):
        token = SAVED_CREDENTIALS.set(self.permit)
        try:
            return FBSClient(
                headless=True,
                progress=AsyncMock(
                    side_effect=lambda m: self.log.append(("progress", m))
                ),
                auth_notify=(
                    AsyncMock(side_effect=lambda m: self.log.append(("notify", m)))
                    if notify
                    else None
                ),
                **options,
            )
        finally:
            SAVED_CREDENTIALS.reset(token)

    def flow(self, states, *, url=LOGIN_URL, notify=True):
        """A client on a mocked Microsoft page that reads as each state in turn.

        The last state repeats until the sign-in's deadline; FBS ends it.
        """
        client = self.make_client(notify=notify)
        client.page = Mock()
        client.page.url = url
        client._capture_auth_state = AsyncMock()
        client.fields = {}
        self.index = 0

        def current():
            return states[min(self.index, len(states) - 1)]

        def body_text(**_):
            if self.body is None:
                raise PlaywrightError("page changed while it was read")
            return self.body

        def locator(selector, **_):
            if selector not in client.fields:
                item = Mock()
                item.first = item
                item.fill = AsyncMock(
                    side_effect=lambda value, s=selector: self.log.append(
                        ("fill", s, value)
                    )
                )
                item.click = AsyncMock(
                    side_effect=lambda *a, s=selector, **k: self.log.append(
                        ("click", s)
                    )
                )
                item.press = AsyncMock()
                item.press_sequentially = AsyncMock()
                item.wait_for = AsyncMock()
                item.input_value = AsyncMock(
                    return_value=PASSWORD if 'type="password"' in selector else EMAIL
                )
                item.is_visible = AsyncMock(return_value=True)
                item.count = AsyncMock(return_value=1)
                item.inner_text = AsyncMock(side_effect=body_text)
                if selector == "iframe#frameBottom":
                    item.count = AsyncMock(side_effect=lambda: int(current() == FBS))
                client.fields[selector] = item
            return client.fields[selector]

        async def pause(_):
            await asyncio.sleep(0.002)

        client.page.locator.side_effect = locator
        client.page.get_by_text.side_effect = locator
        client.page.evaluate = AsyncMock(return_value=None)
        client.page.keyboard = AsyncMock()
        client.page.goto = AsyncMock(
            side_effect=lambda url, **_: self.log.append(("goto", url))
        )
        client.page.wait_for_timeout = AsyncMock(side_effect=pause)

        async def classify():
            state = current()
            self.index += 1
            return state

        client._classify_auth_state = classify
        return client

    async def refused(self, client):
        with self.assertRaises(AuthenticationRequired) as caught:
            await client._complete_interactive_authentication()
        return caught.exception.reason

    def entries(self, kind):
        return [
            entry[1:]
            for entry in self.log
            if isinstance(entry, tuple) and entry[0] == kind
        ]

    def typed(self, selector):
        return [value for s, value in self.entries("fill") if s == selector]

    def assert_progress_is_safe(self):
        for (message,) in self.entries("progress"):
            for word in ("password", "chrome", "credential", EMAIL, "42"):
                self.assertNotIn(word, message.casefold(), message)


class UnattendedSignInTests(SavedSignInCase):
    async def test_signs_in_with_the_saved_values_and_shows_only_the_number(self):
        client = self.flow(
            [
                EMAIL_PAGE,
                PASSWORD_PAGE,
                PASSWORD_PAGE,
                LOADING,
                ("authenticator", WAITING),
                ("authenticator", WAITING),
                ("authenticator", NUMBER),
                ("authenticator", NUMBER),
                STAY,
                FBS,
            ]
        )
        await client._complete_interactive_authentication()
        self.assertEqual(
            self.log,
            [
                "start",
                ("progress", SIGNING_IN),
                ("fill", LOGIN_EMAIL_SELECTOR, EMAIL),
                ("click", SUBMIT),
                # Counted before it is typed (lockout budget).
                "counted",
                ("fill", LOGIN_PASSWORD_SELECTOR, PASSWORD),
                ("click", SUBMIT),
                # First without the number, again when it appears.
                ("notify", WAITING),
                ("notify", NUMBER),
                ("progress", FINISHING),
                ("click", YES),
                ("progress", COMPLETE),
                ("end", "signed_in"),
            ],
        )
        self.assertEqual(
            (self.permit.outcome, self.permit.signed_in, self.permit.password_typed),
            ("signed_in", True, True),
        )
        self.assertFalse(client._unattended)
        self.assert_progress_is_safe()

    async def test_stay_signed_in_answers_no_when_set_to_false(self):
        for value, button, other in (
            ("false", NO, YES),
            ("no", NO, YES),
            ("true", YES, NO),
        ):
            with self.subTest(value=value):
                self.log.clear()
                self.permit = Permit(Credentials(EMAIL, PASSWORD))
                client = self.flow([STAY, FBS])
                with patch.dict(os.environ, {"SAGASU_SAVED_SIGN_IN_STAY": value}):
                    await client._complete_interactive_authentication()
                self.assertIn(("click", button), self.log)
                self.assertNotIn(("click", other), self.log)
                self.assertTrue(self.permit.signed_in)

    async def test_a_refused_password_is_never_typed_again(self):
        client = self.flow([EMAIL_PAGE, PASSWORD_PAGE, FAILED, PASSWORD_PAGE, FBS])
        self.body = (
            "Your account or password is incorrect. If you don't remember your "
            "password, reset it now."
        )
        self.assertEqual(await self.refused(client), "saved_password_rejected")
        self.assertEqual(self.typed(LOGIN_PASSWORD_SELECTOR), [PASSWORD])
        self.assertEqual(self.log.count("counted"), 1)
        self.assertEqual(self.index, 3)  # stopped on the refusal itself
        self.assertEqual(self.entries("notify"), [])
        self.assertEqual(self.permit.outcome, "saved_password_rejected")
        self.assertFalse(self.permit.signed_in)
        self.assert_progress_is_safe()

    async def test_a_denied_or_unanswered_request_is_not_approved(self):
        for body in (
            "You denied the sign-in request.",
            "We didn't hear from you. We sent an identity verification request.",
            "We didn’t hear from you",
            "Request was denied",
        ):
            with self.subTest(body=body):
                self.log.clear()
                self.permit = Permit(Credentials(EMAIL, PASSWORD))
                self.body = body
                client = self.flow(
                    [EMAIL_PAGE, PASSWORD_PAGE, ("authenticator", NUMBER), FAILED, FBS]
                )
                self.assertEqual(await self.refused(client), "sign_in_not_approved")
                self.assertEqual(self.entries("notify"), [(NUMBER,)])
                self.assertEqual(self.permit.outcome, "sign_in_not_approved")
                self.assertEqual(self.typed(LOGIN_PASSWORD_SELECTOR), [PASSWORD])

    async def test_a_lockout_is_blocked(self):
        self.body = "You've tried to sign in too many times with an incorrect account or password."
        client = self.flow([EMAIL_PAGE, PASSWORD_PAGE, FAILED])
        self.assertEqual(await self.refused(client), "sign_in_blocked")
        self.assertEqual(self.permit.outcome, "sign_in_blocked")

    async def test_a_manual_verification_step_needs_the_page(self):
        for states in ([EMAIL_PAGE, PASSWORD_PAGE, MANUAL], [EMAIL_PAGE, MANUAL]):
            with self.subTest(states=[state for state, _ in states]):
                self.log.clear()
                self.permit = Permit(Credentials(EMAIL, PASSWORD))
                client = self.flow(states + [FBS])
                self.assertEqual(await self.refused(client), "sign_in_needs_page")
                self.assertEqual(self.permit.outcome, "sign_in_needs_page")
                self.assertEqual(self.entries("notify"), [])
                self.assertLessEqual(len(self.typed(LOGIN_PASSWORD_SELECTOR)), 1)
                self.assert_progress_is_safe()

    async def test_no_approval_by_the_deadline_is_not_approved(self):
        client = self.flow([EMAIL_PAGE, PASSWORD_PAGE, ("authenticator", NUMBER)])
        with patch.object(fbs_client, "SAVED_SIGN_IN_SECONDS", 0.5):
            self.assertEqual(await self.refused(client), "sign_in_not_approved")
        self.assertGreater(self.index, 4)  # it waited on the number page
        self.assertEqual(self.entries("notify"), [(NUMBER,)])
        self.assertEqual(self.permit.outcome, "sign_in_not_approved")

    async def test_an_unclear_end_after_the_password_needs_the_page(self):
        cases = {
            "provider error": [EMAIL_PAGE, PASSWORD_PAGE, PROVIDER_ERROR],
            "unreadable refusal": [EMAIL_PAGE, PASSWORD_PAGE, FAILED],
            "back to the password": [EMAIL_PAGE, PASSWORD_PAGE, LOADING, PASSWORD_PAGE],
            "still loading at the deadline": [EMAIL_PAGE, PASSWORD_PAGE, LOADING],
        }
        for name, states in cases.items():
            with self.subTest(name):
                self.log.clear()
                self.permit = Permit(Credentials(EMAIL, PASSWORD))
                client = self.flow(states)
                with patch.object(fbs_client, "SAVED_SIGN_IN_SECONDS", 0.5):
                    self.assertEqual(await self.refused(client), "sign_in_needs_page")
                self.assertEqual(self.typed(LOGIN_PASSWORD_SELECTOR), [PASSWORD])
                self.assertEqual(self.permit.outcome, "sign_in_needs_page")
                self.assertEqual(self.entries("notify"), [])

    async def test_a_crash_while_typing_the_password_still_counts_it(self):
        client = self.flow([EMAIL_PAGE, PASSWORD_PAGE, FBS])
        client.page.locator(LOGIN_PASSWORD_SELECTOR).fill.side_effect = PlaywrightError(
            "Target closed"
        )
        self.assertEqual(await self.refused(client), "sign_in_needs_page")
        self.assertTrue(self.permit.password_typed)
        self.assertEqual(self.log.count("counted"), 1)

    async def test_a_failure_before_the_password_is_unfinished(self):
        cases = {
            "provider error": ([EMAIL_PAGE, PROVIDER_ERROR], LOGIN_URL),
            "lost context twice": ([("invalid_context", "lost")], LOGIN_URL),
            "stalled on the email page": ([EMAIL_PAGE], LOGIN_URL),
            "unsupported origin": ([EMAIL_PAGE], "https://login.example.invalid/"),
        }
        for name, (states, url) in cases.items():
            with self.subTest(name):
                self.log.clear()
                self.permit = Permit(Credentials(EMAIL, PASSWORD))
                client = self.flow(states, url=url)
                with patch.object(fbs_client, "SAVED_SIGN_IN_SECONDS", 0.5):
                    self.assertEqual(await self.refused(client), "sign_in_unfinished")
                self.assertEqual(self.typed(LOGIN_PASSWORD_SELECTOR), [])
                self.assertFalse(self.permit.password_typed)
                self.assertEqual(self.permit.outcome, "sign_in_unfinished")

    async def test_without_a_way_to_show_the_number_nothing_is_typed(self):
        client = self.flow([EMAIL_PAGE, PASSWORD_PAGE, FBS], notify=False)
        self.assertEqual(await self.refused(client), "sign_in_unfinished")
        self.assertEqual(self.entries("fill"), [])
        self.assertEqual(self.permit.outcome, "sign_in_unfinished")

    async def test_a_permit_signs_in_once_per_job(self):
        client = self.flow(
            [EMAIL_PAGE, PASSWORD_PAGE, ("authenticator", NUMBER), STAY, FBS]
        )
        await client._complete_interactive_authentication()
        before = list(self.log)
        self.index = 0  # Microsoft asks again in the same job
        self.assertEqual(await self.refused(client), "sign_in_unfinished")
        self.assertEqual(self.log, before)  # nothing typed, shown or counted
        self.assertEqual(self.permit.outcome, "signed_in")

        self.log.clear()
        self.permit = Permit(
            Credentials(EMAIL, PASSWORD), on_start=lambda: self.log.append("start")
        )
        self.body = "You denied the sign-in request."
        client = self.flow(
            [EMAIL_PAGE, PASSWORD_PAGE, ("authenticator", NUMBER), FAILED]
        )
        self.assertEqual(await self.refused(client), "sign_in_not_approved")
        self.index = 0
        self.assertEqual(await self.refused(client), "sign_in_unfinished")
        self.assertEqual(self.log.count("start"), 1)
        self.assertEqual(len(self.entries("notify")), 1)
        self.assertEqual(self.typed(LOGIN_PASSWORD_SELECTOR), [PASSWORD])
        self.assertEqual(self.permit.outcome, "sign_in_not_approved")

    # Regression (fixed): in an unattended sign-in the Authenticator prompt is sent through
    # last_notified and never added to `notifications`, so the invalid_context
    # branch (fbs_client.py, "before any ... Authenticator request") still sees
    # no request and restarts from FBS. With no password typed (an account that
    # goes straight to the phone approval) that starts a second Authenticator
    # request in the same job, which the permit is meant to prevent.
    async def test_lost_context_after_the_number_does_not_restart(self):
        client = self.flow(
            [
                EMAIL_PAGE,
                ("authenticator", NUMBER),
                ("invalid_context", "lost"),
                EMAIL_PAGE,
            ]
        )
        with patch.object(fbs_client, "SAVED_SIGN_IN_SECONDS", 0.5):
            await self.refused(client)
        self.assertEqual(self.entries("goto"), [])
        self.assertEqual(self.typed(LOGIN_EMAIL_SELECTOR), [EMAIL])

    # Regression (fixed): _submit_login_field reports "Microsoft replaced the password field
    # before submission; checking it again" through progress, which becomes the
    # agent-visible stage, although an unattended sign-in is meant to show only
    # fixed words that never mention a password (SIGNING_IN / FINISHING).
    async def test_a_replaced_password_field_is_not_reported_as_a_password(self):
        client = self.flow([EMAIL_PAGE, PASSWORD_PAGE, STAY, FBS])
        field = client.page.locator(LOGIN_PASSWORD_SELECTOR)
        field.input_value = AsyncMock(side_effect=["", PASSWORD])
        await client._complete_interactive_authentication()
        self.assertTrue(self.permit.signed_in)
        self.assertEqual(self.log.count("counted"), 1)
        self.assert_progress_is_safe()


class ReviewRegressionTests(SavedSignInCase):
    """Cases found by the independent reviews of this change."""

    async def remote_client(self, interactive=True):
        with remote_origin():
            client = self.make_client(interactive=interactive)
        client.page = Mock(
            url=LOGIN_URL, goto=AsyncMock(), wait_for_function=AsyncMock()
        )
        client.page.locator.return_value.count = AsyncMock(return_value=0)
        client.context = None
        client._await_silent_renewal = AsyncMock(return_value=False)  # timed out
        client._sign_in_with_saved_credentials = AsyncMock()
        client._wait_for_frame = AsyncMock(
            return_value=Mock(wait_for_selector=AsyncMock())
        )
        client._save_session = AsyncMock()
        return client

    async def test_a_renewal_that_times_out_on_microsoft_signs_in(self):
        # Live finding: the page never settles while prompts are blocked.
        client = await self.remote_client()
        await client.ensure_authenticated()
        client._sign_in_with_saved_credentials.assert_awaited_once_with(fresh=True)

    async def test_a_check_that_times_out_on_microsoft_never_signs_in(self):
        client = await self.remote_client(interactive=False)
        with self.assertRaises(AuthenticationRequired) as caught:
            await client.ensure_authenticated()
        self.assertEqual(caught.exception.reason, "sign_in_needed")
        client._sign_in_with_saved_credentials.assert_not_awaited()

    async def test_resuming_a_live_session_checks_no_password(self):
        # Mid-redirect on Microsoft, then FBS: nothing was typed, so nothing
        # was checked, and the copy must not learn its owner from it.
        client = self.flow([LOADING, FBS])
        await client._complete_interactive_authentication()
        self.assertTrue(self.permit.signed_in)
        self.assertFalse(self.permit.password_typed)
        self.assertFalse(self.permit.verified)

    async def cancelled_at(self, states):
        client = self.flow(states)
        task = asyncio.create_task(client._complete_interactive_authentication())
        for _ in range(500):
            if self.index >= len(states):
                break
            await asyncio.sleep(0.002)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        return self.permit.outcome

    async def test_a_cancel_while_the_number_waits_lets_chat_try_again(self):
        outcome = await self.cancelled_at(
            [EMAIL_PAGE, PASSWORD_PAGE, ("authenticator", NUMBER)]
        )
        # Microsoft accepted the password; only the approval was pending.
        self.assertEqual(outcome, "sign_in_not_approved")

    async def test_a_cancel_before_the_phone_step_is_unfinished(self):
        self.assertEqual(
            await self.cancelled_at([EMAIL_PAGE, LOADING]), "sign_in_unfinished"
        )


class RemoteSavedSignInTests(SavedSignInCase):
    def remote_client(self, *, interactive=True, permit=True):
        with remote_origin():
            if permit:
                client = self.make_client(interactive=interactive)
            else:
                client = FBSClient(
                    interactive=interactive, headless=True, auth_notify=AsyncMock()
                )
        client.context = Mock(
            route=AsyncMock(side_effect=lambda *a: self.log.append(("route",) + a)),
            unroute=AsyncMock(side_effect=lambda *a: self.log.append(("unroute",) + a)),
        )
        client.page = Mock(
            url=LOGIN_URL,
            goto=AsyncMock(side_effect=lambda url, **_: self.log.append(("goto", url))),
            wait_for_function=AsyncMock(),
            wait_for_timeout=AsyncMock(),
        )
        client.page.locator.return_value.count = AsyncMock(return_value=0)
        client._interactive_authentication = AsyncMock(
            side_effect=lambda *a, **k: self.log.append(("sign_in", a, k))
        )
        client._save_session = AsyncMock()
        self.frame = Mock(wait_for_selector=AsyncMock())
        client._wait_for_frame = AsyncMock(return_value=self.frame)
        return client

    def assert_nothing_entered(self, client):
        locator = client.page.locator.return_value
        for action in (locator.fill, locator.click, locator.press, locator.type):
            action.assert_not_called()
        client.page.keyboard.press.assert_not_called()
        client.page.get_by_text.assert_not_called()
        client._interactive_authentication.assert_not_awaited()

    async def test_signs_in_afresh_without_the_guard_then_guards_again(self):
        client = self.remote_client()
        self.assertEqual((client.interactive, client.may_sign_in), (False, True))
        client._await_silent_renewal = AsyncMock(
            side_effect=AuthenticationRequired("x", reason="sign_in_required_on_host")
        )
        self.assertEqual(await client.ensure_authenticated(), (client.page, self.frame))
        guard = (MICROSOFT_LOGIN, client._refuse_prompts)
        steps, signed_in = [], None
        for entry in self.log:
            if isinstance(entry, tuple) and entry[0] == "sign_in":
                steps.append(("sign_in",))
                _, args, kwargs = signed_in = entry
            elif isinstance(entry, tuple) and entry[0] in {"route", "unroute", "goto"}:
                steps.append(entry)
        self.assertEqual(
            steps,
            [
                ("route",) + guard,
                ("goto", FBS_URL),
                ("unroute",) + guard,
                ("goto", FBS_URL),  # a clean sign-in, not the blocked page
                ("sign_in",),
                ("route",) + guard,
            ],
        )
        self.assertIsNotNone(signed_in)
        self.assertEqual(args, (None,))
        self.assertEqual(
            kwargs,
            {
                "credentials": self.permit.credentials,
                "unattended": True,
                "timeout_ms": fbs_client.SAVED_SIGN_IN_SECONDS * 1000,
            },
        )
        self.assertTrue(client._guarding)
        self.assertEqual(self.permit.outcome, "signed_in")
        self.assertIn(("progress", SIGNING_IN), self.log)
        client._save_session.assert_awaited_once()

        # Asked again in the same job: the permit is spent, nothing is retried.
        with self.assertRaises(AuthenticationRequired) as caught:
            await client.ensure_authenticated()
        self.assertEqual(caught.exception.reason, "sign_in_required_on_host")
        client._interactive_authentication.assert_awaited_once()
        self.assertEqual(self.log.count("start"), 1)

    async def test_a_check_that_may_not_sign_in_says_sign_in_is_needed(self):
        client = self.remote_client(interactive=False)
        self.assertFalse(client.may_sign_in)
        client._classify_auth_state = AsyncMock(return_value=("password", "prompt"))
        with self.assertRaises(AuthenticationRequired) as caught:
            await client.ensure_authenticated()
        self.assertEqual(caught.exception.reason, "sign_in_needed")
        self.assert_nothing_entered(client)
        self.assertFalse(self.permit.used)
        self.assertIsNone(self.permit.outcome)
        client.context.unroute.assert_not_awaited()
        client.auth_notify.assert_not_awaited()
        self.assertNotIn(("progress", SIGNING_IN), self.log)

    async def test_without_a_permit_the_host_must_sign_in_as_before(self):
        for interactive in (True, False):
            with self.subTest(interactive=interactive):
                client = self.remote_client(interactive=interactive, permit=False)
                self.assertIsNone(client.sign_in_permit)
                client._classify_auth_state = AsyncMock(
                    return_value=("email", "prompt")
                )
                with self.assertRaises(AuthenticationRequired) as caught:
                    await client.ensure_authenticated()
                self.assertEqual(caught.exception.reason, "sign_in_required_on_host")
                self.assert_nothing_entered(client)
                client.context.unroute.assert_not_awaited()


STAY_PAGE = """<h1>Stay signed in?</h1>
<p>Do this to reduce the number of times you are asked to sign in.</p>
<input type="submit" id="idSIButton9" value="Yes" style="display:none"
  onclick="window.hiddenClicked = true">
<input type="button" id="idBtn_Back" value="No" onclick="answer('no')">
<input type="submit" id="idSIButton9" value="Yes" onclick="answer('yes')">
<script>
function answer(choice) {
  window.choice = choice;
  document.body.innerHTML = '<iframe id="frameBottom"></iframe>';
}
</script>"""


class StaySignedInBrowserTests(SavedSignInCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        if not Path(playwright.chromium.executable_path).is_file():
            self.skipTest("Install Playwright Chromium to run browser regression tests")
        browser = await playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        self.browser = browser

    async def test_answers_the_visible_button_as_set(self):
        for value, choice in (("false", "no"), ("true", "yes")):
            with self.subTest(value=value):
                self.log.clear()
                self.permit = Permit(Credentials(EMAIL, PASSWORD))
                client = self.make_client()
                client.page = await self.browser.new_page()
                client._capture_auth_state = AsyncMock()
                await client.page.set_content(STAY_PAGE)
                self.assertEqual(
                    (await client._classify_auth_state())[0], "stay_signed_in"
                )
                with (
                    patch.dict(os.environ, {"SAGASU_SAVED_SIGN_IN_STAY": value}),
                    patch.object(client, "_trusted_login_url", return_value=True),
                ):
                    await client._complete_interactive_authentication()
                self.assertEqual(await client.page.evaluate("window.choice"), choice)
                self.assertIsNone(
                    await client.page.evaluate("window.hiddenClicked ?? null")
                )
                self.assertTrue(self.permit.signed_in)
                self.assertIn(("progress", FINISHING), self.log)
                self.assertEqual(self.entries("notify"), [])
                await client.page.close()


if __name__ == "__main__":
    unittest.main()
