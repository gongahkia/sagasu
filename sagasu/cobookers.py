"""Live, bounded directory reads through FBS's unsubmitted co-booker picker."""

import re
from dataclasses import replace
from datetime import datetime, timedelta

from playwright.async_api import Error as PlaywrightError

from .fbs_client import (
    FBSError,
    FBS_URL,
    BookingUnavailable,
    AuthenticationRequired,
    FBS_TIMEZONE,
    LayoutChanged,
    SearchConfig,
    minutes_to_time,
    require_future_booking,
)
from .fbs_http import fbs_document_url
from .fbs_paging import collect_pages
from .observability import observed_step
from .personal import SMU_EMAIL_RE

GRID = "bookingFormControl1_DialogSearchCoBooker_searchPanel_gridView"


class CobookerLookupUnavailable(FBSError):
    default_reason = "cobooker_lookup_unavailable"


def validate_query(query):
    if (
        not isinstance(query, str)
        or any(ord(char) < 32 or ord(char) == 127 for char in query)
        or not 2 <= len(query.strip()) <= 100
        or not any(char.isalnum() for char in query)
    ):
        raise ValueError(
            "Use a partial name or SMU email of 2–100 single-line characters."
        )
    return " ".join(query.split())


def validate_clues(school=None, year=None):
    domains = {
        "smu",
        "scis",
        "sis",
        "computing",
        "law",
        "business",
        "accountancy",
        "economics",
        "socsc",
    }
    if school is not None:
        if not isinstance(school, str) or school.strip().casefold() not in domains:
            raise ValueError(
                "Choose an SMU email domain: scis, sis, computing, law, business, accountancy, economics, socsc or smu."
            )
        school = school.strip().casefold()
    if year is not None and (type(year) is not int or not 1900 <= year <= 2199):
        raise ValueError("Use a four-digit year clue from 1900 to 2199.")
    return {"school": school, "year": year}


def filter_candidates(result, clues):
    if not any(value is not None for value in clues.values()):
        return result
    matched, unclassified = [], 0
    for candidate in result["candidates"]:
        local, domain = candidate["email"].casefold().split("@")
        year = re.search(r"(?:^|[._-])(\d{4})$", local)
        if clues["year"] is not None and year is None:
            unclassified += 1
            continue
        expected_domain = (
            "smu.edu.sg"
            if clues["school"] == "smu"
            else f"{clues['school']}.smu.edu.sg"
        )
        if clues["school"] is not None and domain != expected_domain:
            continue
        if clues["year"] is not None and int(year[1]) != clues["year"]:
            continue
        matched.append(candidate)
    return {
        **result,
        "candidates": matched,
        "directory_total": result["total"],
        "total": len(matched) if result["complete"] else None,
        "matching_candidates_read": len(matched),
        "unclassified_candidates_read": unclassified,
        "clues": clues,
        "clue_basis": "Exact verified email domain and trailing four-digit email token; these do not independently establish school membership or enrolment year.",
    }


