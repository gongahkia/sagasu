"""Private local MCP host: one browser owner, independent client sessions.

The Unix socket is private to the current OS user. Every MCP connection has its
own protocol session/elicitation context; operations and reviews share one queue.
Disconnecting a client never restarts or replays an operation.

With SAGASU_HTTP_PORT set the host is a long-lived service: it never exits when
idle, is started only by a service manager, and also serves a key-protected,
restricted MCP endpoint on loopback for a TLS reverse proxy (see remote.py).
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import anyio
from mcp import types
from mcp.shared.message import SessionMessage
from mcp.client.session import ClientSession

from .config import ConfigError
from .runtime import AlreadyRunning, InstanceLock


# How long a command waits for a supervised service to come (back) up before
# reporting it stopped. A service manager restarts it; a command never does.
SERVICE_WAIT_SECONDS = 10


class HostError(ConfigError):
    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


def socket_path(settings):
    if os.name == "nt":
        raise ConfigError(
            "The shared host currently requires macOS or Linux; use a direct MCP connection on Windows."
        )
    root = Path("/tmp") / f"sagasu-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if root.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError(
            "Sagasu's private runtime directory has unsafe ownership or permissions."
        )
    key = hashlib.sha256(str(settings.profile).encode()).hexdigest()[:20]
    return root / (key + ".sock")


def identity_parts(settings):
    """Private handshake digests. Return only comparisons, never these values, to MCP."""
    effective = settings.public()
    effective["configuration_issues"] = [
        (issue.name, issue.blocks_startup) for issue in settings.issues()
    ]
    for key, default in {
        "SMU_LOGIN_EMAIL": "",
        "SMU_LOGIN_PASSWORD": "",
        "SAGASU_STAY_SIGNED_IN": "true",
        "SAGASU_AUTH_ARTIFACT_DIR": "",
    }.items():
        effective[key] = settings.values.get(key, default)
    # Settings added later join the digest only when they are used. An
    # unchanged configuration then keeps the digest that a host started by an
    # earlier version computed, and a code-only upgrade still replaces it.
    if effective["session_cookies"] == "all":
        del effective["session_cookies"]
    if not settings.remote_listener_requested:
        del effective["remote_listener"]
    if settings.values.get("SAGASU_HTTP_KEYS"):
        effective["SAGASU_HTTP_KEYS"] = settings.values["SAGASU_HTTP_KEYS"]
    for key in (
        "SAGASU_HTTP_SIGN_IN_KEY_FILE",
        "SAGASU_SMU_ACCOUNT",
        "SAGASU_SAVED_SIGN_IN_STAY",
    ):
        if settings.values.get(key):
            effective[key] = settings.values[key]
    code = hashlib.sha256()
    for path in sorted((Path(__file__).parent).glob("*.py")):
        code.update(path.name.encode())
        code.update(path.read_bytes())
    return {
        "configuration": hashlib.sha256(
            json.dumps(effective, sort_keys=True).encode()
        ).hexdigest(),
        "python": hashlib.sha256(sys.executable.encode()).hexdigest(),
        "code": code.hexdigest(),
    }


def _fingerprint(parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def fingerprint(settings):
    return _fingerprint(identity_parts(settings))


def recovery_state(service):
    from .action_journal import JournalError

    # Preserve evidence of unresolved writes even when normal TTL pruning could
    # evict their in-memory copy in favour of the durable journal.
    unresolved = sum(
        job.status == "outcome_unknown"
        and not (job.reconciliation or {}).get("action_outcome_verified")
        for job in service.jobs.values()
    )
    journal_healthy = not service.journal_failed
    if service.journal is not None:
        try:
            unresolved = max(unresolved, service.journal.unresolved_count())
        except (JournalError, OSError):
            journal_healthy = False
    service.prune()
    active = sum(not job.terminal for job in service.jobs.values())
    return {
        "active_operations": active,
        "retained_operations": len(service.jobs),
        "pending_reviews": len(service.reviews),
        "unresolved_outcomes": unresolved,
        "journal_healthy": journal_healthy,
        "can_recover": not (
            active
            or service.jobs
            or service.reviews
            or unresolved
            or not journal_healthy
        ),
    }


@asynccontextmanager
async def transport(reader, writer):
    incoming, receive = anyio.create_memory_object_stream(0)
    send, outgoing = anyio.create_memory_object_stream(0)

    async def read():
        async with incoming:
            while line := await reader.readline():
                try:
                    message = SessionMessage(
                        types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                    )
                except Exception as error:
                    message = error
                await incoming.send(message)

    async def write():
        async with outgoing:
            async for message in outgoing:
                writer.write(
                    (
                        message.message.model_dump_json(
                            by_alias=True, exclude_unset=True
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(read)
            group.start_soon(write)
            try:
                yield receive, send
            finally:
                group.cancel_scope.cancel()
    finally:
        writer.close()
        await writer.wait_closed()


async def connect(settings, *, action="mcp", start=True, options=None):
    path = socket_path(settings)
    process = None
    recovered = False
    service_absent = None
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        try:
            parts = identity_parts(settings)
            reader, writer = await asyncio.open_unix_connection(
                str(path), limit=8 * 1024 * 1024
            )
            writer.write(
                (
                    json.dumps(
                        {
                            "fingerprint": _fingerprint(parts),
                            "identity": parts,
                            "action": action,
                            **(options or {}),
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            try:
                line = await asyncio.wait_for(
                    reader.readline(),
                    min(10, max(0.1, deadline - asyncio.get_running_loop().time())),
                )
            except TimeoutError:
                writer.close()
                await writer.wait_closed()
                raise HostError(
                    "The shared host did not respond to its startup handshake.",
                    "host_handshake_timeout",
                ) from None
            if not line:
                # A shutting-down host can close a newly accepted connection
                # before reading its handshake. Wait for its listener to close.
                writer.close()
                await writer.wait_closed()
                await asyncio.sleep(0.05)
                continue
            reply = json.loads(line)
            if not reply.get("ok"):
                writer.close()
                await writer.wait_closed()
                if (
                    action == "mcp"
                    and start
                    and not recovered
                    and reply.get("protocol_version", 0) >= 2
                    and reply.get("mismatch") == ["code"]
                    and reply.get("can_recover")
                    and not settings.issues()
                ):
                    # A bounded upgrade, before forwarding any MCP packet.
                    # The host rechecks identity and protected work atomically.
                    recovered = True
                    retired = await control(
                        settings, "recover", expected_pid=reply["pid"], automatic=True
                    )
                    if (
                        retired.get("shutdown_complete")
                        or retired.get("stopping")
                        or not retired.get("running")
                        or "host_changed" in retired.get("blockers", [])
                    ):
                        # A host that agreed to stop but is still finishing is
                        # on its way out: keep trying until the deadline.
                        continue
                raise HostError(
                    "The shared host uses different code or configuration. Read host_status, then recover_host for guarded recovery. Active work and retained evidence are protected.",
                    "host_configuration_changed",
                )
            return reader, writer, reply
        except (FileNotFoundError, ConnectionRefusedError):
            if not start:
                return None
            if settings.remote_listener_requested:
                # A service manager owns this host. Starting one here would run
                # a second remote listener outside its supervision.
                now = asyncio.get_running_loop().time()
                if service_absent is None:
                    service_absent = now
                if now - service_absent >= SERVICE_WAIT_SECONDS:
                    break
                await asyncio.sleep(0.1)
                continue
            if process is not None and process.poll() is not None:
                raise HostError(
                    "The shared host could not start. Check for an older direct Sagasu process holding the profile and run sagasu doctor.",
                    "host_start_failed",
                )
            try:
                with InstanceLock(path.with_suffix(".startup")):
                    try:
                        _, probe = await asyncio.open_unix_connection(str(path))
                    except (FileNotFoundError, ConnectionRefusedError):
                        process = subprocess.Popen(
                            [
                                sys.executable,
                                "-m",
                                "sagasu",
                                "host-run",
                                "--env-file",
                                str(settings.env_file),
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        while asyncio.get_running_loop().time() < deadline:
                            if process.poll() is not None:
                                raise HostError(
                                    "The shared host could not start. An older direct process may own the profile; inspect its lock owner with sagasu doctor.",
                                    "host_start_failed",
                                )
                            try:
                                _, probe = await asyncio.open_unix_connection(str(path))
                                break
                            except (FileNotFoundError, ConnectionRefusedError):
                                await asyncio.sleep(0.1)
                        else:
                            raise HostError(
                                "The shared host did not become ready before the startup deadline.",
                                "host_start_timeout",
                            )
                    probe.close()
                    await probe.wait_closed()
            except AlreadyRunning:
                pass
            await asyncio.sleep(0.1)
    if service_absent is not None:
        raise HostError(
            "The Sagasu service is not running. Start it with the host's service manager (for example: sudo systemctl start sagasu), then retry. No operation was submitted.",
            "host_service_stopped",
        )
    raise HostError(
        "The shared host did not become ready before the startup deadline. No operation was submitted.",
        "host_start_timeout",
    )


async def bridge_stdio(settings):
    from mcp.server.stdio import stdio_server

    try:
        reader, writer, _ = await connect(settings)
    except (ConfigError, AlreadyRunning, OSError) as error:
        # Only handle attachment failures before any MCP packets are forwarded.
        # A disconnected initialized session must never start another server.
        from .diagnostics import Check
        from .mcp_server import serve

        messages = {
            "host_configuration_changed": "The shared host uses different code or configuration.",
            "host_handshake_timeout": "The shared host did not respond to its connection handshake before the deadline.",
            "host_start_failed": "The shared host exited before it became ready; an older process may own its profile.",
            "host_start_timeout": "The shared host did not become ready before the startup deadline.",
            "host_service_stopped": "The Sagasu service is not running; its service manager starts it.",
        }
        from .host_recovery import HostRecovery

        await serve(
            settings,
            host_controller=HostRecovery(
                settings,
                diagnostic_frontend=True,
                startup_reason=getattr(error, "reason", "host_unavailable"),
            ),
            startup_checks=[
                Check(
                    "runtime.host",
                    "fail",
                    messages.get(
                        getattr(error, "reason", None),
                        "The shared host could not start or attach safely.",
                    ),
                    "Read host_status for the live host, mismatch categories and recovery blockers. Use recover_host only when its guard permits, then reconnect this MCP client. Never delete locks or replay operations. Older hosts require the exact local commands returned by host_status.",
                    True,
                )
            ],
        )
        return
    async with transport(reader, writer) as (remote_read, remote_write):
        async with stdio_server() as (local_read, local_write):
            async with local_read, local_write:
                async with anyio.create_task_group() as group:

                    async def forward(source, target):
                        async for message in source:
                            await target.send(message)
                        group.cancel_scope.cancel()

                    group.start_soon(forward, local_read, remote_write)
                    group.start_soon(forward, remote_read, local_write)


@asynccontextmanager
async def client_session(settings):
    reader, writer, _ = await connect(settings)
    async with transport(reader, writer) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


async def run_operation(settings, tool, arguments=None, *, notify=None, progress=None):
    failure = None
    output = None
    last_prompt = None
    async with client_session(settings) as session:

        async def call(name, arguments=None):
            result = await session.call_tool(name, arguments or {})
            if result.is_error:
                raise ConfigError(
                    "The shared host rejected this operation; inspect sagasu host status."
                )
            return result.structured_content

        operation = await call(tool, arguments)
        while True:
            status = await call(
                "wait_operation",
                {"operation_id": operation["operation_id"], "timeout": 5},
            )
            if status["status"] == "awaiting_mfa" and notify:
                prompt = await call(
                    "authentication_status", {"operation_id": operation["operation_id"]}
                )
                if prompt["prompt"] and prompt["prompt"] != last_prompt:
                    await notify(prompt["prompt"])
                    last_prompt = prompt["prompt"]
                await asyncio.sleep(0.25)
            elif progress:
                await progress(status["stage"])
            if status["terminal"]:
                if status["status"] != "succeeded":
                    error = status.get("error") or {}
                    failure = (
                        error.get("message", "The operation did not succeed.")
                        + " "
                        + error.get("next_action", "")
                    )
                output = status.get("result", {})
                break
    if failure:
        raise ConfigError(failure)
    return output


async def control(settings, action, **options):
    path = socket_path(settings)
    try:
        listener_id = path.stat().st_ino
    except FileNotFoundError:
        listener_id = None
    connection = await connect(settings, action=action, start=False, options=options)
    if connection is None:
        return {"running": False}
    _, writer, reply = connection
    writer.close()
    await writer.wait_closed()
    if action in {"stop", "recover"} and reply.get("stopping"):
        # A successful stop is followed by reconnect in the setup workflow.
        # Wait for cleanup, rather than racing the closing listener/profile.
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            try:
                current = path.stat().st_ino
            except FileNotFoundError:
                reply["shutdown_complete"] = True
                break
            if listener_id is not None and current != listener_id:
                reply["shutdown_complete"] = True
                break
            await asyncio.sleep(0.02)
        else:
            reply["shutdown_complete"] = False
    return reply


async def serve_host(settings, *, service=None, idle_seconds=60):
    from .mcp_server import create_server
    from .mcp_runtime import MCPRuntime
    from .fbs_client import close_shared_browser
    from .host_recovery import HostRecovery

    path = socket_path(settings)
    parts = identity_parts(settings)
    identity = _fingerprint(parts)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    runtime = MCPRuntime(settings, service=service)
    stopped = asyncio.Event()
    clients = set()
    connections = set()
    last_used = asyncio.get_running_loop().time()
    # A requested remote listener makes this a supervised, long-lived service,
    # even while a configuration problem keeps the listener itself off.
    service_mode = settings.remote_listener_requested
    remote_http = None

    async def accept(reader, writer):
        nonlocal last_used
        if stopped.is_set():
            writer.close()
            await writer.wait_closed()
            return
        task = asyncio.current_task()
        clients.add(task)
        connections.add(writer)
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line:
                return
            hello = json.loads(line)
            action = hello.get("action")
            state = recovery_state(service)
            busy = bool(state["active_operations"])
            supplied_parts = hello.get("identity") or {}
            mismatch = [key for key in parts if supplied_parts.get(key) != parts[key]]
            compatible = hello.get("fingerprint") == identity
            # Stop/status remain available after an environment/code change.
            ok = compatible or action in {"status", "stop", "recover"}
            reply = {
                "ok": ok,
                "running": True,
                "pid": os.getpid(),
                "clients": len(clients) - 1,
                "busy": busy,
                "protocol_version": 2,
                "compatible": compatible,
                "mismatch": mismatch,
                "started_at": started_at,
                "python_executable": sys.executable,
                "runtime_mode": runtime.status()["mode"],
                "service_mode": service_mode,
                "remote_listener": remote_http is not None,
                **state,
            }
            if action == "stop":
                reply["stopping"] = not busy
            elif action == "recover":
                blockers = []
                if hello.get("expected_pid") != os.getpid():
                    blockers.append("host_changed")
                if busy:
                    blockers.append("active_operations")
                if state["pending_reviews"]:
                    blockers.append("pending_reviews")
                if state["unresolved_outcomes"]:
                    blockers.append("unresolved_outcomes")
                if not state["journal_healthy"]:
                    blockers.append("journal_unhealthy")
                if state["retained_operations"] and not hello.get(
                    "discard_completed_results", False
                ):
                    blockers.append("retained_operations")
                if hello.get("automatic") and mismatch != ["code"]:
                    blockers.append("not_code_only")
                reply["blockers"] = blockers
                reply["stopping"] = not blockers
            if reply.get("stopping"):
                # Close the submission gate before yielding, so another client
                # cannot enqueue work between the idle check and shutdown.
                service.closed = True
                stopped.set()
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            if not ok or action != "mcp":
                return
            server = create_server(
                service,
                configuration_provider=runtime.status,
                host_controller=HostRecovery(settings, owner_pid=os.getpid()),
                allow_agent_submission=settings.values.get(
                    "SAGASU_MCP_CONFIRMATION", "agent"
                )
                == "agent",
            )
            async with transport(reader, writer) as (receive, send):
                await server._lowlevel_server.run(
                    receive,
                    send,
                    server._lowlevel_server.create_initialization_options(),
                )
        finally:
            clients.discard(task)
            connections.discard(writer)
            last_used = asyncio.get_running_loop().time()
            writer.close()
            await writer.wait_closed()

    async def idle():
        while not stopped.is_set():
            await asyncio.sleep(min(1, idle_seconds))
            if (
                not clients
                and not any(not job.terminal for job in service.jobs.values())
                and asyncio.get_running_loop().time() - last_used >= idle_seconds
            ):
                stopped.set()

    with runtime:
        service = runtime.service
        if path.exists():
            # Diagnostic startup does not own the profile lock. Never replace
            # another live host's listener when starting a limited host.
            try:
                _, existing = await asyncio.open_unix_connection(str(path))
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            else:
                existing.close()
                await existing.wait_closed()
                await service.close()
                raise ConfigError(
                    "A shared host already owns this socket; reconnect to it or inspect sagasu host status."
                )
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            accept, str(path), limit=8 * 1024 * 1024
        )
        listener_id = path.stat().st_ino
        path.chmod(0o600)
        watcher = None if service_mode else asyncio.create_task(idle())
        signals = []

        def request_stop():
            # Same gate as a local stop: refuse new work, then shut down in order.
            service.closed = True
            stopped.set()

        try:
            if service_mode:
                loop = asyncio.get_running_loop()
                for number in (signal.SIGTERM, signal.SIGINT):
                    try:
                        loop.add_signal_handler(number, request_stop)
                        signals.append(number)
                    except (NotImplementedError, RuntimeError, ValueError):
                        pass
                listener = settings.http
                if listener is not None and runtime.status()["operational"]:
                    from .hosted.runtime import start_listener

                    remote_http = await start_listener(
                        settings, service, runtime.status
                    )
            # The server context manager waits for all accepted transports on
            # exit, so perform shutdown explicitly in the required order below.
            await stopped.wait()
        finally:
            for number in signals:
                asyncio.get_running_loop().remove_signal_handler(number)
            server.close()
            if remote_http is not None:
                # Remote callers go first, before local clients and the browser.
                await remote_http.stop()
            if watcher is not None:
                watcher.cancel()
            # Closing the sockets also wakes SDK receive loops whose cleanup
            # shields cancellation while waiting for an incoming message.
            for writer in list(connections):
                writer.close()
            handlers = list(clients)
            for task in handlers:
                task.cancel()
            # Python waits for accepted transports as well as the listener.
            # Close the idle MCP connections before waiting for the server;
            # otherwise a connected client can hold the profile lock forever.
            await asyncio.gather(
                *([watcher] if watcher is not None else []),
                *handlers,
                return_exceptions=True,
            )
            await server.wait_closed()
            await service.close()
            await close_shared_browser()
            try:
                if path.stat().st_ino == listener_id:
                    path.unlink()
            except FileNotFoundError:
                pass


def __getattr__(name):
    if name == "saved_sign_in":
        from .hosted.runtime import saved_sign_in

        return saved_sign_in
    raise AttributeError(name)
