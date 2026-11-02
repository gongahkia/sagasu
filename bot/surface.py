"""One persistent control message and one replaceable receipt per conversation."""

import asyncio
import functools
import logging
import secrets

from telegram import (
    InlineKeyboardButton as Button,
    InlineKeyboardMarkup as Markup,
    InputMediaPhoto,
    InputMediaDocument,
)
from telegram.error import BadRequest
from .personal import PersonalDataError

logger = logging.getLogger(__name__)
STATE_KEY = "conversation_surface"
_locks = {}
_active = {}


def conversation_state(context, chat_id):
    return context.user_data.setdefault(STATE_KEY, {}).setdefault(str(chat_id), {})


def finish_operation(context):
    panel = getattr(context, "ui_panel", None)
    if panel:
        panel.complete()


class Panel:
    def __init__(self, context, message, generation):
        self.context, self.source, self.generation = context, message, generation
        self.chat_id = message.chat_id
        self.key = (id(context.bot), self.chat_id)
        self.state = conversation_state(context, self.chat_id)
        self.lock = _locks.setdefault(self.key, asyncio.Lock())

    def back_to(self, target):
        self.state["back"] = target

    def complete(self):
        active = _active.get(self.key)
        if active and active["task"] is asyncio.current_task():
            active["critical"] = False

    def markup(self, markup):
        rows = list(markup.inline_keyboard) if markup else []
        active = _active.get(self.key, {})
        if active.get("critical"):
            rows = []
        else:
            callbacks = [button.callback_data for row in rows for button in row]
            back = self.state.get("back", "ui:home")
            if (
                back
                and back not in callbacks
                and not any(
                    button.text.startswith(
                        ("← Back", "⬅️ Back", "← Change", "← Saved", "← Choose")
                    )
                    for row in rows
                    for button in row
                )
            ):
                rows.append(
                    [
                        Button(
                            "← Home" if back == "ui:home" else "← Back",
                            callback_data=back,
                        )
                    ]
                )
            callbacks = [button.callback_data for row in rows for button in row]
            if self.state.get("screen") != "home" and "ui:home" not in callbacks:
                rows.append([Button("🏠 Home", callback_data="ui:home")])
        result = []
        for row in rows:
            buttons = []
            for button in row:
                values = button.to_dict()
                if values.get("callback_data"):
                    values["callback_data"] = (
                        f"u:{self.generation}:{values['callback_data']}"
                    )
                    if len(values["callback_data"].encode()) > 64:
                        raise ValueError("Surface callback exceeds Telegram's limit")
                buttons.append(Button.de_json(values, self.context.bot))
            result.append(buttons)
        return Markup(result)

    async def render(
        self, text, *, reply_markup=None, parse_mode=None, sensitive=False, **kwargs
    ):
        async with self.lock:
            if self.state.get("generation") != self.generation:
                return PanelMessage(self.source, self)
            markup = self.markup(reply_markup)
            payload = dict(text=text, parse_mode=parse_mode, reply_markup=markup)
            message_id = self.state.get("message_id")
            if message_id:
                try:
                    await self.context.bot.edit_message_text(
                        chat_id=self.chat_id, message_id=message_id, **payload
                    )
                except BadRequest as error:
                    reason = str(error).casefold()
                    if "message is not modified" in reason:
                        pass
                    elif "message to edit not found" in reason:
                        self.state.pop("message_id", None)
                    elif (
                        "message can't be edited" in reason
                        or "message cannot be edited" in reason
                    ):
                        await self.context.bot.delete_message(
                            chat_id=self.chat_id, message_id=message_id
                        )
                        self.state.pop("message_id", None)
                    else:
                        raise
            if not self.state.get("message_id"):
                task = asyncio.create_task(
                    self.context.bot.send_message(chat_id=self.chat_id, **payload)
                )
                try:
                    sent = await asyncio.shield(task)
                except asyncio.CancelledError:
                    sent = await task
                    self.state["message_id"] = sent.message_id
                    raise
                self.state["message_id"] = sent.message_id
            self.state.update(
                text="Waiting for your SMU sign-in approval." if sensitive else text,
                parse_mode=parse_mode,
                markup=reply_markup.to_dict() if reply_markup else None,
                rendered_generation=self.generation,
            )
        return PanelMessage(self.source, self)

    async def auth_notify(self, owner, text):
        if owner != self.chat_id or self.source.chat.type != "private":
            raise RuntimeError("MFA approval must use the owner's private conversation")
        await self.render(text, sensitive=True)


