"""FBS account reads and explicitly confirmed single-booking cancellation."""

import asyncio
import re
from .action_journal import checkpoint_verified

from playwright.async_api import Error as PlaywrightError

from .fbs_client import FBSError, LayoutChanged, FBSClient
from .observability import event, observed_operation, observed_step, step


@observed_step("account.navigate")
async def open_account_tab(client, tab, marker):
    event("account.navigation", tab=tab, expected_selector=marker, timeout_ms=45000)
    if not client.page:
        raise FBSError("Browser page is unavailable")
    navigation = client.page.frame(name="frameBottom")
    if not navigation:
        raise LayoutChanged("FBS account navigation is unavailable")
    # These tabs replace the content iframe, sometimes more than once. Do not
    # mistake a previous document or an intermediate blank frame for readiness.
    async with client.page.expect_event(
        "framenavigated",
        predicate=lambda frame: frame.name == "frameContent"
        and frame.url != "about:blank",
        timeout=45000,
    ):
        await navigation.locator(f"div#{tab}").click()
    async with asyncio.timeout(45):
        while True:
            frame = client.page.frame(name="frameContent")
            try:
                if frame and await frame.locator(marker).first.is_visible():
                    await client._wait_for_idle(frame)
                    return frame
            except PlaywrightError:
                if frame and not frame.is_detached():
                    raise
            await client.page.wait_for_timeout(250)


@observed_step("receipt.capture")
async def capture_booking_evidence(client, reference):
    """Open VIEW for one exact verified reference; return PNG bytes in memory."""
    if not reference or not reference.strip():
        raise LayoutChanged("The verified booking has no reference number")
    await client.progress("opening the verified booking for its receipt")
    frame = await open_account_tab(client, "MyBookingsTab", "#GridViewBooking_content")
    rows = frame.locator("#GridViewBooking_content tr.row")
    matches = []
    for row in await rows.all():
        cells = row.locator("td")
        if (
            await cells.count() >= 9
            and (await cells.nth(1).inner_text()).strip() == reference
        ):
            matches.append(row)
    if len(matches) != 1:
        raise LayoutChanged(
            "The verified booking could not be uniquely located for its receipt"
        )
    view = matches[0].locator('a[id$="_BtnView"]')
    if await view.count() != 1:
        raise LayoutChanged("The booking VIEW action could not be identified")
    await view.click()
    return await capture_open_booking_details(client, reference)


async def capture_open_booking_details(client, reference):
    """Capture the current details only after its exact reference is verified."""
    async with asyncio.timeout(45):
        while True:
            detail = client.page.frame(name="frameContent")
            try:
                if detail:
                    number = detail.locator("#bookingFormControl1_ObjectNumber")
                    if await number.is_visible():
                        await client._wait_for_idle(detail)
                        identity = number.get_by_text(reference, exact=True)
                        if (
                            await identity.count() != 1
                            or not await identity.is_visible()
                        ):
                            raise LayoutChanged(
                                "Booking receipt reference does not match the verified booking"
                            )
                        await wait_for_receipt_ready(client, detail)
                        await client.progress("capturing the booking details receipt")
                        return await detail.locator("body").screenshot(
                            type="png", animations="disabled"
                        )
            except PlaywrightError:
                if detail and not detail.is_detached():
                    raise
            await client.page.wait_for_timeout(250)


async def wait_for_receipt_ready(client, detail):
    await detail.wait_for_load_state("load", timeout=30000)
    # FBS can overlay the child iframe from its parent document even after the
    # booking details themselves are loaded. A screenshot also captures that
    # overlay, so wait across the whole frame tree rather than only the form.
    for frame in client.page.frames:
        await client._wait_for_idle(frame)
        notices = frame.get_by_text(
            re.compile(r"^(?:Please Wait|Loading)\s*\.{0,3}$", re.I)
        )
        for notice in await notices.all():
            await notice.wait_for(state="hidden", timeout=15000)


TASK_SNAPSHOT = """() => {
    const grid = document.querySelector('#GridViewTask');
    const content = document.querySelector('#GridViewTask_content');
    if (!grid || !content) return null;
    return {
        headers: [...grid.querySelectorAll('th')].map(e => e.innerText.trim()),
        rows: [...content.querySelectorAll('tr.row')].map(row =>
            [...row.cells].map(cell => cell.innerText.trim())),
        summary: grid.innerText,
    };
}"""


