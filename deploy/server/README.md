# Sagasu as a hosted service

Runs the Sagasu host on an Ubuntu 24.04 server and publishes a restricted MCP endpoint over HTTPS, so remote agents can use one person's FBS account with an API key.

This is separate from `deploy/linux`, which installs a headless runtime for an agent that runs on the same machine. Use one or the other.

## What a remote agent can and cannot do

| | Remote agent (API key) | Person at the server |
| --- | --- | --- |
| Search rooms, read bookings, tasks, credits, history | Yes | Yes |
| Book, amend, cancel | Only if `SAGASU_HTTP_WRITES=on` | Yes |
| Change saved contacts, preferences, plans, templates | Only if `SAGASU_HTTP_WRITES=on` | Yes |
| Sign in to SMU, see or answer an MFA prompt | Never | Yes |
| Approve other people's requests, batch several actions | Never | Yes |
| See, read, cancel or submit anything started at the server | Never | Yes |
| Stop, recover or inspect the host; take browser screenshots | Never | Yes |

Remote tools are removed from the tool list, not merely refused, so an agent never sees what it cannot use. The full lists are `REMOTE_HIDDEN_TOOLS` and `REMOTE_WRITE_TOOLS` in `sagasu/mcp_server.py`.

Every operation and review records whether a remote call or the person at the server created it. A remote caller is shown, and can address, only what remote calls created; the owner sees everything.

A remote call never types or clicks on a Microsoft page. When the FBS session has lapsed it waits up to 25 seconds for Microsoft's own single sign-on redirect. If Microsoft asks for anything, the call fails with `sign_in_required_on_host` and further remote FBS requests are refused for 15 minutes without loading the sign-in page again, or until the owner signs in. Multi-date planning calls stop at the first date that needs the owner.

## How it fits together

```
agent --HTTPS + Authorization: Bearer <key>--> Caddy :443 (TLS certificate)
        --> 127.0.0.1:8765  Sagasu host (API-key check, restricted tools)
              --> one Chromium window on a virtual display --> FBS
```

- `sagasu.service` runs `sagasu host-run` as the unprivileged `sagasu` user. With `SAGASU_HTTP_PORT` set, the host never exits when idle and shuts down in order on SIGTERM.
- `sagasu-xvfb.service` provides the virtual display. The browser is not headless, so a person can sign in to it.
- The listener binds to loopback only. Caddy is the only thing reachable from outside, and it forwards only `/mcp`.
- Each request is an independent POST answered with JSON. There are no sessions and no event stream, so agents keep working across a service restart.
- The server stores the SHA-256 digest of each API key, never the key.
- Code, interpreter and browser binaries are owned by root. Sagasu's state lives in `/var/lib/sagasu` (mode 0700).

## Install

Create the first API key on your own computer, and keep it there:

```bash
python3 -c "import secrets; print('sgs_' + secrets.token_urlsafe(32), end='')" > api-key
printf 'owner:%s\n' "$(shasum -a 256 < api-key | cut -d' ' -f1)"
```

Copy the source tree to the server without your local `.env`, `.sagasu/` and `artifacts/`. They can hold your SMU password, browser session and sign-in screenshots.

```bash
rsync -a --exclude .git --exclude .venv --exclude .env --exclude .sagasu --exclude artifacts ./ ubuntu@SERVER:sagasu-src/
```

Then, on the server, run the installer with the public host name and the `label:digest` printed above:

```bash
sudo bash sagasu-src/deploy/server/install.sh --host booking.example.org --key owner:<digest>
```

The host name must already resolve to the server. Without a domain, `A-B-C-D.sslip.io` resolves to the address `A.B.C.D`.

Two steps remain, and both need a person:

1. Allow inbound TCP 443 to the server in the cloud provider's firewall. The installer opens it in the server's own firewall only.
2. Sign in to SMU on the server (next section).

## Signing in

Sign-in happens in the server's own browser, seen through a temporary view that listens on the server's loopback interface and is protected by a one-time password. Reach it through SSH:

```bash
ssh -t -L 5901:127.0.0.1:5900 -L 6080:127.0.0.1:6080 ubuntu@SERVER 'sudo sagasu-signin'
```

Then open `vnc://localhost:5901` (macOS Screen Sharing) or `http://localhost:6080/vnc.html?autoconnect=1&resize=scale` (any browser), enter the password the command printed, and sign in to Microsoft as usual. A viewer may describe the connection as unencrypted; it travels inside SSH. The command reports when sign-in is verified and closes the view.

The view stays open for 15 minutes (`SAGASU_AUTH_TIMEOUT_MS=900000`). Sagasu types nothing. Its sign-in loop still clicks on three Microsoft screens if they appear: "Use my password", the Microsoft Authenticator choice, and a passkey prompt, which it moves past.

`SAGASU_STAY_SIGNED_IN=false` leaves Microsoft's "Stay signed in?" question to you. Yes keeps the server signed in across restarts. No is safer and means signing in again after a restart or when the session lapses.

If Microsoft shows a block page, an error, or a step you do not normally see, do not retry from the server. Stop the service and ask your IT department.

`SMU_LOGIN_PASSWORD` must not be set on a server. Configuration validation rejects it together with the remote listener, and the launcher refuses to start.

## Day to day

