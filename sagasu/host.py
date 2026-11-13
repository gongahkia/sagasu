"""Private local MCP host: one browser owner, independent client sessions.

The Unix socket is private to the current OS user. Every MCP connection has its
own protocol session/elicitation context; operations and reviews share one queue.
Disconnecting a client never restarts or replays an operation.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import asynccontextmanager

import anyio
from mcp import types
from mcp.shared.message import SessionMessage
from mcp.client.session import ClientSession

from .config import ConfigError
from .runtime import AlreadyRunning, InstanceLock


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


def fingerprint(settings):
    sources = sorted((Path(__file__).parent).glob("*.py"))
    digest = hashlib.sha256()
    effective = settings.public()
    for key, default in {
        "SMU_LOGIN_EMAIL": "",
        "SMU_LOGIN_PASSWORD": "",
        "SAGASU_STAY_SIGNED_IN": "true",
        "SAGASU_AUTH_ARTIFACT_DIR": "",
    }.items():
        effective[key] = settings.values.get(key, default)
    digest.update(json.dumps(effective, sort_keys=True).encode())
    digest.update(sys.executable.encode())
    for path in sources:
        digest.update(path.read_bytes())
    return digest.hexdigest()


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


async def connect(settings, *, action="mcp", start=True):
    path = socket_path(settings)
    process = None
    for attempt in range(100):
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(path), limit=8 * 1024 * 1024
            )
            writer.write(
                (
                    json.dumps({"fingerprint": fingerprint(settings), "action": action})
                    + "\n"
                ).encode()
            )
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if not reply.get("ok"):
                writer.close()
                await writer.wait_closed()
                raise ConfigError(
                    "The shared host uses different code or configuration. Run sagasu host stop, then reconnect after active operations finish."
                )
            return reader, writer, reply
        except (FileNotFoundError, ConnectionRefusedError):
            if not start:
                return None
            if process is not None and process.poll() is not None:
                raise ConfigError(
                    "The shared host could not start. Check for an older direct Sagasu process holding the profile and run sagasu doctor."
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
                        for _ in range(100):
                            if process.poll() is not None:
                                raise ConfigError(
                                    "The shared host could not start. An older direct process may own the profile; inspect its lock owner with sagasu doctor."
                                )
                            try:
                                _, probe = await asyncio.open_unix_connection(str(path))
                                break
                            except (FileNotFoundError, ConnectionRefusedError):
                                await asyncio.sleep(0.1)
                        else:
                            raise ConfigError(
                                "The shared host did not become ready before the startup deadline."
                            )
                    probe.close()
                    await probe.wait_closed()
            except AlreadyRunning:
                pass
            await asyncio.sleep(0.1)
    raise ConfigError(
        "The shared host did not become ready before the startup deadline. No operation was submitted."
    )


async def bridge_stdio(settings):
    from mcp.server.stdio import stdio_server

    try:
        reader, writer, _ = await connect(settings)
    except (ConfigError, AlreadyRunning, OSError):
        # Only handle attachment failures before any MCP packets are forwarded.
        # A disconnected initialized session must never start another server.
        from .diagnostics import Check
        from .mcp_server import serve

        await serve(
            settings,
            startup_checks=[
                Check(
                    "runtime.host",
                    "fail",
                    "The shared host could not start or uses different code/configuration.",
                    "Run sagasu host status. When no operations are active, run sagasu host stop and reconnect. If an older direct client owns the profile, close it first. Run sagasu doctor to inspect local configuration.",
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


async def control(settings, action):
    connection = await connect(settings, action=action, start=False)
    if connection is None:
        return {"running": False}
    _, writer, reply = connection
    writer.close()
    await writer.wait_closed()
    return reply


async def serve_host(settings, *, service=None, idle_seconds=60):
    from .mcp_server import create_server
    from .mcp_runtime import MCPRuntime
    from .fbs_client import close_shared_browser

    path = socket_path(settings)
    identity = fingerprint(settings)
    runtime = MCPRuntime(settings, service=service)
    stopped = asyncio.Event()
    clients = set()
    connections = set()
    last_used = asyncio.get_running_loop().time()

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
            busy = any(not job.terminal for job in service.jobs.values())
            # Stop/status remain available after an environment/code change.
            ok = hello.get("fingerprint") == identity or action in {"status", "stop"}
            reply = {
                "ok": ok,
                "running": True,
                "pid": os.getpid(),
                "clients": len(clients) - 1,
                "busy": busy,
            }
            if action == "stop":
                reply["stopping"] = not busy
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            if action == "stop" and not busy:
                stopped.set()
            if not ok or action != "mcp":
                return
            server = create_server(
                service,
                configuration_provider=runtime.status,
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
        path.chmod(0o600)
        watcher = asyncio.create_task(idle())
        try:
            # The server context manager waits for all accepted transports on
            # exit, so perform shutdown explicitly in the required order below.
            await stopped.wait()
        finally:
            server.close()
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
            await asyncio.gather(watcher, *handlers, return_exceptions=True)
            await server.wait_closed()
            await service.close()
            await close_shared_browser()
            path.unlink(missing_ok=True)
