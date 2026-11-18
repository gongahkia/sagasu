"""Saved SMU sign-in: settings, start-up wiring, the store's ledger and owner, and
the deploy kit. Everything stays local (temporary folders, a fake clock, patched
process calls): nothing contacts FBS or Microsoft, needs root or changes the system.
"""

import contextlib
import importlib.util
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dotenv import dotenv_values

from sagasu import console
from sagasu.config import Settings, parse_remote_listener
from sagasu.console import Redact, add_secret
from sagasu.host import identity_parts, saved_sign_in
from sagasu.personal import SMU_EMAIL_RE
from sagasu.saved_sign_in import DAY, FILE_NAME, HOUR, LEDGER_NAME, PASSWORDS_PER_DAY
from sagasu.saved_sign_in import SIGN_INS_PER_HOUR, Credentials, SavedSignIn
from sagasu.saved_sign_in import SavedSignInError, make_key_file, mask
from tests.test_remote import VALID

KIT = Path(__file__).resolve().parents[1] / "deploy" / "server"
KEY_FILE, ACCOUNT = "SAGASU_HTTP_SIGN_IN_KEY_FILE", "SAGASU_SMU_ACCOUNT"
EXCEEDED = "sign_in_attempts_exceeded"
LISTENER = "SAGASU_BROWSER_PROFILE=browser\n" + "".join(f"{k}={v}\n" for k, v in VALID.items())
EMAIL, OTHER = "fixture.owner@smu.edu.sg", "fixture.other@smu.edu.sg"
PASSWORD, NEW_PASSWORD = "Fixture owner pass #1", "Fixture owner pass #2"
GOOD_EMAILS = ("alice.tan@smu.edu.sg", "Alice.Tan.2024@SCIS.smu.edu.sg", "a_b-c@law.smu.edu.sg")
BAD_EMAILS = ("alice@gmail.com", "alice@smu.edu.sg.example.com", "alice@evil.smu.edu.sg",
              "@smu.edu.sg", "alice tan@smu.edu.sg", "alice@@smu.edu.sg", "alice@smu.edu")


def load_settings(directory, text):
    env = Path(directory) / ".env"
    env.write_text(text)
    with patch.dict(os.environ, {}, clear=True):
        return Settings.load(env)


