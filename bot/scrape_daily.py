"""Read-only scheduled scraper. Run with python -m bot.scrape_daily."""

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from .fbs_client import AuthenticationRequired, LayoutChanged
from .scraper_session import (
    ROOT,
    ProfileBusy,
    ScraperClient,
    profile_lock,
    scraper_profile,
)
from .password_auth import PasswordLoginError
from .snapshot import SGT, public_snapshot, search_days, write_json


async def scrape(*, headed=False, verbose=False):
    async def progress(stage):
        if verbose:
            print(f"Progress: {stage}", flush=True)

    results = []
    profile = scraper_profile()
    with profile_lock(profile):
        async with asyncio.timeout(1200):
            async with ScraperClient(
                headless=not headed,
                progress=progress,
                profile_path=profile,
            ) as client:
                for index, config in enumerate(search_days(), 1):
                    print(f"Reading availability day {index}/7", flush=True)
                    results.append(await client.search(config))
    return public_snapshot(results)


def main(argv=None):
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/daily"))
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Watch the saved-session scrape; does not wait for manual login",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print fixed progress labels, never page text or credentials",
    )
    args = parser.parse_args(argv)
    # Replace only after a complete scrape; failures retain the last good data.
    snapshot_path = args.output / "snapshot.json"
    status = {"attempted_at": datetime.now(SGT).isoformat()}
    exit_code = 0
    try:
        snapshot = asyncio.run(scrape(headed=args.headed, verbose=args.verbose))
        write_json(snapshot_path, snapshot)
        status.update(status="ok", message="Daily availability refresh completed.")
    except ProfileBusy:
        status.update(
            status="failed",
            message="Scraper profile is busy. Showing the last successful snapshot.",
        )
        exit_code = 1
    except PasswordLoginError as error:
        status.update(
            status="authentication_required",
            error_code=error.code,
            login_stage=error.stage,
            message=f"{error}. Showing the last successful snapshot.",
        )
        print(f"Login failure: {error.code}; step: {error.stage}", flush=True)
        exit_code = 1
    except AuthenticationRequired:
        print("Renew the local session with python -m bot.scraper_login", flush=True)
        status.update(
            status="authentication_required",
            message="SMU sign-in needs attention. Showing the last successful snapshot.",
        )
        exit_code = 1
    except LayoutChanged:
        status.update(
            status="layout_changed",
            message="FBS availability could not be read reliably. Showing the last successful snapshot.",
        )
        exit_code = 1
    except Exception:
        status.update(
            status="failed",
            message="The daily refresh failed. Showing the last successful snapshot.",
        )
        exit_code = 1
    write_json(args.output / "status.json", status)
    # Fixed messages only: never print exception text, page content, or credentials.
    print(status["message"], flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
