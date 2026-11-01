"""Bounded, read-only health checks. No messages or bookings are sent."""

import asyncio
import importlib.metadata
import os
import platform
import socket
import ssl
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import ConfigError, TOKEN_PATTERN


@dataclass
class Check:
    name: str
    status: str
    message: str
    hint: str = ""


async def probe(host, path, timeout):
    """Return only a status category, never response bodies or redirect URLs."""
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(
                host,
                443,
                ssl=ssl.create_default_context(),
                server_hostname=host,
                happy_eyeballs_delay=0.25,
            )
            writer.write(
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode(
                    "ascii"
                )
            )
            await writer.drain()
            line = await reader.readline()
            status = int(line.split()[1])
            return "http", status
    except TimeoutError:
        return "timeout", None
    except socket.gaierror:
        return "dns", None
    except ssl.SSLError:
        return "tls", None
    except (OSError, ValueError, IndexError):
        return "connection", None
    finally:
        if writer:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except Exception:
                pass


async def browser_check(settings, timeout):
    try:
        from playwright.async_api import async_playwright

        async with asyncio.timeout(timeout):
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    channel=settings.browser, headless=True, timeout=timeout * 1000
                )
                try:
                    page = await browser.new_page()
                    await page.goto("about:blank")
                finally:
                    await browser.close()
        return Check(
            "browser",
            "pass",
            f"{settings.browser} launches successfully in an isolated temporary profile.",
        )
    except Exception:
        return Check(
            "browser",
            "fail",
            f"{settings.browser} could not launch.",
            "On Fedora: python -m playwright install chromium; then use --browser chromium.",
        )


async def token_check(settings, timeout):
    try:
        from telegram import Bot
        from telegram.error import InvalidToken
    except ImportError:
        return Check("telegram.token", "fail", "python-telegram-bot is not installed.")
    try:
        async with asyncio.timeout(timeout):
            async with Bot(settings.token):
                pass  # initialize performs getMe; never sends a Telegram message.
        return Check(
            "telegram.token", "pass", "Telegram accepted the configured token."
        )
    except InvalidToken:
        return Check(
            "telegram.token",
            "fail",
            "Telegram rejected the configured token.",
            "Replace BOT_TOKEN locally using @BotFather.",
        )
    except Exception:
        return Check(
            "telegram.token",
            "fail",
            "Could not verify the token with Telegram.",
            "Check Telegram connectivity and retry. Raw request details are suppressed.",
        )


async def session_check(settings, timeout):
    if not settings.profile.is_dir():
        return Check(
            "session",
            "warn",
            "No browser profile yet.",
            "Run sagasu login to complete Microsoft sign-in and MFA.",
        )
    from .runtime import AlreadyRunning, InstanceLock
    from .fbs_client import AuthenticationRequired, FBSClient

    try:
        with InstanceLock(settings.profile):
            async with asyncio.timeout(timeout):
                async with FBSClient(
                    interactive=False,
                    headless=True,
                    keep_alive=False,
                    profile_path=settings.profile,
                ) as client:
                    await client.ensure_authenticated()
        return Check(
            "session",
            "pass",
            "Saved session reached authenticated FBS search controls.",
        )
    except AlreadyRunning:
        return Check(
            "session",
            "warn",
            "The profile is in use; session validity was not checked.",
            "Stop the other Sagasu process before using doctor --session.",
        )
    except AuthenticationRequired:
        return Check(
            "session",
            "fail",
            "The saved session did not reach authenticated FBS.",
            "Run sagasu login and complete MFA locally.",
        )
    except Exception:
        return Check(
            "session",
            "fail",
            "Session check failed or exceeded its deadline.",
            "Check FBS reachability; renew with sagasu login when FBS is available.",
        )


