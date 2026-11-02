# Sagasu MCP

Sagasu exposes its existing FBS browser backend to local agents over **stdio**. It can search rooms, validate and submit reviewed bookings, inspect My Bookings and Task List, cancel reviewed bookings, approve reviewed tasks, renew the login session, and return details/acknowledgement screenshots. Telegram remains available as another interface.

This is a personal, single-user server. It has no public HTTP listener, OAuth service, OpenAI API dependency, or hosted deployment. Access follows access to the local process and account. Connect only trusted MCP clients: their model/provider can receive booking details, co-booker addresses, and receipt images returned by tools. Passwords, cookies, and Telegram tokens stay in the host's private configuration.

## Install

From the repository root, using Python 3.11 or later:

```sh
source .venv/bin/activate
python -m pip install -e '.[mcp]'
python -m playwright install chromium
sagasu mcp --help
```

With uv instead of pip: `uv pip install --python .venv/bin/python -e '.[mcp]'`.

The optional extra uses the official Python MCP SDK 2.x (tested with 2.3.0). Telegram-only installation does not require the extra. For the full test suite install `.[dev,mcp]`.

Use the existing private `.env`, browser profile and saved session. `SMU_LOGIN_EMAIL` and `SMU_LOGIN_PASSWORD` enable autofill. For remote/headless renewal set `SAGASU_HEADLESS=true`, `BOT_TOKEN`, and `TELEGRAM_OWNER_ID`; open the bot's private Telegram chat and send `/start` beforehand. The owner approves Microsoft Authenticator on their phone. Without Telegram configuration, a previously authenticated session can still be used; interactive headed login is also available via `sagasu login` while MCP is stopped.

## Connect Codex

Add this entry to your private `~/.codex/config.toml`, adjusting all paths for the checkout:

```toml
[mcp_servers.sagasu]
command = "/absolute/path/to/sagasu/.venv/bin/python"
args = ["-m", "bot", "mcp", "--env-file", "/absolute/path/to/sagasu/.env"]
cwd = "/absolute/path/to/sagasu"
startup_timeout_sec = 30
tool_timeout_sec = 60
```

The agent starts the subprocess; do not start a second `sagasu mcp` in another terminal. Running it manually waits for MCP protocol input, which can look like an idle terminal. Banners/progress never go to stdout in this mode; stdout carries only MCP messages. Redacted logs go to stderr, with optional `--log-file /private/path/sagasu.jsonl --log-format json` arguments.

For another stdio-compatible client, the equivalent configuration is:

```json
{
  "mcpServers": {
    "sagasu": {
      "command": "/absolute/path/to/sagasu/.venv/bin/python",
      "args": ["-m", "bot", "mcp", "--env-file", "/absolute/path/to/sagasu/.env"],
      "cwd": "/absolute/path/to/sagasu"
    }
  }
}
```

Each client's config location and support for `cwd` vary. Alternatively use the absolute `.venv/bin/sagasu` executable with `args = ["mcp", "--env-file", "/absolute/path/to/.env"]`; that works independently of the client's working directory. Keep credentials out of MCP arguments and committed config files. Existing environment variables override values in the selected `.env`.

OpenAI's [official MCP setup documentation](https://learn.chatgpt.com/docs/extend/mcp) describes shared Codex configuration and stdio transport support. This change supplies an example; it does not change your global client configuration.

## Keep Telegram available

Add `--telegram` to the MCP command arguments to run Telegram polling and MCP inside **one process**. Both use the same backend and browser lock. Stop any existing `sagasu run` first. The profile lock rejects independent processes sharing the same profile, including `login` and `doctor --session`.

The combined bot lasts only as long as the MCP client keeps its server subprocess running. Closing the agent stops it. A standalone Telegram bot still runs with `sagasu run`; stop that process before starting MCP. Multiple clients launching separate MCP subprocesses cannot share the same live profile. A shared authenticated network service is outside this implementation.

MCP-initiated MFA notifications are sent privately to the configured Telegram owner. They are separate from Telegram's editable control panel; they never contain the password. MCP operation status says to check Telegram without returning the challenge number.

## Tool flow

