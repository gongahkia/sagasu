"""FBS's two-step cancel flow, driven in a real browser against a stand-in; never live FBS.

The stand-in (tests/cancellation_fixture.py) is shaped like the pages FBS served on
20 Nov 2026: CANCEL in My Bookings opens the booking form in cancel mode, whose
"Confirm Cancellation" button asks a browser confirm() about the penalty before it
posts the form. These tests hold Sagasu to its safety contract:

- a failure before OK is answered (or, with no prompt, before the confirm click)
  is CancellationUnavailable / CancellationPenalty / AuthenticationRequired, and
  FBS receives no POST of the form;
- only after OK can it be CancellationOutcomeUnknown;
- only the exact zero-penalty prompt is answered OK; every other dialog is dismissed;
- success needs My Bookings to show the exact reference (same room and time) as
  cancelled or pending cancellation.

Chromium runs behind a dead proxy and every request is routed, so nothing leaves
this machine. Redirects are HTML location.replace() pages, never 3xx.
"""

import asyncio
import copy
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sagasu.action_journal import ActionJournal, JournalError
from sagasu.fbs_account import (
    CancellationOutcomeUnknown,
    CancellationPenalty,
    CancellationUnavailable,
    cancel_booking,
)
from sagasu.fbs_client import AuthenticationRequired, LayoutChanged
from sagasu.fbs_prompts import Answer, PromptDeclined, click_and_accept, expected_prompt
from sagasu.mcp_service import AgentError, AgentService, Backend
from tests.cancellation_fixture import (
    LIST_URL,
    PROMPT,
    ROW,
    CancellationFixture,
    _form,
)
from tests.test_mcp import ROW as MCP_ROW, FakeBackend, ServiceCase

ZERO = PROMPT.format(penalty="0.00")
FIVE = PROMPT.format(penalty="5.00")
TARGET = "panel$UIButton2$UIButton2_bt"

# Records every alert()/confirm() the cancel form shows, and how it was answered,
# on the top window (it outlives the iframe's navigation after a POST).
RECORDER = """<script>
(function () {
  function record(kind, message, answer) {
    try { window.top.__dialogs.push(kind + '|' + answer + '|' + message); } catch (e) {}
  }
  var ask = window.confirm, tell = window.alert;
  window.confirm = function (m) { var r = ask.call(window, m); record('confirm', m, r); return r; };
  window.alert = function (m) { tell.call(window, m); record('alert', m, 'null'); };
})();
</script>"""


def on_click(body):
    """A capture-phase click listener for the visible Confirm Cancellation button."""
    return (
        "<script>document.addEventListener('click', function (event) {"
        "if (!event.target.closest || !event.target.closest('#panel_spanSaveButtons #panel_UIButton2')) return;"
        + body
        + "}, true);</script>"
    )


def post_back():
    return f"__doPostBack({json.dumps(TARGET)}, '')"


class StandIn(CancellationFixture):
    """The shared stand-in plus knobs: extra_script (appended to the cancel form),
    edit_form / edit_listing (HTML transforms), error_before_post /
    error_after_post (read_bookings raises), rows_after_post (what My Bookings
    lists once the form was posted)."""

    KNOBS = dict(
        status=ROW["fbs_status"],
        penalty="0.00",
        prompt=None,
        form_reference=None,
        open_form=True,
        drop_post=False,
        status_after="Cancelled",
        label="Confirm Cancellation",
        extra_script="",
        edit_form=None,
        edit_listing=None,
        error_before_post=None,
        error_after_post=None,
        rows_after_post=None,
    )

    async def setup(self, case):
        for key, value in self.KNOBS.items():
            setattr(self, key, value)
        await super().setup(case)

    async def reset(self, **knobs):
        unknown = set(knobs) - set(self.KNOBS)
        if unknown:
            raise TypeError(f"unknown knobs {unknown}")
        for key, value in {**self.KNOBS, **knobs}.items():
            setattr(self, key, value)
        self.posts.clear()
        self.reasons.clear()
        self.opened = 0
        await self.page.evaluate("window.__dialogs = []")

    async def dialogs(self):
        return [
            tuple(entry.split("|", 2))
            for entry in await self.page.evaluate("window.__dialogs || []")
        ]

    def cancel_form(self):
        page = super().cancel_form()
        if self.edit_form:
            page = self.edit_form(page)
        return page.replace(
            "</form></body>", "</form>" + RECORDER + self.extra_script + "</body>"
        )

    def listing(self):
        page = super().listing()
        return self.edit_listing(page) if self.edit_listing else page

    async def read_bookings(self, client, **kwargs):
        error = self.error_after_post if self.posts else self.error_before_post
        if error is not None:
            raise error
        listing = await super().read_bookings(client, **kwargs)
        if self.posts and self.rows_after_post is not None:
            listing["bookings"] = copy.deepcopy(self.rows_after_post)
            listing["total"] = len(listing["bookings"])
        return listing

    async def reason_on_form(self):
        field = self.frame().locator("#bookingFormControl1_Remarks_c1")
        return await field.input_value() if await field.count() == 1 else None


