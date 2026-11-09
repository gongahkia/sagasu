"""Synthetic FBS protocol fixtures, with no private session or account data."""

import json
import unittest

from sagasu.fbs_client import SearchConfig
from sagasu.fbs_http import HTTPReadUnavailable
from sagasu.fbs_webforms import SearchForm, parse_calendar_document

CONFIG = SearchConfig("10-Nov-2099", "10:00", "12:00")


def calendar_html(*, resources=None, events=None, overrides=None):
    values = {
        "dynamicLoading": False,
        "dynamicEventRendering": "Disabled",
        "days": 1,
        "startDate": "2099-11-10T00:00:00",
        "timeline": [{"start": "2099-11-10T00:00:00", "end": "2099-11-11T00:00:00"}],
        "resources": (
            resources if resources is not None else [{"id": "R1", "name": "Room A"}]
        ),
        "events.list": events if events is not None else [],
    }
    values.update(overrides or {})
    assignments = "\n".join(
        "v." + key + " = " + json.dumps(value) + ";" for key, value in values.items()
    )
    return (
        '<html><body><script>function dps_Init() {\nvar v = new DayPilot.Scheduler("dps");\n'
        + assignments
        + "\nv.init();\n}</script></body></html>"
    )


def event(
    *,
    start="2099-11-10T10:30:00",
    end="2099-11-10T11:00:00",
    text="Booking Time: 10:30-11:00",
    tip=True,
):
    result = {"id": "EVENT-FIXTURE", "resource": "R1", "start": start, "end": end}
    result["toolTip" if tip else "text"] = text
    return result


class CalendarModelTests(unittest.TestCase):
    def test_a_timeline_gap_cannot_be_reported_as_free(self):
        timeline = [
            {"start": "2099-11-10T10:00:00", "end": "2099-11-10T10:30:00"},
            {"start": "2099-11-10T11:00:00", "end": "2099-11-10T12:00:00"},
        ]
        with self.assertRaises(HTTPReadUnavailable):
            parse_calendar_document(
                calendar_html(overrides={"timeline": timeline}), CONFIG, ["Room A"]
            )

    def test_complete_model_produces_exact_free_complement_without_details(self):
        result = parse_calendar_document(
            calendar_html(events=[event()]), CONFIG, ["Room A"]
        )
        self.assertEqual(
            [(s.start, s.end, s.status) for s in result.rooms[0].timeslots],
            [
                ("10:00", "10:30", "free"),
                ("10:30", "11:00", "booked"),
                ("11:00", "12:00", "free"),
            ],
        )
        self.assertTrue(all(s.details is None for s in result.rooms[0].timeslots))

    def test_closed_hours_and_horizon_use_daypilot_text_default_and_full_dates(self):
        blocked = event(
            start="2099-11-10T00:00:00",
            end="2099-11-11T00:00:00",
            text="(00:00-00:00) (not within booking horizon)",
            tip=False,
        )
        result = parse_calendar_document(
            calendar_html(events=[blocked]), CONFIG, ["Room A"]
        )
        self.assertFalse(result.bookable_rooms)
        self.assertTrue(result.booking_horizon["all_checked_rooms_affected"])
        self.assertEqual(result.rooms[0].timeslots[0].end, "12:00")

    def test_hierarchical_resources_require_exact_complete_discovery(self):
        resources = [
            {
                "id": "GROUP",
                "name": "Building",
                "children": [{"id": "R1", "name": "Room A"}],
                "isParent": True,
            }
        ]
        result = parse_calendar_document(
            calendar_html(resources=resources), CONFIG, ["Room A"]
        )
        self.assertEqual(len(result.bookable_rooms), 1)
        with self.assertRaises(HTTPReadUnavailable):
            parse_calendar_document(
                calendar_html(resources=resources), CONFIG, ["Room A", "Room B"]
            )

    def test_unknown_duplicate_or_misassigned_events_fail_closed(self):
        for events in [
            [event(text="Unrecognized")],
            [event(), event()],
            [{**event(), "resource": "OTHER"}],
        ]:
            with self.subTest(events=events), self.assertRaises(HTTPReadUnavailable):
                parse_calendar_document(
                    calendar_html(events=events), CONFIG, ["Room A"]
                )

    def test_dynamic_loading_wrong_date_and_missing_model_are_rejected(self):
        for patch in [
            {"dynamicLoading": True},
            {"dynamicEventRendering": "Progressive"},
            {"days": 2},
            {"startDate": "2099-11-11T00:00:00"},
        ]:
            with self.subTest(patch=patch), self.assertRaises(HTTPReadUnavailable):
                parse_calendar_document(
                    calendar_html(overrides=patch), CONFIG, ["Room A"]
                )
        with self.assertRaises(HTTPReadUnavailable):
            parse_calendar_document("<html>not a calendar</html>", CONFIG, ["Room A"])

    def test_javascript_expressions_cannot_be_interpreted_as_calendar_data(self):
        source = (
            calendar_html()
            .replace("v.resources = [{", "v.resources = arbitraryFunction([{")
            .replace('"Room A"}];', '"Room A"}]);')
        )
        with self.assertRaises(HTTPReadUnavailable):
            parse_calendar_document(source, CONFIG, ["Room A"])
        expression = calendar_html().replace(
            "v.days = 1;", "v.days = 1 + arbitraryFunction();"
        )
        with self.assertRaises(HTTPReadUnavailable):
            parse_calendar_document(expression, CONFIG, ["Room A"])


