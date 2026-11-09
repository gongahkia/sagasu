"""Direct, read-only FBS protocol client using an already authenticated session.

The account paths and search postbacks were observed and compared with live FBS
in November 2026. No login/MFA, action submissions, generic URL dispatch or
JavaScript execution is available here. Chromium is not launched by this client.
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from playwright.async_api import async_playwright

from .config import DEFAULT_PROFILE
from .fbs_client import AuthenticationRequired, FBS_URL, SearchResult, _noop_progress
from .fbs_http import (
    HTTPReadUnavailable,
    MAX_DOCUMENT_BYTES,
    _Document,
    fbs_document_url,
    parse_account_document,
)
from .fbs_webforms import FILTERS, READ_TARGETS, SearchForm, parse_calendar_document

ACCOUNT_PATHS = {
    "bookings": "/modules/homenavigation/mybookingstab.aspx",
    "tasks": "/modules/homenavigation/tasklist.aspx",
}


class FBSReadAPI:
    def __init__(self, *, request=None, profile_path=None, progress=None):
        self.request = request
        self.profile_path = Path(
            profile_path or os.getenv("SAGASU_BROWSER_PROFILE", str(DEFAULT_PROFILE))
        )
        self.progress = progress or _noop_progress
        self._driver = None
        self._owns_request = request is None

    async def __aenter__(self):
        if self.request is not None:
            return self
        try:
            cookies = json.loads(
                (self.profile_path / ".sagasu-session.json").read_text()
            )
            if not isinstance(cookies, list):
                raise ValueError()
            cookies = [
                cookie
                for cookie in cookies
                if isinstance(cookie, dict)
                and cookie.get("domain", "").lstrip(".")
                in {"fbs.intranet.smu.edu.sg", "intranet.smu.edu.sg", "smu.edu.sg"}
            ]
            if not cookies:
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise AuthenticationRequired(
                "Saved FBS HTTP session is unavailable; renew login."
            ) from None
        try:
            self._driver = await async_playwright().start()
            self.request = await self._driver.request.new_context(
                storage_state={"cookies": cookies, "origins": []}
            )
        except BaseException:
            if self._driver:
                await self._driver.stop()
            raise
        return self

    async def __aexit__(self, *_):
        if self._owns_request:
            try:
                if self.request:
                    await self.request.dispose()
            finally:
                if self._driver:
                    await self._driver.stop()

    async def document(self, url, fields=None):
        if not fbs_document_url(url):
            raise HTTPReadUnavailable("untrusted_endpoint")
        if fields is not None and (
            urlsplit(url).path != "/modules/BigSpace/BookingCalendar/Calendar.aspx"
            or fields.get("__EVENTTARGET") not in READ_TARGETS
            or "__CALLBACKID" in fields
            or "__CALLBACKPARAM" in fields
        ):
            raise HTTPReadUnavailable("unsupported_postback")
        headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
        if fields is None:
            response = await self.request.get(
                url, headers=headers, max_redirects=0, max_retries=0, timeout=15000
            )
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            response = await self.request.post(
                url,
                data=urlencode(fields),
                headers=headers,
                max_redirects=0,
                max_retries=0,
                timeout=15000,
            )
        try:
            if response.status in {301, 302, 303, 307, 308, 401, 403}:
                raise AuthenticationRequired("Saved FBS HTTP session needs renewal.")
            if response.status != 200 or not fbs_document_url(response.url):
                raise HTTPReadUnavailable("http_response_rejected")
            if (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != "text/html"
            ):
                raise HTTPReadUnavailable("unsupported_content_type")
            size = response.headers.get("content-length", "")
            if size.isdigit() and int(size) > MAX_DOCUMENT_BYTES:
                raise HTTPReadUnavailable("document_limit")
            body = await response.body()
            if len(body) > MAX_DOCUMENT_BYTES:
                raise HTTPReadUnavailable("document_limit")
            try:
                return body.decode("utf-8-sig")
            except UnicodeError:
                raise HTTPReadUnavailable("unsupported_encoding") from None
        finally:
            await response.dispose()

    async def account(self, kind):
        if kind not in ACCOUNT_PATHS:
            raise HTTPReadUnavailable("unsupported_operation")
        await self.progress("reading FBS account directly over HTTP")
        html = await self.document(urljoin(FBS_URL, ACCOUNT_PATHS[kind]))
        if (
            kind == "tasks"
            and _Document(html).by_id("txtSearch_c1").attrs.get("value", "").strip()
        ):
            raise HTTPReadUnavailable("filtered_account_listing")
        return parse_account_document(kind, html)

    async def search(self, config):
        async with asyncio.timeout(120):
            return await self._search(config)

    async def _search(self, config):
        await self.progress("loading the FBS search form over HTTP")
        url = FBS_URL
        for identifier in ("frameBottom", "frameContent"):
            document = _Document(await self.document(url))
            iframe = document.by_id(identifier)
            if iframe.tag != "iframe" or not iframe.attrs.get("src"):
                raise HTTPReadUnavailable("unreadable_navigation")
            url = urljoin(url, iframe.attrs["src"])
        if urlsplit(url).path != "/modules/BigSpace/BookingCalendar/Calendar.aspx":
            raise HTTPReadUnavailable("unsupported_search_endpoint")
        form = SearchForm(await self.document(url))

        async def post(target, changes=None):
            return SearchForm(await self.document(url, form.payload(target, changes)))

        target_date = datetime.strptime(config.date, "%d-%b-%Y").date()
        for _ in range(33):
            try:
                current = datetime.strptime(
                    form.fields().get("DateBookingFrom_c1$textDate", ""), "%d-%b-%Y"
                ).date()
            except ValueError:
                raise HTTPReadUnavailable("unreadable_search_date") from None
            if current == target_date:
                break
            direction = "Next" if current < target_date else "Prev"
            form = await post(f"BtnDpc{direction}$BtnDpc{direction}_bt")
        else:
            raise HTTPReadUnavailable("date_navigation_limit")
        for field, value in (
            ("TimeFrom_c1$ctl04", config.start_time),
            ("TimeTo_c1$ctl04", config.end_time),
        ):
            form.select_value(field.replace("$", "_"), value)
            form = await post(field, {field: value})
            if form.fields().get(field) != value:
                raise HTTPReadUnavailable("time_not_retained")
        for control, attribute in FILTERS:
            value = form.multi_values(control, getattr(config, attribute))
            field = control + "_textValue"
            if form.fields().get(field, "") != value:
                form = await post(field, {field: value})
            if form.fields().get(field, "") != value:
                raise HTTPReadUnavailable("filter_not_retained")
        capacity = config.capacity or ""
        form.select_value("DropCapacity_c1", capacity)
        if form.fields().get("DropCapacity_c1", "") != capacity:
            form = await post("DropCapacity_c1", {"DropCapacity_c1": capacity})
        fields = form.fields()
        if fields.get("DropCapacity_c1", "") != capacity or any(
            fields.get(control + "_textValue", "")
            != form.multi_values(control, getattr(config, attribute))
            for control, attribute in FILTERS
        ):
            raise HTTPReadUnavailable("filter_not_retained")
        if (
            fields.get("DateBookingFrom_c1$textDate"),
            fields.get("TimeFrom_c1$ctl04"),
            fields.get("TimeTo_c1$ctl04"),
        ) != (config.date, config.start_time, config.end_time):
            raise HTTPReadUnavailable("search_window_mismatch")
        rooms = form.rooms()
        if not rooms:
            return SearchResult(config, [], datetime.now().astimezone().isoformat())
        await self.progress("reading FBS availability directly over HTTP")
        changes = {field: value for field, value in rooms.values()}
        html = await self.document(
            url, form.payload("CheckAvailability$CheckAvailability_bt", changes)
        )
        return parse_calendar_document(html, config, list(rooms))
