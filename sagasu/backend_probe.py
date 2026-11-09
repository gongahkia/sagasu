"""Explicit live, read-only backend reconnaissance and account HTTP parity.

Reports contain counts and bounded request shapes, never credentials, literal
endpoints, response bodies or account rows. Sign in separately if necessary.
"""

import asyncio
import os

from .fbs_client import (
    AuthenticationRequired,
    FBSClient,
    search_config_from_preferences,
)
from .fbs_traffic import ReadTraffic
from .personal import PersonalStore
from .read_backends import HybridReads
from .runtime import InstanceLock


def _failure(stage, error):
    return {
        "name": stage,
        "status": "fail",
        "error_code": type(error).__name__,
        "hint": (
            "Run sagasu login and approve MFA, then retry sagasu backend-probe --live."
            if isinstance(error, AuthenticationRequired)
            else "Run sagasu doctor and retry the read-only backend probe. Raw errors are suppressed."
        ),
    }


def _report():
    return {
        "ok": False,
        "live": True,
        "read_only": True,
        "checks": [],
        "api_verified": [],
        "recommendation": "browser",
        "limitations": [
            "Direct reads reproduce FBS's observed ASP.NET forms and HTML; they are not an official REST API.",
            "Login/MFA, writes, and reconciliation use the browser. Direct reads never submit booking/cancellation/approval actions.",
            "No literal URLs, headers, bodies, cookies, parameter values or account rows are included in this report.",
        ],
    }


async def probe_client(client, reads, config):
    report = _report()
    reads.invalidate()
    stage = "session"
    with ReadTraffic(client.page) as traffic:
        try:
            await client.ensure_authenticated()
            report["checks"].append({"name": stage, "status": "pass"})
            for stage in ("search", "bookings", "tasks"):
                traffic.stage = stage
                try:
                    if stage == "search":
                        result = await reads.verify_search(client, config)
                        provenance = result["read_backend"]
                        check = {
                            "name": stage,
                            "status": "pass",
                            "backend": "browser",
                            "rooms_read": len(result["rooms"]),
                            "api_verification": provenance["api_verification"],
                            "fallback_reason": provenance["fallback_reason"],
                        }
                    else:
                        result = await reads.verify_account(client, stage)
                        provenance = result["read_backend"]
                        check = {
                            "name": stage,
                            "status": "pass",
                            "rows_read": len(result[stage]),
                            "total": result["total"],
                            "complete": result["complete"],
                            "backend": provenance["name"],
                            "api_verification": provenance["api_verification"],
                            "fallback_reason": provenance["fallback_reason"],
                        }
                    report["checks"].append(check)
                except AuthenticationRequired:
                    raise
                except Exception as error:
                    report["checks"].append(_failure(stage, error))
        except Exception as error:
            report["checks"].append(_failure(stage, error))
        finally:
            report["traffic"] = traffic.report()
            report["api_verified"] = reads.capabilities()["api"]["verified"]
    report["ok"] = all(check["status"] == "pass" for check in report["checks"])
    if report["api_verified"]:
        report["recommendation"] = "direct_reads_with_browser_fallback"
    return report


async def run_probe(settings, *, timeout=180):
    """Use the shared host queue when running, otherwise own the private profile."""
    report = _report()
    try:
        async with asyncio.timeout(timeout):
            if os.name != "nt":
                from .host import control, run_operation

                if (await control(settings, "status")).get("running"):
                    return await run_operation(settings, "probe_read_backends")
            with InstanceLock(settings.profile):
                async with FBSClient(
                    interactive=False, keep_alive=False, profile_path=settings.profile
                ) as client:
                    config = search_config_from_preferences(
                        PersonalStore(settings.preferences).preferences()
                    )
                    return await probe_client(client, HybridReads("auto"), config)
    except Exception as error:
        report["checks"].append(_failure("probe", error))
    return report
