"""Credential submission and remote MFA behavior, without external requests."""

import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote
from playwright.async_api import Error as PlaywrightError

from sagasu.fbs_client import (
    AuthenticationRequired,
    FBSClient,
    LOGIN_EMAIL_SELECTOR,
    LOGIN_PASSWORD_SELECTOR,
)


class AuthBrowserFormTest(unittest.IsolatedAsyncioTestCase):
    """Exercise the real DOM shape seen on Microsoft's fresh sign-in screen."""

    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        if not Path(playwright.chromium.executable_path).is_file():
            self.skipTest("Install Playwright Chromium to run browser regression tests")
        browser = await playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        self.client = FBSClient(headless=True)
        self.client.page = await browser.new_page()

    async def test_email_form_ignores_opacity_zero_password_autofill_input(self):
        await self.client.page.set_content(
            '<h1>Sign in</h1><input name="loginfmt" type="email">'
            '<input type="password" aria-hidden="true" tabindex="-1" '
            'style="position:absolute;opacity:0;width:10px;height:13px">'
        )
        # This is the exact distinction :visible alone missed in the live test.
        self.assertEqual(
            await self.client.page.locator('input[type="password"]:visible').count(), 1
        )
        self.assertEqual((await self.client._classify_auth_state())[0], "email")

    async def test_password_form_ignores_hidden_email_autofill_input(self):
        await self.client.page.set_content(
            '<h1>Enter password</h1><input name="loginfmt" type="email" '
            'aria-hidden="true" style="opacity:0">'
            '<input type="password">'
        )
        self.assertEqual((await self.client._classify_auth_state())[0], "password")
        self.assertEqual(
            await self.client.page.locator(LOGIN_EMAIL_SELECTOR).count(), 0
        )

    async def test_submission_targets_real_password_field_instead_of_autofill_decoy(
        self,
    ):
        await self.client.page.set_content(
            '<form onsubmit="event.preventDefault();window.submitted=true">'
            '<input id="decoy" type="password" aria-hidden="true" style="opacity:0">'
            '<input id="actual" type="password"><button type="submit">Sign in</button>'
            "</form>"
        )
        with patch.object(self.client, "_trusted_login_url", return_value=True):
            await self.client._submit_login_field(
                LOGIN_PASSWORD_SELECTOR, "fixture-password", "password"
            )
        self.assertEqual(await self.client.page.locator("#decoy").input_value(), "")
        self.assertEqual(
            await self.client.page.locator("#actual").input_value(), "fixture-password"
        )
        self.assertTrue(await self.client.page.evaluate("window.submitted"))

    async def test_passkey_resume_supports_current_and_legacy_microsoft_pages(self):
        resume = "https://login.microsoftonline.com/tenant/resume?ctx=fixture"
        for config in (
            {"fEnableDirectFidoCancelToCredPicker": True, "urlResume": resume},
            {
                "acmaInitialConfig": {
                    "serverFeatureFlags": {"fEnableDirectFidoCancelToCredPicker": True}
                },
                "acmaInitialResponse": {"_links": {"resume": {"href": resume}}},
            },
        ):
            await self.client.page.evaluate("config => window.$Config = config", config)
            self.assertEqual(await self.client._passkey_resume_url(), resume)

    async def test_passkey_resume_does_not_follow_untrusted_page_configuration(self):
        await self.client.page.evaluate(
            "window.$Config = {fEnableDirectFidoCancelToCredPicker: true, urlResume: 'https://example.invalid/collect'}"
        )
        self.assertIsNone(await self.client._passkey_resume_url())


class AuthTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "SMU_LOGIN_EMAIL": "test@smu.edu.sg",
                "SMU_LOGIN_PASSWORD": "private $test#secret",
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
                item.press = AsyncMock()
                item.press_sequentially = AsyncMock()
                item.wait_for = AsyncMock()
                item.input_value = AsyncMock(
                    return_value=(
                        "private $test#secret"
                        if 'type="password"' in selector
                        else "test@smu.edu.sg"
                    )
                )
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
        client.page.evaluate = AsyncMock(return_value=None)
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
        client.fields[LOGIN_EMAIL_SELECTOR].fill.assert_awaited_once_with(
            "test@smu.edu.sg"
        )
        client.fields[LOGIN_PASSWORD_SELECTOR].fill.assert_awaited_once_with(
            "private $test#secret"
        )
        client.auth_notify.assert_awaited_once_with("Enter number 42.")
        self.assertNotIn(
            "Enter number 42.", [c.args[0] for c in client.progress.await_args_list]
        )

    async def test_empty_email_after_password_is_recovered_once(self):
        client = self.flow(
            [
                ("email", "email"),
                ("password", "password"),
                ("email", "email"),
                ("authenticator", "Enter number 42."),
            ]
        )
        field = client.page.locator(LOGIN_EMAIL_SELECTOR).first
        field.input_value = AsyncMock(
            side_effect=["test@smu.edu.sg", "", "test@smu.edu.sg"]
        )
        await client._complete_interactive_authentication()
        self.assertEqual(field.fill.await_count, 2)
        client.fields[LOGIN_PASSWORD_SELECTOR].fill.assert_awaited_once()
        client.auth_notify.assert_awaited_once_with("Enter number 42.")

    async def test_unchanged_email_screen_does_not_submit_again(self):
        client = self.flow([("email", "email"), ("email", "email")])
        field = client.page.locator(LOGIN_EMAIL_SELECTOR).first
        field.input_value = AsyncMock(return_value="test@smu.edu.sg")
        await client._complete_interactive_authentication()
        field.fill.assert_awaited_once()

    async def test_repeated_empty_email_stops_and_notifies_user(self):
        client = self.flow([("email", "email")] * 3)
        field = client.page.locator(LOGIN_EMAIL_SELECTOR).first
        field.input_value = AsyncMock(
            side_effect=["test@smu.edu.sg", "", "test@smu.edu.sg", ""]
        )
        with self.assertRaisesRegex(AuthenticationRequired, "restarted sign-in"):
            await client._complete_interactive_authentication()
        self.assertEqual(field.fill.await_count, 2)
        client.auth_notify.assert_awaited_once()

    async def test_return_to_password_after_redirect_stops_without_resubmission(self):
        client = self.flow(
            [
                ("password", "password"),
                ("sign_in", "redirect"),
                ("password", "password"),
            ]
        )
        with self.assertRaisesRegex(AuthenticationRequired, "resubmitting"):
            await client._complete_interactive_authentication()
        client.fields[LOGIN_PASSWORD_SELECTOR].fill.assert_awaited_once()

    async def test_password_choice_after_password_selects_authenticator(self):
        client = self.flow(
            [("password", "password"), ("password_choice", "choose method")]
        )
        client._select_authenticator = AsyncMock(return_value=True)
        await client._complete_interactive_authentication()
        client._select_authenticator.assert_awaited_once()
        self.assertNotIn("Use my password", client.fields)

    async def test_cleared_email_is_retyped_and_verified_before_next(self):
        client = self.flow([])
        field = client.page.locator(LOGIN_EMAIL_SELECTOR).first
        field.input_value = AsyncMock(side_effect=["", "test@smu.edu.sg"])
        await client._submit_login_field(
            LOGIN_EMAIL_SELECTOR,
            "test@smu.edu.sg",
            "email",
        )
        field.press_sequentially.assert_awaited_once_with("test@smu.edu.sg", delay=25)
        client.fields[
            'input[type="submit"]:visible, button[type="submit"]:visible'
        ].click.assert_awaited_once()

    async def test_continuously_cleared_email_is_not_submitted_empty(self):
        client = self.flow([])
        field = client.page.locator(LOGIN_EMAIL_SELECTOR).first
        field.input_value = AsyncMock(return_value="")
        with self.assertRaisesRegex(AuthenticationRequired, "No form was submitted"):
            await client._submit_login_field(
                LOGIN_EMAIL_SELECTOR,
                "test@smu.edu.sg",
                "email",
            )
        client.fields[
            'input[type="submit"]:visible, button[type="submit"]:visible'
        ].click.assert_not_awaited()

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
        client.fields[LOGIN_PASSWORD_SELECTOR].fill.assert_awaited_once()
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
            headless=False,
        )
        await client._complete_interactive_authentication()
        client.page.goto.assert_awaited_once_with(
            cancel, wait_until="domcontentloaded", timeout=30000
        )
        client.fields["Use my password"].click.assert_awaited_once()

    async def test_headless_passkey_uses_trusted_method_picker_continuation(self):
        client = self.flow(
            [("webauthn", "passkey"), ("password_choice", "password")],
            url="https://login.microsoft.com/tenant/fido/get",
        )
        resume = "https://login.microsoftonline.com/tenant/resume?ctx=private"
        client.page.evaluate.return_value = resume
        await client._complete_interactive_authentication()
        client.page.goto.assert_awaited_once_with(
            resume, wait_until="domcontentloaded", timeout=30000
        )
        client.page.keyboard.press.assert_not_awaited()
        client.fields["Use my password"].click.assert_awaited_once()

    async def test_headless_bridge_does_not_reset_login_to_email(self):
        cancel = "https://login.microsoftonline.com/tenant/reprocess?ctx=private"
        client = self.flow(
            [("webauthn", "passkey")],
            url="https://login.microsoft.com/tenant/bridge/fido?cancelUrl="
            + quote(cancel, safe=""),
        )
        await client._complete_interactive_authentication()
        client.page.goto.assert_not_awaited()
        client.page.keyboard.press.assert_awaited_once_with("Escape")

    async def test_aborted_passkey_navigation_accepts_verified_credential_picker(self):
        client = self.flow([
            ("webauthn", "passkey"),
            ("sign_in", "loading"),
            ("password_choice", "password"),
            ("password_choice", "password"),
        ])
        client.page.evaluate.return_value = "https://login.microsoftonline.com/tenant/resume"
        client.page.goto.side_effect = PlaywrightError("Page.goto: net::ERR_ABORTED")
        client.page.wait_for_load_state = AsyncMock()
        await client._complete_interactive_authentication()
        client.fields["Use my password"].click.assert_awaited_once()

    async def test_aborted_passkey_navigation_rejects_unexpected_screen(self):
        client = self.flow([("webauthn", "passkey"), ("email", "email")])
        client.page.evaluate.return_value = "https://login.microsoftonline.com/tenant/resume"
        client.page.goto.side_effect = PlaywrightError("Page.goto: net::ERR_ABORTED")
        client.page.wait_for_load_state = AsyncMock()
        with self.assertRaises(PlaywrightError):
            await client._complete_interactive_authentication()

    async def test_passkey_navigation_preserves_other_browser_errors(self):
        client = self.flow([("webauthn", "passkey")])
        client.page.evaluate.return_value = "https://login.microsoftonline.com/tenant/resume"
        client.page.goto.side_effect = PlaywrightError("Page.goto: net::ERR_CONNECTION_RESET")
        with self.assertRaises(PlaywrightError):
            await client._complete_interactive_authentication()

    async def test_headless_bridge_prefers_resume_over_account_reset_url(self):
        resume = "https://login.microsoftonline.com/tenant/resume?ctx=private"
        cancel = "https://login.microsoftonline.com/tenant/reprocess?ctx=private"
        client = self.flow(
            [("webauthn", "passkey"), ("password_choice", "password")],
            url="https://login.microsoft.com/tenant/bridge/fido?resumeUrl="
            + quote(resume, safe="")
            + "&cancelUrl="
            + quote(cancel, safe=""),
        )
        await client._complete_interactive_authentication()
        client.page.goto.assert_awaited_once_with(
            resume, wait_until="domcontentloaded", timeout=30000
        )
        client.fields["Use my password"].click.assert_awaited_once()

    async def test_passkey_continuation_rejects_untrusted_origin(self):
        client = self.flow([("webauthn", "passkey")])
        client.page.evaluate.return_value = "https://example.invalid/collect"
        await client._complete_interactive_authentication()
        client.page.goto.assert_not_awaited()

    async def test_repeated_passkey_continuation_stops_instead_of_looping(self):
        client = self.flow([("webauthn", "passkey")] * 2)
        client.page.evaluate.return_value = (
            "https://login.microsoftonline.com/tenant/resume?ctx=test"
        )
        with self.assertRaisesRegex(AuthenticationRequired, "restarted sign-in"):
            await client._complete_interactive_authentication()
        client.page.goto.assert_awaited_once()

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

    async def test_unsupported_step_notifies_user_and_stops_headless(self):
        client = self.flow([("manual", "An interactive browser is required")])
        with self.assertRaisesRegex(AuthenticationRequired, "interactive browser"):
            await client._complete_interactive_authentication()
        client.auth_notify.assert_awaited_once()

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