def duplicate_row(page):
    row = re.search(r'<tr class="row">.*?</tr>', page, re.S)[0]
    return page.replace("</tbody>", row + "</tbody>")


class BrowserCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fbs = StandIn()
        await self.fbs.setup(self)
        await self.fbs.reset()
        # Generous enough for a loaded machine on the paths that succeed; tests
        # that wait out a deadline shorten it themselves.
        for name, value in (
            ("sagasu.fbs_account.CANCEL_FORM_SECONDS", 15),
            ("sagasu.fbs_account.CANCEL_SUBMIT_MS", 15000),
            ("sagasu.fbs_account.CANCEL_CHECKS", 1),
        ):
            patcher = patch(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def cancel(self, reason="Plans changed", **changes):
        return await cancel_booking(self.fbs.client, {**ROW, **changes}, reason)

    def assert_nothing_posted(self):
        self.assertEqual(self.fbs.posts, [], "FBS received a cancellation POST")
        self.assertEqual(self.fbs.reasons, [])

    async def assert_no_confirm_accepted(self):
        for kind, answer, message in await self.fbs.dialogs():
            self.assertNotEqual(answer, "true", f"{kind} {message!r} was accepted")


class CancellationHappyPathTests(BrowserCase):
    async def test_zero_penalty_posts_once_with_normalised_reason_and_checkpoints_exact_reference(
        self,
    ):
        reason = "  Plans\n\tchanged  for   the team  " + "x" * 300
        expected_text = " ".join(reason.split())[:255]
        self.assertEqual(len(expected_text), 255)
        with patch("sagasu.fbs_account.checkpoint_verified") as checkpoint:
            result = await self.cancel(reason)
        self.assertEqual(result, {"status": "cancelled", "fbs_status": "Cancelled"})
        self.assertEqual(self.fbs.opened, 1)
        self.assertEqual(self.fbs.posts, [TARGET])
        self.assertEqual(self.fbs.reasons, [expected_text])
        self.assertTrue(self.fbs.reasons[0].startswith("Plans changed for the team x"))
        self.assertEqual(await self.fbs.dialogs(), [("confirm", "true", ZERO)])
        checkpoint.assert_called_once_with(
            {
                "status": "cancelled",
                "fbs_status": "Cancelled",
                "reference_number": ROW["reference_number"],
            }
        )

    async def test_pending_cancellation_is_reported_as_pending(self):
        await self.fbs.reset(status_after="Pending Cancellation")
        with patch("sagasu.fbs_account.checkpoint_verified") as checkpoint:
            result = await self.cancel()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(self.fbs.posts), 1)
        self.assertEqual(checkpoint.call_args.args[0]["status"], "pending")

    async def test_no_prompt_is_refused_before_submitting(self):
        # FBS's Confirm Cancellation asked the penalty prompt on 20 Nov.
        # Without it the penalty cannot be checked, so nothing is submitted.
        await self.fbs.reset(prompt="none")
        with patch("sagasu.fbs_account.checkpoint_verified") as checkpoint:
            with self.assertRaises(CancellationUnavailable) as caught:
                await self.cancel("No longer needed")
        self.assertNotIsInstance(caught.exception, CancellationOutcomeUnknown)
        self.assertEqual(self.fbs.posts, [])
        self.assertEqual(await self.fbs.dialogs(), [])
        checkpoint.assert_not_called()

    async def test_prompt_that_arrives_late_but_within_the_window_is_accepted(self):
        await self.fbs.reset(
            extra_script=on_click(
                "event.stopImmediatePropagation(); event.preventDefault();"
                f"setTimeout(function () {{ if (confirm({json.dumps(ZERO)})) {post_back()}; }}, 600);"
            )
        )
        with patch("sagasu.fbs_prompts.PROMPT_SECONDS", 8):
            result = await self.cancel()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.fbs.posts, [TARGET])
        self.assertEqual(await self.fbs.dialogs(), [("confirm", "true", ZERO)])


