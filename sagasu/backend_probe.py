"""Explicit live, read-only backend reconnaissance and account HTTP parity.

Reports contain counts and bounded request shapes, never credentials, literal
endpoints, response bodies or account rows. Sign in separately if necessary.
"""

import asyncio
import os

from .fbs_client import AuthenticationRequired, FBSClient, search_config_from_preferences
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
            "HTTP account reads require a complete browser match in each browser context; no standalone REST API has been established.",
            "Availability and all writes still use the browser. JSON traffic alone does not prove an endpoint is usable or read-only.",
            "No literal URLs, headers, bodies, cookies, parameter values or account rows are included in this report.",
        ],
    }


async def probe_client(client, reads, config):
    report = _report()
    stage = "session"
    with ReadTraffic(client.page) as traffic:
        try:
            await client.ensure_authenticated()
            report["checks"].append({"name": stage, "status": "pass"})
            for stage in ("search", "bookings", "tasks"):
                traffic.stage = stage
                try:
                    if stage == "search":
                        # Inspect the actual browser search, even in strict API
                        # mode. This probe is the explicit discovery operation.
                        result = await client.search(config)
                        check = {
                            "name": stage, "status": "pass", "backend": "browser",
                            "rooms_read": len(result.rooms),
                            "bookable_rooms": len(result.bookable_rooms),
                            "api_verification": "unsupported_until_endpoint_verified",
                        }
                    else:
                        result = await reads.verify_account(client, stage)
                        check = {
                            "name": stage, "status": "pass",
                            "rows_read": len(result[stage]), "total": result["total"],
                            "complete": result["complete"],
                            **result["read_backend"],
                        }
                        # 'name' in provenance describes the backend, not stage.
                        check["backend"] = check.pop("name")
                        check["name"] = stage
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
        report["recommendation"] = "hybrid_for_verified_account_reads"
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
                    config = search_config_from_preferences(PersonalStore(settings.preferences).preferences())
                    return await probe_client(client, HybridReads("auto"), config)
    except Exception as error:
        report["checks"].append(_failure("probe", error))
    return report