def parse_tasks(snapshot):
    """Validate the task table and distinguish an empty list from unreadable data."""
    required = {
        "Ref. Number": "reference_number",
        "Date & Time of Use": "date_time",
        "Facility": "room",
        "Type of Request": "request_type",
        "Requested By": "requested_by",
        "Status": "status",
    }
    if not isinstance(snapshot, dict):
        raise LayoutChanged("FBS Task List is unreadable")
    headers = [
        " ".join(label.split()).casefold() for label in snapshot.get("headers", [])
    ]
    if any(headers.count(label.casefold()) != 1 for label in required):
        raise LayoutChanged("FBS Task List columns have changed")
    total_match = re.search(
        r"(\d+)\s+ITEM\(S\)\s+TOTAL", snapshot.get("summary", ""), re.I
    )
    if not total_match:
        raise LayoutChanged("FBS Task List total could not be verified")
    total = int(total_match[1])
    tasks = []
    for row in snapshot.get("rows", []):
        if total == 0 and len(row) == 1 and row[0].strip().casefold() == "no items":
            # FBS renders its empty-state message as a single colspan data row.
            continue
        if len(row) != len(headers):
            raise LayoutChanged("FBS Task List row is incomplete")
        item = {
            key: " ".join(row[headers.index(label.casefold())].split())
            for label, key in required.items()
        }
        if not item["reference_number"] or not item["status"]:
            raise LayoutChanged("FBS task identity or status is missing")
        tasks.append(item)
    if len(tasks) > total or (total and not tasks):
        raise LayoutChanged("FBS Task List rows do not agree with its total")
    if not total and not re.search(r"\bNo items\b", snapshot.get("summary", ""), re.I):
        raise LayoutChanged("An empty FBS Task List could not be verified")
    return {"tasks": tasks, "total": total, "complete": len(tasks) == total}


@observed_step("tasks.read")
async def read_task_list(client):
    await client.ensure_authenticated()
    await client.progress("checking FBS Task List")
    frame = await open_account_tab(client, "TaskListTab", "#GridViewTask_content")
    # An inherited keyword filter must not hide pending items.
    field = frame.locator("input#txtSearch_c1")
    if (await field.input_value()).strip():
        await frame.locator("a#buttonReset").click()
        await client._wait_for_idle(frame)
        if (await field.input_value()).strip():
            raise LayoutChanged("FBS Task List search filter could not be cleared")
    result = parse_tasks(await frame.evaluate(TASK_SNAPSHOT))
    from .fbs_tasks import add_task_capabilities

    await add_task_capabilities(frame, result)
    event(
        "tasks.results",
        total=result["total"],
        read=len(result["tasks"]),
        complete=result["complete"],
    )
    return result


class CancellationOutcomeUnknown(FBSError):
    """Cancellation was attempted but FBS did not provide a verifiable result."""


async def booking_rows(frame):
    records = []
    for row in await frame.locator("#GridViewBooking_content tr.row").all():
        cells = row.locator("td")
        if (
            await cells.count() == 1
            and (await cells.first.inner_text()).strip().casefold() == "no items"
        ):
            continue
        if await cells.count() < 9:
            raise LayoutChanged("My Bookings contains an unreadable row")
        values = [
            " ".join((await cell.inner_text()).split()) for cell in await cells.all()
        ]
        cancel = row.locator('a[id$="_BtnCancel"]:visible')
        record = {
            "reference_number": values[1],
            "date_time": values[2],
            "room": values[4],
            "fbs_status": values[8],
            "can_cancel": await cancel.count() == 1,
        }
        if not record["reference_number"] or not record["fbs_status"]:
            raise LayoutChanged("My Bookings reference or status is missing")
        records.append((record, row))
    return records


@observed_step("bookings.read")
async def read_bookings(client):
    await client.ensure_authenticated()
    frame = await open_account_tab(client, "MyBookingsTab", "#GridViewBooking_content")
    rows = await booking_rows(frame)
    summary = await frame.locator("#GridViewBooking").inner_text()
    total = re.search(r"(\d+)\s+ITEM\(S\)\s+TOTAL", summary, re.I)
    if not total or int(total[1]) < len(rows) or (int(total[1]) and not rows):
        raise LayoutChanged("My Bookings total could not be verified")
    if not int(total[1]) and not re.search(r"\bNo items\b", summary, re.I):
        raise LayoutChanged("An empty My Bookings list could not be verified")
    event("bookings.results", read=len(rows), total=int(total[1]))
    return {
        "bookings": [record for record, _ in rows],
        "total": int(total[1]),
        "complete": len(rows) == int(total[1]),
    }


async def confirm_cancellation_dialog(frame, reference, reason):
    """Only act on a uniquely identified cancellation confirmation for this ref."""
    container = frame.locator('[role="dialog"]:visible')
    if await container.count() != 1:
        container = frame.locator("#FullPanel:visible")
    if await container.count() != 1:
        return False
    text = await container.inner_text()
    if not re.search(
        r"cancel booking|booking cancellation|confirm cancellation|reason for cancellation",
        text,
        re.I,
    ):
        return False
    if not await container.get_by_text(reference, exact=True).count():
        return False
    reasons = container.locator(
        'textarea[id*="reason" i]:visible,input[id*="reason" i]:visible'
    )
    if await reasons.count() > 1:
        raise LayoutChanged("Cancellation reason field is ambiguous")
    if await reasons.count() == 1:
        await reasons.fill(reason)
    matches = []
    for button in await container.locator(
        'a:visible,button:visible,input[type="submit"]:visible,input[type="button"]:visible'
    ).all():
        label = await button.inner_text()
        if not label.strip():
            label = await button.get_attribute("value") or ""
        if re.fullmatch(
            r"confirm|yes|confirm cancellation|cancel booking", label.strip(), re.I
        ):
            matches.append(button)
    if len(matches) != 1:
        raise LayoutChanged("Cancellation confirmation button is ambiguous")
    with step("cancellation.confirm_dialog"):
        await matches[0].click()
    return True


