"""Credential submission and remote MFA behavior, without external requests."""

import os
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

from bot.auth import owner_auth_notifier
from bot.fbs_client import AuthenticationRequired, FBSClient


class AuthTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "SMU_LOGIN_EMAIL": "test@smu.edu.sg",
                "SMU_LOGIN_PASSWORD": "private $test#secret",
                "TELEGRAM_OWNER_ID": "42",
                "SAGASU_AUTH_TIMEOUT_MS": "1000",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def flow(
        self,
        states,
        *,
        url="https://login.microsoftonline.com/tenant/login",
        headless=True,
    ):
        client = FBSClient(
            headless=headless, progress=AsyncMock(), auth_notify=AsyncMock()
        )
        client.page = Mock()
        client.page.url = url
        client._capture_auth_state = AsyncMock()
        self.index = 0
        client.fields = {}

        def locator(selector, **kwargs):
            if selector not in client.fields:
                item = Mock()
                item.first = item
                item.fill = AsyncMock()
                item.click = AsyncMock()
                item.is_visible = AsyncMock(return_value=True)
                item.count = AsyncMock(return_value=1)
                if selector == "iframe#frameBottom":
                    item.count = AsyncMock(
                        side_effect=lambda: int(self.index >= len(states))
                    )
                client.fields[selector] = item
            return client.fields[selector]

        client.page.locator.side_effect = locator
        client.page.get_by_text.side_effect = locator
        client.page.keyboard = AsyncMock()
        client.page.goto = AsyncMock()
        client.page.wait_for_timeout = AsyncMock()

        async def classify():
            state = states[self.index]
            self.index += 1
            return state

        client._classify_auth_state = classify
        return client

    async def test_fills_email_and_password_once_and_completes_after_mfa(self):
        client = self.flow(
            [
                ("email", "email"),
                ("password", "password"),
                ("password", "password"),
                ("authenticator", "Enter number 42."),
                ("authenticator", "Enter number 42."),
            ]
        )
        await client._complete_interactive_authentication()
        client.fields[
            'input[name="loginfmt"]:visible, input[type="email"]:visible'
        ].fill.assert_awaited_once_with("test@smu.edu.sg")
        client.fields['input[type="password"]:visible'].fill.assert_awaited_once_with(
            "private $test#secret"
        )
        client.auth_notify.assert_awaited_once_with("Enter number 42.")
        self.assertNotIn(
            "Enter number 42.", [c.args[0] for c in client.progress.await_args_list]
        )

    async def test_number_appearing_late_or_changing_sends_updated_notification(self):
        client = self.flow(
            [
                ("authenticator", "Waiting for number"),
                ("authenticator", "Enter number 24."),
                ("authenticator", "Enter number 24."),
                ("authenticator", "Enter number 31."),
            ]
        )
        await client._complete_interactive_authentication()
        self.assertEqual(
            [c.args[0] for c in client.auth_notify.await_args_list],
            ["Waiting for number", "Enter number 24.", "Enter number 31."],
        )

    async def test_rejected_password_stops_without_retry(self):
        client = self.flow(
            [("password", "password"), ("failed", "Microsoft rejected sign-in")]
        )
        with self.assertRaisesRegex(AuthenticationRequired, "rejected"):
            await client._complete_interactive_authentication()
        client.fields['input[type="password"]:visible'].fill.assert_awaited_once()
        client.auth_notify.assert_awaited_once_with("Microsoft rejected sign-in")

    async def test_credentials_never_submitted_to_untrusted_origin(self):
        client = self.flow(
            [("password", "password")],
            url="https://login.microsoftonline.com.evil.test/login",
        )
        with self.assertRaisesRegex(AuthenticationRequired, "unsupported origin"):
            await client._complete_interactive_authentication()
        self.assertEqual(list(client.fields), ["iframe#frameBottom"])

    def test_trusted_login_urls_reject_non_https_userinfo_and_nonstandard_ports(self):
        for url in [
            "http://login.microsoftonline.com/login",
            "https://user@login.microsoftonline.com/login",
            "https://login.microsoftonline.com:8443/login",
            "https://login.microsoftonline.com:bad/login",
        ]:
            self.assertFalse(FBSClient._trusted_login_url(url))
        self.assertTrue(
            FBSClient._trusted_login_url("https://login.microsoft.com/login")
        )

    async def test_passkey_uses_microsoft_cancel_url_and_selects_password(self):
        cancel = "https://login.microsoftonline.com/tenant/resume?ctx=test"
        client = self.flow(
            [("webauthn", "passkey"), ("password_choice", "password")],
            url="https://login.microsoft.com/tenant/bridge/fido?cancelUrl="
            + quote(cancel, safe=""),
        )
        await client._complete_interactive_authentication()
        client.page.goto.assert_awaited_once_with(
            cancel, wait_until="domcontentloaded", timeout=30000
        )
        client.fields["Use my password"].click.assert_awaited_once()

    async def test_method_picker_selects_authenticator_without_prior_passkey(self):
        client = self.flow([("method_picker", "choose method")])
        option = Mock(is_visible=AsyncMock(return_value=True), click=AsyncMock())
        picker = Mock(all=AsyncMock(return_value=[option]))
        client.page.get_by_text.side_effect = None
        client.page.get_by_text.return_value = picker
        await client._complete_interactive_authentication()
        option.click.assert_awaited_once()

    async def test_headless_missing_password_fails_with_actionable_message(self):
        os.environ.pop("SMU_LOGIN_PASSWORD")
        client = self.flow([("password", "password")])
        with self.assertRaisesRegex(AuthenticationRequired, "SMU_LOGIN_PASSWORD"):
            await client._complete_interactive_authentication()

    async def test_unsupported_step_notifies_owner_and_stops_headless(self):
        client = self.flow([("manual", "An interactive browser is required")])
        with self.assertRaisesRegex(AuthenticationRequired, "interactive browser"):
            await client._complete_interactive_authentication()
        client.auth_notify.assert_awaited_once()

    async def test_notification_goes_to_owner_private_chat(self):
        bot = Mock(send_message=AsyncMock())
        await owner_auth_notifier(bot)("Enter number 42.")
        bot.send_message.assert_awaited_once_with(
            chat_id=42, text="🔐 Enter number 42.", parse_mode=None
        )

    async def test_notification_failure_is_sanitized_and_stops_login(self):
        bot = Mock(send_message=AsyncMock(side_effect=RuntimeError("private token")))
        with self.assertRaises(AuthenticationRequired) as caught:
            await owner_auth_notifier(bot)("MFA")
        self.assertNotIn("private token", str(caught.exception))
        self.assertIn("/start", str(caught.exception))

    async def test_unconfigured_owner_never_sends_mfa_to_requesting_chat(self):
        os.environ.pop("TELEGRAM_OWNER_ID")
        bot = Mock(send_message=AsyncMock())
        with self.assertRaises(AuthenticationRequired):
            await owner_auth_notifier(bot)("MFA")
        bot.send_message.assert_not_awaited()

    async def test_classification_reads_visible_number_and_distinguishes_method_picker(
        self,
    ):
        client = FBSClient()
        client.page = Mock(url="https://login.microsoftonline.com/tenant/login")
        body = Mock(
            inner_text=AsyncMock(
                return_value="Approve sign in request\nMicrosoft Authenticator\n56"
            )
        )
        empty = Mock(count=AsyncMock(return_value=0))
        empty.first = empty
        client.page.locator.side_effect = lambda selector: (
            body if selector == "body" else empty
        )
        state, prompt = await client._classify_auth_state()
        self.assertEqual(state, "authenticator")
        self.assertIn("number 56", prompt)
        body.inner_text.return_value = "Verify your identity\nFace, fingerprint, PIN or security key\nApprove a request on my Microsoft Authenticator app"
        self.assertEqual((await client._classify_auth_state())[0], "method_picker")

    async def test_session_doctor_does_not_submit_credentials_even_when_configured(
        self,
    ):
        client = FBSClient(interactive=False, headless=True)
        client.page = Mock(goto=AsyncMock(), wait_for_function=AsyncMock())
        client.page.locator.return_value.count = AsyncMock(return_value=0)
        client._complete_interactive_authentication = AsyncMock()
        with self.assertRaises(AuthenticationRequired):
            await client.ensure_authenticated()
        client._complete_interactive_authentication.assert_not_awaited()
