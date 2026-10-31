"""Generate or install a user daily scheduler after live scraping is verified."""

import argparse
import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

if __package__:
    from .public_data import read_json, sanitize_snapshot, sanitize_status
else:
    from public_data import read_json, sanitize_snapshot, sanitize_status

ROOT = Path(__file__).resolve().parents[1]
LABEL = "sg.sagasu.availability"


def command(*args, allowed=(0,)):
    result = subprocess.run(args, capture_output=True, timeout=30)
    if result.returncode not in allowed:
        raise RuntimeError(
            "Scheduler operation failed; inspect the local scheduler privately"
        )
    return result


def scheduler_kind():
    system = platform.system()
    if system == "Darwin" and shutil.which("launchctl"):
        if (
            command(
                "launchctl", "print", f"gui/{os.getuid()}", allowed=tuple(range(256))
            ).returncode
            == 0
        ):
            return "launchd"
    if system == "Linux" and shutil.which("systemctl"):
        if (
            command(
                "systemctl", "--user", "show-environment", allowed=tuple(range(256))
            ).returncode
            == 0
        ):
            return "systemd"
    if shutil.which("crontab"):
        return "cron"
    raise RuntimeError(
        "No supported user scheduler detected; use python -m bot.refresh_daily manually"
    )


def schedule_files(kind, root, python, channel, *, profile=None, search_path=None):
    root = Path(root)
    logs = root / "artifacts/schedule"
    program = [str(python), "-m", "bot.refresh_daily"]
    environment = {"SAGASU_BROWSER_CHANNEL": channel, "PATH": search_path or os.defpath}
    if profile:
        environment["SAGASU_SCRAPER_PROFILE"] = str(
            Path(profile).expanduser().absolute()
        )
    if kind == "launchd":
        # launchd calendar intervals follow the machine timezone, not EnvironmentVariables.TZ.
        if datetime.now().astimezone().utcoffset() != timedelta(hours=8):
            raise RuntimeError(
                "Set the Mac timezone to Singapore before scheduling 06:17 locally"
            )
        value = {
            "Label": LABEL,
            "ProgramArguments": program,
            "WorkingDirectory": str(root),
            "EnvironmentVariables": environment,
            "StartCalendarInterval": {"Hour": 6, "Minute": 17},
            "ProcessType": "Background",
            "StandardOutPath": str(logs / "refresh.log"),
            "StandardErrorPath": str(logs / "refresh-error.log"),
        }
        return {f"{LABEL}.plist": plistlib.dumps(value)}
    if kind == "systemd":
        # systemd's quoted argument syntax differs from shell escaping.
        quote = (
            lambda text: '"'
            + str(text).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            + '"'
        )
        service = "\n".join(
            [
                "[Unit]",
                "Description=Sagasu local read-only availability refresh",
                "[Service]",
                "Type=oneshot",
                f"WorkingDirectory={quote(root)}",
                *[
                    f'Environment={quote(key + "=" + value)}'
                    for key, value in environment.items()
                ],
                "ExecStart=" + " ".join(quote(part) for part in program),
                "TimeoutStartSec=30min",
                "",
            ]
        )
        timer = "\n".join(
            [
                "[Unit]",
                "Description=Daily Sagasu refresh at 06:17 Singapore",
                "[Timer]",
                "OnCalendar=*-*-* 06:17:00 Asia/Singapore",
                "Persistent=true",
                "[Install]",
                "WantedBy=timers.target",
                "",
            ]
        )
        return {f"{LABEL}.service": service.encode(), f"{LABEL}.timer": timer.encode()}
    if datetime.now().astimezone().utcoffset() != timedelta(hours=8):
        raise RuntimeError(
            "Cron fallback requires the machine timezone to be Singapore"
        )
    if any("%" in str(value) for value in [root, python, *environment.values()]):
        raise RuntimeError("Cron paths must not contain percent characters")
    env_command = " ".join(
        key + "=" + shlex.quote(value) for key, value in environment.items()
    )
    line = (
        "17 6 * * * cd "
        + shlex.quote(str(root))
        + " && "
        + env_command
        + " "
        + shlex.join(program)
        + " >> "
        + shlex.quote(str(logs / "refresh.log"))
        + " 2>&1 # "
        + LABEL
        + "\n"
    )
    return {f"{LABEL}.cron": line.encode()}


def install(kind, files):
    home = Path.home()
    if kind == "launchd":
        destination = home / "Library/LaunchAgents" / f"{LABEL}.plist"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise RuntimeError(
                "A Sagasu LaunchAgent already exists; unload it before replacing it"
            )
        destination.write_bytes(next(iter(files.values())))
        destination.chmod(0o600)
        try:
            command("launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination))
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    elif kind == "systemd":
        destination = home / ".config/systemd/user"
        destination.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (destination / name).write_bytes(content)
        command("systemctl", "--user", "daemon-reload")
        command("systemctl", "--user", "enable", "--now", f"{LABEL}.timer")
    else:
        previous = command("crontab", "-l", allowed=(0, 1)).stdout
        if LABEL.encode() in previous:
            raise RuntimeError(
                "A Sagasu cron entry already exists; remove it before replacing it"
            )
        subprocess.run(
            ["crontab", "-"],
            input=previous.rstrip() + b"\n" + next(iter(files.values())),
            capture_output=True,
            check=True,
            timeout=30,
        )


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install the detected user scheduler (otherwise only generate files)",
    )
    args = parser.parse_args()
    try:
        # Do not schedule against a mocked/empty first-run data file.
        snapshot = sanitize_snapshot(read_json(ROOT / "artifacts/daily/snapshot.json"))
        status = sanitize_status(read_json(ROOT / "artifacts/daily/status.json"))
        if snapshot is None or status["status"] != "ok":
            raise ValueError(
                "A complete successful local snapshot is required before scheduling"
            )
        kind = scheduler_kind()
        files = schedule_files(
            kind,
            ROOT,
            Path(sys.executable).absolute(),
            os.getenv("SAGASU_BROWSER_CHANNEL", "chrome"),
            profile=os.getenv("SAGASU_SCRAPER_PROFILE"),
            search_path=os.getenv("PATH"),
        )
        directory = ROOT / "artifacts/schedule"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, content in files.items():
            (directory / name).write_bytes(content)
        if args.install:
            install(kind, files)
        print(
            f'{kind} daily schedule {"installed" if args.install else "generated under artifacts/schedule"}: 06:17 Singapore. This does not run an immediate scrape.'
        )
    except Exception:
        print(
            "Scheduler setup failed. Verify a successful local snapshot, Singapore timezone, and user scheduler access. Existing scheduler diagnostics are kept private."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
