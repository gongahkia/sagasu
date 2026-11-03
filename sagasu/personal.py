"""Private, transactional contacts and search preferences for local agents.

Connections are short-lived; no database handles or credentials enter operation state.
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
    def from_environment(cls):
        from .config import DEFAULT_PREFERENCES

        return cls(os.getenv("SAGASU_PREFERENCES_PATH") or DEFAULT_PREFERENCES)

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
            # Additive table keeps existing readers/rollback compatible with schema 1.
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS booking_templates (scope TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(scope,id), UNIQUE(scope,name))"
                )
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

    def templates(self):
        with self._db() as db:
            return [
                {"id": key, "name": name, **json.loads(payload)}
                for key, name, payload in db.execute(
                    "SELECT id,name,payload FROM booking_templates WHERE scope=? ORDER BY name",
                    (self.scope,),
                )
            ]

    def save_template(self, name, room, start_time, end_time, purpose, co_booker):
        from .fbs_client import SearchConfig

        if (
            not all(
                isinstance(v, str) and v.strip()
                for v in (name, room, purpose, co_booker)
            )
            or len(name) > 80
            or len(room) > 200
            or len(purpose) > 200
        ):
            raise PersonalDataError(
                "Provide a template name, room, purpose, and co-booker within their length limits."
            )
        try:
            SearchConfig("01-Jan-2099", start_time, end_time)
        except (ValueError, TypeError):
            raise PersonalDataError(
                "Template times must describe a same-day interval on half-hour boundaries."
            ) from None
        if not SMU_EMAIL_RE.fullmatch(co_booker.strip()):
            raise PersonalDataError(
                "The template needs a valid full SMU co-booker email."
            )
        payload = {
            "room": room.strip(),
            "start_time": start_time,
            "end_time": end_time,
            "purpose": purpose.strip(),
            "co_booker": co_booker.strip().casefold(),
        }
        with self._db(write=True) as db:
            existing = db.execute(
                "SELECT id FROM booking_templates WHERE scope=? AND name=?",
                (self.scope, name.strip()),
            ).fetchone()
            if (
                not existing
                and db.execute(
                    "SELECT COUNT(*) FROM booking_templates WHERE scope=?",
                    (self.scope,),
                ).fetchone()[0]
                >= 100
            ):
                raise PersonalDataError("Saved templates are limited to 100.")
            key = existing[0] if existing else secrets.token_urlsafe(12)
            db.execute(
                "INSERT INTO booking_templates VALUES(?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET name=excluded.name,payload=excluded.payload",
                (self.scope, key, name.strip(), json.dumps(payload)),
            )
        return {"id": key, "name": name.strip(), **payload}

    def delete_template(self, template_id):
        with self._db(write=True) as db:
            if not db.execute(
                "DELETE FROM booking_templates WHERE scope=? AND id=?",
                (self.scope, template_id),
            ).rowcount:
                raise PersonalDataError("That booking template no longer exists.")
        return {"deleted": True, "id": template_id}

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
            from .fbs_client import search_config_from_preferences

            try:
                search_config_from_preferences(current)
            except (ValueError, TypeError):
                raise PersonalDataError(
                    "Those preferences do not produce a valid same-day search window. Change the start or duration."
                ) from None
            db.execute(
                "INSERT INTO profiles(scope,search,migrated) VALUES(?,?,1) ON CONFLICT(scope) DO UPDATE SET search=excluded.search,migrated=1",
                (self.scope, json.dumps(current)),
            )
        return current