@observed_step("cancellation.execute")
async def cancel_booking(client, expected, reason):
    await read_bookings(client)
    frame = client.page.frame(name="frameContent")
    matches = [
        (record, row)
        for record, row in await booking_rows(frame)
        if record["reference_number"] == expected["reference_number"]
    ]
    if len(matches) != 1:
        raise FBSError(
            "This booking is no longer uniquely visible. Refresh /bookings before cancelling."
        )
    current, row = matches[0]
    if any(
        current[key] != expected[key] for key in ("room", "date_time", "fbs_status")
    ):
        raise FBSError(
            "Booking details or status changed. Refresh /bookings and review again."
        )
    if not current["can_cancel"]:
        raise FBSError(
            "FBS does not offer cancellation for this booking in your account."
        )
    rejected_dialog = False

    async def handle_dialog(dialog):
        nonlocal rejected_dialog
        if (
            dialog.type == "confirm"
            and re.search(r"cancel", dialog.message, re.I)
            and re.search(r"booking|reservation", dialog.message, re.I)
        ):
            await dialog.accept()
        else:
            rejected_dialog = True
            await dialog.dismiss()

    client.page.on("dialog", handle_dialog)
    try:
        event("cancellation.submitting")
        # From this point a lost response is uncertain. Never automatically click
        # CANCEL again, and never infer cancellation from a disappearing row.
        await row.locator('a[id$="_BtnCancel"]:visible').click()
        confirmed_dialog = False
        for _ in range(60):
            if rejected_dialog:
                raise LayoutChanged("An unexpected FBS dialog was dismissed")
            frame = client.page.frame(name="frameContent")
            if frame:
                try:
                    records = await booking_rows(frame)
                    found = [
                        record
                        for record, _ in records
                        if record["reference_number"] == expected["reference_number"]
                    ]
                    if len(found) == 1:
                        status = found[0]["fbs_status"]
                        if re.match(r"cancelled\b|canceled\b", status, re.I):
                            event("cancellation.verified", fbs_status=status)
                            checkpoint_verified(
                                {
                                    "status": "cancelled",
                                    "fbs_status": status,
                                    "reference_number": expected["reference_number"],
                                }
                            )
                            return {"status": "cancelled", "fbs_status": status}
                        if re.search(r"pending.*cancel|cancel.*pending", status, re.I):
                            event("cancellation.pending", fbs_status=status)
                            checkpoint_verified(
                                {
                                    "status": "pending",
                                    "fbs_status": status,
                                    "reference_number": expected["reference_number"],
                                }
                            )
                            return {"status": "pending", "fbs_status": status}
                    if not confirmed_dialog:
                        confirmed_dialog = await confirm_cancellation_dialog(
                            frame, expected["reference_number"], reason
                        )
                except PlaywrightError:
                    if not frame.is_detached():
                        raise
            await client.page.wait_for_timeout(500)
        raise LayoutChanged("Cancelled status was not observed in My Bookings")
    except Exception as error:
        event("cancellation.outcome_unknown", level=40, error_type=type(error).__name__)
        raise CancellationOutcomeUnknown(
            "Cancellation was attempted but could not be verified. Check FBS My Bookings before retrying."
        ) from None
    finally:
        client.page.remove_listener("dialog", handle_dialog)


@observed_operation("backend.bookings")
async def fetch_bookings(*, auth_notify=None):
    try:
        async with FBSClient(auth_notify=auth_notify) as client:
            return await read_bookings(client)
    except FBSError:
        raise
    except Exception:
        raise FBSError(
            "Could not read My Bookings. Check FBS or try /bookings again."
        ) from None


@observed_operation("backend.cancellation")
async def execute_cancellation(expected, reason, *, auth_notify=None):
    try:
        async with FBSClient(auth_notify=auth_notify) as client:
            result = await cancel_booking(client, expected, reason)
            if result["status"] == "cancelled":
                try:
                    result["receipt_png"] = await capture_booking_evidence(
                        client, expected["reference_number"]
                    )
                except Exception as error:
                    event(
                        "cancellation.receipt_unavailable",
                        error_type=type(error).__name__,
                    )
                    result["receipt_error"] = (
                        "Cancellation is verified, but its screenshot could not be captured. Check this reference in FBS My Bookings."
                    )
            return result
    except FBSError:
        raise
    except Exception:
        raise FBSError(
            "Could not prepare cancellation. Refresh /bookings and try again."
        ) from None
