import json
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.public_data import sanitize_snapshot, sanitize_status, write_json
from scripts.publish_snapshot import (
    PublishError,
    github_repository,
    merge,
    publish,
    request_pages,
)
from scripts.schedule_refresh import schedule_files, scheduler_kind


def snapshot():
    return {
        "version": 1,
        "updated_at": "2026-10-31T15:00:00+08:00",
        "timezone": "Asia/Singapore",
        "days": [
            {
                "date": (datetime(2026, 10, 31) + timedelta(days=offset))
                .date()
                .isoformat(),
                "start_time": "08:00",
                "end_time": "22:00",
                "rooms": [
                    {
                        "name": "Synthetic GSR",
                        "timeslots": [
                            {"start": "08:00", "end": "22:00", "status": "free"}
                        ],
                    }
                ],
            }
            for offset in range(7)
        ],
    }


class PublicDataTest(unittest.TestCase):
    def test_nested_private_fields_and_arbitrary_status_text_are_stripped(self):
        source = snapshot()
        source["credentials"] = "SECRET"
        source["days"][0]["rooms"][0]["timeslots"][0]["details"] = "PRIVATE BOOKER"
        self.assertEqual(sanitize_snapshot(source), snapshot())
        status = sanitize_status(
            {"status": "failed", "message": "SECRET", "cookies": "SECRET"}
        )
        self.assertNotIn("SECRET", json.dumps(status))
        self.assertEqual(set(status), {"status", "attempted_at", "message"})

    def test_missing_days_gaps_and_duplicate_dates_are_rejected(self):
        for mutation in ("days", "slots", "dates"):
            source = snapshot()
            if mutation == "days":
                source["days"].pop()
            elif mutation == "slots":
                source["days"][0]["rooms"][0]["timeslots"][0]["start"] = "09:00"
            else:
                source["days"][1]["date"] = source["days"][0]["date"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                sanitize_snapshot(source)

    def test_invalid_success_cannot_replace_retained_data_or_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "retained/snapshot.json", snapshot())
            write_json(root / "retained/status.json", {"status": "ok"})
            write_json(root / "attempt/snapshot.json", {"version": 1, "days": []})
            write_json(root / "attempt/status.json", {"status": "ok"})
            before = (root / "retained/status.json").read_bytes()
            with self.assertRaises(ValueError):
                merge(root / "attempt", root / "retained")
            self.assertEqual((root / "retained/status.json").read_bytes(), before)
            self.assertEqual(
                json.loads((root / "retained/snapshot.json").read_text()), snapshot()
            )


class PublisherGitTest(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        ).stdout

    def test_only_two_public_files_are_pushed_and_failure_retains_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, working, attempt = (
                root / "remote.git",
                root / "working",
                root / "attempt",
            )
            remote.mkdir()
            working.mkdir()
            self.git(remote, "init", "--bare", "--quiet")
            self.git(working, "init", "--quiet")
            self.git(working, "remote", "add", "origin", str(remote))
            (working / "user-work.txt").write_text("preserve staged work")
            self.git(working, "add", "user-work.txt")
            original = self.git(working, "status", "--porcelain")
            write_json(attempt / "snapshot.json", {**snapshot(), "private": "SECRET"})
            write_json(attempt / "status.json", {"status": "ok", "message": "SECRET"})
            (attempt / "credentials.json").write_text("SECRET")
            self.assertTrue(publish(attempt, working))
            self.assertEqual(self.git(working, "status", "--porcelain"), original)
            self.assertEqual(
                set(
                    self.git(
                        remote, "ls-tree", "--name-only", "availability-data"
                    ).splitlines()
                ),
                {"snapshot.json", "status.json"},
            )
            exported = self.git(remote, "show", "availability-data:snapshot.json")
            self.assertNotIn("SECRET", exported)
            self.assertEqual(json.loads(exported), snapshot())
            write_json(attempt / "snapshot.json", {"days": []})
            write_json(
                attempt / "status.json",
                {"status": "authentication_required", "message": "SECRET"},
            )
            self.assertTrue(publish(attempt, working))
            self.assertEqual(
                self.git(remote, "show", "availability-data:snapshot.json"), exported
            )
            self.assertNotIn(
                "SECRET", self.git(remote, "show", "availability-data:status.json")
            )
            self.assertFalse(publish(attempt, working))
            self.assertEqual(self.git(working, "status", "--porcelain"), original)