| Tool | Behavior |
| --- | --- |
| `server_status` | Version, confirmation requirements, notification availability, retained operation IDs; no FBS request. |
| `login` | Start actual FBS session verification/renewal. |
| `search_rooms` | Start a search. Omit `config` to use shared personal preferences, initially rolling two-hour SCIS1 GSR defaults. Explicit config is a complete one-off override using `DD-Mon-YYYY` and half-hour `HH:MM` times in SGT. |
| `search_options` | List the same supported filter values, time choices, durations and capacity codes as Telegram. |
| `list_cobookers`, `save_cobooker`, `delete_cobooker` | Read, add, edit or remove saved contacts shared with the configured Telegram owner. |
| `get_search_preferences`, `set_search_preferences`, `reset_search_preferences` | Read, patch or reset shared search defaults without contacting FBS or submitting a booking. |
| `list_bookings`, `list_tasks` | Read FBS account data. Tasks include observed `can_approve`/`can_view` capabilities; listing does not submit an action. |
| `operation_status` | Poll progress without holding an MCP call open during scraping or MFA. |
| `operation_result` | Read a result, with `offset` and `limit` (1–50, default 20) for room/booking/task rows. Preserve FBS `total`/`complete` coverage information. |
| `booking_review` | Choose an exact free room from a successful search and purpose, plus either `cobooker_id` or `co_booker` (full SMU email or a unique saved name). Returns an immutable review lasting ten minutes. |
| `cancellation_review` | Choose an exact cancellable booking reference from a successful My Bookings operation, and supply a reason. |
| `task_approval_review` | Choose one uniquely listed, actionable task reference from a successful Task List operation. Prepare a review without approving. |
| `task_details` | Open that task's read-only VIEW action and capture its reference-verified booking details as a background operation. |
| `get_review`, `discard_review` | Inspect or discard an unsubmitted review. |
| `validate_booking` | Exercise the complete booking form for a review without final submission. Leaves the review available. |
| `confirm_review` | Request human confirmation through MCP form elicitation, then enqueue the approved action once. |
| `cancel_operation` | Stop reads/validation or a queued operation. Refuses to interrupt a running write. |
| `operation_receipt` | Return an operation's in-memory PNG, including task details and approval evidence. Inspect `receipt_kind` in the operation result. |
| `booking_receipt` | Compatible screenshot retrieval alias; also accepts task operation IDs. |

Example agent request: “Search SCIS1 group study rooms for the next two hours. Show me the available rooms, then prepare a booking for my chosen room and co-booker. Validate it before asking me to confirm.”

Start a search/read and keep its `operation_id`. Poll at a reasonable interval (for example 2–5 seconds); on `succeeded`, read `operation_result` and follow `pagination.next_offset` as needed. For booking, use `booking_review` with that search ID, then optionally `validate_booking`, then `confirm_review`. Poll the resulting write operation and request `booking_receipt` if `receipt_available` is true. Cancellation follows the same pattern from `list_bookings` and `cancellation_review`. Backend submission always rechecks the exact room or reference.

For task approval: `list_tasks` → poll/read results → optional `task_details` and `operation_receipt` → `task_approval_review` → `confirm_review` → poll/read the approval result → `operation_receipt`. The backend rechecks the exact task's reference, room, time, request type, requester, and status, clears other checkbox selections, verifies only the reviewed task is selected, and clicks Approve Selected once. It requires a positive task status or a positive FBS acknowledgement together with removal of that exact task; disappearance alone is not success. Ambiguous outcomes disable that review and require manual reconciliation in FBS Task List/the booking before retrying. Task rejection is not implemented.

Approval means **your task was approved**, not that every participant has confirmed the booking. The result reports `task_status` and `booking_status_verified: false`. Screenshots use `receipt_kind: booking_details` when the exact booking is accessible in My Bookings, or `approval_acknowledgement` for the captured Task List acknowledgement. Receipt failure preserves a verified approval. The live Approve Selected control was inspected, but pending-task row actions, acknowledgement wording, and a full live approval still need verification when a pending request is available; the automated tests use local browser fixtures.

`confirm_review` does not accept a model-supplied `confirmed=true` argument. Its approval parameter is supplied by the MCP client's human confirmation form. Decline, cancel, false, expiry, or missing form support results in no submission. Clients without form elicitation can search, inspect, and validate; use Telegram to complete a write. Client-side tool approval alone does not replace the form. Never configure an elicitation callback that automatically approves writes.

The server uses SDK resolvers so confirmation works with both legacy and current MCP protocol connections. The MCP client is trusted to present the question to the human and faithfully return their answer.

## Saved co-bookers and shared search preferences

Telegram and MCP use a private SQLite store, by default `personal.sqlite3` beside the Telegram state file. `SAGASU_PREFERENCES_PATH` overrides it; relative paths resolve against the selected `.env`. Both processes must use the same store path and `TELEGRAM_OWNER_ID` to share the owner's data. MCP without an owner uses a separate local profile; other Telegram users have separate profiles. The database is created with mode 0600 and contains names, email addresses, and preferences; keep it private alongside the other runtime state. `sagasu paths` reports its resolved path. No OpenAI credentials are needed for these tools.

Existing valid saved contacts and search settings are imported once from the configured private Telegram persistence file. Standalone MCP imports only the configured owner, so it can use existing data before the next Telegram interaction. Telegram also migrates each user's legacy fields on their first interaction. Existing contact IDs are preserved when valid; duplicate emails are normalized. Later edits and deletions in the shared store take precedence, so stale Telegram state does not resurrect removed contacts or reset preferences. The legacy persistence file is retained for conversation state. Migration reads only that configured trusted local file, never a client-supplied path.

