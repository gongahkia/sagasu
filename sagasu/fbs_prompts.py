"""FBS's native confirmation prompt on its write buttons.

FBS guards "Confirm Cancellation" (cancel) and "Confirm" (amend) with a browser
confirm(): "A Cancellation Penalty of [0.00] will be imposed. Please click Ok if
you wish to proceed". Playwright dismisses unanswered prompts, and a dismissed
prompt makes the button return before FBS posts anything. So the prompt is read
from the button before clicking, and only that exact prompt with a zero penalty
is answered OK. Every other prompt is dismissed, which submits nothing.
"""

import asyncio
import re
from decimal import Decimal

PENALTY = re.compile(
    r"A Cancellation Penalty of \[(\d{1,7}(?:\.\d{1,2})?)\] will be imposed\. "
    r"Please click Ok if you wish to proceed"
)
CALL = re.compile(r"\bconfirm\s*\(")
GUARD = re.compile(
    r"if\s*\(\s*!\s*confirm\s*\(\s*'([^'\\]*)'\s*\)\s*\)\s*return\s+false\s*;"
)
PROMPT_SECONDS = 15


class PromptDeclined(Exception):
    """FBS would ask something Sagasu does not accept. Nothing was submitted."""

    def __init__(self, message, *, penalty=False):
        super().__init__(message)
        self.penalty = penalty


def expected_prompt(onclick):
    """The exact prompt this button shows, or None when it shows none."""
    onclick = onclick or ""
    calls = CALL.findall(onclick)
    if not calls:
        return None
    guards = GUARD.findall(onclick)
    if len(guards) != 1 or len(calls) != 1:
        raise PromptDeclined("FBS's confirmation step has changed")
    match = PENALTY.fullmatch(guards[0])
    if match is None:
        raise PromptDeclined("FBS asks an unrecognised question before submitting")
    if Decimal(match[1]) != 0:
        raise PromptDeclined("FBS would impose a cancellation penalty", penalty=True)
    return guards[0]


class Answer:
    """Whether the prompt was answered OK. Set before OK reaches the page."""

    def __init__(self, message):
        self.message = message
        self.accepted = False
        self.declined = False


async def click_and_accept(page, button, answer, *, seconds=None):
    """Click, and answer OK only to answer.message.

    answer.accepted becomes True before OK is sent, so a caller that sees an
    exception can still tell whether FBS may have received the submission.
    Only answer.declined (a dialog Sagasu dismissed) proves FBS posted
    nothing; with no dialog at all, a post cannot be ruled out.
    """
    message = answer.message
    seconds = PROMPT_SECONDS if seconds is None else seconds
    decided = asyncio.Event()

    async def handle(dialog):
        if (
            not decided.is_set()
            and dialog.type == "confirm"
            and dialog.message == message
        ):
            answer.accepted = True
            decided.set()
            await dialog.accept()
            return
        if not answer.accepted:
            answer.declined = True
        decided.set()
        await dialog.dismiss()

    page.on("dialog", handle)
    try:
        await button.click()
        try:
            await asyncio.wait_for(decided.wait(), seconds)
        except TimeoutError:
            pass
    finally:
        # Decide before the listener goes: a handler Playwright has already
        # queued (it runs listeners as tasks) must now dismiss, never accept,
        # so answer.accepted is final once this returns or raises.
        decided.set()
        page.remove_listener("dialog", handle)
