"""Verified booking metadata, explicit coverage, and no inferred confirmations."""

import json
import unittest

from sagasu.booking_details import apply_history, parse_booking_details, parse_history
from sagasu.fbs_client import LayoutChanged


RECORD = {
    "reference_number": "BK-TEST-1",
    "room": "LKS Project Room 5-1",
    "date_time": "16-Nov-2026 (Mon) 10:00 - 12:00 (2hrs)",
    "fbs_status": "Confirmed",
    "can_cancel": True,
}


def snapshot():
    values = {
        "reference_number": "Ref Number:\tBK-TEST-1",
        "purpose": "Purpose:\tProject: α research meeting",
        "room": "Facility:\tLKS Project Room 5-1",
        "building": "Building:\tLi Ka Shing Library",
        "floor": "Floor:\tLevel 5",
        "facility_type": "Facility Type:\tProject Room (Level 5)",
        "date": "Date Requested:\t16-Nov-2026",
        "start_time": "Time From:\t10:00",
        "end_time": "To:\t12:00 (2hrs)",
        "on_behalf_of": "Book For:\tSELF",
        "created_by": "First Created By:\tOriginal Booker on 12-Nov-2026 at 10:30:00 (4 days ago)",
        "modified_by": "Last Modified By:\tOther Person on 13-Nov-2026 at 11:45:10 (3 days ago)",
        "credits_deducted": "Booking Credits Deducted:\t12.00",
        "use_type": "Use Type:\tStudy",
        "booking_usage": "Booking Usage:\t",
    }
    return {
        "fields": {
            key: [{"control": False, "value": value}] for key, value in values.items()
        },
        "participants": {
            "headers": ["", "NAME"],
            "rows": [["", "Co Booker"]],
            "summary": "1 ITEM(S) TOTAL",
        },
    }


class BookingMetadataTests(unittest.TestCase):
    def test_read_only_display_fields_preserve_values_and_parse_creator_audit(self):
        result = parse_booking_details(snapshot(), RECORD, include_additional=True)
        self.assertEqual(result["purpose"], "Project: α research meeting")
        self.assertEqual(result["booked_by"]["name"], "Original Booker")
        self.assertEqual(result["created_at"], "2026-11-12T10:30:00+08:00")
        self.assertEqual(
            result["additional_details"]["modified_by"]["name"], "Other Person"
        )
        self.assertEqual(
            result["additional_details"]["modified_at"], "2026-11-13T11:45:10+08:00"
        )
        self.assertEqual(result["additional_details"]["credits_deducted"], "12.00")
        self.assertIsNone(result["additional_details"]["booking_usage"])
        self.assertEqual(result["field_status"]["booking_usage"], "empty")
        self.assertTrue(result["reference_verified"])

    def test_status_last_modifier_and_participant_status_never_imply_confirmer(self):
        data = snapshot()
        data["participants"] = {
            "headers": ["NAME", "STATUS"],
            "rows": [["Co Booker", "Confirmed"]],
            "summary": "1 ITEM(S) TOTAL",
        }
        result = parse_booking_details(data, RECORD, include_additional=True)
        self.assertIsNone(result["confirmed_by"])
        self.assertIsNone(result["approval_history"])
        self.assertEqual(result["field_status"]["confirmed_by"], "not_exposed")

    def test_editable_control_value_takes_priority_over_its_caption_wrapper(self):
        data = snapshot()
        data["fields"]["purpose"] = [
            {"control": False, "value": "Purpose:"},
            {"control": True, "value": "Actual purpose: unchanged"},
        ]
        self.assertEqual(
            parse_booking_details(data, RECORD)["purpose"], "Actual purpose: unchanged"
        )

    def test_absent_and_empty_fields_are_distinguished(self):
        data = snapshot()
        del data["fields"]["purpose"]
        data["fields"]["building"][0]["value"] = "Building:"
        result = parse_booking_details(data, RECORD)
        self.assertIsNone(result["purpose"])
        self.assertEqual(result["field_status"]["purpose"], "not_exposed")
        self.assertEqual(result["field_status"]["building"], "empty")

    def test_wrong_or_ambiguous_reference_is_rejected(self):
        for candidates in [
            [{"value": "Ref Number: BK-OTHER"}],
            [{"value": "Ref Number: BK-TEST-1"}, {"value": "Ref Number: BK-OTHER"}],
        ]:
            data = snapshot()
            data["fields"]["reference_number"] = candidates
            with self.assertRaises(LayoutChanged):
                parse_booking_details(data, RECORD)

    def test_changed_facility_or_time_is_rejected(self):
        for key, value in [
            ("room", "Facility: Another Room"),
            ("start_time", "Time From: 11:00"),
            ("date", "Date Requested: 23-Nov-2026"),
        ]:
            data = snapshot()
            data["fields"][key][0]["value"] = value
            with self.assertRaises(LayoutChanged):
                parse_booking_details(data, RECORD)

    def test_name_only_participants_do_not_acquire_invented_emails(self):
        result = parse_booking_details(snapshot(), RECORD)
        self.assertEqual(
            result["co_bookers"],
            [{"name": "Co Booker", "email": None, "confirmation_status": None}],
        )
        self.assertIsNone(result["co_booker"])
        self.assertTrue(result["co_bookers_coverage"]["complete"])

    def test_partial_participants_never_auto_select_a_single_visible_cobooker(self):
        data = snapshot()
        data["participants"] = {
            "headers": ["NAME", "EMAIL"],
            "rows": [["First", "one@smu.edu.sg"]],
            "summary": "2 ITEM(S) TOTAL",
        }
        result = parse_booking_details(data, RECORD)
        self.assertFalse(result["co_bookers_coverage"]["complete"])
        self.assertIsNone(result["co_booker"])
        self.assertEqual(result["co_bookers"][0]["email"], "one@smu.edu.sg")

    def test_optional_additional_fields_are_not_exposed_when_disabled(self):
        self.assertNotIn(
            "additional_details", parse_booking_details(snapshot(), RECORD)
        )

    def test_ambiguous_optional_fields_are_not_guessed(self):
        data = snapshot()
        data["fields"]["purpose"].append({"value": "Purpose: Different"})
        result = parse_booking_details(data, RECORD)
        self.assertIsNone(result["purpose"])
        self.assertEqual(result["field_status"]["purpose"], "ambiguous")


