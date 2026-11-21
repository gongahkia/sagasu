# Sagasu as a hosted service

Runs the Sagasu host on an Ubuntu 24.04 server and publishes a restricted MCP endpoint over HTTPS, so remote agents can use one person's FBS account with an API key.

This is separate from `deploy/linux`, which installs a headless runtime for an agent that runs on the same machine. Use one or the other.

## What a remote agent can and cannot do

| | Remote agent (API key) | Person at the server |
| --- | --- | --- |
| Search rooms, read bookings, tasks, credits, history | Yes | Yes |
| Book, amend, cancel | Only if `SAGASU_HTTP_WRITES=on` | Yes |
| Change saved contacts, preferences, plans, templates | Only if `SAGASU_HTTP_WRITES=on` | Yes |
| Sign in to SMU | Only with the person's own saved sign-in (below); the person approves on their phone | Yes |
| See or answer an MFA prompt | Sees the number to enter, in results, on its own requests only | Yes |
| Approve one Task List item the person asks for (such as accepting a co-booking) | Only if `SAGASU_HTTP_WRITES=on` | Yes |
| Batch several actions | Never | Yes |
| See, read, cancel or submit anything started at the server | Never | Yes |
| Stop, recover or inspect the host; take browser screenshots | Never | Yes |

Remote tools are removed from the tool list, not merely refused, so an agent never sees what it cannot use. The full lists are `REMOTE_HIDDEN_TOOLS` and `REMOTE_WRITE_TOOLS` in `sagasu/mcp_server.py`.

Every operation and review records whether a remote call or the person at the server created it. A remote caller is shown, and can address, only what remote calls created; the owner sees everything.

When the FBS session has lapsed, a remote call first waits up to 25 seconds for Microsoft's own single sign-on redirect. If Microsoft asks for more, what happens depends on whether the person saved their SMU sign-in on the copy ("Saved sign-in" below). With one saved, the copy signs in with it and the call's result shows the number to enter in Microsoft Authenticator. Without one, a remote call never types or clicks on a Microsoft page: it fails with `sign_in_setup_needed` (or `sign_in_required_on_host` where saving is off), and further remote FBS requests are refused without loading the sign-in page again until the person signs in. Multi-date planning calls stop at the first date that needs the person.

## How it fits together

```
agent --HTTPS + Authorization: Bearer <key>--> Caddy :443 (TLS certificate)
        --> /run/sagasu/http.sock  Sagasu host (API-key check, restricted tools)
              --> one Chromium window on a virtual display --> FBS
```

- `sagasu.service` runs `sagasu host-run` as the unprivileged `sagasu` user. With a listener set, the host never exits when idle and shuts down in order on SIGTERM.
- `sagasu-xvfb.service` provides the virtual display. The browser is not headless, so a person can sign in to it.
- The listener is a socket file that only its own account and Caddy can open, so no other local account can reach it. Caddy is the only thing reachable from outside, and it forwards only `/mcp` and the OAuth paths.
- Each teammate's copy (below) is the same stack under its own account, display, socket and host name.
- Each request is an independent POST answered with JSON. There are no sessions and no event stream, so agents keep working across a service restart.
- The server stores the SHA-256 digest of each API key, never the key.
- Code, interpreter and browser binaries are owned by root. Sagasu's state lives in `/var/lib/sagasu` (mode 0700).

## One address for everyone

Every copy is published at one address, the owner's: `https://HOST/mcp`. A small gateway (`sagasu-gateway.service`, `sagasu/gateway.py`) sends each request to the copy of the person whose key it carries:

- **With an API key** (`Authorization: Bearer` or `X-API-Key`), the request goes to the copy that owns the key, unchanged. That copy checks the key again, so the gateway alone cannot get anyone into a copy with a key.
- **Apps that sign in through OAuth** (ChatGPT, Claude's "Sign in") sign in at the gateway: the person types their key once. The app's requests reach that key's copy with the gateway's own key for it (label `gateway`) and the person's key named by its SHA-256 digest (`X-Sagasu-Caller`). The **copy decides**: it accepts only if its own OAuth is on (`sagasu-ctl [--instance NAME] oauth on`) and the digest is one of its own keys now. So removing a key or switching OAuth off takes effect at once, wherever the app signed in.

The routing table, `/etc/sagasu/gateway-routes.json` (root:sagasu-gateway, 0640), maps each key's digest to its copy, records each copy's OAuth switch, and holds the gateway's key for each copy. It holds no person's key. `sagasu-ctl` rewrites it after `key-add`, `key-remove`, `oauth on|off`, `instance-add` and `instance-remove` (or by hand: `sagasu-ctl gateway-sync`); the gateway re-reads it within a second. A copy whose settings cannot be read is left off with a warning; the others are still written. `sagasu-ctl gateway-sync --rotate` gives every copy a new gateway key (after a suspected leak). `connections` lists apps signed in at a copy's own address and at the shared one.

The gateway runs as `sagasu-gateway` with no browser and no network (socket files only): it can reach the copies' sockets (it is in the web proxy's group) and its own state (`/var/lib/sagasu-gateway`, its OAuth sign-ins). It holds a key to every copy, so it is the most sensitive part; its key opens only `/mcp`, never a copy's sign-in page or OAuth. If the gateway is down, the web proxy sends `/mcp` straight to the owner's copy, so the owner's keys keep working; teammates' own addresses (`NAME.HOST`) never go through it. The SMU sign-in page stays on each copy's own address: that is where the links agents give people lead.