class CancellationRefusedBeforeSubmitTests(BrowserCase):
    """Every failure here happens before OK: nothing may reach FBS's form."""

    async def test_nonzero_penalty_refuses_without_filling_or_posting(self):
        for penalty in ("5.00", "0.01", "100"):
            with self.subTest(penalty=penalty):
                await self.fbs.reset(penalty=penalty)
                with self.assertRaises(CancellationPenalty) as raised:
                    await self.cancel()
                self.assertEqual(raised.exception.reason, "cancellation_penalty")
                self.assertEqual(self.fbs.opened, 1)
                self.assert_nothing_posted()
                self.assertEqual(await self.fbs.reason_on_form(), "")
                self.assertEqual(await self.fbs.dialogs(), [])

    async def test_unrecognised_or_doubled_prompt_refuses_without_posting(self):
        for name, prompt in (
            ("other question", "Are you sure you want to cancel this booking?"),
            ("extra words", ZERO + " today"),
            ("unreadable amount", PROMPT.format(penalty="1,000.00")),
            # Closes the fixture's guard and opens a second identical one.
            ("two guards", ZERO + "'))return false;if(!confirm('" + ZERO),
        ):
            with self.subTest(name):
                await self.fbs.reset(prompt=prompt)
                with self.assertRaises(CancellationUnavailable) as raised:
                    await self.cancel()
                self.assertNotIsInstance(raised.exception, CancellationPenalty)
                self.assertEqual(raised.exception.reason, "cancellation_unavailable")
                self.assert_nothing_posted()
                self.assertEqual(await self.fbs.reason_on_form(), "")
                self.assertEqual(await self.fbs.dialogs(), [])

    async def test_form_for_another_reference_refuses(self):
        await self.fbs.reset(form_reference="BK-20990101-000002")
        with self.assertRaises(CancellationUnavailable):
            await self.cancel()
        self.assertEqual(self.fbs.opened, 1)
        self.assert_nothing_posted()

    async def test_form_that_never_opens_refuses_after_its_deadline(self):
        await self.fbs.reset(open_form=False)
        started = time.monotonic()
        with patch("sagasu.fbs_account.CANCEL_FORM_SECONDS", 1.0):
            with self.assertRaises(CancellationUnavailable):
                await self.cancel()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.fbs.opened, 1)
        self.assert_nothing_posted()

    async def test_changed_form_refuses_without_posting(self):
        for name, knobs in (
            ("button says Cancel Booking", dict(label="Cancel Booking")),
            ("button says Confirm", dict(label="Confirm")),
            (
                "no reason caption",
                dict(
                    edit_form=lambda page: page.replace(
                        "Reason for Cancelling*:", "Remarks:"
                    )
                ),
            ),
            (
                "no reason field",
                dict(
                    edit_form=lambda page: page.replace(
                        'id="bookingFormControl1_Remarks_c1"',
                        'id="bookingFormControl1_Remarks_c9"',
                    )
                ),
            ),
            (
                "two visible confirm buttons",
                dict(
                    edit_form=lambda page: page.replace(
                        '<a id="panel_UIButton2" style="display:none"',
                        '<a id="panel_UIButton2"',
                    )
                ),
            ),
        ):
            with self.subTest(name):
                await self.fbs.reset(**knobs)
                with patch("sagasu.fbs_account.CANCEL_FORM_SECONDS", 1.0):
                    with self.assertRaises(CancellationUnavailable):
                        await self.cancel()
                self.assertEqual(self.fbs.opened, 1)
                self.assert_nothing_posted()
                self.assertEqual(await self.fbs.dialogs(), [])

    async def test_changed_listing_refuses_before_clicking_cancel(self):
        for name, knobs, changes, reason in (
            ("listed room differs", {}, {"room": "Room B"}, "Plans changed"),
            (
                "listed time differs",
                {},
                {"date_time": "02-Jan-2099 (Fri) 15:00 - 17:00 (2hrs)"},
                "Plans changed",
            ),
            (
                "listed status changed",
                {"status": "Pending Confirmation"},
                {},
                "Plans changed",
            ),
            (
                "reviewed status differs",
                {},
                {"fbs_status": "Pending Confirmation"},
                "Plans changed",
            ),
            (
                "no CANCEL offered",
                {"status": "Cancelled"},
                {"fbs_status": "Cancelled"},
                "Plans changed",
            ),
            (
                "reference not listed",
                {},
                {"reference_number": "BK-20990101-000009"},
                "Plans changed",
            ),
            ("reference listed twice", {"edit_listing": duplicate_row}, {}, "Plans"),
            ("blank reason", {}, {}, " \n\t  "),
        ):
            with self.subTest(name):
                await self.fbs.reset(**knobs)
                with self.assertRaises(CancellationUnavailable):
                    await self.cancel(reason, **changes)
                self.assertEqual(self.fbs.opened, 0, "CANCEL was clicked")
                self.assert_nothing_posted()

    async def test_unreadable_listing_is_unavailable_and_never_clicks(self):
        broken = _form(
            LIST_URL,
            '<div id="GridViewBooking"><div id="GridViewBooking_content"><table><tbody>'
            f'<tr class="row"><td></td><td>{ROW["reference_number"]}</td></tr>'
            "</tbody></table></div>1 ITEM(S) TOTAL</div>",
        )
        await self.fbs.reset(edit_listing=lambda page: broken)
        with self.assertRaises(CancellationUnavailable) as raised:
            await self.cancel()
        self.assertIsInstance(raised.exception.__cause__, LayoutChanged)
        self.assertEqual(self.fbs.opened, 0)
        self.assert_nothing_posted()

    async def test_lapsed_sign_in_before_clicking_is_reported_as_such(self):
        await self.fbs.reset(error_before_post=AuthenticationRequired("lapsed"))
        with self.assertRaises(AuthenticationRequired) as raised:
            await self.cancel()
        self.assertNotIsInstance(raised.exception, CancellationOutcomeUnknown)
        self.assertEqual(self.fbs.opened, 0)
        self.assert_nothing_posted()


