"""Live SMU FBS client built against the authenticated October 2026 DOM.

The client is intentionally single-user.  It stores browser session state in a
local Playwright profile and supports optional privately configured credentials
and user-approved MFA renewal.  Booking submission is opt-in through ``submit=True`` only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Awaitable, Callable, Iterable
from urllib.parse import parse_qs, urlparse

from .observability import event, observed_operation, observed_step, step
from .origin import is_remote
from .action_journal import checkpoint_verified
from .personal_options import SCRAPE_PARAM_CHOICES
from .personal import SMU_EMAIL_RE

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
FBS_HOST = "fbs.intranet.smu.edu.sg"
# How long a remote read waits for Microsoft's own single sign-on redirect.
SILENT_RENEWAL_SECONDS = 25
# Microsoft pages that wait for a person. Anything else is a redirect in
# progress or an error page, and says nothing about whether sign-in is needed.
SIGN_IN_PROMPTS = frozenset(
    {
        "email",
        "password",
        "password_choice",
        "method_picker",
        "authenticator",
        "stay_signed_in",
        "account",
        "webauthn",
        "manual",
        "failed",
    }
)
PROFILE_PATH = DEFAULT_PROFILE
AUTH_TIMEOUT_MS = 600000

# Microsoft leaves opacity-zero autofill inputs in otherwise visible layouts.
# Playwright's :visible includes them; aria-hidden identifies those decoys.
LOGIN_EMAIL_SELECTOR = (
    'input[name="loginfmt"]:visible:not([aria-hidden="true"]), '
    'input[type="email"]:visible:not([aria-hidden="true"])'
)
LOGIN_PASSWORD_SELECTOR = 'input[type="password"]:visible:not([aria-hidden="true"])'
AUTH_ARTIFACT_DIR = DEFAULT_ARTIFACTS
FBS_TIMEZONE = timezone(timedelta(hours=8), "SGT")
logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]
_BROWSER_LOCK = asyncio.Lock()
_SHARED_PLAYWRIGHT = None
_SHARED_CONTEXT: BrowserContext | None = None


class FBSError(RuntimeError):
    """Base FBS client failure."""

    default_reason = None

    def __init__(self, message, *, reason=None, details=None):
        super().__init__(message)
        self.reason = reason or self.default_reason
        self.details = details or {}


class CalendarSelectionLost(FBSError):
    """The calendar did not retain exactly the requested room/time selection."""


class FBSApplicationError(FBSError):
    """FBS returned its application-error page or observed error redirect."""

    default_reason = "fbs_application_error"


class BookingOutcomeUnknown(FBSError):
    """Final confirmation was attempted but its result could not be verified."""


class AuthenticationRequired(FBSError):
    """The saved browser session is absent or expired."""

    default_reason = "session_expired"


class LayoutChanged(FBSError):
    """The live FBS DOM no longer matches the expected structure."""

    default_reason = "layout_changed"


def stops_remote_job(error) -> bool:
    """True when a remote caller met a sign-in problem only the owner can fix.

    A loop that records a failed date and moves on must re-raise instead, or
    every remaining date reloads Microsoft's sign-in page and the job still
    reports success.
    """
    return is_remote() and isinstance(error, AuthenticationRequired)


def resolve_facility_types(
    requested: Iterable[str], available: Iterable[str]
) -> tuple[str, ...]:
    requested = tuple(" ".join(value.split()) for value in requested)
    available = set(available)
    matched = tuple(value for value in requested if value in available)
    # building postbacks remove inapplicable alternatives from this list.
    if any(
        value not in available and value not in SCRAPE_PARAM_CHOICES["facility_type"]
        for value in requested
    ) or (requested and not matched):
        raise FBSError(
            "The requested facility types do not match the current FBS options.",
            reason="search_filter_unavailable",
            details={
                "requested_facility_types": [
                    value
                    for value in requested
                    if value in SCRAPE_PARAM_CHOICES["facility_type"]
                ],
                "available_facility_types": [
                    value
                    for value in SCRAPE_PARAM_CHOICES["facility_type"]
                    if value in available
                ],
            },
        )
    return matched


def calendar_time_x(headers: list[dict], value: str, header_minutes: int) -> float:
    """Map a time boundary inside an hourly (or cell) header to its screen x."""
    minute = time_to_minutes(value)
    if header_minutes <= 0:
        raise LayoutChanged("Calendar header duration is invalid")
    candidates = []
    for header in headers:
        try:
            start = time_to_minutes(header["label"])
        except ValueError:
            continue
        if start <= minute < start + header_minutes:
            candidates.append((header, start))
    # The final visible hour can provide its right edge for an exclusive end.
    if not candidates:
        for header in headers:
            try:
                start = time_to_minutes(header["label"])
            except ValueError:
                continue
            if minute == start + header_minutes:
                candidates.append((header, start))
    if len(candidates) != 1:
        raise LayoutChanged(f"Calendar time boundary is missing or ambiguous: {value}")
    header, start = candidates[0]
    if (
        not math.isfinite(header["x"])
        or not math.isfinite(header["width"])
        or header["width"] <= 0
    ):
        raise LayoutChanged("Calendar time header has invalid geometry")
    return header["x"] + header["width"] * (minute - start) / header_minutes


class BookingUnavailable(FBSError):
    """The requested room or timeslot is no longer available."""

    default_reason = "room_unavailable"


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
    complete: bool | None = True

    @property
    def bookable_rooms(self) -> list[RoomAvailability]:
        return self.ranked_rooms()

    def ranked_rooms(self, preferred_rooms=()) -> list[RoomAvailability]:
        rooms = [
            room
            for room in self.rooms
            if room.is_free(self.config.start_time, self.config.end_time)
        ]
        priorities = {
            name.casefold(): index for index, name in enumerate(preferred_rooms)
        }
        return sorted(
            rooms,
            key=lambda room: (
                priorities.get(room.name.casefold(), len(priorities)),
                room.name.casefold(),
            ),
        )

    @property
    def booking_horizon(self) -> dict | None:
        affected = [
            room.name
            for room in self.rooms
            if any(
                slot.status == "outside_booking_horizon"
                and time_to_minutes(slot.start) < time_to_minutes(self.config.end_time)
                and time_to_minutes(slot.end) > time_to_minutes(self.config.start_time)
                for slot in room.timeslots
            )
        ]
        if not affected:
            return None
        all_blocked = len(affected) == len(self.rooms)
        return {
            "reason": "outside_booking_horizon",
            "checked_rooms": len(self.rooms),
            "affected_rooms": affected,
            "all_checked_rooms_affected": all_blocked,
            "message": (
                f"FBS marks the requested time as outside the booking horizon for all {len(self.rooms)} checked rooms. "
                "For a future date, it is too far ahead to book these rooms yet; retry closer to the date."
                if all_blocked
                else "Some checked rooms are outside their booking horizon. Other rooms were checked at the same date and time; use the available choices."
            ),
            "scope": "Checked rooms only; booking horizons may differ for other facilities.",
        }

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "booking_horizon": self.booking_horizon,
            "rooms_checked": len(self.rooms),
        }


@dataclass(frozen=True)
class BookingRequest:
    search: SearchConfig
    room: str
    purpose: str
    co_booker: str
    usage_type: str = "AdHoc"
    booking_usage: str = "Meeting"
    send_calendar_invite: bool = False
    co_bookers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.room.strip():
            raise ValueError("A room is required")
        if not self.purpose.strip():
            raise ValueError("A booking purpose is required")
        if not self.co_booker.strip():
            raise ValueError("A co-booker is required")
        from .booking_options import validate_usage

        validate_usage(self.usage_type, self.booking_usage)
        if (
            not isinstance(self.co_bookers, tuple)
            or self.co_bookers
            and (
                not 1 <= len(self.co_bookers) <= 8
                or any(
                    not isinstance(email, str) or not SMU_EMAIL_RE.fullmatch(email)
                    for email in self.co_bookers
                )
                or len({email.casefold() for email in self.co_bookers})
                != len(self.co_bookers)
                or self.co_bookers[0].casefold() != self.co_booker.casefold()
            )
        ):
            raise ValueError(
                "Choose 1–8 distinct full SMU co-booker emails; the legacy co_booker must match the first entry."
            )

    @property
    def participants(self):
        return self.co_bookers or (self.co_booker,)

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
            co_bookers=tuple(value.get("co_bookers", ())),
        )

    def to_dict(self) -> dict:
        result = asdict(self)
        if not self.co_bookers:
            result.pop("co_bookers")
        return result


def require_future_booking(
    config: SearchConfig, *, now: datetime | None = None
) -> None:
    """FBS rejects an entire booking if its start is already in the past."""
    start = datetime.strptime(
        f"{config.date} {config.start_time}", "%d-%b-%Y %H:%M"
    ).replace(tzinfo=FBS_TIMEZONE)
    current = now if now is not None else datetime.now(FBS_TIMEZONE)
    if current.tzinfo is None:
        raise ValueError("Booking clock must include a timezone")
    if start <= current:
        raise BookingUnavailable(
            f"The requested start ({config.date}, {config.start_time} SGT) has already passed. "
            "Choose a future start time in /config, then search again. No booking was submitted."
        )


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
    horizon = re.search(
        r"\((\d{2}:\d{2})-(\d{2}:\d{2})\)\s*\(not within booking horizon\)",
        title,
        re.IGNORECASE,
    )
    if horizon:
        return TimeSlot(horizon.group(1), horizon.group(2), "outside_booking_horizon")
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
    logger.debug("FBS progress: %s", stage)


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
        # A remote caller holds only an API key: it may wait for the existing
        # Microsoft session to renew FBS, never start or steer a sign-in.
        self.silent_renewal = is_remote()
        self.interactive = interactive and not self.silent_renewal
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

    def _configure_password_manager(self) -> None:
        """Disable Chrome credential UI in our private profile, preserving other prefs."""
        directory = self.profile_path / "Default"
        path = directory / "Preferences"
        temporary = None
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            values = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(values, dict) or not isinstance(
                values.get("profile", {}), dict
            ):
                raise ValueError()
            values["credentials_enable_service"] = False
            values["credentials_enable_autosignin"] = False
            values.setdefault("profile", {})["password_manager_enabled"] = False
            with NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(values, stream)
            temporary.replace(path)
        except (OSError, ValueError, TypeError):
            raise FBSError(
                "Could not configure the private browser profile. Close other browsers using it and check its permissions/preferences."
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @observed_step("browser.open")
    async def __aenter__(self) -> "FBSClient":
        global _SHARED_CONTEXT, _SHARED_PLAYWRIGHT
        event("browser.lock_wait")
        await _BROWSER_LOCK.acquire()
        event("browser.lock_acquired", headless=self.headless)
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
                    event("browser.ready", reused=True)
                    return self
                except Exception:
                    _SHARED_CONTEXT = None
                    _SHARED_PLAYWRIGHT = None
            self.profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._configure_password_manager()
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
            event("browser.ready", reused=False)
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
                saved = [cookie for cookie in cookies if cookie.get("expires") == -1]
                if os.getenv("SAGASU_SESSION_COOKIES", "all").lower() == "fbs":
                    # Keep only what FBS itself receives; Microsoft's sign-in
                    # session then lives in the running browser, not this file.
                    saved = [
                        cookie
                        for cookie in saved
                        if self._sent_to_fbs(cookie.get("domain"))
                    ]
                json.dump(saved, stream)
            temporary.replace(self.session_path)
        except Exception:
            raise FBSError(
                "Private session could not be saved. Check browser-profile permissions."
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _sent_to_fbs(domain) -> bool:
        """True for a cookie domain a browser would send to the FBS host."""
        name = str(domain or "").lstrip(".").lower()
        return name.endswith("smu.edu.sg") and (
            name == FBS_HOST or FBS_HOST.endswith("." + name)
        )

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc is not None:
                from .operation_diagnostics import capture_failure

                await capture_failure(self, exc)
            if not self.keep_alive:
                try:
                    if self.context:
                        await asyncio.wait_for(self.context.close(), timeout=15)
                finally:
                    if self._playwright:
                        await asyncio.wait_for(self._playwright.stop(), timeout=15)
        finally:
            _BROWSER_LOCK.release()

    @observed_step("auth.ensure")
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
                if not (self.silent_renewal and await self._await_silent_renewal()):
                    raise AuthenticationRequired(
                        "FBS session expired; run an interactive login first"
                    ) from error
            else:
                try:
                    await self._complete_interactive_authentication()
                except Exception as auth_error:
                    if isinstance(auth_error, AuthenticationRequired):
                        raise
                    if self.page.is_closed() is True:
                        raise AuthenticationRequired(
                            "The sign-in browser closed before authentication completed.",
                            reason="authentication_interrupted",
                        ) from None
                    raise AuthenticationRequired(
                        "Microsoft sign-in could not complete.",
                        reason=(
                            "authentication_timeout"
                            if isinstance(auth_error, PlaywrightTimeoutError)
                            else "authentication_failed"
                        ),
                    ) from auth_error

        frame = await self._wait_for_frame("frameContent")
        await frame.wait_for_selector(
            "input#DateBookingFrom_c1_textDate", timeout=30000
        )
        await self._save_session()
        return self.page, frame

    async def _await_silent_renewal(self) -> bool:
        """Let Microsoft's single sign-on redirect finish. Never types or clicks.

        With a live Microsoft session the redirect returns to FBS unaided within
        seconds. A page that asks a person for anything means the owner must
        sign in on the host; a remote caller is told so and nothing is entered.
        Returns False when the redirect simply did not finish in time.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SILENT_RENEWAL_SECONDS
        previous, repeats = None, 0
        while loop.time() < deadline:
            reading = None  # None: still redirecting or loading
            try:
                if await self.page.locator("iframe#frameBottom").count():
                    event("auth.silent_renewal", outcome="renewed")
                    return True
                url = self.page.url
                if self._trusted_login_url(url):
                    state, _ = await self._classify_auth_state()
                    if state in SIGN_IN_PROMPTS:
                        reading = ("prompt", state)
                    elif state in {"provider_error", "invalid_context"}:
                        reading = ("stalled", state)
                elif not url.startswith(("https://", "http://")):
                    # The browser's own error page: FBS or the network failed.
                    reading = ("stalled", "browser_error_page")
                elif not url.startswith("https://fbs.intranet.smu.edu.sg/"):
                    reading = ("prompt", "unsupported_origin")
            except PlaywrightError:
                # A redirect replaced the document while it was being read.
                reading = None
            repeats = repeats + 1 if reading and reading == previous else 1
            previous = reading
            # Consecutive identical readings, so a page in mid-redirect is not
            # mistaken for one that has settled. A redirect chain may pass
            # through an unfamiliar origin, so that must persist for longer.
            if reading and repeats >= (8 if reading[1] == "unsupported_origin" else 2):
                kind, state = reading
                if kind == "prompt":
                    event(
                        "auth.silent_renewal", outcome="sign_in_required", state=state
                    )
                    raise AuthenticationRequired(
                        "SMU sign-in is required on the host.",
                        reason="sign_in_required_on_host",
                    )
                # An error page proves nothing about sign-in: report that the
                # session did not renew, and do not pause other requests.
                event("auth.silent_renewal", outcome="stalled", state=state)
                return False
            try:
                await self.page.wait_for_timeout(750)
            except PlaywrightError:
                break
        event("auth.silent_renewal", outcome="timeout")
        return False

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

    @classmethod
    def _trusted_login_continuation_url(cls, url: str) -> bool:
        """A Microsoft origin alone does not make a resume GET usable.

        The FIDO bridge can publish its endpoint before its transaction context
        is ready. Never navigate a bare/empty continuation or reconstruct ctx.
        """
        if not cls._trusted_login_url(url):
            return False
        parsed = urlparse(url)
        contexts = parse_qs(parsed.query, keep_blank_values=True).get("ctx", [])
        return not parsed.fragment and len(contexts) == 1 and bool(contexts[0].strip())

    @observed_step("auth.login")
    async def _complete_interactive_authentication(self) -> None:
        if not self.page:
            raise FBSError("Browser page is unavailable")
        started = asyncio.get_running_loop().time()
        previous_state = None
        email_submitted = password_submitted = method_selected = False
        email_submissions = 0
        cancelled_passkey = False
        resumed_passkeys = set()
        notifications = set()
        context_restarts = 0
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
            state_changed = state != previous_state
            if state_changed:
                event("auth.state", state=state)
            if state_changed:
                await self._capture_auth_state(state)
                # Number matching goes only to the private agent authentication prompt.
                await self.progress(
                    "Open Microsoft Authenticator to approve sign-in"
                    if state == "authenticator"
                    else prompt
                )
                previous_state = state
            if state == "invalid_context":
                # This restarts the read/login transaction only, before any
                # password submission or Authenticator request. Never replay a
                # password, MFA challenge, or downstream booking action.
                if (
                    context_restarts == 0
                    and not password_submitted
                    and not method_selected
                    and not any(item[0] == "authenticator" for item in notifications)
                ):
                    context_restarts += 1
                    await self.progress(
                        "Restarting FBS sign-in with a fresh Microsoft request"
                    )
                    event("auth.context_restarted", attempts=context_restarts)
                    await self.page.goto(
                        FBS_URL, wait_until="domcontentloaded", timeout=60000
                    )
                    email_submitted = False
                    cancelled_passkey = False
                    resumed_passkeys.clear()
                    previous_state = None
                    continue
                raise AuthenticationRequired(
                    "Microsoft rejected the sign-in request context. Start a fresh login.",
                    reason="authentication_context_invalid",
                )
            if state == "provider_error":
                raise AuthenticationRequired(
                    "Microsoft returned a sign-in error page.",
                    reason="authentication_failed",
                )
            if (
                state in {"authenticator", "failed", "manual"}
                and (state, prompt) not in notifications
            ):
                if self.auth_notify:
                    await self.auth_notify(prompt)
                elif self.headless:
                    raise AuthenticationRequired(
                        "Headless sign-in needs an agent authentication prompt callback. Use sagasu mcp or sagasu login."
                    )
                else:
                    await self.progress(prompt)
                notifications.add((state, prompt))
            if state == "failed" or (state == "manual" and self.headless):
                raise AuthenticationRequired(
                    prompt,
                    reason=(
                        "sign_in_rejected"
                        if state == "failed"
                        else "interactive_verification_required"
                    ),
                )

            trusted = self._trusted_login_url(self.page.url)
            if state == "email" and trusted:
                login_email = os.getenv("SMU_LOGIN_EMAIL", "").strip()
                field = self.page.locator(LOGIN_EMAIL_SELECTOR).first
                if login_email and (
                    not email_submitted or not (await field.input_value()).strip()
                ):
                    if email_submissions >= 2:
                        await self._stop_reset_login()
                    if email_submitted:
                        await self.progress(
                            "Microsoft restarted sign-in; restoring your email once"
                        )
                    await self._submit_login_field(
                        LOGIN_EMAIL_SELECTOR,
                        login_email,
                        "email",
                    )
                    email_submitted = True
                    email_submissions += 1
                elif not login_email and self.headless:
                    raise AuthenticationRequired(
                        "Set SMU_LOGIN_EMAIL for headless sign-in.",
                        reason="missing_email",
                    )
            elif state == "password" and trusted:
                if password_submitted and state_changed:
                    await self._stop_reset_login()
                password = os.getenv("SMU_LOGIN_PASSWORD", "")
                if password and not password_submitted:
                    await self._submit_login_field(
                        LOGIN_PASSWORD_SELECTOR, password, "password"
                    )
                    password_submitted = True
                elif not password and self.headless:
                    raise AuthenticationRequired(
                        "Set SMU_LOGIN_PASSWORD for headless password sign-in.",
                        reason="missing_password",
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
            elif state == "password_choice" and trusted:
                if not password_submitted:
                    await self.page.get_by_text("Use my password", exact=True).click()
                elif not method_selected:
                    method_selected = await self._select_authenticator()
                    if not method_selected:
                        await self._stop_reset_login()
            elif state == "account" and trusted and not email_submitted:
                email = os.getenv("SMU_LOGIN_EMAIL", "").strip()
                if email:
                    account = self.page.get_by_text(email, exact=True)
                    if await account.count():
                        await account.first.click()
                        email_submitted = True
            elif state == "webauthn" and trusted:
                # Current Microsoft pages provide a continuation to the method picker.
                # The bridge's cancelUrl can instead reset login to the email screen.
                resume = await self._passkey_resume_url()
                bridge_parameters = parse_qs(urlparse(self.page.url).query)
                resume = resume or bridge_parameters.get("resumeUrl", [""])[0]
                cancel = bridge_parameters.get("cancelUrl", [""])[0]
                if resume and self._trusted_login_continuation_url(resume):
                    if resume in resumed_passkeys or len(resumed_passkeys) >= 2:
                        await self._stop_reset_login()
                    resumed_passkeys.add(resume)
                    await self.progress("opening Microsoft's alternate sign-in methods")
                    try:
                        await self.page.goto(
                            resume, wait_until="domcontentloaded", timeout=30000
                        )
                    except PlaywrightError as error:
                        # Microsoft can supersede this navigation while still
                        # successfully displaying its credential picker.
                        if "net::ERR_ABORTED" not in str(error):
                            raise
                        await self.page.wait_for_load_state(
                            "domcontentloaded", timeout=10000
                        )
                        # The superseding document may still be hydrating.
                        for _ in range(20):
                            await self.page.wait_for_timeout(500)
                            if not self._trusted_login_url(self.page.url):
                                raise
                            next_state, _ = await self._classify_auth_state()
                            if next_state in {"password_choice", "method_picker"}:
                                break
                            if next_state not in {"sign_in", "webauthn"}:
                                raise
                        else:
                            raise
                elif (
                    not self.headless
                    and cancel
                    and self._trusted_login_continuation_url(cancel)
                    and not cancelled_passkey
                ):
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
                method_selected = await self._select_authenticator()
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
        raise AuthenticationRequired(message, reason="authentication_timeout")

    async def _passkey_resume_url(self) -> str | None:
        """Read Microsoft's supplied method-picker continuation without calling its APIs."""
        resume = await self.page.evaluate(
            """() => {
                const candidates = [];
                for (const config of [window.$Config, window.ServerData]) {
                    if (config?.fEnableDirectFidoCancelToCredPicker)
                        candidates.push(config.urlResume);
                    if (config?.acmaInitialConfig?.serverFeatureFlags?.fEnableDirectFidoCancelToCredPicker)
                        candidates.push(config.acmaInitialResponse?._links?.resume?.href);
                }
                for (const href of candidates) {
                    if (typeof href !== 'string') continue;
                    try {
                        const u = new URL(href);
                        const ctx = u.searchParams.getAll('ctx');
                        if (u.protocol === 'https:' && !u.username && !u.password &&
                            (!u.port || u.port === '443') && !u.hash &&
                            ['login.microsoftonline.com','login.microsoft.com','login.windows.net'].includes(u.hostname) &&
                            ctx.length === 1 && ctx[0].trim()) return href;
                    } catch {}
                }
                return null;
            }"""
        )
        return (
            resume
            if isinstance(resume, str) and self._trusted_login_continuation_url(resume)
            else None
        )

    async def _submit_login_field(self, selector: str, value: str, kind: str) -> None:
        """Commit field changes and verify them before submitting a Microsoft form."""
        if not self.page:
            raise AuthenticationRequired("Microsoft sign-in page is unavailable.")
        field = self.page.locator(selector).first
        submit = self.page.locator(
            'input[type="submit"]:visible, button[type="submit"]:visible'
        ).first
        # Microsoft may hydrate/replace the form after its first paint. Wait for
        # the button before typing, commit via blur, then check the resulting DOM.
        await submit.wait_for(state="visible", timeout=15000)
        for attempt in range(2):
            if not self._trusted_login_url(self.page.url):
                raise AuthenticationRequired(
                    "Microsoft sign-in origin changed; submission stopped."
                )
            await field.fill(value)
            if attempt == 1 and kind == "email":
                await field.fill("")
                await field.press_sequentially(value, delay=25)
            await field.press("Tab")
            await self.page.wait_for_timeout(250)
            if (
                self._trusted_login_url(self.page.url)
                and await field.input_value() == value
            ):
                await submit.click()
                return
            await self.progress(
                f"Microsoft replaced the {kind} field before submission; checking it again"
            )
        raise AuthenticationRequired(
            f"Microsoft cleared or changed the {kind} field before submission. "
            "No form was submitted; complete this step in the browser or retry the search."
        )

    async def _stop_reset_login(self) -> None:
        message = (
            "Microsoft restarted sign-in instead of reaching MFA. "
            "Sagasu stopped rather than resubmitting your password. "
            "Retry once with sagasu login in a visible browser; if this repeats, "
            "check that Microsoft offers Authenticator for your SMU account."
        )
        if self.auth_notify:
            await self.auth_notify(message)
        raise AuthenticationRequired(message)

    async def _select_authenticator(self) -> bool:
        if not self.page:
            return False
        options = self.page.get_by_text(
            re.compile(r"Microsoft Authenticator|Approve a request", re.I)
        )
        for option in await options.all():
            if await option.is_visible():
                await option.click()
                return True
        return False

    async def _classify_auth_state(self) -> tuple[str, str]:
        if not self.page:
            return "unknown", "waiting for Microsoft sign-in"
        body = ""
        try:
            body = (await self.page.locator("body").inner_text(timeout=2000)).casefold()
        except Exception:
            pass
        if "ctx parameter is empty or not valid" in body:
            return "invalid_context", "Microsoft lost the sign-in request context"
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
        if (
            re.search(r"\baadsts\d{5,}\b", body)
            or "sorry, but we’re having trouble signing you in" in body
            or "sorry, but we're having trouble signing you in" in body
        ):
            return "provider_error", "Microsoft returned a sign-in error page"
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
                "Microsoft requires an interactive verification step beyond Authenticator push approval. Complete it in an interactive browser; do not send passwords or verification codes in agent chat.",
            )
        if await self.page.locator(LOGIN_PASSWORD_SELECTOR).count():
            return "password", (
                "entering your configured SMU password"
                if os.getenv("SMU_LOGIN_PASSWORD")
                else "enter your Microsoft password in Chrome"
            )
        if await self.page.locator(LOGIN_EMAIL_SELECTOR).count():
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

        if not self.page or state in {"password", "authenticator"}:
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

    @observed_step("availability.search")
    async def search(
        self, config: SearchConfig, *, room_name: str | None = None
    ) -> SearchResult:
        try:
            result = await self._search_available(config, room_name=room_name)
            if (
                self.page
                and isinstance(self.page.url, str)
                and self._trusted_login_url(self.page.url)
            ):
                raise AuthenticationRequired(
                    "SMU session expired during search. Renew login before searching again."
                )
            return result
        except (PlaywrightError, LayoutChanged):
            if (
                self.page
                and isinstance(self.page.url, str)
                and self._trusted_login_url(self.page.url)
            ):
                raise AuthenticationRequired(
                    "SMU session expired during search. Renew login before searching again; no booking was submitted."
                ) from None
            raise

    async def _search_available(
        self, config: SearchConfig, *, room_name: str | None = None
    ) -> SearchResult:
        page, frame = await self.ensure_authenticated()
        event(
            "search.requested",
            scope="single_room" if room_name else "filters",
            date=config.date,
            start=config.start_time,
            end=config.end_time,
            target_room=room_name,
        )
        await self.progress("configuring availability search")
        await self._set_date(frame, config.date)
        for control, value in (
            ("TimeFrom_c1_ctl04", config.start_time),
            ("TimeTo_c1_ctl04", config.end_time),
        ):
            field = frame.locator(f"select#{control}")
            if await field.input_value() != value:
                await field.select_option(value)
                await self._wait_for_idle(frame)
        if room_name:
            await self.progress("searching only the requested facility")
            for control in (
                "DropMultiBuildingList_c1",
                "DropMultiFloorList_c1",
                "DropMultiFacilityTypeList_c1",
                "DropMultiEquipmentList_c1",
            ):
                if (
                    await frame.locator(f"#{control}_textValue").first.input_value()
                ).strip():
                    await self._set_multi_select(frame, control, ())
            await frame.locator("select#DropCapacity_c1").select_option(index=0)
            await self._wait_for_idle(frame)
            await self._set_facility_keyword(frame, room_name)
        else:
            if (
                await frame.locator("input#panel_SimpleSearch_c1").input_value()
            ).strip():
                await self._set_facility_keyword(frame, "")
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
        matching_rooms = await self._matching_room_names(frame, only_room=room_name)
        if not matching_rooms:
            return SearchResult(config, [], datetime.now().astimezone().isoformat())

        await frame.locator("a#CheckAvailability").click()
        await frame.locator("a#btnMakeBooking").wait_for(timeout=60000)
        await frame.locator("div.scheduler_bluewhite_rowheader_inner").first.wait_for(
            timeout=30000
        )
        await page.wait_for_timeout(1000)

        rooms = await self._read_availability(frame, matching_rooms, config)
        event(
            "search.results",
            rooms=len(rooms),
            free_rooms=sum(
                room.is_free(config.start_time, config.end_time) for room in rooms
            ),
        )
        return SearchResult(config, rooms, datetime.now().astimezone().isoformat())

    @observed_step("booking.execute")
    async def book(self, request: BookingRequest, *, submit: bool = False) -> dict:
        """Prepare or submit a booking.

        ``submit=False`` stops after every required field and checkbox is populated.
        Only ``submit=True`` clicks FBS's final CONFIRM control.
        """

        event("booking.requested", submit=submit)
        require_future_booking(request.search)
        result = await self.search(request.search, room_name=request.room)
        require_future_booking(request.search)
        room = next((item for item in result.rooms if item.name == request.room), None)
        if not room or not room.is_free(
            request.search.start_time, request.search.end_time
        ):
            if result.booking_horizon:
                await self.progress(
                    "Checking other rooms at the same time for booking-horizon restrictions"
                )
                try:
                    alternatives = await self.search(request.search)
                except FBSError:
                    raise BookingUnavailable(
                        "The requested room is outside its booking horizon. "
                        "Other rooms could not be checked, so a shared booking-horizon restriction is unverified. "
                        "Retry the search. No booking was submitted.",
                        reason="outside_booking_horizon",
                    ) from None
                diagnosis = alternatives.booking_horizon
                message = (
                    diagnosis["message"]
                    if diagnosis
                    else "The requested room is outside its booking horizon."
                )
                raise BookingUnavailable(
                    message
                    + f" Checked {len(alternatives.rooms)} rooms at the same date/time; "
                    f"{len(alternatives.bookable_rooms)} alternatives are available. "
                    "Run a fresh search to review alternatives before booking. No booking was submitted.",
                    reason="outside_booking_horizon",
                    details={
                        "checked_rooms": len(alternatives.rooms),
                        "available_total": len(alternatives.bookable_rooms),
                        "all_checked_rooms_affected": bool(
                            diagnosis and diagnosis["all_checked_rooms_affected"]
                        ),
                    },
                )
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
        booking_frame = await self._prepare_booking_frame(
            frame, request.search, request.room
        )
        with step("booking.fill_form"):
            await booking_frame.locator(
                "input#bookingFormControl1_TextboxPurpose_c1"
            ).fill(request.purpose)
            await booking_frame.locator(
                "select#bookingFormControl1_DropDownUsageType_c1"
            ).select_option(label=request.usage_type)
            await self._wait_for_idle(booking_frame)
            await booking_frame.locator(
                "select#bookingFormControl1_DropDownSpaceBookingUsage_c1"
            ).select_option(label=request.booking_usage)
            await self._wait_for_idle(booking_frame)
            await self.progress("adding co-booker to booking form")
            selected_names = [
                await self._add_co_booker(booking_frame, email)
                for email in request.participants
            ]
            if len(request.participants) > 1:
                rows = booking_frame.locator(
                    "#bookingFormControl1_GridCoBookers_content tr.row"
                )
                names = []
                for row in await rows.all():
                    cells = row.locator("td")
                    if await cells.count() != 2:
                        raise LayoutChanged("The co-booker form rows changed")
                    names.append(" ".join((await cells.nth(1).inner_text()).split()))
                if Counter(names) != Counter(selected_names):
                    raise LayoutChanged(
                        "Not all selected co-bookers were retained in the unsubmitted form"
                    )

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

        await self._verify_booking_details(booking_frame, request)
        require_future_booking(request.search)
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
        if request.co_bookers:
            preview["co_bookers"] = list(request.co_bookers)
            preview["co_booker_verification"] = (
                "Exact directory email selections and retained name-row counts; native form rows do not expose email addresses."
            )
        if not submit:
            event("booking.validation_passed", submitted=False)
            return preview

        await self.progress("submitting confirmed booking")
        confirm = booking_frame.locator("a#panel_UIButton2").first
        try:
            with step("booking.submit"):
                await confirm.click()
            await self.page.wait_for_timeout(2000)
            preview.update(await self._verify_booking(request))
        except Exception:
            raise BookingOutcomeUnknown(
                "Final confirmation was attempted, but its outcome could not be verified. "
                "Check FBS My Bookings before creating another booking."
            ) from None
        checkpoint_verified(preview)
        # Receipt failures must never turn an already verified booking into an
        # uncertain submission or invite the user to book it again.
        try:
            from .fbs_account import capture_booking_evidence

            preview["receipt_png"] = await capture_booking_evidence(
                self, preview.get("reference_number", "")
            )
        except Exception as error:
            logger.warning(
                "Verified booking receipt unavailable (%s)", type(error).__name__
            )
            preview["receipt_error"] = (
                "Your booking is verified, but its details screenshot could not be captured. "
                "View this reference in FBS My Bookings; do not create a duplicate booking."
            )
        return preview

    @observed_step("booking.verify_form")
    async def _verify_booking_details(
        self, frame: Frame, request: BookingRequest
    ) -> None:
        """Check the server's selected room/datetimes before any final confirmation."""
        labels = frame.get_by_text(request.room, exact=True)
        if not any([await label.is_visible() for label in await labels.all()]):
            raise BookingUnavailable(
                "FBS booking details do not show the requested room. Nothing was confirmed."
            )
        day = datetime.strptime(request.search.date, "%d-%b-%Y").strftime("%Y-%m-%d")
        for suffix, time_value in (
            ("Start", request.search.start_time),
            ("End", request.search.end_time),
        ):
            value = await frame.locator(
                f"select#bookingFormControl1_DropDown{suffix}Time_c1"
            ).input_value()
            try:
                actual = datetime.strptime(value, "%m/%d/%Y %I:%M:%S %p")
            except ValueError:
                raise LayoutChanged(
                    "FBS booking details contain an unreadable date/time. Nothing was confirmed."
                ) from None
            expected = datetime.fromisoformat(f"{day}T{time_value}:00")
            if actual != expected:
                raise BookingUnavailable(
                    "FBS booking details do not match the requested date/time. Nothing was confirmed."
                )
        if (
            await frame.locator(
                "input#bookingFormControl1_TextboxPurpose_c1"
            ).input_value()
            != request.purpose
        ):
            raise BookingUnavailable(
                "FBS changed the booking purpose. Nothing was confirmed."
            )
        for control, expected in (
            ("DropDownUsageType_c1", request.usage_type),
            ("DropDownSpaceBookingUsage_c1", request.booking_usage),
        ):
            selected = await frame.locator(
                f"select#bookingFormControl1_{control} option:checked"
            ).all_text_contents()
            if selected != [expected]:
                raise BookingUnavailable(
                    "FBS changed the requested booking classification. Nothing was confirmed."
                )

    @observed_step("booking.verify_result")
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
                            event(
                                "booking.verified",
                                fbs_status=(await cells[8].inner_text()).strip(),
                            )
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

    @observed_step("search.date")
    async def _set_date(self, frame: Frame, target_value: str) -> None:
        target = datetime.strptime(target_value, "%d-%b-%Y").date()
        field = frame.locator("input#DateBookingFrom_c1_textDate")
        current = datetime.strptime(await field.input_value(), "%d-%b-%Y").date()
        if current != target and (current.year, current.month) == (
            target.year,
            target.month,
        ):
            # use the visible native picker; readonly date fields are not edited.
            await field.click()
            day = frame.locator(
                f'#__calendar_daysTable div.day[title="{target_value}"]'
            )
            if await day.count() != 1:
                raise LayoutChanged("FBS date-picker day could not be verified")
            await day.click()
            await self._wait_for_idle(frame)
            if await field.input_value() != target_value:
                raise LayoutChanged("FBS date-picker selection was not retained")
            return
        for _ in range(32):
            current_value = await field.input_value()
            current = datetime.strptime(current_value, "%d-%b-%Y").date()
            if current == target:
                return
            selector = "a#BtnDpcNext" if current < target else "a#BtnDpcPrev"
            await frame.locator(selector).click()
            await self._wait_for_idle(frame)
        raise FBSError(f"Could not navigate FBS calendar to {target_value}")

    @observed_step("search.filter")
    async def _set_multi_select(
        self, frame: Frame, control_id: str, values: Iterable[str]
    ) -> None:
        values = tuple(values)
        event(
            "search.filter_configured",
            level=logging.DEBUG,
            control=control_id,
            selections=len(values),
        )
        await self._wait_for_idle(frame)
        opener = frame.locator(f"#{control_id}_textItem:visible").first
        current_value = await frame.locator(
            f"#{control_id}_textValue"
        ).first.input_value()
        if not values and not current_value.strip():
            return
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
        if control_id == "DropMultiFacilityTypeList_c1" and values:
            choices = frame.locator(f'[id="{control_id}::ddl::"]')
            if await choices.count() != 1:
                raise LayoutChanged("FBS facility-type options could not be verified")
            labels = [
                " ".join(label.split())
                for label in await choices.locator("label").all_text_contents()
            ]
            if (
                not labels
                or len(set(labels)) != len(labels)
                or any(not label for label in labels)
            ):
                raise LayoutChanged(
                    "FBS facility-type options are unreadable or ambiguous"
                )
            values = resolve_facility_types(values, labels)
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
            """() => {
                const manager = window.Sys && Sys.WebForms && Sys.WebForms.PageRequestManager;
                if (manager && manager.getInstance().get_isInAsyncPostBack()) return false;
                return [...document.querySelectorAll('#__updateProgress__')].every((el) => {
                const style = window.getComputedStyle(el);
                return style.display === 'none' || style.visibility === 'hidden' ||
                    style.pointerEvents === 'none' || el.getBoundingClientRect().height === 0;
                });
            }""",
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

    @observed_step("search.facility_keyword")
    async def _set_facility_keyword(self, frame: Frame, room_name: str) -> None:
        field = frame.locator("input#panel_SimpleSearch_c1")
        if not await field.is_visible():
            watermark = frame.locator("#panel_SimpleSearch input:visible")
            if not await watermark.count():
                watermark = field.locator("xpath=..").locator("input:visible")
            await watermark.first.click()
        await field.wait_for(state="visible", timeout=10000)
        await field.fill(room_name)
        await frame.locator("a#panel_buttonSimpleSearch").click()
        await self._wait_for_idle(frame)

    @observed_step("facilities.select")
    async def _matching_room_names(
        self, frame: Frame, *, only_room: str | None = None
    ) -> list[str]:
        pagination = frame.locator('table#GridResults_gv a[href*="Page$"]')
        if await pagination.count():
            raise LayoutChanged(
                "FBS room results are paginated; complete discovery is required"
            )
        names = await self._room_names(frame)
        if not names or (only_room and only_room not in names):
            return []
        checkboxes = frame.locator('table#GridResults_gv input[type="checkbox"]')
        if len(set(names)) != len(names) or await checkboxes.count() != len(names):
            raise LayoutChanged(
                "Matching facilities do not have unique selection controls"
            )
        async with asyncio.timeout(90):
            await self.progress(
                "selecting the requested facility only"
                if only_room
                else "selecting all matching facilities"
            )
            for name, checkbox in zip(names, await checkboxes.all()):
                if only_room:
                    await checkbox.set_checked(name == only_room, timeout=10000)
                else:
                    await checkbox.check(timeout=10000)
            await self._wait_for_idle(frame)
        selected = await checkboxes.evaluate_all(
            "(els) => els.filter(el => el.checked).length"
        )
        expected = 1 if only_room else len(names)
        if selected != expected or await self._room_names(frame) != names:
            raise LayoutChanged(
                "The requested facility selection could not be verified"
            )
        event("facilities.selected", discovered=len(names), selected=selected)
        return [only_room] if only_room else names

    @observed_step("calendar.read")
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

    async def _calendar_headers(self, frame: Frame) -> tuple[list[dict], int]:
        metadata = await frame.evaluate(
            """() => {
            const s = window.dps;
            const headers = s && s.timeHeaders;
            return {group: headers && headers.length ? headers[headers.length - 1].groupBy : null,
                cell: s && s.cellDuration};
        }"""
        )
        duration = (
            60
            if metadata["group"] == "Hour"
            else metadata["cell"] if metadata["group"] == "Cell" else None
        )
        if not isinstance(duration, (int, float)) or duration <= 0:
            raise LayoutChanged("Calendar time-header scale is unsupported")
        headers = []
        for element in await frame.locator(
            "#dps .scheduler_bluewhite_timeheadercol"
        ).all():
            label = (await element.inner_text()).strip()
            box = await element.bounding_box()
            if box and box["width"] > 0:
                headers.append({"label": label, **box})
        return headers, duration

    async def _calendar_row_box(self, frame: Frame, scroll, room_name: str) -> dict:
        await scroll.evaluate("el => {el.scrollTop = 0;}")
        for _ in range(200):
            await frame.wait_for_timeout(250)
            view = await scroll.bounding_box()
            metrics = await scroll.evaluate(
                "el => ({top:el.scrollTop, height:el.clientHeight, full:el.scrollHeight})"
            )
            if not view or metrics["height"] <= 0:
                raise LayoutChanged("Calendar scroll area is not visible")
            matches = []
            for row in await frame.locator(
                "#dps .scheduler_bluewhite_rowheader_inner"
            ).all():
                if (await row.inner_text()).strip() == room_name:
                    box = await row.bounding_box()
                    if box and box["height"] > 0:
                        matches.append(box)
            if len(matches) > 1:
                raise LayoutChanged("Requested calendar room is ambiguous")
            if matches:
                box = matches[0]
                center = box["y"] + box["height"] / 2
                margin = min(40, metrics["height"] / 3)
                top_safe = center > view["y"] + margin or metrics["top"] <= 0
                bottom_safe = (
                    center < view["y"] + metrics["height"] - margin
                    or metrics["top"] >= metrics["full"] - metrics["height"] - 1
                )
                if top_safe and bottom_safe:
                    return box
                target = metrics["top"] + center - (view["y"] + metrics["height"] / 2)
            else:
                target = metrics["top"] + max(1, metrics["height"] * 0.75)
            target = max(0, min(metrics["full"] - metrics["height"], target))
            if abs(target - metrics["top"]) < 1:
                break
            await scroll.evaluate("(el, top) => {el.scrollTop = top;}", target)
        raise LayoutChanged(f"Calendar row could not be brought into view: {room_name}")

    async def _selection_snapshot(self, frame: Frame) -> list[dict]:
        result = await frame.evaluate(
            """() => {
            const s = window.dps;
            if (!s || !Array.isArray(s.resources) || !s.events ||
                !Array.isArray(s.events.list)) return null;
            const manager = window.Sys && Sys.WebForms && Sys.WebForms.PageRequestManager;
            if (manager && manager.getInstance().get_isInAsyncPostBack()) return [];
            const names = new Map();
            const visit = rows => rows.forEach(row => {
                if (row.children && row.children.length) visit(row.children);
                else names.set(String(row.id), row.name);
            });
            visit(s.resources);
            // range.all() is only the transient mouse selection. FBS clears it
            // after its postback and renders committed selections as events.
            // Inspect only the observed FBS selection status and tooltip fields;
            // never export booking titles, attendees or the rest of the tag.
            return s.events.list.filter(e => Array.isArray(e.tag) &&
                String(e.tag[7]) === '10' &&
                /^\\d{2}:\\d{2} - \\d{2}:\\d{2} \\(selected\\)$/.test(e.tag[10])
            ).map(e => ({
                room: names.get(String(e.resource)) || null,
                start: String(e.start), end: String(e.end),
            }));
        }"""
        )
        if not isinstance(result, list):
            raise LayoutChanged(
                "FBS calendar selection cannot be verified; Make Booking was not clicked."
            )
        return result

    @observed_step("calendar.verify_selection")
    async def _verify_calendar_selection(
        self, frame: Frame, room: str, start: str, end: str, date: str | None = None
    ) -> None:
        expected_day = (
            datetime.strptime(date, "%d-%b-%Y").strftime("%Y-%m-%d") if date else None
        )

        def matches(ranges):
            if len(ranges) != 1:
                return False
            selection = ranges[0]
            try:
                actual_start = datetime.fromisoformat(selection["start"])
                actual_end = datetime.fromisoformat(selection["end"])
            except (KeyError, ValueError, TypeError):
                return False
            return (
                selection.get("room") == room
                and actual_start.strftime("%H:%M:%S") == start + ":00"
                and actual_end.strftime("%H:%M:%S") == end + ":00"
                and actual_start.date() == actual_end.date()
                and (
                    expected_day is None
                    or actual_start.strftime("%Y-%m-%d") == expected_day
                )
            )

        # Require stable server-rendered selected slots after mouse-up. The
        # transient green drag preview is not evidence of server acceptance.
        consecutive = 0
        for _ in range(120):
            await self._check_calendar_errors(frame)
            if matches(await self._selection_snapshot(frame)):
                consecutive += 1
                if consecutive >= 2:
                    event("selection.verified", stable_snapshots=consecutive)
                    return
            else:
                consecutive = 0
            await frame.wait_for_timeout(250)
        raise CalendarSelectionLost(
            "FBS did not retain the requested room and timeslot. No booking was submitted. "
            "Start a fresh search and try validation again."
        )

    async def _check_calendar_errors(self, frame: Frame) -> None:
        errors = await frame.locator(".error:visible").all_text_contents()
        for text in errors:
            reason = re.sub(r"^\s*dismiss\b", "", text, flags=re.I)
            reason = " ".join(reason.split())
            if not reason:
                continue
            if "click and drag to select timeslot" in reason.casefold():
                raise CalendarSelectionLost(
                    "FBS rejected the calendar selection before opening booking details. No booking was submitted."
                )
            if "past booking slots" in reason.casefold():
                raise BookingUnavailable(
                    "FBS rejected this timeslot because its start time has passed. "
                    "Choose a future start time in /config, then search again. No booking was submitted."
                )
            raise BookingUnavailable(
                f"FBS rejected the selected timeslot: {reason[:1200]} No booking was submitted."
            )

    @observed_step("booking.prepare")
    async def _prepare_booking_frame(
        self, frame: Frame, search: SearchConfig, room: str
    ) -> Frame:
        for attempt in range(2):
            try:
                # A rejected previous attempt leaves a toast behind. Dismiss it
                # before retrying so it cannot be mistaken for a new rejection.
                dismiss = frame.locator(".error:visible").get_by_text(
                    re.compile(r"^dismiss$", re.I)
                )
                for control in await dismiss.all():
                    await control.click()
                await self._drag_timeslot(
                    frame,
                    room,
                    search.start_time,
                    search.end_time,
                    date=search.date,
                )
                button = frame.locator("a#btnMakeBooking")
                # Moving to the toolbar can trigger scrolling or a late mouse
                # handler. Verify once more after that movement, before clicking.
                await button.hover()
                await self._verify_calendar_selection(
                    frame,
                    room,
                    search.start_time,
                    search.end_time,
                    search.date,
                )
                await button.click()
                for _ in range(60):
                    details = self.page.frame(name="frameBookingDetails")
                    if (
                        details
                        and await details.locator(
                            "input#bookingFormControl1_TextboxPurpose_c1"
                        ).is_visible()
                    ):
                        return details
                    current = self.page.frame(name="frameContent")
                    if current:
                        await self._check_calendar_errors(current)
                    await self.page.wait_for_timeout(500)
                raise LayoutChanged(
                    "FBS did not open booking details after Make Booking. No final confirmation was attempted."
                )
            except CalendarSelectionLost:
                event(
                    "selection.retry",
                    level=logging.WARNING,
                    attempt=attempt + 1,
                    retrying=not bool(attempt),
                )
                if attempt:
                    raise
                await self.progress(
                    "FBS cleared the selection; retrying the room/time selection once"
                )
                frame = self.page.frame(name="frameContent")
                if not frame:
                    raise LayoutChanged(
                        "FBS calendar disappeared during selection recovery"
                    ) from None
                clear = frame.get_by_text(re.compile(r"^clear selection$", re.I))
                if await clear.count():
                    await clear.first.click()
                    await self._wait_for_idle(frame)
        raise CalendarSelectionLost(
            "FBS calendar selection failed before booking preparation."
        )

    @observed_step("calendar.select")
    async def _drag_timeslot(
        self,
        frame: Frame,
        room_name: str,
        start_time: str,
        end_time: str,
        *,
        date: str | None = None,
    ) -> None:
        if not self.page:
            raise FBSError("Browser page is unavailable")
        scroll = frame.locator("#dps .scheduler_bluewhite_scrollable")
        if await scroll.count() != 1:
            raise LayoutChanged("Calendar scroll area is not uniquely identifiable")
        row_box = await self._calendar_row_box(frame, scroll, room_name)
        # Locator.bounding_box uses main-page coordinates, including iframe offsets.
        # FBS labels hours but has half-hour cells; interpolate within each hour.
        for _ in range(4):
            headers, duration = await self._calendar_headers(frame)
            start_x = calendar_time_x(headers, start_time, duration) + 2
            end_x = calendar_time_x(headers, end_time, duration) - 2
            view = await scroll.bounding_box()
            metrics = await scroll.evaluate(
                "el => ({left:el.scrollLeft, width:el.clientWidth, full:el.scrollWidth})"
            )
            if not view or end_x <= start_x or end_x - start_x > metrics["width"] - 8:
                raise LayoutChanged(
                    "Requested calendar range does not fit the visible grid"
                )
            margin = min(40, metrics["width"] / 4)
            left_safe = start_x >= view["x"] + margin or metrics["left"] <= 0
            right_safe = (
                end_x <= view["x"] + metrics["width"] - margin
                or metrics["left"] >= metrics["full"] - metrics["width"] - 1
            )
            if (
                left_safe
                and right_safe
                and view["x"] + 2 <= start_x < end_x <= view["x"] + metrics["width"] - 2
            ):
                break
            target = (
                metrics["left"]
                + (start_x + end_x) / 2
                - (view["x"] + metrics["width"] / 2)
            )
            target = max(0, min(metrics["full"] - metrics["width"], target))
            await scroll.evaluate("(el, left) => {el.scrollLeft = left;}", target)
            await frame.wait_for_timeout(250)
        else:
            raise LayoutChanged("Calendar time range could not be brought into view")
        y = row_box["y"] + row_box["height"] / 2
        await self.page.mouse.move(start_x, y)
        await self.page.mouse.down()
        try:
            await frame.wait_for_timeout(100)
            await self.page.mouse.move(end_x, y, steps=20)
            await frame.wait_for_timeout(100)
        finally:
            await self.page.mouse.up()
        await self._wait_for_idle(frame)
        await self._verify_calendar_selection(
            frame, room_name, start_time, end_time, date
        )

    async def _wait_for_frame(self, name: str) -> Frame:
        if not self.page:
            raise FBSError("Browser page is unavailable")
        for _ in range(60):
            frame = self.page.frame(name=name)
            if frame:
                return frame
            await self.page.wait_for_timeout(500)
        raise LayoutChanged(f"FBS frame was not created: {name}")

    @observed_step("cobooker.search")
    async def _search_co_booker_picker(self, frame: Frame, query: str) -> None:
        await frame.locator("a#bookingFormControl1_GridCoBookers_ctl14").click()
        search_input = frame.locator(
            "input#bookingFormControl1_DialogSearchCoBooker_searchPanel_textBox_c1"
        )
        # This legacy FBS control displays a watermark input and hides its real
        # textbox until the visible watermark is focused.
        if not await search_input.is_visible():
            watermark = frame.locator(
                "#bookingFormControl1_DialogSearchCoBooker_searchPanel_textBox input:visible"
            ).first
            await watermark.click(timeout=15000)
        await search_input.wait_for(state="visible", timeout=15000)
        await search_input.fill(query)
        await self.progress("searching for the requested co-booker")
        await frame.locator(
            "a#bookingFormControl1_DialogSearchCoBooker_searchPanel_buttonSearch"
        ).click()
        await self._wait_for_idle(frame)

    @observed_step("booking.cobooker")
    async def _add_co_booker(self, frame: Frame, query: str) -> str:
        if not SMU_EMAIL_RE.fullmatch(query):
            raise ValueError("Select a verified full SMU co-booker email first")
        await self._search_co_booker_picker(frame, query)
        result_link = frame.locator(
            "table#bookingFormControl1_DialogSearchCoBooker_searchPanel_gridView_gv"
        ).get_by_text(query, exact=False)
        await result_link.first.wait_for(timeout=20000)
        if await result_link.count() != 1:
            raise LayoutChanged("The exact co-booker email result is ambiguous")
        selected_label = (await result_link.first.inner_text()).strip()
        if not re.fullmatch(
            r".+?\s+\(" + re.escape(query) + r"\)", selected_label, re.I
        ):
            raise LayoutChanged(
                "The directory result does not match the exact requested email"
            )
        selected_name = re.sub(
            r"\s*\(" + re.escape(query) + r"\)\s*$", "", selected_label, flags=re.I
        ).strip()
        row = result_link.first.locator("xpath=ancestor::tr[1]")
        checkbox = row.locator('input[type="checkbox"]')
        if await checkbox.count() == 0:
            raise LayoutChanged("Co-booker result checkbox was not found")
        await checkbox.check()
        await self.progress("adding the matched co-booker")
        await frame.locator(
            "a#bookingFormControl1_DialogSearchCoBooker_dialogBox_b1"
        ).click()

        # FBS duplicates this table id for its header/body rendering. Match the
        # email or the name captured from that exact email search result.
        identity = re.compile(
            "|".join(re.escape(value) for value in {query, selected_name} if value),
            re.I,
        )
        grid = (
            frame.locator("table#bookingFormControl1_GridCoBookers_gv:visible")
            .filter(has_text=identity)
            .first
        )
        await grid.wait_for(timeout=15000)
        added = grid.get_by_text(identity).first
        await added.wait_for(timeout=15000)
        added_row = added.locator("xpath=ancestor::tr[1]")
        added_checkbox = added_row.locator('input[type="checkbox"]')
        if await added_checkbox.count():
            await added_checkbox.check()
        return " ".join(selected_name.split())


