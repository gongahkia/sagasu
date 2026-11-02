"""Private, transactional personal data shared by Telegram and MCP.

Connections are short-lived; no database handles or credentials enter bot state.
"""

import json
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .personal_options import SCRAPE_PARAM_CHOICES

SMU_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+-]+@(smu\.edu\.sg|(scis|sis|law|business|accountancy|economics|socsc)\.smu\.edu\.sg)$",
    re.I,
)
FILTERS = {
    "buildings": "building",
    "floors": "floor",
    "facility_types": "facility_type",
    "equipment": "equipment",
}
SEARCH_KEYS = {*FILTERS, "date_raw", "start_time", "duration_hrs", "room_capacity"}


class PersonalDataError(ValueError):
    """Sanitized personal-store validation or persistence failure."""


def validate_preferences(patch):
    if not isinstance(patch, dict) or set(patch) - SEARCH_KEYS:
        raise PersonalDataError(
            "Unknown search preference. Use search_options for supported fields and choices."
        )
    result = {}
    for key, value in patch.items():
        if value is None:
            result[key] = None  # Explicit null removes an override.
        elif key in FILTERS:
            if not isinstance(value, list) or any(
                not isinstance(item, str)
                or item not in SCRAPE_PARAM_CHOICES[FILTERS[key]]
                for item in value
            ):
                raise PersonalDataError("Choose filter values from search_options.")
            result[key] = list(dict.fromkeys(value))
        elif key == "date_raw":
            try:
                result[key] = datetime.strptime(value.title(), "%d %B %Y").strftime(
                    "%d %B %Y"
                )
            except (ValueError, TypeError, AttributeError):
                raise PersonalDataError(
                    "date_raw must be a date such as 03 November 2026; null restores rolling dates."
                ) from None
        elif key == "start_time":
            if value not in SCRAPE_PARAM_CHOICES["start_time"]:
                raise PersonalDataError(
                    "Start time must use HH:MM on half-hour boundaries; null restores rolling starts."
                )
            result[key] = value
        elif key == "duration_hrs":
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or value not in SCRAPE_PARAM_CHOICES["duration"]
            ):
                raise PersonalDataError(
                    "Choose a supported duration from search_options."
                )
            result[key] = value
        elif key == "room_capacity":
            if not isinstance(value, str) or value not in {
                choice for _, choice in SCRAPE_PARAM_CHOICES["capacity"]
            }:
                raise PersonalDataError(
                    "Choose a capacity code from search_options; null means any capacity."
                )
            result[key] = value
    return result


