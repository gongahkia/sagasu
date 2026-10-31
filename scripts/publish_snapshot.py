"""Merge one attempt into retained public data; failed runs cannot replace it."""

import argparse
import json
import shutil
from pathlib import Path


def merge(attempt, retained):
    attempt, retained = Path(attempt), Path(retained)
    status = json.loads((attempt / "status.json").read_text(encoding="utf-8"))
    if status["status"] == "ok":
        snapshot = json.loads((attempt / "snapshot.json").read_text(encoding="utf-8"))
        if snapshot.get("version") != 1 or len(snapshot.get("days", [])) != 7:
            raise ValueError("Refusing an incomplete snapshot")
        shutil.copyfile(attempt / "snapshot.json", retained / "snapshot.json")
    shutil.copyfile(attempt / "status.json", retained / "status.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--retained", type=Path, required=True)
    args = parser.parse_args()
    merge(args.attempt, args.retained)
