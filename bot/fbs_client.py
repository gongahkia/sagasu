"""Live SMU FBS client built against the authenticated October 2026 DOM.

The client is intentionally single-user.  It stores browser session state in a
local Playwright profile and supports optional privately configured credentials
and owner-approved MFA renewal.  Booking submission is opt-in through ``submit=True`` only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Awaitable, Callable, Iterable
from urllib.parse import parse_qs, urlparse

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


try:
    from .config import DEFAULT_ARTIFACTS, DEFAULT_PROFILE
except ImportError:
    if __package__:
        raise
    from config import DEFAULT_ARTIFACTS, DEFAULT_PROFILE

FBS_URL = "https://fbs.intranet.smu.edu.sg/home"
PROFILE_PATH = DEFAULT_PROFILE
AUTH_TIMEOUT_MS = 600000
AUTH_ARTIFACT_DIR = DEFAULT_ARTIFACTS
logger = logging.getLogger(__name__)

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
            result.append(
                TimeSlot(minutes_to_time(cursor), minutes_to_time(start), "free")
            )
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
        result.append(
            TimeSlot(minutes_to_time(cursor), minutes_to_time(window_end), "free")
        )
    return result


async def _noop_progress(stage: str) -> None:
    logger.info("FBS: %s", stage)


async def close_shared_browser():
    global _SHARED_CONTEXT, _SHARED_PLAYWRIGHT
    async with _BROWSER_LOCK:
        context, playwright = _SHARED_CONTEXT, _SHARED_PLAYWRIGHT
        _SHARED_CONTEXT = _SHARED_PLAYWRIGHT = None
        try:
            if context:
                await asyncio.wait_for(context.close(), timeout=15)
        finally:
            if playwright:
                await asyncio.wait_for(playwright.stop(), timeout=15)


class FBSClient:
    def __init__(
        self,
        *,
        interactive: bool = True,
        headless: bool | None = None,
        auth_notify: ProgressCallback | None = None,
        progress: ProgressCallback | None = None,
        keep_alive: bool = True,
        profile_path: Path | None = None,
    ) -> None:
        self.interactive = interactive
        self.headless = (
            os.getenv("SAGASU_HEADLESS", "false").lower() in {"1", "true", "yes"}
            if headless is None
            else headless
        )
        self.auth_notify = auth_notify
        self.progress = progress or _noop_progress
        self.keep_alive = keep_alive
        self.profile_path = Path(
            profile_path or os.getenv("SAGASU_BROWSER_PROFILE", str(PROFILE_PATH))
        ).expanduser()
        self._playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def __aenter__(self) -> "FBSClient":
        global _SHARED_CONTEXT, _SHARED_PLAYWRIGHT
        await _BROWSER_LOCK.acquire()
        try:
            if self.keep_alive and _SHARED_CONTEXT is not None:
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
            self.profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._playwright = await async_playwright().start()
            self.context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_path),
                channel=os.getenv("SAGASU_BROWSER_CHANNEL", "chrome"),
                headless=self.headless,
                viewport={"width": 1440, "height": 1000},
            )
            self.page = (
                self.context.pages[0]
                if self.context.pages
                else await self.context.new_page()
            )
            await self._restore_session()
            if self.keep_alive:
                _SHARED_PLAYWRIGHT = self._playwright
                _SHARED_CONTEXT = self.context
            return self
        except BaseException:
            try:
                try:
                    if self.context:
                        await asyncio.wait_for(self.context.close(), timeout=15)
                finally:
                    if self._playwright:
                        await asyncio.wait_for(self._playwright.stop(), timeout=15)
            finally:
                _BROWSER_LOCK.release()
            raise

    @property
    def session_path(self):
        return self.profile_path / ".sagasu-session.json"

    async def _restore_session(self):
        if not self.session_path.exists():
            return
        try:
            cookies = json.loads(self.session_path.read_text(encoding="utf-8"))
            if not isinstance(cookies, list):
                raise ValueError()
            await asyncio.wait_for(self.context.add_cookies(cookies), timeout=15)
        except Exception:
            if not self.interactive:
                raise AuthenticationRequired(
                    "Saved session is unreadable. Renew it with sagasu login."
                ) from None
            logger.warning(
                "Saved session could not be restored; complete login locally to renew it."
            )

    async def _save_session(self):
        temporary = None
        try:
            cookies = await asyncio.wait_for(self.context.cookies(), timeout=15)
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".sagasu-session-",
                suffix=".tmp",
                dir=self.profile_path,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(
                    [cookie for cookie in cookies if cookie.get("expires") == -1],
                    stream,
                )
            temporary.replace(self.session_path)
        except Exception:
            raise FBSError(
                "Private session could not be saved. Check browser-profile permissions."
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            if not self.keep_alive:
                try:
                    if self.context:
                        await asyncio.wait_for(self.context.close(), timeout=15)
                finally:
                    if self._playwright:
                        await asyncio.wait_for(self._playwright.stop(), timeout=15)
        finally:
            _BROWSER_LOCK.release()

    async def ensure_authenticated(self) -> tuple[Page, Frame]:
        if not self.page:
            raise FBSError("Browser has not been started")
        await self.progress("opening FBS")
        try:
            await self.page.goto(FBS_URL, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError:
            raise FBSError(
                "FBS navigation timed out after 60 seconds. Open "
                f"{FBS_URL} in a normal browser on this computer to check access. "
                "Retry the search once FBS is reachable."
            ) from None
        except PlaywrightError:
            raise FBSError(
                "The browser could not open FBS. Check your connection and open "
                f"{FBS_URL} in a normal browser on this computer."
            ) from None

        try:
            await self.page.wait_for_function(
                """() => !!document.querySelector('iframe#frameBottom') ||
                ['login.microsoftonline.com', 'login.microsoft.com', 'login.windows.net']
                    .includes(location.hostname)""",
                timeout=15000,
            )
            if not await self.page.locator("iframe#frameBottom").count():
                raise AuthenticationRequired("Microsoft sign-in required")
        except Exception as error:
            if not self.interactive:
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
        await self._save_session()
        return self.page, frame

    @staticmethod
    def _trusted_login_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname
            in {"login.microsoftonline.com", "login.microsoft.com", "login.windows.net"}
            and port in {None, 443}
            and not parsed.username
            and not parsed.password
        )

    async def _complete_interactive_authentication(self) -> None:
        if not self.page:
            raise FBSError("Browser page is unavailable")
        started = asyncio.get_running_loop().time()
        previous_state = None
        email_submitted = password_submitted = method_selected = False
        cancelled_passkey = False
        notifications = set()
        timeout_ms = int(os.getenv("SAGASU_AUTH_TIMEOUT_MS", str(AUTH_TIMEOUT_MS)))
        while (asyncio.get_running_loop().time() - started) * 1000 < timeout_ms:
            if await self.page.locator("iframe#frameBottom").count():
                await self.progress(
                    "Microsoft authentication complete; continuing FBS operation"
                )
                return

            if not self._trusted_login_url(self.page.url):
                # Redirects may briefly return to FBS while its frames load.
                if not self.page.url.startswith("https://fbs.intranet.smu.edu.sg/"):
                    raise AuthenticationRequired(
                        "Microsoft sign-in opened an unsupported origin; credentials were not submitted."
                    )
                await self.page.wait_for_timeout(750)
                continue
            state, prompt = await self._classify_auth_state()
            if state != previous_state:
                await self._capture_auth_state(state)
                # Number matching is sent privately to the owner, never to a group/search message.
                await self.progress(
                    "Open Microsoft Authenticator to approve sign-in"
                    if state == "authenticator"
                    else prompt
                )
                previous_state = state
            if (
                state in {"authenticator", "failed", "manual"}
                and (state, prompt) not in notifications
            ):
                if self.auth_notify:
                    await self.auth_notify(prompt)
                elif self.headless:
                    raise AuthenticationRequired(
                        "Headless sign-in needs a private Telegram MFA notifier. Start sagasu run."
                    )
                else:
                    await self.progress(prompt)
                notifications.add((state, prompt))
            if state == "failed" or (state == "manual" and self.headless):
                raise AuthenticationRequired(prompt)

            trusted = self._trusted_login_url(self.page.url)
            if state == "email" and not email_submitted and trusted:
                login_email = os.getenv("SMU_LOGIN_EMAIL", "").strip()
                if login_email:
                    await self.page.locator(
                        'input[name="loginfmt"]:visible, input[type="email"]:visible'
                    ).first.fill(login_email)
                    await self.page.locator(
                        'input[type="submit"]:visible, button[type="submit"]:visible'
                    ).first.click()
                    email_submitted = True
                elif self.headless:
                    raise AuthenticationRequired(
                        "Set SMU_LOGIN_EMAIL for headless sign-in."
                    )
            elif state == "password" and not password_submitted and trusted:
                password = os.getenv("SMU_LOGIN_PASSWORD", "")
                if password:
                    await self.page.locator(
                        'input[type="password"]:visible'
                    ).first.fill(password)
                    await self.page.locator(
                        'input[type="submit"]:visible, button[type="submit"]:visible'
                    ).first.click()
                    password_submitted = True
                elif self.headless:
                    raise AuthenticationRequired(
                        "Set SMU_LOGIN_PASSWORD for headless password sign-in."
                    )
            elif (
                state == "stay_signed_in"
                and trusted
                and os.getenv("SAGASU_STAY_SIGNED_IN", "true").lower()
                not in {"0", "false", "no"}
            ):
                yes = self.page.locator("#idSIButton9:visible").first
                if await yes.count():
                    await yes.click()
            elif state == "password_choice" and trusted and not password_submitted:
                await self.page.get_by_text("Use my password", exact=True).click()
            elif state == "account" and trusted and not email_submitted:
                email = os.getenv("SMU_LOGIN_EMAIL", "").strip()
                if email:
                    account = self.page.get_by_text(email, exact=True)
                    if await account.count():
                        await account.first.click()
                        email_submitted = True
            elif state == "webauthn" and trusted:
                # Microsoft supplies its own cancellation URL for the browser-native prompt.
                # Navigating to that trusted URL cancels it without OS-level keyboard automation.
                cancel = parse_qs(urlparse(self.page.url).query).get("cancelUrl", [""])[
                    0
                ]
                if cancel and self._trusted_login_url(cancel) and not cancelled_passkey:
                    cancelled_passkey = True
                    await self.page.goto(
                        cancel, wait_until="domcontentloaded", timeout=30000
                    )
                else:
                    await self.page.keyboard.press("Escape")
                    alternate = self.page.get_by_text("Sign in another way", exact=True)
                    if await alternate.count() and await alternate.first.is_visible():
                        await alternate.first.click()
            elif state == "method_picker" and trusted and not method_selected:
                authenticator = self.page.get_by_text(
                    re.compile(r"Microsoft Authenticator|Approve a request", re.I)
                )
                for option in await authenticator.all():
                    if await option.is_visible():
                        await option.click()
                        method_selected = True
                        break
                if not method_selected and self.headless:
                    message = "Microsoft did not offer Authenticator push approval. This sign-in needs an interactive browser or an SMU-supported authentication method."
                    if self.auth_notify:
                        await self.auth_notify(message)
                    raise AuthenticationRequired(message)
            await self.page.wait_for_timeout(750)
        message = (
            "Microsoft sign-in timed out. Start a new search when ready to approve MFA."
        )
        if self.auth_notify:
            await self.auth_notify(message)
        raise AuthenticationRequired(message)

    async def _classify_auth_state(self) -> tuple[str, str]:
        if not self.page:
            return "unknown", "waiting for Microsoft sign-in"
        body = ""
        try:
            body = (await self.page.locator("body").inner_text(timeout=2000)).casefold()
        except Exception:
            pass
        if any(
            text in body
            for text in (
                "password is incorrect",
                "incorrect password",
                "account or password is incorrect",
                "too many times",
                "account has been locked",
                "you denied",
                "request was denied",
                "we didn't hear from you",
                "sign-in was blocked",
                "sign in was blocked",
            )
        ):
            return (
                "failed",
                "Microsoft sign-in failed or was denied/blocked. Check your credentials or SMU sign-in policy before retrying.",
            )
        if await self.page.locator(
            'input[name="otc"]:visible, input[name="ProofConfirmation"]:visible'
        ).count() or any(
            text in body
            for text in (
                "help us protect your account",
                "update your password",
                "enter the characters",
            )
        ):
            return (
                "manual",
                "Microsoft requires an interactive verification step beyond Authenticator push approval. Complete it in an interactive browser; do not send passwords or verification codes to Telegram.",
            )
        if await self.page.locator('input[type="password"]:visible').count():
            return "password", (
                "entering your configured SMU password"
                if os.getenv("SMU_LOGIN_PASSWORD")
                else "enter your Microsoft password in Chrome"
            )
        if await self.page.locator(
            'input[name="loginfmt"]:visible, input[type="email"]:visible'
        ).count():
            return "email", (
                "entering your configured SMU email"
                if os.getenv("SMU_LOGIN_EMAIL", "").strip()
                else "enter your SMU email in Chrome"
            )
        if "stay signed in" in body:
            return "stay_signed_in", "keeping the Microsoft session signed in"
        if "use my password" in body:
            return "password_choice", "selecting password sign-in"
        if "verify your identity" in body or "choose a way to sign in" in body:
            return "method_picker", "selecting Microsoft Authenticator"
        if "face, fingerprint, pin or security key" in body or "/fido" in self.page.url:
            return "webauthn", "switching from device passkey to another sign-in method"
        if (
            "authenticator" in body
            or "approve a request" in body
            or "approve sign in request" in body
        ):
            number = ""
            for selector in ("#idRichContext_DisplaySign", ".displaySign"):
                display = self.page.locator(selector).first
                if await display.count() and await display.is_visible():
                    candidate = (await display.inner_text()).strip()
                    if re.fullmatch(r"[0-9]{2,3}", candidate):
                        number = candidate
                        break
            if not number:
                number = next(
                    (
                        line.strip()
                        for line in body.splitlines()
                        if re.fullmatch(r"[0-9]{2,3}", line.strip())
                    ),
                    "",
                )
            suffix = (
                f" Enter number {number}."
                if number
                else " The matching number will follow when Microsoft displays it."
            )
            return (
                "authenticator",
                f"Open Microsoft Authenticator and approve the Sagasu SMU sign-in.{suffix} Only approve if you initiated this request.",
            )
        if "pick an account" in body or "choose an account" in body:
            return "account", "selecting your configured SMU account"
        return "sign_in", "waiting for Microsoft sign-in"

    async def _capture_auth_state(self, state: str) -> None:
        """Capture auth UI locally while masking all form-field values."""

        if not self.page or state == "password":
            return
        artifact_dir = Path(
            os.getenv("SAGASU_AUTH_ARTIFACT_DIR", str(AUTH_ARTIFACT_DIR))
        )
        artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = artifact_dir / f"{stamp}-{state}.png"
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
        await frame.locator("select#TimeFrom_c1_ctl04").select_option(config.start_time)
        await self._wait_for_idle(frame)
        await frame.locator("select#TimeTo_c1_ctl04").select_option(config.end_time)
        await self._wait_for_idle(frame)
        await self._set_multi_select(
            frame, "DropMultiBuildingList_c1", config.buildings
        )
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
        await self._set_multi_select(
            frame, "DropMultiEquipmentList_c1", config.equipment
        )

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
        await booking_frame.locator("input#bookingFormControl1_TextboxPurpose_c1").fill(
            request.purpose
        )
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
                                "reference_number": (
                                    await cells[1].inner_text()
                                ).strip(),
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
        current_value = await frame.locator(
            f"#{control_id}_textValue"
        ).first.input_value()
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

    async def _room_names(self, frame: Frame) -> list[str]:
        result = []
        for row in await frame.locator("table#GridResults_gv tbody tr").all():
            cells = await row.locator("td").all()
            if len(cells) < 2:
                continue
            value = (await cells[1].inner_text()).strip()
            if value:
                result.append(value)
        return result

    async def _matching_room_names(self, frame: Frame) -> list[str]:
        pagination = frame.locator('table#GridResults_gv a[href*="Page$"]')
        if await pagination.count():
            raise LayoutChanged(
                "FBS room results are paginated; complete discovery is required"
            )
        names = await self._room_names(frame)
        if not names:
            return names
        checkboxes = frame.locator('table#GridResults_gv input[type="checkbox"]')
        if len(set(names)) != len(names) or await checkboxes.count() != len(names):
            raise LayoutChanged(
                "Matching facilities do not have unique selection controls"
            )
        async with asyncio.timeout(90):
            await self.progress("selecting all matching facilities")
            for checkbox in await checkboxes.all():
                await checkbox.check(timeout=10000)
            await self._wait_for_idle(frame)
        selected = await checkboxes.evaluate_all(
            "(els) => els.filter(el => el.checked).length"
        )
        if selected != len(names) or await self._room_names(frame) != names:
            raise LayoutChanged("Not all matching facilities are selected")
        return names

    async def _read_availability(
        self, frame: Frame, room_names: list[str], config: SearchConfig
    ) -> list[RoomAvailability]:
        try:
            from .fbs_calendar import read_complete_calendar
        except ImportError:
            if __package__:
                raise
            from fbs_calendar import read_complete_calendar

        await self._wait_for_idle(frame)
        return await read_complete_calendar(frame, room_names, config)

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
            config.get("buildings") or ("School of Computing & Information Systems 1",)
        ),
        floors=tuple(config.get("floors") or ("Level 2", "Level 3", "Level 4")),
        facility_types=tuple(config.get("facility_types") or ("Group Study Room",)),
        capacity=config.get("room_capacity"),
        equipment=tuple(config.get("equipment") or ()),
    )


async def search_fbs(
    config: SearchConfig,
    progress: ProgressCallback | None = None,
    *,
    auth_notify: ProgressCallback | None = None,
) -> SearchResult:
    try:
        async with FBSClient(progress=progress, auth_notify=auth_notify) as client:
            return await client.search(config)
    except FBSError:
        raise
    except Exception as error:
        logger.debug("Unexpected search failure (%s)", type(error).__name__)
        raise FBSError(
            "FBS search failed. Run sagasu doctor to check connectivity, browser setup and configuration."
        ) from None


async def execute_booking(
    request: BookingRequest,
    *,
    submit: bool,
    progress: ProgressCallback | None = None,
    auth_notify: ProgressCallback | None = None,
) -> dict:
    try:
        async with FBSClient(progress=progress, auth_notify=auth_notify) as client:
            return await client.book(request, submit=submit)
    except FBSError:
        raise
    except Exception as error:
        logger.debug("Unexpected booking failure (%s)", type(error).__name__)
        raise FBSError(
            "The booking outcome could not be confirmed. Check FBS My Bookings before retrying."
        ) from None
