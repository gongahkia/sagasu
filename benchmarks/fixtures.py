"""Measure synthetic user workflows: python -m benchmarks.fixtures --repetitions 3.

These timings include test setup/cleanup and local browser fixtures. They do not
measure FBS write latency. No real booking, cancellation or approval is submitted.
"""

import argparse
import json
import logging
import time
import unittest

from sagasu.benchmark import save_report, summarize

SUITES = [
    "tests.test_booking_selection.ValidationBoundaryTest",
    "tests.test_booking_selection.SelectionRetentionTest",
    "tests.test_fbs_account",
    "tests.test_task_approval",
    "tests.test_mcp_workflows.WorkflowProtocolTests",
    "tests.test_mcp.ServiceTests",
    "tests.test_mcp_wire",
    "tests.test_personal.StoreTests",
    "tests.test_repeat",
    "tests.test_alternatives",
    "tests.test_cobookers",
    "tests.test_booking_details_mcp",
    "tests.test_booking_details_browser",
    "tests.test_browser_debug",
    "tests.test_reconciliation",
    "tests.test_action_journal",
]


class TimedResult(unittest.TestResult):
    def __init__(self, measurements):
        super().__init__()
        self.measurements = measurements

    def startTest(self, test):
        super().startTest(test)
        self.started = time.perf_counter_ns()
        self.outcome = "success"

    def addFailure(self, test, error):
        self.outcome = "failed"
        super().addFailure(test, error)

    def addError(self, test, error):
        self.outcome = "failed"
        super().addError(test, error)

    def addSkip(self, test, reason):
        self.outcome = "skipped"
        super().addSkip(test, reason)

    def addSubTest(self, test, subtest, error):
        if error:
            self.outcome = "failed"
        super().addSubTest(test, subtest, error)

    def stopTest(self, test):
        self.measurements.setdefault(test.id(), []).append(
            {
                "status": self.outcome,
                "elapsed_ms": round((time.perf_counter_ns() - self.started) / 1e6, 3),
            }
        )
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output")
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 30:
        parser.error("Use 1–30 repetitions.")
    measurements = {}
    logging.disable(logging.CRITICAL)
    for _ in range(args.repetitions):
        suite = unittest.defaultTestLoader.loadTestsFromNames(SUITES)
        suite.run(TimedResult(measurements))
    report = {
        "scope": "Synthetic workflows; includes setup/cleanup; excludes real FBS write latency.",
        "repetitions": args.repetitions,
        "cases": [
            {"name": name, **summarize(samples), "measurements": samples}
            for name, samples in measurements.items()
        ],
        "ok": all(
            s["status"] == "success"
            for samples in measurements.values()
            for s in samples
        ),
    }
    if args.output:
        save_report(args.output, report)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
