"""Exact-task approval through FBS's observed Approve Selected control.

No disappearing-row-only success, bulk approvals, automatic retries, or rejection.
"""

import asyncio
import re
from urllib.parse import urlsplit

from .action_journal import checkpoint_verified

from playwright.async_api import Error as PlaywrightError

from .fbs_client import AuthenticationRequired, FBSError, FBSClient, LayoutChanged
from .fbs_account import (
    TASK_SNAPSHOT,
    capture_booking_evidence,
    wait_for_receipt_ready,
    parse_tasks,
    read_task_list,
)
from .observability import event, observed_operation, observed_step

IDENTITY = ("reference_number", "date_time", "room", "request_type", "requested_by")
CHECKBOX = 'input[type="checkbox"]:visible:not(:disabled)'
VIEW = 'a[id$="_BtnView"]:visible'

# A co-booker confirms on the task's own page (VIEW), with FBS's workflow
# buttons "Confirm" and "Reject", seen on the page FBS served on 21 Nov 2026.
# The Task List's "Approve Selected" stays hidden until a row is ticked.
DETAILS_PATH = "/modules/homenavigation/bookingenquirytask.aspx"
DETAIL_REFERENCE = '[id$="bookingFormControl1_ObjectNumber"]:visible'
WORKFLOW_BUTTON = (
    'a[id^="panel_datalistWorkflowButtons_"][id$="_buttonWorkflowAction"]:visible'
)
CONFIRM_PAGE_SECONDS = 45
CONFIRM_SUBMIT_MS = 45000
CONFIRM_CHECKS = 4


def confirmable(task):
    """A co-booking awaiting this account's confirmation, with a page to confirm on."""
    return (
        task.get("can_view") is True
        and " ".join(str(task.get("status", "")).split()).casefold()
        == "pending confirmation"
    )


class TaskUnavailable(FBSError):
    """A reviewed task changed or FBS does not expose an unambiguous action."""


class TaskApprovalOutcomeUnknown(FBSError):
    """Approval was attempted but not positively verified. Never auto-retry."""


APPROVE_NAME = re.compile(r"^\s*Approve Selected\s*$", re.I)


def approval_control(frame):
    return frame.get_by_role("link", name=APPROVE_NAME)


async def approval_available(frame):
    """FBS offers Approve Selected for this list.

    FBS's grid script shows the Task List's row-action toolbar
    (#GridViewTask_actsonitems) only while a row is ticked (gridv.acton) and
    hides it on load. So the button counts when it is visible and enabled, or
    when it is in that toolbar, hidden by nothing but the toolbar, and not
    disabled. approve_task ticks the row and then requires it visible before
    pressing it.
    """
    action = frame.get_by_role("link", name=APPROVE_NAME, include_hidden=True)
    if await action.count() != 1:
        return False
    if await action.is_visible():
        return await action.is_enabled()
    return await action.evaluate(
        """el => {
            const bar = el.closest('#GridViewTask_actsonitems');
            if (!bar || !bar.closest('#GridViewTask')) return false;
            if (el.closest('[disabled]') || el.getAttribute('aria-disabled') === 'true'
                || el.classList.contains('aspNetDisabled')) return false;
            for (let node = el; node && node !== bar; node = node.parentElement) {
                const style = getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
            }
            return !bar.parentElement || bar.parentElement.checkVisibility();
        }"""
    )


def pending_status(status):
    return bool(re.search(r"\bpending\b|\bawaiting\b", status, re.I))


async def task_rows(frame, result=None):
    result = result or parse_tasks(await frame.evaluate(TASK_SNAPSHOT))
    if not result["tasks"]:
        return []
    rows = await frame.locator("#GridViewTask_content tr.row").all()
    if len(rows) != len(result["tasks"]):
        raise LayoutChanged("Task List changed while reading its rows")
    return list(zip(result["tasks"], rows))


async def add_task_capabilities(frame, result):
    available = await approval_available(frame)
    for task, row in await task_rows(frame, result):
        task["can_approve"] = (
            available
            and pending_status(task["status"])
            and await row.locator(CHECKBOX).count() == 1
        )
        task["can_view"] = await row.locator(VIEW).count() == 1
        task["can_confirm"] = confirmable(task)