class RefreshTest(unittest.TestCase):
    def test_startup_failure_publishes_failure_status_and_keeps_local_snapshot(self):
        from bot.refresh_daily import refresh

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "snapshot.json", snapshot())
            write_json(root / "status.json", {"status": "ok"})
            with (
                patch("bot.refresh_daily.profile_lock", return_value=nullcontext()),
                patch(
                    "bot.refresh_daily.scrape_main", side_effect=RuntimeError("PRIVATE")
                ),
                patch("bot.refresh_daily.publish") as publishing,
                patch("builtins.print"),
            ):
                self.assertEqual(refresh(root), 1)
                publishing.assert_called_once_with(root)
            self.assertEqual(
                json.loads((root / "snapshot.json").read_text()), snapshot()
            )
            status = json.loads((root / "status.json").read_text())
            self.assertEqual(status["status"], "failed")
            self.assertNotIn("PRIVATE", json.dumps(status))

    def test_publishing_failure_returns_nonzero(self):
        from bot.refresh_daily import refresh

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("bot.refresh_daily.profile_lock", return_value=nullcontext()),
            patch("bot.refresh_daily.scrape_main", return_value=0),
            patch("bot.refresh_daily.publish", side_effect=PublishError("PRIVATE")),
            patch("builtins.print"),
        ):
            self.assertEqual(refresh(directory), 2)


class SchedulerTest(unittest.TestCase):
    def test_linux_prefers_working_user_systemd(self):
        with (
            patch("scripts.schedule_refresh.platform.system", return_value="Linux"),
            patch("scripts.schedule_refresh.shutil.which", return_value="/bin/tool"),
            patch("scripts.schedule_refresh.command", return_value=Mock(returncode=0)),
        ):
            self.assertEqual(scheduler_kind(), "systemd")

    def test_missing_user_bus_falls_back_to_cron(self):
        with (
            patch("scripts.schedule_refresh.platform.system", return_value="Linux"),
            patch("scripts.schedule_refresh.shutil.which", return_value="/bin/tool"),
            patch("scripts.schedule_refresh.command", return_value=Mock(returncode=1)),
        ):
            self.assertEqual(scheduler_kind(), "cron")

    def test_launchagent_preserves_paths_and_contains_no_login_credentials(self):
        import plistlib

        with patch("scripts.schedule_refresh.datetime") as clock:
            clock.now.return_value.astimezone.return_value.utcoffset.return_value = (
                timedelta(hours=8)
            )
            files = schedule_files(
                "launchd",
                "/tmp/project with spaces",
                "/tmp/project with spaces/.venv/bin/python",
                "chromium",
                search_path="/usr/bin:/usr/local/bin",
            )
        value = plistlib.loads(next(iter(files.values())))
        self.assertEqual(value["StartCalendarInterval"], {"Hour": 6, "Minute": 17})
        self.assertEqual(
            value["ProgramArguments"][0], "/tmp/project with spaces/.venv/bin/python"
        )
        self.assertEqual(
            set(value["EnvironmentVariables"]), {"SAGASU_BROWSER_CHANNEL", "PATH"}
        )
        self.assertNotIn("RunAtLoad", value)

    def test_systemd_timer_uses_singapore_timezone_and_persistence(self):
        files = schedule_files(
            "systemd",
            "/tmp/project with spaces",
            "/tmp/project with spaces/.venv/bin/python",
            "chromium",
        )
        timer = files["sg.sagasu.availability.timer"].decode()
        service = files["sg.sagasu.availability.service"].decode()
        self.assertIn("06:17:00 Asia/Singapore", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn('WorkingDirectory="/tmp/project with spaces"', service)
        self.assertIn("TimeoutStartSec=30min", service)


class WorkflowDispatchTest(unittest.TestCase):
    def test_github_remote_forms_map_to_the_same_repository(self):
        self.assertEqual(
            github_repository("https://github.com/example/sagasu.git"), "example/sagasu"
        )
        self.assertEqual(
            github_repository("git@github.com:example/sagasu.git"), "example/sagasu"
        )
        self.assertIsNone(github_repository("/tmp/test.git"))

    def test_dispatch_uses_default_branch_and_no_credentials_in_arguments(self):
        with (
            patch(
                "scripts.publish_snapshot.subprocess.run",
                return_value=Mock(returncode=0),
            ) as run,
            patch("builtins.print"),
        ):
            request_pages("/usr/local/bin/gh", "example/sagasu")
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/local/bin/gh",
                "workflow",
                "run",
                "availability.yml",
                "--repo",
                "example/sagasu",
            ],
        )

    def test_dispatch_failure_is_not_reported_as_publish_success(self):
        with patch(
            "scripts.publish_snapshot.subprocess.run", return_value=Mock(returncode=1)
        ):
            with self.assertRaises(PublishError) as error:
                request_pages("/usr/local/bin/gh", "example/sagasu")
        self.assertEqual(error.exception.operation, "workflow_dispatch")
