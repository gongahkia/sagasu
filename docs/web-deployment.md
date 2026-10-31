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

Before enabling publishing or scheduling, confirm a **real seven-day scrape** succeeds and compare room coverage and intervals against live FBS. The strict scraper rejects unreadable events, missing calendar rows and result pagination instead of assuming rooms are free. These conditions must be checked against live FBS after MFA; mocked/synthetic tests do not validate campus-wide completeness. Do not publish an incomplete scrape by disabling strict checks.

## Static GitHub Pages publishing

GitHub Actions no longer logs into SMU, installs a browser, or reads SMU password secrets. Remove obsolete `SMU_LOGIN_EMAIL` and `SMU_LOGIN_PASSWORD` repository secrets if previously configured. Do not attach a self-hosted Actions runner to this public repository.

The `Publish availability website` workflow checks out the default branch, fetches the `availability-data` branch's public `snapshot.json` and `status.json` when present, and builds Pages. If the data branch does not exist yet, it uses the honest empty state in `web/data`. Select **GitHub Actions** as the Pages source in repository settings. Keep the development branch as the default branch. If the `github-pages` environment restricts deployment branches, allow both the default branch and `availability-data` (data-push runs retain that event ref).

Website pushes, public-data pushes, manual dispatch, and a daily 22:17 UTC rebuild can trigger deployment. The daily rebuild is only a static publish fallback; it does not refresh FBS data. A push of `availability-data` checks out website code from the default branch. Git credentials that suppress workflow triggers (such as Actions' own token) require a manual dispatch or the scheduled rebuild.

Local sanitized-data push automation and scheduler installation are deferred until the first successful live scrape. Only the two public JSON files should reach `availability-data`. Never push an entire working directory, browser profile, `.env`, raw FBS results, or diagnostics. `scripts/publish_snapshot.py` remains available to merge an attempt directory into retained public data: only an `ok` attempt replaces the snapshot; failures update status while preserving the snapshot. The destination directory must already exist.

## Local scheduling gate

This checkout was inspected on macOS with `launchctl` available. After live validation, use a user LaunchAgent with absolute paths, a working directory at this repository, and an explicit browser channel; launchd does not inherit interactive shell configuration. Keep sanitized-data publishing in that local job, separate from Actions. A sleeping/offline Mac cannot guarantee on-time scraping. On supported Linux hosts, prefer a user systemd timer with persistence; detect availability before installing one. Do not install a scheduler until both live scraping and the public-data push have been verified.

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

Automated login tests verify code paths only. Live authentication, saved-session reuse, seven-day coverage and scheduled publishing require separate verification on the owner's computer.