def search_config_from_preferences(config: dict) -> SearchConfig:
    """Translate saved agent search preferences into the live FBS contract."""

    now = datetime.now(FBS_TIMEZONE)
    duration = float(config.get("duration_hrs", 2))
    next_slot = now.replace(
        minute=now.minute // 30 * 30, second=0, microsecond=0
    ) + timedelta(minutes=30)
    # FBS's current search contract covers one calendar date. Keep the rolling
    # default as a full window rather than truncating it at midnight.
    if (next_slot + timedelta(hours=duration)).date() != next_slot.date():
        next_slot = (next_slot + timedelta(days=1)).replace(hour=0, minute=0)
    date_raw = config.get("date_raw") or (
        now if config.get("start_time") else next_slot
    ).strftime("%d %B %Y")
    date = datetime.strptime(date_raw.title(), "%d %B %Y").strftime("%d-%b-%Y")
    start = config.get("start_time") or next_slot.strftime("%H:%M")
    end = minutes_to_time(time_to_minutes(start) + int(duration * 60))
    return SearchConfig(
        date=date,
        start_time=start,
        end_time=end,
        buildings=tuple(
            config["buildings"]
            if config.get("buildings") is not None
            else ("School of Computing & Information Systems 1",)
        ),
        floors=tuple(
            config["floors"]
            if config.get("floors") is not None
            else ("Level 2", "Level 3", "Level 4")
        ),
        facility_types=tuple(
            config["facility_types"]
            if config.get("facility_types") is not None
            else ("Group Study Room",)
        ),
        capacity=config.get("room_capacity"),
        equipment=tuple(config.get("equipment") or ()),
    )


