"""FBS account reads and explicitly confirmed single-booking cancellation."""

import asyncio
import re
from urllib.parse import urlsplit
from .action_journal import checkpoint_verified

from playwright.async_api import Error as PlaywrightError

from .fbs_client import (
    AuthenticationRequired,
    BookingUnavailable,
    FBSError,
    FBSApplicationError,
    LayoutChanged,
    FBSClient,
    FBS_URL,
)
from .fbs_prompts import Answer, PromptDeclined, click_and_accept, expected_prompt
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
                if (
                    frame
                    and (await frame.title()).strip().casefold() == "appexception.aspx"
                ):
                    raise FBSApplicationError(
                        "FBS returned its application-error page.",
                    )
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
    if not matches:
        await read_bookings(client, reference=reference)
        frame = client.page.frame(name="frameContent")
        matches = [
            row
            for record, row in await booking_rows(frame)
            if record["reference_number"] == reference
        ]
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


async def _open_task_list(client):
    try:
        return (
            await open_account_tab(client, "TaskListTab", "#GridViewTask_content"),
            False,
        )
    except FBSApplicationError as original:
        cookies = [
            c
            for c in await client.context.cookies(FBS_URL)
            if c["name"] == "ASP.NET_SessionId"
        ]
        if (
            len(cookies) != 1
            or cookies[0]["domain"].lstrip(".") != urlsplit(FBS_URL).hostname
            or cookies[0]["path"] != "/"
        ):
            raise
        cookie = cookies[0]
        scope = {key: cookie[key] for key in ("name", "domain", "path")}
        interactive = client.interactive
        may_sign_in = getattr(client, "may_sign_in", interactive)
        recovered = False
        try:
            # a fresh application session recovered the observed task error;
            # authentication cookies and queued action payloads stay intact.
            await client.progress(
                "renewing the FBS application session once for Task List"
            )
            client.interactive = False
            client.may_sign_in = False
            await client.context.clear_cookies(**scope)
            await client.ensure_authenticated()
            frame = await open_account_tab(
                client, "TaskListTab", "#GridViewTask_content"
            )
            await client._save_session()
            recovered = True
            event("tasks.application_session_recovered")
            return frame, True
        except Exception as error:
            event(
                "tasks.application_session_recovery_failed",
                error_type=type(error).__name__,
            )
            raise original from None
        finally:
            client.interactive = interactive
            client.may_sign_in = may_sign_in
            if not recovered:
                await client.context.clear_cookies(**scope)
                await client.context.add_cookies([cookie])
                await client._save_session()


@observed_step("tasks.read")
async def read_task_list(client, *, reference=None):
    await client.ensure_authenticated()
    await client.progress("checking FBS Task List")
    frame, recovered = await _open_task_list(client)
    # An inherited keyword filter must not hide pending items.
    field = frame.locator("input#txtSearch_c1")
    if (await field.input_value()).strip():
        await frame.locator("a#buttonReset").click()
        await client._wait_for_idle(frame)
        if (await field.input_value()).strip():
            raise LayoutChanged("FBS Task List search filter could not be cleared")
    from .fbs_tasks import add_task_capabilities
    from .fbs_paging import collect_pages

    async def read_page():
        result = parse_tasks(await frame.evaluate(TASK_SNAPSHOT))
        await add_task_capabilities(frame, result)
        return result

    result = await collect_pages(
        client, frame, "GridViewTask", "tasks", read_page, reference=reference
    )
    if recovered:
        result["application_session_recovered"] = True
    event(
        "tasks.results",
        total=result["total"],
        read=len(result["tasks"]),
        complete=result["complete"],
    )
    return result


class CancellationOutcomeUnknown(FBSError):
    """Cancellation was attempted but FBS did not provide a verifiable result."""


class CancellationUnavailable(BookingUnavailable):
    """FBS offered no cancellation Sagasu could submit. Nothing was submitted."""

    default_reason = "cancellation_unavailable"


