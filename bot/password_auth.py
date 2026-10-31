"""Bounded password-only login for read-only scheduled jobs.

No screenshots, browser traces, page text, or credential values are logged.
An actual second-factor challenge is a failure, never automatically approved.
"""

import asyncio
import os
import re

from .fbs_client import AuthenticationRequired


async def password_sign_in(page, *, timeout_seconds=120):
    email = os.environ.get("SMU_LOGIN_EMAIL", "").strip()
    password = os.environ.get("SMU_LOGIN_PASSWORD", "")
    if not email or not password:
        raise AuthenticationRequired("Scheduled login credentials are missing")
    try:
        async with asyncio.timeout(timeout_seconds):
            await _password_flow(page, email, password)
    except AuthenticationRequired:
        raise
    except Exception:
        # Playwright errors can include page text or entered values.
        raise AuthenticationRequired(
            "Password-only login failed or timed out; check the sign-in route locally"
        ) from None


async def _password_flow(page, email, password):
    email_attempts = password_attempts = alternate_attempts = 0
    while True:
        if await page.locator("iframe#frameBottom").count():
            return
        body = (await page.locator("body").inner_text(timeout=5000)).casefold()
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
            raise AuthenticationRequired("SMU rejected the scheduled sign-in")

        email_field = page.locator(
            'input[name="loginfmt"]:visible, input#userNameInput:visible, input[type="email"]:visible'
        ).first
        password_field = page.locator('input[type="password"]:visible').first
        if await password_field.count():
            if password_attempts >= 1:
                await page.wait_for_timeout(750)
                continue
            # The federated form may show both username and password together.
            if await email_field.count():
                await email_field.fill(email)
            await password_field.fill(password)
            await page.locator(
                '#submitButton:visible, input[type="submit"]:visible, button[type="submit"]:visible'
            ).first.click()
            password_attempts += 1
        elif await email_field.count():
            if email_attempts >= 1:
                await page.wait_for_timeout(750)
                continue
            await email_field.fill(email)
            await page.locator(
                'input[type="submit"]:visible, button[type="submit"]:visible'
            ).first.click()
            email_attempts += 1
        elif "stay signed in" in body:
            await page.locator("#idSIButton9:visible").click()
        else:
            # Choose only a password route. Never choose/approve Authenticator.
            password_option = page.get_by_text(
                re.compile(
                    r"^(use (your |a )?password( instead)?|password|sign in with (your |a )?password)$",
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
                            await page.keyboard.press("Escape")
                            await option.click()
                            alternate_attempts += 1
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
                raise AuthenticationRequired(
                    "SMU requires interactive verification; unattended login is unavailable"
                )
        await page.wait_for_timeout(750)
