"""Preference ranking and bounded alternative plans preserve user constraints."""

import copy
import unittest
from dataclasses import asdict
from unittest.mock import AsyncMock, patch
from mcp import Client
from sagasu.alternatives import candidates, find_alternatives
from sagasu.fbs_client import (
    SearchConfig,
    SearchResult,
    RoomAvailability,
    TimeSlot,
    FBSError,
)
from sagasu.mcp_service import Backend, AgentError
from sagasu.mcp_server import create_server
from tests.test_mcp import ServiceCase, CONFIG, ROOM


class AlternativeTests(ServiceCase):
    async def source(self, horizon=False):
        job = await self.finish(self.service.search(CONFIG))
        if horizon:
            job.result["rooms"] = [
                {
                    "name": "A",
                    "timeslots": [
                        {
                            "start": "14:00",
                            "end": "16:00",
                            "status": "outside_booking_horizon",
                        }
                    ],
                }
            ]
        return job

    async def test_preference_ranking_precedes_pagination_with_explanations(self):
        job = await self.source()
        job.result["rooms"] = [
            {**copy.deepcopy(ROOM), "name": name} for name in ["A", "Preferred", "Z"]
        ]
        result = self.service.available_rooms(
            job.id, limit=1, preferred_rooms=["Z", "Preferred"]
        )
        self.assertEqual(result["rooms"][0]["name"], "Z")
        self.assertIn("Explicit room preference 1", result["rooms"][0]["match_reasons"])
        self.assertEqual(result["available_total"], 3)
        self.assertEqual(result["scraped_at"], job.result["scraped_at"])

    async def test_horizon_returns_wait_action_and_default_alternatives_do_not_open_browser(
        self,
    ):
        job = await self.source(True)
        result = self.service.available_rooms(job.id)
        self.assertEqual(result["suggested_action"], "wait_for_booking_window")
        self.assertIsNone(result["next_tool"])
        with patch("sagasu.mcp_service.FBSClient") as browser:
            result = await Backend().run(
                "alternatives", {"source": job.result, "configs": []}, None, None
            )
        browser.assert_not_called()
        self.assertTrue(result["booking_horizon"]["all_checked_rooms_affected"])
        self.assertEqual(result["probes"], [])

    async def test_probe_generation_is_bounded_and_keeps_duration_date_and_other_filters(
        self,
    ):
        job = await self.source()
        job.result["config"].update(
            buildings=["Li Ka Shing Library"],
            floors=["Level 5"],
            facility_types=["Project Room"],
            capacity="From6To10Pax",
        )
        choices = candidates(job.result, 120, buildings=["School of Accountancy"])
        self.assertEqual(len(choices), 6)
        self.assertEqual(choices[0].start_time, "14:00")
        for choice in choices:
            self.assertEqual(choice.date, CONFIG["date"])
            self.assertEqual(choice.end_time[:2], str(int(choice.start_time[:2]) + 2))
            self.assertEqual(choice.buildings, ("School of Accountancy",))
            self.assertEqual(choice.floors, ("Level 5",))
            self.assertEqual(choice.facility_types, ("Project Room",))
            self.assertEqual(choice.capacity, "From6To10Pax")
        self.assertEqual(candidates(job.result), [])

    async def test_failed_probe_does_not_hide_other_results_or_echo_page_text(self):
        job = await self.source()
        configs = candidates(job.result, 30)
        browser = AsyncMock()
        browser.search.side_effect = [
            FBSError("PRIVATE"),
            SearchResult(
                configs[1],
                [
                    RoomAvailability(
                        "B",
                        [TimeSlot(configs[1].start_time, configs[1].end_time, "free")],
                    )
                ],
                "now",
            ),
        ]
        result = await find_alternatives(
            browser, {"source": job.result, "configs": configs}
        )
        self.assertEqual(
            [p["status"] for p in result["probes"]], ["failed", "succeeded"]
        )
        self.assertEqual(result["probes"][1]["available_rooms"], ["B"])
        self.assertNotIn("PRIVATE", str(result))

    async def test_mcp_exposes_wait_action_and_preference_ranking(self):
        job = await self.source(True)
        async with Client(create_server(self.service)) as client:
            response = await client.call_tool(
                "available_rooms", {"operation_id": job.id}
            )
            self.assertFalse(response.is_error)
            self.assertEqual(
                response.structured_content["suggested_action"],
                "wait_for_booking_window",
            )
            tools = {t.name for t in (await client.list_tools()).tools}
            self.assertIn("search_alternatives", tools)
        for shift in (-30, 1, 121, True):
            with self.assertRaises(AgentError):
                self.service.search_alternatives(job.id, shift)
