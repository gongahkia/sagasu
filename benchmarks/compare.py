"""Compare like-for-like backend reports: python -m benchmarks.compare OLD NEW."""

import argparse
import json
import math
import statistics
from pathlib import Path

from sagasu.benchmark import save_report


def measurements(case):
    samples = case.get("measurements")
    if not isinstance(samples, list) or not samples:
        raise ValueError("Every compared case needs nonempty measurements.")
    for sample in samples:
        if (
            sample.get("status") not in {"success", "failed"}
            or type(sample.get("elapsed_ms")) not in (int, float)
            or not math.isfinite(sample["elapsed_ms"])
            or sample["elapsed_ms"] < 0
        ):
            raise ValueError(
                "Measurements require success/failed status and finite nonnegative elapsed_ms."
            )
    return samples


def compare_reports(
    baseline,
    candidate,
    *,
    minimum_samples=5,
    latency_percent=20,
    latency_ms=100,
    failure_percentage_points=5,
):
    if (
        type(minimum_samples) is not int
        or minimum_samples < 1
        or any(
            type(v) not in (int, float) or not math.isfinite(v) or v < 0
            for v in (latency_percent, latency_ms, failure_percentage_points)
        )
    ):
        raise ValueError(
            "Use a positive sample floor and finite nonnegative thresholds."
        )
    context = baseline.get("comparison_context")
    if (
        not context
        or context != candidate.get("comparison_context")
        or baseline.get("in_progress")
        or candidate.get("in_progress")
    ):
        raise ValueError(
            "Completed reports with identical comparison_context are required; rerun unmatched configurations."
        )

    def cases(report):
        rows = report.get("cases", [])
        names = [row["name"] for row in rows]
        if not rows or len(set(names)) != len(names):
            raise ValueError("Reports need unique nonempty case names.")
        return {row["name"]: row for row in rows}

    old, new = cases(baseline), cases(candidate)
    output = []
    for name in sorted(old.keys() | new.keys()):
        if name not in old or name not in new:
            output.append({"name": name, "status": "unmatched"})
            continue
        if "skipped" in old[name] or "skipped" in new[name]:
            output.append({"name": name, "status": "skipped"})
            continue
        a, b = measurements(old[name]), measurements(new[name])
        sa, sb = [
            [v["elapsed_ms"] for v in samples if v["status"] == "success"]
            for samples in (a, b)
        ]
        fa, fb = [
            sum(v["status"] == "failed" for v in samples) / len(samples)
            for samples in (a, b)
        ]
        item = {
            "name": name,
            "baseline_samples": len(a),
            "candidate_samples": len(b),
            "failure_rate_change_percentage_points": round((fb - fa) * 100, 3),
            "alerts": [],
        }
        if min(len(a), len(b)) < minimum_samples:
            item["status"] = "insufficient_samples"
        else:
            item["status"] = "compared"
            if fb > fa and (fb - fa) * 100 >= failure_percentage_points:
                item["alerts"].append("increased_failures")
            if min(len(sa), len(sb)) >= minimum_samples:
                ma, mb = statistics.median(sa), statistics.median(sb)
                delta = mb - ma
                item.update(
                    baseline_median_ms=ma,
                    candidate_median_ms=mb,
                    latency_change_ms=round(delta, 3),
                    latency_change_percent=round(delta / ma * 100, 3) if ma else None,
                )
                if (
                    delta > 0
                    and delta >= latency_ms
                    and (ma == 0 or delta / ma * 100 >= latency_percent)
                ):
                    item["alerts"].append("slower_successful_operations")
            else:
                item["latency_status"] = "insufficient_successes"
        output.append(item)
    alerts = sum(bool(row.get("alerts")) for row in output)
    incomplete = any(row["status"] != "compared" for row in output)
    return {
        "format": "sagasu.backend-comparison.v1",
        "scope": "Backend sample regression checks, not end-to-end model token/speed evidence or a statistical significance test",
        "thresholds": {
            "minimum_samples": minimum_samples,
            "latency_percent": latency_percent,
            "latency_ms": latency_ms,
            "failure_percentage_points": failure_percentage_points,
        },
        "cases": output,
        "regressions": alerts,
        "complete": not incomplete,
        "ok": not alerts and not incomplete,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--minimum-samples", type=int, default=5)
    parser.add_argument("--latency-percent", type=float, default=20)
    parser.add_argument("--latency-ms", type=float, default=100)
    args = parser.parse_args(argv)
    try:
        result = compare_reports(
            json.loads(args.baseline.read_text()),
            json.loads(args.candidate.read_text()),
            minimum_samples=args.minimum_samples,
            latency_percent=args.latency_percent,
            latency_ms=args.latency_ms,
        )
    except (OSError, ValueError, TypeError, KeyError):
        parser.error(
            "Reports are invalid, incomplete or do not describe comparable runs."
        )
    if args.output:
        save_report(args.output, result)
    print(json.dumps(result, indent=2))
    return 1 if result["regressions"] else 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
