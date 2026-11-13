"""Exact-task approval through FBS's observed Approve Selected control.

No disappearing-row-only success, bulk approvals, automatic retries, or rejection.
"""

import re
from .action_journal import checkpoint_verified

from playwright.async_api import Error as PlaywrightError

from .fbs_client import FBSError, FBSClient, LayoutChanged
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


class TaskUnavailable(FBSError):
    """A reviewed task changed or FBS does not expose an unambiguous action."""


class TaskApprovalOutcomeUnknown(FBSError):
    """Approval was attempted but not positively verified. Never auto-retry."""


def approval_control(frame):
    return frame.get_by_role("link", name=re.compile(r"^\s*Approve Selected\s*$", re.I))


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
    action = approval_control(frame)
    available = (
        await action.count() == 1
        and await action.is_visible()
        and await action.is_enabled()
    )
    for task, row in await task_rows(frame, result):
        task["can_approve"] = (
            available
            and pending_status(task["status"])
            and await row.locator(CHECKBOX).count() == 1
        )
        task["can_view"] = await row.locator(VIEW).count() == 1


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
        if await frame.locator(
            "#objectBase_ActivityHistoryDialogBox_p1:visible"
        ).count():
            raise LayoutChanged(
                "Workflow history must close before capturing a booking receipt"
            )
        result["receipt_png"] = await frame.locator("body").screenshot(
            type="png", animations="disabled"
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


async def select_only_task(frame, row):
    # Clear the header's Select All and every checked data row before choosing one.
    for checkbox in await frame.locator(
        '#GridViewTask input[type="checkbox"]:checked'
    ).all():
        await checkbox.uncheck()
    target = row.locator(CHECKBOX)
    if await target.count() != 1:
        raise TaskUnavailable("This task has no unique selectable checkbox.")
    await target.check()
    if (
        await frame.locator('#GridViewTask input[type="checkbox"]:checked').count() != 1
        or not await target.is_checked()
    ):
        raise TaskUnavailable(
            "FBS did not retain exactly one selected task. No approval was submitted."
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
    if await frame.locator('#GridViewTask input[type="checkbox"]:checked').count() != 1:
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
                except PlaywrightError:
                    if not frame.is_detached():
                        raise
            await client.page.wait_for_timeout(500)
        raise LayoutChanged("A positive task approval outcome was not observed")
    except Exception as error:
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


@observed_operation("backend.task_action")
async def execute_task_action(
    expected, *, approve=False, auth_notify=None, progress=None
):
    try:
        async with FBSClient(auth_notify=auth_notify, progress=progress) as client:
            if approve:
                return await approve_task(client, expected)
            return await capture_task_details(client, expected)
    except FBSError:
        raise
    except Exception:
        raise FBSError(
            "Could not prepare the FBS task action. Refresh Task List and inspect the redacted logs."
        ) from None
