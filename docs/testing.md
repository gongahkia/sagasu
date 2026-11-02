# Reliability verification

The normal test suite uses fake FBS backends and local browser fixtures. It does
not access a real SMU account or create, cancel, or approve live bookings.

## Implemented checks

- [x] Durable reservations and minimal private action outcomes for booking,
  cancellation and task approval; original submission IDs deduplicate retries
  across restart and in-memory cache expiry.
- [x] Real subprocess crash tests after a fake submission and after positive
  verification but before receipt capture. Neither recovered action is replayed.
- [x] Interrupted queued actions recover as cancelled; running actions recover
  as uncertain. Verified outcomes retain actual pending/confirmed FBS statuses.
- [x] Journal corruption, unsupported schemas and storage failure stop writes;
  credentials, purpose, co-booker emails, MFA prompts and images are excluded.
- [x] Independent stdlib-only JSON-RPC subprocess client verifies MCP
  initialization, discovery, tool calls, resources, prompts, invalid requests,
  continued connectivity, clean protocol stdout and EOF shutdown.
- [x] Authentication regressions cover changed MFA numbers, denied approval,
  cancellation while waiting, session expiry during searches, stale-prompt
  cleanup and credential redaction in results/logs.
- [x] Explicit `sagasu smoke --live --json` command checks saved authentication,
  availability search and My Bookings without submitting any account/booking
  action. Mock-based tests enforce opt-in, read-only calls, timeout cleanup,
  expired-session handling and output redaction.

## Open live verification — not run

- [ ] Run `sagasu smoke --live --json --timeout 180` against an authenticated SMU
  session. Requires access to the actual FBS service; stop MCP first and complete
  `sagasu login` separately if needed. Record the check date and count/coverage
  report here, without account details, credentials or screenshots.
- [ ] Verify current Microsoft login/MFA and session-expiry behavior with a real
  SMU account. Local fixtures do not establish current tenant behavior.
- [ ] Verify the live booking/cancellation/task-approval DOM and result wording
  when the user actually requests those actions. Do not create real bookings or
  inject crashes in production merely to exercise tests. The read-only smoke
  command deliberately cannot close this item.

These remain open until directly observed; automated fixture results do not mark
them complete. No new tool automatically replays or reconciles uncertain writes.
