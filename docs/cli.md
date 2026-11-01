# Sagasu CLI

Sagasu runs a personal Telegram bot. The CLI handles setup, diagnostics, local Microsoft login, and runtime management; it never submits a room booking. Booking still requires explicit confirmation in Telegram.

## Install and start

From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m playwright install chromium
sagasu --help
```

The editable install adds `sagasu` to the active environment. `python -m bot` and `.venv/bin/python -m bot` provide the same interface without relying on shell activation. Python 3.11+ is required. There is no Rust build or Node dependency.

```sh
sagasu config init
# Edit .env privately: set BOT_TOKEN and your numeric TELEGRAM_OWNER_ID.
sagasu doctor
sagasu run
```

`config init` creates a template with mode 0600 and refuses to overwrite an existing file. Existing `.env` files are preserved. The default browser channel for the CLI is Chromium; pass `--browser chrome` for an installed Google Chrome channel. Playwright's branded Chrome installer does not support Fedora; bundled Chromium may use a fallback Linux build and still needs the host libraries required by that build.

`python -m bot.bot` and `python bot/bot.py` still start the bot when run without arguments. They also accept CLI commands/help. `make start`, `make doctor`, `make login`, and `make test` work from an activated environment. On startup, the bot checks token syntax and reports rejected tokens without showing the token or a raw traceback. It verifies Telegram connectivity before reporting that it is connected.

## Commands

| Command | Behavior |
| --- | --- |
| `sagasu run` | Start Telegram polling; Ctrl+C stops the bot and closes its shared browser. |
| `sagasu doctor` | Check Python/dependencies, config, writable data paths, isolated browser startup, FBS/Telegram HTTPS reachability, and Telegram token acceptance. Sends no Telegram messages. |
| `sagasu doctor --offline` | Check local setup/browser startup; skip service requests and token verification. |
| `sagasu doctor --session` | Also check the actual saved FBS session with the bot stopped. No manual login or credential submission. |
| `sagasu doctor --json` | Structured report on stdout, exit 1 if a health check fails. |
| `sagasu login` | Open the bot's own profile and use configured credentials (or manual entry) and wait for MFA approval, then save the authenticated session. Does not require a Telegram token. |
| `sagasu config init` | Create a private configuration template without overwriting anything. |
| `sagasu config show --json` | Inspect configuration with token and owner ID redacted. |
| `sagasu paths` | Show environment-file, browser-profile, and Telegram state paths. |
| `sagasu --version` | Show the installed Sagasu version. |

A successful network check establishes reachability, not authentication or calendar correctness. A syntactically valid token is not proof Telegram accepts it; online doctor checks acceptance separately. Doctor reports session validity only with `--session`. If FBS is unreachable, it skips opening your saved profile and reports that distinction. Offline mode cannot be combined with `--session`.

Default doctor probe timeout is 10 seconds; override with `--timeout 1..60`. Independent FBS and Telegram probes run concurrently. Browser startup and token/session checks are separate bounded operations, so the whole command may take longer than one probe timeout. Login defaults to 600 seconds, configurable with `login --timeout 1..1800`. An FBS outage is reported separately from an expired session; increasing MFA timeouts does not fix an unreachable site.

## Output and logging

Common options work before or after the command:

```sh
sagasu --env-file /path/to/private.env doctor --browser chromium
sagasu run --no-spinner --no-color
sagasu run --verbose --log-file artifacts/sagasu.log
sagasu doctor --timeout 5 --json > artifacts/doctor.json
```

- `--spinner dots|line|none` chooses the progress style. `--no-spinner` disables animation.
- Animation is enabled only on a terminal, not in redirected output, CI, or `TERM=dumb`. Non-Unicode streams fall back to an ASCII spinner.
- `--no-color` and the `NO_COLOR` environment variable disable colors.
- `--quiet` prints only warnings/errors; structured JSON still prints.
- `--verbose` enables redacted debug logs. Raw authentication tracebacks remain suppressed.
- `--log-file PATH` enables rotating logs (1 MB, three backups). The current log is created with mode 0600. Tokens, Telegram token URLs and URL query strings are redacted.
- Human progress/log output goes to stderr. Configuration values, paths, version/help and JSON reports go to stdout.

Exit codes: 0 success/help, 1 runtime or doctor failure, 2 configuration/usage error, 130 keyboard cancellation. `run_polling` handles normal Ctrl+C shutdown itself and may return success after a clean stop. Closing active browser operations is part of shutdown; let the bot finish shutting down before restarting it.

## Configuration and private state

In a checkout, `.env`, `bot/.fbs-profile`, and `bot/bot_state.pickle` remain the defaults. When installed as a wheel outside a checkout, state uses the user's application-state directory and the default environment file is `.env` in the current directory. `sagasu paths` reports the resolved locations.

Precedence: explicit CLI browser override, then exported environment variables, then the selected environment file, then defaults. Relative profile/state paths are resolved against that environment file's directory. `--env-file` selects an explicit file regardless of working directory. Variable interpolation in `.env` is disabled so credential values are treated literally.

Relevant settings:

- `BOT_TOKEN`: BotFather token; required for run, not for login or configuration commands.
- `TELEGRAM_OWNER_ID`: your positive numeric Telegram user ID. If unset, the bot accepts other users; doctor/startup warn about this.
- `SAGASU_BROWSER_CHANNEL`: defaults to `chromium` for CLI commands.
- `SAGASU_BROWSER_PROFILE`: private browser profile override.
- `SAGASU_STATE_PATH`: private Telegram persistence-file override.
- `SAGASU_AUTH_TIMEOUT_MS`: interactive authentication timeout, 1000–1800000 ms.
- `SMU_LOGIN_EMAIL`: optional username prefill.
- `SMU_LOGIN_PASSWORD`: optional password autofill, read from private configuration; requires an email and owner ID. Omit it to type the password yourself.
- `SAGASU_HEADLESS=true`: run Chromium without a desktop. Saved sessions and automatic credentials work in this mode; `doctor --session` still never submits credentials.

Session-only cookies are atomically saved as `.sagasu-session.json` inside the browser profile with mode 0600, then restored before FBS navigation. Original expiration values are preserved; SMU can still expire/revoke the session and require MFA. Browser profile and Telegram state contain private data and must not be committed or uploaded. `login`, `run`, and session checks take the same kernel lock to prevent concurrent profile use. Stop the bot before renewing login or running `doctor --session`. The persistent lock file is intentionally not deleted; the operating system releases ownership when the process exits.

The CLI does not reuse the removed public scraper's profile. If you encounter FBS/Microsoft login after migration, complete it in the bot's own Chrome window.

## Booking in Telegram

Send `/start` and choose **Find available rooms** (use **Search settings** or `/config` first to adjust the date/time). Open a room and tap **Book this room →**, or choose **Auto-pick best room**.

The default search lasts two hours and starts at the next strictly future half-hour in Singapore time: at 13:24 it searches 13:30–15:30; at 13:30 it searches 14:00–16:00. Defaults are resolved again for each search rather than saved as a fixed time. `/config` and `/status` show the current resolved window. Explicit date, time and duration choices are preserved; choose **Rolling date** / **Next half-hour (rolling)** in their pickers or **Reset to defaults** to restore automatic timing. Start-time choices include every half-hour. Since the current FBS search covers one date, a rolling window that would cross midnight advances to 00:00 on the following date instead of shortening the duration.

Past starts are rejected before scraping, opening room details or asking booking questions. Settings display a warning immediately for a manually selected past start. The bot rechecks after long searches and during questions so a window that expires while you are using the bot cannot produce a new booking review. It clears expired results and drafts while retaining saved co-bookers.

Sagasu asks for the booking purpose, then the co-booker. Pick a saved name or send a full SMU email address. New co-bookers can be used once or saved with a name for future bookings. `/cobookers` also lets you add/remove saved people independently of a booking. Saved contacts belong to your Telegram user and survive bot restarts in the private Telegram state file; `/logout` keeps them.

Review the exact room, date, time, purpose and co-booker. **Validate only** checks the FBS form without submitting. **Confirm & book** performs the actual booking and accepts the displayed FBS acknowledgement/declaration. A review expires after 10 minutes. Questions expire after 15 minutes of inactivity; `/cancel` ends questions and discards pending reviews while keeping saved contacts. Back buttons let you change answers before review.

Validation and booking use FBS’s specific-facility search, select the exact room, and read only that room’s calendar. Initial room discovery still uses your configured building/floor/type filters. A missing target never falls back to other rooms.

The requested start must be in the future in Singapore time (SGT), even when the room is empty and the end time has not passed. Sagasu checks before opening the browser, after the availability search, and before final confirmation. If the start has passed, choose a later time in `/config` and search again. FBS policy rejections are reported directly and are not retried as lost calendar selections.

After submission is verified in My Bookings, Sagasu opens **VIEW** for that exact reference and sends a screenshot of the booking details to the requesting user's private Telegram chat. The caption includes the reference, room, time and FBS status. The image is held in memory, with no receipt files added to Telegram persistence or the repository. If Telegram cannot accept it as a photo, Sagasu tries the original PNG as a document. Screenshot or delivery failure keeps the booking marked as verified and explains how to find the reference in FBS; it never triggers another booking. **Validate only** does not create or send a receipt.

Use `/tasks` (alias `/tasklist`) or **FBS Task List** in `/start` to check for requests awaiting your action. This feature is available in private chats and uses the same session/MFA flow as searches. It lists each task's reference, facility, dates/times, request type, requester and status. If FBS paginates the list, the response states how many items were read and directs you to FBS for the remainder. Approval and rejection actions remain in FBS. An unreadable or failed check is reported as a failure, never as an empty list.

Availability uses the calendar’s full dates as well as its clock labels: an unavailable period from midnight to the following midnight stays blocked. Booking selection interpolates half-hour boundaries within FBS’s hourly headers and scrolls the target into view. Sagasu verifies FBS retained the exact selection before clicking Make Booking and retries a lost selection once. Validation checks the selected room and full start/end datetimes on the FBS booking form. If final confirmation is attempted but cannot be verified in My Bookings, Sagasu disables that review and asks you to check FBS before retrying.

`/book` starts the questions for your selected room. The old `/book purpose | email` shortcut remains supported. Room/time details are captured when the questions start, and old question buttons cannot modify a new setup. Favorite selection uses stable contact IDs, so removing one contact does not make an old button choose someone else. If a co-booker shares the same email as an existing saved person, saving updates that person's name instead of creating a duplicate.

## Remote sign-in with phone approval

Configure these on the host running Sagasu, using a private `.env` or the host's secret store:

```dotenv
BOT_TOKEN=your-bot-token
TELEGRAM_OWNER_ID=your-numeric-id
SMU_LOGIN_EMAIL=your.name@smu.edu.sg
SMU_LOGIN_PASSWORD='your-private-password'
SAGASU_BROWSER_CHANNEL=chromium
SAGASU_HEADLESS=true
```

Keep `.env` readable only by your account (`chmod 600 .env` on Linux/macOS). Quote password values containing `#` or spaces; dotenv interpolation is disabled. Passwords are excluded from config output and redacted from logs. Sagasu disables Chrome password saving and automatic password-manager sign-in in its private profile before launch. Never send passwords or one-time verification codes to Telegram. Browser profiles and saved sessions are also private credentials.