async def directory_context(read, progress, *, now=None):
    """Find a bounded, disposable picker context without changing a booking request."""
    from .alternatives import result_from_dict

    now = now or datetime.now(FBS_TIMEZONE)
    probes = 0
    for offset in (0, 1):
        start = (
            max(480, (now.hour * 60 + now.minute) // 30 * 30 + 30)
            if offset == 0
            else 480
        )
        if start >= 1410:
            continue
        for building in (
            "School of Computing & Information Systems 1",
            "Li Ka Shing Library",
        ):
            config = SearchConfig(
                (now.date() + timedelta(days=offset)).strftime("%d-%b-%Y"),
                minutes_to_time(start),
                "23:30",
                buildings=(building,),
                facility_types=("Group Study Room", "Project Room"),
            )
            probes += 1
            await progress("finding a temporary unsent form for directory lookup only")
            try:
                snapshot = result_from_dict(await read(config))
                if snapshot.config != config:
                    raise LayoutChanged("The directory context search changed scope")
                for room in snapshot.rooms:
                    for minute in range(start, 1410, 30):
                        begin, end = minutes_to_time(minute), minutes_to_time(
                            minute + 30
                        )
                        if room.is_free(begin, end):
                            return {
                                "search": replace(
                                    config, start_time=begin, end_time=end
                                ),
                                "room": room.name,
                                "context_probes": probes,
                            }
            except AuthenticationRequired:
                raise
            except (FBSError, PlaywrightError, TimeoutError):
                continue
    raise CobookerLookupUnavailable(
        "No temporary directory context could be verified; this says nothing about whether the person exists"
    )


def parse_candidates(snapshot):
    if not isinstance(snapshot, dict):
        raise CobookerLookupUnavailable("The FBS directory grid is unavailable")
    headers = [
        " ".join(value.split()).casefold() for value in snapshot.get("headers", [])
    ]
    totals = re.findall(r"(\d+)\s+ITEM\(S\)\s+TOTAL", snapshot.get("summary", ""), re.I)
    rows = snapshot.get("rows", [])
    if (
        [value for value in headers if value] != ["name"]
        or len(set(totals)) != 1
        or len(rows) > 200
    ):
        raise CobookerLookupUnavailable(
            "The FBS directory layout or coverage is unreadable"
        )
    total = int(totals[0])
    candidates = []
    for row in rows:
        if total == 0 and len(row) == 1 and row[0].strip().casefold() == "no items":
            continue
        if len(row) != 2 or any(
            not isinstance(value, str) or len(value) > 1000 for value in row
        ):
            raise CobookerLookupUnavailable("The FBS directory row is unreadable")
        match = re.fullmatch(r"(.+?)\s+\(([^()\s]+)\)", " ".join(row[1].split()))
        if not match or not SMU_EMAIL_RE.fullmatch(match[2]):
            raise CobookerLookupUnavailable(
                "The FBS directory identity could not be verified"
            )
        candidates.append({"name": match[1], "email": match[2].casefold()})
    if (
        len(candidates) > total
        or (total and not candidates)
        or len({row["email"] for row in candidates}) != len(candidates)
    ):
        raise CobookerLookupUnavailable("The FBS directory coverage is inconsistent")
    return {"candidates": candidates, "total": total}


@observed_step("cobooker.lookup")
async def search_cobookers(client, search, room, query):
    query = validate_query(query)
    require_future_booking(search)
    await client.progress(
        "checking the selected room before opening the co-booker picker"
    )
    result = await client.search(search, room_name=room)
    if not any(
        item.name == room and item.is_free(search.start_time, search.end_time)
        for item in result.rooms
    ):
        raise BookingUnavailable(
            "Choose a currently free room before searching the co-booker picker."
        )
    if not client.page:
        raise CobookerLookupUnavailable("The FBS browser is unavailable")
    frame = client.page.frame(name="frameContent")
    if not frame:
        raise CobookerLookupUnavailable("The FBS calendar is unavailable")
    try:
        require_future_booking(search)
        form = await client._prepare_booking_frame(frame, search, room)
        if not fbs_document_url(form.url):
            raise CobookerLookupUnavailable("The FBS picker left the trusted origin")
        await client._search_co_booker_picker(form, query)
        root = form.locator("#" + GRID)
        await root.wait_for(state="visible", timeout=20000)

        async def read_page():
            snapshot = await root.evaluate(
                """root => ({
                    headers: [...root.querySelectorAll('th')].map(e => e.innerText.trim()),
                    rows: [...root.querySelectorAll('tr.row')].map(row =>
                        [...row.cells].map(cell => cell.innerText.trim())),
                    summary: root.innerText
                })"""
            )
            return parse_candidates(snapshot)

        result = await collect_pages(
            client,
            form,
            GRID,
            "candidates",
            read_page,
            max_pages=5,
            identity_key="email",
        )
        return {**result, "query": query, "source": "fbs_directory"}
    except (PlaywrightError, LayoutChanged, TimeoutError) as error:
        from .operation_diagnostics import capture_failure

        await capture_failure(client, error)
        raise CobookerLookupUnavailable(
            "The FBS co-booker picker could not be read reliably"
        ) from error
    finally:
        # leaving the form discards the UI context without selecting or submitting.
        try:
            await client.page.goto(
                FBS_URL, wait_until="domcontentloaded", timeout=15000
            )
        except PlaywrightError as error:
            raise CobookerLookupUnavailable(
                "The browser could not leave the unsubmitted co-booker picker"
            ) from error
