"""Read-only scheduled scraper. Run with python -m bot.scrape_daily."""

import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import load_dotenv

from .fbs_client import AuthenticationRequired, FBSClient, LayoutChanged
from .snapshot import SGT, public_snapshot, search_days, write_json


async def scrape():
    if not os.environ.get("SMU_LOGIN_EMAIL", "").strip() or not os.environ.get(
        "SMU_LOGIN_PASSWORD"
    ):
        raise AuthenticationRequired("Scheduled login credentials are missing")
    results = []
    with TemporaryDirectory(prefix="sagasu-browser-") as directory:
        async with asyncio.timeout(1200):
            async with FBSClient(
                interactive=False,
                headless=True,
                keep_alive=False,
                profile_path=Path(directory),
                unattended=True,
                strict_availability=True,
            ) as client:
                for index, config in enumerate(search_days(), 1):
                    print(f"Reading availability day {index}/7", flush=True)
                    results.append(await client.search(config))
    return public_snapshot(results)


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/daily"))
    args = parser.parse_args()
    # Never allow a leftover output file to masquerade as this run's success.
    snapshot_path = args.output / "snapshot.json"
    snapshot_path.unlink(missing_ok=True)
    status = {"attempted_at": datetime.now(SGT).isoformat()}
    exit_code = 0
    try:
        snapshot = asyncio.run(scrape())
        write_json(snapshot_path, snapshot)
        status.update(status="ok", message="Daily availability refresh completed.")
    except AuthenticationRequired:
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
