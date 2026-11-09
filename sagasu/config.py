"""Private host configuration for Sagasu's agent runtime and browser."""

import os
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
DEFAULT_DATA = SOURCE_ROOT / ".sagasu" if SOURCE_ROOT else USER_STATE
DEFAULT_PROFILE = DEFAULT_DATA / "browser"
DEFAULT_PREFERENCES = DEFAULT_DATA / "personal.sqlite3"
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
TEMPLATE = """# Sagasu is a private local MCP server. Configure optional SMU autofill below.
SAGASU_BROWSER_CHANNEL=chromium
SAGASU_MCP_CONFIRMATION=agent
# auto verifies session-backed HTTP account reads; browser disables HTTP probing.
SAGASU_READ_BACKEND=auto
# Optional private Microsoft sign-in autofill:
# SMU_LOGIN_EMAIL=your.name@smu.edu.sg
# SMU_LOGIN_PASSWORD='your-private-password'
# SAGASU_HEADLESS=true
# MFA approval remains yours; agents read the prompt with authentication_status.
"""


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    env_file: Path
    browser: str
    profile: Path
    preferences: Path
    auth_timeout_ms: int
    values: dict = field(repr=False)

    @property
    def journal(self):
        path = Path(
            self.values.get("SAGASU_JOURNAL_PATH")
            or self.preferences.with_name("actions.sqlite3")
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
        base = path.parent
        keep = {
            key: str(value)
            for key, value in values.items()
            if value is not None
            and (
                key.startswith("SAGASU_")
                or key in {"SMU_LOGIN_EMAIL", "SMU_LOGIN_PASSWORD"}
            )
        }
        return cls(
            path,
            channel,
            (
                base
                / Path(
                    values.get("SAGASU_BROWSER_PROFILE") or DEFAULT_PROFILE
                ).expanduser()
            ).resolve(),
            (
                base
                / Path(
                    values.get("SAGASU_PREFERENCES_PATH") or DEFAULT_PREFERENCES
                ).expanduser()
            ).resolve(),
            timeout,
            keep,
        )

    def validate(self):
        if self.values.get("SAGASU_READ_BACKEND", "auto") not in {
            "auto",
            "browser",
            "api",
        }:
            raise ConfigError("SAGASU_READ_BACKEND must be auto, browser or api.")
        if self.journal == self.preferences:
            raise ConfigError(
                "Action history and personal preferences must use different files."
            )
        if self.values.get("SAGASU_MCP_CONFIRMATION", "agent") not in {
            "human",
            "agent",
        }:
            raise ConfigError("SAGASU_MCP_CONFIRMATION must be human or agent.")
        if (
            self.values.get("SMU_LOGIN_PASSWORD")
            and not self.values.get("SMU_LOGIN_EMAIL", "").strip()
        ):
            raise ConfigError("Password autofill requires SMU_LOGIN_EMAIL.")

    def activate(self):
        os.environ.update(self.values)
        os.environ.update(
            SAGASU_BROWSER_CHANNEL=self.browser,
            SAGASU_BROWSER_PROFILE=str(self.profile),
            SAGASU_PREFERENCES_PATH=str(self.preferences),
            SAGASU_JOURNAL_PATH=str(self.journal),
            SAGASU_AUTH_TIMEOUT_MS=str(self.auth_timeout_ms),
        )

    def public(self):
        return {
            "env_file": str(self.env_file),
            "env_file_exists": self.env_file.is_file(),
            "password_autofill": bool(self.values.get("SMU_LOGIN_PASSWORD")),
            "headless": self.values.get("SAGASU_HEADLESS", "false").lower()
            in {"1", "true", "yes"},
            "browser": self.browser,
            "read_backend": self.values.get("SAGASU_READ_BACKEND", "auto"),
            "mcp_confirmation": self.values.get("SAGASU_MCP_CONFIRMATION", "agent"),
            "browser_profile": str(self.profile),
            "personal_preferences": str(self.preferences),
            "action_history": str(self.journal),
            "auth_timeout_seconds": self.auth_timeout_ms // 1000,
        }