**First install of the gateway** (`install.sh --update` on a server that had none): while the copies are stopped, every copy gets a gateway key and the table is written (the run stops if the owner's copy cannot be added). The owner's OAuth sign-ins move to the gateway (`/var/lib/sagasu/oauth.sqlite3` becomes `oauth.sqlite3.moved-to-gateway`), so apps signed in at this address stay signed in. The gateway must then answer a test request, or the installer backs it out: gateway disabled, sign-ins moved back, the previous Caddyfile restored.

**To back it out by hand:** `sudo systemctl disable --now sagasu-gateway`; `sudo cp -p /etc/caddy/Caddyfile.previous /etc/caddy/Caddyfile && sudo systemctl reload caddy`; if apps should stay signed in, move `/var/lib/sagasu-gateway/oauth.sqlite3` back to `/var/lib/sagasu/oauth.sqlite3` (owner `sagasu`, mode 0600) and restart `sagasu`. The `gateway` keys left in each copy's settings are harmless; `key-remove` refuses that label, so remove them by hand if wanted.

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

Sign-in happens in the server's own browser. There are two ways to reach it.

**From any browser, with a key: `https://HOST/sign-in`.** Enter an API key, then sign in to Microsoft on a live picture of its sign-in page: tap the picture, and type into the box above it. It works on a phone. Only Microsoft's sign-in pages are shown (never FBS, the address bar or other tabs). Only taps, text of up to 256 printable characters, and Enter, Backspace, Tab and Escape are passed on, and none of it is logged or kept. The page names the copy it signs in to, and it accepts a key posted from itself only. Its cookie is HttpOnly, SameSite=Strict, limited to `/sign-in` and valid for 20 minutes, and a newer visit replaces an older one. A sign-in left without the page for 90 seconds is stopped, so it does not hold up agents' requests. Apps that sign in through OAuth are sent through this page after the key and return once SMU sign-in is done. Agents that use a key header receive the link in `sign_in_required_on_host` errors (`sign_in_url`).

**Through SSH**, with a temporary view that listens on the server's loopback interface and is protected by a one-time password:

```bash
ssh -t -L 5901:127.0.0.1:5900 -L 6080:127.0.0.1:6080 ubuntu@SERVER 'sudo sagasu-signin'
```

Then open `vnc://localhost:5901` (macOS Screen Sharing) or `http://localhost:6080/vnc.html?autoconnect=1&resize=scale` (any browser), enter the password the command printed, and sign in to Microsoft as usual. A viewer may describe the connection as unencrypted; it travels inside SSH. The command reports when sign-in is verified and closes the view.

The view stays open for 15 minutes (`SAGASU_AUTH_TIMEOUT_MS=900000`). Sagasu types nothing. Its sign-in loop still clicks on three Microsoft screens if they appear: "Use my password", the Microsoft Authenticator choice, and a passkey prompt, which it moves past.

On the live picture, `SAGASU_STAY_SIGNED_IN=false` leaves Microsoft's "Stay signed in?" question to you. Yes keeps the server signed in across restarts; No means signing in again after a restart or when the session lapses. When Sagasu signs in with a saved sign-in, nobody is watching, so it answers itself: Yes, unless `SAGASU_SAVED_SIGN_IN_STAY=false`.

If Microsoft shows a block page, an error, or a step you do not normally see, do not retry from the server. Stop the service and ask your IT department.

`SMU_LOGIN_PASSWORD` must not be set on a server: it would keep a password in plain text. Configuration validation rejects it together with the remote listener, and the launcher refuses to start. The launcher also refuses a sign-in key that is not a root-owned file in `/etc/sagasu` that only root can change.

### Saved sign-in: signing in from chat

Each copy has its own key, `/etc/sagasu/NAME.signin-key` (root-owned, readable only by that copy's account), which `install.sh` creates once and never rewrites. With it set (`SAGASU_HTTP_SIGN_IN_KEY_FILE`), the person saves their SMU email and password on the copy's own sign-in page, once. They are kept encrypted in the copy's state folder (`saved-sign-in.json`, AES-256-GCM). After that:

1. When SMU needs a new sign-in, the agent's request signs in with them. Nobody watches the browser; Sagasu answers Microsoft's "Stay signed in?" itself (Yes, unless `SAGASU_SAVED_SIGN_IN_STAY=false`).
2. The request's result has a `sign_in` field with the number to enter in Microsoft Authenticator and the words to give the person. The agent shows it and ends its turn.
3. The person enters the number and replies "done". The agent's next `wait_operation` waits through the approval and returns the result.

The person never gives a password to an agent or a chat. When a sign-in page is needed (first use, a changed password), errors carry `sign_in_url`: a one-time link to the copy's page that needs no access key, used once and valid for 15 minutes.

Safeguards:

- **One account per copy.** A copy accepts only the first SMU account Microsoft accepted on it, or the one set with `sagasu-ctl [--instance NAME] smu-account EMAIL` (or `instance-add NAME EMAIL`). Nobody can be led into saving their password on someone else's copy.
- **Limits on disk.** At most 4 sign-ins an hour and 5 passwords typed a day, kept in `sign-in-ledger.json` so restarts do not reset them.
- **Holds.** After a sign-in that ends short of FBS, remote requests stop signing in. A missed or denied approval is lifted when the person asks their agent to sign in again. A rejected password (which is deleted at once), a lock or block, or an unclear end after the password is lifted only on the person's own sign-in page or by a successful sign-in.
- **Checks never sign in.** `session_status` and other checks fail with `sign_in_needed` instead of sending a request to the phone.
- **Records.** The sign-in page lists recent sign-ins and where they came from.

The encryption protects copies of the state folder. It does not protect against whoever controls the running service or the server: the administrator can read every saved password. Only host copies for people who trust whoever administers the server.

Without the key (or if it is unreadable), the copy works exactly as before: agents cannot sign in, and the person signs in on the live picture.

## Day to day

```bash
sudo sagasu-ctl status            # services, address, key labels, write setting
sudo sagasu-ctl writes on|off     # allow or stop remote booking, amending, cancelling
sudo sagasu-ctl oauth on|off      # allow or stop apps signing in through OAuth
sudo sagasu-ctl connections       # apps signed in through OAuth, per key
sudo sagasu-ctl key-add laptop    # new API key, printed once
sudo sagasu-ctl key-remove laptop # revoke a key
sudo sagasu-ctl logs              # private host log: one line per remote tool call
sudo sagasu-ctl forget-sign-in    # delete the SMU session, saved password and sign-in record
sudo sagasu-ctl smu-account EMAIL # accept only this SMU account on the copy
sudo bash sagasu-src/deploy/server/install.sh --update   # after copying new code
```

`--update` keeps keys, settings and saved state. It refreshes a proxy configuration this kit wrote, and it leaves a service you had disabled stopped and disabled. If a step fails, it starts Sagasu again with what is installed.

Changing a setting restarts the service, which closes the browser. With a saved sign-in, the next request signs in again and shows the number to approve. Otherwise sign in again at `https://HOST/sign-in` or with `sudo sagasu-signin`.

To switch saved sign-ins off for a copy: `sagasu-ctl [--instance NAME] forget-sign-in`, then remove the `SAGASU_HTTP_SIGN_IN_KEY_FILE` line from its settings file and restart it. An older version of this kit ignores the line, but its `forget-sign-in` does not delete `saved-sign-in.json`; run the new one first.

## Teammates' copies

A copy serves one SMU account. Never give a teammate a key to yours: everything they did would happen as you. Give them their own copy, on the same server:

```bash
sudo sagasu-ctl instance-add alex alex@smu.edu.sg         # prints https://alex.<your host>/mcp and a key
sudo sagasu-ctl instance-list
sudo sagasu-ctl --instance alex status                    # any copy command, for their copy
sudo sagasu-ctl instance-remove alex --yes                # the copy, its sign-in and data
```

`instance-add` creates a separate account (`sagasu-alex`), state folder, virtual display, socket, settings file and Caddy site, and one API key, printed once. The host name is the owner's with the copy's name in front, so with sslip.io it needs no DNS change. The copy starts with OAuth on and booking on (`--read-only` keeps booking off). The optional email locks the copy to that teammate's SMU account. Send the teammate the address and the key. They connect their app, save their SMU sign-in on the page that opens, and approve one sign-in; after that their agent signs them in from chat. They never need the server itself.

Optionally, `access-add alex 'ssh-ed25519 AAAA...'` gives a teammate an SSH login instead, and `access-remove alex` takes it away. `access-add` creates a login, `fbs-alex`, that has no password and accepts only that key. The key can run only `sagasu-self alex`, which accepts a fixed list of commands for that copy (`signin`, `status`, `key-add`, `key-remove`, `connections`, `writes`, `oauth`, `restart`, `forget-sign-in`, `help`). It can forward only to that copy's own sign-in view ports. There is no shell, no other forwarding, and no access to other copies. The teammate signs in with:

```bash
ssh -t -L 6080:127.0.0.1:<view port> fbs-alex@SERVER signin
```

`access-add` prints the view port. The teammate then opens `http://localhost:6080/vnc.html?autoconnect=1&resize=scale`, as with the owner's sign-in.

The installer adds `/etc/ssh/sshd_config.d/60-sagasu.conf`, which reads these keys from root-owned files in `/etc/ssh/sagasu_keys/` and allows local forwarding only. It checks the file with `sshd -t` before reloading SSH. Anyone with root on the server can still reach every copy's sign-in, so only host copies for people who trust whoever administers the server.

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
| `SAGASU_HTTP_SOCKET` | Turns the listener on as a socket file (absolute path, at most 100 characters) and makes the host a long-lived service. The kit uses `/run/sagasu/http.sock`, which Caddy forwards to. Set this or `SAGASU_HTTP_PORT`, not both. |
| `SAGASU_HTTP_PORT` | Instead of a socket: a loopback TCP port, 1024–65535. Any local account can connect to it, so prefer the socket. Change `reverse_proxy` in `/etc/caddy/Caddyfile` to match. |
| `SAGASU_HTTP_BIND` | With `SAGASU_HTTP_PORT`: loopback address only. Default `127.0.0.1`. |
| `SAGASU_HTTP_ALLOWED_HOSTS` | Public host name(s) agents use, comma-separated. Each name also needs to be a site address in `/etc/caddy/Caddyfile`. |
| `SAGASU_HTTP_KEYS` | `label:sha256hex`, comma-separated, 1–16 entries. A key also opens the SMU sign-in page at `/sign-in`. |
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