async def locate_reviewed_task(client, expected):
    result = await read_task_list(client, reference=expected["reference_number"])
    if not any(
        task["reference_number"] == expected["reference_number"]
        for task in result["tasks"]
    ):
        raise TaskUnavailable(
            "The reviewed task was not found in the checked FBS pages. Refresh Task List and inspect its coverage before reviewing again."
        )
    frame = client.page.frame(name="frameContent")
    matches = [
        (task, row)
        for task, row in await task_rows(frame, result)
        if all(task.get(key) == expected.get(key) for key in IDENTITY)
    ]
    if len(matches) != 1 or matches[0][0]["status"] != expected.get("status"):
        raise TaskUnavailable(
            "The task changed or is no longer uniquely visible. Refresh Task List and review it again."
        )
    return frame, matches[0][0], matches[0][1]


@observed_step("tasks.details")
async def capture_task_details(client, expected):
    from .booking_details import (
        HISTORY_GRID,
        read_open_booking_details,
        task_booking_record,
        wait_for_details,
    )

    _, current, row = await locate_reviewed_task(client, expected)
    if not current.get("can_view"):
        raise TaskUnavailable("FBS does not expose a unique VIEW action for this task.")
    await client.progress("opening the exact task's booking details")
    await row.locator(VIEW).click()
    details = await read_open_booking_details(client, task_booking_record(current))
    details["source"] = "task_list"
    result = {
        "status": "details",
        "task": current,
        "booking_details": details,
        "reference_number": current["reference_number"],
        "receipt_kind": "booking_details",
    }
    try:
        frame, _ = await wait_for_details(client, current["reference_number"])
        await wait_for_receipt_ready(client, frame)
        if (
            await frame.locator(
                "#objectBase_ActivityHistoryDialogBox_p1:visible"
            ).count()
            or await frame.locator("#" + HISTORY_GRID + ":visible").count()
        ):
            raise LayoutChanged(
                "Workflow history must close before capturing a booking receipt"
            )
        result["receipt_png"] = await frame.locator("body").screenshot(
            type="png", animations="disabled", mask=[frame.locator("#" + HISTORY_GRID)]
        )
    except (PlaywrightError, LayoutChanged, TimeoutError):
        result["receipt_error"] = "Booking metadata verified; screenshot unavailable."
    return result


def approval_acknowledged(message, reference):
    """Accept a positive server acknowledgement, never an error containing 'approved'."""
    text = " ".join(message.split())
    if re.search(
        r"\b(?:not|cannot|failed|error|unable|unsuccessful|rejected)\b|\b0\s+(?:tasks?|requests?|bookings?)\b",
        text,
        re.I,
    ):
        return False
    refs = re.findall(r"\bBK-[A-Za-z0-9-]+\b", text)
    if refs and reference not in refs:
        return False
    counts = re.findall(r"\b(\d+)\s+(?:tasks?|requests?|bookings?)\b", text, re.I)
    if any(int(count) != 1 for count in counts):
        return False
    return bool(
        re.search(r"\b(?:approved|confirmed)\b", text, re.I)
        and (
            re.search(r"\bsuccess(?:ful(?:ly)?)?\b", text, re.I)
            or re.search(
                r"\b(?:task|request|booking)(?:\(s\)|s)?\s+(?:(?:has|have)\s+been\s+|is\s+|are\s+)?(?:approved|confirmed)\b",
                text,
                re.I,
            )
        )
    )


HEADER_BOX = '#GridViewTask_header input[type="checkbox"][id$="checkMultiple"]'
ROW_BOX = '#GridViewTask_content input[type="checkbox"]'


async def only_one_task_selected(frame):
    """Exactly one data row is ticked, and nothing else but possibly the header.

    FBS's grid script ticks the header's select-all itself once every row on
    the page is ticked (gridv.acton), as it does for a person. That is allowed
    only when the list holds a single task (one row, "1 ITEM(S) TOTAL"), so
    "all" can only mean this task, never other rows or other pages.
    """
    ticked_rows = await frame.locator(ROW_BOX + ":checked").count()
    header = await frame.locator(HEADER_BOX + ":checked").count()
    ticked = await frame.locator('#GridViewTask input[type="checkbox"]:checked').count()
    if ticked_rows != 1 or ticked != ticked_rows + header:
        return False
    if not header:
        return True
    rows = await frame.locator("#GridViewTask_content tr.row").count()
    totals = re.findall(
        r"(\d+)\s+ITEM\(S\)\s+TOTAL",
        await frame.locator("#GridViewTask").inner_text(),
        re.I,
    )
    return header == 1 and rows == 1 and set(totals) == {"1"}


