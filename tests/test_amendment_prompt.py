"""FBS's penalty prompt on the amend form's CONFIRM button.

On 20 Nov 2026 an amendment against the real FBS clicked CONFIRM, posted
nothing and waited 45 s: CONFIRM asks a browser confirm() ("A Cancellation
Penalty of [0.00] will be imposed...") and Playwright dismisses unanswered
prompts. The stand-in (tests/amendment_fixture.py) now guards CONFIRM the same
way. Only the stand-in's routes are reached.
"""

import asyncio

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sagasu import fbs_prompts
from sagasu.action_journal import ActionJournal
from sagasu.amendments import (
    AmendmentOutcomeUnknown,
    AmendmentPenalty,
    AmendmentUnavailable,
    execute_amendment,
    open_edit,
    prepare_amendment,
    read_amendment_options,
)
from sagasu.errors import public_failure
from sagasu.fbs_prompts import PromptDeclined, expected_prompt
from sagasu.mcp_service import AgentService, Backend
from tests.amendment_fixture import (
    PROMPT,
    SOURCE,
    ZERO_PENALTY_PROMPT,
    AmendmentFixture,
)

KEY = b"k" * 32
# Seconds to wait for a prompt that never comes, and milliseconds to wait for
# a POST response that never comes. Both keep headroom for a loaded suite.
PROMPT_WAIT = 2.0
RESPONSE_WAIT_MS = 1500
# CONFIRM's onclick exactly as FBS served it on 20 Nov 2026.
REAL_ONCLICK = (
    "if(!confirm('A Cancellation Penalty of [0.00] will be imposed. Please click "
    "Ok if you wish to proceed'))return false;btn.cl(this);"
    r"setTimeout('__doPostBack(\'panel$UIButton2$UIButton2_bt\',\'\')', 0);"
    "return false;"
)


class AmendmentPromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = AmendmentFixture()
        await self.fixture.setup(self)
        # Each prompt Sagasu handled: (message it would accept, accepted, declined).
        self.answered = []
        real = fbs_prompts.click_and_accept

        async def spy(page, button, answer, **kwargs):
            # click_and_accept binds PROMPT_SECONDS as a default argument, so
            # patching the module constant does not shorten it; pass it here.
            kwargs.setdefault("seconds", PROMPT_WAIT)
            try:
                return await real(page, button, answer, **kwargs)
            finally:
                self.answered.append((answer.message, answer.accepted, answer.declined))

        for name, value in [
            ("sagasu.amendments.click_and_accept", spy),
            ("sagasu.fbs_prompts.PROMPT_SECONDS", PROMPT_WAIT),
            (
                "sagasu.amendments.capture_booking_evidence",
                AsyncMock(return_value="receipt"),
            ),
        ]:
            mocked = patch(name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.checkpoint = Mock()
        mocked = patch("sagasu.amendments.checkpoint_verified", self.checkpoint)
        mocked.start()
        self.addCleanup(mocked.stop)

    async def payload(self, changes=None):
        native = await read_amendment_options(self.fixture.client, "BK-TEST")
        return prepare_amendment(
            native, changes or {"purpose": "Changed purpose"}, KEY
        )

    async def submit(self, payload):
        return await execute_amendment(
            payload, submit=True, progress=AsyncMock(), auth_notify=AsyncMock()
        )

    def assert_nothing_clicked(self):
        self.assertEqual(self.fixture.posts, 0)
        self.assertNotIn("CONFIRM_CLICK", self.fixture.actions)
        self.assertNotIn("CONFIRM", self.fixture.actions)
        self.assertEqual(self.answered, [])
        self.assertEqual(self.fixture.saved, SOURCE)
        self.checkpoint.assert_not_called()

    def reset_confirm(self):
        self.fixture.prompt = ZERO_PENALTY_PROMPT
        self.fixture.onclick = None
        self.fixture.shown_prompt = None
        self.fixture.alert_first = None
        self.fixture.silent_confirm = False
        self.fixture.actions.clear()
        self.answered.clear()

    # -- the stand-in matches FBS ------------------------------------------------

    async def test_stand_in_confirm_is_guarded_like_fbs(self):
        self.assertEqual(expected_prompt(REAL_ONCLICK), ZERO_PENALTY_PROMPT)
        frame, _ = await open_edit(self.fixture.client, "BK-TEST")
        onclick = await frame.locator("a#panel_UIButton2").get_attribute("onclick")
        self.assertEqual(expected_prompt(onclick), ZERO_PENALTY_PROMPT)
        self.assertEqual(self.fixture.posts, 0)

    async def test_plain_click_on_guarded_confirm_posts_nothing_as_on_20_nov(self):
        # What the code before the fix did: click CONFIRM with no dialog
        # listener. Playwright dismisses the prompt and nothing is posted,
        # exactly as FBS behaved on 20 Nov. The stand-in must reproduce it.
        from sagasu.amendments import open_edit

        frame, _ = await open_edit(self.fixture.client, "BK-TEST")
        await frame.locator("a#panel_UIButton2:visible").click()
        await asyncio.sleep(1)
        self.assertIn("CONFIRM_CLICK", self.fixture.actions)
        self.assertNotIn("CONFIRM", self.fixture.actions)
        self.assertEqual(self.fixture.posts, 0)
        self.assertEqual(self.fixture.saved, SOURCE)

    async def test_missing_prompt_is_refused_not_clicked(self):
        payload = await self.payload()
        with patch("sagasu.amendments.expected_prompt", lambda onclick: None):
            with self.assertRaises(AmendmentUnavailable) as caught:
                await self.submit(payload)
        self.assertNotIsInstance(caught.exception, AmendmentOutcomeUnknown)
        self.assertNotIn("CONFIRM_CLICK", self.fixture.actions)
        self.assertEqual(self.fixture.posts, 0)

    # -- the zero-penalty prompt ---------------------------------------------------

    async def test_zero_penalty_prompt_is_answered_ok_and_amends_once(self):
        payload = await self.payload()
        result = await self.submit(payload)
        self.assertEqual(result["status"], "verified_amendment")
        self.assertTrue(result["submitted"])
        self.assertEqual(result["fbs_status"], "Pending Confirmation")
        self.assertEqual(self.fixture.posts, 1)
        self.assertEqual(self.fixture.actions.count("CONFIRM_CLICK"), 1)
        self.assertEqual(self.fixture.actions.count("CONFIRM"), 1)
        self.assertEqual(self.answered, [(ZERO_PENALTY_PROMPT, True, False)])
        self.assertEqual(self.fixture.saved["purpose"], "Changed purpose")
        self.checkpoint.assert_called_once()

    async def test_page_without_prompt_is_refused_before_clicking(self):
        # FBS's CONFIRM asked the penalty prompt on 20 Nov. A button without
        # it gives no way to check the penalty, so nothing is submitted.
        self.fixture.prompt = None
        payload = await self.payload()
        with self.assertRaises(AmendmentUnavailable) as caught:
            await self.submit(payload)
        self.assertNotIsInstance(caught.exception, AmendmentOutcomeUnknown)
        self.assertEqual(self.fixture.posts, 0)
        self.assertEqual(self.fixture.actions.count("CONFIRM_CLICK"), 0)
        self.assertEqual(self.answered, [])
        self.checkpoint.assert_not_called()

    async def test_dropped_post_after_ok_is_outcome_unknown(self):
        payload = await self.payload()
        self.fixture.drop_response = True
        with (
            patch("sagasu.amendments.CONFIRM_TIMEOUT_MS", RESPONSE_WAIT_MS),
            self.assertRaises(AmendmentOutcomeUnknown),
        ):
            await self.submit(payload)
        self.assertEqual(self.fixture.posts, 1)
        self.assertEqual(self.answered, [(ZERO_PENALTY_PROMPT, True, False)])
        self.checkpoint.assert_not_called()

    # -- prompts read from the button and refused before clicking -------------------

    async def test_penalty_is_refused_before_clicking(self):
        payload = await self.payload()
        for penalty in ["12.50", "0.01", "1"]:
            with self.subTest(penalty=penalty):
                self.reset_confirm()
                self.fixture.prompt = PROMPT.format(penalty=penalty)
                with self.assertRaises(AmendmentPenalty) as caught:
                    await self.submit(payload)
                error = caught.exception
                self.assertIsInstance(error, AmendmentUnavailable)
                self.assertNotIsInstance(error, AmendmentOutcomeUnknown)
                self.assertEqual(error.reason, "amendment_penalty")
                self.assertEqual(public_failure(error)["reason"], "amendment_penalty")
                self.assert_nothing_clicked()

    async def test_unrecognised_prompt_is_refused_before_clicking(self):
        payload = await self.payload()
        for text in [
            "Are you sure you want to amend this booking?",
            ZERO_PENALTY_PROMPT + " and your credits will be forfeited",
            PROMPT.format(penalty="0.00 SGD"),
            PROMPT.format(penalty="0.001"),
            ZERO_PENALTY_PROMPT.replace("Penalty", "Fee"),
        ]:
            with self.subTest(text=text):
                self.reset_confirm()
                self.fixture.prompt = text
                with self.assertRaises(AmendmentUnavailable) as caught:
                    await self.submit(payload)
                error = caught.exception
                self.assertNotIsInstance(error, AmendmentPenalty)
                self.assertEqual(error.reason, "amendment_unavailable")
                self.assert_nothing_clicked()

    async def test_changed_confirmation_step_is_refused_before_clicking(self):
        payload = await self.payload()
        guard = f"if(!confirm('{ZERO_PENALTY_PROMPT}'))return false;"
        for onclick in [
            guard + guard + "submitEdit();return false",
            f"confirm('{ZERO_PENALTY_PROMPT}');submitEdit();return false",
            f'if(!confirm("{ZERO_PENALTY_PROMPT}"))return false;submitEdit();return false',
            guard + "if(confirm('Also email everyone?'))notify();submitEdit();return false",
        ]:
            with self.subTest(onclick=onclick):
                self.reset_confirm()
                self.fixture.onclick = onclick
                with self.assertRaises(AmendmentUnavailable) as caught:
                    await self.submit(payload)
                self.assertNotIsInstance(caught.exception, AmendmentPenalty)
                self.assert_nothing_clicked()

    def test_expected_prompt_reads_only_the_exact_guard(self):
        self.assertIsNone(expected_prompt(None))
        self.assertIsNone(expected_prompt("submitEdit();return false"))
        self.assertEqual(
            expected_prompt(
                f"if ( ! confirm( '{PROMPT.format(penalty='0')}' ) ) return false ;x()"
            ),
            PROMPT.format(penalty="0"),
        )
        with self.assertRaises(PromptDeclined) as caught:
            expected_prompt(
                f"if(!confirm('{PROMPT.format(penalty='9999999.99')}'))return false;"
            )
        self.assertTrue(caught.exception.penalty)
        with self.assertRaises(PromptDeclined) as caught:
            expected_prompt("if(!confirm('Proceed?'))return false;")
        self.assertFalse(caught.exception.penalty)

    # -- a different dialog after the click ---------------------------------------

    async def test_other_dialog_is_dismissed_and_nothing_is_posted(self):
        payload = await self.payload()
        for knob, value in [
            ("shown_prompt", "Delete this booking?"),
            ("shown_prompt", PROMPT.format(penalty="12.50")),
            ("alert_first", "Your session is about to expire"),
        ]:
            with self.subTest(knob=knob, value=value):
                self.reset_confirm()
                setattr(self.fixture, knob, value)
                with self.assertRaises(AmendmentUnavailable) as caught:
                    await self.submit(payload)
                self.assertNotIsInstance(caught.exception, AmendmentOutcomeUnknown)
                self.assertNotIsInstance(caught.exception, AmendmentPenalty)
                self.assertIn("CONFIRM_CLICK", self.fixture.actions)
                self.assertNotIn("CONFIRM", self.fixture.actions)
                self.assertEqual(self.fixture.posts, 0)
                self.assertEqual(self.answered, [(ZERO_PENALTY_PROMPT, False, True)])
                self.assertEqual(self.fixture.saved, SOURCE)
                self.checkpoint.assert_not_called()

    async def test_click_that_posts_without_a_dialog_is_not_reported_as_nothing_submitted(
        self,
    ):
        # Regression: "no dialog appeared" used to mean "nothing submitted".
        # Only a dialog Sagasu dismissed proves that; if confirm() returns true
        # without a dialog, the click posts and must be verified like any post.
        payload = await self.payload()
        self.fixture.silent_confirm = True
        with patch("sagasu.amendments.CONFIRM_TIMEOUT_MS", RESPONSE_WAIT_MS):
            result = await self.submit(payload)
        self.assertEqual(self.fixture.posts, 1)
        self.assertEqual(self.fixture.saved["purpose"], "Changed purpose")
        self.assertEqual(self.answered, [(ZERO_PENALTY_PROMPT, False, False)])
        self.assertEqual(result["status"], "verified_amendment")

    async def test_click_with_no_dialog_and_no_post_is_uncertain_not_unavailable(
        self,
    ):
        payload = await self.payload()
        self.fixture.prompt = ZERO_PENALTY_PROMPT
        self.fixture.onclick = "return false"  # the click does nothing at all
        with patch("sagasu.amendments.CONFIRM_TIMEOUT_MS", RESPONSE_WAIT_MS), patch(
            "sagasu.amendments.expected_prompt", lambda onclick: ZERO_PENALTY_PROMPT
        ):
            with self.assertRaises(AmendmentOutcomeUnknown):
                await self.submit(payload)
        self.assertEqual(self.fixture.posts, 0)


class AmendmentPromptServiceTests(unittest.IsolatedAsyncioTestCase):
    """The prompt's outcome through AgentService, as an agent's job sees it."""

    async def asyncSetUp(self):
        self.fixture = AmendmentFixture()
        await self.fixture.setup(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.journal = ActionJournal(Path(temporary.name) / "actions.sqlite3")
        self.service = AgentService(
            backend=Backend(interactive_auth=False), journal=self.journal
        )
        self.addAsyncCleanup(self.service.close)
        mocked = patch(
            "sagasu.amendments.capture_booking_evidence",
            AsyncMock(return_value="receipt"),
        )
        mocked.start()
        self.addCleanup(mocked.stop)

    async def run_amendment(self, changes):
        native = self.service.amendment_options("BK-TEST")
        await self.service.get(native["operation_id"]).task
        review = self.service.review_amendment(native["operation_id"], changes)
        with patch("sagasu.amendments.CONFIRM_TIMEOUT_MS", RESPONSE_WAIT_MS):
            submitted = self.service.submit(review["review_id"])
            job = self.service.get(submitted["operation_id"])
            await job.task
        return job

    def assert_failed_not_unknown(self, job, reason):
        self.assertEqual(job.status, "failed")
        self.assertNotEqual(job.status, "outcome_unknown")
        self.assertEqual(job.error["reason"], reason)
        self.assertNotEqual(job.error["next_tool"], "reconcile_operation")
        public = job.public()
        self.assertFalse(public["reconciliation_required"])
        self.assertEqual(public["error"]["reason"], reason)
        record = self.journal.get(job.id)
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["reconciliation_required"])

    async def test_penalty_refusal_fails_the_job_with_public_reason_amendment_penalty(
        self,
    ):
        self.fixture.prompt = PROMPT.format(penalty="12.50")
        before = copy.deepcopy(self.fixture.saved)
        job = await self.run_amendment({"purpose": "PRIVATE PURPOSE"})
        self.assert_failed_not_unknown(job, "amendment_penalty")
        self.assertEqual(job.error["code"], "AmendmentPenalty")
        self.assertFalse(job.error["retry_safe"])
        self.assertEqual(self.fixture.posts, 0)
        self.assertNotIn("CONFIRM_CLICK", self.fixture.actions)
        self.assertEqual(self.fixture.saved, before)

    async def test_unrecognised_prompt_fails_the_job_as_amendment_unavailable(self):
        self.fixture.prompt = "Are you sure you want to amend this booking?"
        job = await self.run_amendment({"purpose": "Changed purpose"})
        self.assert_failed_not_unknown(job, "amendment_unavailable")
        self.assertEqual(self.fixture.posts, 0)
        self.assertNotIn("CONFIRM_CLICK", self.fixture.actions)

    async def test_dismissed_other_dialog_fails_the_job_not_outcome_unknown(self):
        self.fixture.shown_prompt = "Delete this booking?"
        job = await self.run_amendment({"purpose": "Changed purpose"})
        self.assert_failed_not_unknown(job, "amendment_unavailable")
        self.assertIn("CONFIRM_CLICK", self.fixture.actions)
        self.assertEqual(self.fixture.posts, 0)

    async def test_zero_penalty_prompt_succeeds_through_the_service(self):
        job = await self.run_amendment({"purpose": "Changed purpose"})
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.result["status"], "verified_amendment")
        self.assertEqual(self.fixture.posts, 1)
        self.assertEqual(self.journal.get(job.id)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
