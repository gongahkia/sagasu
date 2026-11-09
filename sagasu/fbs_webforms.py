"""Data-only parsing of FBS's observed ASP.NET search forms and calendar.

No JavaScript is executed. Dropdown IDs come from the current reply, and every
post uses newly returned form state. Only the known read controls are accepted.
"""

import json
import re
from datetime import datetime

from .fbs_calendar import dated_event_slot
from .fbs_client import (
    FBS_TIMEZONE,
    LayoutChanged,
    RoomAvailability,
    SearchResult,
    add_free_windows,
    parse_event_title,
)
from .fbs_http import HTTPReadUnavailable, _Document, _Node, _text

FILTERS = (
    ("DropMultiBuildingList_c1", "buildings"),
    ("DropMultiFloorList_c1", "floors"),
    ("DropMultiFacilityTypeList_c1", "facility_types"),
    ("DropMultiEquipmentList_c1", "equipment"),
)
READ_TARGETS = {
    "TimeFrom_c1$ctl04",
    "TimeTo_c1$ctl04",
    "DropCapacity_c1",
    "BtnDpcNext$BtnDpcNext_bt",
    "BtnDpcPrev$BtnDpcPrev_bt",
    "CheckAvailability$CheckAvailability_bt",
    *(control + "_textValue" for control, _ in FILTERS),
}


class SearchForm:
    def __init__(self, html):
        self.document = _Document(html)
        forms = [n for n in self.document.root.nodes() if n.tag == "form"]
        if len(forms) != 1:
            raise HTTPReadUnavailable("unreadable_form")
        self.form = forms[0]

    def fields(self):
        result = {}
        for node in self.form.nodes():
            name = node.attrs.get("name")
            if not name or "disabled" in node.attrs:
                continue
            if node.tag == "input":
                kind = node.attrs.get("type", "text").lower()
                if kind in {"button", "submit", "image", "reset", "file"}:
                    continue
                if kind in {"checkbox", "radio"} and "checked" not in node.attrs:
                    continue
                value = node.attrs.get(
                    "value", "on" if kind in {"checkbox", "radio"} else ""
                )
            elif node.tag == "select":
                options = [
                    n
                    for n in node.nodes()
                    if n.tag == "option" and "disabled" not in n.attrs
                ]
                selected = [n for n in options if "selected" in n.attrs]
                if not options:
                    continue
                value = (selected or options)[0].attrs.get(
                    "value", _text((selected or options)[0])
                )
            elif node.tag == "textarea":
                value = node.text()
            else:
                continue
            if name in result:
                raise HTTPReadUnavailable("ambiguous_form_field")
            result[name] = value
        return result

    def payload(self, target, changes=None):
        if target not in READ_TARGETS:
            raise HTTPReadUnavailable("unsupported_postback")
        result = self.fields()
        # FBS returns a blank PageUniqueId after the first full postback; the
        # updated ViewStateKey still identifies its current server-side state.
        if "__PageUniqueId__" not in result or not result.get("__ViewStateKey__"):
            raise HTTPReadUnavailable("missing_page_state")
        # Full form responses carry the fresh state and model. AJAX flags would
        # instead require applying partial HTML/JavaScript delta messages.
        for key in (
            "ctl00",
            "__ASYNCPOST",
            "__MicrosoftAjax",
            "__CALLBACKID",
            "__CALLBACKPARAM",
        ):
            result.pop(key, None)
        result.update(changes or {})
        result.update(__EVENTTARGET=target, __EVENTARGUMENT="")
        return result

    def multi_values(self, control, labels):
        root = self.document.by_id(control + "::ddl::")
        choices = {}
        for label in root.nodes():
            if label.tag != "label":
                continue
            inputs = [
                n
                for n in label.nodes()
                if n.tag == "input" and n.attrs.get("type") == "checkbox"
            ]
            name = _text(label)
            if (
                len(inputs) != 1
                or not re.fullmatch(r"\d+", inputs[0].attrs.get("id", ""))
                or name in choices
            ):
                raise HTTPReadUnavailable("ambiguous_filter_options")
            choices[name] = inputs[0].attrs["id"]
        normalized = [" ".join(label.split()) for label in labels]
        if len(set(normalized)) != len(normalized) or any(
            label not in choices for label in normalized
        ):
            raise HTTPReadUnavailable("unsupported_filter_option")
        # The website emits selections in the current dropdown's order.
        return ",".join(value for name, value in choices.items() if name in normalized)

    def select_value(self, identifier, value):
        node = self.document.by_id(identifier)
        if (
            node.tag != "select"
            or "disabled" in node.attrs
            or not any(
                n.tag == "option"
                and n.attrs.get("value", _text(n)) == value
                and "disabled" not in n.attrs
                for n in node.nodes()
            )
        ):
            raise HTTPReadUnavailable("unsupported_select_option")
        return value

    def rooms(self):
        table = self.document.by_id("GridResults_gv")
        for node in table.nodes():
            if "Page$" in node.attrs.get("href", "") or "Page$" in node.attrs.get(
                "onclick", ""
            ):
                raise HTTPReadUnavailable("incomplete_facility_listing")
        result = {}
        for row in table.nodes():
            if row.tag != "tr":
                continue
            cells = [n for n in row.children if isinstance(n, _Node) and n.tag == "td"]
            if len(cells) < 2:
                continue
            name = _text(cells[1])
            boxes = [
                n
                for n in row.nodes()
                if n.tag == "input" and n.attrs.get("type") == "checkbox"
            ]
            if (
                not name
                or name in result
                or len(boxes) != 1
                or "disabled" in boxes[0].attrs
            ):
                raise HTTPReadUnavailable("unreadable_facility_row")
            field = boxes[0].attrs.get("name", "")
            if not re.fullmatch(r"GridResults\$gv\$ctl\d+\$checkMultiple", field):
                raise HTTPReadUnavailable("unreadable_facility_selection")
            result[name] = (field, boxes[0].attrs.get("value", "on"))
        return result