async def select_only_task(frame, row):
    # Clear the header's Select All and every checked data row before choosing one.
    # Re-query each pass: unticking the header unticks every row (gridv),
    # so a snapshot of locators would point at boxes that are already clear.
    checked = frame.locator('#GridViewTask input[type="checkbox"]:checked')
    for _ in range(50):
        if not await checked.count():
            break
        await checked.first.uncheck()
    if await checked.count():
        raise TaskUnavailable(
            "FBS kept other tasks selected. No approval was submitted."
        )
    target = row.locator(CHECKBOX)
    if await target.count() != 1:
        raise TaskUnavailable("This task has no unique selectable checkbox.")
    await target.check()
    if not await only_one_task_selected(frame) or not await target.is_checked():
        raise TaskUnavailable(
            "FBS did not retain exactly one selected task. No approval was submitted."
        )


# After Approve Selected, FBS opens an in-page box and approves nothing until
# its CONFIRM is pressed. Read from the live page on 21 Nov 2026:
#   #popupDialog > ... > div.dialog
#     span#popupDialog_l1.title          "Approve Selected"
#     a#popupDialog_bc                   "[x close]"   (postback)
#     textarea#textComments_c1           "Comments:"   (optional)
#     a#popupDialog_b1                   "Confirm"     (postback, no prompt)
# Closing it removes #popupDialog and leaves the task pending.
APPROVE_BOX = "#popupDialog div.dialog:visible"
APPROVE_BOX_TITLE = "#popupDialog #popupDialog_l1"
APPROVE_BOX_CONFIRM = "#popupDialog a#popupDialog_b1:visible"
APPROVE_BOX_CLOSE = "#popupDialog a#popupDialog_bc:visible"
APPROVE_BOX_BUTTONS = (
    "#popupDialog .dlgbuttons a:visible, #popupDialog .dlgbuttons button:visible, "
    '#popupDialog .dlgbuttons input[type="button"]:visible, '
    '#popupDialog .dlgbuttons input[type="submit"]:visible'
)
ACCEPTED_STATUSES = {
    "confirmed",
    "approved",
    "pending confirmation",  # other co-bookers have yet to confirm
    "pending approval",
}
QUIET_CHECKS = 4  # loop passes to wait for FBS's acknowledgement first


async def approval_box(frame):
    """The CONFIRM button of FBS's open "Approve Selected" box, or None.

    That box is FBS asking before it approves, so a refusal while it is open
    (TaskUnavailable) submits nothing. Any other box is not understood, and
    what FBS did is then unknown (LayoutChanged).
    """
    if not await frame.locator(APPROVE_BOX).count():
        return None
    from .fbs_prompts import PromptDeclined, expected_prompt

    titles = [
        " ".join(text.split()).casefold()
        for text in await frame.locator(APPROVE_BOX_TITLE).all_inner_texts()
    ]
    confirm = frame.locator(APPROVE_BOX_CONFIRM)
    labels = [
        " ".join(text.split()).casefold() for text in await confirm.all_inner_texts()
    ]
    # The observed box has one button in its bar. Any other button there
    # (FBS numbers them popupDialog_b1, _b2, ...) makes it a different box.
    buttons = await frame.locator(APPROVE_BOX_BUTTONS).count()
    if (
        await frame.locator(APPROVE_BOX).count() != 1
        or titles != ["approve selected"]
        or labels != ["confirm"]
        or buttons != 1
    ):
        raise LayoutChanged("FBS opened a box Sagasu does not recognise")
    try:
        prompt = expected_prompt(await confirm.get_attribute("onclick"))
    except PromptDeclined:
        prompt = "unexpected"
    if prompt is not None:
        raise TaskUnavailable(
            "FBS now asks a question before approving. No approval was submitted."
        )
    return confirm


