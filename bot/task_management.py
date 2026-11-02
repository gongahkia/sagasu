"""Single-task Telegram reviews and screenshots on the editable surface."""

import secrets
import time

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

from .auth import owner_auth_notifier
from .fbs_client import FBSError
from .fbs_tasks import TaskApprovalOutcomeUnknown, execute_task_action
from .observability import event, observed_operation
from .surface import deliver_receipt, finish_operation


def listing(context, chat_id):
    result = context.user_data.get("task_list_result")
    state = context.user_data.get("task_list_view")
    if not result:
        return None
    if not state:
        state = {
            "id": secrets.token_urlsafe(6),
            "chat_id": chat_id,
            "expires_at": time.time() + 600,
        }
        context.user_data["task_list_view"] = state
    return state


@observed_operation("tasks.action")
async def task_callback(update, context):
    from .bot import render_tasks

    query = update.callback_query
    await query.answer()
    if query.message.chat.type != "private":
        return
    parts = query.data.split(":")
    if len(parts) not in {3, 4}:
        return
    _, token, action, *values = parts
    reviews = context.user_data.setdefault("task_approval_reviews", {})
    for key in list(reviews):
        if reviews[key]["expires_at"] <= time.time():
            reviews.pop(key)
    if action in {"confirm", "back"}:
        review = reviews.pop(token, None)
        if not review or review["chat_id"] != query.message.chat_id:
            await query.message.reply_text(
                "This task review expired or was already used. Refresh Task List."
            )
            return
        if action == "back":
            await render_tasks(query.message, context, review["page"])
            return
        expected = review["task"]
        status = await query.message.reply_text(
            "⏳ Rechecking this task before approval…"
        )
        try:
            try:
                result = await execute_task_action(
                    expected,
                    approve=True,
                    auth_notify=owner_auth_notifier(
                        context.bot, render=getattr(context, "ui_auth_notify", None)
                    ),
                )
            finally:
                finish_operation(context)
        except TaskApprovalOutcomeUnknown as error:
            context.user_data.pop("task_list_result", None)
            await status.edit_text(
                f"⚠️ {error} This review is disabled; do not blindly repeat approval."
            )
            return
        except FBSError as error:
            await status.edit_text(f"⚠️ {error} Refresh Task List for a new review.")
            return
        context.user_data.pop("task_list_result", None)
        context.user_data.pop("task_list_view", None)
        if getattr(context, "ui_panel", None):
            context.ui_panel.back_to("open_tasks")
        text = f"✅ Task approval verified in FBS.\nReference: {expected['reference_number']}\nRoom: {expected['room']}\nTask status: {result['task_status']}\n\nThis approves your task; the booking may still require other confirmations."
        await status.edit_text(
            text,
            parse_mode=None,
            reply_markup=Markup(
                [[Button("↻ Refresh Task List", callback_data="open_tasks")]]
            ),
        )
        await send_task_receipt(context, query, status, result, text)
        return

    view = listing(context, query.message.chat_id)
    if (
        not view
        or view["id"] != token
        or view["expires_at"] <= time.time()
        or view["chat_id"] != query.message.chat_id
    ):
        await query.message.reply_text("This Task List expired. Refresh /tasks.")
        return
    try:
        index = int(values[0])
    except (ValueError, IndexError):
        return
    tasks = context.user_data["task_list_result"]["tasks"]
    if not 0 <= index < len(tasks):
        return
    expected = dict(tasks[index])
    if action == "view":
        status = await query.message.reply_text(
            "⏳ Opening this task's booking details…"
        )
        try:
            result = await execute_task_action(
                expected,
                auth_notify=owner_auth_notifier(
                    context.bot, render=getattr(context, "ui_auth_notify", None)
                ),
            )
        except FBSError as error:
            await status.edit_text(f"⚠️ {error}")
            return
        await render_tasks(status, context, index // 4)
        await send_task_receipt(
            context,
            query,
            status,
            result,
            "Task details opened. No approval was submitted.",
        )
        return
    if action != "approve" or not expected.get("can_approve"):
        return
    key = secrets.token_urlsafe(8)
    reviews.clear()
    reviews[key] = {
        "task": expected,
        "chat_id": query.message.chat_id,
        "expires_at": time.time() + 300,
        "page": index // 4,
    }
    if getattr(context, "ui_panel", None):
        context.ui_panel.back_to(f"task:{key}:back")
    event("tasks.approval_review_created")
    await query.message.reply_text(
        f"Approve this FBS task?\n\nReference: {expected['reference_number']}\nRoom: {expected['room']}\nWhen: {expected['date_time']}\nRequest: {expected['request_type']}\nRequested by: {expected['requested_by']}\nStatus: {expected['status']}\n\nOnly this task will be selected. Confirming submits approval to FBS. This review expires in 5 minutes.",
        parse_mode=None,
        reply_markup=Markup(
            [
                [Button("Confirm task approval", callback_data=f"task:{key}:confirm")],
                [Button("← Back to tasks", callback_data=f"task:{key}:back")],
            ]
        ),
    )


async def send_task_receipt(context, query, status, result, text):
    if result.get("receipt_png"):
        kind = (
            "approval acknowledgement"
            if result.get("receipt_kind") == "approval_acknowledgement"
            else "booking details"
        )
        try:
            await deliver_receipt(
                context,
                query.from_user.id,
                result["receipt_png"],
                f"FBS {kind} · {result['reference_number']}\nTask status: {result.get('task_status', result['task']['status'])}",
            )
        except Exception as error:
            event("tasks.receipt_delivery_failed", error_type=type(error).__name__)
            await status.edit_text(
                text
                + "\nThe screenshot could not be delivered. Check FBS; do not repeat a verified approval.",
                parse_mode=None,
            )
    elif result.get("receipt_error"):
        await status.edit_text(text + "\n" + result["receipt_error"], parse_mode=None)
