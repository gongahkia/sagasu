"""Run matched Codex CLI tasks against isolated synthetic Sagasu MCP servers.

python -m benchmarks.conversations --run --repetitions 3 --output REPORT.json
No .env, real browser profile, saved contacts or FBS network path is used.
"""

import argparse
import asyncio
import hashlib
import io
import json
import os
import signal
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from sagasu.benchmark import save_report

ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(__file__).with_name("conversation_cases.json")
BASELINE = "51fb3a2"
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": [
                "available",
                "unavailable",
                "unknown",
                "clarify",
                "verified",
                "pending",
                "confirmed",
                "unsupported",
                "ready",
            ],
        },
        "answer": {"type": "string"},
        "email": {"type": ["string", "null"]},
        "legs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    key: {"type": "string"}
                    for key in ("date", "room", "start_time", "end_time")
                },
                "required": ["date", "room", "start_time", "end_time"],
            },
        },
    },
    "required": ["decision", "answer", "email", "legs"],
}


def parse_events(text):
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    turns = [e["usage"] for e in events if e.get("type") == "turn.completed"]
    if not turns or any(e.get("type") in {"error", "turn.failed"} for e in events):
        raise ValueError("No successfully completed Codex turn")
    keys = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "reasoning_output_tokens",
    )
    if any(
        type(t.get(k)) is not int or t[k] < 0
        for t in turns
        for k in ("input_tokens", "output_tokens")
    ):
        raise ValueError("Codex did not report complete token usage")
    totals = {k: sum(t.get(k, 0) for t in turns) for k in keys}
    totals["reported_total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    totals["mcp_calls"] = sum(
        e.get("type") == "item.completed"
        and e.get("item", {}).get("type") == "mcp_tool_call"
        for e in events
    )
    totals["command_executions"] = sum(
        e.get("type") == "item.completed"
        and e.get("item", {}).get("type") == "command_execution"
        for e in events
    )
    totals["mcp_tool_names"] = sorted(
        {
            e.get("item", {}).get("tool", "unknown")
            for e in events
            if e.get("type") == "item.completed"
            and e.get("item", {}).get("type") == "mcp_tool_call"
        }
    )
    return totals


def grade(case, result, trace):
    problems = []
    kinds = [row["kind"] for row in trace]
    if result.get("decision") != case["decision"]:
        problems.append("wrong_decision")
    writes = kinds.count("native_write")
    if (
        writes != case.get("writes", 0)
        or not case.get("writes")
        and any(k in kinds for k in ("book", "cancel", "amend", "approve_task"))
    ):
        problems.append("unexpected_write_count")
    if case.get("email") and (
        result.get("email") != case["email"] or "cobooker_search" not in kinds
    ):
        problems.append("unverified_person")
    if case["id"] == "ambiguous_person" and "cobooker_search" not in kinds:
        problems.append("directory_not_checked")
    if (
        case["id"] == "ambiguous_date"
        and case["decision"] == "clarify"
        and "search" in kinds
    ):
        problems.append("date_was_guessed")
    if "plan" not in case:
        return problems
    mode = case["plan"]
    dates = (
        ["2099-11-02", "2099-11-03"]
        if mode == "common"
        else (
            ["2099-11-02", "2099-11-09"]
            if mode == "recurring"
            else ["2099-11-09"] if mode == "relative" else ["2099-11-02"]
        )
    )
    legs = result.get("legs", [])
    if (
        len(legs) != (2 if mode in {"split", "common", "recurring"} else 1)
        or set(leg.get("date") for leg in legs) != set(dates)
        or kinds.count("search") < len(dates)
    ):
        problems.append("incomplete_or_unverified_plan")
        return problems

    def minutes(value):
        hour, minute = map(int, value.split(":"))
        if not 0 <= hour < 24 or minute not in (0, 30):
            raise ValueError()
        return hour * 60 + minute

    for leg in legs:
        try:
            a, b = minutes(leg["start_time"]), minutes(leg["end_time"])
            free = {"SCIS1 GSR 2-1": (720, 840), "SCIS1 GSR 2-2": (840, 960)}[
                leg["room"]
            ]
            if not free[0] <= a < b <= free[1] or mode != "split" and b - a != 120:
                problems.append("unavailable_or_wrong_duration_leg")
        except (ValueError, KeyError, TypeError):
            problems.append("invalid_leg")
    if mode == "split":
        ordered = sorted(legs, key=lambda leg: leg["start_time"])
        if (
            ordered[0]["start_time"] != "12:00"
            or ordered[-1]["end_time"] != "16:00"
            or ordered[0]["end_time"] != ordered[1]["start_time"]
            or ordered[0]["room"] == ordered[1]["room"]
        ):
            problems.append("split_does_not_cover_window")
    if (
        mode == "common"
        and len({(leg["room"], leg["start_time"], leg["end_time"]) for leg in legs})
        != 1
    ):
        problems.append("not_common_across_dates")
    return problems


