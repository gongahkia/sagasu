import asyncio
import io
import json
import logging
import unittest

from sagasu.console import Reporter, Redact, EventFormatter
from sagasu.observability import ContextFilter, event, observed_operation, observed_step


class TraceTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_operations_have_separate_ids_and_paired_step_timings(
        self,
    ):
        @observed_step("calendar.read")
        async def read(tag):
            await asyncio.sleep(0)
            event("calendar.coverage", tag=tag, rooms=3)

        @observed_operation("search")
        async def search(tag):
            await read(tag)

        with self.assertLogs("sagasu.observability", level="INFO") as logs:
            await asyncio.gather(search("a"), search("b"))
        roots = [r for r in logs.records if r.event == "operation.started"]
        self.assertEqual(len({r.operation_id for r in roots}), 2)
        snapshots = [r for r in logs.records if r.event == "calendar.coverage"]
        self.assertNotEqual(snapshots[0].operation_id, snapshots[1].operation_id)
        for root in roots:
            records = [r for r in logs.records if r.operation_id == root.operation_id]
            begin = next(r for r in records if r.event == "step.started")
            finish = next(r for r in records if r.event == "step.completed")
            self.assertEqual(begin.step_id, finish.step_id)
            self.assertGreaterEqual(finish.fields["duration_ms"], 0)
            self.assertEqual(records[-1].event, "operation.completed")

    async def test_failures_keep_types_and_locations_without_exception_values(self):
        @observed_operation("search")
        @observed_step("calendar.read")
        async def broken():
            try:
                raise ValueError("private password and booking contents")
            except ValueError:
                raise RuntimeError("secret redirect?session=private") from None

        with self.assertLogs("sagasu.observability", level="INFO") as logs:
            with self.assertRaises(RuntimeError):
                await broken()
        record = next(r for r in logs.records if r.event == "operation.failed")
        self.assertEqual(record.fields["error_chain"], ["RuntimeError", "ValueError"])
        self.assertTrue(record.fields["error_frames"])
        data = EventFormatter(structured=True).format(record)
        self.assertNotIn("private password", data)
        self.assertNotIn("session=private", data)
        self.assertNotIn("secret redirect", data)

    async def test_cancellation_is_distinct_and_context_does_not_leak(self):
        @observed_operation("search")
        async def cancelled():
            raise asyncio.CancelledError()

        with self.assertLogs("sagasu.observability", level="INFO") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await cancelled()
            event("outside")
        self.assertEqual(logs.records[-2].event, "operation.cancelled")
        self.assertIsNone(logs.records[-1].operation_id)

    async def test_recovered_failure_is_visible_in_operation_summary(self):
        @observed_step("receipt.deliver")
        async def fail():
            raise RuntimeError("private")

        @observed_operation("booking")
        async def recovered():
            try:
                await fail()
            except RuntimeError:
                return "verified"

        with self.assertLogs("sagasu.observability", level="INFO") as logs:
            self.assertEqual(await recovered(), "verified")
        self.assertEqual(logs.records[-1].fields["outcome"], "completed_with_issues")


class FormattingTest(unittest.TestCase):
    def test_structured_fields_and_free_text_are_redacted_recursively(self):
        record = logging.LogRecord(
            "test",
            logging.INFO,
            "",
            0,
            "Enter number 88. me@smu.edu.sg password-value",
            (),
            None,
        )
        ContextFilter().filter(record)
        record.fields = {
            "nested": [{"purpose": "private title", "cookie": "private session"}],
            "url": "https://example.test/login?session=private",
            "safe": "password-value",
            "rooms": 4,
        }
        Redact(["password-value"]).filter(record)
        data = EventFormatter(structured=True).format(record)
        for private in (
            "88",
            "me@smu.edu.sg",
            "password-value",
            "private title",
            "private session",
            "session=private",
        ):
            self.assertNotIn(private, data)
        self.assertEqual(json.loads(data)["fields"]["rooms"], 4)

    def test_structured_reporter_outputs_json_lines_without_spinner_or_banner(self):
        stream = io.StringIO()
        reporter = Reporter(stream=stream, structured=True)
        reporter.banner()
        with reporter.progress("Starting") as progress:
            progress.finish("Ready")
        reporter.write('{"event":"test"}', raw=True)
        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(lines[0]["event"], "cli.message")
        self.assertEqual(lines[-1]["event"], "test")
        self.assertFalse(reporter.animated)
