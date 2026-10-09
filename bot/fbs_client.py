"""Live SMU FBS client built against the authenticated October 2026 DOM.

The client is intentionally single-user.  It stores browser session state in a
local Playwright profile, supports interactive MFA renewal, and never stores an
SMU password.  Booking submission is opt-in through ``submit=True`` only.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from playwright.async_api import BrowserContext, Frame, Page, async_playwright


FBS_URL = "https://fbs.intranet.smu.edu.sg/home"
PROFILE_PATH = Path(
    os.getenv(
        "SAGASU_BROWSER_PROFILE",
        str(Path(__file__).resolve().parent / ".fbs-profile"),
    )
)
AUTH_TIMEOUT_MS = int(os.getenv("SAGASU_AUTH_TIMEOUT_MS", "600000"))
AUTH_ARTIFACT_DIR = Path(
    os.getenv(
        "SAGASU_AUTH_ARTIFACT_DIR",
        str(Path(__file__).resolve().parents[1] / "artifacts" / "auth"),
    )
)

ProgressCallback = Callable[[str], Awaitable[None]]
_BROWSER_LOCK = asyncio.Lock()
_SHARED_PLAYWRIGHT = None
_SHARED_CONTEXT: BrowserContext | None = None


class FBSError(RuntimeError):
    """Base FBS client failure."""


class AuthenticationRequired(FBSError):
    """The saved browser session is absent or expired."""


class LayoutChanged(FBSError):
    """The live FBS DOM no longer matches the expected structure."""


class BookingUnavailable(FBSError):
    """The requested room or timeslot is no longer available."""


@dataclass(frozen=True)
class SearchConfig:
    date: str
    start_time: str
    end_time: str
    buildings: tuple[str, ...] = ()
    floors: tuple[str, ...] = ()
    facility_types: tuple[str, ...] = ()
    capacity: str | None = None
    equipment: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            datetime.strptime(self.date, "%d-%b-%Y")
            start = time_to_minutes(self.start_time)
            end = time_to_minutes(self.end_time)
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid FBS date or time format") from error
        if not (0 <= start < end <= 24 * 60):
            raise ValueError("Booking end time must be after its start time")
        if start % 30 or end % 30:
            raise ValueError("FBS times must use 30-minute increments")

    @classmethod
    def from_mapping(cls, value: dict) -> "SearchConfig":
        return cls(
            date=value["date"],
            start_time=value["start_time"],
            end_time=value["end_time"],
            buildings=tuple(value.get("buildings") or ()),
            floors=tuple(value.get("floors") or ()),
            facility_types=tuple(value.get("facility_types") or ()),
            capacity=value.get("capacity"),
            equipment=tuple(value.get("equipment") or ()),
        )


@dataclass(frozen=True)
class TimeSlot:
    start: str
    end: str
    status: str
    details: str | None = None


@dataclass
class RoomAvailability:
    name: str
    timeslots: list[TimeSlot] = field(default_factory=list)

    def is_free(self, start: str, end: str) -> bool:
        requested_start = time_to_minutes(start)
        requested_end = time_to_minutes(end)
        return any(
            slot.status == "free"
            and time_to_minutes(slot.start) <= requested_start
            and time_to_minutes(slot.end) >= requested_end
            for slot in self.timeslots
        )

    @property
    def free_minutes(self) -> int:
        return sum(
            time_to_minutes(slot.end) - time_to_minutes(slot.start)
            for slot in self.timeslots
            if slot.status == "free"
        )


@dataclass
class SearchResult:
    config: SearchConfig
    rooms: list[RoomAvailability]
    scraped_at: str

    @property
    def bookable_rooms(self) -> list[RoomAvailability]:
        rooms = [
            room
            for room in self.rooms
            if room.is_free(self.config.start_time, self.config.end_time)
        ]
        return sorted(rooms, key=lambda room: (-room.free_minutes, room.name.lower()))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BookingRequest:
    search: SearchConfig
    room: str
    purpose: str
    co_booker: str
    usage_type: str = "AdHoc"
    booking_usage: str = "Meeting"
    send_calendar_invite: bool = False

    def __post_init__(self) -> None:
        if not self.room.strip():
            raise ValueError("A room is required")
        if not self.purpose.strip():
            raise ValueError("A booking purpose is required")
        if not self.co_booker.strip():
            raise ValueError("A co-booker is required")

    @classmethod
    def from_mapping(cls, value: dict) -> "BookingRequest":
        return cls(
            search=SearchConfig.from_mapping(value["search"]),
            room=value["room"],
            purpose=value["purpose"],
            co_booker=value["co_booker"],
            usage_type=value.get("usage_type", "AdHoc"),
            booking_usage=value.get("booking_usage", "Meeting"),
            send_calendar_invite=bool(value.get("send_calendar_invite", False)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def time_to_minutes(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value)
    if not match:
        raise ValueError(f"Invalid time: {value}")
    hours, minutes = map(int, match.groups())
    if hours > 23 or minutes > 59:
        raise ValueError(f"Invalid time: {value}")
    return hours * 60 + minutes


def minutes_to_time(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def parse_event_title(title: str) -> TimeSlot | None:
    booked = re.search(r"Booking Time:\s*(\d{2}:\d{2})-(\d{2}:\d{2})", title)
    if booked:
        return TimeSlot(booked.group(1), booked.group(2), "booked", title)
    unavailable = re.search(
        r"\((\d{2}:\d{2})-(\d{2}:\d{2})\)\s*\(not available\)",
        title,
        re.IGNORECASE,
    )
    if unavailable:
        return TimeSlot(
            unavailable.group(1), unavailable.group(2), "unavailable", title
        )
    return None


def add_free_windows(
    blocked: Iterable[TimeSlot], start_time: str, end_time: str
) -> list[TimeSlot]:
    """Return blocked intervals plus their free complement inside a search window."""

    window_start = time_to_minutes(start_time)
    window_end = time_to_minutes(end_time)
    if window_end <= window_start:
        raise ValueError("end_time must be after start_time")

    clamped: list[tuple[int, int, TimeSlot]] = []
    for slot in blocked:
        start = max(window_start, time_to_minutes(slot.start))
        end = min(window_end, time_to_minutes(slot.end))
        if start < end:
            clamped.append((start, end, slot))
    clamped.sort(key=lambda item: (item[0], item[1]))

    result: list[TimeSlot] = []
    cursor = window_start
    for start, end, slot in clamped:
        if start > cursor:
            result.append(TimeSlot(minutes_to_time(cursor), minutes_to_time(start), "free"))
        if end > cursor:
            result.append(
                TimeSlot(
                    minutes_to_time(max(start, cursor)),
                    minutes_to_time(end),
                    slot.status,
                    slot.details,
                )
            )
            cursor = end
    if cursor < window_end:
        result.append(TimeSlot(minutes_to_time(cursor), minutes_to_time(window_end), "free"))
    return result


async def _noop_progress(_stage: str) -> None:
    return None


class FBSClient:
    def __init__(
        self,
        *,
        interactive: bool = True,
        headless: bool = False,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.interactive = interactive
        self.headless = headless
        self.progress = progress or _noop_progress
        self._playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._auth_method_switched = False

    async def __aenter__(self) -> "FBSClient":
        global _SHARED_CONTEXT, _SHARED_PLAYWRIGHT
        await _BROWSER_LOCK.acquire()
        try:
            if _SHARED_CONTEXT is not None:
                try:
                    self.context = _SHARED_CONTEXT
                    self._playwright = _SHARED_PLAYWRIGHT
                    pages = self.context.pages
                    self.page = next(
                        (
                            page
                            for page in pages
                            if page.url.startswith("https://fbs.intranet.smu.edu.sg/")
                        ),
                        pages[0] if pages else await self.context.new_page(),
                    )
                    return self
                except Exception:
                    _SHARED_CONTEXT = None
                    _SHARED_PLAYWRIGHT = None
            PROFILE_PATH.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._playwright = await async_playwright().start()
            self.context = await self._playwright.chromium.launch_persistent_context(
                str(PROFILE_PATH),
                channel=os.getenv("SAGASU_BROWSER_CHANNEL", "chrome"),
                headless=self.headless,
                viewport={"width": 1440, "height": 1000},
            )
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            _SHARED_PLAYWRIGHT = self._playwright
            _SHARED_CONTEXT = self.context
            return self
        except Exception:
            _BROWSER_LOCK.release()
            raise

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        # Keep the authenticated FBS window alive for the bot process lifetime.
        # This avoids triggering Microsoft authentication for every command.
        _BROWSER_LOCK.release()

    async def ensure_authenticated(self) -> tuple[Page, Frame]:
        if not self.page:
            raise FBSError("Browser has not been started")
        await self.progress("opening FBS")
        await self.page.goto(FBS_URL, wait_until="domcontentloaded", timeout=60000)

        try:
            await self.page.wait_for_selector("iframe#frameBottom", timeout=15000)
        except Exception as error:
            if not self.interactive or self.headless:
                raise AuthenticationRequired(
                    "FBS session expired; run an interactive login first"
                ) from error
            try:
                await self._complete_interactive_authentication()
            except Exception as auth_error:
                if isinstance(auth_error, AuthenticationRequired):
                    raise
                raise AuthenticationRequired(
                    "Timed out waiting for Microsoft login and MFA"
                ) from auth_error

        frame = await self._wait_for_frame("frameContent")
        await frame.wait_for_selector(
            "input#DateBookingFrom_c1_textDate", timeout=30000
        )
        return self.page, frame

    async def _complete_interactive_authentication(self) -> None:
        if not self.page:
            raise FBSError("Browser page is unavailable")
        started = asyncio.get_running_loop().time()
        previous_state = None
        email_submitted = False
        while (asyncio.get_running_loop().time() - started) * 1000 < AUTH_TIMEOUT_MS:
            if await self.page.locator("iframe#frameBottom").count():
                await self.progress("Microsoft authentication complete; continuing scrape")
                return

            state, prompt = await self._classify_auth_state()
            if state != previous_state:
                await self._capture_auth_state(state)
                await self.progress(prompt)
                previous_state = state

            if state == "email" and not email_submitted:
                login_email = os.getenv("SMU_LOGIN_EMAIL", "").strip()
                if login_email:
                    field = self.page.locator(
                        'input[name="loginfmt"]:visible, input[type="email"]:visible'
                    ).first
                    await field.fill(login_email)
                    submit = self.page.locator(
                        'input[type="submit"]:visible, button[type="submit"]:visible'
                    ).first
                    await submit.click()
                    email_submitted = True
            elif state == "stay_signed_in" and os.getenv(
                "SAGASU_STAY_SIGNED_IN", "true"
            ).lower() not in {"0", "false", "no"}:
                yes = self.page.locator("#idSIButton9:visible").first
                if await yes.count():
                    await yes.click()
            elif state == "webauthn" and not self._auth_method_switched and os.getenv(
                "SAGASU_PREFER_AUTHENTICATOR", "true"
            ).lower() not in {"0", "false", "no"}:
                # Cancel the host-device passkey prompt and choose a method that
                # can be approved remotely from the owner's phone.
                await self.page.keyboard.press("Escape")
                alternate = self.page.get_by_text("Sign in another way", exact=True)
                if await alternate.count():
                    await alternate.first.click()
                    self._auth_method_switched = True
            elif state == "method_picker" and self._auth_method_switched:
                authenticator = self.page.get_by_text(
                    re.compile(r"Microsoft Authenticator|Approve a request", re.I)
                )
                for option in await authenticator.all():
                    if await option.is_visible():
                        await option.click()
                        break

            await self.page.wait_for_timeout(750)
        raise AuthenticationRequired("Timed out waiting for Microsoft login and MFA")

    async def _classify_auth_state(self) -> tuple[str, str]:
        if not self.page:
            return "unknown", "waiting for Microsoft sign-in in Chrome"
        body = ""
        try:
            body = (await self.page.locator("body").inner_text(timeout=2000)).casefold()
        except Exception:
            pass
        if await self.page.locator('input[type="password"]:visible').count():
            return "password", "enter your Microsoft password in Chrome"
        if await self.page.locator(
            'input[name="loginfmt"]:visible, input[type="email"]:visible'
        ).count():
            if os.getenv("SMU_LOGIN_EMAIL", "").strip():
                return "email", "entering your configured SMU email"
            return "email", "enter your SMU email in Chrome"
        if "stay signed in" in body:
            return "stay_signed_in", "keeping the local Microsoft session signed in"
        if "face, fingerprint, pin or security key" in body:
            return "webauthn", "switching from host-device sign-in to Authenticator"
        if "verify your identity" in body and "authenticator" in body:
            return "method_picker", "selecting Microsoft Authenticator"
        if "authenticator" in body or "approve a request" in body:
            number = ""
            for selector in ("#idRichContext_DisplaySign", ".displaySign"):
                display = self.page.locator(selector).first
                if await display.count():
                    candidate = (await display.inner_text()).strip()
                    if re.fullmatch(r"\d{2,3}", candidate):
                        number = candidate
                        break
            if not number:
                # Microsoft's number-matching value is sometimes plain text
                # without a stable DOM id.
                number = next(
                    (
                        line.strip()
                        for line in body.splitlines()
                        if re.fullmatch(r"\d{2,3}", line.strip())
                    ),
                    "",
                )
            suffix = f" and enter number {number}" if number else ""
            return (
                "authenticator",
                f"approve the Microsoft Authenticator request{suffix}",
            )
        if "pick an account" in body or "choose an account" in body:
            return "account", "choose your SMU Microsoft account in Chrome"
        return "sign_in", "complete Microsoft sign-in in Chrome"

    async def _capture_auth_state(self, state: str) -> None:
        """Capture auth UI locally while masking all form-field values."""

        if not self.page or state == "password":
            return
        AUTH_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = AUTH_ARTIFACT_DIR / f"{stamp}-{state}.png"
        try:
            await self.page.screenshot(
                path=str(path),
                full_page=False,
                mask=[self.page.locator("input, textarea")],
            )
            path.chmod(0o600)
        except Exception:
            # A redirect can replace the page while the screenshot is taken.
            pass

    async def search(self, config: SearchConfig) -> SearchResult:
        page, frame = await self.ensure_authenticated()
        await self.progress("configuring availability search")
        await self._set_date(frame, config.date)
        await frame.locator("select#TimeFrom_c1_ctl04").select_option(
            config.start_time
        )
        await self._wait_for_idle(frame)
        await frame.locator("select#TimeTo_c1_ctl04").select_option(config.end_time)
        await self._wait_for_idle(frame)
        await self._set_multi_select(frame, "DropMultiBuildingList_c1", config.buildings)
        await self._set_multi_select(frame, "DropMultiFloorList_c1", config.floors)
        await self._set_multi_select(
            frame, "DropMultiFacilityTypeList_c1", config.facility_types
        )
        if config.capacity:
            await frame.locator("select#DropCapacity_c1").select_option(
                value=config.capacity
            )
        else:
            await frame.locator("select#DropCapacity_c1").select_option(index=0)
        await self._wait_for_idle(frame)
        await self._set_multi_select(frame, "DropMultiEquipmentList_c1", config.equipment)

        await self.progress("loading matching facilities")
        await frame.locator("table#GridResults_gv").wait_for(timeout=30000)
        matching_rooms = await self._matching_room_names(frame)
        if not matching_rooms:
            return SearchResult(config, [], datetime.now().astimezone().isoformat())

        await frame.locator("a#CheckAvailability").click()
        await frame.locator("a#btnMakeBooking").wait_for(timeout=60000)
        await frame.locator("div.scheduler_bluewhite_rowheader_inner").first.wait_for(
            timeout=30000
        )
        await page.wait_for_timeout(1000)

        rooms = await self._read_availability(frame, matching_rooms, config)
        return SearchResult(config, rooms, datetime.now().astimezone().isoformat())

    async def book(self, request: BookingRequest, *, submit: bool = False) -> dict:
        """Prepare or submit a booking.

        ``submit=False`` stops after every required field and checkbox is populated.
        Only ``submit=True`` clicks FBS's final CONFIRM control.
        """

        result = await self.search(request.search)
        room = next((item for item in result.rooms if item.name == request.room), None)
        if not room or not room.is_free(
            request.search.start_time, request.search.end_time
        ):
            raise BookingUnavailable(
                f"{request.room} is no longer free from "
                f"{request.search.start_time} to {request.search.end_time}"
            )
        if not self.page:
            raise FBSError("Browser page is unavailable")
        frame = self.page.frame(name="frameContent")
        if not frame:
            raise LayoutChanged("Availability frame disappeared")

        await self.progress("selecting requested room and timeslot")
        await self._drag_timeslot(
            frame,
            request.room,
            request.search.start_time,
            request.search.end_time,
        )
        await frame.locator("a#btnMakeBooking").click()
        booking_frame = await self._wait_for_frame("frameBookingDetails")
        await booking_frame.locator(
            "input#bookingFormControl1_TextboxPurpose_c1"
        ).fill(request.purpose)
        await booking_frame.locator(
            "select#bookingFormControl1_DropDownUsageType_c1"
        ).select_option(label=request.usage_type)
        await booking_frame.locator(
            "select#bookingFormControl1_DropDownSpaceBookingUsage_c1"
        ).select_option(label=request.booking_usage)
        await self._add_co_booker(booking_frame, request.co_booker)

        invite = booking_frame.locator(
            "input#bookingFormControl1_SendCalendarInvitation_c1"
        )
        if request.send_calendar_invite:
            await invite.check()
        else:
            await invite.uncheck()
        await booking_frame.locator(
            "input#bookingFormControl1_TermsAndConditionsCheckbox_c1"
        ).check()

        preview = {
            "status": "ready" if not submit else "submitting",
            "room": request.room,
            "date": request.search.date,
            "start_time": request.search.start_time,
            "end_time": request.search.end_time,
            "purpose": request.purpose,
            "co_booker": request.co_booker,
            "usage_type": request.usage_type,
            "booking_usage": request.booking_usage,
        }
        if not submit:
            return preview

        await self.progress("submitting confirmed booking")
        confirm = booking_frame.locator("a#panel_UIButton2").first
        await confirm.click()
        await self.page.wait_for_timeout(2000)
        preview.update(await self._verify_booking(request))
        return preview

    async def _verify_booking(self, request: BookingRequest) -> dict:
        """Verify a submitted booking against FBS's My Bookings table."""

        if not self.page:
            raise FBSError("Browser page is unavailable")
        await self.progress("verifying booking in My Bookings")
        frame_bottom = self.page.frame(name="frameBottom")
        if not frame_bottom:
            raise LayoutChanged("FBS navigation frame disappeared after submission")
        await frame_bottom.locator("div#MyBookingsTab").click()

        expected_date = datetime.strptime(request.search.date, "%d-%b-%Y").strftime(
            "%d-%b-%Y"
        )
        for _ in range(30):
            frame_content = self.page.frame(name="frameContent")
            if frame_content:
                rows = frame_content.locator(
                    "div#GridViewBooking_content table tbody tr.row"
                )
                try:
                    for row in await rows.all():
                        cells = await row.locator("td").all()
                        if len(cells) < 9:
                            continue
                        date_time = (await cells[2].inner_text()).strip()
                        room = (await cells[4].inner_text()).strip()
                        if (
                            expected_date in date_time
                            and request.search.start_time in date_time
                            and request.search.end_time in date_time
                            and room.casefold() == request.room.casefold()
                        ):
                            return {
                                "status": "verified",
                                "reference_number": (await cells[1].inner_text()).strip(),
                                "fbs_status": (await cells[8].inner_text()).strip(),
                            }
                except Exception:
                    # The content iframe can be replaced while My Bookings loads.
                    pass
            await self.page.wait_for_timeout(1000)
        raise FBSError(
            "FBS submission outcome could not be verified in My Bookings; "
            "check FBS before retrying to avoid a duplicate booking"
        )

    async def _set_date(self, frame: Frame, target_value: str) -> None:
        target = datetime.strptime(target_value, "%d-%b-%Y").date()
        field = frame.locator("input#DateBookingFrom_c1_textDate")
        for _ in range(32):
            current_value = await field.input_value()
            current = datetime.strptime(current_value, "%d-%b-%Y").date()
            if current == target:
                return
            selector = "a#BtnDpcNext" if current < target else "a#BtnDpcPrev"
            await frame.locator(selector).click()
            await self._wait_for_idle(frame)
        raise FBSError(f"Could not navigate FBS calendar to {target_value}")

    async def _set_multi_select(
        self, frame: Frame, control_id: str, values: Iterable[str]
    ) -> None:
        await self._wait_for_idle(frame)
        opener = frame.locator(f"#{control_id}_textItem:visible").first
        current_value = await frame.locator(f"#{control_id}_textValue").first.input_value()
        await opener.click()
        panel = frame.locator(f"#{control_id}_panelContainer:visible").first
        await panel.wait_for(state="visible", timeout=10000)
        clear = panel.locator('input[type="button"][value="Clear"]:visible').first
        if current_value.strip() and await clear.count():
            await clear.click()
            await self._wait_for_idle(frame)
            # Clear triggers a legacy postback and closes/replaces the panel.
            opener = frame.locator(f"#{control_id}_textItem:visible").first
            await opener.click()
            panel = frame.locator(f"#{control_id}_panelContainer:visible").first
            await panel.wait_for(state="visible", timeout=10000)
        for value in values:
            # FBS visually renders options inside the dropdown, but its legacy
            # control may mount their label nodes outside panelContainer.
            option = frame.get_by_text(value, exact=True)
            if await option.count() == 0:
                raise FBSError(f"FBS option not found in {control_id}: {value}")
            clicked = False
            for candidate in await option.all():
                if await candidate.is_visible():
                    await candidate.click()
                    clicked = True
                    break
            if not clicked:
                raise FBSError(f"Visible FBS option not found in {control_id}: {value}")
        await panel.locator('input[type="button"][value="OK"]:visible').first.click()
        await self._wait_for_idle(frame)

    async def _wait_for_idle(self, frame: Frame) -> None:
        await frame.wait_for_function(
            """() => [...document.querySelectorAll('#__updateProgress__')].every((el) => {
                const style = window.getComputedStyle(el);
                return style.display === 'none' || style.visibility === 'hidden' ||
                    style.pointerEvents === 'none' || el.getBoundingClientRect().height === 0;
            })""",
            timeout=60000,
        )
        await frame.wait_for_timeout(250)

    async def _matching_room_names(self, frame: Frame) -> list[str]:
        result = []
        for row in await frame.locator("table#GridResults_gv tbody tr").all():
            cells = await row.locator("td").all()
            if len(cells) < 2:
                continue
            value = (await cells[1].inner_text()).strip()
            if value:
                result.append(value)
        return result

    async def _read_availability(
        self, frame: Frame, room_names: list[str], config: SearchConfig
    ) -> list[RoomAvailability]:
        room_headers: dict[str, float] = {}
        for header in await frame.locator(
            "div.scheduler_bluewhite_rowheader_inner"
        ).all():
            name = (await header.inner_text()).strip()
            if name not in room_names:
                continue
            box = await header.bounding_box()
            if box:
                room_headers[name] = box["y"] + box["height"] / 2

        blocked_by_room: dict[str, list[TimeSlot]] = {
            room: [] for room in room_names
        }
        events = frame.locator(
            "div.scheduler_bluewhite_event.scheduler_bluewhite_event_line0"
        )
        for event in await events.all():
            title = await event.get_attribute("title")
            box = await event.bounding_box()
            slot = parse_event_title(title or "")
            if not box or not slot or not room_headers:
                continue
            event_y = box["y"] + box["height"] / 2
            room = min(room_headers, key=lambda name: abs(room_headers[name] - event_y))
            blocked_by_room[room].append(slot)

        return [
            RoomAvailability(
                room,
                add_free_windows(
                    blocked_by_room.get(room, []),
                    config.start_time,
                    config.end_time,
                ),
            )
            for room in room_names
        ]

    async def _drag_timeslot(
        self, frame: Frame, room_name: str, start_time: str, end_time: str
    ) -> None:
        headers = frame.locator("div.scheduler_bluewhite_rowheader_inner")
        row = None
        for candidate in await headers.all():
            if (await candidate.inner_text()).strip() == room_name:
                row = candidate
                break
        if not row:
            raise LayoutChanged(f"Calendar row not found for {room_name}")
        row_box = await row.bounding_box()
        if not row_box:
            raise LayoutChanged(f"Calendar row is not visible for {room_name}")

        start_box = await self._time_header_box(frame, start_time)
        end_box = await self._time_header_box(frame, end_time)
        start_x = start_box["x"] + 2
        end_x = end_box["x"] - 2
        y = row_box["y"] + row_box["height"] / 2
        if end_x <= start_x:
            raise LayoutChanged("Unable to calculate the calendar drag range")
        await self.page.mouse.move(start_x, y)
        await self.page.mouse.down()
        await self.page.mouse.move(end_x, y, steps=20)
        await self.page.mouse.up()
        await frame.wait_for_timeout(1200)

    async def _time_header_box(self, frame: Frame, value: str) -> dict:
        selectors = [
            "div.scheduler_bluewhite_timeheader_cell_inner",
            "div.scheduler_bluewhite_timeheadergroup_inner",
        ]
        for selector in selectors:
            locator = frame.locator(selector)
            for candidate in await locator.all():
                if (await candidate.inner_text()).strip() == value:
                    box = await candidate.bounding_box()
                    if box:
                        return box
        raise LayoutChanged(f"Calendar time header not found: {value}")

    async def _wait_for_frame(self, name: str) -> Frame:
        if not self.page:
            raise FBSError("Browser page is unavailable")
        for _ in range(60):
            frame = self.page.frame(name=name)
            if frame:
                return frame
            await self.page.wait_for_timeout(500)
        raise LayoutChanged(f"FBS frame was not created: {name}")

    async def _add_co_booker(self, frame: Frame, query: str) -> None:
        await frame.locator("a#bookingFormControl1_GridCoBookers_ctl14").click()
        search_input = frame.locator(
            "input#bookingFormControl1_DialogSearchCoBooker_searchPanel_textBox_c1"
        )
        await search_input.wait_for(state="visible", timeout=15000)
        await search_input.fill(query)
        await frame.locator(
            "a#bookingFormControl1_DialogSearchCoBooker_searchPanel_buttonSearch"
        ).click()

        result_link = frame.locator(
            "#bookingFormControl1_DialogSearchCoBooker_searchPanel_gridView_gv"
        ).get_by_text(query, exact=False)
        await result_link.first.wait_for(timeout=20000)
        row = result_link.first.locator("xpath=ancestor::tr[1]")
        checkbox = row.locator('input[type="checkbox"]')
        if await checkbox.count() == 0:
            raise LayoutChanged("Co-booker result checkbox was not found")
        await checkbox.check()
        await frame.locator(
            "a#bookingFormControl1_DialogSearchCoBooker_dialogBox_b1"
        ).click()

        grid = frame.locator("#bookingFormControl1_GridCoBookers_gv")
        await grid.wait_for(timeout=15000)
        added = grid.get_by_text(query, exact=False)
        if await added.count() == 0:
            # FBS may display a name rather than an email after selection.
            rows = grid.locator("tr")
            if await rows.count() < 2:
                raise FBSError(f"Co-booker was not added: {query}")
            added_row = rows.nth(1)
        else:
            added_row = added.first.locator("xpath=ancestor::tr[1]")
        added_checkbox = added_row.locator('input[type="checkbox"]')
        if await added_checkbox.count():
            await added_checkbox.check()


