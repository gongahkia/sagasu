"""Synthetic stdio MCP for agent evaluations. Never opens a browser or contacts FBS."""

import argparse
import asyncio
import copy
import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from sagasu.action_journal import ActionJournal
from sagasu.fbs_client import BookingOutcomeUnknown, FBSError
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentService, Backend
from sagasu.personal import PersonalStore

ROOM_A, ROOM_B = "SCIS1 GSR 2-1", "SCIS1 GSR 2-2"
EMAIL = "fixture.person.2023@scis.smu.edu.sg"


def calendar(config):
    def room(name, spans):
        return {
            "name": name,
            "timeslots": [
                {"start": start, "end": end, "status": status, "details": None}
                for start, end, status in spans
            ],
        }

    return {
        "config": asdict(config),
        "rooms": [
            room(ROOM_A, [("12:00", "14:00", "free"), ("14:00", "16:00", "booked")]),
            room(ROOM_B, [("12:00", "14:00", "booked"), ("14:00", "16:00", "free")]),
        ],
        "scraped_at": "2099-11-01T10:00:00+08:00",
        "read_backend": {"name": "fixture"},
    }


class FixtureBackend(Backend):
    def __init__(self, scenario, trace):
        super().__init__(interactive_auth=False)
        self.scenario, self.trace = scenario, Path(trace)
        self.write_count = 0

    def log(self, kind, **metadata):
        with self.trace.open("a") as stream:
            stream.write(json.dumps({"kind": kind, **metadata}) + "\n")

    async def run(self, kind, payload, progress, notify):
        self.log(kind, **({"date": payload.date} if kind == "search" else {}))
        if kind in {"room_plan", "alternatives", "clashes"}:
            return await super().run(kind, payload, progress, notify)
        if kind == "search":
            if self.scenario == "failed_search":
                raise FBSError(
                    "Synthetic unreadable search; no availability conclusion"
                )
            return calendar(payload)
        if kind == "session":
            return {"authenticated": True}
        if kind == "bookings":
            return {
                "bookings": [
                    {
                        "reference_number": "FIXTURE-001",
                        "room": ROOM_A,
                        "date_time": "02-Nov-2099 12:00 - 14:00",
                        "fbs_status": (
                            "Pending Confirmation" if self.write_count else "Confirmed"
                        ),
                        "can_cancel": True,
                    }
                ],
                "total": 1,
                "complete": True,
            }
        if kind == "tasks":
            return {"tasks": [], "total": 0, "complete": True}
        if kind == "cobooker_search":
            people = [
                {"name": "FIXTURE ALEX TAN", "email": EMAIL},
                {
                    "name": "FIXTURE ALEX LIM",
                    "email": "fixture.person.2024@scis.smu.edu.sg",
                },
            ]
            result = {
                "query": payload["query"],
                "candidates": people,
                "total": 2,
                "complete": True,
                "source": "fbs_directory",
            }
            if payload.get("clues"):
                from sagasu.cobookers import filter_candidates

                result = filter_candidates(result, payload["clues"])
            return result
        if kind == "validate":
            return {"status": "ready", "submitted": False}
        if kind == "book":
            self.write_count += 1
            search = getattr(payload, "search", None)
            matches = (
                getattr(payload, "room", None) == ROOM_A
                and getattr(search, "date", None) == "02-Nov-2099"
                and getattr(search, "start_time", None) == "12:00"
                and getattr(search, "end_time", None) == "14:00"
                and getattr(payload, "purpose", None) == "Study"
                and tuple(
                    getattr(
                        payload, "participants", (getattr(payload, "co_booker", None),)
                    )
                )
                == (EMAIL,)
            )
            self.log(
                "native_write", count=self.write_count, target_matches_request=matches
            )
            if self.scenario == "unknown_submission":
                raise BookingOutcomeUnknown("Synthetic response lost after submission")
            return {
                "status": "verified",
                "reference_number": "FIXTURE-001",
                "fbs_status": "Pending Confirmation",
            }
        if kind == "reconcile":
            return {
                "state": "unresolved",
                "action_outcome_verified": False,
                "safe_to_retry": False,
                "complete": True,
            }
        # An unimplemented fixture path must fail here, never fall through to native FBS.
        raise RuntimeError("Operation is not available in the isolated fixture backend")


async def serve(scenario, trace):
    with tempfile.TemporaryDirectory(prefix="sagasu-agent-fixture-") as temporary:
        root = Path(temporary)
        service = AgentService(
            backend=FixtureBackend(scenario, trace),
            profile=PersonalStore(root / "personal.sqlite3"),
            journal=ActionJournal(root / "actions.sqlite3"),
        )
        server = create_server(
            service,
            allow_agent_submission=True,
            configuration_provider=lambda: {
                "operational": True,
                "mode": "operational",
                "authentication": {"session_valid": True},
                "checks": [],
            },
        )
        try:
            await server.run_stdio_async()
        finally:
            await service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--trace", required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.scenario, args.trace))