def export_baseline(revision, destination):
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    archive = subprocess.check_output(["git", "archive", resolved, "sagasu"], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for member in stream.getmembers():
            if not member.isfile():
                continue
            path = destination / member.name
            if not path.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Unsafe archive path")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(stream.extractfile(member).read())
    return resolved


async def stop_process(process):
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.communicate()


async def run_case(case, repo, reference_time, timeout, *, fixture_path=None):
    with tempfile.TemporaryDirectory(prefix="sagasu-agent-run-") as temporary:
        root = Path(temporary)
        trace, final, schema = [
            root / name for name in ("trace.jsonl", "answer.json", "schema.json")
        ]
        schema.write_text(json.dumps(SCHEMA))
        config = {
            "command": sys.executable,
            "args": [
                str(fixture_path or ROOT / "benchmarks/agent_fixture.py"),
                "--scenario",
                case["id"],
                "--trace",
                str(trace),
            ],
            "env": {"PYTHONPATH": str(repo)},
            "required": True,
        }
        # Inline TOML uses JSON scalar/array literals, without shell interpolation.
        server_config = (
            "mcp_servers.sagasu={"
            + ",".join(
                (
                    f"{key}={json.dumps(value)}"
                    if not isinstance(value, dict)
                    else f"{key}={{"
                    + ",".join(f"{k}={json.dumps(v)}" for k, v in value.items())
                    + "}"
                )
                for key, value in config.items()
            )
            + "}"
        )
        command = [
            "codex",
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "multi_agent",
            "--disable",
            "multi_agent_v2",
            "--json",
            "-C",
            str(root),
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(final),
            "-c",
            server_config,
            "-",
        ]
        environment = {
            k: v for k, v in os.environ.items() if not k.startswith(("SAGASU_", "SMU_"))
        }
        prompt = f"You are evaluating Sagasu using synthetic records. All tool data is fictional. Reference time: {reference_time}. Use only the connected Sagasu MCP and local reasoning/code to fulfil the user request; do not use other services. Never inspect the evaluation schema, trace, other files or server source to discover expected answers. Do not edit the project. Use explicit SGT dates. For a plan, return only one valid option (one leg per requested date, or two contiguous legs for a room change). legs[].date must be ISO YYYY-MM-DD. Return the required JSON and put an ordinary user-facing explanation/question in answer. Empty legs and null email when inapplicable. User request: {case['request']}"
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(prompt.encode()), timeout
            )
        except asyncio.TimeoutError:
            await stop_process(process)
            return {
                "status": "failed",
                "error": "deadline_exceeded",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        except asyncio.CancelledError:
            await stop_process(process)
            raise
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        try:
            if process.returncode:
                raise ValueError("CLI failed")
            metrics = parse_events(stdout.decode())
            answer = json.loads(final.read_text())
            calls = (
                [json.loads(line) for line in trace.read_text().splitlines()]
                if trace.exists()
                else []
            )
            problems = grade(case, answer, calls)
            sample = {
                "status": "success" if not problems else "failed",
                "elapsed_ms": elapsed,
                **metrics,
                "grading_errors": problems,
                "reported_decision": answer.get("decision"),
                "native_writes": sum(row["kind"] == "native_write" for row in calls),
            }
            if case.get("follow_up") and not problems:
                following = {"id": case["id"], **case["follow_up"]}
                following["request"] = (
                    f"Continue this conversation. Earlier user request: {case['request']}\nAssistant response: {answer['answer']}\nNew user message: {following['request']}"
                )
                next_sample = await run_case(
                    following, repo, reference_time, timeout, fixture_path=fixture_path
                )
                sample["status"] = next_sample["status"]
                sample["grading_errors"] += next_sample.get("grading_errors", [])
                sample["turns"] = 2
                for key in (*metrics, "elapsed_ms", "native_writes"):
                    if key == "mcp_tool_names":
                        sample[key] = sorted(
                            set(sample[key]) | set(next_sample.get(key, []))
                        )
                    else:
                        sample[key] += next_sample.get(key, 0)
                if next_sample.get("error"):
                    sample["error"] = next_sample["error"]
            return sample
        except (ValueError, OSError, KeyError, TypeError):
            return {
                "status": "failed",
                "error": "incomplete_cli_output",
                "elapsed_ms": elapsed,
            }


async def evaluate(args):
    data = json.loads(DATASET.read_text())
    cases = [case for case in data["cases"] if not args.case or case["id"] in args.case]
    if not cases or args.case and set(args.case) - {c["id"] for c in cases}:
        raise ValueError("Choose known conversation case IDs")
    version = subprocess.check_output(["codex", "--version"], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="sagasu-agent-snapshots-") as temporary:
        baseline = Path(temporary) / "baseline"
        candidate = Path(temporary) / "candidate"
        revision = export_baseline(args.baseline, baseline)
        candidate_revision = export_baseline("HEAD", candidate)
        fixture_path = Path(temporary) / "agent_fixture.py"
        fixture_path.write_bytes(
            Path(__file__).with_name("agent_fixture.py").read_bytes()
        )
        report = {
            "format": "sagasu.agent-evaluation.v1",
            "baseline_revision": revision,
            "candidate_revision": candidate_revision,
            "source_isolation": "Both MCP packages and the shared fixture driver are immutable snapshots for this run; uncommitted changes are excluded",
            "codex_version": version,
            "model": "CLI default, identical configuration in both arms",
            "evaluation_source_sha256": hashlib.sha256(
                Path(__file__).read_bytes()
                + Path(__file__).with_name("agent_fixture.py").read_bytes()
                + DATASET.read_bytes()
            ).hexdigest(),
            "scope": "Synthetic MCP records; real Codex reasoning/tool exchanges/CLI startup included; excludes real FBS latency. Each sample is a fresh independent conversation task.",
            "tokens": "Raw CLI usage. reported_total_tokens=input_tokens+output_tokens, without subtracting cached inputs; reasoning_output_tokens is retained separately without adding it a second time.",
            "repetitions": args.repetitions,
            "in_progress": True,
            "cases": [],
        }
        save_report(args.output, report)
        for case in cases:
            row = {
                "name": case["id"],
                "features": case["features"],
                "baseline": [],
                "candidate": [],
            }
            report["cases"].append(row)
            for index in range(args.repetitions):
                # Alternate ordering to reduce systematic warm-up/cache effects.
                arms = [("baseline", baseline), ("candidate", candidate)]
                for arm, repo in arms if index % 2 == 0 else reversed(arms):
                    sample = await run_case(
                        case,
                        repo,
                        data["reference_time"],
                        args.timeout,
                        fixture_path=fixture_path,
                    )
                    row[arm].append(sample)
                    print(
                        f"{case['id']} {arm} {index + 1}: {sample['status']}",
                        flush=True,
                    )
                    save_report(args.output, report)
            correct = all(
                sample["status"] == "success"
                for arm in ("baseline", "candidate")
                for sample in row[arm]
            )
            row["equivalent_correctness"] = correct
            if correct:
                row["median_changes_percent"] = {
                    metric: round(
                        (
                            statistics.median(s[metric] for s in row["candidate"])
                            / statistics.median(s[metric] for s in row["baseline"])
                            - 1
                        )
                        * 100,
                        3,
                    )
                    for metric in ("elapsed_ms", "reported_total_tokens")
                    if statistics.median(s[metric] for s in row["baseline"]) > 0
                }
            row["retention_status"] = (
                "insufficient_repetitions"
                if args.repetitions < 3
                else (
                    "review_measured_changes"
                    if correct
                    else "correctness_not_equivalent"
                )
            )
        report["in_progress"] = False
        report["ok"] = all(
            sample["status"] == "success"
            for row in report["cases"]
            for sample in row["candidate"]
        )
        report["unevaluated_feature_ids"] = sorted(
            set(
                [
                    7,
                    8,
                    9,
                    10,
                    11,
                    12,
                    14,
                    16,
                    17,
                    18,
                    19,
                    20,
                    21,
                    22,
                    23,
                    24,
                    25,
                    26,
                    27,
                    29,
                    45,
                    46,
                    47,
                    48,
                ]
            )
            - {feature for row in report["cases"] for feature in row["features"]}
        )
        save_report(args.output, report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Explicitly run real model calls against synthetic MCP",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--case", action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if (
        os.name == "nt"
        or not args.run
        or not 1 <= args.repetitions <= 10
        or not 10 <= args.timeout <= 600
    ):
        parser.error("Use --run, 1–10 repetitions and 10–600 seconds per sample")
    try:
        report = asyncio.run(evaluate(args))
    except (OSError, ValueError, subprocess.SubprocessError):
        parser.error(
            "Evaluation could not start; check Codex CLI, baseline revision and selected cases"
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
