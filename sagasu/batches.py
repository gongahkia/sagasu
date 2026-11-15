"""Aggregate existing immutable reviews without bypassing individual write jobs."""

import asyncio
import copy

from .action_journal import RESULT_KEYS, safe_fields
from .observability import event


def batch_result(job):
    members = []
    for index, entry in enumerate(job.batch_members):
        child = entry.get("job")
        members.append(
            {
                "item_index": index,
                "review_id": entry["review_id"],
                "kind": entry["kind"],
                "operation_id": child.id if child else None,
                "status": child.status if child else "queue_failed",
                "result": (
                    safe_fields(child.result or {}, RESULT_KEYS) if child else None
                ),
                "error": copy.deepcopy(child.error) if child else entry.get("error"),
            }
        )
    return {
        "members": members,
        "total": len(members),
        "complete": all(
            entry.get("job") is None or entry["job"].terminal
            for entry in job.batch_members
        ),
        "all_operations_succeeded": bool(members)
        and all(member["status"] == "succeeded" for member in members),
        "outcomes_known": all(
            member["status"] in {"succeeded", "failed", "cancelled"}
            for member in members
        ),
        "authentication_operations": [
            member["operation_id"]
            for member in members
            if member["status"] == "awaiting_mfa"
        ],
        "automatic_retries": False,
        "policy": "Each native action has an independent outcome and durable history. Successes are not rolled back on other failures. Pending Confirmation is not final confirmation. Batch monitors expire on restart; use individual action history/operation IDs afterward.",
    }


async def run_batch(service, job, specs):
    # this monitor must not hold the browser queue while child jobs acquire it.
    job.status, job.stage = "running", "Queueing individually reviewed actions"
    job.updated_at = service.clock()
    try:
        tasks = []
        for spec in specs:
            entry = {
                "review_id": spec["review_id"],
                "kind": spec["kind"],
                "job": None,
                "error": None,
            }
            job.batch_members.append(entry)
            try:
                queued = service.start(
                    spec["kind"], spec["payload"], review_id=spec["review_id"]
                )
                child = service.get(queued["operation_id"])
                entry["job"] = child
                service.submitted_reviews[spec["review_id"]] = child.id
                tasks.append(child.task)
            except Exception as error:
                entry["error"] = {
                    "code": type(error).__name__,
                    "message": "This action could not be queued. Inspect action_history before preparing another review.",
                    "retry_safe": False,
                    "next_tool": "action_history",
                }
        job.stage = "Waiting for individual outcomes; inspect members for authentication prompts"
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        for entry in job.batch_members:
            child = entry.get("job")
            if child and child.task.cancelled() and child.status == "queued":
                child.status, child.stage, child.updated_at = (
                    "cancelled",
                    "Stopped before execution",
                    service.clock(),
                )
                service._persist_outcome(child)
        job.result = batch_result(job)
        job.status, job.stage = (
            "succeeded",
            "Batch complete; inspect each individual outcome",
        )
    except asyncio.CancelledError:
        job.result = batch_result(job)
        job.status, job.stage = (
            "cancelled",
            "Batch monitor stopped; inspect individual operation IDs",
        )
    except Exception as error:
        job.result = batch_result(job)
        job.status, job.stage = (
            "failed",
            "Batch monitor failed; inspect individual operation IDs",
        )
        job.error = {
            "code": type(error).__name__,
            "message": job.stage,
            "retry_safe": False,
        }
    finally:
        job.updated_at = service.clock()
        job.batch_members.clear()
        event("agent.operation_finished", agent_operation_id=job.id, outcome=job.status)
