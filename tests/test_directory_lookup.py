"""Standalone lookup contexts and filtering never invent directory identities."""

import asyncio
import unittest
from dataclasses import asdict
from datetime import datetime
from unittest.mock import AsyncMock, patch

from sagasu.cobookers import (
    CobookerLookupUnavailable,
    directory_context,
    filter_candidates,
    validate_clues,
)
from sagasu.fbs_client import AuthenticationRequired, FBS_TIMEZONE, LayoutChanged
from sagasu.mcp_service import AgentError, Backend
from tests.test_mcp import Client, ServiceCase

if Client is not None:
    from sagasu.mcp_server import create_server


NOW = datetime(2099, 11, 1, 23, 15, tzinfo=FBS_TIMEZONE)


def result(config, status="free"):
    return {
        "config": asdict(config),
        "scraped_at": NOW.isoformat(),
        "rooms": [
            {
                "name": "Room",
                "timeslots": [{"start": "08:00", "end": "09:00", "status": status}],
            }
        ],
    }


class DirectoryClueTests(unittest.TestCase):
    def test_filters_verified_emails_without_guessing_or_changing_names(self):
        candidates = [
            {"name": "SAME NAME", "email": "person.2023@scis.smu.edu.sg"},
            {"name": "SAME NAME", "email": "person.2024@scis.smu.edu.sg"},
            {"name": "SAME NAME", "email": "person.2023@business.smu.edu.sg"},
            {"name": "SAME NAME", "email": "person@scis.smu.edu.sg"},
        ]
        source = {"candidates": candidates, "total": 4, "complete": True}
        filtered = filter_candidates(source, validate_clues("SCIS", 2023))
        self.assertEqual(filtered["candidates"], candidates[:1])
        self.assertEqual(filtered["directory_total"], 4)
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["unclassified_candidates_read"], 1)
        self.assertEqual(len(source["candidates"]), 4)
        self.assertIn("do not independently establish", filtered["clue_basis"])

    def test_partial_grid_never_claims_a_complete_match_count(self):
        source = {
            "candidates": [{"name": "Name", "email": "name.2023@smu.edu.sg"}],
            "total": 30,
            "complete": False,
            "coverage_reason": "page_limit",
        }
        filtered = filter_candidates(source, validate_clues("smu", 2023))
        self.assertIsNone(filtered["total"])
        self.assertFalse(filtered["complete"])
        self.assertEqual(filtered["matching_candidates_read"], 1)
        self.assertEqual(filtered["directory_total"], 30)
        self.assertEqual(filtered["coverage_reason"], "page_limit")

    def test_email_year_tokens_are_bounded_and_no_clues_preserves_output(self):
        source = {
            "candidates": [{"name": "Name", "email": "name2023@smu.edu.sg"}],
            "total": 1,
            "complete": True,
        }
        self.assertEqual(
            filter_candidates(source, validate_clues())["candidates"],
            source["candidates"],
        )
        self.assertEqual(
            filter_candidates(source, validate_clues(year=2023))["candidates"], []
        )
        for school, year in [
            ("SCIS 2023", None),
            ("scis.smu.edu.sg", None),
            (None, True),
            (None, "2023"),
            (None, 100),
            ([], None),
        ]:
            with self.subTest(school=school, year=year), self.assertRaises(ValueError):
                validate_clues(school, year)


class DirectoryContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_day_uses_next_day_and_selects_only_a_free_half_hour(self):
        read = AsyncMock(side_effect=result)
        context = await directory_context(read, AsyncMock(), now=NOW)
        self.assertEqual(context["search"].date, "02-Nov-2099")
        self.assertEqual(context["search"].start_time, "08:00")
        self.assertEqual(context["search"].end_time, "08:30")
        self.assertEqual(context["room"], "Room")
        self.assertEqual(context["context_probes"], 1)
        self.assertEqual(read.await_count, 1)

    async def test_today_skips_elapsed_free_cells_and_probes_are_bounded(self):
        now = NOW.replace(hour=14, minute=45)
        read = AsyncMock(side_effect=result)
        context = await directory_context(read, AsyncMock(), now=now)
        self.assertEqual(context["search"].date, "02-Nov-2099")
        self.assertEqual(context["context_probes"], 3)
        self.assertEqual(read.await_args_list[0].args[0].start_time, "15:00")
        unavailable = AsyncMock(side_effect=lambda config: result(config, "booked"))
        with self.assertRaises(CobookerLookupUnavailable):
            await directory_context(unavailable, AsyncMock(), now=now)
        self.assertEqual(unavailable.await_count, 4)

    async def test_read_failure_never_becomes_person_absence_and_auth_cancel_propagate(
        self,
    ):
        read = AsyncMock(side_effect=LayoutChanged("PRIVATE CONTENT"))
        with self.assertRaises(CobookerLookupUnavailable):
            await directory_context(read, AsyncMock(), now=NOW)
        self.assertEqual(read.await_count, 2)
        for error in [
            AuthenticationRequired("Session expired"),
            asyncio.CancelledError(),
        ]:
            with self.subTest(error=error), self.assertRaises(type(error)):
                await directory_context(
                    AsyncMock(side_effect=error), AsyncMock(), now=NOW
                )

    async def test_backend_finishes_auto_search_before_opening_picker_browser(self):
        backend = Backend(interactive_auth=False)
        backend.reads.direct = AsyncMock(side_effect=result)
        client = AsyncMock()
        events = []

        async def context(read, progress):
            events.append("context")
            value = await directory_context(read, progress, now=NOW)
            events.append("context_ready")
            return value

        async def enter():
            events.append("browser")
            return client

        client.__aenter__.side_effect = enter
        directory = {"candidates": [], "total": 0, "complete": True}
        # the direct router receives kind/config/progress, rather than just config.
        backend.reads.direct.side_effect = lambda kind, config, progress: result(config)
        with (
            patch("sagasu.cobookers.directory_context", side_effect=context),
            patch("sagasu.mcp_service.FBSClient", return_value=client),
            patch(
                "sagasu.cobookers.search_cobookers", AsyncMock(return_value=directory)
            ) as picker,
        ):
            output = await backend.run(
                "cobooker_search",
                {"search": None, "room": None, "query": "Name"},
                AsyncMock(),
                AsyncMock(),
            )
        self.assertEqual(events, ["context", "context_ready", "browser"])
        self.assertEqual(picker.await_args.args[1].start_time, "08:00")
        self.assertEqual(output["lookup_context"]["purpose"], "directory_lookup_only")
        self.assertFalse(output["lookup_context"]["submitted"])


@unittest.skipIf(Client is None, "Install Sagasu for MCP protocol tests")
class StandaloneDirectoryProtocolTests(ServiceCase):
    async def test_partial_name_and_clues_start_one_job_without_search_or_contact(self):
        self.backend.run = AsyncMock(
            return_value={"candidates": [], "total": 0, "complete": True}
        )
        async with Client(create_server(self.service)) as client:
            response = await client.call_tool(
                "search_cobookers",
                {"query": "Wee Kiat", "school": "SCIS", "year": 2023},
            )
            await self.finish(response.structured_content)
        kind, payload = self.backend.run.await_args.args[:2]
        self.assertEqual(kind, "cobooker_search")
        self.assertIsNone(payload["search"])
        self.assertEqual(payload["clues"], {"school": "scis", "year": 2023})
        self.assertEqual(self.service.reviews, {})
        self.assertIsNone(self.service.profile)

    async def test_mixed_context_inputs_and_invalid_clues_fail_before_queueing(self):
        self.backend.run = AsyncMock()
        for kwargs in [
            {"room": "Room"},
            {"search_operation_id": "job"},
            {"school": "unknown"},
            {"year": True},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(AgentError):
                self.service.search_cobookers(query="Name", **kwargs)
        self.backend.run.assert_not_awaited()
