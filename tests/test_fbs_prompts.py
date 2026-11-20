"""FBS's confirm() before a write: read it, answer OK only to a zero penalty.

Browser tests use a local page from set_content() in headless Chromium behind a
dead proxy, so nothing can leave this machine. Never live FBS.
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from sagasu.fbs_prompts import (
    PROMPT_SECONDS,
    Answer,
    PromptDeclined,
    click_and_accept,
    expected_prompt,
)

TEXT = (
    "A Cancellation Penalty of [{}] will be imposed. "
    "Please click Ok if you wish to proceed"
)
ZERO = TEXT.format("0.00")
POSTBACK = "__doPostBack('panel$UIButton2','')"


def guard(message, tail=POSTBACK):
    """The onclick FBS put on "Confirm Cancellation" (20 Nov 2026)."""
    return "if(!confirm('" + message + "'))return false;" + tail


class ExpectedPromptTests(unittest.TestCase):
    def test_no_prompt_means_none(self):
        for onclick in (None, "", "return true;", POSTBACK, "javascript:void(0)"):
            with self.subTest(onclick=onclick):
                self.assertIsNone(expected_prompt(onclick))

    def test_zero_penalty_returns_the_exact_prompt(self):
        for amount in ("0.00", "0", "0.0", "00.00"):
            with self.subTest(amount=amount):
                message = TEXT.format(amount)
                self.assertEqual(expected_prompt(guard(message)), message)

    def test_guard_whitespace_is_tolerated(self):
        onclick = "if ( ! confirm( '" + ZERO + "' ) ) return false ; " + POSTBACK
        self.assertEqual(expected_prompt(onclick), ZERO)

    def test_non_zero_penalty_is_declined_as_a_penalty(self):
        for amount in ("5.00", "0.01", "1", "100", "1234567.99"):
            with self.subTest(amount=amount):
                with self.assertRaises(PromptDeclined) as caught:
                    expected_prompt(guard(TEXT.format(amount)))
                self.assertIs(caught.exception.penalty, True)

    def test_anything_else_is_declined_without_penalty(self):
        cases = {
            "other wording": guard("Are you sure you want to cancel?"),
            "two confirm calls": guard(ZERO) + ";" + guard(ZERO, ""),
            "second confirm outside a guard": guard(ZERO, "confirm('Again?');"),
            "double quotes": 'if(!confirm("' + ZERO + '"))return false;' + POSTBACK,
            "text before the message": guard("Note: " + ZERO),
            "text after the message": guard(ZERO + ". Fees apply."),
            "unguarded confirm": "confirm('" + ZERO + "');" + POSTBACK,
            "guard without negation": "if(confirm('" + ZERO + "'))return false;",
            "guard returning true": "if(!confirm('" + ZERO + "'))return true;",
            "window.confirm": "if(!window.confirm('" + ZERO + "'))return false;",
            "escaped quote": guard("Penalty \\' " + ZERO),
            "three decimals": guard(TEXT.format("0.000")),
            "negative": guard(TEXT.format("-0.00")),
            "comma decimal": guard(TEXT.format("0,00")),
            "padded amount": guard(TEXT.format(" 0.00 ")),
            "empty amount": guard(TEXT.format("")),
            "lower-case wording": guard(ZERO.lower()),
        }
        for label, onclick in cases.items():
            with self.subTest(label):
                with self.assertRaises(PromptDeclined) as caught:
                    expected_prompt(onclick)
                self.assertIs(caught.exception.penalty, False)

    def test_confirm_with_space_before_parenthesis_is_not_called_promptless(self):
        # Regression: `confirm ('…')` is legal JavaScript and still shows the
        # prompt. Reading it as "no prompt" made the callers click without a
        # listener, so the prompt was dismissed and the write recorded as
        # uncertain although nothing was posted.
        onclick = "if(!confirm ('" + ZERO + "'))return false;" + POSTBACK
        self.assertEqual(expected_prompt(onclick), ZERO)
        unguarded = "confirm ('" + ZERO + "');" + POSTBACK
        with self.assertRaises(PromptDeclined):
            expected_prompt(unguarded)

    def test_declined_carries_a_message_and_defaults_to_no_penalty(self):
        error = PromptDeclined("changed")
        self.assertEqual(str(error), "changed")
        self.assertIs(error.penalty, False)


class FakePage:
    def __init__(self):
        self.handlers = []

    def on(self, event, handler):
        assert event == "dialog"
        self.handlers.append(handler)

    def remove_listener(self, event, handler):
        assert event == "dialog"
        self.handlers.remove(handler)


class FakeDialog:
    def __init__(self, kind, message, answer=None):
        self.type, self.message = kind, message
        self.answer = answer
        self.seen_accepted = None
        self.result = None

    async def accept(self):
        self.seen_accepted = self.answer.accepted if self.answer else None
        self.result = "accepted"

    async def dismiss(self):
        self.result = "dismissed"


class FakeButton:
    def __init__(self, page, dialogs=(), error=None):
        self.page, self.dialogs, self.error = page, dialogs, error

    async def click(self):
        for dialog in self.dialogs:
            for handler in list(self.page.handlers):
                await handler(dialog)
        if self.error:
            raise self.error


class ClickAndAcceptUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepted_is_set_before_ok_is_sent(self):
        page, answer = FakePage(), Answer(ZERO)
        dialog = FakeDialog("confirm", ZERO, answer)
        await click_and_accept(page, FakeButton(page, [dialog]), answer, seconds=0.5)
        self.assertEqual(dialog.result, "accepted")
        self.assertIs(dialog.seen_accepted, True)
        self.assertIs(answer.accepted, True)
        self.assertIs(answer.declined, False)
        self.assertEqual(page.handlers, [])

    async def test_only_the_first_exact_confirm_is_accepted(self):
        page, answer = FakePage(), Answer(ZERO)
        first, second = FakeDialog("confirm", ZERO), FakeDialog("confirm", ZERO)
        await click_and_accept(
            page, FakeButton(page, [first, second]), answer, seconds=0.5
        )
        self.assertEqual((first.result, second.result), ("accepted", "dismissed"))
        self.assertIs(answer.accepted, True)
        self.assertIs(answer.declined, False)

    async def test_wrong_type_or_text_is_dismissed(self):
        for dialog in (
            FakeDialog("alert", ZERO),
            FakeDialog("prompt", ZERO),
            FakeDialog("beforeunload", ZERO),
            FakeDialog("confirm", TEXT.format("5.00")),
            FakeDialog("confirm", ZERO + " "),
            FakeDialog("confirm", ""),
        ):
            with self.subTest(kind=dialog.type, message=dialog.message):
                page, answer = FakePage(), Answer(ZERO)
                await click_and_accept(
                    page, FakeButton(page, [dialog]), answer, seconds=0.5
                )
                self.assertEqual(dialog.result, "dismissed")
                self.assertIs(answer.accepted, False)
                self.assertIs(answer.declined, True)

    async def test_listener_is_removed_when_the_click_fails(self):
        page, answer = FakePage(), Answer(ZERO)
        with self.assertRaises(RuntimeError):
            await click_and_accept(
                page,
                FakeButton(page, error=RuntimeError("detached")),
                answer,
                seconds=0.5,
            )
        self.assertEqual(page.handlers, [])
        self.assertIs(answer.accepted, False)

    async def test_click_is_made_once(self):
        page, answer = FakePage(), Answer(ZERO)
        button = AsyncMock()
        await click_and_accept(page, button, answer, seconds=0.5)
        button.click.assert_awaited_once_with()

    def test_default_wait_is_generous(self):
        self.assertGreaterEqual(PROMPT_SECONDS, 5)


class ClickAndAcceptBrowserTests(unittest.IsolatedAsyncioTestCase):
    """A real Chromium page; every request would go to a dead local proxy."""

    async def asyncSetUp(self):
        driver = await async_playwright().start()
        self.addAsyncCleanup(driver.stop)
        browser = await driver.chromium.launch(
            headless=True, proxy={"server": "http://127.0.0.1:9"}
        )
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()
        self.requests = []
        self.page.on("request", lambda request: self.requests.append(request.url))

        async def refuse(route):
            await route.abort()

        await self.page.route("**/*", refuse)

    async def asyncTearDown(self):
        # set_content() itself makes no request; nor should any button here.
        self.assertEqual(self.requests, [])

    async def button(self, onclick):
        # Record every confirm()/prompt() answer while keeping the native dialog.
        await self.page.set_content(
            "<html><head><script>"
            "var realConfirm = window.confirm, realPrompt = window.prompt;"
            "window.confirm = function (m) { var r = realConfirm.call(window, m);"
            " document.body.dataset.answers = (document.body.dataset.answers || '')"
            " + String(r) + ';'; return r; };"
            "window.prompt = function (m) { var r = realPrompt.call(window, m);"
            " document.body.dataset.answers = (document.body.dataset.answers || '')"
            " + String(r) + ';'; return r; };"
            "</script></head><body>"
            '<a id="go" href="#" onclick="' + onclick + '">Confirm Cancellation</a>'
            "</body></html>"
        )
        return self.page.locator("#go")

    async def data(self):
        return await self.page.evaluate("Object.assign({}, document.body.dataset)")

    async def answers(self, count):
        await self.page.wait_for_function(
            "(document.body.dataset.answers || '').split(';').length > " + str(count),
            timeout=5000,
        )
        return (await self.data())["answers"].split(";")[:-1]

    async def test_exact_zero_penalty_prompt_is_accepted_and_handler_runs(self):
        button = await self.button(
            guard(ZERO, "document.body.dataset.flag='posted';return false;")
        )
        message = expected_prompt(await button.get_attribute("onclick"))
        self.assertEqual(message, ZERO)
        answer = Answer(message)
        started = time.monotonic()
        await click_and_accept(self.page, button, answer, seconds=5)
        self.assertLess(time.monotonic() - started, 4)
        self.assertIs(answer.accepted, True)
        self.assertIs(answer.declined, False)
        self.assertEqual(await self.answers(1), ["true"])
        self.assertEqual((await self.data()).get("flag"), "posted")

    async def test_confirm_with_other_text_is_dismissed(self):
        button = await self.button(
            guard(TEXT.format("5.00"), "document.body.dataset.flag='posted';")
        )
        answer = Answer(ZERO)
        await click_and_accept(self.page, button, answer, seconds=2)
        self.assertIs(answer.accepted, False)
        self.assertIs(answer.declined, True)
        self.assertEqual(await self.answers(1), ["false"])
        self.assertNotIn("flag", await self.data())

    async def test_prompt_dialog_with_the_exact_text_is_dismissed(self):
        button = await self.button(
            "if(prompt('" + ZERO + "')===null)return false;"
            "document.body.dataset.flag='posted';"
        )
        answer = Answer(ZERO)
        await click_and_accept(self.page, button, answer, seconds=2)
        self.assertIs(answer.accepted, False)
        self.assertIs(answer.declined, True)
        self.assertEqual(await self.answers(1), ["null"])
        self.assertNotIn("flag", await self.data())

    async def test_alert_before_the_confirm_refuses_both(self):
        button = await self.button(
            "alert('Something changed');"
            + guard(ZERO, "document.body.dataset.flag='posted';")
        )
        answer = Answer(ZERO)
        await click_and_accept(self.page, button, answer, seconds=2)
        self.assertIs(answer.accepted, False)
        self.assertIs(answer.declined, True)
        self.assertEqual(await self.answers(1), ["false"])
        self.assertNotIn("flag", await self.data())

    async def test_second_exact_confirm_is_refused(self):
        button = await self.button(
            guard(ZERO, guard(ZERO, "document.body.dataset.flag='posted';"))
        )
        answer = Answer(ZERO)
        await click_and_accept(self.page, button, answer, seconds=2)
        self.assertIs(answer.accepted, True)
        self.assertEqual(await self.answers(2), ["true", "false"])
        self.assertNotIn("flag", await self.data())

    async def test_no_dialog_returns_after_the_wait(self):
        button = await self.button("document.body.dataset.clicked='yes';return false;")
        answer = Answer(ZERO)
        started = time.monotonic()
        await click_and_accept(self.page, button, answer, seconds=0.6)
        self.assertGreaterEqual(time.monotonic() - started, 0.55)
        self.assertIs(answer.accepted, False)
        self.assertIs(answer.declined, False)
        self.assertEqual((await self.data()).get("clicked"), "yes")

    async def test_confirm_inside_the_wait_is_accepted(self):
        button = await self.button(
            "setTimeout(function(){"
            + guard(ZERO, "document.body.dataset.flag='posted';").replace(
                "return false;", "return;"
            )
            + "}, 300);return false;"
        )
        answer = Answer(ZERO)
        await click_and_accept(self.page, button, answer, seconds=3)
        self.assertIs(answer.accepted, True)
        self.assertEqual(await self.answers(1), ["true"])
        self.assertEqual((await self.data()).get("flag"), "posted")

    async def test_confirm_after_the_wait_is_dismissed(self):
        button = await self.button(
            "setTimeout(function(){"
            + guard(ZERO, "document.body.dataset.flag='posted';").replace(
                "return false;", "return;"
            )
            + "}, 1500);return false;"
        )
        answer = Answer(ZERO)
        started = time.monotonic()
        await click_and_accept(self.page, button, answer, seconds=0.5)
        self.assertLess(time.monotonic() - started, 1.4)
        self.assertIs(answer.accepted, False)
        self.assertEqual(await self.answers(1), ["false"])
        self.assertNotIn("flag", await self.data())
        self.assertIs(answer.accepted, False)

    async def test_prompt_after_return_is_dismissed(self):
        button = await self.button("return false;")
        await click_and_accept(self.page, button, Answer(ZERO), seconds=0.5)
        # Past its moment, even the exact prompt must not be answered OK.
        await self.page.evaluate("setTimeout(function(){ confirm('" + ZERO + "'); }, 0)")
        self.assertEqual(await self.answers(1), ["false"])


if __name__ == "__main__":
    unittest.main()