class CancellationDialogTests(BrowserCase):
    """Only the exact zero-penalty confirm is answered OK; anything else is dismissed."""

    async def test_unexpected_dialogs_are_dismissed_and_nothing_posts(self):
        handler = (
            "var b = document.querySelector('#panel_spanSaveButtons #panel_UIButton2');"
            "b.onclick = function () { %s };"
        )
        for name, script in (
            ("alert before the prompt", on_click("alert('Your session ends soon');")),
            (
                "another confirm before the prompt",
                on_click("confirm('Also delete your other bookings?');"),
            ),
            (
                # The onclick attribute still reads [0.00]; the page asks [5.00].
                "the page asks a penalty its attribute does not show",
                "<script>"
                + handler
                % (
                    f"if (!confirm({json.dumps(FIVE)})) return false;"
                    f"setTimeout(function () {{ {post_back()}; }}, 0); return false;"
                )
                + "</script>",
            ),
            (
                "the expected words as an alert",
                "<script>"
                + handler % (f"alert({json.dumps(ZERO)}); return false;")
                + "</script>",
            ),
        ):
            with self.subTest(name):
                await self.fbs.reset(extra_script=script)
                with self.assertRaises(CancellationUnavailable) as raised:
                    await self.cancel()
                self.assertNotIsInstance(raised.exception, CancellationOutcomeUnknown)
                await asyncio.sleep(0.5)  # a post-back the page scheduled would land
                self.assertTrue(await self.fbs.dialogs(), "the page showed no dialog")
                await self.assert_no_confirm_accepted()
                self.assert_nothing_posted()

    async def test_prompt_after_the_window_is_dismissed_and_nothing_posts(self):
        # No dialog within the window: Sagasu cannot rule out that the button
        # posted, so the outcome is unknown (never "nothing was submitted"). The
        # prompt that turns up afterwards must still be dismissed.
        await self.fbs.reset(
            extra_script=on_click(
                "event.stopImmediatePropagation(); event.preventDefault();"
                f"setTimeout(function () {{ if (confirm({json.dumps(ZERO)})) {post_back()}; }}, 1500);"
            )
        )
        with (
            patch("sagasu.fbs_prompts.PROMPT_SECONDS", 0.6),
            patch("sagasu.fbs_account.CANCEL_SUBMIT_MS", 2500),
            patch("sagasu.fbs_account.checkpoint_verified") as checkpoint,
        ):
            with self.assertRaises(CancellationOutcomeUnknown):
                await self.cancel()
        deadline = time.monotonic() + 8
        while not await self.fbs.dialogs() and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        self.assertEqual(await self.fbs.dialogs(), [("confirm", "false", ZERO)])
        await asyncio.sleep(0.5)
        self.assert_nothing_posted()
        checkpoint.assert_not_called()

    async def test_button_that_posts_without_its_declared_prompt_is_verified(self):
        # The attribute declares the zero-penalty prompt but the page's handler
        # posts without asking. No dialog was seen, so Sagasu treats the click
        # as a submission and trusts only My Bookings.
        await self.fbs.reset(
            extra_script=on_click(
                "event.stopImmediatePropagation(); event.preventDefault();"
                f"{post_back()};"
            )
        )
        with (
            patch("sagasu.fbs_prompts.PROMPT_SECONDS", 0.6),
            patch("sagasu.fbs_account.checkpoint_verified") as checkpoint,
        ):
            result = await self.cancel()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.fbs.posts, [TARGET])
        self.assertEqual(await self.fbs.dialogs(), [])
        checkpoint.assert_called_once()

    async def test_page_that_posts_despite_a_dismissed_dialog_is_never_success(self):
        # Not FBS's behaviour (its onclick returns false when the prompt is
        # dismissed), but if a page posted anyway Sagasu must never call it a
        # verified cancellation.
        await self.fbs.reset(
            extra_script="<script>"
            "var b = document.querySelector('#panel_spanSaveButtons #panel_UIButton2');"
            f"b.onclick = function () {{ alert({json.dumps(ZERO)});"
            f"setTimeout(function () {{ {post_back()}; }}, 0); return false; }};"
            "</script>"
        )
        with patch("sagasu.fbs_account.checkpoint_verified") as checkpoint:
            with self.assertRaises((CancellationUnavailable, CancellationOutcomeUnknown)):
                await self.cancel()
        checkpoint.assert_not_called()
        await self.assert_no_confirm_accepted()

    async def test_penalty_prompt_written_with_a_space_is_refused_as_a_penalty(self):
        # Regression: "confirm (" was once read as "no prompt", so Sagasu clicked
        # with no dialog listener and reported an unknown outcome instead of
        # the penalty.
        await self.fbs.reset(
            edit_form=lambda page: page.replace("confirm(&#x27;", "confirm (&#x27;"),
            penalty="5.00",
        )
        try:
            with patch("sagasu.fbs_account.CANCEL_SUBMIT_MS", 1500):
                with self.assertRaises(CancellationPenalty):
                    await self.cancel()
        finally:
            self.assert_nothing_posted()


