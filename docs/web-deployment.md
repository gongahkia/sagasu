# Public availability website

Sagasu's website publishes a read-only daily snapshot of campus-wide group study rooms for today plus six days, 08:00–22:00 Singapore time. Visitors filter this snapshot locally; the website never logs visitors into SMU or submits bookings. The existing Telegram bot remains a separate personal deployment; see the website's **Your own bot** guide.

## Run locally

Use Python 3.12 with the repository's requirements installed in a virtual environment. No Node build or web framework is needed.

```sh
python scripts/build_site.py
python -m http.server 8000 --directory dist
```

Open `http://localhost:8000`. Until a scrape succeeds, the site displays an honest empty state. There is no generated sample availability.

## Validate unattended login first

The scheduled client uses a fresh temporary Chrome profile and a bounded password-only sign-in flow. It recognizes standard Microsoft email/password fields, SMU's federated username/password form, and password alternatives under **Sign in another way**. These selectors still need verification against your actual SMU login and GitHub's runner environment. The user's successful local password-only login does not guarantee cloud-runner access.

1. Install Chrome with `python -m playwright install chrome`.
2. Set `SMU_LOGIN_EMAIL` and `SMU_LOGIN_PASSWORD` privately in your local `.env` or environment. Do not send them in chat, place them in command arguments, or commit them.
3. Run `python -m bot.scrape_daily`.
4. A successful run writes `artifacts/daily/snapshot.json` and `status.json`. A failed run writes only a sanitized status. For a successful local preview run `python scripts/build_site.py --data artifacts/daily`.
5. Confirm the published room names/times against FBS. Then validate the same login with a manually dispatched GitHub workflow.

If the route requests MFA, device approval, additional registration, or blocks the runner, the job fails. It does not approve a prompt, circumvent an actual second factor, export a browser session, or upload login screenshots. Confirm the current supported sign-in route locally before adjusting selectors. Fully unattended operation remains unverified until a GitHub-hosted run succeeds.

## Configure GitHub

1. In repository **Settings → Secrets and variables → Actions**, add repository secrets `SMU_LOGIN_EMAIL` and `SMU_LOGIN_PASSWORD`.
2. In **Settings → Pages**, select **GitHub Actions** as the build and deployment source. Ensure Actions is allowed to write repository contents; branch rules must permit the workflow to maintain `availability-data`.
3. Keep your normal development branch as the default branch. Never make `availability-data` the default branch.
4. Push the implementation to that default branch and open **Actions → Daily availability and website → Run workflow**. A push of website files deploys the site without using SMU secrets; manual dispatch performs the first scrape.
5. Verify the run and its Pages deployment URL. Review the seven-day data and compare it to FBS before advertising the site.
6. Enable GitHub Actions failure notifications in your GitHub notification preferences.

The workflow runs at `22:17 UTC` (06:17 the following day in Singapore), with a 30-minute job limit and serialized runs. GitHub scheduled workflows are best effort: runs can be delayed or dropped, and public-repository schedules can be disabled after inactivity. Dispatch manually when needed. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

The workflow only runs on the default branch and does not expose credentials to pull-request workflows. Secrets are scoped to the scrape step. No booking code is called by the scheduled entrypoint. Credentials are not needed in the browser, Pages settings, Telegram, or static site build.

## Data and failures

The workflow retains only `snapshot.json` and `status.json` on `availability-data`, then builds and deploys the static website using the Pages artifact/deployment actions. A complete successful seven-day scrape replaces the snapshot. A failure updates the attempt status but retains the previous complete snapshot. The workflow still reports failure after deploying the updated status so GitHub notifications work.

Public data includes room names, dates, free/busy intervals, and timestamps. Raw event descriptions, names of bookers, emails, credentials, browser profiles, and login diagnostics are not published. Treat both the Pages site and data branch as public.

- **No successful scrape:** the website shows the first-run empty state.
- **Valid result with no free rooms:** the site shows no matching rooms, without labelling the scrape a failure.
- **Authentication failure:** check secret names/values and test the password route locally. Do not repeatedly retry a rejected password.
- **Layout failure:** the strict scraper stops if events cannot be parsed or matching room rows cannot be read. It does not infer availability from unreadable events. Pagination that prevents complete room discovery must be supported before publishing a campus-wide result.
- **Partial scrape:** no new snapshot is published. The retained data stays intact.
- **Old snapshot:** after 36 hours the website marks the data stale. Failed workflow attempts show a separate status and timestamp.
- **Runner/dependency/publishing failure:** the workflow fails; the already deployed website stays available and becomes stale based on its timestamp. No attempt status can be published if infrastructure fails before the scraper runs.
- **Pages permission failure:** confirm Pages uses Actions and the `github-pages` environment permits the default branch.

The daily scrape is read-only but still depends on FBS availability, access from GitHub runners, and SMU's authentication policies. The website labels results as snapshots and directs users to check FBS before booking.

## Checks

```sh
python -m unittest discover -v
python -m compileall -q bot scripts
node --test tests/*.test.cjs
pre-commit run --all-files --show-diff-on-failure
```

The mocked login tests verify expected route behavior; they do not prove live SMU authentication. Keep real credential tests manual and never capture credentials in test fixtures.

## Diagnose a local login failure

On Fedora, try Playwright's bundled Chromium with `python -m playwright install chromium`, then set `SAGASU_BROWSER_CHANNEL=chromium` locally. The branded Chrome installer only supports selected distributions.

```sh
SAGASU_BROWSER_CHANNEL=chromium python -m bot.scrape_daily --headed --verbose
```

This opens the same fresh-profile password-only flow in a visible browser. When authentication fails in an interactive terminal, the browser stays open until you press Enter so you can read the prompt yourself. The command prints fixed login stages and writes an `error_code` and `login_stage` to `status.json`: `missing_credentials`, `rejected`, `interactive_verification`, `timeout`, or `browser_error`. These do not include credentials, URLs, or raw page content. Describe the prompt and share those codes when diagnosing; do not share credentials or verification codes. A successful run assisted by manual interaction does not validate unattended Actions login.

For Microsoft's security-key screen, the scheduled browser installs a hook before login that attaches an AbortController to passkey requests on the Microsoft login hosts. The scraper cancels the pending request and then selects **Use my password** when offered. This rejects the passkey request; it does not supply a passkey or approve MFA. It retries cancellation briefly if the request starts after the screen appears. Chrome password saving is disabled in the scheduled browser profile. The personal Telegram browser is unchanged. See the [WebAuthn abort specification](https://www.w3.org/TR/webauthn-3/#sctn-abortoperation) and [Playwright initialization scripts](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-add-init-script).