@observed_operation("backend.search")
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


@observed_operation("backend.booking")
async def execute_booking(
    request: BookingRequest,
    *,
    submit: bool,
    progress: ProgressCallback | None = None,
    auth_notify: ProgressCallback | None = None,
    interactive: bool = True,
) -> dict:
    try:
        require_future_booking(request.search)
        async with FBSClient(
            progress=progress, auth_notify=auth_notify, interactive=interactive
        ) as client:
            return await client.book(request, submit=submit)
    except FBSError:
        raise
    except Exception as error:
        logger.debug("Unexpected booking failure (%s)", type(error).__name__)
        raise FBSError(
            "The booking outcome could not be confirmed. Check FBS My Bookings before retrying."
        ) from None


@observed_operation("backend.tasks")
async def check_task_list(
    progress: ProgressCallback | None = None,
    *,
    auth_notify: ProgressCallback | None = None,
) -> dict:
    from .fbs_account import read_task_list

    try:
        async with FBSClient(progress=progress, auth_notify=auth_notify) as client:
            return await read_task_list(client)
    except FBSError:
        raise
    except Exception as error:
        logger.debug("Task List check failed (%s)", type(error).__name__)
        raise FBSError(
            "Could not read FBS Task List. Try /tasks again or open it in FBS."
        ) from None