def load_kit(name):
    """A deploy-kit script (no .py suffix) as a module, without running main."""
    loader = SourceFileLoader("kit_" + name.replace("-", "_"), str(KIT / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


@contextlib.contextmanager
def patched(*patchers):
    with contextlib.ExitStack() as stack:
        yield [stack.enter_context(patcher) for patcher in patchers]


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class TempCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        self.state.mkdir()

    def read_only_state(self):
        if os.geteuid() == 0:
            self.skipTest("root can write to a read-only folder")
        os.chmod(self.state, 0o500)
        self.addCleanup(os.chmod, self.state, 0o700)


class SettingsTests(unittest.TestCase):
    def test_key_file_and_account_are_read_with_the_listener(self):
        listener, problems = parse_remote_listener(
            {**VALID, KEY_FILE: " /etc/sagasu/sagasu.signin-key ", ACCOUNT: " Fixture.Owner@SMU.edu.sg "}
        )
        self.assertEqual(problems, [])
        self.assertEqual(listener.sign_in_key_file, "/etc/sagasu/sagasu.signin-key")
        self.assertEqual(listener.smu_account, "Fixture.Owner@SMU.edu.sg")
        for values in (VALID, {**VALID, KEY_FILE: "  ", ACCOUNT: ""}):
            listener, problems = parse_remote_listener(values)
            self.assertEqual((listener.sign_in_key_file, listener.smu_account, problems), (None, None, []))
        longest = "/" + "k" * 199
        self.assertEqual(parse_remote_listener({**VALID, KEY_FILE: longest})[0].sign_in_key_file, longest)
        for email in GOOD_EMAILS:
            listener, problems = parse_remote_listener({**VALID, ACCOUNT: email})
            self.assertEqual((listener.smu_account, problems), (email, []))

    def test_a_bad_key_file_or_account_keeps_the_listener_off(self):
        keys = ("sagasu.signin-key", "~/sign-in.key", "./sign-in.key", "/etc/sagasu/\0key", "/" + "k" * 200)
        changes = [{KEY_FILE: key} for key in keys] + [{ACCOUNT: e} for e in ("not-an-email", *BAD_EMAILS)]
        for change in changes:
            with self.subTest(change=change):
                listener, problems = parse_remote_listener({**VALID, **change})
                self.assertIsNone(listener)
                self.assertEqual(len(problems), 1)
                [(name, value)] = change.items()
                self.assertIn(name, problems[0])
                self.assertNotIn(value, problems[0])  # never repeats an address or path

    def test_settings_report_whether_saved_sign_in_is_set_up(self):
        key = f"{KEY_FILE}=/etc/sagasu/sagasu.signin-key\n"
        with tempfile.TemporaryDirectory() as directory:
            for extra, expected in (("", False), (key, True), (key + f"{ACCOUNT}={EMAIL}\n", True)):
                settings = load_settings(directory, LISTENER + extra)
                self.assertEqual(settings.issues(), [])
                remote = settings.public()["remote_listener"]
                self.assertTrue(remote["enabled"])
                self.assertIs(remote["saved_sign_in"], expected)
            broken = load_settings(directory, LISTENER + f"{ACCOUNT}=someone@gmail.com\n")
            self.assertFalse(broken.public()["remote_listener"]["enabled"])
            [issue] = broken.issues()
            self.assertEqual((issue.name, issue.blocks_startup), ("config.remote_listener", True))
            self.assertIn(ACCOUNT, issue.message)
            self.assertNotIn("someone@gmail.com", json.dumps(broken.public()))

    def test_each_saved_sign_in_setting_changes_the_host_digest(self):
        # As tests.test_remote's test_every_new_setting_changes_the_digest_when_used.
        key_a, key_b = (f"{KEY_FILE}=/etc/sagasu/{n}.signin-key\n" for n in "ab")
        extras = ("", key_a, key_b, key_a + f"{ACCOUNT}={EMAIL}\n", key_a + f"{ACCOUNT}={OTHER}\n")
        texts = [LISTENER + extra for extra in extras]
        with tempfile.TemporaryDirectory() as directory:
            digests = {identity_parts(load_settings(directory, t))["configuration"] for t in texts}
        self.assertEqual(len(digests), len(texts))


class StartUpTests(TempCase):
    def setUp(self):
        super().setUp()
        self.settings = SimpleNamespace(preferences=self.state / "personal.sqlite3")

    def listener(self, key, account=None):
        return SimpleNamespace(sign_in_key_file=None if key is None else str(key), smu_account=account)

    def test_off_without_the_setting_or_a_usable_key(self):
        for listener in (None, object(), self.listener(None), self.listener("")):
            with self.subTest(listener=listener):
                self.assertIsNone(saved_sign_in(self.settings, listener))
        garbage, short = self.root / "garbage.key", self.root / "short.key"
        garbage.write_bytes(b"not base64 at all!\n")
        short.write_bytes(b"c2hvcnQ=\n")
        for key in (self.root / "missing.key", garbage, short, self.root):
            with self.subTest(key=key.name), patch("sagasu.observability.event") as event:
                self.assertIsNone(saved_sign_in(self.settings, self.listener(key)))
                self.assertEqual(event.call_args.args[0], "saved_sign_in.unavailable")
        # Without the cryptography package the copy also behaves as before.
        good = make_key_file(self.root / "good.key")
        with patch.dict(sys.modules, {"cryptography.hazmat.primitives.ciphers.aead": None}):
            self.assertIsNone(saved_sign_in(self.settings, self.listener(good)))
        self.assertEqual(list(self.state.iterdir()), [])

    def test_a_readable_key_gives_a_store_in_the_state_folder(self):
        key = make_key_file(self.root / "sign-in.key")
        store = saved_sign_in(self.settings, self.listener(key, EMAIL))
        self.assertIsInstance(store, SavedSignIn)
        self.assertEqual((store.path, store.ledger_path), (self.state / FILE_NAME, self.state / LEDGER_NAME))
        self.assertEqual((store.key_file, store.account), (key, EMAIL))
        # From real settings: the listener's own key and account.
        settings = load_settings(
            self.root,
            LISTENER + f"SAGASU_PREFERENCES_PATH={self.state}/personal.sqlite3\n"
            f"{KEY_FILE}={key}\n{ACCOUNT}={EMAIL}\n",
        )
        store = saved_sign_in(settings, settings.http)
        self.assertEqual(store.path, self.state.resolve() / FILE_NAME)
        self.assertEqual(store.owner(), EMAIL)
        with self.assertRaises(SavedSignInError):
            store.check_account(OTHER)


class RuntimeSecretTests(unittest.TestCase):
    def secret(self, value):
        self.addCleanup(console._RUNTIME_SECRETS.discard, value)
        add_secret(value)
        return value

    def test_every_redact_filter_removes_a_secret_learnt_later(self):
        earlier, configured = Redact(), Redact(["configured-fixture-secret"])
        secret = self.secret(f"Fixture runtime {uuid.uuid4().hex}")
        inner = self.secret(secret[:20])  # one inside another is still removed whole
        for redact in (earlier, configured, Redact()):
            self.assertEqual(redact.clean(f"typed {secret} in"), "typed <redacted> in")
            self.assertEqual(redact.clean(inner), "<redacted>")
        record = logging.LogRecord("sagasu.test", logging.INFO, __file__, 1, "saw %s", (secret,), None)
        record.fields = {"detail": f"has {secret}", "nested": [secret]}
        earlier.filter(record)
        self.assertNotIn(secret, record.getMessage())
        self.assertNotIn(secret, json.dumps(record.fields))
        # Tiny or non-text values are not secrets.
        before = set(console._RUNTIME_SECRETS)
        for value in ("", "abc", None, 12345, b"fixture-bytes"):
            add_secret(value)
        self.assertEqual(console._RUNTIME_SECRETS, before)
        self.assertEqual(Redact().clean("abc and 12345"), "abc and 12345")

    def test_loading_the_saved_password_registers_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SavedSignIn(directory, make_key_file(Path(directory) / "sign-in.key"))
            password = f"Fixture load {uuid.uuid4().hex}"
            self.addCleanup(console._RUNTIME_SECRETS.discard, password)
            redact = Redact()
            store.save(EMAIL, password)
            self.assertIn(password, redact.clean(password))  # not yet read back
            self.assertEqual(store.load().password, password)
            self.assertNotIn(password, redact.clean(f"value={password}"))


class LedgerTests(TempCase):
    def setUp(self):
        super().setUp()
        self.clock = SimpleNamespace(now=1_000_000.0)
        self.store = self.open()

    def open(self):
        # The ledger needs no key; a fresh object reads what a restart would.
        return SavedSignIn(self.state, None, clock=lambda: self.clock.now)

    def test_a_fresh_ledger_allows_a_sign_in(self):
        self.assertIsNone(self.store.refusal())
        self.assertIsNone(self.store.held())
        self.assertEqual(self.store.recent(), [])
        self.assertFalse((self.state / LEDGER_NAME).exists())

    def assert_limit(self, kind, count, spacing, window):
        start = self.clock.now
        for i in range(count):
            self.assertIsNone(self.store.refusal())
            self.clock.now = start + spacing * i
            self.store.record(kind)
        reason, minutes = self.open().refusal()  # survives a restart
        self.assertEqual(reason, EXCEEDED)
        remaining = start + window - self.clock.now  # from the oldest attempt
        self.assertTrue(remaining <= minutes * 60 <= remaining + 60, minutes)
        self.clock.now = start + window - 1
        self.assertEqual(self.store.refusal(), (EXCEEDED, 1))
        self.clock.now = start + window
        self.assertIsNone(self.store.refusal())

    def test_sign_ins_are_limited_per_hour(self):
        self.assert_limit("starts", SIGN_INS_PER_HOUR, 600, HOUR)
        self.assertEqual((self.state / LEDGER_NAME).stat().st_mode & 0o777, 0o600)

    def test_passwords_are_limited_per_day(self):
        self.assert_limit("passwords", PASSWORDS_PER_DAY, HOUR, DAY)

    def test_the_wait_is_never_more_than_a_day(self):
        for _ in range(PASSWORDS_PER_DAY):
            self.store.record("passwords")
        self.assertEqual(self.store.refusal(), (EXCEEDED, 1440))

    # Regression (fixed): SavedSignIn.refusal checks the hourly limit first and returns its
    # wait, even when the daily password limit is also used up and lasts far
    # longer; the person is told to retry in about an hour and is refused again.
    def test_the_wait_given_covers_both_limits(self):
        start = self.clock.now
        for _ in range(PASSWORDS_PER_DAY):
            self.store.record("passwords")
        self.clock.now = start + 2 * HOUR
        for _ in range(SIGN_INS_PER_HOUR):
            self.store.record("starts")
        reason, minutes = self.store.refusal()
        self.assertEqual(reason, EXCEEDED)
        self.clock.now += minutes * 60
        self.assertIsNone(self.store.refusal())

    def test_an_unreadable_ledger_counts_as_limits_used_up(self):
        path = self.state / LEDGER_NAME
        for damage in (b"{not json", b"[]", b"null", b"\xff\xfe\xfd", None):
            with self.subTest(damage=damage):
                path.mkdir() if damage is None else path.write_bytes(damage)
                refused = self.open().refusal()
                self.assertEqual(refused[0], EXCEEDED)
                self.assertGreaterEqual(refused[1], 1)
                path.rmdir() if path.is_dir() else path.unlink()
        self.assertIsNone(self.open().refusal())  # missing is simply unused
        # The next write replaces a damaged ledger with one still used up.
        path.write_text("{not json")
        self.store.record("starts")
        self.assertIsInstance(json.loads(path.read_text()), dict)
        self.assertEqual(self.open().refusal()[0], EXCEEDED)

    # Regression (fixed): SavedSignIn._ledger only treats bad JSON (or a non-dict) as
    # unreadable. A dict whose "starts"/"passwords" are not lists is iterated
    # as is: strings read as "no attempts" (limits off) and a number raises
    # TypeError out of refusal(), instead of counting as limits used up.
    def test_a_ledger_with_damaged_fields_counts_as_used_up(self):
        path = self.state / LEDGER_NAME
        for damaged in ({"starts": "xxxxx", "passwords": "xxxxx"}, {"starts": 7}):
            path.write_text(json.dumps(damaged))
            refused = self.open().refusal()
            self.assertIsNotNone(refused, damaged)
            self.assertEqual(refused[0], EXCEEDED)

    def test_a_ledger_that_cannot_be_written_stops_sign_ins(self):
        self.read_only_state()
        self.store.record("starts")
        self.assertEqual(self.store.refusal(), ("sign_in_needs_page", None))
        os.chmod(self.state, 0o700)
        self.store.record("starts")
        self.assertIsNone(self.store.refusal())

    def test_a_hold_only_the_page_may_lift(self):
        for _ in range(SIGN_INS_PER_HOUR):
            self.store.record("starts")
        self.store.hold("saved_password_rejected", page_only=True)
        expected = {"reason": "saved_password_rejected", "page_only": True, "at": self.clock.now}
        self.assertEqual(self.store.held(), expected)
        # A hold is the reason given, ahead of the limits, with no wait.
        self.assertEqual(self.store.refusal(), ("saved_password_rejected", None))
        self.assertFalse(self.store.lift(by_page=False))
        self.assertFalse(self.store.lift(by_page=False, asked_at=self.clock.now + 60))
        self.store.record("passwords")  # recording keeps the hold
        self.assertEqual(self.open().held(), expected)  # survives a restart
        self.assertTrue(self.store.lift(by_page=True))
        self.assertIsNone(self.store.held())
        self.assertIsNone(self.open().held())
        self.assertEqual(self.store.refusal()[0], EXCEEDED)
        self.assertTrue(self.store.lift(by_page=False))  # nothing to lift

    def test_chat_lifts_only_a_hold_set_before_the_ask(self):
        self.clock.now = 5000.0
        self.store.hold("sign_in_not_approved", page_only=False)
        # Set after (or as) the person asked: it is their answer, so it stays.
        self.assertFalse(self.store.lift(by_page=False, asked_at=4000.0))
        self.assertFalse(self.store.lift(by_page=False, asked_at=5000.0))
        self.assertEqual(self.store.refusal(), ("sign_in_not_approved", None))
        self.assertTrue(self.store.lift(by_page=False, asked_at=5000.5))
        self.assertIsNone(self.store.held())
        self.store.hold("sign_in_unfinished", page_only=False)
        self.assertTrue(self.store.lift(by_page=False))
        self.store.hold("sign_in_unfinished", page_only=False)
        self.assertTrue(self.store.lift(by_page=True))
        self.assertIsNone(self.store.refusal())

    def test_recent_keeps_the_last_ten(self):
        for i in range(12):
            self.clock.now += 1
            self.store.note(f"key{i}", "chat" if i % 2 else "page", "signed_in")
        recent = self.open().recent()
        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[0]["by"], "key2")
        last = {"at": int(self.clock.now), "by": "key11", "how": "chat", "outcome": "signed_in"}
        self.assertEqual(recent[-1], last)
        self.assertIsNone(self.store.refusal())  # notes are not attempts


class OwnershipTests(TempCase):
    def setUp(self):
        super().setUp()
        self.key = make_key_file(self.root / "sign-in.key")
        self.store = self.open()

    def open(self, account=None):
        return SavedSignIn(self.state, self.key, account=account)

    def refuse(self, store, email):
        with self.assertRaises(SavedSignInError) as caught:
            store.check_account(email)
        return str(caught.exception)

    def test_the_administrators_account_pins_the_copy(self):
        self.store.save(OTHER, PASSWORD)  # saved before the lock: never used after
        store = self.open(account=" Fixture.Owner@SMU.edu.sg ")
        self.assertIsNone(store.load())
        self.assertFalse(store.summary()["saved"])
        self.assertEqual(store.owner(), "Fixture.Owner@SMU.edu.sg")
        store.check_account(EMAIL)  # any case
        message = self.refuse(store, OTHER)
        self.assertIn(mask("Fixture.Owner@SMU.edu.sg"), message)
        self.assertNotIn("fixture.owner", message.lower())
        self.assertNotIn(OTHER, message)
        before = store.path.read_bytes()
        with self.assertRaises(SavedSignInError):
            store.save(OTHER, NEW_PASSWORD)
        self.assertEqual(store.path.read_bytes(), before)
        store.save(EMAIL, PASSWORD)
        self.assertEqual(store.load(), Credentials(EMAIL, PASSWORD))
        store.erase()  # the administrator's lock is in the settings, not the file
        self.assertEqual(store.owner(), "Fixture.Owner@SMU.edu.sg")

    def test_a_verified_save_pins_the_copy_and_an_unverified_one_does_not(self):
        self.store.save(EMAIL, PASSWORD)
        self.assertIsNone(self.store.owner())
        self.store.check_account(OTHER)
        self.store.save(OTHER, PASSWORD)  # not yet anyone's: replaced
        self.assertEqual(self.store.load().email, OTHER)
        self.store.save(EMAIL, PASSWORD, verified=True)
        self.assertEqual(self.open().owner(), EMAIL)
        self.refuse(self.store, OTHER)
        with self.assertRaises(SavedSignInError):
            self.store.save(OTHER, NEW_PASSWORD)
        self.assertEqual(self.store.load(), Credentials(EMAIL, PASSWORD))
        self.store.save(EMAIL, PASSWORD)  # the same password stays verified
        self.assertTrue(self.store.summary()["verified"])

    def test_mark_verified_pins_the_saved_account(self):
        self.store.mark_verified()  # nothing saved: nothing written
        self.assertFalse(self.store.path.exists())
        self.store.save(EMAIL, PASSWORD)
        self.store.mark_verified()
        self.assertEqual(self.store.owner(), EMAIL)
        self.assertTrue(self.store.summary()["verified"])
        self.refuse(self.store, OTHER)
        self.assertEqual(self.store.load(), Credentials(EMAIL, PASSWORD))

    def test_forgetting_the_password_keeps_the_owner(self):
        self.store.save(EMAIL, PASSWORD, verified=True)
        self.store.forget_password()
        self.store.mark_verified()  # without a password: no change
        self.assertIsNone(self.store.load())
        self.assertEqual(self.store.owner(), EMAIL)
        self.refuse(self.store, OTHER)
        shown = {"available": True, "saved": False, "verified": True, "email": mask(EMAIL), "owner": EMAIL}
        self.assertEqual(self.store.summary(), shown)

    def test_a_new_password_for_the_owner_keeps_the_copy_pinned(self):
        # "the first one Microsoft accepted here": a later unverified password
        # for that account, after a refused one or not, does not unpin it.
        for forget_first in (True, False):
            with self.subTest(forget_first=forget_first):
                self.store.erase()
                self.store.save(EMAIL, PASSWORD, verified=True)
                if forget_first:
                    self.store.forget_password()  # saved_password_rejected
                self.store.save(EMAIL, NEW_PASSWORD)
                self.assertEqual(self.open().owner(), EMAIL)
                self.refuse(self.store, OTHER)
                self.assertEqual(self.store.load(), Credentials(EMAIL, NEW_PASSWORD))

    def test_erase_removes_the_owner_and_all(self):
        self.store.save(EMAIL, PASSWORD, verified=True)
        self.store.erase()
        self.assertFalse(self.store.path.exists())
        self.assertIsNone(self.store.owner())
        empty = {"available": True, "saved": False, "verified": False, "email": None, "owner": None}
        self.assertEqual(self.store.summary(), empty)
        self.store.save(OTHER, PASSWORD)
        self.assertEqual(self.store.load().email, OTHER)

    def test_only_smu_addresses_are_accepted(self):
        for email in (*BAD_EMAILS, "", None, "x" * 250 + "@smu.edu.sg"):
            with self.subTest(email=email):
                self.assertIn("SMU email", self.refuse(self.store, email))

    def test_summary_never_contains_the_password(self):
        save = lambda: self.store.save(EMAIL, PASSWORD)  # noqa: E731
        for step in (save, self.store.mark_verified, self.store.forget_password, save, self.store.erase):
            step()
            self.assertNotIn(PASSWORD, json.dumps(self.store.summary()))
            if self.store.path.exists():
                self.assertNotIn(PASSWORD, self.store.path.read_text())
        self.store.save(EMAIL, PASSWORD)  # unverified: the address only masked
        self.assertNotIn(EMAIL, json.dumps(self.store.summary()))


class SagasuCtlTests(TempCase):
    @classmethod
    def setUpClass(cls):
        cls.ctl = load_kit("sagasu-ctl")

    def copy_in(self, name):
        """A Copy whose state folder is this test's own."""
        copy = self.ctl.Copy(name)
        original, copy.state = copy.state, str(self.state)
        copy.sign_in_state = [path.replace(original, copy.state, 1) for path in copy.sign_in_state]
        return copy

    def fill(self):
        for folder in ("profile/Default", "auth"):
            (self.state / folder).mkdir(parents=True)
            (self.state / folder / "data").write_text("x")
        # The store, its ledger and leftovers of _write_private's temporary files.
        for name in (FILE_NAME, LEDGER_NAME):
            for path in (name, f".{Path(name).stem}-abc123.tmp"):
                (self.state / path).write_text("{}")
        kept = sorted(["personal.sqlite3", "actions.sqlite3", "oauth.sqlite3", "host.log"])
        for name in kept:
            (self.state / name).write_text("kept")
        return kept

    def test_each_copy_has_its_own_key_and_sign_in_files(self):
        owner, alice = self.ctl.Copy(), self.ctl.Copy("alice")
        self.assertEqual(owner.sign_in_key, "/etc/sagasu/sagasu.signin-key")
        self.assertEqual(alice.sign_in_key, "/etc/sagasu/alice.signin-key")
        self.assertIn("sagasu", self.ctl.RESERVED)  # no copy can take the owner's key
        for copy, state in ((owner, "/var/lib/sagasu"), (alice, "/var/lib/sagasu-alice")):
            self.assertEqual(copy.state, state)
            parts = ("profile", "auth", FILE_NAME, LEDGER_NAME)
            self.assertEqual(copy.sign_in_state, [f"{state}/{part}" for part in parts])
        install = (KIT / "install.sh").read_text()
        self.assertIn("local key=/etc/sagasu/$name.signin-key", install)
        self.assertIn('ensure_signin_key sagasu sagasu "$SETTINGS"', install)
        # The owner's store sits next to its preferences, in the folder that
        # forget-sign-in clears.
        example = dotenv_values(KIT / "sagasu.env.example")
        self.assertEqual(str(Path(example["SAGASU_PREFERENCES_PATH"]).parent), owner.state)
        self.assertEqual(example["SAGASU_BROWSER_PROFILE"], owner.sign_in_state[0])
        self.assertEqual(example["SAGASU_AUTH_ARTIFACT_DIR"], owner.sign_in_state[1])

    def test_smu_email_pattern_matches_sagasus_own(self):
        for email in GOOD_EMAILS:
            self.assertTrue(self.ctl.SMU_EMAIL.fullmatch(email), email)
        for email in BAD_EMAILS + ("alice@smu.edu.sg\n",):
            self.assertFalse(self.ctl.SMU_EMAIL.fullmatch(email), email)
        for email in GOOD_EMAILS + BAD_EMAILS + ("a+b%c@socsc.smu.edu.sg",):
            self.assertEqual(bool(self.ctl.SMU_EMAIL.fullmatch(email)), bool(SMU_EMAIL_RE.fullmatch(email)))

    # Regression (fixed): sagasu-ctl's SMU_EMAIL (like sagasu.personal) allows + and % in the
    # name, but value()'s PLAIN pattern does not, so after `smu-account` or
    # `instance-add NAME EMAIL` writes such an address, `status` exits asking to
    # rewrite the SAGASU_SMU_ACCOUNT line "as plain" (which it already is).
    def test_an_account_the_kit_accepts_can_be_read_back(self):
        account = "alice+fbs@smu.edu.sg"
        self.assertTrue(self.ctl.SMU_EMAIL.fullmatch(account))
        try:
            read = self.ctl.value([f"{ACCOUNT}={account}"], ACCOUNT, "", "/etc/sagasu/alice.env")
        except SystemExit as refused:
            self.fail(f"status refuses the line it wrote: {refused.code}")
        self.assertEqual(read, account)

    def owner_main(self, sudo_user="owner"):
        ctl = self.ctl
        return patched(
            patch.object(ctl, "instance_add"),
            patch.object(ctl.os, "geteuid", return_value=0),
            patch.dict(os.environ, {"SUDO_USER": sudo_user}),
            patch.object(ctl, "read", return_value=[]),
            patch.object(ctl.subprocess, "run", side_effect=AssertionError("no commands")),
        )

    def test_instance_add_arguments(self):
        email = "alice.tan@smu.edu.sg"
        for rest, writes, account in (
            ([], "on", None), ([email], "on", email),
            (["--read-only"], "off", None), ([email, "--read-only"], "off", email),
        ):
            with self.subTest(rest=rest), self.owner_main() as (add, *_):
                self.ctl.main(["instance-add", "alice", *rest])
                add.assert_called_once_with("alice", writes=writes, account=account)
        for rest in ([], [email, "other.tan@smu.edu.sg"], ["--read-only", "--read-only"],
                     [email, "--read-only", "extra"]):
            with self.subTest(rest=rest), self.owner_main() as (add, *_):
                with self.assertRaises(SystemExit):
                    self.ctl.main(["instance-add", *(["alice", *rest] if rest else [])])
                add.assert_not_called()
        with self.owner_main(sudo_user="fbs-bob") as (add, *_), self.assertRaises(SystemExit):
            self.ctl.main(["instance-add", "alice", email])
        add.assert_not_called()

    def test_forget_sign_in_deletes_the_sign_in_and_starts_the_copy_again(self):
        for name, unit in (("alice", "sagasu@alice"), (None, "sagasu")):
            with self.subTest(copy=name):
                kept, seen = self.fill(), []

                def run(command, **options):
                    seen.append((command, options, (self.state / FILE_NAME).exists()))
                    return Mock(returncode=0)

                with patch.object(self.ctl.subprocess, "run", side_effect=run), quiet():
                    self.ctl.forget_sign_in(self.copy_in(name))
                stop, start = ["systemctl", "stop", unit], ["systemctl", "start", unit]
                # Stopped before anything is deleted, started after.
                self.assertEqual(seen, [(stop, {"check": True}, True), (start, {"check": True}, False)])
                self.assertEqual(sorted(p.name for p in self.state.iterdir()), kept)
                for left in kept:
                    (self.state / left).unlink()

    def test_the_copy_starts_again_even_if_a_delete_fails(self):
        self.fill()
        self.read_only_state()
        with patch.object(self.ctl.subprocess, "run") as run, quiet():
            with self.assertRaises(OSError):
                self.ctl.forget_sign_in(self.copy_in("alice"))
        commands = [c.args[0] for c in run.call_args_list]
        self.assertEqual(commands, [["systemctl", verb, "sagasu@alice"] for verb in ("stop", "start")])

    def test_sagasu_self_hands_forget_sign_in_to_sagasu_ctl(self):
        kit = load_kit("sagasu-self")
        self.assertEqual(kit.parse(["forget-sign-in"]), ["forget-sign-in"])
        self.assertIsNone(kit.parse(["forget-sign-in", "now"]))
        self.assertIn("forget-sign-in", kit.HELP)
        instance, real_isfile = "/etc/sagasu/instances/alice", os.path.isfile
        # As root (through sudo): sagasu-ctl does the work, for this copy only.
        run = Mock(return_value=Mock(returncode=0))
        with patched(
            patch.object(kit.os.path, "isfile", side_effect=lambda p: p == instance or real_isfile(p)),
            patch.object(sys, "argv", ["sagasu-self", "alice", "forget-sign-in"]),
            patch.object(kit.os, "geteuid", return_value=0),
            patch.dict(os.environ, {"SUDO_USER": "fbs-alice"}),
            patch.object(kit.os, "execv", side_effect=AssertionError("no exec")),
            patch.object(kit.subprocess, "run", run),
        ), self.assertRaises(SystemExit) as caught:
            kit.main()
        self.assertEqual(caught.exception.code, 0)
        run.assert_called_once_with(["/opt/sagasu/bin/sagasu-ctl", "--instance", "alice", "forget-sign-in"])

    def test_the_shell_scripts_parse(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not available")
        for name in ("install.sh", "sagasu-launch"):
            result = subprocess.run([bash, "-n", str(KIT / name)], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")


class LaunchCheckTests(TempCase):
    """The settings check that sagasu-launch runs before the service starts."""

    @classmethod
    def setUpClass(cls):
        text = (KIT / "sagasu-launch").read_text()
        start = text.index("-c '") + len("-c '")
        cls.snippet = text[start : text.index("'", start)]
        assert "dotenv_values" in cls.snippet and "sys.exit(4)" in cls.snippet
        probe = subprocess.run([sys.executable, "-I", "-c", "import dotenv"], capture_output=True)
        if probe.returncode:
            raise unittest.SkipTest("python-dotenv is not available to this interpreter")

    def check(self, text):
        env = self.root / "sagasu.env"
        env.write_text(text)
        result = subprocess.run(
            [sys.executable, "-I", "-c", self.snippet, str(env), str(self.state)],
            capture_output=True, text=True, cwd=self.root, timeout=60,
        )
        self.assertNotIn("Traceback", result.stderr)
        return result.returncode

    def test_a_password_in_the_settings_refuses_start(self):
        self.assertEqual(self.check("SMU_LOGIN_PASSWORD=fixture-password\n"), 3)
        key = make_key_file(self.root / "sign-in.key")
        self.assertEqual(self.check(f"SMU_LOGIN_PASSWORD=fixture-password\n{KEY_FILE}={key}\n"), 3)
        self.assertEqual(self.check("SMU_LOGIN_PASSWORD=\n"), 0)

    def test_a_key_outside_etc_sagasu_or_writable_by_others_refuses_start(self):
        key = make_key_file(self.root / "sign-in.key")
        writable = make_key_file(self.root / "writable.key")
        os.chmod(writable, 0o660)
        link = self.root / "linked.key"
        link.symlink_to(key)
        in_state = make_key_file(self.state / "sign-in.key")
        for path in (key, writable, link, in_state, self.root):
            self.assertEqual(self.check(f"{KEY_FILE}={path}\n"), 4, path)

    def test_no_key_or_a_missing_one_lets_it_start(self):
        self.assertEqual(self.check(""), 0)
        missing = f"/etc/sagasu/fixture-{uuid.uuid4().hex}.signin-key"
        for value in ("", f"{self.root}/missing.key", missing):
            self.assertEqual(self.check(f"{KEY_FILE}={value}\n"), 0, value)


if __name__ == "__main__":
    unittest.main()