Start the bot with `sagasu run`. Open its **private Telegram chat** and send `/start` before searching; Telegram must allow the bot to message you. When FBS needs renewal, Sagasu fills credentials only on supported HTTPS Microsoft sign-in origins, chooses password sign-in when offered, and selects Microsoft Authenticator push approval. It sends a new private message to `TELEGRAM_OWNER_ID`, including Microsoft's [matching number](https://learn.microsoft.com/en-us/entra/identity/authentication/how-to-mfa-number-match). Open Authenticator and approve the sign-in you just initiated. Search or booking validation continues after Microsoft returns to FBS. Numbers that appear late or change generate an updated notification; identical prompts are sent once per login attempt. Numbers are kept out of shared search-progress messages.

`sagasu login` can also use this flow when a valid bot token and owner ID are configured. Stop a running bot first because both commands lock the same profile. Without headless mode or configured credentials, the browser still allows manual login. `doctor` never sends MFA notifications or enters credentials.

The host needs Python, Playwright's Chromium and its operating-system libraries, network access to FBS/Microsoft/Telegram, and persistent storage for the browser profile. Only one Sagasu instance should use the profile and bot token. A desktop is unnecessary for an Authenticator push flow; a CAPTCHA, forced password change, unsupported verification method, or SMU policy requiring a managed device can still require interactive access. Password rejection/denial stops the attempt; Sagasu does not repeatedly submit passwords or automatically resend MFA pushes. If Microsoft redirects back to an empty email screen, Sagasu restores the email once. A repeated reset or a new password challenge after submission stops with a private diagnostic instead of waiting indefinitely. Authentication waits for `SAGASU_AUTH_TIMEOUT_MS` (10 minutes by default), then reports failure; start a new search when ready.

The supplied Unicode banner appears on interactive `run` startup. Quiet mode and redirected output omit it; narrow or non-Unicode terminals show `SAGASU`.

## Rattles attribution

The small Python port in `bot/_vendor/rattles.py` uses the frame/timing model and selected presets from [vyfor/rattles](https://github.com/vyfor/rattles), pinned to revision `6eabf671ee86ddd773f458b9c25ef9d3f00ae53b`. It is a subset, not a full Rust binding. The preset origins include [sindresorhus/cli-spinners](https://github.com/sindresorhus/cli-spinners). Both MIT license notices are included under `bot/_vendor/licenses` and in built wheels.
