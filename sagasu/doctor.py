"""Bounded, read-only health checks. No messages or bookings are sent."""

import asyncio
import os
import socket
import ssl
from dataclasses import asdict

from .diagnostics import Check, local_checks


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


async def session_check(settings, timeout):
    if os.name != "nt":
        from .host import control, run_operation

        if (await control(settings, "status")).get("running"):
            try:
                async with asyncio.timeout(timeout):
                    await run_operation(settings, "session_status")
                return Check(
                    "session", "pass", "Saved session verified through the shared host."
                )
            except Exception:
                return Check(
                    "session",
                    "fail",
                    "Shared-host session verification failed or timed out.",
                    "Run sagasu login to renew the session; inspect sagasu host status for queued work.",
                )
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

    for check in local_checks(settings):
        add(check)
    if any(check.status == "fail" and check.blocks_startup for check in checks):
        add(
            Check(
                "browser",
                "skip",
                "Correct local configuration before launching a browser.",
            )
        )
        add(
            Check(
                "network",
                "skip",
                "Correct local configuration before checking FBS or the saved session.",
            )
        )
        return {"ok": False, "checks": [asdict(check) for check in checks]}
    add(await browser_check(settings, timeout))
    if offline:
        add(Check("network", "skip", "Offline mode: FBS and session requests skipped."))
    else:
        kind, status = await probe("fbs.intranet.smu.edu.sg", "/home", timeout)
        reachable = kind == "http" and 200 <= status < 400
        if reachable:
            add(Check("network.fbs", "pass", f"HTTPS responded (HTTP {status})."))
        else:
            add(
                Check(
                    "network.fbs",
                    "warn" if kind == "http" and status in (401, 403) else "fail",
                    (
                        f"HTTPS responded (HTTP {status})."
                        if kind == "http"
                        else f"HTTPS check failed: {kind}."
                    ),
                    "Check https://fbs.intranet.smu.edu.sg/home in a normal browser on this computer.",
                )
            )
        if session and reachable:
            add(await session_check(settings, timeout))
        elif session:
            add(
                Check("session", "skip", "FBS is not reachable; no profile was opened.")
            )
        else:
            add(
                Check(
                    "session",
                    "skip",
                    "Session validity not checked; use doctor --session with the MCP server stopped.",
                )
            )
    return {
        "ok": not any(check.status == "fail" for check in checks),
        "checks": [asdict(check) for check in checks],
    }
