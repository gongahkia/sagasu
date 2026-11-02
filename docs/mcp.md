# Sagasu for agents

Sagasu is a private, single-user MCP server for SMU Facility Booking System
searches, bookings, cancellations and task approvals. MCP clients use the same
Playwright backend, saved browser session, contacts and search preferences.
There is no public HTTP listener or OpenAI API dependency. Only connect trusted
clients: their model provider may receive room details, co-booker addresses,
current authentication prompts and receipt images.

## Install and authenticate

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
sagasu config init
sagasu doctor --timeout 30
sagasu login
```

Python 3.11+ is required. Complete SMU Microsoft sign-in and MFA in the private
browser. Optional `SMU_LOGIN_EMAIL` and `SMU_LOGIN_PASSWORD` enable host-side
autofill. Do not put passwords in agent messages or client command arguments.
See [CLI configuration](cli.md) for private paths and headless login.

Your client launches `sagasu mcp`; running it manually waits for protocol input.
Only one process can own a profile at a time. Stop MCP before running standalone
login or `doctor --session`. Close the existing server before starting another
client with the same profile. Closing an agent can stop its server; keep it open until writes
finish. Saved contacts/preferences and browser sessions survive restarts;
operation IDs, reviews, authentication prompts and screenshots do not.

## Connect an MCP client

Configure your client's standard stdio transport to launch the installed executable:

```sh
/absolute/path/to/sagasu/.venv/bin/sagasu mcp --env-file /absolute/path/to/sagasu/.env
```

The equivalent command/arguments entry is:

```json
{
  "command": "/absolute/path/to/sagasu/.venv/bin/sagasu",
  "args": ["mcp", "--env-file", "/absolute/path/to/sagasu/.env"]
}
```

Your client controls its configuration file format and server lifecycle. Sagasu
requires no client extension, harness SDK, provider API or custom bridge. The
server uses the official Python MCP SDK with standard tools, resources, prompts
and capability negotiation. It supports both current and legacy protocol
connections through the SDK. Human-mode confirmation requires standard MCP form
elicitation; agent mode does not.

Use an absolute executable and environment-file path so launching from another
working directory works. Keep credentials in private host configuration. Allow
at least 30 seconds for startup and 60 seconds per tool call; `wait_operation`
returns within 30 seconds even while the underlying browser operation continues.
Standard output carries only MCP messages; diagnostics go to standard error.

## Query to action

Agent submission is the default (`SAGASU_MCP_CONFIRMATION=agent`). A request such
as “Book any available SCIS1 GSR tomorrow 14:00–16:00 for project work with my
saved co-booker Alex” authorizes the agent to search, prepare the exact review,
optionally validate, submit and return the actual FBS status/reference and image.
It asks for missing required details or ambiguous contacts. A search-only request
does not authorize a booking. Submitting a booking accepts FBS's displayed
acknowledgement/declaration.

`SAGASU_MCP_CONFIRMATION=human` instead requires a client-rendered MCP confirmation
form through `confirm_review`; `submit_review` is not exposed. Clients without
form elicitation can search and validate in this mode but cannot submit. Restart
the server after changing the mode. `server_status` reports the active policy,
negotiated form capability, current Singapore time, pending reviews and operations.

## Agent workflow

1. Call `server_status`. Follow its `submission_tool`; all booking times are SGT.
2. Use `search_options`, `get_search_preferences` and `list_cobookers` as needed.
3. Call `search_rooms` and keep its `operation_id`.
4. Use `wait_operation` (default 20 seconds, maximum 30). Reuse that ID until
   terminal. Timeout or caller cancellation does not cancel the browser operation.
5. On `awaiting_mfa`, call `authentication_status`. Relay the current Microsoft
   prompt and matching number privately to the user, who approves on their phone.
   Wait again afterward. Never request passwords or approve MFA on their behalf.
6. On successful search, call `available_rooms` for rooms free for the **entire**
   interval, ranked by free time and then room name. Follow `next_offset`.
7. Prepare `booking_review` with the exact room, purpose and either a saved
   `cobooker_id` or a full SMU `co_booker` email/unique saved name.
8. Optionally call `validate_booking` and wait. It exercises the form without
   final submission and leaves the review available.
9. Call `submit_review` in agent mode, or `confirm_review` in human mode.
10. Wait for the write, inspect its actual `fbs_status`, and call
    `operation_receipt` if `receipt_available` is true.

Cancellation: `list_bookings` → wait → `cancellation_review` → submission tool →
wait. Task approval: `list_tasks` → wait → optional `task_details` →
`task_approval_review` → submission tool → wait. Approval of your task does not
prove the whole booking is confirmed; inspect the returned status.

The `sagasu://workflow` MCP resource contains these sequences and recovery rules.
The `book_room` MCP prompt takes a `request` string and guides the agent through
the workflow. Neither reading a resource nor expanding a prompt submits an action.