```bash
sudo sagasu-ctl status            # services, address, key labels, write setting
sudo sagasu-ctl writes on|off     # allow or stop remote booking, amending, cancelling
sudo sagasu-ctl oauth on|off      # allow or stop apps signing in through OAuth
sudo sagasu-ctl connections       # apps signed in through OAuth, per key
sudo sagasu-ctl key-add laptop    # new API key, printed once
sudo sagasu-ctl key-remove laptop # revoke a key
sudo sagasu-ctl logs              # private host log: one line per remote tool call
sudo bash sagasu-src/deploy/server/install.sh --update   # after copying new code
```

`--update` keeps keys, settings and saved state. It refreshes a proxy configuration this kit wrote, and it leaves a service you had disabled stopped and disabled. If a step fails, it starts Sagasu again with what is installed.

Changing a setting restarts the service, which closes the browser. If agents then report `sign_in_required_on_host`, run `sudo sagasu-signin` again.

## Connecting an agent

- URL: `https://HOST/mcp` (streamable HTTP)
- Header: `Authorization: Bearer <key>`
- Or, for an agent platform that reserves `Authorization` for itself: `X-API-Key: <key>` (the key alone). Both headers may be sent only if they carry the same key.

### Apps that only support OAuth

With `SAGASU_HTTP_OAUTH=on` (`sudo sagasu-ctl oauth on`), apps such as ChatGPT and Claude can connect with their OAuth sign-in instead of a header:

1. In the app, add the URL `https://HOST/mcp` and choose OAuth. If it asks how to register, choose dynamic or automatic registration (DCR).
2. The app opens this server's sign-in page. Paste the API key there once.
3. The app receives tokens that count as that key: same access, same label in the log.

How it is limited:

- Codes are sent back only to redirect URIs on `SAGASU_OAUTH_REDIRECT_HOSTS` (by default chatgpt.com, claude.ai, claude.com and loopback). Registering any other redirect is refused.
- PKCE is required. Access tokens last 1 hour; refresh tokens rotate and last 30 days; after 90 days the key must be entered again.
- Removing or replacing a key ends every token issued for it at once.
- The server stores only SHA-256 digests of tokens, in `/var/lib/sagasu/oauth.sqlite3`. Presenting an already-used refresh token ends that whole sign-in.
- Wrong keys on the sign-in page are limited to 10 per address and 50 overall in 10 minutes, and 5 per sign-in.
- `sudo sagasu-ctl connections` lists the apps signed in, per key.

Only enter a key on the sign-in page when you started the connection from the app yourself.

| Answer | Meaning |
| --- | --- |
| `401` | No valid key, or more than one key header |
| `403` | The request carries a browser `Origin` header |
| `405` | Not a POST. There is no event stream to open and no session to end |
| `421` | The `Host` is not in `SAGASU_HTTP_ALLOWED_HOSTS` |
| `503` | More than 32 keyed requests are in progress; retry after a second |

## Settings

All in `/etc/sagasu/sagasu.env`. See `sagasu.env.example`.

| Setting | Meaning |
| --- | --- |
| `SAGASU_HTTP_PORT` | Turns the listener on and makes the host a long-lived service. 1024–65535. Caddy forwards to `127.0.0.1:8765`; change `reverse_proxy` in `/etc/caddy/Caddyfile` to match. |
| `SAGASU_HTTP_BIND` | Loopback address only. Default `127.0.0.1`. Change `reverse_proxy` to match. |
| `SAGASU_HTTP_ALLOWED_HOSTS` | Public host name(s) agents use, comma-separated. Each name also needs to be a site address in `/etc/caddy/Caddyfile`. |
| `SAGASU_HTTP_KEYS` | `label:sha256hex`, comma-separated, 1–16 entries. |
| `SAGASU_HTTP_OAUTH` | `off` (default) or `on`: serve the OAuth sign-in for apps that cannot send a key header. |
| `SAGASU_OAUTH_REDIRECT_HOSTS` | Hosts an OAuth app's redirect may use, comma-separated. Default `chatgpt.com,claude.ai,claude.com,localhost,127.0.0.1`. |
| `SAGASU_HTTP_WRITES` | `off` (default) or `on`. `on` needs `SAGASU_MCP_CONFIRMATION=agent`. |
| `SAGASU_SESSION_COOKIES` | `all` (default) or `fbs`: keep only cookies FBS receives in the saved session file. |

An invalid listener setting keeps the listener off and puts the host in diagnostic mode. `sudo sagasu-ctl status` then shows `"runtime_mode": "diagnostic"` and `"remote_listener": false`, and `sudo /opt/sagasu/bin/sagasu-launch doctor --offline` prints the reason.

Write each setting as plain `NAME=value`. `sagasu-ctl` refuses to edit a line that has quotes, spaces, `export` or a trailing comment.

## What this does not protect against

- Anyone who holds an API key can read your bookings and saved contacts, and with writes on can book and cancel in your name. Keep keys in a password manager and revoke any that leak.
- Anyone with root on the server can use the signed-in browser, including the Microsoft session behind it. Keep the server patched and SSH key-only.
- Other accounts on the same machine. Caddy reaches Sagasu over a loopback TCP port, so a local user who took that port while the service was restarting would receive forwarded keys. Use a machine that nobody else has an account on.
- The server signs in to SMU from a data-centre address. SMU's sign-in monitoring may treat that as a break-in.
