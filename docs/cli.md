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
| `sagasu login` | Open the bot's own profile and wait for manual SMU password/MFA, then save the authenticated session. Does not require a Telegram token. |
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
- `SMU_LOGIN_EMAIL`: optional username prefill; passwords and MFA stay manual.

Session-only cookies are atomically saved as `.sagasu-session.json` inside the browser profile with mode 0600, then restored before FBS navigation. Original expiration values are preserved; SMU can still expire/revoke the session and require MFA. Browser profile and Telegram state contain private data and must not be committed or uploaded. `login`, `run`, and session checks take the same kernel lock to prevent concurrent profile use. Stop the bot before renewing login or running `doctor --session`. The persistent lock file is intentionally not deleted; the operating system releases ownership when the process exits.

The CLI does not reuse the removed public scraper's profile. If you encounter FBS/Microsoft login after migration, complete it in the bot's own Chrome window.

## Rattles attribution

The small Python port in `bot/_vendor/rattles.py` uses the frame/timing model and selected presets from [vyfor/rattles](https://github.com/vyfor/rattles), pinned to revision `6eabf671ee86ddd773f458b9c25ef9d3f00ae53b`. It is a subset, not a full Rust binding. The preset origins include [sindresorhus/cli-spinners](https://github.com/sindresorhus/cli-spinners). Both MIT license notices are included under `bot/_vendor/licenses` and in built wheels.