## Searches and saved preferences

`search_rooms()` uses saved defaults, initially SCIS1 group study rooms on levels
2–4 for two hours starting at the next strictly future half-hour. A rolling window
that would cross midnight advances to midnight on the next day.

Use `preferences` for a partial, **one-off** override without changing saved defaults:

```json
{
  "preferences": {
    "date_raw": "2099-11-01",
    "start_time": "14:00",
    "duration_hrs": 2,
    "floors": ["Level 2", "Level 3"]
  }
}
```

Replace the example with your actual future date. Omitted fields retain defaults;
explicit `null` clears that saved override for this search. A complete `config`
uses `date`, `start_time` and `end_time` with optional filters. Supply `config` OR
`preferences`, never both. Dates accept ISO `YYYY-MM-DD`; original `DD-Mon-YYYY`
(`config.date`) and `DD Month YYYY` (`preferences.date_raw`) also work. Times use
half-hour `HH:MM` boundaries and must form a future same-day interval.

`set_search_preferences` persists only supplied fields; `reset_search_preferences`
restores defaults and keeps contacts. Supported filters include buildings, floors,
facility types, capacity and equipment; obtain exact values from `search_options`.
`save_cobooker` upserts by normalized SMU email or updates an exact ID;
`delete_cobooker` removes only that contact. Names can repeat, so use IDs when
ambiguous. These tools modify local preferences, never FBS bookings. Reviews
snapshot contact/search details and are not changed by later preference edits.

## Operations, receipts and recovery

Jobs progress through `queued`, `running`, `awaiting_mfa`, `succeeded`, `failed`,
`cancelled` or `outcome_unknown`. Status includes `terminal`, `next_tool` and
`poll_after_ms`. `operation_result` paginates rooms/bookings/tasks (limit 1–50).
Keep FBS's own `total`/`complete` fields when reporting partially read listings.
`cancel_operation` stops queued jobs or running reads/validation; it refuses to
interrupt running writes. `discard_review` abandons an unsubmitted review.

Repeated `submit_review` calls with the **same review ID** return the original
operation while retained, including failed/uncertain outcomes. They do not submit
again. Another review is a new action. Backend writes recheck exact rooms or
references, enforce future starts, and verify FBS's actual result. **Pending
Confirmation** and **Pending Cancellation** are not final confirmations.

An `outcome_unknown` or an interrupted write requires reconciliation in My
Bookings/Task List before creating another review. Do not infer failure from a
missing row or expired ID. No action is automatically replayed after restart.
Reviews expire after ten minutes; up to 32 operations and 32 reviews are retained.
Completed operations expire after one hour and may be evicted sooner at capacity.

Receipts are in-memory PNGs limited to 4 MiB. `operation_receipt` returns the image;
`booking_receipt` remains an alias. Check `receipt_kind` for task details versus
approval acknowledgement. Missing screenshots do not undo verified actions.
Authentication prompts are also transient and cleared on completion/cancellation;
ordinary operation statuses and logs do not include matching numbers.

Private logs contain operation/step IDs, timings and sanitized exception types.
Run `sagasu doctor` for setup problems; see [CLI logging](cli.md). Tests use fake
backends/local browser fixtures and do not submit real bookings or approvals.

## Development checks

```sh
python -m pip install -e '.[dev]'
python -m unittest discover -v
pre-commit run --all-files
```
