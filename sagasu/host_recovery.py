"""Agent-facing local host diagnosis and guarded recovery; never touches FBS."""

import asyncio
import sys

from .config import ConfigError
from .host import connect, control
from .runtime import AlreadyRunning


class HostRecovery:
    def __init__(
        self,
        settings,
        *,
        owner_pid=None,
        diagnostic_frontend=False,
        startup_reason=None,
    ):
        self.settings = settings
        self.owner_pid = owner_pid
        self.diagnostic_frontend = diagnostic_frontend
        self.startup_reason = startup_reason
        self.lock = asyncio.Lock()

    def commands(self):
        prefix = [sys.executable, "-m", "sagasu"]
        suffix = ["--env-file", str(self.settings.env_file)]
        return {
            "status": [*prefix, "host", "status", *suffix],
            "stop_idle_host": [*prefix, "host", "stop", *suffix],
            "doctor": [*prefix, "doctor", *suffix],
        }

    async def status(self):
        try:
            report = await control(self.settings, "status")
        except (ConfigError, AlreadyRunning, OSError):
            return {
                "running": None,
                "reason": "host_unreachable",
                "can_recover": False,
                "next_action": "Use the exact local commands to inspect setup and the lock owner. Do not delete locks, kill processes or replay operations.",
                "commands": self.commands(),
            }
        supported = report.get("protocol_version", 0) >= 2
        same_process = (
            self.owner_pid is not None and report.get("pid") == self.owner_pid
        )
        if not report.get("running"):
            reason = "host_not_running"
        elif not supported:
            reason = "legacy_host"
        elif not report.get("compatible"):
            reason = "host_mismatch"
        else:
            reason = "compatible"
        # Whitelist the handshake response. Private comparison digests and
        # credentials must never become tool output, including on failure.
        result = {
            key: report[key]
            for key in (
                "running",
                "pid",
                "clients",
                "busy",
                "protocol_version",
                "compatible",
                "mismatch",
                "started_at",
                "python_executable",
                "runtime_mode",
                "active_operations",
                "retained_operations",
                "pending_reviews",
                "unresolved_outcomes",
                "journal_healthy",
            )
            if key in report
        }
        result.update(
            reason=reason,
            startup_reason=self.startup_reason,
            current_python_executable=sys.executable,
            configuration_loaded_at_startup=True,
            guarded_recovery_supported=supported,
            can_recover=(bool(report.get("can_recover")) or not report.get("running"))
            and not same_process
            and not self.settings.issues(),
            automatic_code_recovery=bool(report.get("can_recover"))
            and report.get("mismatch") == ["code"],
            reconnect_required=self.diagnostic_frontend
            or (same_process and reason == "host_mismatch"),
            next_tool=(
                "session_status"
                if reason == "compatible" and not self.diagnostic_frontend
                else (
                    "recover_host"
                    if reason != "compatible"
                    and not same_process
                    and (supported or not report.get("running"))
                    else None
                )
            ),
            commands=self.commands(),
            next_action=(
                "This older host cannot report protected state for guarded recovery. Run the status command; when idle, run the stop command, then reconnect. Do not delete locks or replay operations."
                if reason == "legacy_host"
                else (
                    "Reconnect the MCP client to load changed code; code-only recovery is automatic only when no protected state remains."
                    if same_process and reason == "host_mismatch"
                    else (
                        "Inspect recovery blockers. recover_host never interrupts work, discards pending reviews, retries FBS actions or signs in. A repaired diagnostic frontend still requires reconnecting."
                        if reason != "compatible" or self.diagnostic_frontend
                        else "Host attachment is healthy. Use session_status to check FBS authentication separately."
                    )
                )
            ),
        )
        return result

    async def recover(self, *, discard_completed_results=False):
        async with self.lock:
            report = await self.status()
            base = {
                "recovered": False,
                "reconnect_required": self.diagnostic_frontend,
                "operations_replayed": False,
                "configuration_files_changed": False,
                "next_tool": "host_status",
            }
            if self.settings.issues():
                return {
                    **base,
                    "reason": "configuration_required",
                    "next_tool": "configuration_status",
                }
            if report.get("reason") == "host_unreachable":
                return {**base, "reason": "host_unreachable"}
            if report.get("running"):
                if report.get("compatible"):
                    return {
                        **base,
                        "reason": "already_compatible",
                        "runtime_mode": report.get("runtime_mode"),
                        "next_tool": (
                            None if self.diagnostic_frontend else "server_status"
                        ),
                        "next_action": (
                            "Reconnect this diagnostic MCP client; the shared host is ready."
                            if self.diagnostic_frontend
                            else "The host already matches. Use session_status to check authentication separately."
                        ),
                    }
                if not report.get("guarded_recovery_supported"):
                    return {
                        **base,
                        "reason": "legacy_host",
                        "commands": self.commands(),
                    }
                if report.get("pid") == self.owner_pid:
                    return {
                        **base,
                        "reason": "client_reconnect_required",
                        "reconnect_required": True,
                    }
                try:
                    retired = await control(
                        self.settings,
                        "recover",
                        expected_pid=report["pid"],
                        discard_completed_results=discard_completed_results,
                    )
                except (ConfigError, AlreadyRunning, OSError):
                    return {**base, "reason": "host_unreachable"}
                if not retired.get("stopping"):
                    return {
                        **base,
                        "reason": "recovery_blocked",
                        "blockers": retired.get("blockers", ["host_changed"]),
                        "next_action": "Clear the reported blockers through the original connection: finish active/MFA work, inspect retained results and reviews, and reconcile unresolved writes. Nothing was stopped or replayed.",
                    }
                if not retired.get("shutdown_complete"):
                    return {
                        **base,
                        "reason": "host_still_stopping",
                        "retry_after_ms": 2000,
                    }
            try:
                # Starting a host takes its existing private locks and checks
                # storage; it does not navigate, authenticate or replay a job.
                connection = await connect(self.settings)
                _, writer, reply = connection
                writer.close()
                await writer.wait_closed()
            except (ConfigError, AlreadyRunning, OSError):
                return {
                    **base,
                    "reason": "host_start_failed",
                    "commands": self.commands(),
                }
            ready = reply.get("runtime_mode") == "operational"
            return {
                **base,
                "recovered": ready,
                "reason": "host_ready" if ready else "configuration_required",
                "host_pid": reply["pid"],
                "runtime_mode": reply.get("runtime_mode"),
                "next_action": "Reconnect this MCP client, then call server_status and session_status. Authentication is checked separately; no FBS operation was retried.",
            }