Use `list_cobookers` to obtain stable IDs. `save_cobooker(name, email)` updates an existing contact with that normalized email, or creates one. Supply `cobooker_id` to edit its name/address explicitly. Names may repeat; an ambiguous name cannot be used to book, so choose an exact ID. Deleting a contact affects future selection and does not cancel any booking. Contacts are limited to 200 per user and list results paginate. Changes made through MCP appear when Telegram next renders its contacts/settings, and Telegram saves are immediately available through MCP.

`set_search_preferences` patches only supplied fields; omitted fields remain unchanged, and explicit `null` removes an override. Supported fields mirror Telegram: `date_raw` (e.g. `03 November 2026`), `start_time`, `duration_hrs`, `room_capacity`, `buildings`, `floors`, `facility_types`, and `equipment`. Obtain exact codes and filter names from `search_options`. An empty filter list uses the same default behavior as Telegram. For example:

```json
{
  "preferences": {
    "date_raw": null,
    "start_time": null,
    "duration_hrs": 3,
    "floors": ["Level 2", "Level 3"]
  }
}
```

This restores rolling dates/starts while saving a three-hour duration and preferred floors. `get_search_preferences` returns both saved overrides and the current effective SGT search window. Fixed dates/start times remain fixed and can expire; actual searches reject past starts. `reset_search_preferences` restores default search behavior and keeps contacts. Settings and contacts persist across restarts, while operation IDs/reviews/receipts still expire as described below. Preference tools can run without form elicitation; they do not submit FBS actions. Actual booking, cancellation and approval retain their explicit confirmation requirement.

Example agent request: “Save Lexuan as a co-booker using this SMU email. Remember SCIS1 floors 2 and 3 with a rolling three-hour window. Search using those settings, then prepare a booking with Lexuan for me to review.” Contact emails and search settings are copied into each prepared review; later personal-data changes do not silently alter an existing review.

## Outcomes, lifetime, and debugging

Operations report `queued`, `running`, `awaiting_mfa`, `succeeded`, `failed`, `cancelled`, or `outcome_unknown`. A successful operation means the backend finished; inspect its actual `fbs_status`. A booking marked **Pending Confirmation** is still awaiting FBS confirmation. Cancellation may also return a pending outcome instead of a final cancellation.

An uncertain write is not retried. Inspect My Bookings by reference before preparing another attempt. Expired/missing IDs after restart do not prove a write failed. There is no durable write journal or automatic replay. The process tries to let active writes finish during shutdown, but an MCP client can force-kill its subprocess sooner; use My Bookings to reconcile any interrupted attempt.

The server retains at most 32 operations and 32 reviews. Completed operations/receipts expire after an hour and older completed records can be evicted sooner when full. Reviews expire after ten minutes and are consumed before a write is queued. Images stay in memory and are limited to 4 MiB; a missing screenshot does not undo a verified booking. Save useful receipts in your client before disconnecting. Active jobs fill a bounded queue rather than growing memory without limit.

`agent.operation_started` links the public `agent_operation_id` to the existing logging `operation_id`; subsequent browser/login/calendar events share that trace. `agent.operation_failed` records the exception type without raw values. Use the [CLI logging guide](cli.md#output-and-logging), and run `sagasu doctor` for browser/network/configuration issues. Session checks require MCP/Telegram to release the profile first.

## ChatGPT web / private tunnels

ChatGPT web cannot directly launch this local stdio command. OpenAI's [Secure MCP Tunnel documentation](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) supports forwarding to a local stdio server using `tunnel-client`. That route needs a Platform tunnel, the appropriate workspace permissions, and a restricted runtime key, configured separately; none are provisioned by Sagasu.

Use the absolute Sagasu MCP command above as the tunnel's local MCP command, and keep its host and tunnel client online. The tunnel has a 120-second request deadline, so Sagasu returns operation IDs for long scraping/MFA work instead of holding the tool call open. Verify that the ChatGPT client/tunnel supports form elicitation before attempting writes; otherwise finish those actions through Telegram. An end-to-end ChatGPT tunnel connection has not been tested here. MCP does not keep the browser running while its host is asleep or powered off.

## Tests

```sh
python -m pip install -e '.[dev,mcp]'
python -m unittest discover -v
```

Tests exercise a fake FBS backend, actual MCP client/server exchanges, and local Chromium DOM fixtures. They cover a subprocess stdio handshake, user confirmation/refusal, one-shot writes, pagination, MFA status, receipt image content, cancellation/approval safety, exact checkbox selection, wrong/stale acknowledgement handling, expiry, and queue bounds. Install Chromium before running the full suite. Tests never create/cancel/approve live FBS requests or send Telegram prompts.
