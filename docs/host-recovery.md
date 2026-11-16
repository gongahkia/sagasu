# Shared-host diagnosis and recovery

These capabilities belong to Sagasu's local shared host. They work across MCP
clients using that host on macOS or Linux; they do not depend on Codex commands,
a particular checkout path, or FBS authentication. Direct MCP connections report
that shared-host recovery is unavailable.

## Agent workflow

1. Start with `server_status`, then `configuration_status` for setup failures.
2. If `runtime.host` fails, call `host_status`. It reports the live host PID,
   startup time, Python executable, and which identity components differ:
   `code`, `configuration`, or `python`. Credentials and fingerprint digests are
   excluded. Older hosts are identified as `legacy_host`; their mismatch cannot
   be classified reliably.
3. Inspect active operations, pending reviews, retained results, unresolved
   outcomes and journal health. Call `recover_host` only to repair a host that
   is blocking the requested workflow. Recovery rechecks its guards inside the
   host rather than trusting a previous status response.
4. Follow `reconnect_required`. A diagnostic MCP session remains diagnostic
   after its daemon is repaired; initialized sessions cannot switch servers in
   place. Use the client's normal reconnection mechanism, then call
   `server_status` again.
5. Check FBS authentication separately with `session_status`. Renew using
   `login` only when needed; relay MFA through `authentication_status` and wait
   for the user. A host mismatch is not evidence of an expired FBS session.

`host_status` is observational: it does not launch a host or browser, navigate
to FBS, submit credentials, or repair anything. It returns fallback CLI commands
as argument arrays using the current absolute Python executable and environment
file, so an agent does not depend on shell activation or `sagasu` being on PATH.

## Automatic code upgrades

Before forwarding any MCP messages, a new connection may replace a host when:

- Both sides support the guarded recovery protocol.
- Only code differs; the effective configuration and Python executable match.
- The connecting configuration is valid.
- No active operation, pending review, retained result, unresolved write or
  unhealthy journal remains.

Recovery is attempted once. The host verifies its expected PID and closes its
submission gate before yielding, preventing a newly queued operation from racing
the idle check. Concurrent reconnecting clients converge on one replacement host.
Other idle clients connected to the retired host may need to reconnect.

There is no automatic restart after an initialized MCP connection disconnects,
and no automatic change of configuration, interpreter, credentials or account.
Neither reads nor writes are replayed.

## Explicit recovery and protected state

`recover_host()` can start a missing host or replace an incompatible host after
the same safety checks. It never interrupts queued/running/MFA work, discards a
pending review, or removes a lock file. It also refuses unresolved writes,
including those retained only in durable action history after memory expiry.
Resolve these through the original client and `reconcile_operation` first.

Completed operation results and images block recovery by default. Set
`discard_completed_results=true` only when the user explicitly permits losing
those snapshots and receipts. This option does not override the other guards.
Durable write history, saved preferences, contacts and browser session storage
are preserved; no FBS action is retried.

A tool running inside the host cannot restart that same host while delivering
its response. It returns `client_reconnect_required`; a fresh connection can then
attempt a safe code upgrade. Compatible hosts are left running, making repeated
recovery requests harmless.

Legacy hosts lack the protected-state protocol, so the tool refuses an automatic
repair and returns exact manual commands. Unreachable hosts and older direct
processes also require inspecting local setup/lock ownership. Do not delete
locks, force-kill processes, or infer booking outcomes from these diagnostics.
