"""Retain sanitized data locally, or push only public JSON to availability-data."""

import argparse
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

if __package__:
    from .public_data import read_json, sanitize_snapshot, sanitize_status, write_json
else:
    from public_data import read_json, sanitize_snapshot, sanitize_status, write_json

ROOT = Path(__file__).resolve().parents[1]
BRANCH = "availability-data"
PUBLIC_FILES = {"snapshot.json", "status.json"}


def load_attempt(attempt):
    attempt = Path(attempt)
    status = sanitize_status(read_json(attempt / "status.json"))
    snapshot = None
    if status["status"] == "ok":
        snapshot = sanitize_snapshot(read_json(attempt / "snapshot.json"))
        if snapshot is None:
            raise ValueError("A successful attempt must contain a complete snapshot")
    return status, snapshot


def merge(attempt, retained):
    status, snapshot = load_attempt(attempt)
    retained = Path(retained)
    if status["status"] == "ok":
        write_json(retained / "snapshot.json", snapshot)
    write_json(retained / "status.json", status)


class PublishError(RuntimeError):
    """Fixed publishing diagnostics; never emit Git credential errors."""

    def __init__(self, message, *, operation="git", code="failed"):
        super().__init__(message)
        self.operation = operation
        self.code = code


def git(directory, *args, allowed=(0,)):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PublishError("Git operation failed or timed out") from None
    if result.returncode not in allowed:
        detail = result.stderr.casefold()
        code = "failed"
        for candidate, patterns in (
            (
                "authentication",
                (
                    "authentication failed",
                    "could not read username",
                    "invalid username",
                    "could not read password",
                ),
            ),
            ("access_denied", ("permission to", "403", "repository not found")),
            ("remote_changed", ("non-fast-forward", "fetch first")),
            ("network", ("could not resolve", "failed to connect", "ssl certificate")),
        ):
            if any(pattern in detail for pattern in patterns):
                code = candidate
                break
        operation = "commit" if args[0] == "-c" else args[0]
        raise PublishError(
            "Git operation failed; check remote access and branch rules locally",
            operation=operation,
            code=code,
        )
    return result


def publish(attempt, repository=ROOT):
    # Validate before contacting GitHub. This does not touch the caller's checkout/index.
    load_attempt(attempt)
    repository = Path(repository).resolve()
    remote = git(repository, "remote", "get-url", "origin").stdout.strip()
    branch = git(
        repository,
        "ls-remote",
        "--exit-code",
        "--heads",
        "origin",
        f"refs/heads/{BRANCH}",
        allowed=(0, 2),
    )
    with TemporaryDirectory(prefix="sagasu-public-") as directory:
        retained = Path(directory)
        git(retained, "init", "--quiet")
        git(retained, "remote", "add", "origin", remote)
        # Reuse the authenticated local GitHub CLI in this temporary repository.
        # Avoid changing global Git settings or relying on stale Keychain credentials.
        gh = shutil.which("gh")
        if (
            gh
            and urlparse(remote).hostname == "github.com"
            and urlparse(remote).scheme == "https"
        ):
            auth = subprocess.run(
                [gh, "auth", "status", "--hostname", "github.com"],
                capture_output=True,
                timeout=30,
            )
            if auth.returncode == 0:
                git(retained, "config", "credential.helper", "")
                git(
                    retained,
                    "config",
                    "--add",
                    "credential.helper",
                    "!" + shlex.quote(gh) + " auth git-credential",
                )
        if branch.returncode == 0:
            git(
                retained,
                "fetch",
                "--quiet",
                "--depth=1",
                "origin",
                f"refs/heads/{BRANCH}",
            )
            git(retained, "checkout", "--quiet", "--detach", "FETCH_HEAD")
            files = set(
                git(retained, "ls-tree", "--name-only", "HEAD").stdout.splitlines()
            )
            if files != PUBLIC_FILES:
                raise PublishError(
                    "Public data branch must contain only snapshot.json and status.json"
                )
            # Reconstruct existing data too, so failed attempts cannot republish unknown fields.
            write_json(
                retained / "snapshot.json",
                sanitize_snapshot(read_json(retained / "snapshot.json")),
            )
            write_json(
                retained / "status.json",
                sanitize_status(read_json(retained / "status.json")),
            )
        else:
            git(retained, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
            write_json(retained / "snapshot.json", None)
        merge(attempt, retained)
        git(retained, "config", "user.name", "Sagasu local publisher")
        git(retained, "config", "user.email", "sagasu@localhost")
        git(retained, "add", "--", "snapshot.json", "status.json")
        if git(retained, "diff", "--cached", "--quiet", allowed=(0, 1)).returncode == 0:
            return False
        git(
            retained,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "Refresh public availability snapshot",
        )
        # Ordinary fast-forward push: concurrent remote updates fail safely; never force.
        git(retained, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", type=Path, required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--retained", type=Path)
    destination.add_argument("--push", action="store_true")
    parser.add_argument("--repository", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        if args.push:
            changed = publish(args.attempt, args.repository)
            print(
                "Sanitized public data published."
                if changed
                else "Public data is already current."
            )
        else:
            merge(args.attempt, args.retained)
    except PublishError as error:
        print(
            f"Publishing stopped: {error.operation}; diagnostic: {error.code}. Raw Git diagnostics suppressed."
        )
        return 1
    except Exception:
        print(
            "Public publishing could not be confirmed. Check JSON completeness, Git access, and data branch rules locally; raw diagnostics suppressed."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
