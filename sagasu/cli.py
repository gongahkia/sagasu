"""Sagasu's command-line interface and agent-first MCP runtime."""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import (
    BROWSERS,
    ConfigError,
    DEFAULT_ENV,
    Settings,
    TEMPLATE,
)
from .console import Reporter, configure_logging
from .runtime import AlreadyRunning, InstanceLock
from .errors import public_failure


def timeout_value(value):
    try:
        number = int(value)
        if 1 <= number <= 60:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(
        "timeout must be an integer between 1 and 60 seconds"
    )


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file",
        type=Path,
        default=argparse.SUPPRESS,
        help="Environment file (default: repository .env, or current directory when installed)",
    )
    common.add_argument(
        "--browser",
        choices=BROWSERS,
        default=argparse.SUPPRESS,
        help="Override SAGASU_BROWSER_CHANNEL; use chromium on Fedora",
    )
    common.add_argument(
        "--spinner",
        choices=("dots", "line", "none"),
        default=argparse.SUPPRESS,
        help="Rattles spinner style (TTY only)",
    )
    common.add_argument(
        "--no-spinner",
        dest="spinner",
        action="store_const",
        const="none",
        default=argparse.SUPPRESS,
        help="Disable progress animation",
    )
    common.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable colors (also respects NO_COLOR)",
    )
    common.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Only print warnings/errors; JSON reports are unaffected",
    )
    common.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Verbose redacted logs; raw authentication tracebacks are suppressed",
    )
    common.add_argument(
        "--log-file",
        type=Path,
        default=argparse.SUPPRESS,
        help="Optional rotating, redacted log file (0600)",
    )
    common.add_argument(
        "--log-format",
        choices=("text", "json"),
        default=argparse.SUPPRESS,
        help="Timestamped text or JSON Lines logs (default: text)",
    )
    result = argparse.ArgumentParser(
        prog="sagasu",
        description="Agent assistant for SMU room searches, bookings and account management.",
        epilog="Start with: sagasu doctor, then sagasu login. Your agent launches sagasu mcp.",
        parents=[common],
    )
    result.add_argument("--version", action="version", version=f"sagasu {__version__}")
    commands = result.add_subparsers(dest="command", metavar="COMMAND")
    mcp = commands.add_parser(
        "mcp", help="Serve the local agent MCP connection over stdio", parents=[common]
    )
    mcp.add_argument(
        "--direct",
        action="store_true",
        help="Own the browser directly (single client; Windows fallback)",
    )
    commands.add_parser("host-run", help=argparse.SUPPRESS, parents=[common])
    host = commands.add_parser(
        "host", help="Inspect or stop the shared local browser host", parents=[common]
    )
    host.add_argument("action", choices=("status", "stop"))
    login = commands.add_parser(
        "login",
        help="Sign in to FBS/MFA using the private browser profile",
        parents=[common],
    )
    login.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Sign-in and MFA deadline, 1–1800 seconds (default: 600)",
    )
    doctor = commands.add_parser(
        "doctor",
        help="Check configuration, browser startup, network and optional FBS session",
        parents=[common],
    )
    doctor.add_argument(
        "--offline",
        action="store_true",
        help="Skip FBS network requests",
    )
    doctor.add_argument(
        "--session",
        action="store_true",
        help="Check the saved FBS session (stop the MCP server first; no manual login)",
    )
    doctor.add_argument(
        "--timeout",
        type=timeout_value,
        default=10,
        help="Per-probe deadline in seconds, 1–60 (default: 10)",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Print a structured report on stdout; progress is suppressed",
    )
    smoke = commands.add_parser(
        "smoke",
        help="Opt-in live read-only FBS session/search/bookings check",
        parents=[common],
    )
    smoke.add_argument(
        "--live",
        action="store_true",
        help="Explicitly allow this command to access your saved FBS session",
    )
    smoke.add_argument(
        "--timeout", type=int, default=180, help="Overall deadline in seconds, 1–600"
    )
    smoke.add_argument("--json", action="store_true")
    probe = commands.add_parser(
        "backend-probe",
        help="Read-only browser/HTTP parity probe with redacted network metadata",
        parents=[common],
    )
    probe.add_argument(
        "--live",
        action="store_true",
        help="Explicitly use the saved FBS session; never starts login/MFA",
    )
    probe.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Overall deadline in seconds, 1–600 (default: 180)",
    )
    probe.add_argument("--json", action="store_true")
    paths = commands.add_parser(
        "paths",
        help="Show active configuration and data paths without secrets",
        parents=[common],
    )
    paths.add_argument("--json", action="store_true")
    config = commands.add_parser(
        "config",
        help="Create or inspect configuration without exposing credentials",
        parents=[common],
    )
    actions = config.add_subparsers(dest="config_action", required=True)
    actions.add_parser(
        "init",
        help="Create a private environment template; never overwrites an existing file",
        parents=[common],
    )
    show = actions.add_parser(
        "show", help="Show redacted configuration", parents=[common]
    )
    show.add_argument("--json", action="store_true")
    return result


