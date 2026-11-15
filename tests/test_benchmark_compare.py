import copy
import unittest

from benchmarks.compare import compare_reports


def report(samples):
    return {
        "comparison_context": {"backend": "fixture"},
        "cases": [{"name": "search", "measurements": samples}],
    }


def samples(ms, count=5):
    return [{"status": "success", "elapsed_ms": ms} for _ in range(count)]


class ComparisonTests(unittest.TestCase):
    def test_flags_only_slowdowns_exceeding_absolute_and_relative_thresholds(self):
        self.assertEqual(
            compare_reports(report(samples(1000)), report(samples(1300)))[
                "regressions"
            ],
            1,
        )
        self.assertEqual(
            compare_reports(report(samples(100)), report(samples(150)))["regressions"],
            0,
        )
        self.assertEqual(
            compare_reports(report(samples(1000)), report(samples(1100)))[
                "regressions"
            ],
            0,
        )

    def test_failures_remain_visible_even_if_success_latency_improves(self):
        result = compare_reports(
            report(samples(1000)),
            report(samples(500, 4) + [{"status": "failed", "elapsed_ms": 10}]),
        )
        self.assertEqual(result["cases"][0]["alerts"], ["increased_failures"])
        self.assertEqual(result["cases"][0]["latency_status"], "insufficient_successes")

    def test_missing_skipped_and_under_sampled_cases_are_not_green(self):
        for candidate in [
            report(samples(1000, 2)),
            {
                "comparison_context": {"backend": "fixture"},
                "cases": [{"name": "other", "measurements": samples(1000)}],
            },
            {
                "comparison_context": {"backend": "fixture"},
                "cases": [{"name": "search", "skipped": "none"}],
            },
        ]:
            self.assertFalse(compare_reports(report(samples(1000)), candidate)["ok"])

    def test_unmatched_contexts_partial_reports_invalid_or_duplicate_samples_fail(self):
        base = report(samples(1000))
        candidates = [report(samples(float("nan"))), report(samples(-1))]
        mismatch = copy.deepcopy(base)
        mismatch["comparison_context"] = {"backend": "other"}
        candidates.append(mismatch)
        pending = {**base, "in_progress": True}
        candidates.append(pending)
        candidates.append({**base, "cases": base["cases"] * 2})
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                compare_reports(base, candidate)