async def reviewed_task_is_the_one_ticked(frame, current):
    """With FBS's box open: the list still shows the reviewed task, unchanged,
    and that row, and only that row, is ticked. The page FBS returns with the
    box is the first view of what the server will approve."""
    same = [
        (task, row)
        for task, row in await task_rows(frame)
        if all(task.get(key) == current[key] for key in IDENTITY)
        and task.get("status") == current["status"]
    ]
    return (
        len(same) == 1
        and await same[0][1].locator('input[type="checkbox"]').count() == 1
        and await same[0][1].locator('input[type="checkbox"]').is_checked()
        and await only_one_task_selected(frame)
    )


async def close_approval_box(frame):
    """Best effort: leave FBS without an open box after a refusal."""
    try:
        close = frame.locator(APPROVE_BOX_CLOSE)
        if await close.count() == 1:
            await close.click()
    except Exception as error:
        event("tasks.approval_box_left_open", error_type=type(error).__name__)


async def left_tasks_and_listed(client, current):
    """Proof after CONFIRM when FBS shows no acknowledgement Sagasu knows.

    Only for a co-booking that awaited this account's confirmation: seen live,
    such a booking is not in My Bookings until it is accepted. The exact
    reference must have left a complete Task List and be listed once in My
    Bookings with a status seen after acceptance. Returns that status, else
    None. For any other task a vanished row proves nothing.
    """
    from .fbs_account import read_bookings

    if " ".join(str(current.get("status", "")).split()).casefold() != (
        "pending confirmation"
    ):
        return None
    reference = current["reference_number"]
    listing = await read_task_list(client, reference=reference)
    if listing.get("complete") is not True or any(
        task.get("reference_number") == reference for task in listing["tasks"]
    ):
        return None
    bookings = await read_bookings(client, reference=reference)
    rows = [
        row for row in bookings["bookings"] if row.get("reference_number") == reference
    ]
    if len(rows) != 1:
        return None
    status = " ".join(str(rows[0].get("fbs_status", "")).split())
    return status if status.casefold() in ACCEPTED_STATUSES else None


def page_is_loading(error):
    """A read cut short because the frame is loading a new page: read again."""
    return bool(
        re.search(
            r"execution context was destroyed|navigat|frame was detached",
            str(error),
            re.I,
        )
    )