class CancellationPenalty(CancellationUnavailable):
    """FBS would charge a penalty to cancel. Nothing was submitted."""

    default_reason = "cancellation_penalty"


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
async def read_bookings(client, *, reference=None):
    await client.ensure_authenticated()
    frame = await open_account_tab(client, "MyBookingsTab", "#GridViewBooking_content")
    from .fbs_paging import collect_pages
    from .fbs_http import (
        BOOKING_FILTER_DEFAULTS,
        account_filters_clear,
        booking_listing_scope,
    )

    async def filters():
        return await frame.evaluate(
            """ids => Object.fromEntries(ids.flatMap(id => {
                const element = document.getElementById(id);
                return element ? [[id, element.value]] : [];
            }))""",
            list(BOOKING_FILTER_DEFAULTS),
        )

    values = await filters()
    if not account_filters_clear("bookings", values):
        reset = frame.locator("a#ViewBooking_Search_buttonReset")
        if await reset.count() != 1:
            raise LayoutChanged("My Bookings search filters could not be reset safely")
        await reset.click()
        await client._wait_for_idle(frame)
        values = await filters()
        if not account_filters_clear("bookings", values):
            raise LayoutChanged("My Bookings search filters could not be cleared")

    async def read_page():
        return await read_booking_page(frame)

    result = await collect_pages(
        client, frame, "GridViewBooking", "bookings", read_page, reference=reference
    )
    result["listing_scope"] = booking_listing_scope(values)
    return result


async def read_booking_page(frame):
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


# FBS's cancel is a two-step native flow: CANCEL in My Bookings opens the booking
# form in cancel mode (nothing is submitted yet); its "Confirm Cancellation"
# button asks the penalty prompt and then posts the form.
EDIT_PATH = "/modules/bigspace/bookingmanagement/edit.aspx"
CANCEL_FORM_SECONDS = 45
CANCEL_SUBMIT_MS = 45000
CANCEL_CHECKS = 4
REASON_LIMIT = 255


