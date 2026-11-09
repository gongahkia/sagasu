"""Read routing behind the existing domain tools. Writes never enter this router.

HTTP eligibility is learned from a browser read, verified against the full
browser listing and held only in memory for the same browser context. A fresh
host/session must verify again. Unsupported reads remain browser operations.
"""

import os
import time
from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError

from .fbs_account import read_bookings, read_task_list
from .fbs_client import AuthenticationRequired
from .fbs_http import HTTPReadUnavailable, read_account_http
from .fbs_traffic import ReadTraffic
from .observability import event

READ_MODES = {"auto", "browser", "api"}
READ_FAILURES = (HTTPReadUnavailable, AuthenticationRequired, PlaywrightError, TimeoutError)


@dataclass
class _VerifiedDocument:
    url: str = field(repr=False)
    verified_at: float


def equivalent_listing(kind, browser, http):
    """Exact domain parity, including action permissions, status and coverage."""
    if not browser.get("complete") or not http.get("complete") or browser["total"] != http["total"]:
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

    def capabilities(self):
        now = time.monotonic()
        return {
            "mode": self.mode,
            "browser": ["search", "bookings", "tasks"],
            "api": {
                "protocol": "session_backed_http_html",
                "candidates": ["bookings", "tasks"],
                "verified": sorted(k for k, v in self._verified.items() if now - v.verified_at < self.verification_ttl),
                "verification": "Complete browser parity required in this browser context; expires after 300 seconds.",
                "availability_search": "unsupported_until_endpoint_verified",
                "requires_browser_session": True,
            },
            "writes": {"backend": "browser", "automatic_fallback": False},
            "reconciliation": "browser",
        }

    def _bind(self, client):
        if self._context is not client.context:
            self._context = client.context
            self._verified.clear()

    @staticmethod
    def _result(result, backend, reason=None, verification=None):
        return {**result, "read_backend": {
            "name": backend,
            "protocol": "http_html" if backend == "api" else "browser_dom",
            "fallback_reason": reason,
            "api_verification": verification,
        }}

    async def search(self, client, config):
        if self.mode == "api":
            raise HTTPReadUnavailable("unsupported_operation")
        result = (await client.search(config)).to_dict()
        return self._result(result, "browser", "no_verified_search_endpoint" if self.mode == "auto" else None)

    async def account(self, client, kind):
        if kind not in {"bookings", "tasks"}:
            raise HTTPReadUnavailable("unsupported_operation")
        self._bind(client)
        read_browser = read_bookings if kind == "bookings" else read_task_list
        if self.mode == "browser":
            return self._result(await read_browser(client), "browser")
        endpoint = self._verified.get(kind)
        if endpoint and time.monotonic() - endpoint.verified_at >= self.verification_ttl:
            self._verified.pop(kind, None)
            endpoint = None
        if endpoint:
            try:
                result = await read_account_http(client, kind, endpoint.url)
                event("read.backend", operation=kind, backend="api")
                return self._result(result, "api", verification="verified")
            except READ_FAILURES as error:
                self._verified.pop(kind, None)
                if self.mode == "api":
                    raise
                reason = getattr(error, "code", None) or "http_read_failed"
                event("read.fallback", operation=kind, reason=reason)
                # Only these reads can fall back. Never dispatch/replay writes.
                return self._result(await read_browser(client), "browser", reason, "invalidated")
        if self.mode == "api":
            raise HTTPReadUnavailable("browser_verification_required")
        return await self.verify_account(client, kind)

    async def verify_account(self, client, kind):
        """Read in the browser, discover a GET, compare its HTTP representation.

        Always return the browser observation for the verification read. Nothing
        persistent is enabled by a probe and no endpoint names are guessed.
        """
        if kind not in {"bookings", "tasks"}:
            raise HTTPReadUnavailable("unsupported_operation")
        self._bind(client)
        self._verified.pop(kind, None)
        read_browser = read_bookings if kind == "bookings" else read_task_list
        with ReadTraffic(client.page) as traffic:
            result = await read_browser(client)
            frame = client.page.frame(name="frameContent")
            url = traffic.document_url(frame)
        reason = "no_observed_get_document"
        if not result.get("complete"):
            reason = "incomplete_browser_listing"
        elif url:
            try:
                http = await read_account_http(client, kind, url)
                if equivalent_listing(kind, result, http):
                    self._verified[kind] = _VerifiedDocument(url, time.monotonic())
                    event("read.verified", operation=kind, protocol="http_html")
                    return self._result(result, "browser", verification="verified")
                reason = "browser_http_mismatch"
            except READ_FAILURES as error:
                reason = getattr(error, "code", None) or "http_read_failed"
        return self._result(result, "browser", reason, "unverified")