class CancellationAfterSubmitTests(BrowserCase):
    """Once OK is answered, only a verified listing is success; the rest is unknown."""

    async def test_dropped_post_after_ok_is_outcome_unknown(self):
        await self.fbs.reset(drop_post=True)
        with (
            patch("sagasu.fbs_account.CANCEL_SUBMIT_MS", 1500),
            patch("sagasu.fbs_account.checkpoint_verified") as checkpoint,
        ):
            with self.assertRaises(CancellationOutcomeUnknown):
                await self.cancel()
        self.assertEqual(self.fbs.posts, [TARGET], "expected exactly one POST attempt")
        self.assertEqual(await self.fbs.dialogs(), [("confirm", "true", ZERO)])
        checkpoint.assert_not_called()

    async def test_unverified_listing_after_post_is_outcome_unknown(self):
        cancelled = {**ROW, "fbs_status": "Cancelled"}
        for name, knobs in (
            ("still Confirmed", dict(status_after="Confirmed")),
            ("Cancellation Rejected", dict(status_after="Cancellation Rejected")),
            ("cancelled in another room", dict(rows_after_post=[{**cancelled, "room": "Room B"}])),
            (
                "cancelled at another time",
                dict(
                    rows_after_post=[
                        {**cancelled, "date_time": "01-Jan-2099 (Thu) 16:00 - 18:00 (2hrs)"}
                    ]
                ),
            ),
            ("listed twice", dict(rows_after_post=[cancelled, cancelled])),
            ("no longer listed", dict(rows_after_post=[])),
            ("sign-in lapsed", dict(error_after_post=AuthenticationRequired("lapsed"))),
        ):
            with self.subTest(name):
                await self.fbs.reset(**knobs)
                with patch("sagasu.fbs_account.checkpoint_verified") as checkpoint:
                    with self.assertRaises(CancellationOutcomeUnknown):
                        await self.cancel()
                checkpoint.assert_not_called()
                self.assertEqual(self.fbs.posts, [TARGET])

    async def test_listing_is_read_again_before_giving_up(self):
        reads = []
        original = self.fbs.read_bookings

        async def slow_to_show(client, **kwargs):
            listing = await original(client, **kwargs)
            reads.append(listing["bookings"][0]["fbs_status"])
            if self.fbs.posts and len(reads) == 2:
                listing["bookings"][0]["fbs_status"] = "Confirmed"
            return listing

        with (
            patch("sagasu.fbs_account.read_bookings", slow_to_show),
            patch("sagasu.fbs_account.CANCEL_CHECKS", 2),
            patch("sagasu.fbs_account.checkpoint_verified") as checkpoint,
        ):
            result = await self.cancel()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(reads), 3)
        checkpoint.assert_called_once()
        self.assertEqual(len(self.fbs.posts), 1)


