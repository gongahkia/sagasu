"""Structured, task-local operation tracing without recording function arguments."""

import asyncio
import contextvars
import functools
import logging
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)
_operation = contextvars.ContextVar("sagasu_operation", default=None)
_step = contextvars.ContextVar("sagasu_step", default=None)


@dataclass
class Operation:
    id: str
    name: str
    started: float
    issues: int = 0


def error_details(error):
    """Keep code locations and exception types, never values or source lines."""
    chain, frames, seen = [], [], set()
    current = error
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        chain.append(type(current).__name__)
        for frame in traceback.extract_tb(current.__traceback__)[-8:]:
            frames.append({"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name})
        current = current.__cause__ or current.__context__
    return {"error_type": type(error).__name__, "error_chain": chain, "error_frames": frames}


def event(name, *, level=logging.INFO, **fields):
    operation = _operation.get()
    step = _step.get()
    if operation and level >= logging.WARNING:
        operation.issues += 1
    logger.log(level, name, extra={
        "event": name,
        "operation_id": operation.id if operation else None,
        "operation": operation.name if operation else None,
        "step": step[0] if step else None,
        "step_id": step[1] if step else None,
        "elapsed_ms": round((time.monotonic() - operation.started) * 1000, 1) if operation else None,
        "fields": fields,
    })


@contextmanager
def step(name):
    started = time.monotonic()
    parent = _step.get()
    token = _step.set((name, uuid.uuid4().hex[:8]))
    event("step.started", parent_step_id=parent[1] if parent else None)
    try:
        yield
    except BaseException as error:
        cancelled = isinstance(error, (asyncio.CancelledError, KeyboardInterrupt))
        event("step.cancelled" if cancelled else "step.failed", level=logging.WARNING if cancelled else logging.ERROR,
              duration_ms=round((time.monotonic() - started) * 1000, 1), **error_details(error))
        raise
    else:
        event("step.completed", duration_ms=round((time.monotonic() - started) * 1000, 1))
    finally:
        _step.reset(token)


def observed_step(name):
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            with step(name):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


def observed_operation(name):
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            if _operation.get():
                with step(name):
                    return await function(*args, **kwargs)
            operation = Operation(uuid.uuid4().hex[:12], name, time.monotonic())
            token = _operation.set(operation)
            step_token = _step.set(None)
            event("operation.started")
            try:
                result = await function(*args, **kwargs)
            except BaseException as error:
                cancelled = isinstance(error, (asyncio.CancelledError, KeyboardInterrupt))
                event("operation.cancelled" if cancelled else "operation.failed", level=logging.WARNING if cancelled else logging.ERROR, **error_details(error))
                raise
            else:
                event("operation.completed", outcome="completed_with_issues" if operation.issues else "ok", issue_events=operation.issues)
                return result
            finally:
                _step.reset(step_token)
                _operation.reset(token)
        return wrapped
    return decorate


class ContextFilter(logging.Filter):
    def filter(self, record):
        operation = _operation.get()
        active_step = _step.get()
        if not hasattr(record, "event"):
            record.event = "log"
            record.operation_id = operation.id if operation else None
            record.operation = operation.name if operation else None
            record.step = active_step[0] if active_step else None
            record.step_id = active_step[1] if active_step else None
            record.elapsed_ms = round((time.monotonic() - operation.started) * 1000, 1) if operation else None
            record.fields = {}
        return True
