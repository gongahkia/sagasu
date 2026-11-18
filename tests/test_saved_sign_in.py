"""The saved SMU sign-in: encrypted at rest, private, and never shown."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sagasu.saved_sign_in import (
    FILE_NAME,
    Credentials,
    SavedSignIn,
    SavedSignInError,
    make_key_file,
    mask,
)

EMAIL = "fixture.person@smu.edu.sg"
PASSWORD = "Fixture pass-word #1"


class StoreCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.key = make_key_file(self.root / "sign-in.key")
        self.store = SavedSignIn(self.state, self.key)


class SavedSignInTests(StoreCase):
    def test_round_trip_keeps_nothing_readable_on_disk(self):
        self.assertTrue(self.store.available)
        self.assertIsNone(self.store.load())
        self.store.save(EMAIL, PASSWORD)
        self.assertEqual(self.store.load(), Credentials(EMAIL, PASSWORD))
        raw = (self.state / FILE_NAME).read_text()
        self.assertNotIn(PASSWORD, raw)
        self.assertNotIn(EMAIL, raw)
        self.assertNotIn("fixture", raw.lower())
        mode = stat.S_IMODE((self.state / FILE_NAME).stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o600)
        # No temporary file is left behind.
        self.assertEqual([p.name for p in self.state.iterdir()], [FILE_NAME])

    def test_password_never_appears_in_text_forms(self):
        credentials = Credentials(EMAIL, PASSWORD)
        for text in (repr(credentials), str(credentials), f"{credentials}"):
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(EMAIL, text)
        self.store.save(EMAIL, PASSWORD)
        summary = self.store.summary()
        self.assertEqual(
            summary,
            {
                "available": True,
                "saved": True,
                "verified": False,
                "email": "f…n@smu.edu.sg",
                "owner": None,
            },
        )
        self.assertNotIn(PASSWORD, json.dumps(summary))

    def test_masking(self):
        self.assertEqual(mask("ab@smu.edu.sg"), "a…@smu.edu.sg")
        self.assertEqual(mask("a@x.y"), "a…@x.y")
        self.assertEqual(mask("not-an-email"), "(saved)")

    def test_another_key_or_a_damaged_file_reads_as_nothing_saved(self):
        self.store.save(EMAIL, PASSWORD)
        other = SavedSignIn(self.state, make_key_file(self.root / "other.key"))
        self.assertIsNone(other.load())
        self.assertFalse(other.summary()["saved"])
        (self.state / FILE_NAME).write_text("{not json")
        self.assertIsNone(self.store.load())
        (self.state / FILE_NAME).write_text(json.dumps({"version": 9}))
        self.assertIsNone(self.store.load())

    def test_a_tampered_file_is_refused(self):
        self.store.save(EMAIL, PASSWORD)
        stored = json.loads((self.state / FILE_NAME).read_text())
        data = bytearray(__import__("base64").b64decode(stored["data"]))
        data[0] ^= 1
        stored["data"] = __import__("base64").b64encode(bytes(data)).decode()
        (self.state / FILE_NAME).write_text(json.dumps(stored))
        self.assertIsNone(self.store.load())

    def test_forgetting_the_password_keeps_the_email_hint(self):
        self.store.save(EMAIL, PASSWORD)
        self.store.forget_password()
        self.assertIsNone(self.store.load())
        summary = self.store.summary()
        self.assertEqual((summary["saved"], summary["email"]), (False, "f…n@smu.edu.sg"))
        self.store.erase()
        self.assertFalse((self.state / FILE_NAME).exists())
        self.store.erase()  # nothing to erase is not an error

    def test_without_a_key_nothing_can_be_saved(self):
        for key in (None, self.root / "missing.key"):
            store = SavedSignIn(self.state, key)
            self.assertFalse(store.available)
            with self.assertRaises(SavedSignInError):
                store.save(EMAIL, PASSWORD)
            self.assertIsNone(store.load())
            self.assertEqual(store.summary()["available"], False)
        short = self.root / "short.key"
        short.write_bytes(b"c2hvcnQ=\n")
        self.assertFalse(SavedSignIn(self.state, short).available)

    def test_bad_values_are_refused_before_anything_is_written(self):
        for email, password in (
            ("no-at-sign", PASSWORD),
            ("two@@smu.edu.sg", PASSWORD),
            ("space @smu.edu.sg", PASSWORD),
            ("someone@gmail.com", PASSWORD),
            (EMAIL, ""),
            (EMAIL, "line\nbreak"),
            (EMAIL, "x" * 257),
        ):
            with self.subTest(email=email, password=len(password)):
                with self.assertRaises(SavedSignInError) as caught:
                    self.store.save(email, password)
                self.assertNotIn(password or "\0", str(caught.exception))
        self.assertFalse((self.state / FILE_NAME).exists())

    def test_a_failed_write_leaves_the_old_sign_in(self):
        self.store.save(EMAIL, PASSWORD)
        with patch("sagasu.saved_sign_in.NamedTemporaryFile", side_effect=OSError):
            with self.assertRaises(SavedSignInError):
                self.store.save(EMAIL, "another password")
        self.assertEqual(self.store.load().password, PASSWORD)

    def test_the_key_file_is_never_overwritten(self):
        with self.assertRaises(FileExistsError):
            make_key_file(self.key)

    def test_events_never_carry_the_values(self):
        with patch("sagasu.saved_sign_in.event") as event:
            self.store.save(EMAIL, PASSWORD)
            self.store.load()
            self.store.forget_password()
            self.store.erase()
        logged = json.dumps([[c.args, c.kwargs] for c in event.call_args_list], default=str)
        self.assertNotIn(PASSWORD, logged)
        self.assertNotIn(EMAIL, logged)


class ReviewRegressionTests(StoreCase):
    """Cases found by the independent reviews of this change."""

    def test_the_learnt_owner_survives_an_unchecked_save(self):
        self.store.save(EMAIL, PASSWORD, verified=True)
        self.assertEqual(self.store.owner(), EMAIL)
        # A new password saved while still signed in is not checked, but the
        # copy stays the same person's.
        self.store.save(EMAIL, "a new password")
        self.assertEqual(self.store.owner(), EMAIL)
        with self.assertRaises(SavedSignInError):
            self.store.save("someone.else@smu.edu.sg", PASSWORD)
        self.store.forget_password()
        with self.assertRaises(SavedSignInError):
            self.store.save("someone.else@smu.edu.sg", PASSWORD)
        self.store.erase()  # only the administrator's reset clears it
        self.store.save("someone.else@smu.edu.sg", PASSWORD)

    def test_an_unwritable_state_folder_fails_closed(self):
        self.store.save(EMAIL, PASSWORD, verified=True)
        with patch("sagasu.saved_sign_in._write_private", side_effect=OSError):
            self.store.record("starts")
            self.assertEqual(self.store.refusal(), ("sign_in_needs_page", None))
            self.store.hold("saved_password_rejected", page_only=True)
            self.assertEqual(self.store.held()["reason"], "saved_password_rejected")
            with self.assertRaises(SavedSignInError):
                self.store.forget_password(refused=True)
            # Still on disk, but never used again by this process.
            self.assertIsNone(self.store.load())
        # Writable again: the next successful write lifts the refusal.
        self.store.lift(by_page=True)
        self.assertIsNone(self.store.refusal())

    def test_a_blocked_hold_may_be_lifted_from_chat_after_a_day(self):
        now = [1000.0]
        store = SavedSignIn(self.state, self.key, clock=lambda: now[0])
        store.hold("sign_in_blocked", page_only=True)
        self.assertFalse(store.lift(by_page=False, asked_at=now[0] + 1))
        now[0] += 24 * 3600
        self.assertTrue(store.lift(by_page=False, asked_at=now[0]))
        self.assertIsNone(store.held())


if __name__ == "__main__":
    unittest.main()