@observed_step("tasks.approve")
async def approve_task(client, expected):
    frame, current, row = await locate_reviewed_task(client, expected)
    if not current.get("can_approve"):
        raise TaskUnavailable(
            "FBS does not offer approval for this task in your account."
        )
    await client.progress("selecting only the reviewed task for approval")
    notice = frame.locator("#MessagePanel:visible")
    if await notice.count():
        dismiss = notice.get_by_role("link", name=re.compile(r"^Dismiss$", re.I))
        if await dismiss.count() != 1:
            raise LayoutChanged("Cannot clear the previous FBS acknowledgement safely")
        await dismiss.click()
        await notice.wait_for(state="hidden", timeout=5000)
    await select_only_task(frame, row)
    # Read the row again after selection; it must still describe the reviewed action.
    check = [
        (task, candidate)
        for task, candidate in await task_rows(frame)
        if all(task.get(key) == current[key] for key in IDENTITY)
        and task.get("status") == current["status"]
    ]
    if len(check) != 1 or not await check[0][1].locator(CHECKBOX).is_checked():
        raise TaskUnavailable(
            "Task details changed during selection. No approval was submitted."
        )
    action = approval_control(frame)
    if (
        await action.count() != 1
        or not await action.is_visible()
        or not await action.is_enabled()
    ):
        raise TaskUnavailable("The FBS approval action changed. Refresh Task List.")
    if not await only_one_task_selected(frame):
        raise TaskUnavailable(
            "The task selection changed before submission. No approval was submitted."
        )
    rejected_dialog = False

    async def handle_dialog(dialog):
        nonlocal rejected_dialog
        if (
            dialog.type == "confirm"
            and re.search(r"\bapprov(?:e|al)\b|\bconfirm\b", dialog.message, re.I)
            and re.search(r"\btask|\bbooking|\brequest", dialog.message, re.I)
            and not re.search(r"\breject|\bcancel|\bdelete", dialog.message, re.I)
        ):
            await dialog.accept()
        else:
            rejected_dialog = True
            await dialog.dismiss()

    client.page.on("dialog", handle_dialog)
    confirmed, quiet, look_elsewhere = False, 0, False
    try:
        event("tasks.approval_submitting")
        await client.progress("submitting the confirmed task approval")
        await action.click()  # Exactly once. Any later failure is an uncertain outcome.
        for _ in range(60):
            if rejected_dialog:
                raise LayoutChanged("An unexpected FBS confirmation was dismissed")
            frame = client.page.frame(name="frameContent")
            if frame:
                try:
                    await client._wait_for_idle(frame)
                    if not confirmed:
                        # Real FBS asks again in its own box; nothing is
                        # approved until that box's CONFIRM is pressed.
                        try:
                            box_confirm = await approval_box(frame)
                            if box_confirm is not None and not (
                                await reviewed_task_is_the_one_ticked(frame, current)
                            ):
                                raise TaskUnavailable(
                                    "The task or its selection changed before confirmation. No approval was submitted."
                                )
                        except TaskUnavailable:
                            await close_approval_box(frame)
                            raise
                        if box_confirm is not None:
                            event("tasks.approval_confirming")
                            confirmed = True
                            await box_confirm.click()  # Exactly once.
                            await client.page.wait_for_timeout(500)
                            continue
                    if not await frame.locator("#GridViewTask_content").is_visible():
                        # FBS serves the page hidden until its script runs.
                        await client.page.wait_for_timeout(500)
                        continue
                    result = parse_tasks(await frame.evaluate(TASK_SNAPSHOT))
                    found = [
                        task
                        for task in result["tasks"]
                        if all(task[key] == current[key] for key in IDENTITY)
                    ]
                    positive_status = len(found) == 1 and bool(
                        re.fullmatch(
                            r"approved|confirmed|completed", found[0]["status"], re.I
                        )
                    )
                    label = frame.locator("#MessagePanel:visible #MessageLabel:visible")
                    acknowledged = await label.count() == 1 and approval_acknowledged(
                        await label.inner_text(), current["reference_number"]
                    )
                    if positive_status or (not found and acknowledged):
                        outcome = {
                            "status": "approved",
                            "reference_number": current["reference_number"],
                            "task": dict(current),
                            "task_status": (
                                found[0]["status"]
                                if positive_status
                                else "Approval acknowledged by FBS"
                            ),
                            "booking_status_verified": False,
                        }
                        event(
                            "tasks.approval_verified",
                            verification=(
                                "task_status"
                                if positive_status
                                else "acknowledgement_and_removed_task"
                            ),
                        )
                        checkpoint_verified(outcome)
                        await capture_approval_receipt(client, frame, outcome)
                        return outcome
                    if (
                        confirmed
                        and not found
                        and not await frame.locator(APPROVE_BOX).count()
                    ):
                        # CONFIRM was pressed and the task left this page, but
                        # FBS showed no acknowledgement Sagasu knows. Give it a
                        # moment, then look for proof in fresh listings.
                        quiet += 1
                        if quiet >= QUIET_CHECKS:
                            look_elsewhere = True
                            break
                except PlaywrightError as error:
                    # Reads are safe to repeat; CONFIRM is never pressed twice.
                    if not frame.is_detached() and not page_is_loading(error):
                        raise
            await client.page.wait_for_timeout(500)
        if not look_elsewhere:
            raise LayoutChanged("A positive task approval outcome was not observed")
        # Once, outside the loop: any error here is an uncertain outcome.
        listed = await left_tasks_and_listed(client, current)
        if listed is None:
            raise LayoutChanged("The approved task was not found in My Bookings")
        outcome = {
            "status": "approved",
            "reference_number": current["reference_number"],
            "task": dict(current),
            "task_status": "Left the Task List; My Bookings shows " + listed,
            "booking_status_verified": False,
        }
        event(
            "tasks.approval_verified",
            verification="left_task_list_and_listed_in_my_bookings",
        )
        checkpoint_verified(outcome)
        await capture_approval_receipt(
            client, client.page.frame(name="frameContent") or frame, outcome
        )
        return outcome
    except Exception as error:
        if isinstance(error, TaskUnavailable) and not confirmed:
            # Raised only with FBS's "Approve Selected" box open and its
            # CONFIRM not pressed: nothing was approved, so a retry is safe.
            event("tasks.approval_not_submitted")
            raise
        event(
            "tasks.approval_outcome_unknown", level=40, error_type=type(error).__name__
        )
        raise TaskApprovalOutcomeUnknown(
            "Approval was attempted but could not be verified. Check FBS Task List and the booking before trying again."
        ) from None
    finally:
        client.page.remove_listener("dialog", handle_dialog)


