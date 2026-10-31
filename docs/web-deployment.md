# Public availability website

Sagasu publishes a read-only daily snapshot of campus-wide group study rooms for today plus six days, 08:00–22:00 Asia/Singapore. The website filters public JSON locally. Booking remains in the separate personal Telegram bot with explicit confirmation.

## Local login and first live scrape

Use Python 3.12 or newer and the repository requirements in a virtual environment. Run commands from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m bot.scraper_login
```

Chrome is the default browser. To use bundled Chromium (including on Fedora), install it and set the channel for **both** login and scraping:

```sh
.venv/bin/python -m playwright install chromium
export SAGASU_BROWSER_CHANNEL=chromium
.venv/bin/python -m bot.scraper_login
```

Complete Microsoft login and MFA in the browser. If a native passkey request appears, cancel it yourself and select the appropriate login method. Choosing **Use my password** does not remove MFA. No SMU password environment variable is required or read by this authentication path. The scraper does not automatically enter a username, password, approve MFA, or record authentication screenshots. Do not paste credentials or codes into chat or the terminal.

The login command waits up to ten minutes (override with `--timeout SECONDS`, maximum 1800), verifies visible FBS search controls in the FBS content frame, closes the browser, and saves its session. A navigation shell alone does not count as success. Closing the browser or reaching the deadline fails login. Pressing Enter is not required and does not resume a failed login.

The dedicated default profile is `artifacts/scraper-profile`, separate from the Telegram bot's `bot/.fbs-profile`. `artifacts/` is ignored by Git. An optional `SAGASU_SCRAPER_PROFILE` can point outside the repository or under its ignored `artifacts/` directory; it must not overlap the bot profile. This directory contains private authentication state: never commit or upload it. Chrome does not reliably restore session-only cookies after shutdown, so the scraper also saves those cookies as `.sagasu-session.json` inside this profile with owner-only permissions (0600), after FBS readiness is verified. It restores them before navigation without changing expiry values; the server still enforces session expiry. Persistent cookies and other browser storage remain in the Chrome profile. This private file must never be included with the two public JSON files. A nonblocking OS file lock prevents login and scraper processes from opening it concurrently. The kernel releases the lock after a process exits; do not delete the lock file. Chrome also protects against opening a profile already in use.

After login succeeds:

```sh
.venv/bin/python -m bot.scrape_daily --verbose
.venv/bin/python scripts/build_site.py --data artifacts/daily
.venv/bin/python -m http.server 8000 --directory dist
```

The scraper reuses the saved profile, defaults to headless, and never submits credentials or enters the booking path. `--headed` shows the scrape without pausing for login. Missing/expired sessions fail on a login redirect; FBS readiness waits are bounded to 90 seconds per search and the entire seven-day scrape to 20 minutes. Renew sessions with `python -m bot.scraper_login`, then retry once. Do not repeatedly submit credentials or run the old fresh-profile/password route.

Success writes `snapshot.json` and `status.json` to `artifacts/daily` (override with `--output`). Failure exits nonzero and writes a fixed, sanitized failure status while retaining the previous complete snapshot. Check the exit status and `status.json`; an existing snapshot does not prove the current attempt succeeded.

Before enabling publishing or scheduling, confirm a **real seven-day scrape** succeeds and compare room coverage and intervals against live FBS. The strict scraper rejects unreadable events, missing calendar rows and result pagination instead of assuming rooms are free. These conditions must be checked against live FBS after MFA; mocked/synthetic tests do not validate campus-wide completeness. Do not publish an incomplete scrape by disabling strict checks. The live FBS calendar has a grouped resource tree, scroll-rendered row headers, and multiple event lines for overlapping bookings. The public reader explicitly selects every discovered room, matches the complete resource tree against discovery, scans the calendar scroll area for every room's coordinates, and reads every event line. It requires the rendered event count to match the loaded calendar event list and rejects dynamic/server-windowed event loading, unknown events and ambiguous room assignments. The personal bot's calendar reader remains unchanged.

## Static GitHub Pages publishing

GitHub Actions no longer logs into SMU, installs a browser, or reads SMU password secrets. Remove obsolete `SMU_LOGIN_EMAIL` and `SMU_LOGIN_PASSWORD` repository secrets if previously configured. Do not attach a self-hosted Actions runner to this public repository.

The `Publish availability website` workflow checks out the default branch, fetches the `availability-data` branch's public `snapshot.json` and `status.json` when present, and builds Pages. If the data branch does not exist yet, it uses the honest empty state in `web/data`. Select **GitHub Actions** as the Pages source in repository settings. Keep the development branch as the default branch. The `github-pages` environment only needs to allow deployments from the default branch.

Website pushes, manual dispatch from the local publisher, and a daily 22:17 UTC rebuild can trigger deployment. The daily rebuild is only a static publish fallback; it does not refresh FBS data. The orphan `availability-data` branch contains no workflow files, so its pushes cannot launch a workflow by themselves. After publishing, the local command explicitly dispatches `availability.yml` on the default branch with the authenticated GitHub CLI. An unchanged-data retry also dispatches, allowing recovery from a previous dispatch failure. Failed dispatch exits nonzero even if the data push succeeded; retry the publish command or dispatch manually.

After a complete live scrape, publish the current attempt:

```sh
.venv/bin/python scripts/publish_snapshot.py --attempt artifacts/daily --push
```

The publisher creates or updates `availability-data` through a temporary Git repository, without changing your checkout, index or staged work. Only `snapshot.json` and `status.json` are committed. It validates seven consecutive days and full interval coverage, reconstructs allowlisted fields and canonical status messages, and refuses a data branch containing other files. A failed attempt keeps the remote's previous complete snapshot and updates status. Pushes are ordinary fast-forward pushes; concurrent changes fail safely, without force-pushing. A first failed attempt can publish an honest empty snapshot and failure status.

An installed and authenticated `gh` CLI with repository push and workflow-dispatch access is required for GitHub publishing, separately from SMU login. For an HTTPS GitHub remote, the temporary checkout uses that CLI as its Git credential helper, avoiding stale Keychain Git credentials. SSH remotes retain their existing SSH authentication. Non-GitHub remotes support data-only publishing without dispatch (also used by local integration tests). Git prompting is disabled and operations have deadlines; diagnostics never include raw Git output. Authenticate GitHub locally with `gh auth login --web` if needed; never put a token in chat or an SMU password in GitHub secrets.

For a complete daily cycle, including publishing a failed-refresh status:

```sh
.venv/bin/python -m bot.refresh_daily
```

Use `--no-push` to refresh local data only. This command serializes the whole scrape/publish cycle with a second local process lock. It exits nonzero for scraping or publishing failures. `scripts/publish_snapshot.py --attempt PATH --retained PATH` still supports local retention without contacting GitHub. Never push an entire working directory, browser profile, `.env`, raw FBS results or diagnostics.

## Local daily scheduling

A live headless seven-day scrape was verified on 31 October 2026: 100 rooms per day for 31 October–6 November. Saved-session reuse worked without further MFA. The public allowlist and a real push of the two sanitized data files were also verified. A manual trigger of the installed LaunchAgent also completed all seven days and pushed successfully (exit status 0). The publisher's explicit workflow dispatch succeeded, and the HTTPS site at https://gabrielongzm.com/sagasu/ was checked against the LaunchAgent-produced public JSON. This does not establish how long SMU keeps the session valid.

Generate the schedule first, then install it after validating local scraping and Git push access:

```sh
.venv/bin/python scripts/schedule_refresh.py
.venv/bin/python scripts/schedule_refresh.py --install
```

The script detects the actual OS and user scheduler. This machine is macOS in the Asia/Singapore timezone with a GUI user `launchctl` domain. It generates an ignored preview under `artifacts/schedule` and installs a user LaunchAgent named `sg.sagasu.availability` for 06:17 Singapore time. It preserves absolute interpreter/repository paths, browser channel and the executable search path needed for Git/`gh`, without copying login credentials. It does not trigger an immediate scrape at installation.

On Linux it prefers a working user systemd manager, using an explicit `Asia/Singapore` calendar timezone and a persistent timer. If no user systemd manager or macOS GUI scheduler is available, it can append a tagged user-cron entry while preserving other entries. LaunchAgent and cron installation require the machine's current UTC offset to be +08:00; their calendar time follows the machine timezone. Regenerate if changing timezone or moving the repository. No supported scheduler means a clear failure; run `python -m bot.refresh_daily` manually instead. No self-hosted Actions runner is involved.

Check the Mac job without printing its environment:

```sh
launchctl list sg.sagasu.availability
```

Run one immediate cycle when needed with `launchctl kickstart gui/$(id -u)/sg.sagasu.availability`. Fixed progress/status output is in ignored `artifacts/schedule/refresh.log`; scheduler startup errors are in `refresh-error.log`. If macOS denies access to the repository on Desktop, grant appropriate local access or move the checkout to an accessible location and regenerate the schedule. Sleeping/offline/logged-out computers cannot guarantee a fresh snapshot at the scheduled time. Session expiry requires another manual `python -m bot.scraper_login` and a refresh; the job never submits stored SMU credentials.

Remove a Mac schedule with `launchctl bootout gui/$(id -u)/sg.sagasu.availability`, then remove `~/Library/LaunchAgents/sg.sagasu.availability.plist`. On systemd, disable with `systemctl --user disable --now sg.sagasu.availability.timer`; unit files are under `~/.config/systemd/user`. For cron, remove only the line tagged `sg.sagasu.availability` with `crontab -e`. Keep other user jobs intact.

## Data and failures

Public data retains the existing allowlist: room names, dates, interval times/statuses, and timestamps. Event descriptions, booker identities, emails, authentication state and raw FBS data are excluded. Treat the site and data branch as public.

- **No successful scrape:** the site shows an empty state, with no invented availability.
- **No matching/free rooms:** a valid complete result may be empty.
- **Authentication failure:** renew the dedicated local session manually, then retry.
- **Layout failure/partial scrape:** retain the last complete snapshot; investigate live DOM completeness before publishing.
- **Old snapshot:** the frontend marks data stale after 36 hours, independently of redeployment.
- **Refresh failure:** a separate status/timestamp reports failure while keeping the last successful data.
- **Network/publishing failure:** the deployed site remains available and becomes stale; publishing failures may prevent a new failure status from reaching it.

## Checks

```sh
.venv/bin/python -m unittest discover -v
node --test tests/*.test.cjs
.venv/bin/python -m compileall -q bot scripts
.venv/bin/pre-commit run --all-files --show-diff-on-failure
```

Automated login tests verify code paths only. Live authentication, saved-session reuse and seven-day coverage were verified separately on this computer. The first overnight scheduled run and long-term session lifetime still require observation. Linux systemd/cron generation is unit-tested, but has not been live-tested on this Mac.
