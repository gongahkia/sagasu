"""Validated room preferences shared by availability and read-only room plans."""

import copy
import math

DEFAULTS = {
    "preferred_rooms": [],
    "avoided_rooms": [],
    "time_preference": "earliest",
    "weights": {
        "room_preference": 2.0,
        "time_preference": 1.0,
        "date_preference": 1.0,
        "single_room": 2.0,
    },
}


def merge_preferences(current=None, patch=None):
    from .personal import PersonalDataError

    result = copy.deepcopy(DEFAULTS)
    for values in (
        current if current is not None else {},
        patch if patch is not None else {},
    ):
        if not isinstance(values, dict) or set(values) - set(DEFAULTS):
            raise PersonalDataError("Unknown room preference field.")
        for key, value in values.items():
            if value is None:
                result[key] = copy.deepcopy(DEFAULTS[key])
            elif key in {"preferred_rooms", "avoided_rooms"}:
                maximum = 20 if key == "preferred_rooms" else 100
                if (
                    not isinstance(value, list)
                    or len(value) > maximum
                    or any(
                        not isinstance(v, str)
                        or not v.strip()
                        or len(v) > 200
                        or any(ord(c) < 32 or ord(c) == 127 for c in v)
                        for v in value
                    )
                ):
                    raise PersonalDataError(
                        "Use up to 20 preferred or 100 avoided room names, each 1–200 characters."
                    )
                normalized = [v.strip() for v in value]
                if len({v.casefold() for v in normalized}) != len(normalized):
                    raise PersonalDataError("Room preference names must be unique.")
                result[key] = normalized
            elif key == "time_preference":
                if not isinstance(value, str) or value not in {"earliest", "latest"}:
                    raise PersonalDataError(
                        "Choose earliest or latest time preference."
                    )
                result[key] = value
            elif key == "weights":
                if not isinstance(value, dict) or set(value) - set(DEFAULTS["weights"]):
                    raise PersonalDataError(
                        "Choose supported room-plan scoring weights."
                    )
                for name, weight in value.items():
                    if (
                        type(weight) not in {int, float}
                        or not math.isfinite(weight)
                        or not 0 <= weight <= 1000
                    ):
                        raise PersonalDataError(
                            "Scoring weights must be finite numbers from 0 to 1000."
                        )
                    result[key][name] = float(weight)
    if {v.casefold() for v in result["preferred_rooms"]} & {
        v.casefold() for v in result["avoided_rooms"]
    }:
        raise PersonalDataError("A room cannot be both preferred and avoided.")
    return result
