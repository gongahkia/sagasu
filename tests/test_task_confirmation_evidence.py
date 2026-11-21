"""A vanished task needs positive confirmation evidence, not absence alone."""

from unittest.mock import AsyncMock, patch

from sagasu.fbs_tasks import confirm_task
from tests.test_task_confirm import CONFIRMED, TASK, StandInCase


class ConfirmationEvidenceTests(StandInCase):
    async def test_removed_task_with_positive_acknowledgement_is_confirmed(self):
        listing = self.s.listing

        def acknowledged_listing():
            body = listing()
            if self.s.posts:
                message = (
                    '<div id="MessagePanel"><span id="MessageLabel">'
                    f'Booking {TASK["reference_number"]} confirmed successfully'
                    "</span></div>"
                )
                body = body.replace("</form>", message + "</form>")
            return body

        with (
            patch.object(self.s, "listing", side_effect=acknowledged_listing),
            patch("sagasu.fbs_account.read_bookings", new=AsyncMock()) as bookings,
        ):
            result = await confirm_task(self.s.client, TASK)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(self.s.posts, CONFIRMED)
        bookings.assert_not_awaited()

    async def test_removed_task_without_a_booking_is_not_confirmed(self):
        with patch(
            "sagasu.fbs_account.read_bookings",
            new=AsyncMock(return_value={"bookings": [], "complete": True}),
        ):
            await self.unknown()

    async def test_removed_task_with_rejected_booking_is_not_confirmed(self):
        with patch(
            "sagasu.fbs_account.read_bookings",
            new=AsyncMock(
                return_value={
                    "bookings": [
                        {
                            "reference_number": TASK["reference_number"],
                            "fbs_status": "Rejected",
                        }
                    ],
                    "complete": True,
                }
            ),
        ):
            await self.unknown()