class CancellationReasonTests(BrowserCase):
    async def test_reason_with_emoji_within_the_service_limit_is_submitted(self):
        # Regression: the reason was cut to 255 code points, but FBS's
        # maxlength=255 counts UTF-16 units as the browser does, so 200 emoji
        # (accepted by review_cancellation) never matched and could not cancel.
        reason = "\U0001F64F" * 200
        try:
            result = await self.cancel(reason)
        finally:
            self.assertLessEqual(len(self.fbs.posts), 1)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(self.fbs.posts), 1)
        sent = self.fbs.reasons[0]
        self.assertTrue(reason.startswith(sent))
        self.assertLessEqual(len(sent.encode("utf-16-le")) // 2, 255)


class ExpectedPromptTests(unittest.TestCase):
    """The prompt is read from the button before clicking (no browser)."""

    def guard(self, text):
        return f"if(!confirm('{text}'))return false;btn.cl(this);setTimeout('__doPostBack(\\'{TARGET}\\',\\'\\')',0);return false;"

    def test_zero_penalty_prompt_is_the_only_one_answered(self):
        for amount in ("0.00", "0", "0.0", "000.00"):
            with self.subTest(amount=amount):
                text = PROMPT.format(penalty=amount)
                self.assertEqual(expected_prompt(self.guard(text)), text)

    def test_no_prompt(self):
        for onclick in (None, "", "btn.cl(this);setTimeout('__doPostBack()',0);return false;"):
            with self.subTest(onclick=onclick):
                self.assertIsNone(expected_prompt(onclick))

    def test_any_penalty_is_declined_as_a_penalty(self):
        for amount in ("5.00", "0.01", "1", "9999999.99"):
            with self.subTest(amount=amount):
                with self.assertRaises(PromptDeclined) as raised:
                    expected_prompt(self.guard(PROMPT.format(penalty=amount)))
                self.assertTrue(raised.exception.penalty)

    def test_other_or_ambiguous_prompts_are_declined(self):
        for name, onclick in (
            ("other text", self.guard("Are you sure?")),
            ("unreadable amount", self.guard(PROMPT.format(penalty="1,000.00"))),
            ("negative amount", self.guard(PROMPT.format(penalty="-0.00"))),
            ("trailing words", self.guard(ZERO + " now")),
            ("two guards", self.guard(ZERO) + self.guard(ZERO)),
            ("guard plus bare confirm", self.guard(ZERO) + f"confirm('{ZERO}');"),
            ("double quotes", f'if(!confirm("{ZERO}"))return false;'),
            ("window.confirm", f"if(!window.confirm('{ZERO}'))return false;"),
            ("escaped quote", self.guard("A Cancellation Penalty of [0.00] won\\'t apply")),
        ):
            with self.subTest(name):
                with self.assertRaises(PromptDeclined) as raised:
                    expected_prompt(onclick)
                self.assertFalse(raised.exception.penalty)

    def test_spaced_confirm_call_is_still_recognised_as_a_prompt(self):
        # Regression: "confirm (" was once read as "no prompt".
        with self.assertRaises(PromptDeclined) as raised:
            expected_prompt(
                f"if(!confirm ('{FIVE}'))return false;btn.cl(this);return false;"
            )
        self.assertTrue(raised.exception.penalty)
        spaced = f"if ( ! confirm ( '{ZERO}' ) ) return false;btn.cl(this);"
        self.assertEqual(expected_prompt(spaced), ZERO)
        with self.assertRaises(PromptDeclined):
            expected_prompt(f"if(!confirm('{ZERO}'))return false;confirm ('{ZERO}');")


class Dialog:
    def __init__(self, kind, message, events):
        self.type, self.message, self.events = kind, message, events

    async def accept(self):
        self.events.append(("accept", self.type, self.message))

    async def dismiss(self):
        self.events.append(("dismiss", self.type, self.message))


class FakePage:
    def __init__(self):
        self.handlers = []

    def on(self, name, handler):
        assert name == "dialog"
        self.handlers.append(handler)

    def remove_listener(self, name, handler):
        self.handlers.remove(handler)

    async def show(self, dialog):
        for handler in list(self.handlers):
            await handler(dialog)
        if not self.handlers:
            await dialog.dismiss()  # Playwright's default with no listener


class ClickAndAcceptTests(unittest.IsolatedAsyncioTestCase):
    """The dialog policy itself, with stand-in dialogs (no browser)."""

    async def run_click(self, *shown, seconds=0.6):
        page, events = FakePage(), []
        answer = Answer(ZERO)
        observed = []

        async def click():
            for kind, message in shown:
                dialog = Dialog(kind, message, events)
                original = dialog.accept

                async def accept(dialog=dialog, original=original):
                    # OK must be recorded before it reaches the page.
                    observed.append(answer.accepted)
                    await original()

                dialog.accept = accept
                await page.show(dialog)

        button = Mock(click=AsyncMock(side_effect=click))
        await click_and_accept(page, button, answer, seconds=seconds)
        self.assertEqual(page.handlers, [], "dialog listener left registered")
        self.assertTrue(all(observed))
        return answer, events

    async def test_exact_zero_penalty_confirm_is_accepted(self):
        answer, events = await self.run_click(("confirm", ZERO))
        self.assertTrue(answer.accepted)
        self.assertFalse(answer.declined)
        self.assertEqual(events, [("accept", "confirm", ZERO)])

    async def test_other_dialogs_are_dismissed_and_poison_the_answer(self):
        for name, shown in (
            ("alert first", [("alert", "Session ends"), ("confirm", ZERO)]),
            ("other confirm first", [("confirm", "Delete all?"), ("confirm", ZERO)]),
            ("penalty confirm", [("confirm", FIVE)]),
            ("expected words as alert", [("alert", ZERO)]),
            ("expected words as prompt", [("prompt", ZERO)]),
            ("near miss", [("confirm", ZERO + " ")]),
        ):
            with self.subTest(name):
                answer, events = await self.run_click(*shown)
                self.assertFalse(answer.accepted)
                self.assertTrue(answer.declined)
                self.assertEqual([event[0] for event in events], ["dismiss"] * len(shown))

    async def test_second_prompt_after_ok_is_dismissed(self):
        answer, events = await self.run_click(("confirm", ZERO), ("confirm", ZERO))
        self.assertTrue(answer.accepted)
        self.assertFalse(answer.declined)
        self.assertEqual(
            events, [("accept", "confirm", ZERO), ("dismiss", "confirm", ZERO)]
        )

    async def test_no_dialog_returns_after_the_window_without_accepting(self):
        started = time.monotonic()
        answer, events = await self.run_click(seconds=0.6)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(answer.accepted)
        self.assertFalse(answer.declined)
        self.assertEqual(events, [])

    async def test_patched_prompt_seconds_bounds_the_wait(self):
        # Regression: PROMPT_SECONDS was once frozen into a default argument,
        # so patching the module constant changed nothing.
        page = FakePage()
        button = Mock(click=AsyncMock())
        with patch("sagasu.fbs_prompts.PROMPT_SECONDS", 0.5):
            try:
                await asyncio.wait_for(
                    click_and_accept(page, button, Answer(ZERO)), 3
                )
            except TimeoutError:
                self.fail("click_and_accept ignored the patched PROMPT_SECONDS")


class CancelErrorBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.cancel_errors = []

    async def run(self, kind, payload, progress, notify):
        if kind == "cancel" and self.cancel_errors:
            self.calls.append((kind, payload))
            raise self.cancel_errors.pop(0)
        return await super().run(kind, payload, progress, notify)


class CancellationServiceTests(ServiceCase):
    """How the job runner reports each cancellation outcome (fake backend)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.backend = CancelErrorBackend()
        self.service.backend = self.backend

    async def cancel_review(self, service=None):
        service = service or self.service
        source = service.start("bookings")
        await service.get(source["operation_id"]).task
        return service.review_cancellation(
            source["operation_id"], MCP_ROW["reference_number"], "Plans changed"
        )

    def cancels(self):
        return [kind for kind, _ in self.backend.calls].count("cancel")

    async def test_refusals_fail_without_blocking_a_new_review(self):
        for error, reason, next_tool in (
            (
                CancellationUnavailable("private detail BK-SECRET"),
                "cancellation_unavailable",
                "list_bookings",
            ),
            (
                CancellationPenalty("private detail BK-SECRET"),
                "cancellation_penalty",
                None,
            ),
        ):
            with self.subTest(reason):
                before = self.cancels()
                self.backend.cancel_errors = [error]
                review = await self.cancel_review()
                job = await self.finish(self.service.submit(review["review_id"]))
                self.assertEqual(job.status, "failed")
                public = job.public()
                self.assertEqual(public["error"]["reason"], reason)
                self.assertEqual(public["error"]["next_tool"], next_tool)
                self.assertFalse(public["reconciliation_required"])
                self.assertNotIn("BK-SECRET", json.dumps(public))
                # The same review replays the same failed job, never FBS.
                self.assertEqual(
                    self.service.submit(review["review_id"])["operation_id"], job.id
                )
                retry = await self.cancel_review()
                second = await self.finish(self.service.submit(retry["review_id"]))
                self.assertEqual(second.status, "succeeded")
                self.assertEqual(second.result["status"], "cancelled")
                self.assertEqual(self.cancels(), before + 2)

    async def test_outcome_unknown_blocks_a_second_submission(self):
        self.backend.cancel_errors = [CancellationOutcomeUnknown("private BK-SECRET")]
        review = await self.cancel_review()
        job = await self.finish(self.service.submit(review["review_id"]))
        self.assertEqual(job.status, "outcome_unknown")
        public = job.public()
        self.assertTrue(public["reconciliation_required"])
        self.assertEqual(public["next_tool"], "reconcile_operation")
        self.assertNotIn("BK-SECRET", json.dumps(public))
        retry = await self.cancel_review()
        with self.assertRaises(AgentError):
            self.service.submit(retry["review_id"])
        self.assertEqual(self.cancels(), 1)

    async def test_durable_history_keeps_the_same_split_across_restart(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "actions.sqlite3"
        for error, blocked in (
            (CancellationPenalty("private"), False),
            (CancellationOutcomeUnknown("private"), True),
        ):
            with self.subTest(type(error).__name__):
                backend = CancelErrorBackend()
                backend.cancel_errors = [error]
                first = AgentService(
                    backend=backend, journal=ActionJournal(path), clock=lambda: self.now
                )
                review = await self.cancel_review(first)
                operation = first.submit(review["review_id"])
                await first.get(operation["operation_id"]).task
                await first.close()
                journal = ActionJournal(path)
                journal.recover(self.now)
                restarted = AgentService(
                    backend=backend, journal=journal, clock=lambda: self.now
                )
                self.addAsyncCleanup(restarted.close)
                retry = await self.cancel_review(restarted)
                if blocked:
                    with self.assertRaises((AgentError, JournalError)):
                        restarted.submit(retry["review_id"])
                    self.assertEqual(
                        [kind for kind, _ in backend.calls].count("cancel"), 1
                    )
                else:
                    job = restarted.submit(retry["review_id"])
                    await restarted.get(job["operation_id"]).task
                    self.assertEqual(
                        restarted.get(job["operation_id"]).status, "succeeded"
                    )
                    self.assertEqual(
                        [kind for kind, _ in backend.calls].count("cancel"), 2
                    )


class StandInBackend(FakeBackend):
    """My Bookings comes from the stand-in's row; cancel runs Sagasu's real
    Backend (cancel_booking, then the receipt attempt) in the stand-in browser."""

    def __init__(self, fbs):
        super().__init__()
        self.fbs = fbs
        self.real = Backend(interactive_auth=False)

    async def run(self, kind, payload, progress, notify):
        self.calls.append((kind, payload))
        if kind == "bookings":
            return {"bookings": [self.fbs.row()], "total": 1, "complete": True}
        if kind == "cancel":
            return await self.real.run(kind, payload, progress, notify)
        raise AssertionError(f"unexpected {kind}")


class CancellationServiceBrowserTests(BrowserCase):
    """The job runner plus the real browser path, end to end against the stand-in."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = patch("sagasu.mcp_service.FBSClient", self.fbs.context)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = StandInBackend(self.fbs)
        self.service = AgentService(backend=self.backend)
        self.addAsyncCleanup(self.service.close)

    async def submit(self, reason="Plans changed"):
        source = self.service.start("bookings")
        await self.service.get(source["operation_id"]).task
        review = self.service.review_cancellation(
            source["operation_id"], ROW["reference_number"], reason
        )
        operation = self.service.submit(review["review_id"])
        await self.service.get(operation["operation_id"]).task
        return self.service.get(operation["operation_id"])

    async def test_penalty_fails_then_a_new_review_cancels(self):
        await self.fbs.reset(penalty="5.00")
        job = await self.submit()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.public()["error"]["reason"], "cancellation_penalty")
        self.assert_nothing_posted()

        await self.fbs.reset()
        job = await self.submit()
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.result["status"], "cancelled")
        self.assertEqual(job.result["reference_number"], ROW["reference_number"])
        # The stand-in has no account tabs, so the receipt is unavailable,
        # which must not change the verified outcome.
        self.assertIn("receipt_error", job.result)
        self.assertEqual(self.fbs.posts, [TARGET])

    async def test_dropped_post_is_outcome_unknown_and_blocks_a_repeat(self):
        await self.fbs.reset(drop_post=True)
        with patch("sagasu.fbs_account.CANCEL_SUBMIT_MS", 1500):
            job = await self.submit()
        self.assertEqual(job.status, "outcome_unknown")
        self.assertTrue(job.public()["reconciliation_required"])
        self.assertEqual(self.fbs.posts, [TARGET])
        with self.assertRaises(AgentError):
            await self.submit()
        self.assertEqual(len(self.fbs.posts), 1)


if __name__ == "__main__":
    unittest.main()
