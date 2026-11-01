"""Read-only account pages: booking receipts and the user's FBS Task List."""

import asyncio
import re

from playwright.async_api import Error as PlaywrightError

from .fbs_client import FBSError, LayoutChanged


async def open_account_tab(client, tab, marker):
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
    return parse_tasks(await frame.evaluate(TASK_SNAPSHOT))