class WorkflowHistoryTests(unittest.TestCase):
    def history(self, rows, total=None):
        return {
            "headers": ["ACTION", "USER", "DATE/TIME", "COMMENTS"],
            "rows": rows,
            "summary": f"{len(rows) if total is None else total} ITEM(S) TOTAL",
        }

    def test_workflow_comments_are_never_returned(self):
        history = parse_history(
            self.history(
                [
                    [
                        "Confirm",
                        "Co Booker",
                        "13-Nov-2026 10:30",
                        "PRIVATE_WORKFLOW_COMMENT",
                    ]
                ]
            )
        )
        self.assertNotIn("PRIVATE_WORKFLOW_COMMENT", json.dumps(history))
        self.assertFalse(history["comments_included"])
        self.assertEqual(history["events"][0]["at"], "2026-11-13T10:30+08:00")

    def test_send_for_confirmation_is_not_confirmation_or_approval(self):
        history = parse_history(
            self.history(
                [["Send For Confirmation", "Original Booker", "12-Nov-2026 10:30", ""]]
            )
        )
        result = apply_history(parse_booking_details(snapshot(), RECORD), history)
        self.assertEqual(result["confirmed_by"], [])
        self.assertEqual(result["approved_by"], [])

    def test_explicit_confirm_and_approve_actions_keep_their_distinct_meanings(self):
        history = parse_history(
            self.history(
                [
                    ["Confirm", "Confirmer", "13-Nov-2026 10:30", ""],
                    ["Approve", "Approver", "13-Nov-2026 11:30", ""],
                ]
            )
        )
        result = apply_history(parse_booking_details(snapshot(), RECORD), history)
        self.assertEqual(result["confirmed_by"][0]["name"], "Confirmer")
        self.assertEqual(result["approved_by"][0]["name"], "Approver")
        self.assertEqual(result["confirmed_at"], "2026-11-13T10:30+08:00")
        self.assertEqual(result["fbs_status"], RECORD["fbs_status"])

    def test_absence_in_partial_history_does_not_mean_nobody_confirmed(self):
        history = parse_history(
            self.history(
                [["Send For Confirmation", "Original Booker", "12-Nov-2026 10:30", ""]],
                total=3,
            )
        )
        result = apply_history(parse_booking_details(snapshot(), RECORD), history)
        self.assertIsNone(result["confirmed_by"])
        self.assertEqual(
            result["field_status"]["confirmed_by"], "not_observed_in_partial_history"
        )

    def test_unknown_columns_or_unverified_totals_are_rejected(self):
        for change in [
            {"headers": ["OTHER", "USER", "DATE/TIME", "COMMENTS"]},
            {"summary": "Unknown total"},
            {"rows": [["Confirm", "Actor"]]},
        ]:
            data = self.history([["Confirm", "Actor", "13-Nov-2026 10:30", ""]])
            data.update(change)
            with self.assertRaises(LayoutChanged):
                parse_history(data)
