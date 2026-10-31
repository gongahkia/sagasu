"""Run the local read-only scrape and publish sanitized data, including failures."""

import argparse
from datetime import datetime
from pathlib import Path

from scripts.public_data import write_json
from scripts.publish_snapshot import publish

from .scrape_daily import main as scrape_main
from .scraper_session import ROOT, ProfileBusy, profile_lock
from .snapshot import SGT


def refresh(output, *, push=True):
    output = Path(output)
    # Serialize the entire scrape/publish cycle as well as individual browser use.
    with profile_lock(ROOT / "artifacts/refresh-job"):
        # If startup fails, a prior ok status must not masquerade as this attempt.
        write_json(
            output / "status.json",
            {
                "status": "failed",
                "attempted_at": datetime.now(SGT).isoformat(),
                "message": "The daily refresh failed. Showing the last successful snapshot.",
            },
        )
        try:
            result = scrape_main(["--output", str(output)])
        except Exception:
            result = 1
            print("Local scrape failed before completion; raw diagnostics suppressed.")
        if push:
            try:
                changed = publish(output)
                print(
                    "Sanitized public data published."
                    if changed
                    else "Public data is already current."
                )
            except Exception:
                print(
                    "Public publishing could not be confirmed. Check Git access and branch rules locally; raw diagnostics suppressed."
                )
                return 2
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/daily")
    parser.add_argument(
        "--no-push", action="store_true", help="Refresh local data only"
    )
    args = parser.parse_args()
    try:
        return refresh(args.output, push=not args.no_push)
    except ProfileBusy:
        print("Another local refresh is running; this run was skipped.")
        return 1
    except KeyboardInterrupt:
        print("Local refresh cancelled.")
        return 130
    except Exception:
        print("Local refresh could not start; check local paths and permissions.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