def search_config_from_bot(config: dict) -> SearchConfig:
    """Translate the Telegram bot's compact config into the live FBS contract."""

    date_raw = config.get("date_raw") or datetime.now().strftime("%d %B %Y")
    date = datetime.strptime(date_raw.title(), "%d %B %Y").strftime("%d-%b-%Y")
    start = config.get("start_time", "11:00")
    duration = float(config.get("duration_hrs", 2.5))
    end = minutes_to_time(time_to_minutes(start) + int(duration * 60))
    return SearchConfig(
        date=date,
        start_time=start,
        end_time=end,
        buildings=tuple(
            config.get("buildings")
            or ("School of Computing & Information Systems 1",)
        ),
        floors=tuple(config.get("floors") or ("Level 2", "Level 3", "Level 4")),
        facility_types=tuple(config.get("facility_types") or ("Group Study Room",)),
        capacity=config.get("room_capacity"),
        equipment=tuple(config.get("equipment") or ()),
    )


async def search_fbs(
    config: SearchConfig, progress: ProgressCallback | None = None
) -> SearchResult:
    try:
        async with FBSClient(progress=progress) as client:
            return await client.search(config)
    except FBSError:
        raise
    except Exception as error:
        raise FBSError(f"Unexpected FBS search failure: {error}") from error


async def execute_booking(
    request: BookingRequest,
    *,
    submit: bool,
    progress: ProgressCallback | None = None,
) -> dict:
    try:
        async with FBSClient(progress=progress) as client:
            return await client.book(request, submit=submit)
    except FBSError:
        raise
    except Exception as error:
        raise FBSError(f"Unexpected FBS booking failure: {error}") from error
