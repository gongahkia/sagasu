"""A person's SMU email and password, saved on their own copy of Sagasu.

Hosted path only (see SETUP.md, "Hosting for a group"): active only when the
remote listener has a sign-in key file. The local path never saves a password.

With them saved, the copy signs in to SMU by itself when the session lapses and
asks the person, through their agent, only to approve the sign-in in Microsoft
Authenticator. The person types them once, on this copy's sign-in page; they
never pass through an agent or a chat.

The file sits in the copy's private state directory, encrypted (AES-256-GCM)
with a key that lives outside it (root-owned, readable only by this copy's
account). The state directory can then be copied or inspected without exposing
a password. It does not protect against someone who controls the running
service or the server: the server's administrator can read it, as they can read
everything this copy holds.

A copy belongs to one SMU account: the one its administrator set
(SAGASU_SMU_ACCOUNT), or else the first one Microsoft accepted here. Another
account's email is refused, so nobody can be led into saving their password on
someone else's copy.

A ledger next to it (not secret) records sign-in attempts, so the limits on
phone requests and password submissions survive restarts, and holds a sign-in
pause until the person acts.

Nothing here logs, prints or returns a password. The email appears only masked.
"""

import base64
import contextvars
import json
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

from .observability import event
from .personal import SMU_EMAIL_RE

# Set by the job runner, only for a job that may sign in with these. The browser
# client reads it; nothing puts them in the process environment, which the
# browser would inherit.
SAVED_CREDENTIALS = contextvars.ContextVar("sagasu_saved_credentials", default=None)

FILE_NAME = "saved-sign-in.json"
LEDGER_NAME = "sign-in-ledger.json"
KEY_BYTES = 32
VERSION = 1
# Bound to this purpose: the same key could never decrypt anything else.
ASSOCIATED_DATA = b"sagasu saved SMU sign-in v1"
EMAIL_LIMIT = 254
PASSWORD_LIMIT = 256

# Limits, from chat and the sign-in page together. Each sign-in can send a
# request to the person's phone; each password typed counts toward Microsoft's
# lockout of their whole SMU account.
SIGN_INS_PER_HOUR = 4
PASSWORDS_PER_DAY = 5
HOUR, DAY = 3600, 86400
RECENT = 10
# Microsoft's own lock clears by itself. After this long a "blocked" hold may
# also be lifted by the person asking their agent; the limits still apply.
BLOCK_SECONDS = DAY


class SavedSignInError(RuntimeError):
    """The saved sign-in cannot be used or changed; the message is safe to show."""


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str = field(repr=False)

    def __repr__(self):
        return f"Credentials(email={mask(self.email)!r}, password=<hidden>)"

    __str__ = __repr__


class Permit:
    """Leave for one job to sign in once with these credentials.

    The browser client takes it before typing anything; a second sign-in in the
    same job is refused, so no loop can send more than one Authenticator request.
    """

    def __init__(
        self, credentials, *, saved=True, on_start=None, on_password=None, on_end=None
    ):
        self.credentials = credentials
        self.saved = saved  # from the store, rather than a form just filled in
        self._on_start = on_start
        self._on_password = on_password
        self._on_end = on_end
        self.used = False
        self.password_typed = False
        self.approval_requested = False  # Microsoft showed the phone step
        self.signed_in = False  # FBS opened after this sign-in began
        self.outcome = None

    @property
    def verified(self):
        """Microsoft itself accepted this password: it was typed, and FBS
        opened. A sign-in that only resumed an existing session checks nothing."""
        return self.signed_in and self.password_typed

    def take(self):
        if self.used:
            return False
        self.used = True
        if self._on_start is not None:
            self._on_start()
        return True

    def finish(self, outcome):
        """How the sign-in ended: "signed_in" or one of the failure reasons."""
        if self.outcome is not None:
            return
        self.outcome = outcome
        self.signed_in = outcome == "signed_in"
        if self._on_end is not None:
            self._on_end(self, outcome)

    def typing_password(self):
        """Called just before the password is typed: counted first, so a crash
        in between still counts it."""
        self.password_typed = True
        if self._on_password is not None:
            self._on_password()

    def __repr__(self):
        return f"Permit({self.credentials!r}, used={self.used})"


