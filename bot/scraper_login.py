"""Bootstrap/renew the dedicated scraper session with manual Microsoft MFA."""

import argparse
import asyncio

from dotenv import load_dotenv

from .fbs_client import AuthenticationRequired
from .scraper_session import (
    ROOT,
    ProfileBusy,
    ScraperClient,
    profile_lock,
    scraper_profile,
)


async def login(timeout=600):
    profile = scraper_profile()
    with profile_lock(profile):
        async with ScraperClient(
            profile_path=profile,
            headless=False,
            manual_login=True,
            login_timeout=timeout,
        ) as client:
            print(
                "Complete Microsoft login and MFA in the browser. "
                "No credentials or verification codes belong in the terminal.",
                flush=True,
            )
            await client.ensure_authenticated()
    print(
        "Authenticated FBS search controls verified; scraper session saved.", flush=True
    )


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=int, default=600, help="Manual login deadline in seconds"
    )
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800:
        parser.error("--timeout must be between 1 and 1800 seconds")
    try:
        asyncio.run(login(args.timeout))
    except ProfileBusy:
        print("Scraper profile is busy. Close the other scraper/login run and retry.")
        return 1
    except AuthenticationRequired:
        print(
            "Login did not reach authenticated FBS before the deadline or browser closed."
        )
        return 1
    except KeyboardInterrupt:
        print("Login cancelled; browser closed.")
        return 130
    except Exception:
        print(
            "Unable to open or save the scraper browser. Check browser installation and profile configuration."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
