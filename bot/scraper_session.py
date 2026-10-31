"""Private, read-only scraper sessions; deliberately separate from bot login."""

import asyncio
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from .fbs_client import AuthenticationRequired, FBSClient, FBS_URL, PROFILE_PATH

ROOT = Path(__file__).resolve().parents[1]


class SessionStorageError(RuntimeError):
    """Private session state could not be saved; never expose browser details."""


class ProfileBusy(RuntimeError):
    """Another local process owns the scraper profile."""


def scraper_profile():
    path = (
        Path(
            os.getenv("SAGASU_SCRAPER_PROFILE", str(ROOT / "artifacts/scraper-profile"))
        )
        .expanduser()
        .resolve()
    )
    bot_profile = (
        Path(os.getenv("SAGASU_BROWSER_PROFILE", str(PROFILE_PATH)))
        .expanduser()
        .resolve()
    )
    if (
        path == bot_profile
        or path in bot_profile.parents
        or bot_profile in path.parents
    ):
        raise ValueError("The scraper profile must be separate from the bot profile")
    # Custom profiles must remain outside tracked directories too.
    if path.is_relative_to(ROOT) and not path.is_relative_to(ROOT / "artifacts"):
        raise ValueError("Repository-local scraper profiles must be under artifacts")
    return path


@contextmanager
def profile_lock(path):
    """Nonblocking kernel lock; crashes release it without stale PID recovery."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    # Never unlink the lock: a waiter may already hold an open descriptor.
    descriptor = os.open(path / ".sagasu.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProfileBusy(
                "Scraper profile is in use; try after the other run ends"
            ) from None
        yield
    finally:
        os.close(descriptor)


class ScraperClient(FBSClient):
    """Never automate credentials or enter the booking path, even during MFA."""

    def __init__(self, *, manual_login=False, login_timeout=600, **kwargs):
        super().__init__(
            interactive=False,
            unattended=False,
            keep_alive=False,
            strict_availability=True,
            **kwargs,
        )
        self.manual_login = manual_login
        self.login_timeout = login_timeout

    @property
    def session_path(self):
        return self.profile_path / ".sagasu-session.json"

    async def __aenter__(self):
        await super().__aenter__()
        try:
            if self.session_path.exists():
                try:
                    cookies = json.loads(self.session_path.read_text(encoding="utf-8"))
                    if not isinstance(cookies, list):
                        raise ValueError("Invalid private session state")
                    async with asyncio.timeout(15):
                        await self.context.add_cookies(cookies)
                except Exception:
                    if not self.manual_login:
                        raise AuthenticationRequired(
                            "Saved session is unreadable; run python -m bot.scraper_login"
                        ) from None
                    # Manual renewal can repair missing/corrupt saved state.
            return self
        except BaseException:
            await super().__aexit__(None, None, None)
            raise

    async def _save_session(self):
        """Chrome drops session cookies on restart; keep them private locally.

        Preserve original expiry values. The server still decides when the
        session expires, and persistent cookies remain managed by Chrome.
        """
        temporary = None
        try:
            async with asyncio.timeout(15):
                cookies = await self.context.cookies()
            session_cookies = [cookie for cookie in cookies if cookie["expires"] == -1]
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".sagasu-session-",
                suffix=".tmp",
                dir=self.profile_path,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                # NamedTemporaryFile creates the file with mode 0600.
                json.dump(session_cookies, stream)
            temporary.replace(self.session_path)
        except Exception:
            raise SessionStorageError(
                "Unable to save private scraper session"
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def ensure_authenticated(self):
        if not self.page:
            raise AuthenticationRequired("Scraper browser is unavailable")
        try:
            # The outer deadline covers navigation, redirects and readiness checks.
            async with asyncio.timeout(self.login_timeout if self.manual_login else 90):
                await self.page.goto(
                    FBS_URL, wait_until="domcontentloaded", timeout=60000
                )
                while True:
                    if self.page.is_closed():
                        raise AuthenticationRequired("Scraper browser was closed")
                    if self.page.url.startswith("https://fbs.intranet.smu.edu.sg/"):
                        frame = self.page.frame(name="frameContent")
                        # A shell/navigation iframe alone does not prove FBS is ready.
                        if frame and frame.url.startswith(
                            "https://fbs.intranet.smu.edu.sg/"
                        ):
                            try:
                                for selector in (
                                    "input#DateBookingFrom_c1_textDate",
                                    "select#TimeFrom_c1_ctl04",
                                    "select#TimeTo_c1_ctl04",
                                    "#DropMultiFacilityTypeList_c1_textItem",
                                    "table#GridResults_gv",
                                ):
                                    await frame.locator(selector).wait_for(
                                        state="visible", timeout=1000
                                    )
                            except Exception:
                                # Redirects/postbacks can replace the frame.
                                await asyncio.sleep(0.5)
                                continue
                            await self._save_session()
                            return self.page, frame
                    elif not self.manual_login:
                        raise AuthenticationRequired(
                            "Session expired; run python -m bot.scraper_login"
                        )
                    await asyncio.sleep(0.5)
        except (AuthenticationRequired, SessionStorageError):
            raise
        except Exception:
            raise AuthenticationRequired(
                "FBS session is not ready; run python -m bot.scraper_login"
            ) from None

    async def book(self, *args, **kwargs):
        raise RuntimeError("The public scraper cannot book facilities")