def mask(email):
    """a…n@smu.edu.sg: enough for a person to recognise their own account."""
    name, at, domain = (email or "").partition("@")
    if not at:
        return "(saved)"
    shown = name[0] + "…" + name[-1] if len(name) > 2 else name[:1] + "…"
    return f"{shown}@{domain}"


def valid_email(email):
    return (
        isinstance(email, str)
        and len(email) <= EMAIL_LIMIT
        and bool(SMU_EMAIL_RE.fullmatch(email))
    )


def valid_password(password):
    return (
        isinstance(password, str)
        and 1 <= len(password) <= PASSWORD_LIMIT
        and password.isprintable()
        and "\n" not in password
        and "\r" not in password
    )


def same_account(first, second):
    return (first or "").strip().casefold() == (second or "").strip().casefold()


def make_key_file(path):
    """Write a fresh random key. For tests and the install kit's own use."""
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(base64.b64encode(secrets.token_bytes(KEY_BYTES)) + b"\n")
    return path


def _write_private(path, data):
    """Atomically replace path with data, readable only by this account."""
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="." + path.stem + "-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class SavedSignIn:
    """The saved email and password of the one person this copy belongs to."""

    def __init__(self, state_dir, key_file, *, account=None, clock=time.time):
        self.path = Path(state_dir) / FILE_NAME
        self.ledger_path = Path(state_dir) / LEDGER_NAME
        self.key_file = Path(key_file) if key_file else None
        # Set by the administrator in the root-owned settings; else learnt.
        self.account = (account or "").strip() or None
        self.clock = clock
        # Fail closed: if the state folder cannot be written, a hold or a
        # refused password is still kept here, and no sign-in starts until a
        # write succeeds again.
        self._unwritable = False
        self._memory_hold = None
        self._refused_password = None

    # -- the key -----------------------------------------------------------------

    def _cipher(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise SavedSignInError(
                "Saving a sign-in needs the cryptography package on the server."
            ) from None
        if self.key_file is None:
            raise SavedSignInError("Saving a sign-in is not set up on this server.")
        # The launcher refuses to start with a key this account could rewrite.
        try:
            raw = self.key_file.read_bytes()
            key = base64.b64decode(raw.strip(), validate=True)
        except (OSError, ValueError):
            raise SavedSignInError(
                "The server's sign-in key is missing or unreadable."
            ) from None
        if len(key) != KEY_BYTES:
            raise SavedSignInError("The server's sign-in key is not valid.")
        return AESGCM(key)

    @property
    def available(self):
        """True when this copy can save and use a sign-in."""
        try:
            self._cipher()
        except SavedSignInError:
            return False
        return True

    # -- reading -----------------------------------------------------------------

    def _read(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            event("saved_sign_in.unreadable", level=logging.WARNING)
            return None
        try:
            stored = json.loads(text)
            if not isinstance(stored, dict) or stored.get("version") != VERSION:
                raise ValueError()
            nonce = base64.b64decode(stored["nonce"], validate=True)
            data = base64.b64decode(stored["data"], validate=True)
            plain = json.loads(
                self._cipher().decrypt(nonce, data, ASSOCIATED_DATA).decode("utf-8")
            )
            if not isinstance(plain, dict):
                raise ValueError()
            return plain
        except SavedSignInError:
            raise
        except Exception:
            # A wrong key, a damaged file or a format from elsewhere. Say so
            # once, without any of its contents, and treat it as not saved.
            event("saved_sign_in.undecryptable", level=logging.WARNING)
            return None

    def _plain(self):
        try:
            return self._read() or {}
        except SavedSignInError:
            return {}

    def owner(self, plain=None):
        """The SMU account this copy belongs to, if known.

        Set by the administrator, or learnt from the first sign-in Microsoft
        accepted here. Once learnt it stays, whatever is saved later; only
        erase() (the administrator's reset) clears it.
        """
        if self.account:
            return self.account
        plain = self._plain() if plain is None else plain
        owner = plain.get("owner")
        if valid_email(owner):
            return owner
        # Files written before "owner" was kept on its own.
        return plain.get("email") if plain.get("verified") else None

    def load(self):
        """The saved Credentials, or None when no usable sign-in is saved."""
        plain = self._plain()
        email, password = plain.get("email"), plain.get("password")
        if not (valid_email(email) and valid_password(password)):
            return None
        if password == self._refused_password:
            return None  # refused, and could not be deleted: never again
        owner = self.owner(plain)
        if owner and not same_account(email, owner):
            return None
        from .console import add_secret

        add_secret(password)
        return Credentials(email, password)

    def summary(self):
        """What the person may see: whether one is saved, and for whom."""
        try:
            self._cipher()
            plain = self._read() or {}
        except SavedSignInError:
            return {"available": False, "saved": False, "email": None, "owner": None}
        email = plain.get("email")
        owner = self.owner()
        return {
            "available": True,
            "saved": self.load() is not None,
            "verified": bool(plain.get("verified")),
            "email": mask(email) if valid_email(email) else None,
            # The account the form is locked to, shown in full to the person
            # on their own page so they can recognise it.
            "owner": owner,
        }

    # -- writing -----------------------------------------------------------------

    def _write(self, plain):
        cipher = self._cipher()
        nonce = secrets.token_bytes(12)
        data = cipher.encrypt(nonce, json.dumps(plain).encode("utf-8"), ASSOCIATED_DATA)
        try:
            _write_private(
                self.path,
                {
                    "version": VERSION,
                    "nonce": base64.b64encode(nonce).decode("ascii"),
                    "data": base64.b64encode(data).decode("ascii"),
                },
            )
        except OSError:
            raise SavedSignInError(
                "The sign-in could not be saved on the server."
            ) from None

    def check_account(self, email):
        """Refuse an email that is not this copy's own SMU account."""
        if not valid_email(email):
            raise SavedSignInError("Enter your SMU email address (name@smu.edu.sg).")
        owner = self.owner()
        if owner and not same_account(email, owner):
            raise SavedSignInError(
                f"This copy of Sagasu belongs to {mask(owner)}. Only that account "
                "can be saved here."
            )

    def save(self, email, password, *, verified=False):
        """Save them. verified: Microsoft accepted them just now, here."""
        email = email.strip() if isinstance(email, str) else email
        self.check_account(email)
        if not valid_password(password):
            raise SavedSignInError("That password cannot be saved.")
        previous = self._plain()
        verified = verified or (
            bool(previous.get("verified"))
            and same_account(previous.get("email"), email)
            and previous.get("password") == password
        )
        owner = self.owner(previous) or (email if verified else None)
        self._write(
            {"email": email, "password": password, "verified": verified, "owner": owner}
        )
        if password == self._refused_password:
            self._refused_password = None  # saved again on purpose
        event("saved_sign_in.saved", verified=verified)

    def mark_verified(self):
        plain = self._plain()
        if plain.get("password") and not plain.get("verified"):
            owner = self.owner(plain) or plain.get("email")
            self._write({**plain, "verified": True, "owner": owner})
            event("saved_sign_in.verified")

    def forget_password(self, *, refused=False):
        """Keep only the email and whose copy this is; never retry a password.

        refused: Microsoft refused it. If it cannot be deleted from disk, it is
        still never used again by this process.
        """
        plain = self._plain()
        email = plain.get("email")
        try:
            if valid_email(email):
                self._write(
                    {
                        "email": email,
                        "verified": bool(plain.get("verified")),
                        "owner": self.owner(plain),
                    }
                )
                event("saved_sign_in.password_forgotten")
            else:
                self.erase()
        except SavedSignInError:
            if refused and plain.get("password"):
                # Still on disk: this process never uses it again.
                self._refused_password = plain["password"]
            raise

    def erase(self):
        """Delete the file outright, owner and all (the administrator's reset)."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise SavedSignInError("The saved sign-in could not be removed.") from None
        event("saved_sign_in.forgotten")

    # -- the ledger ----------------------------------------------------------------

    def _ledger(self):
        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(
                not isinstance(data.get(name, []), list)
                for name in ("starts", "passwords", "recent")
            ):
                raise ValueError()
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            event("saved_sign_in.ledger_unreadable", level=logging.WARNING)
            # Unreadable: assume the limits are used up rather than unused.
            now = self.clock()
            data = {
                "starts": [now] * SIGN_INS_PER_HOUR,
                "passwords": [now] * PASSWORDS_PER_DAY,
            }
        now = self.clock()

        def recent(name, window):
            return [
                float(at)
                for at in data.get(name, [])
                if isinstance(at, (int, float)) and 0 <= now - at < window
            ]

        hold = data.get("hold")
        return {
            "starts": recent("starts", HOUR),
            "passwords": recent("passwords", DAY),
            "hold": hold if isinstance(hold, dict) and hold.get("reason") else None,
            "recent": [
                item for item in data.get("recent", []) if isinstance(item, dict)
            ][-RECENT:],
        }

    def _save_ledger(self, ledger):
        try:
            _write_private(self.ledger_path, ledger)
            self._unwritable = False
        except OSError:
            # Counts and holds that cannot be kept must not read as "none".
            self._unwritable = True
            event("saved_sign_in.ledger_unwritable", level=logging.WARNING)

    def record(self, kind):
        """kind: "starts" (a sign-in began) or "passwords" (one was typed)."""
        ledger = self._ledger()
        ledger[kind].append(self.clock())
        self._save_ledger(ledger)

    def note(self, by, how, outcome):
        """Remember how a sign-in ended, for the person's own page."""
        ledger = self._ledger()
        ledger["recent"] = (
            ledger["recent"]
            + [{"at": int(self.clock()), "by": by, "how": how, "outcome": outcome}]
        )[-RECENT:]
        self._save_ledger(ledger)

    def refusal(self):
        """Why a sign-in may not start now, or None. (reason, minutes to wait)"""
        if self._unwritable:
            # Nothing could be recorded: only the person's own page goes on.
            return "sign_in_needs_page", None
        ledger = self._ledger()
        hold = ledger["hold"] or self._memory_hold
        if hold is not None:
            return hold["reason"], None
        now = self.clock()
        waits = []
        if len(ledger["starts"]) >= SIGN_INS_PER_HOUR:
            waits.append(_minutes(ledger["starts"][0] + HOUR - now))
        if len(ledger["passwords"]) >= PASSWORDS_PER_DAY:
            waits.append(_minutes(ledger["passwords"][0] + DAY - now))
        # Both used up: the longer wait is the true one.
        return ("sign_in_attempts_exceeded", max(waits)) if waits else None

    def hold(self, reason, *, page_only):
        """Stop signing in for remote requests until the person acts.

        page_only: only the person's own sign-in page (or a successful sign-in)
        lifts it. Otherwise they may also ask their agent to try again.
        """
        hold = {"reason": reason, "page_only": page_only, "at": self.clock()}
        ledger = self._ledger()
        ledger["hold"] = hold
        self._save_ledger(ledger)
        # Kept in memory only if the disk refused it, so it still holds.
        self._memory_hold = hold if self._unwritable else None

    def held(self):
        return self._ledger()["hold"] or self._memory_hold

    def lift(self, *, by_page, asked_at=None):
        """Lift the hold. From chat, only one that allows it, set before the ask."""
        ledger = self._ledger()
        hold = ledger["hold"] or self._memory_hold
        if hold is None:
            return True
        page_only = hold.get("page_only") and not (
            hold.get("reason") == "sign_in_blocked"
            and self.clock() - float(hold.get("at", 0)) >= BLOCK_SECONDS
        )
        if not by_page and (
            page_only or (asked_at is not None and float(hold.get("at", 0)) >= asked_at)
        ):
            return False
        self._memory_hold = None
        ledger["hold"] = None
        self._save_ledger(ledger)
        return True

    def recent(self):
        return self._ledger()["recent"]

    def forget_ledger(self):
        self.ledger_path.unlink(missing_ok=True)


def _minutes(seconds):
    return max(1, min(1440, int(seconds // 60) + 1))