async def capture_approval_receipt(client, frame, outcome):
    """Keep the acknowledged outcome even when the details/receipt are inaccessible."""
    fallback = None
    try:
        await client.progress("capturing FBS's approval acknowledgement")
        fallback = await frame.locator("body").screenshot(
            type="png", animations="disabled"
        )
    except Exception as error:
        event(
            "tasks.acknowledgement_screenshot_failed", error_type=type(error).__name__
        )
    try:
        outcome["receipt_png"] = await capture_booking_evidence(
            client, outcome["reference_number"]
        )
        outcome["receipt_kind"] = "booking_details"
    except Exception as error:
        event("tasks.approval_details_unavailable", error_type=type(error).__name__)
        if fallback:
            outcome["receipt_png"], outcome["receipt_kind"] = (
                fallback,
                "approval_acknowledgement",
            )
        else:
            outcome["receipt_error"] = (
                "Approval verified, but its screenshot could not be captured. Check FBS before retrying any action."
            )


async def open_confirmation_page(client, current, row):
    """VIEW the exact task; return (frame, its one visible Confirm button)."""
    await client.progress("opening the exact task's page to confirm it")
    await row.locator(VIEW).click()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONFIRM_PAGE_SECONDS
    problem = "FBS did not open the task's page"
    while loop.time() < deadline:
        frame = client.page.frame(name="frameContent")
        try:
            if frame and urlsplit(frame.url).path.casefold() == DETAILS_PATH:
                refs = frame.locator(DETAIL_REFERENCE)
                if await refs.count():
                    await client._wait_for_idle(frame)
                    shown = [
                        " ".join(text.split())
                        for text in await refs.locator(".control").all_inner_texts()
                    ]
                    if shown != [current["reference_number"]]:
                        raise TaskUnavailable(
                            "FBS opened a different booking. No confirmation was submitted."
                        )
                    buttons = frame.locator(WORKFLOW_BUTTON)
                    confirms = []
                    for index in range(await buttons.count()):
                        button = buttons.nth(index)
                        label = " ".join((await button.inner_text()).split())
                        shadow = await button.evaluate(
                            "el => !!el.closest('#panel_buttonShadow')"
                        )
                        if label.casefold() == "confirm" and not shadow:
                            confirms.append(button)
                    if len(confirms) == 1:
                        return frame, confirms[0]
                    # Still loading, or FBS offers no single Confirm: keep
                    # looking until the deadline, then refuse unsubmitted.
                    problem = "FBS does not offer one Confirm action for this task"
        except PlaywrightError:
            if frame is None or not frame.is_detached():
                raise
        await client.page.wait_for_timeout(250)
    raise TaskUnavailable(problem + ". No confirmation was submitted.")


POSITIVE_STATUS = re.compile(r"approved|confirmed|completed", re.I)


async def verify_confirmed(client, current):
    """Fresh Task List reads: the exact task Confirmed/Approved/Completed, or
    no row with its reference left on a complete listing."""
    reference = current["reference_number"]
    for attempt in range(CONFIRM_CHECKS):
        if attempt:
            await client.page.wait_for_timeout(2000)
        listing = await read_task_list(client, reference=reference)
        same = [t for t in listing["tasks"] if t.get("reference_number") == reference]
        found = [t for t in same if all(t.get(k) == current[k] for k in IDENTITY)]
        if len(found) > 1:
            break
        if found:
            status = " ".join(str(found[0]["status"]).split())
            if POSITIVE_STATUS.fullmatch(status):
                return status
            if not pending_status(status):
                break  # e.g. Rejected or Cancelled: never a confirmation
            continue
        if not same and listing.get("complete") is True:
            return "Confirmed (left the Task List)"
    raise LayoutChanged("A confirmed task was not observed in Task List")


