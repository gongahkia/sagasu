"""Keep MCP diagnostics reachable when operational startup cannot complete."""

from contextlib import ExitStack

from .action_journal import ActionJournal, JournalError
from .config import ConfigurationRequired
from .diagnostics import Check, configuration_status
from .mcp_service import AgentService
from .personal import PersonalDataError, PersonalStore
from .runtime import InstanceLock


class UnavailableBackend:
    configuration_unavailable = True

    def capabilities(self):
        return {"mode": "diagnostic", "routed_tools": [], "launches_chromium": False}

    async def run(self, kind, payload, progress, notify):
        raise ConfigurationRequired(
            "Sagasu is in diagnostic mode. Read configuration_status, apply the local fixes, and reconnect before retrying."
        )


class MCPRuntime:
    def __init__(self, settings, *, service=None, startup_checks=()):
        self.settings = settings
        self.service = service
        self.startup_checks = list(startup_checks)
        self.locks = ExitStack()

    def status(self):
        return configuration_status(self.settings, startup_checks=self.startup_checks)

    def __enter__(self):
        if not self.status()["operational"]:
            self.service = AgentService(backend=UnavailableBackend())
            return self
        try:
            self.locks.enter_context(InstanceLock(self.settings.profile))
            self.locks.enter_context(
                InstanceLock(
                    self.settings.journal.parent
                    / ("." + self.settings.journal.name + ".lock")
                )
            )
            if self.service is None:
                personal = PersonalStore(self.settings.preferences)
                # Existing private data must be readable; never reset it as a fallback.
                if self.settings.preferences.exists():
                    personal.preferences()
                self.service = AgentService(
                    profile=personal, journal=ActionJournal(self.settings.journal)
                )
            if self.service.journal:
                self.service.journal.recover(self.service.clock())
        except (OSError, PersonalDataError, JournalError):
            self.locks.close()
            self.startup_checks.append(
                Check(
                    "runtime.storage",
                    "fail",
                    "Private preferences or action history could not be opened safely.",
                    "Check SAGASU_PREFERENCES_PATH and SAGASU_JOURNAL_PATH, their permissions and database compatibility. Preserve existing files; do not reset action history. Reconnect after correcting the problem.",
                    True,
                )
            )
            self.service = AgentService(backend=UnavailableBackend())
        except BaseException:
            self.locks.close()
            raise
        return self

    def __exit__(self, *args):
        self.locks.close()