def print_values(values, as_json=False):
    if as_json:
        print(json.dumps(values, indent=2))
    else:
        for name, value in values.items():
            print(f"{name.replace('_', ' ')}: {value}")


async def login(settings, reporter, timeout):
    from .host import run_operation

    reporter.write(
        "Complete Microsoft sign-in and MFA. Credentials remain in your private host configuration."
    )
    with reporter.progress("Opening private FBS login") as progress:

        async def update(stage):
            progress.message = stage
            reporter.write(stage)

        async with asyncio.timeout(timeout):
            if os.name == "nt":
                from .fbs_client import FBSClient

                with InstanceLock(settings.profile):
                    async with FBSClient(
                        keep_alive=False,
                        profile_path=settings.profile,
                        progress=update,
                        auth_notify=update,
                    ) as client:
                        await client.ensure_authenticated()
            else:
                await run_operation(settings, "login", notify=update)
        progress.finish("Authenticated FBS controls verified; private session saved.")


def main(argv=None):
    command_parser = parser()
    args = command_parser.parse_args(argv)
    if not args.command:
        command_parser.print_help()
        return 0
    if args.command == "doctor" and args.offline and args.session:
        command_parser.error(
            "--session needs network access; it cannot be combined with --offline"
        )
    if args.command == "login" and not 1 <= args.timeout <= 1800:
        command_parser.error("login --timeout must be between 1 and 1800 seconds")
    if args.command in {"smoke", "backend-probe"} and (
        not args.live or not 1 <= args.timeout <= 600
    ):
        command_parser.error(
            f"{args.command} requires --live and --timeout between 1 and 600 seconds; no live check was run"
        )
    as_json = getattr(args, "json", False)
    reporter = Reporter(
        spinner="none" if as_json else getattr(args, "spinner", "dots"),
        no_color=getattr(args, "no_color", False),
        quiet=getattr(args, "quiet", False) or as_json,
        structured=getattr(args, "log_format", "text") == "json",
    )
    try:
        if args.command == "config" and args.config_action == "init":
            path = Path(getattr(args, "env_file", DEFAULT_ENV)).expanduser().resolve()
            if path.exists():
                raise ConfigError(
                    "Configuration already exists; no file was changed. Use sagasu config show or edit it locally."
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(TEMPLATE)
            reporter.write(
                f"Created {path}. Configure optional SMU sign-in locally, then run sagasu doctor."
            )
            return 0
        settings = Settings.load(
            getattr(args, "env_file", None),
            getattr(args, "browser", None),
            diagnostic=args.command in {"doctor", "mcp", "host", "host-run"},
        )
        configure_logging(
            reporter,
            verbose=getattr(args, "verbose", False),
            log_file=getattr(args, "log_file", None),
            secrets=(settings.values.get("SMU_LOGIN_PASSWORD", ""),),
            log_format=getattr(args, "log_format", "text"),
        )
        if args.command == "config":
            print_values(settings.public(), as_json)
            return 0
        if args.command == "paths":
            print_values(
                {
                    key: settings.public()[key]
                    for key in (
                        "env_file",
                        "browser_profile",
                        "personal_preferences",
                        "action_history",
                    )
                },
                as_json,
            )
            return 0
        if args.command == "doctor":
            from .doctor import diagnose

            if not any(issue.blocks_startup for issue in settings.issues()):
                settings.activate()
            with reporter.progress("Checking Sagasu health") as progress:
                report = asyncio.run(
                    diagnose(
                        settings,
                        offline=args.offline,
                        session=args.session,
                        timeout=args.timeout,
                        emit=(
                            None
                            if as_json
                            else lambda check: reporter.check(
                                check.name, check.status, check.message, check.hint
                            )
                        ),
                    )
                )
                if not as_json:
                    progress.finish(
                        (
                            "Health checks passed."
                            if report["ok"]
                            else "Health checks found issues."
                        ),
                        failed=not report["ok"],
                    )
            report["version"] = __version__
            if as_json:
                print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        if args.command not in {"mcp", "host", "host-run"}:
            settings.validate()
        settings.activate()
        if args.command == "backend-probe":
            from .backend_probe import run_probe

            report = asyncio.run(run_probe(settings, timeout=args.timeout))
            if as_json:
                print(json.dumps(report, indent=2))
            else:
                for check in report["checks"]:
                    reporter.check(
                        check["name"],
                        check["status"],
                        (
                            f"Browser read passed; HTTP verification: {check.get('api_verification', 'not applicable')}."
                            if check["status"] == "pass"
                            else check["hint"]
                        ),
                    )
                    if check.get("fallback_reason"):
                        reporter.write(f"HTTP unavailable: {check['fallback_reason']}")
                reporter.write(
                    f"Recommended backend: {report['recommendation']}. Use --json for the redacted traffic report."
                )
            return 0 if report["ok"] else 1
        if args.command == "smoke":
            from .smoke import run_smoke

            report = asyncio.run(run_smoke(settings, timeout=args.timeout))
            if as_json:
                print(json.dumps(report, indent=2))
            else:
                for check in report["checks"]:
                    reporter.check(
                        check["name"],
                        check["status"],
                        (
                            "Read-only check passed."
                            if check["status"] == "pass"
                            else check["hint"]
                        ),
                    )
            return 0 if report["ok"] else 1
        if args.command in {"host", "host-run"}:
            from .host import control, serve_host

            if args.command == "host-run":
                from .diagnostics import configuration_status

                configure_logging(
                    reporter,
                    log_file=(
                        settings.profile.parent / "host.log"
                        if configuration_status(settings)["operational"]
                        else None
                    ),
                    secrets=(settings.values.get("SMU_LOGIN_PASSWORD", ""),),
                )
                asyncio.run(serve_host(settings))
                return 0
            result = asyncio.run(control(settings, args.action))
            print_values(result, as_json=True)
            return 1 if args.action == "stop" and result.get("busy") else 0
        if args.command == "mcp":
            if not args.direct and os.name != "nt":
                from .host import bridge_stdio

                asyncio.run(bridge_stdio(settings))
                return 0
            try:
                from .mcp_server import serve
            except ImportError:
                raise ConfigError(
                    "Agent runtime dependencies are missing or incompatible. Install the project with: python -m pip install -e ."
                ) from None
            asyncio.run(serve(settings))
            return 0
        if args.command == "login":
            asyncio.run(login(settings, reporter, args.timeout))
            return 0
        return 0
    except ConfigError as error:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": "configuration", "message": str(error)}
                )
            )
        else:
            reporter.write(f"Configuration error: {error}", error=True)
        return 2
    except AlreadyRunning as error:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": "already_running", "message": str(error)}
                )
            )
        else:
            reporter.write(str(error), error=True)
        return 1
    except KeyboardInterrupt:
        reporter.write("Cancelled; Sagasu stopped.")
        return 130
    except Exception as error:
        # Library exceptions can contain private configuration values.
        name = type(error).__name__
        failure = public_failure(error)
        if failure:
            message = failure["message"] + " " + failure["next_action"]
        elif name in {"TimedOut", "NetworkError", "ConnectError", "TimeoutError"}:
            message = "The operation timed out or a service is unreachable. Run sagasu doctor to check connectivity."
        elif name == "AuthenticationRequired":
            message = "FBS authentication did not finish. Run sagasu doctor, then retry sagasu login when FBS is reachable."
        else:
            message = "Sagasu could not complete the operation. Run sagasu doctor; check paths and browser installation. Raw error details are suppressed."
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "operation_failed",
                        "message": message,
                        **failure,
                    }
                )
            )
        else:
            reporter.write(message, error=True)
        logging.getLogger(__name__).debug(
            "Operation failed with exception type %s", name
        )
        return 1
