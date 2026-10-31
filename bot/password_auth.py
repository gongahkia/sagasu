"""Bounded password-only login for read-only scheduled jobs.

No screenshots, browser traces, page text, or credential values are logged.
An actual second-factor challenge is a failure, never automatically approved.
"""

import asyncio
import os
import re

from playwright.async_api import Error as PlaywrightError

from .fbs_client import AuthenticationRequired


LOGIN_FAILURES = {
    "missing_credentials": "Scheduled login credentials are missing",
    "rejected": "SMU rejected the scheduled sign-in",
    "interactive_verification": "SMU requires interactive verification; unattended login is unavailable",
    "timeout": "Password-only login timed out",
    "browser_error": "The browser could not complete the password-only login",
}


class PasswordLoginError(AuthenticationRequired):
    """Only fixed codes/stages may enter logs or public status JSON."""

    def __init__(self, code, stage="checking_credentials"):
        self.code = code
        self.stage = stage
        super().__init__(LOGIN_FAILURES[code])


async def password_sign_in(page, *, timeout_seconds=120, progress=None):
    email = os.environ.get("SMU_LOGIN_EMAIL", "").strip()
    password = os.environ.get("SMU_LOGIN_PASSWORD", "")
    if not email or not password:
        raise PasswordLoginError("missing_credentials")
    stage = "waiting_for_login"

    async def report(value):
        nonlocal stage
        if stage != value:
            stage = value
            if progress:
                await progress(f"login: {value}")

    try:
        async with asyncio.timeout(timeout_seconds):
            await _password_flow(page, email, password, report)
    except AuthenticationRequired:
        raise
    except TimeoutError:
        raise PasswordLoginError("timeout", stage) from None
    except Exception:
        # Playwright errors can include page text or entered values.
        raise PasswordLoginError("browser_error", stage) from None


async def _password_flow(page, email, password, report):
    email_attempts = password_attempts = alternate_attempts = 0
    security_prompt_cancelled = False
    transition_polls_remaining = 0
    password_submitted = False
    while True:
        if await page.locator("iframe#frameBottom").count():
            await report("authenticated")
            return
        try:
            body = (await page.locator("body").inner_text(timeout=5000)).casefold()
        except PlaywrightError as error:
            # Microsoft can redirect while we read the previous document.
            if "execution context was destroyed" not in str(error).casefold():
                raise
            await page.wait_for_timeout(250)
            continue
        if any(
            text in body
            for text in (
                "incorrect password",
                "password is incorrect",
                "account has been locked",
                "account or password is incorrect",
                "sign-in was blocked",
                "access has been blocked",
            )
        ):
            raise PasswordLoginError("rejected", "credential_rejected")

        if (
            not security_prompt_cancelled
            and "choose a way to sign in" not in body
            and any(
                text in body
                for text in (
                    "face, fingerprint, pin or security key",
                    "your device will open a security window",
                )
            )
        ):
            await report("cancelling_security_key_prompt")
            await _cancel_passkey(page, report)
            security_prompt_cancelled = True
            transition_polls_remaining = 12
            await page.wait_for_timeout(750)
            continue

        email_field = page.locator(
            'input[name="loginfmt"]:visible, input#userNameInput:visible, input[type="email"]:visible'
        ).first
        password_field = page.locator('input[type="password"]:visible').first
        if await password_field.count():
            transition_polls_remaining = 0
            if password_submitted or password_attempts >= 2:
                await page.wait_for_timeout(750)
                continue
            await report("entering_password")
            # The federated form may show both username and password together.
            if await email_field.count():
                await email_field.fill(email)
            await password_field.fill(password)
            await page.locator(
                '#submitButton:visible, input[type="submit"]:visible, button[type="submit"]:visible'
            ).first.click()
            password_attempts += 1
            password_submitted = True
            await report("waiting_after_password")
        elif await email_field.count():
            transition_polls_remaining = 0
            if email_attempts >= 1:
                await page.wait_for_timeout(750)
                continue
            await report("entering_username")
            await email_field.fill(email)
            await page.locator(
                'input[type="submit"]:visible, button[type="submit"]:visible'
            ).first.click()
            email_attempts += 1
            await report("waiting_after_username")
        elif "stay signed in" in body:
            await report("stay_signed_in")
            await page.locator("#idSIButton9:visible").click()
        else:
            # Choose only a password route. Never choose/approve Authenticator.
            password_option = page.get_by_text(
                re.compile(
                    r"^(use (my |your |a )?password( instead)?|password|sign in with (my |your |a )?password)$",
                    re.I,
                )
            )
            alternate = page.get_by_text(
                re.compile(
                    r"^(sign in another way|other ways to sign in|use another sign-in method)$",
                    re.I,
                )
            )
            clicked = False
            if alternate_attempts < 3:
                for options in (password_option, alternate):
                    for option in await options.all():
                        if await option.is_visible():
                            await report("selecting_password_route")
                            await option.click()
                            if options is password_option:
                                password_submitted = False
                                security_prompt_cancelled = False
                            alternate_attempts += 1
                            transition_polls_remaining = 12
                            clicked = True
                            break
                    if clicked:
                        break
            if not clicked and any(
                text in body
                for text in (
                    "approve a request",
                    "approve sign in",
                    "authenticator",
                    "enter code",
                    "enter the code",
                    "verify your identity",
                    "security key",
                    "fingerprint",
                    "passkey",
                    "more information required",
                )
            ):
                if transition_polls_remaining:
                    # The request may start after the first cancellation attempt.
                    if "your device will open a security window" in body:
                        await _cancel_passkey(page, report)
                    # Cancelling WebAuthn or choosing an alternate method can
                    # leave the old prompt visible while Microsoft redirects.
                    # Poll for the password choice before declaring MFA needed.
                    await report("waiting_for_password_choices")
                    transition_polls_remaining -= 1
                    await page.wait_for_timeout(750)
                    continue
                raise PasswordLoginError(
                    "interactive_verification", "verification_prompt"
                )
        await page.wait_for_timeout(750)


async def _cancel_passkey(page, report):
    cancelled = 0
    for frame in page.frames:
        try:
            cancelled += await frame.evaluate(
                "() => window.__sagasuCancelPasskeyRequest?.() ?? 0"
            )
        except PlaywrightError as error:
            if "execution context was destroyed" not in str(error).casefold():
                raise
    if cancelled:
        await report("passkey_request_cancelled")
    else:
        # Best effort for requests that predate installation of the hook.
        await page.keyboard.press("Escape")
