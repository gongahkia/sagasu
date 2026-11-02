"""Configuration resolution shared by the CLI and browser client."""

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

CANDIDATE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = CANDIDATE_ROOT if (CANDIDATE_ROOT / "pyproject.toml").is_file() else None
if sys.platform == "darwin":
    USER_STATE = Path.home() / "Library/Application Support/Sagasu"
elif os.name == "nt":
    USER_STATE = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Sagasu"
else:
    USER_STATE = (
        Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "sagasu"
    )
DEFAULT_PROFILE = (
    SOURCE_ROOT / "bot/.fbs-profile" if SOURCE_ROOT else USER_STATE / "browser"
)
DEFAULT_PERSISTENCE = (
    SOURCE_ROOT / "bot/bot_state.pickle"
    if SOURCE_ROOT
    else USER_STATE / "bot_state.pickle"
)
DEFAULT_ARTIFACTS = (
    SOURCE_ROOT / "artifacts/auth" if SOURCE_ROOT else USER_STATE / "auth"
)
DEFAULT_ENV = (SOURCE_ROOT or Path.cwd()) / ".env"
BROWSERS = (
    "chromium",
    "chrome",
    "chrome-beta",
    "chrome-dev",
    "chrome-canary",
    "msedge",
    "msedge-beta",
    "msedge-dev",
)
TOKEN_PATTERN = re.compile(r"[0-9]+:[A-Za-z0-9_-]{20,}")
TEMPLATE = """BOT_TOKEN=replace-with-your-telegram-bot-token
TELEGRAM_OWNER_ID=123456789
# Use chromium with: python -m playwright install chromium
SAGASU_BROWSER_CHANNEL=chromium
# Optional: SMU_LOGIN_EMAIL=your.name@smu.edu.sg
# Optional: SMU_LOGIN_PASSWORD=your-private-password
# MFA approval stays manual; enable SAGASU_HEADLESS=true on a remote host.
"""


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    env_file: Path
    token: str = field(repr=False)
    owner_id: str
    browser: str
    profile: Path
    persistence: Path
    auth_timeout_ms: int
    values: dict = field(repr=False)

    @property
    def preferences(self):
        path = Path(
            self.values.get("SAGASU_PREFERENCES_PATH")
            or self.persistence.with_name("personal.sqlite3")
        ).expanduser()
        return (self.env_file.parent / path).resolve()

    @classmethod
    def load(cls, env_file=None, browser=None):
        path = Path(env_file or DEFAULT_ENV).expanduser().resolve()
        if path.exists() and not path.is_file():
            raise ConfigError("The environment-file path must name a file.")
        try:
            values = {**dotenv_values(path, interpolate=False), **os.environ}
            channel = browser or values.get("SAGASU_BROWSER_CHANNEL") or "chromium"
            timeout = int(values.get("SAGASU_AUTH_TIMEOUT_MS") or "600000")
            if not 1000 <= timeout <= 1800000:
                raise ValueError()
        except (OSError, ValueError):
            raise ConfigError(
                "Cannot read configuration, or SAGASU_AUTH_TIMEOUT_MS is invalid (1000–1800000)."
            ) from None
        if channel not in BROWSERS:
            raise ConfigError(
                "Unsupported browser channel. Choose chromium, chrome, or an available Chrome/Edge channel."
            )
        profile = Path(
            values.get("SAGASU_BROWSER_PROFILE") or DEFAULT_PROFILE
        ).expanduser()
        persistence = Path(
            values.get("SAGASU_STATE_PATH") or DEFAULT_PERSISTENCE
        ).expanduser()
        base = path.parent
        keep = {
            key: str(value)
            for key, value in values.items()
            if value is not None
            and (
                key.startswith("SAGASU_")
                or key
                in {
                    "BOT_TOKEN",
                    "TELEGRAM_OWNER_ID",
                    "SMU_LOGIN_EMAIL",
                    "SMU_LOGIN_PASSWORD",
                }
            )
        }
        return cls(
            path,
            values.get("BOT_TOKEN") or "",
            values.get("TELEGRAM_OWNER_ID") or "",
            channel,
            (base / profile).resolve(),
            (base / persistence).resolve(),
            timeout,
            keep,
        )

    def validate(self, *, require_token=True):
        if require_token and not TOKEN_PATTERN.fullmatch(self.token):
            raise ConfigError(
                "BOT_TOKEN is missing, still a placeholder, or malformed. Set the token from @BotFather in your .env; do not paste it into chat."
            )
        if self.owner_id and (
            not self.owner_id.isascii()
            or not self.owner_id.isdigit()
            or int(self.owner_id) <= 0
        ):
            raise ConfigError(
                "TELEGRAM_OWNER_ID must be your positive numeric Telegram user ID."
            )

        if self.values.get("SMU_LOGIN_PASSWORD"):
            if not self.values.get("SMU_LOGIN_EMAIL", "").strip() or not self.owner_id:
                raise ConfigError(
                    "Password autofill requires SMU_LOGIN_EMAIL and TELEGRAM_OWNER_ID."
                )

    def activate(self):
        os.environ.update(self.values)
        os.environ.update(
            SAGASU_BROWSER_CHANNEL=self.browser,
            SAGASU_BROWSER_PROFILE=str(self.profile),
            SAGASU_STATE_PATH=str(self.persistence),
            SAGASU_PREFERENCES_PATH=str(self.preferences),
            SAGASU_AUTH_TIMEOUT_MS=str(self.auth_timeout_ms),
        )

    def public(self):
        return {
            "env_file": str(self.env_file),
            "env_file_exists": self.env_file.is_file(),
            "bot_token": "configured (redacted)" if self.token else "missing",
            "owner_id": "configured (redacted)" if self.owner_id else "not restricted",
            "password_autofill": bool(self.values.get("SMU_LOGIN_PASSWORD")),
            "headless": self.values.get("SAGASU_HEADLESS", "false").lower()
            in {"1", "true", "yes"},
            "browser": self.browser,
            "browser_profile": str(self.profile),
            "telegram_state": str(self.persistence),
            "personal_preferences": str(self.preferences),
            "auth_timeout_seconds": self.auth_timeout_ms // 1000,
        }
