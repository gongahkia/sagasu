# Sagasu CLI

Sagasu is an agent-first SMU room assistant. The CLI handles private
configuration, diagnostics, Microsoft login and MCP client setup. Python 3.11+
is required. The main install includes the official MCP SDK.

```sh
uv venv
source .venv/bin/activate
uv pip install -e .
python -m playwright install chromium
sagasu config init
sagasu doctor --timeout 30
sagasu login
```

Complete SMU sign-in/MFA in the private browser, then connect an agent using the
[MCP guide](mcp.md). The client launches `sagasu mcp`. There is no background chat
service. `python -m sagasu` is equivalent to the installed `sagasu` command.

## Commands

| Command | Behavior |
| --- | --- |
| `mcp` | Serve a private stdio MCP session. The client launches this process. |
| `login` | Open the private browser, use optional configured autofill, wait for manual sign-in/MFA, and save the session. |
| `doctor` | Check dependencies, configuration, storage, isolated browser startup and FBS HTTPS reachability. No booking/account writes. |
| `doctor --offline` | Check local setup without network requests. |
| `doctor --session` | Also verify the saved FBS session without submitting credentials. Stop MCP first. |
| `smoke --live --json` | Explicit live read-only session, search and bookings check using saved authentication. Does not sign in or submit booking/account forms. |
| `doctor --json` | Structured report; exit 1 if checks fail. |
| `config init` | Create a private `.env` with mode 0600; never overwrite an existing file. |
| `config show --json` | Show resolved configuration without private credential values. |
| `paths` | Show environment file, browser profile, preferences database and action journal paths. |
| `--version` | Show installed version. |

`doctor --timeout 1..60` sets each probe deadline; browser startup/session checks
are separate bounded operations. Login defaults to 600 seconds and accepts
`--timeout 1..1800`. Offline mode and session verification cannot be combined.
Reachability does not prove an authenticated session or calendar correctness.

## Private configuration

A checkout defaults to `.env`, `.sagasu/browser`, `.sagasu/personal.sqlite3`, `.sagasu/actions.sqlite3` and
`artifacts/auth`. A wheel uses the user's application-state directory for private
state and the current directory's `.env`. `sagasu paths` reports resolved locations.
Relative configured paths resolve against the selected environment file.

Precedence: explicit CLI browser override → exported environment variables →
selected `.env` → defaults. Variable interpolation is disabled so passwords are
read literally. Never source `.env` in a shell.

| Setting | Purpose |
| --- | --- |
| `SAGASU_MCP_CONFIRMATION` | `agent` (default) submits reviewed actions on your request without another form; `human` requires an MCP client confirmation form. Restart the server after changing it. |
| `SAGASU_BROWSER_CHANNEL` | `chromium` by default; supported installed Chrome/Edge channels are also accepted. |
| `SAGASU_BROWSER_PROFILE` | Private browser profile path. |
| `SAGASU_PREFERENCES_PATH` | Private SQLite contacts/search preferences path. |
| `SAGASU_JOURNAL_PATH` | Private durable write-outcome SQLite file; defaults to `actions.sqlite3` beside preferences. |
| `SAGASU_AUTH_TIMEOUT_MS` | Sign-in/MFA deadline, 1000–1800000 milliseconds. |
| `SMU_LOGIN_EMAIL` | Optional Microsoft username autofill. |
| `SMU_LOGIN_PASSWORD` | Optional private password autofill; requires an email. |
| `SAGASU_HEADLESS` | `true` to run without a desktop. Saved sessions or configured credentials are needed. |
| `SAGASU_STAY_SIGNED_IN` | Accept Microsoft's stay-signed-in prompt; defaults to true. |

In MCP, authentication challenges are read using `authentication_status` for the
awaiting operation. The agent relays the number/prompt; you approve Authenticator
on your phone. No credential or verification-code input tool is exposed. For
headless standalone login, prompts appear in the local terminal. Unsupported
interactive verification methods still require a visible browser.

Session-only cookies are privately saved/restored inside the profile, retaining
original expiration. SMU can still expire/revoke them. Login, MCP and session
checks share a kernel lock; close the active client before renewing standalone
login. The lock file is intentionally retained; process exit releases ownership.
Do not commit/upload browser profiles, preference databases or authentication
artifacts.

## Output and logging

Common options work before or after a command:

```sh
sagasu --env-file /private/sagasu.env doctor --browser chromium
sagasu mcp --log-format json --log-file artifacts/sagasu.jsonl
sagasu login --no-spinner --no-color
```

Human progress and logs use stderr. JSON command results/configuration use stdout;
MCP stdout carries only protocol messages. Spinners appear only in capable
terminals. `--quiet` hides informational console output, `--verbose` adds redacted
diagnostics, and `--log-file` enables private rotating logs (1 MB, three backups).
`--log-format json` emits JSON Lines with timestamps, operation/step IDs and timing.
`NO_COLOR` or `--no-color` disables colors.

Logs omit passwords, cookies, MFA numbers, booking purpose/co-booker data and
screenshot contents. Exceptions record types and code locations without raw
values, locals or browser tracebacks. Treat logs as private operational data.
Submission verification does not imply a pending booking is already confirmed.

Exit codes: 0 success/help, 1 health/runtime failure, 2 configuration/usage error,
130 keyboard interruption. Let active writes finish before shutting down; inspect
FBS after an uncertain or forcibly interrupted action rather than replaying it.

## Ubuntu host

`deploy/linux/install.sh` installs a private runtime, Chromium and the
`sagasu-host` wrapper on Ubuntu 24.04. It creates no daemon or public listener.
An agent on that host launches `/usr/local/bin/sagasu-host mcp` as the `sagasu`
service account. Configure `/etc/sagasu/sagasu.env` privately; browser/preferences
live in `/var/lib/sagasu`. `sudo sagasu-host doctor` and `sudo sagasu-host login`
use the same paths. Headless MFA prompts come through the connected agent or the
standalone login terminal. The installer does not grant agents sudo access or
provision a remote MCP transport.

## Explicit live smoke check

Stop the MCP server first. After completing `sagasu login`, invoke:

```sh
sagasu smoke --live --json --timeout 180
```

This checks the saved session, searches using current personal defaults, and
reads My Bookings. Output includes counts and FBS listing coverage, not account
rows or credentials. The default overall deadline is 180 seconds (allowed 1–600).
An expired session fails with an actionable hint; the check never submits login
credentials, renews MFA, validates a booking form, books, cancels or approves.
It is never part of the default test suite or CI. See [verification status](testing.md)
for checks that remain open until run against a real SMU account.
