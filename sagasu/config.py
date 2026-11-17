"""Private host configuration for Sagasu's agent runtime and browser."""

import ipaddress
import os
import re
import stat
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
# auto uses direct FBS reads with browser fallback; browser forces browser reads.
SAGASU_READ_BACKEND=auto
# Optional private Microsoft sign-in autofill:
# SMU_LOGIN_EMAIL=your.name@smu.edu.sg
# SMU_LOGIN_PASSWORD='your-private-password'
# SAGASU_HEADLESS=true
# MFA approval remains yours; agents read the prompt with authentication_status.
# Optional remote listener for a private server (see deploy/server/README.md):
# SAGASU_HTTP_PORT=8765
# SAGASU_HTTP_ALLOWED_HOSTS=booking.example.org
# SAGASU_HTTP_KEYS=laptop:<sha256 of the API key, hex>
# SAGASU_HTTP_WRITES=off
"""

KEY_LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
KEY_DIGEST = re.compile(r"[0-9a-f]{64}")
HOST_NAME = re.compile(
    r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
)


class ConfigError(ValueError):
    pass


class ConfigurationRequired(ConfigError):
    """An operation was blocked before contacting FBS."""

    reason = "configuration_required"


@dataclass(frozen=True)
class ConfigurationIssue:
    name: str
    message: str
    hint: str
    blocks_startup: bool = True


@dataclass(frozen=True)
class RemoteListener:
    """A loopback HTTP listener published by a TLS reverse proxy on the same host."""

    bind: str
    port: int
    allowed_hosts: tuple[str, ...]
    keys: dict = field(repr=False)  # SHA-256 hex digest -> label; never the key
    writes: bool = False


def parse_remote_listener(values):
    """Return (listener or None, problems). Nothing is returned for a partial setup."""
    port_text = (values.get("SAGASU_HTTP_PORT") or "").strip()
    if not port_text:
        return None, []
    problems = []
    try:
        port = int(port_text)
        if not 1024 <= port <= 65535:
            raise ValueError()
    except ValueError:
        port = None
        problems.append("SAGASU_HTTP_PORT must be an integer from 1024 to 65535.")
    bind = (values.get("SAGASU_HTTP_BIND") or "127.0.0.1").strip()
    try:
        if not ipaddress.ip_address(bind).is_loopback:
            raise ValueError()
    except ValueError:
        problems.append(
            "SAGASU_HTTP_BIND must be a loopback address (127.0.0.1 or ::1); publish it through a TLS reverse proxy on the same host."
        )
    hosts = tuple(
        dict.fromkeys(
            item.strip().lower()
            for item in (values.get("SAGASU_HTTP_ALLOWED_HOSTS") or "").split(",")
            if item.strip()
        )
    )
    if not hosts or not all(HOST_NAME.fullmatch(host) for host in hosts):
        problems.append(
            "SAGASU_HTTP_ALLOWED_HOSTS must list the public host name(s) clients use, without scheme, port or path."
        )
    keys, labels = {}, set()
    entries = [
        item.strip()
        for item in (values.get("SAGASU_HTTP_KEYS") or "").split(",")
        if item.strip()
    ]
    valid_keys = bool(entries) and len(entries) <= 16
    for entry in entries:
        label, _, digest = entry.partition(":")
        digest = digest.strip().lower()
        if (
            not KEY_LABEL.fullmatch(label)
            or not KEY_DIGEST.fullmatch(digest)
            or label in labels
            or digest in keys
        ):
            valid_keys = False
            break
        labels.add(label)
        keys[digest] = label
    if not valid_keys:
        problems.append(
            "SAGASU_HTTP_KEYS must hold 1 to 16 unique label:sha256-hex entries. Store only the SHA-256 digest of each API key, never the key."
        )
    writes = (values.get("SAGASU_HTTP_WRITES") or "off").strip().lower()
    if writes not in {"on", "off"}:
        problems.append("SAGASU_HTTP_WRITES must be on or off.")
    elif writes == "on" and values.get("SAGASU_MCP_CONFIRMATION", "agent") != "agent":
        problems.append(
            "SAGASU_HTTP_WRITES=on needs SAGASU_MCP_CONFIRMATION=agent; a remote API key cannot show a human confirmation form."
        )
    if values.get("SMU_LOGIN_PASSWORD"):
        problems.append(
            "SMU_LOGIN_PASSWORD must not be configured together with the remote listener; sign in on the host instead."
        )
    if problems:
        return None, problems
    return RemoteListener(bind, port, hosts, keys, writes == "on"), []


@dataclass(frozen=True)
class Settings:
    env_file: Path
    browser: str
    profile: Path
    preferences: Path
    auth_timeout_ms: int
    values: dict = field(repr=False)
    load_issues: tuple[ConfigurationIssue, ...] = field(default=(), repr=False)
    journal_path: Path | None = field(default=None, repr=False)

    @property
    def env_file_exists(self):
        try:
            return stat.S_ISREG(self.env_file.stat().st_mode)
        except OSError:
            return False

    @property
    def remote_listener_requested(self):
        """True when this host is meant to run as a long-lived remote service."""
        return bool((self.values.get("SAGASU_HTTP_PORT") or "").strip())

    @property
    def http(self):
        """The validated remote listener, or None when it is off or misconfigured."""
        return parse_remote_listener(self.values)[0]

    @property
    def journal(self):
        if self.journal_path is not None:
            return self.journal_path
        path = Path(
            self.values.get("SAGASU_JOURNAL_PATH")
            or self.preferences.with_name("actions.sqlite3")
        ).expanduser()
        return (self.env_file.parent / path).resolve()

    @classmethod
    def load(cls, env_file=None, browser=None, *, diagnostic=False):
        issues = []

        def issue(name, message, hint):
            if not diagnostic:
                raise ConfigError(message)
            issues.append(ConfigurationIssue(name, message, hint))

        def resolve(value, base, fallback, name, variable):
            try:
                if "\0" in str(value):
                    raise ValueError()
                return (base / Path(value).expanduser()).resolve()
            except (OSError, ValueError, RuntimeError):
                issue(
                    name,
                    "A configured path is invalid.",
                    f"Correct {variable} locally, then reconnect Sagasu.",
                )
                return fallback

        path = resolve(
            env_file or DEFAULT_ENV,
            Path.cwd(),
            DEFAULT_ENV,
            "config.file",
            "--env-file",
        )
        file_values = {}
        try:
            if any(item.name == "config.file" for item in issues):
                # A fallback path is for diagnostics only, never another source
                # of credentials or operational settings.
                pass
            elif not stat.S_ISREG(path.stat().st_mode):
                issue(
                    "config.file",
                    "The environment-file path must name a file.",
                    "Point --env-file at a readable environment file, then reconnect Sagasu.",
                )
            else:
                file_values = dotenv_values(path, interpolate=False)
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError):
            issue(
                "config.file",
                "The environment file could not be read.",
                "Check --env-file, its permissions and UTF-8 encoding, then reconnect Sagasu.",
            )
        values = {**file_values, **os.environ}
        channel = browser or values.get("SAGASU_BROWSER_CHANNEL") or "chromium"
        try:
            timeout = int(values.get("SAGASU_AUTH_TIMEOUT_MS") or "600000")
            if not 1000 <= timeout <= 1800000:
                raise ValueError()
        except ValueError:
            issue(
                "config.auth_timeout",
                "SAGASU_AUTH_TIMEOUT_MS is invalid (1000–1800000).",
                "Set SAGASU_AUTH_TIMEOUT_MS to an integer from 1000 to 1800000, then reconnect Sagasu.",
            )
            timeout = 600000
        if channel not in BROWSERS:
            issue(
                "config.browser",
                "Unsupported browser channel. Choose chromium, chrome, or an available Chrome/Edge channel.",
                "Set SAGASU_BROWSER_CHANNEL=chromium or select an installed supported browser, then reconnect Sagasu.",
            )
            channel = "chromium"
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
        profile = resolve(
            values.get("SAGASU_BROWSER_PROFILE") or DEFAULT_PROFILE,
            base,
            DEFAULT_PROFILE,
            "config.profile",
            "SAGASU_BROWSER_PROFILE",
        )
        preferences = resolve(
            values.get("SAGASU_PREFERENCES_PATH") or DEFAULT_PREFERENCES,
            base,
            DEFAULT_PREFERENCES,
            "config.preferences",
            "SAGASU_PREFERENCES_PATH",
        )
        journal = resolve(
            values.get("SAGASU_JOURNAL_PATH")
            or preferences.with_name("actions.sqlite3"),
            base,
            DEFAULT_PREFERENCES.with_name("actions.sqlite3"),
            "config.history",
            "SAGASU_JOURNAL_PATH",
        )
        return cls(
            path,
            channel,
            profile,
            preferences,
            timeout,
            keep,
            tuple(issues),
            journal,
        )

    def issues(self):
        issues = list(self.load_issues)
        if self.values.get("SAGASU_HEADLESS", "false").lower() not in {
            "1",
            "true",
            "yes",
            "0",
            "false",
            "no",
        }:
            issues.append(
                ConfigurationIssue(
                    "config.headless",
                    "SAGASU_HEADLESS must be a boolean.",
                    "Set SAGASU_HEADLESS=true or false, then reconnect Sagasu.",
                )
            )
        if self.values.get("SAGASU_READ_BACKEND", "auto") not in {
            "auto",
            "browser",
            "api",
        }:
            issues.append(
                ConfigurationIssue(
                    "config.read_backend",
                    "SAGASU_READ_BACKEND must be auto, browser or api.",
                    "Set SAGASU_READ_BACKEND=auto, browser or api, then reconnect Sagasu.",
                )
            )
        if self.journal == self.preferences:
            issues.append(
                ConfigurationIssue(
                    "config.storage",
                    "Action history and personal preferences must use different files.",
                    "Set SAGASU_JOURNAL_PATH and SAGASU_PREFERENCES_PATH to different files, then reconnect Sagasu.",
                )
            )
        if self.values.get("SAGASU_MCP_CONFIRMATION", "agent") not in {
            "human",
            "agent",
        }:
            issues.append(
                ConfigurationIssue(
                    "config.confirmation",
                    "SAGASU_MCP_CONFIRMATION must be human or agent.",
                    "Set SAGASU_MCP_CONFIRMATION=human or agent, then reconnect Sagasu.",
                )
            )
        if self.values.get("SAGASU_SESSION_COOKIES", "all").lower() not in {
            "all",
            "fbs",
        }:
            issues.append(
                ConfigurationIssue(
                    "config.session_cookies",
                    "SAGASU_SESSION_COOKIES must be all or fbs.",
                    "Set SAGASU_SESSION_COOKIES=all, or fbs to keep only FBS cookies in the saved session file, then reconnect Sagasu.",
                )
            )
        issues.extend(
            ConfigurationIssue(
                "config.remote_listener",
                problem,
                "Correct the SAGASU_HTTP_* settings privately on the host, then restart the Sagasu service. The remote listener stays off until they are valid.",
            )
            for problem in parse_remote_listener(self.values)[1]
        )
        if (
            self.values.get("SMU_LOGIN_PASSWORD")
            and not self.values.get("SMU_LOGIN_EMAIL", "").strip()
        ):
            issues.append(
                ConfigurationIssue(
                    "config.login_email",
                    "Password autofill requires SMU_LOGIN_EMAIL.",
                    "Set SMU_LOGIN_EMAIL privately before automatic login. An existing saved session may still work.",
                    blocks_startup=False,
                )
            )
        return issues

    def validate(self):
        issues = self.issues()
        if issues:
            raise ConfigError(issues[0].message)

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
            "env_file_exists": self.env_file_exists,
            "password_autofill": bool(self.values.get("SMU_LOGIN_PASSWORD")),
            "headless": self.values.get("SAGASU_HEADLESS", "false").lower()
            in {"1", "true", "yes"},
            "browser": self.browser,
            "read_backend": (
                value
                if (value := self.values.get("SAGASU_READ_BACKEND", "auto"))
                in {"auto", "browser", "api"}
                else "invalid"
            ),
            "mcp_confirmation": (
                value
                if (value := self.values.get("SAGASU_MCP_CONFIRMATION", "agent"))
                in {"agent", "human"}
                else "invalid"
            ),
            "browser_profile": str(self.profile),
            "personal_preferences": str(self.preferences),
            "action_history": str(self.journal),
            "auth_timeout_seconds": self.auth_timeout_ms // 1000,
            "session_cookies": (
                value
                if (value := self.values.get("SAGASU_SESSION_COOKIES", "all").lower())
                in {"all", "fbs"}
                else "invalid"
            ),
            "remote_listener": (
                {
                    "enabled": True,
                    "bind": http.bind,
                    "port": http.port,
                    "allowed_hosts": list(http.allowed_hosts),
                    "key_labels": sorted(http.keys.values()),
                    "writes": http.writes,
                }
                if (http := self.http) is not None
                else {
                    "enabled": False,
                    "requested": self.remote_listener_requested,
                }
            ),
        }