def valid_contact(name, email):
    if (
        not isinstance(name, str)
        or not 1 <= len(name.strip()) <= 40
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise PersonalDataError("Use a single-line co-booker name of 1–40 characters.")
    if not isinstance(email, str) or not SMU_EMAIL_RE.fullmatch(email.strip()):
        raise PersonalDataError(
            "The co-booker must have a valid full SMU email address."
        )
    return name.strip(), email.strip().casefold()


class PersonalStore:
    def __init__(self, path, scope="local"):
        self.path, self.scope = Path(path).expanduser().resolve(), scope

    @classmethod
    def from_environment(cls, *, persistence_path=None, owner_id=None):
        from .config import DEFAULT_PERSISTENCE

        state = Path(
            persistence_path or os.getenv("SAGASU_STATE_PATH") or DEFAULT_PERSISTENCE
        )
        path = os.getenv("SAGASU_PREFERENCES_PATH") or state.with_name(
            "personal.sqlite3"
        )
        owner = owner_id if owner_id is not None else os.getenv("TELEGRAM_OWNER_ID", "")
        return cls(path, f"telegram:{owner}" if owner else "local")

    def for_user(self, user_id):
        if (
            isinstance(user_id, bool)
            or not str(user_id).isascii()
            or not str(user_id).isdigit()
            or int(user_id) <= 0
        ):
            raise PersonalDataError(
                "A positive Telegram user ID is required for personal data."
            )
        return PersonalStore(self.path, f"telegram:{user_id}")

    @contextmanager
    def _db(self, *, write=False):
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            self.path.chmod(0o600)
            connection = sqlite3.connect(self.path, timeout=2)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise PersonalDataError(
                    "Personal data uses an unsupported schema version; no data was changed."
                )
            if version == 0:
                with connection:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS profiles (scope TEXT PRIMARY KEY, search TEXT NOT NULL DEFAULT '{}', migrated INTEGER NOT NULL DEFAULT 0)"
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS contacts (scope TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL, PRIMARY KEY(scope,id), UNIQUE(scope,email))"
                    )
                    connection.execute("PRAGMA user_version=1")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except PersonalDataError:
            if connection:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            if connection:
                connection.rollback()
            raise PersonalDataError(
                "Personal data could not be read or saved. Check the private preferences file and permissions; no fallback reset was performed."
            ) from None
        finally:
            if connection:
                connection.close()

    def contacts(self):
        with self._db() as db:
            return {
                key: {"name": name, "email": email}
                for key, name, email in db.execute(
                    "SELECT id,name,email FROM contacts WHERE scope=? ORDER BY rowid",
                    (self.scope,),
                )
            }

    def is_migrated(self):
        with self._db() as db:
            row = db.execute(
                "SELECT migrated FROM profiles WHERE scope=?", (self.scope,)
            ).fetchone()
            return bool(row and row[0])

    def save_contact(self, name, email, *, contact_id=None):
        name, email = valid_contact(name, email)
        with self._db(write=True) as db:
            if (
                contact_id is not None
                and not db.execute(
                    "SELECT 1 FROM contacts WHERE scope=? AND id=?",
                    (self.scope, contact_id),
                ).fetchone()
            ):
                raise PersonalDataError("That saved co-booker no longer exists.")
            duplicate = db.execute(
                "SELECT id FROM contacts WHERE scope=? AND email=?", (self.scope, email)
            ).fetchone()
            if duplicate and contact_id is not None and duplicate[0] != contact_id:
                raise PersonalDataError(
                    "That email already belongs to another saved co-booker."
                )
            key = contact_id or (
                duplicate[0] if duplicate else secrets.token_urlsafe(6)
            )
            if (
                not duplicate
                and not contact_id
                and db.execute(
                    "SELECT COUNT(*) FROM contacts WHERE scope=?", (self.scope,)
                ).fetchone()[0]
                >= 200
            ):
                raise PersonalDataError("Saved co-bookers are limited to 200 per user.")
            db.execute(
                "INSERT INTO contacts(scope,id,name,email) VALUES(?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET name=excluded.name,email=excluded.email",
                (self.scope, key, name, email),
            )
            db.execute(
                "INSERT INTO profiles(scope,migrated) VALUES(?,1) ON CONFLICT(scope) DO UPDATE SET migrated=1",
                (self.scope,),
            )
        return {"id": key, "name": name, "email": email}

    def delete_contact(self, contact_id):
        with self._db(write=True) as db:
            if not db.execute(
                "DELETE FROM contacts WHERE scope=? AND id=?", (self.scope, contact_id)
            ).rowcount:
                raise PersonalDataError("That saved co-booker no longer exists.")
        return {"deleted": True, "id": contact_id}

    def resolve_contact(self, value):
        saved = self.contacts()
        if value in saved:
            return saved[value]["email"]
        matches = [
            person["email"]
            for person in saved.values()
            if value.casefold()
            in {person["name"].casefold(), person["email"].casefold()}
        ]
        if len(matches) != 1:
            raise PersonalDataError(
                "Saved co-booker is missing or ambiguous; list contacts and use its exact ID."
            )
        return matches[0]

    def preferences(self):
        with self._db() as db:
            row = db.execute(
                "SELECT search FROM profiles WHERE scope=?", (self.scope,)
            ).fetchone()
            value = json.loads(row[0]) if row else {}
            return {
                key: item
                for key, item in validate_preferences(value).items()
                if item is not None
            }

    def update_preferences(self, patch, *, reset=False):
        patch = validate_preferences(patch)
        with self._db(write=True) as db:
            row = db.execute(
                "SELECT search FROM profiles WHERE scope=?", (self.scope,)
            ).fetchone()
            current = (
                {} if reset or not row else validate_preferences(json.loads(row[0]))
            )
            for key, value in patch.items():
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            from .fbs_client import search_config_from_bot

            try:
                search_config_from_bot(current)
            except (ValueError, TypeError):
                raise PersonalDataError(
                    "Those preferences do not produce a valid same-day search window. Change the start or duration."
                ) from None
            db.execute(
                "INSERT INTO profiles(scope,search,migrated) VALUES(?,?,1) ON CONFLICT(scope) DO UPDATE SET search=excluded.search,migrated=1",
                (self.scope, json.dumps(current)),
            )
        return current

    def import_legacy(self, user_data):
        """Import plain, validated fields once; canonical edits/deletions always win."""
        with self._db(write=True) as db:
            row = db.execute(
                "SELECT migrated FROM profiles WHERE scope=?", (self.scope,)
            ).fetchone()
            if row and row[0]:
                return
            search = {}
            legacy_search = user_data.get("scrape_config") or {}
            for key, value in (
                legacy_search.items() if isinstance(legacy_search, dict) else ()
            ):
                if key in SEARCH_KEYS:
                    try:
                        search.update(validate_preferences({key: value}))
                    except PersonalDataError:
                        pass
            search = {key: value for key, value in search.items() if value is not None}
            contacts = user_data.get("saved_cobookers") or {}
            for key, person in (
                list(contacts.items())[:200] if isinstance(contacts, dict) else ()
            ):
                try:
                    name, email = valid_contact(person.get("name"), person.get("email"))
                    if not isinstance(key, str) or not re.fullmatch(
                        r"[A-Za-z0-9_-]{1,16}", key
                    ):
                        key = secrets.token_urlsafe(6)
                    db.execute(
                        "INSERT OR IGNORE INTO contacts(scope,id,name,email) VALUES(?,?,?,?)",
                        (self.scope, key, name, email),
                    )
                except (PersonalDataError, AttributeError):
                    continue
            db.execute(
                "INSERT INTO profiles(scope,search,migrated) VALUES(?,?,1) ON CONFLICT(scope) DO UPDATE SET migrated=1",
                (self.scope, json.dumps(search)),
            )


async def migrate_telegram_state(store, path, *, owner_id):
    """Read only the configured, trusted local Telegram pickle; never an MCP path."""
    if not Path(path).is_file() or not owner_id:
        return
    profile = store.for_user(owner_id)
    if profile.is_migrated():
        return
    from telegram.ext import PicklePersistence

    try:
        data = await PicklePersistence(filepath=str(path)).get_user_data()
    except Exception:
        raise PersonalDataError(
            "Existing Telegram state could not be migrated. Check the configured private state file before starting MCP."
        ) from None
    profile.import_legacy(data.get(int(owner_id), {}))


def profile_for(context):
    profile = getattr(context, "personal_profile", None)
    return profile if isinstance(profile, PersonalStore) else None