async def diagnose(settings, *, offline=False, session=False, timeout=10, emit=None):
    checks = []

    def add(check):
        checks.append(check)
        if emit:
            emit(check)

    add(
        Check(
            "python",
            "pass",
            f"Python {platform.python_version()} on {platform.system()}.",
        )
    )
    for distribution in ("python-telegram-bot", "playwright", "python-dotenv"):
        try:
            version = importlib.metadata.version(distribution)
            add(Check(f"dependency.{distribution}", "pass", f"Installed ({version})."))
        except importlib.metadata.PackageNotFoundError:
            add(
                Check(
                    f"dependency.{distribution}",
                    "fail",
                    "Not installed.",
                    "Install Sagasu in this Python environment: python -m pip install -e .",
                )
            )
    add(
        Check(
            "config.file",
            "pass" if settings.env_file.is_file() else "warn",
            (
                "Environment file found."
                if settings.env_file.is_file()
                else "No environment file; exported variables are used."
            ),
            (
                ""
                if settings.env_file.is_file()
                else "Create a template with sagasu config init."
            ),
        )
    )
    valid_token = bool(TOKEN_PATTERN.fullmatch(settings.token))
    add(
        Check(
            "config.token",
            "pass" if valid_token else "fail",
            (
                "Token format looks valid; value redacted."
                if valid_token
                else "BOT_TOKEN is missing, malformed, or still a placeholder."
            ),
            (
                ""
                if valid_token
                else "Set your real @BotFather token in the environment file."
            ),
        )
    )
    try:
        settings.validate(require_token=False)
        add(
            Check(
                "config.owner",
                "pass" if settings.owner_id else "warn",
                (
                    "Owner ID is configured."
                    if settings.owner_id
                    else "No owner restriction: any Telegram user can access this bot."
                ),
                (
                    ""
                    if settings.owner_id
                    else "Set TELEGRAM_OWNER_ID to your numeric Telegram user ID."
                ),
            )
        )
    except ConfigError:
        add(
            Check(
                "config.owner",
                "fail",
                "Owner ID must be a positive numeric Telegram user ID.",
            )
        )
    for name, target in (
        ("profile", settings.profile),
        ("state", settings.persistence),
    ):
        parent = target if target.is_dir() else target.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        writable = os.access(parent, os.W_OK)
        add(
            Check(
                f"storage.{name}",
                "pass" if writable else "fail",
                (
                    "Parent directory is writable."
                    if writable
                    else "Parent directory is not writable."
                ),
            )
        )
    add(await browser_check(settings, timeout))
    if offline:
        add(
            Check(
                "network",
                "skip",
                "Offline mode: FBS, Telegram and session requests skipped.",
            )
        )
    else:
        outcomes = await asyncio.gather(
            probe("fbs.intranet.smu.edu.sg", "/home", timeout),
            probe("api.telegram.org", "/", timeout),
        )
        for name, (kind, status) in zip(("fbs", "telegram"), outcomes):
            if kind == "http" and 200 <= status < 400:
                add(
                    Check(
                        f"network.{name}", "pass", f"HTTPS responded (HTTP {status})."
                    )
                )
            elif kind == "http":
                add(
                    Check(
                        f"network.{name}",
                        "warn" if status in (401, 403) else "fail",
                        f"HTTPS responded (HTTP {status}).",
                        "Check the service in a normal browser on this computer.",
                    )
                )
            else:
                add(
                    Check(
                        f"network.{name}",
                        "fail",
                        f"HTTPS check failed: {kind}.",
                        (
                            "For FBS, test https://fbs.intranet.smu.edu.sg/home in a normal browser; an outage or network policy may prevent access."
                            if name == "fbs"
                            else "Check network access to api.telegram.org."
                        ),
                    )
                )
        if valid_token and outcomes[1][0] == "http":
            add(await token_check(settings, timeout))
        if session:
            if outcomes[0][0] == "http" and 200 <= outcomes[0][1] < 400:
                add(await session_check(settings, timeout))
            else:
                add(
                    Check(
                        "session",
                        "skip",
                        "FBS is not reachable; no profile was opened.",
                    )
                )
        else:
            add(
                Check(
                    "session",
                    "skip",
                    "Session validity not checked; use doctor --session with the bot stopped.",
                )
            )
    return {
        "ok": not any(check.status == "fail" for check in checks),
        "checks": [asdict(check) for check in checks],
    }