class FormTests(unittest.TestCase):
    def test_page_identity_may_be_blank_after_the_initial_post(self):
        form = self.form().form
        for node in form.nodes():
            if node.attrs.get("name") == "__PageUniqueId__":
                node.attrs["value"] = ""
        parser = self.form()
        parser.form = form
        self.assertEqual(parser.payload("TimeFrom_c1$ctl04")["__PageUniqueId__"], "")

    def form(self, content=""):
        return SearchForm(
            '<form><input name="__PageUniqueId__" value="PAGE-FIXTURE"><input name="__ViewStateKey__" value="STATE-FIXTURE">'
            + content
            + "</form>"
        )

    def test_successful_controls_and_fresh_state_are_used_without_ajax_flags(self):
        form = self.form(
            '<input name="ctl00" value="old"><input name="__ASYNCPOST" value="true"><input name="unused" disabled value="bad"><input name="selected" type="checkbox" checked><input name="unselected" type="checkbox"><select name="time"><option value="10:00">10</option><option selected value="11:00">11</option></select>'
        )
        payload = form.payload("TimeFrom_c1$ctl04", {"TimeFrom_c1$ctl04": "11:00"})
        self.assertEqual(payload["__ViewStateKey__"], "STATE-FIXTURE")
        self.assertEqual(payload["selected"], "on")
        self.assertEqual(payload["time"], "11:00")
        for key in ["ctl00", "__ASYNCPOST", "unused", "unselected"]:
            self.assertNotIn(key, payload)

    def test_booking_cancel_approval_and_unknown_postbacks_are_rejected(self):
        for target in ["CONFIRM", "Cancel$Cancel_bt", "ApproveSelected", "arbitrary"]:
            with self.subTest(target=target), self.assertRaises(HTTPReadUnavailable):
                self.form().payload(target)

    def test_dropdown_values_follow_current_response_instead_of_global_indexes(self):
        choices = '<div id="Filter::ddl::"><label><input type="checkbox" id="0">Classroom</label><label><input type="checkbox" id="1">Group Study Room</label></div>'
        self.assertEqual(
            self.form(choices).multi_values("Filter", ["Group Study Room"]), "1"
        )
        changed = choices.replace('id="1"', 'id="8"')
        self.assertEqual(
            self.form(changed).multi_values("Filter", ["Group Study Room"]), "8"
        )
        with self.assertRaises(HTTPReadUnavailable):
            self.form(choices).multi_values("Filter", ["Unknown room type"])

    def test_partial_facility_table_cannot_claim_complete_availability(self):
        table = '<table id="GridResults_gv"><tr><td><input type="checkbox" name="GridResults$gv$ctl02$checkMultiple"></td><td>Room A</td></tr></table>'
        self.assertEqual(list(self.form(table).rooms()), ["Room A"])
        with self.assertRaises(HTTPReadUnavailable):
            self.form(
                table.replace(
                    "</table>", '<tr><td><a href="Page$Next">Next</a></td></tr></table>'
                )
            ).rooms()