class PanelMessage:
    def __init__(self, source, panel):
        self._source, self._panel = source, panel

    def __getattr__(self, name):
        return getattr(self._source, name)

    async def reply_text(self, text, **kwargs):
        return await self._panel.render(text, **kwargs)

    async def edit_text(self, text, **kwargs):
        return await self._panel.render(text, **kwargs)


class PanelQuery:
    def __init__(self, source, panel, data):
        self._source, self._panel, self.data = source, panel, data
        self.message = PanelMessage(source.message, panel)

    def __getattr__(self, name):
        return getattr(self._source, name)

    async def answer(self, *args, **kwargs):
        try:
            return await self._source.answer(*args, **kwargs)
        except BadRequest as error:
            if "query is too old" not in str(error).casefold():
                raise

    async def edit_message_text(self, text, **kwargs):
        return await self._panel.render(text, **kwargs)

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        return await self._panel.render(
            self._panel.state.get("text", "Working…"),
            parse_mode=self._panel.state.get("parse_mode"),
            reply_markup=reply_markup,
        )


class SurfaceUpdate:
    def __init__(self, source, panel, data):
        self._source = source
        self.message = PanelMessage(source.message, panel) if source.message else None
        self.callback_query = (
            PanelQuery(source.callback_query, panel, data)
            if source.callback_query
            else None
        )
        self.effective_message = (
            self.callback_query.message if self.callback_query else self.message
        )

    def __getattr__(self, name):
        return getattr(self._source, name)


class SurfaceContext:
    def __init__(self, source, panel):
        self._source, self.ui_panel = source, panel
        self.ui_auth_notify = panel.auth_notify

    def __getattr__(self, name):
        return getattr(self._source, name)


def surface_handler(function, *, personal_store=None):
    @functools.wraps(function)
    async def wrapped(update, context):
        source = update.effective_message
        if not source:
            return
        state = conversation_state(context, source.chat_id)
        query = update.callback_query
        data = query.data if query else ""
        if query and data.startswith("u:"):
            _, generation, data = data.split(":", 2)
            if (
                state.get("generation") != generation
                or state.get("message_id") != source.message_id
            ):
                await query.answer(
                    "That screen has changed. Use the latest buttons.", show_alert=True
                )
                return
        elif query and state.get("message_id"):
            await query.answer(
                "That menu is outdated. Send /start to open the current screen.",
                show_alert=True,
            )
            return
        key = (id(context.bot), source.chat_id)
        active = _active.get(key)
        if active and active["critical"]:
            if query:
                await query.answer(
                    "Your booking action is still running. Please wait.",
                    show_alert=True,
                )
            await clean_input(update, context, state)
            return
        generation = secrets.token_hex(4)
        state["generation"] = generation
        state["screen"] = (
            "home" if data == "ui:home" or function.__name__ == "start" else "view"
        )
        state["back"] = "ui:home"
        if query and not state.get("message_id"):
            state["message_id"] = source.message_id
        panel = Panel(context, source, generation)
        critical = data.startswith("booking_confirm:") or (
            data.startswith(("manage:", "task:")) and data.endswith(":confirm")
        )
        _active[key] = {"task": asyncio.current_task(), "critical": critical}
        try:
            if active and active["task"] is not asyncio.current_task():
                active["task"].cancel()
                try:
                    await active["task"]
                except asyncio.CancelledError:
                    pass
            bound = SurfaceContext(context, panel)
            if personal_store is not None:
                user = update.effective_user
                if user is None:
                    return
                bound.personal_profile = personal_store.for_user(user.id)
                bound.personal_profile.import_legacy(context.user_data)
            await function(SurfaceUpdate(update, panel, data), bound)
        except PersonalDataError as error:
            await panel.render(f"⚠️ {error}", parse_mode=None)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                if (
                    _active.get(key, {}).get("task") is asyncio.current_task()
                    and _active[key]["critical"]
                ):
                    panel.complete()
                    if state.get("generation") == generation and state.get("text"):
                        markup = (
                            Markup.de_json(state["markup"], context.bot)
                            if state.get("markup")
                            else None
                        )
                        await panel.render(
                            state["text"],
                            parse_mode=state.get("parse_mode"),
                            reply_markup=markup,
                        )
                if (
                    state.get("generation") == generation
                    and state.get("rendered_generation") != generation
                    and state.get("text")
                ):
                    markup = (
                        Markup.de_json(state["markup"], context.bot)
                        if state.get("markup")
                        else None
                    )
                    await panel.render(
                        state["text"],
                        parse_mode=state.get("parse_mode"),
                        reply_markup=markup,
                    )
            finally:
                if _active.get(key, {}).get("task") is asyncio.current_task():
                    _active.pop(key, None)
                await clean_input(update, context, state)

    return wrapped


