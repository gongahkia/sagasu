"""Secret-free client configuration, printed for the user to merge locally."""

import json
import sys


def client_config(settings, client, *, telegram=False):
    args = ["-m", "bot", "mcp", "--env-file", str(settings.env_file)]
    if telegram:
        args.append("--telegram")
    entry = {"command": sys.executable, "args": args}
    if client == "codex":
        return "\n".join(
            [
                "[mcp_servers.sagasu]",
                "command = " + json.dumps(entry["command"]),
                "args = " + json.dumps(args),
                "startup_timeout_sec = 30",
                "tool_timeout_sec = 60",
            ]
        )
    if client == "pi":
        entry.update(
            timeout=60,
            exposure="deferred",
            description="Find and book SMU rooms, manage bookings and co-bookers. Start with server_status.",
        )
    return json.dumps({"mcpServers": {"sagasu": entry}}, indent=2)
