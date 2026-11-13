"""Shared, redacted local diagnostics for the CLI and MCP. No FBS/browser access."""

import importlib.metadata
import os
import platform
from dataclasses import asdict, dataclass


@dataclass
class Check:
    name: str
    status: str
    message: str
    hint: str = ""
    blocks_startup: bool = False


def local_checks(settings):
    checks = [
        Check(
            "python",
            "pass",
            f"Python {platform.python_version()} on {platform.system()}.",
        )
    ]
    for distribution in ("mcp", "playwright", "python-dotenv"):
        try:
            version = importlib.metadata.version(distribution)
            checks.append(
                Check(f"dependency.{distribution}", "pass", f"Installed ({version}).")
            )
        except importlib.metadata.PackageNotFoundError:
            checks.append(
                Check(
                    f"dependency.{distribution}",
                    "fail",
                    "Not installed.",
                    "Install Sagasu in this Python environment: python -m pip install -e .",
                    True,
                )
            )
    checks.append(
        Check(
            "config.file",
            "pass" if settings.env_file.is_file() else "warn",
            (
                "Environment file found."
                if settings.env_file.is_file()
                else "No environment file; exported variables are used."
            ),
            (
                ""
                if settings.env_file.is_file()
                else "Create a template with sagasu config init, or configure exported variables."
            ),
        )
    )
    issues = settings.issues()
    if not issues:
        checks.append(
            Check(
                "config.settings",
                "pass",
                "Agent configuration is valid; credentials redacted.",
            )
        )
    checks.extend(
        Check(issue.name, "fail", issue.message, issue.hint, issue.blocks_startup)
        for issue in issues
    )
    for name, target, variable in (
        ("profile", settings.profile, "SAGASU_BROWSER_PROFILE"),
        ("preferences", settings.preferences, "SAGASU_PREFERENCES_PATH"),
        ("history", settings.journal, "SAGASU_JOURNAL_PATH"),
    ):
        try:
            wrong_type = target.exists() and (
                not target.is_dir() if name == "profile" else not target.is_file()
            )
            parent = target if target.is_dir() else target.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            writable = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
            if name != "profile" and target.is_file():
                writable = writable and os.access(target, os.W_OK)
            valid = not wrong_type and writable
            message = (
                "The configured path has the wrong file type."
                if wrong_type
                else (
                    "Parent directory is writable."
                    if writable
                    else "The configured path is not writable."
                )
            )
        except (OSError, ValueError, RuntimeError):
            valid, message = False, "The configured path could not be inspected."
        checks.append(
            Check(
                f"storage.{name}",
                "pass" if valid else "fail",
                message,
                (
                    ""
                    if valid
                    else f"Correct {variable} and its permissions locally, then reconnect Sagasu. Do not delete existing session or action-history files."
                ),
                not valid,
            )
        )
    if settings.public()["headless"]:
        for variable, label in (
            ("SMU_LOGIN_EMAIL", "email"),
            ("SMU_LOGIN_PASSWORD", "password"),
        ):
            value = settings.values.get(variable, "")
            if not (value.strip() if label == "email" else value):
                checks.append(
                    Check(
                        f"authentication.{label}",
                        "warn",
                        f"Headless automatic login needs {variable}.",
                        f"Set {variable} privately on the host if login is needed. An existing saved session may still work; never send credentials in chat.",
                    )
                )
    return checks


def configuration_status(settings, *, startup_checks=()):
    checks = [*local_checks(settings), *startup_checks]
    operational = not any(
        check.status == "fail" and check.blocks_startup for check in checks
    )
    return {
        "ok": not any(check.status == "fail" for check in checks),
        "mode": "operational" if operational else "diagnostic",
        "operational": operational,
        "configuration_source": "loaded_at_startup",
        "checks": [asdict(check) for check in checks],
        "capabilities": {
            "diagnostics": True,
            "fbs_operations": operational,
            "personal_data": operational,
            "automatic_login": operational
            and bool(settings.values.get("SMU_LOGIN_EMAIL", "").strip())
            and bool(settings.values.get("SMU_LOGIN_PASSWORD")),
        },
        "authentication": {
            "headless": settings.public()["headless"],
            "email_configured": bool(
                settings.values.get("SMU_LOGIN_EMAIL", "").strip()
            ),
            "password_configured": bool(settings.values.get("SMU_LOGIN_PASSWORD")),
            "session_valid": "unknown_until_checked",
            "session_status_tool": "session_status",
        },
        "next_action": (
            "Inspect server_status for capabilities. Use session_status to verify the saved session; use login only when renewal is needed."
            if operational
            else "Apply the failing checks' local fixes. Run sagasu doctor to recheck. Inspect sagasu host status; when no operations are active, run sagasu host stop and reconnect the MCP client. Configuration changes take effect after reconnecting."
        ),
        "configuration_status_tool": "configuration_status",
        "workflow_resource": "sagasu://workflow",
        "credentials_policy": "Configure credentials privately on the host. Never paste passwords into chat.",
    }
