"""In-memory screenshots of the current FBS page, without navigation or login."""

import re
from datetime import datetime
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from .fbs_client import FBSError, FBSClient, FBS_TIMEZONE
from .fbs_http import fbs_document_url
from .observability import observed_step


class ScreenshotUnavailable(FBSError):
    default_reason = "browser_screenshot_unavailable"


async def _frame_mask(frame):
    parent = frame.parent_frame
    if parent:
        for locator in await parent.locator("iframe,frame").all():
            handle = await locator.element_handle()
            if handle:
                try:
                    if await handle.content_frame() is frame:
                        return locator
                finally:
                    await handle.dispose()
    raise ScreenshotUnavailable("The external frame could not be masked.")


@observed_step("browser.screenshot")
async def capture_browser_screenshot(client):
    page = client.page
    if not page or page.is_closed() or not fbs_document_url(page.url):
        raise ScreenshotUnavailable("No current FBS page is available.")
    changed = False

    def invalidate(_):
        nonlocal changed
        changed = True

    events = ("framenavigated", "frameattached", "framedetached")
    for event in events:
        page.on(event, invalidate)
    try:
        masks, errors, external = [], [], 0
        for frame in page.frames:
            if frame.url == "about:blank" and frame is not page.main_frame:
                masks.append(await _frame_mask(frame))
                external += 1
                continue
            if FBSClient._trusted_login_url(frame.url):
                raise ScreenshotUnavailable("Authentication pages cannot be captured.")
            if not fbs_document_url(frame.url):
                parsed = urlsplit(frame.url)
                native_error = (
                    parsed.hostname == "appexception.aspx"
                    and parsed.path in {"", "/"}
                    and not (
                        parsed.query
                        or parsed.fragment
                        or parsed.username
                        or parsed.password
                    )
                    and await frame.evaluate("location.protocol") == "chrome-error:"
                )
                if native_error:
                    errors.extend(
                        code
                        for code in await frame.locator(
                            ".error-code"
                        ).all_text_contents()
                        if re.fullmatch(r"ERR_[A-Z_]{1,64}", code)
                    )
                else:
                    masks.append(await _frame_mask(frame))
                    external += 1
                continue
            if await frame.locator(
                'input[type="password"],input[autocomplete="one-time-code"],#idRichContext_DisplaySign,.displaySign'
            ).count():
                raise ScreenshotUnavailable(
                    "Authentication controls cannot be captured."
                )
            masks.append(frame.locator("input,textarea,select,[contenteditable]"))
        if changed:
            raise ScreenshotUnavailable("The page changed before capture.")
        png = await page.screenshot(
            type="png",
            full_page=False,
            animations="disabled",
            mask=masks,
            mask_color="#000000",
            timeout=10000,
        )
        if changed or not fbs_document_url(page.url):
            raise ScreenshotUnavailable("The page changed during capture.")
        return {
            "receipt_png": png,
            "receipt_kind": "browser_debug",
            "captured_at": datetime.now(FBS_TIMEZONE).isoformat(),
            "scope": "current_fbs_viewport",
            "form_fields_masked": True,
            "external_frames_masked": external,
            "browser_error_codes": sorted(set(errors)),
            "next_tool": "operation_receipt",
        }
    except (PlaywrightError, ValueError):
        raise ScreenshotUnavailable(
            "The current FBS page could not be captured."
        ) from None
    finally:
        for event in events:
            page.remove_listener(event, invalidate)
