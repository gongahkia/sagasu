"""Build the static site using only sanitized snapshot files."""

import argparse
import json
import shutil
from pathlib import Path

if __package__:
    from .public_data import sanitize_snapshot, sanitize_status
else:
    from public_data import sanitize_snapshot, sanitize_status

ROOT = Path(__file__).resolve().parents[1]


def build(output, data):
    output = Path(output).resolve()
    # Never recursively delete a caller-selected directory.
    output.mkdir(parents=True, exist_ok=True)
    for name in (
        "index.html",
        "setup.html",
        "styles.css",
        "app.js",
        "search.js",
        "map-data.js",
        "floor-geometry.js",
        "floor-3d.js",
    ):
        shutil.copyfile(ROOT / "web" / name, output / name)
    (output / "assets").mkdir(exist_ok=True)
    shutil.copyfile(
        ROOT / "asset/logo/icon_no_words.jpg", output / "assets/icon_no_words.jpg"
    )
    (output / "data").mkdir(exist_ok=True)
    for name in ("snapshot.json", "status.json"):
        source = Path(data) / name
        if not source.exists():
            source = ROOT / "web/data" / name
        # Parse before copying: no broken JSON reaches deployment.
        value = json.loads(source.read_text(encoding="utf-8"))
        value = (
            sanitize_snapshot(value)
            if name == "snapshot.json"
            else sanitize_status(value)
        )
        (output / "data" / name).write_text(
            json.dumps(value, indent=2) + "\n", encoding="utf-8"
        )
    (output / ".nojekyll").touch()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--data", type=Path, default=ROOT / "web/data")
    args = parser.parse_args()
    build(args.output, args.data)