@observed_step("tasks.confirm")
async def confirm_task(client, expected):
    """Confirm one co-booking on its own page. Only a pressed Confirm is uncertain."""
    from .fbs_prompts import PromptDeclined, expected_prompt

    try:
        _, current, row = await locate_reviewed_task(client, expected)
        if not confirmable(current):
            raise TaskUnavailable(
                "FBS does not offer confirmation for this task in your account."
            )
        frame, button = await open_confirmation_page(client, current, row)
        try:
            if expected_prompt(await button.get_attribute("onclick")) is not None:
                raise PromptDeclined("FBS now asks a question before confirming")
        except PromptDeclined as declined:
            raise TaskUnavailable(
                f"{declined}. No confirmation was submitted."
            ) from None
    except (TaskUnavailable, AuthenticationRequired):
        raise
    except Exception as error:
        # Confirm was never pressed, so FBS received nothing.
        event("tasks.confirmation_not_submitted", error_type=type(error).__name__)
        raise TaskUnavailable(
            "The confirmation could not be prepared. No confirmation was submitted."
        ) from error
    try:
        async with client.page.expect_response(
            lambda response: response.request.method == "POST"
            and response.frame is frame
            and urlsplit(response.url).path.casefold() == DETAILS_PATH,
            timeout=CONFIRM_SUBMIT_MS,
        ) as pending:
            event("tasks.confirmation_submitting")
            await client.progress("submitting the confirmed co-booking")
            await button.click()  # Exactly once. Any later failure is uncertain.
        await (await pending.value).finished()
        # What FBS showed right after Confirm, before the Task List is read again.
        after = client.page.frame(name="frameContent") or frame
        after_png, acknowledged = None, False
        try:
            await client._wait_for_idle(after)
            label = after.locator("#MessagePanel:visible #MessageLabel:visible")
            acknowledged = await label.count() == 1 and approval_acknowledged(
                await label.inner_text(), current["reference_number"]
            )
            after_png = await after.locator("body").screenshot(
                type="png", animations="disabled"
            )
        except Exception as error:
            event("tasks.confirmation_page_unread", error_type=type(error).__name__)
        status = await verify_confirmed(client, current)
        outcome = {
            "status": "approved",
            "reference_number": current["reference_number"],
            "task": dict(current),
            "task_status": status,
            "booking_status_verified": False,
        }
        event("tasks.confirmation_verified", acknowledged=bool(acknowledged))
        checkpoint_verified(outcome)
        try:
            outcome["receipt_png"] = await capture_booking_evidence(
                client, outcome["reference_number"]
            )
            outcome["receipt_kind"] = "booking_details"
        except Exception as error:
            event(
                "tasks.confirmation_details_unavailable",
                error_type=type(error).__name__,
            )
            if after_png:
                outcome["receipt_png"] = after_png
                outcome["receipt_kind"] = "confirmation_page"
            else:
                outcome["receipt_error"] = (
                    "Confirmation verified, but its screenshot could not be captured. Check FBS before retrying any action."
                )
        return outcome
    except Exception as error:
        event(
            "tasks.confirmation_outcome_unknown",
            level=40,
            error_type=type(error).__name__,
        )
        raise TaskApprovalOutcomeUnknown(
            "Confirmation was submitted but could not be verified. Check FBS Task List and the booking before trying again."
        ) from None


@observed_operation("backend.task_action")
async def execute_task_action(
    expected, *, approve=False, auth_notify=None, progress=None, interactive=True
):
    try:
        async with FBSClient(
            auth_notify=auth_notify, progress=progress, interactive=interactive
        ) as client:
            if approve:
                # Approvers use Approve Selected; a co-booker confirms on the
                # task's own page.
                if expected.get("can_approve") or not expected.get("can_confirm"):
                    return await approve_task(client, expected)
                return await confirm_task(client, expected)
            return await capture_task_details(client, expected)
    except FBSError:
        raise
    except Exception:
        raise FBSError(
            "Could not prepare the FBS task action. Refresh Task List and inspect the redacted logs."
        ) from None