def _assignment(source, key):
    matches = list(re.finditer(r"(?m)^\s*v\." + re.escape(key) + r"\s*=\s*", source))
    if len(matches) != 1:
        raise HTTPReadUnavailable("unreadable_calendar_model")
    try:
        tail = source[matches[0].end() :]
        value, end = json.JSONDecoder().raw_decode(tail)
        if not tail[end:].lstrip().startswith(";"):
            raise ValueError()
        return value
    except (ValueError, RecursionError):
        raise HTTPReadUnavailable("unreadable_calendar_model") from None


def _local_time(value):
    try:
        result = datetime.fromisoformat(value)
        return (
            result.astimezone(FBS_TIMEZONE).replace(tzinfo=None)
            if result.tzinfo
            else result
        )
    except (ValueError, TypeError):
        raise HTTPReadUnavailable("unreadable_calendar_time") from None


def parse_calendar_document(html, config, expected_rooms):
    document = _Document(html)
    scripts = [
        "".join(c for c in n.children if isinstance(c, str))
        for n in document.root.nodes()
        if n.tag == "script"
    ]
    initializers = [
        source
        for source in scripts
        if re.search(r"function\s+dps_Init\s*\(\s*\)", source)
        and re.search(
            r"var\s+v\s*=\s*new\s+DayPilot\.Scheduler\(\s*['\"]dps['\"]\s*\)", source
        )
    ]
    if len(initializers) != 1:
        raise HTTPReadUnavailable("unreadable_calendar_model")
    source = initializers[0]
    if (
        _assignment(source, "dynamicLoading") is not False
        or _assignment(source, "dynamicEventRendering") != "Disabled"
        or type(_assignment(source, "days")) is not int
        or _assignment(source, "days") != 1
    ):
        raise HTTPReadUnavailable("incomplete_calendar_model")
    try:
        if (
            _local_time(_assignment(source, "startDate")).date()
            != datetime.strptime(config.date, "%d-%b-%Y").date()
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPReadUnavailable("calendar_date_mismatch") from None
    timeline = _assignment(source, "timeline")
    if not isinstance(timeline, list) or not timeline or len(timeline) > 3000:
        raise HTTPReadUnavailable("incomplete_calendar_timeline")
    intervals = []
    for cell in timeline:
        if not isinstance(cell, dict):
            raise HTTPReadUnavailable("incomplete_calendar_timeline")
        start, end = _local_time(cell.get("start")), _local_time(cell.get("end"))
        if end <= start:
            raise HTTPReadUnavailable("incomplete_calendar_timeline")
        intervals.append((start, end))
    covered = datetime.strptime(config.date + " " + config.start_time, "%d-%b-%Y %H:%M")
    requested_end = datetime.strptime(
        config.date + " " + config.end_time, "%d-%b-%Y %H:%M"
    )
    for start, end in sorted(intervals):
        if end <= covered:
            continue
        if start > covered:
            break
        covered = end
        if covered >= requested_end:
            break
    if covered < requested_end:
        raise HTTPReadUnavailable("incomplete_calendar_timeline")
    resources, events = _assignment(source, "resources"), _assignment(
        source, "events.list"
    )
    if not isinstance(resources, list) or not isinstance(events, list):
        raise HTTPReadUnavailable("unreadable_calendar_model")
    leaves = {}
    count = 0

    def visit(rows, depth=0):
        nonlocal count
        if depth > 20:
            raise HTTPReadUnavailable("calendar_resource_limit")
        for row in rows:
            count += 1
            if count > 5000 or not isinstance(row, dict):
                raise HTTPReadUnavailable("unreadable_calendar_resources")
            children = row.get("children", [])
            if not isinstance(children, list):
                raise HTTPReadUnavailable("unreadable_calendar_resources")
            if children:
                visit(children, depth + 1)
            else:
                key, name = row.get("id"), row.get("name")
                if (
                    type(key) not in {str, int}
                    or not isinstance(name, str)
                    or not name
                    or key in leaves
                    or name in leaves.values()
                    or row.get("isParent")
                ):
                    raise HTTPReadUnavailable("unreadable_calendar_resources")
                leaves[key] = name

    visit(resources)
    if set(leaves.values()) != set(expected_rooms) or len(leaves) != len(
        expected_rooms
    ):
        raise HTTPReadUnavailable("calendar_resource_mismatch")
    blocked = {name: [] for name in expected_rooms}
    identities = set()
    for item in events:
        if (
            not isinstance(item, dict)
            or type(item.get("resource")) not in {str, int}
            or item["resource"] not in leaves
        ):
            raise HTTPReadUnavailable("unreadable_calendar_event")
        # DayPilot uses event.text for the title when no explicit toolTip is
        # supplied (notably FBS's closed-hours/horizon events).
        title = item.get("toolTip")
        if title is None:
            title = item.get("text")
        slot = parse_event_title(title) if isinstance(title, str) else None
        if slot is None:
            raise HTTPReadUnavailable("unknown_calendar_event")
        identity = tuple(
            str(item.get(key)) for key in ("id", "resource", "start", "end")
        )
        if identity in identities:
            raise HTTPReadUnavailable("duplicate_calendar_event")
        identities.add(identity)
        try:
            dated = dated_event_slot(
                {"start_date": item.get("start"), "end_date": item.get("end")},
                slot.status,
                config,
            )
        except LayoutChanged:
            raise HTTPReadUnavailable("unreadable_calendar_event") from None
        if dated:
            blocked[leaves[item["resource"]]].append(dated)
    rooms = [
        RoomAvailability(
            name, add_free_windows(blocked[name], config.start_time, config.end_time)
        )
        for name in expected_rooms
    ]
    return SearchResult(config, rooms, datetime.now().astimezone().isoformat())
