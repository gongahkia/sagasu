"""Direct read routing and explicit browser parity checks; no write dispatch.

Normal direct reads validate FBS's response schema and coverage. The explicit
probe compares the same HTTP implementation with browser results. A detected
mismatch disables that direct operation until a new check or invalidation.
"""

import os
import time
from playwright.async_api import Error as PlaywrightError

from .fbs_account import read_bookings, read_task_list
from .fbs_api import FBSReadAPI
from .fbs_client import AuthenticationRequired
from .fbs_http import HTTPReadUnavailable

READ_MODES = {"auto", "browser", "api"}
READ_FAILURES = (
    HTTPReadUnavailable,
    AuthenticationRequired,
    PlaywrightError,
    TimeoutError,
)
READ_KINDS = {"search", "bookings", "tasks"}


def equivalent_listing(kind, browser, http):
    """Exact domain parity, including permissions, references and coverage."""
    if (
        not browser.get("complete")
        or not http.get("complete")
        or browser["total"] != http["total"]
    ):
        return False

    def rows(value):
        items = value[kind]
        refs = [r["reference_number"] for r in items]
        if len(set(refs)) != len(refs) or len(items) != value["total"]:
            return None
        return sorted(items, key=lambda r: r["reference_number"])

    expected, actual = rows(browser), rows(http)
    return expected is not None and actual is not None and expected == actual


class HybridReads:
    verification_ttl = 300

    def __init__(self, mode=None):
        self.mode = mode or os.getenv("SAGASU_READ_BACKEND", "auto")
        if self.mode not in READ_MODES:
            raise ValueError("SAGASU_READ_BACKEND must be auto, browser or api.")
        self._context = None
        self._verified = {}
        self._blocked = {}

    def invalidate(self):
        self._context = None
        self._verified.clear()
        self._blocked.clear()

    def _bind(self, client):
        if self._context is not client.context:
            self._context = client.context
            self._verified.clear()

    def capabilities(self):
        now = time.monotonic()
        return {
            "mode": self.mode,
            "routed_tools": ["search_rooms", "list_bookings", "list_tasks"],
            "browser": sorted(READ_KINDS),
            "browser_workflows": [
                "booking_details",
                "alternatives",
                "repeat",
                "task_details",
                "validate",
                "smoke",
            ],
            "api": {
                "protocol": "aspnet_webforms_and_html",
                "supported": sorted(READ_KINDS),
                "verified": sorted(
                    k
                    for k, verified_at in self._verified.items()
                    if now - verified_at < self.verification_ttl
                ),
                "disabled": dict(self._blocked),
                "verification": "Every direct response validates schema and coverage. verified lists exact browser comparisons in the last 300 seconds.",
                "availability_search": "supported_webforms",
                "requires_saved_session": True,
                "launches_chromium": False,
                "authentication": "Browser login/MFA saves the private FBS session.",
            },
            "writes": {"backend": "browser", "automatic_fallback": False},
            "reconciliation": "browser",
        }

    @staticmethod
    def _result(result, backend, reason=None, verification=None):
        return {
            **result,
            "read_backend": {
                "name": backend,
                "protocol": "http_html" if backend == "api" else "browser_dom",
                "fallback_reason": reason,
                "api_verification": verification,
            },
        }

    async def direct(self, kind, payload=None, progress=None, *, request=None):
        if kind not in READ_KINDS or self.mode == "browser":
            raise HTTPReadUnavailable("unsupported_operation")
        if kind in self._blocked:
            raise HTTPReadUnavailable(self._blocked[kind])
        async with FBSReadAPI(
            request=request,
            profile_path=os.getenv("SAGASU_BROWSER_PROFILE"),
            progress=progress,
        ) as api:
            result = (
                (await api.search(payload)).to_dict()
                if kind == "search"
                else await api.account(kind)
            )
        result = self._result(result, "api", verification="schema_validated")
        result["read_backend"]["protocol"] = (
            "aspnet_webforms" if kind == "search" else "http_html"
        )
        return result

    async def search(self, client, config):
        """Browser read selected explicitly or after one known direct failure."""
        if self.mode == "api":
            raise HTTPReadUnavailable("browser_fallback_disabled")
        return self._result((await client.search(config)).to_dict(), "browser")

    async def account(self, client, kind):
        if kind not in {"bookings", "tasks"} or self.mode == "api":
            raise HTTPReadUnavailable("browser_fallback_disabled")
        read = read_bookings if kind == "bookings" else read_task_list
        return self._result(await read(client), "browser")

    def _checked(self, kind, browser, reason=None):
        if reason:
            self._verified.pop(kind, None)
            self._blocked[kind] = reason
        else:
            self._blocked.pop(kind, None)
            self._verified[kind] = time.monotonic()
        return self._result(
            browser, "browser", reason, "unverified" if reason else "verified"
        )

    async def verify_search(self, client, config):
        self._bind(client)
        browser = (await client.search(config)).to_dict()
        try:
            async with FBSReadAPI(request=client.context.request) as api:
                http = (await api.search(config)).to_dict()

            def comparable(result):
                return {
                    "config": result["config"],
                    "rooms": sorted(result["rooms"], key=lambda r: r["name"]),
                    "booking_horizon": result.get("booking_horizon"),
                }

            if comparable(browser) == comparable(http):
                return self._checked("search", browser)
            reason = "browser_http_mismatch"
        except READ_FAILURES as error:
            reason = getattr(error, "code", None) or "http_read_failed"
        return self._checked("search", browser, reason)

    async def verify_account(self, client, kind):
        """Compare the actual direct account adapter with complete browser rows."""
        if kind not in {"bookings", "tasks"}:
            raise HTTPReadUnavailable("unsupported_operation")
        self._bind(client)
        read = read_bookings if kind == "bookings" else read_task_list
        browser = await read(client)
        if not browser.get("complete"):
            return self._checked(kind, browser, "incomplete_browser_listing")
        try:
            async with FBSReadAPI(request=client.context.request) as api:
                http = await api.account(kind)
            if equivalent_listing(kind, browser, http):
                return self._checked(kind, browser)
            reason = "browser_http_mismatch"
        except READ_FAILURES as error:
            reason = getattr(error, "code", None) or "http_read_failed"
        return self._checked(kind, browser, reason)