async def clean_input(update, context, state):
    message = update.message
    if (
        message
        and message.chat.type == "private"
        and message.message_id
        not in (state.get("message_id"), state.get("receipt_message_id"))
    ):
        try:
            await context.bot.delete_message(
                chat_id=message.chat_id, message_id=message.message_id
            )
        except Exception as error:
            logger.debug("Handled input cleanup unavailable (%s)", type(error).__name__)


async def deliver_receipt(context, chat_id, png, caption):
    state = conversation_state(context, chat_id)
    key = (id(context.bot), chat_id)
    async with _locks.setdefault(key, asyncio.Lock()):
        for kind in ("photo", "document"):
            try:
                if state.get("receipt_message_id"):
                    media = (
                        InputMediaPhoto(png, caption=caption[:1024], parse_mode=None)
                        if kind == "photo"
                        else InputMediaDocument(
                            png,
                            filename="sagasu-receipt.png",
                            caption=caption[:1024],
                            parse_mode=None,
                        )
                    )
                    try:
                        await context.bot.edit_message_media(
                            chat_id=chat_id,
                            message_id=state["receipt_message_id"],
                            media=media,
                        )
                        return
                    except BadRequest as error:
                        reason = str(error).casefold()
                        if "message is not modified" in reason:
                            return
                        if (
                            "message to edit not found" not in reason
                            and "can't be edited" not in reason
                            and "cannot be edited" not in reason
                        ):
                            raise
                        if "message to edit not found" not in reason:
                            # Remove a known old receipt before creating its replacement.
                            await context.bot.delete_message(
                                chat_id=chat_id, message_id=state["receipt_message_id"]
                            )
                        state.pop("receipt_message_id", None)
                send = (
                    context.bot.send_photo(
                        chat_id=chat_id,
                        photo=png,
                        caption=caption[:1024],
                        parse_mode=None,
                    )
                    if kind == "photo"
                    else context.bot.send_document(
                        chat_id=chat_id,
                        document=png,
                        filename="sagasu-receipt.png",
                        caption=caption[:1024],
                        parse_mode=None,
                    )
                )
                task = asyncio.create_task(send)
                try:
                    sent = await asyncio.shield(task)
                except asyncio.CancelledError:
                    sent = await task
                    state["receipt_message_id"] = sent.message_id
                    raise
                state["receipt_message_id"] = sent.message_id
                return
            except BadRequest:
                if kind == "document":
                    raise