async def open_cancellation_form(client, reference):
    """FBS's cancel form for exactly this reference: (frame, confirm, reason)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CANCEL_FORM_SECONDS
    problem = "FBS did not open its cancellation form"
    while loop.time() < deadline:
        frame = client.page.frame(name="frameContent")
        try:
            if frame and urlsplit(frame.url).path.casefold() == EDIT_PATH:
                number = frame.locator("#bookingFormControl1_ObjectNumber_c1")
                if await number.count() == 1 and await number.is_visible():
                    await client._wait_for_idle(frame)
                    if (await number.inner_text()).strip() != reference:
                        raise CancellationUnavailable(
                            "FBS opened a different booking for cancellation"
                        )
                    confirm = frame.locator("a#panel_UIButton2:visible")
                    reason = frame.locator("#bookingFormControl1_Remarks_c1:visible")
                    caption = frame.locator("#bookingFormControl1_Remarks")
                    if (
                        await confirm.count() == 1
                        and " ".join((await confirm.inner_text()).split()).casefold()
                        == "confirm cancellation"
                        and await reason.count() == 1
                        and await caption.count() == 1
                        and re.search(
                            r"reason for cancel", await caption.inner_text(), re.I
                        )
                    ):
                        return frame, confirm, reason
                    # Still loading, or FBS changed the form: keep looking
                    # until the deadline, then refuse without submitting.
                    problem = "FBS's cancellation form has changed"
        except PlaywrightError:
            if frame is None or not frame.is_detached():
                raise
        await client.page.wait_for_timeout(250)
    raise CancellationUnavailable(problem)


async def verify_cancelled(client, expected):
    """Read My Bookings again until the exact reference shows its new status."""
    reference = expected["reference_number"]
    for attempt in range(CANCEL_CHECKS):
        if attempt:
            await client.page.wait_for_timeout(2000)
        listing = await read_bookings(client, reference=reference)
        found = [
            row for row in listing["bookings"] if row["reference_number"] == reference
        ]
        if len(found) != 1 or any(
            found[0].get(key) != expected.get(key) for key in ("room", "date_time")
        ):
            continue
        status = found[0]["fbs_status"]
        if status.strip().casefold() == expected["fbs_status"].strip().casefold():
            continue  # unchanged: not (yet) cancelled
        for pattern, outcome in (
            (r"cancell?ed", "cancelled"),
            (r"pending\s+cancell?ation|cancell?ation\s+pending", "pending"),
        ):
            if re.fullmatch(pattern, status.strip(), re.I):
                name = "verified" if outcome == "cancelled" else "pending"
                event("cancellation." + name, fbs_status=status)
                checkpoint_verified(
                    {
                        "status": outcome,
                        "fbs_status": status,
                        "reference_number": reference,
                    }
                )
                return {"status": outcome, "fbs_status": status}
    raise LayoutChanged("Cancelled status was not observed in My Bookings")


async def still_as_reviewed(client, expected):
    """True only when My Bookings shows the reference exactly as reviewed."""
    reference = expected["reference_number"]
    try:
        listing = await read_bookings(client, reference=reference)
    except Exception:
        return False
    found = [row for row in listing["bookings"] if row["reference_number"] == reference]
    return (
        len(found) == 1
        and found[0].get("can_cancel") is True
        and all(
            found[0].get(key) == expected.get(key)
            for key in ("room", "date_time", "fbs_status")
        )
    )


async def _cancel(client, expected, reason, state):
    reference = expected["reference_number"]
    await read_bookings(client, reference=reference)
    frame = client.page.frame(name="frameContent")
    matches = [
        (record, row)
        for record, row in await booking_rows(frame)
        if record["reference_number"] == reference
    ]
    if len(matches) != 1:
        raise CancellationUnavailable(
            "This booking is no longer uniquely visible. Refresh /bookings before cancelling."
        )
    current, row = matches[0]
    if any(
        current[key] != expected[key] for key in ("room", "date_time", "fbs_status")
    ):
        raise CancellationUnavailable(
            "Booking details or status changed. Refresh /bookings and review again."
        )
    if not current["can_cancel"]:
        raise CancellationUnavailable(
            "FBS does not offer cancellation for this booking in your account."
        )
    # FBS's maxlength counts UTF-16 units, as the browser does.
    text = (
        " ".join(reason.split())
        .encode("utf-16-le")[: REASON_LIMIT * 2]
        .decode("utf-16-le", errors="ignore")
    )
    if not text:
        raise CancellationUnavailable("A cancellation reason is required")
    # On 20 Nov this click only opened FBS's form; FBS asks again there.
    await row.locator('a[id$="_BtnCancel"]:visible').click()
    try:
        form, confirm, field = await open_cancellation_form(client, reference)
    except asyncio.CancelledError:
        state["submitted"] = True  # cannot check now; stay uncertain
        raise
    except Exception:
        # No usable form: say "nothing submitted" only if the booking is
        # provably untouched, in case FBS ever cancels from the grid itself.
        if not await still_as_reviewed(client, expected):
            state["submitted"] = True
        raise
    try:
        message = expected_prompt(await confirm.get_attribute("onclick"))
    except PromptDeclined as declined:
        kind = CancellationPenalty if declined.penalty else CancellationUnavailable
        raise kind(str(declined)) from None
    if message is None:
        # Both FBS write buttons asked the penalty prompt on 20 Nov. Without
        # it the penalty cannot be checked, so do not submit.
        raise CancellationUnavailable(
            "FBS's cancellation form no longer asks to confirm"
        )
    await field.fill(text)
    if await field.input_value() != text:
        raise CancellationUnavailable("FBS did not keep the cancellation reason")
    answer = state["answer"] = Answer(message)
    async with client.page.expect_response(
        lambda response: response.request.method == "POST"
        and response.frame is form
        and urlsplit(response.url).path.casefold() == EDIT_PATH,
        timeout=CANCEL_SUBMIT_MS,
    ) as pending:
        try:
            await click_and_accept(client.page, confirm, answer)
        except BaseException:
            # Only a dialog Sagasu dismissed proves the button posted nothing.
            if not answer.declined:
                state["submitted"] = True
            raise
        if answer.declined and not answer.accepted:
            raise CancellationUnavailable(
                "FBS asked something unexpected before cancelling"
            )
        # Answered OK, or no dialog at all: a post cannot be ruled out.
        state["submitted"] = True
        event("cancellation.submitting")
    response = await pending.value
    await response.finished()
    event("cancellation.submitted", http_status=response.status)
    after = client.page.frame(name="frameContent")
    if after:
        try:
            await client._wait_for_idle(after)
        except PlaywrightError:
            pass  # verify_cancelled reopens My Bookings either way
    return await verify_cancelled(client, expected)


@observed_step("cancellation.execute")
async def cancel_booking(client, expected, reason):
    """Cancel through FBS's own form. Only a submitted cancellation is uncertain."""
    state = {"submitted": False, "answer": None}
    try:
        return await _cancel(client, expected, reason, state)
    except Exception as error:
        if state["submitted"] or (state["answer"] and state["answer"].accepted):
            event(
                "cancellation.outcome_unknown",
                level=40,
                error_type=type(error).__name__,
            )
            raise CancellationOutcomeUnknown(
                "Cancellation was submitted but could not be verified. Check FBS My Bookings before retrying."
            ) from None
        if isinstance(error, (CancellationUnavailable, AuthenticationRequired)):
            raise
        event("cancellation.not_submitted", level=30, error_type=type(error).__name__)
        raise CancellationUnavailable(
            "The cancellation could not be prepared; nothing was submitted"
        ) from error


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
