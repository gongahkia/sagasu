# Set up Sagasu with a local agent

Sagasu is a local MCP server for SMU FBS. Install it once on the computer where
your MCP client runs, then register its stdio launch command with that client.
The same server can serve different local agents.

Sagasu also has a hosted path: one person runs it on a server for a group, and
everyone else connects their agent app over HTTPS without installing anything.
See [Hosting for a group](#hosting-for-a-group). The rest of this guide is the
local path.

## Give this to your agent

```text
Install and connect https://github.com/gongahkia/sagasu for the local agent
client I am using. Read the repository's SETUP.md first.

Determine whether your tools operate on my computer or in a remote sandbox.
If you have local access, perform the supported installation and client
registration. Preserve existing client settings, MCP servers and Sagasu data.
If you cannot access my computer, give me the exact local steps instead of
installing into your sandbox or claiming the local setup is complete.

Use absolute executable and environment-file paths. Let me enter credentials
privately and complete MFA. Finish by verifying server readiness and the saved
FBS session through MCP. Do not create a booking or change account/saved data
as a setup test. Report what succeeded and any remaining manual step.
```

If your agent only retrieves the repository overview, point it directly to
[this guide](https://github.com/gongahkia/sagasu/blob/main/SETUP.md). Reading a
document does not grant an agent access to your computer. A sandboxed chat can
prepare the commands/configuration but ultimately a a local agent or you (a human) must apply them.

## Before installing

- This guide covers macOS and Linux with the shared local host. Have Git,
  [uv](https://docs.astral.sh/uv/getting-started/installation/), and your chosen
  agent/MCP client installed. On macOS with Homebrew, install uv with
  `brew install uv` if needed.
- Sagasu requires Python 3.11+. The commands below select Python 3.12, also used
  by this repository's CI, uv can download it if needed.
- Your client needs local stdio MCP support, either built in or through a
  supported adapter. A local terminal alone does not establish MCP support.
- If Sagasu or an entry named `sagasu` already exists, inspect and reuse it.
  Do not overwrite its configuration, recreate its virtual environment, reset
  data, or replace a working client entry as part of a fresh-install recipe.

The Ubuntu VM installer under `deploy/linux/` is a separate deployment path.
It is not needed for an ordinary personal desktop installation.

## Install on the local computer

Run this **new-install** block in a local terminal. Change `SAGASU_DIR` first if
you want another location. The subshell stops at the first failure. If the clone
already exists, use the existing-install guidance above instead of rerunning it.

```sh
SAGASU_DIR="$HOME/sagasu"
(
  set -eu
  git clone https://github.com/gongahkia/sagasu "$SAGASU_DIR"
  uv venv --python 3.12 "$SAGASU_DIR/.venv"
  uv pip install --python "$SAGASU_DIR/.venv/bin/python" -e "$SAGASU_DIR"
  "$SAGASU_DIR/.venv/bin/python" -m playwright install chromium
  "$SAGASU_DIR/.venv/bin/python" -m sagasu --env-file "$SAGASU_DIR/.env" config init
  "$SAGASU_DIR/.venv/bin/python" -m sagasu --env-file "$SAGASU_DIR/.env" doctor --timeout 30
)
```

On Linux, Chromium may need OS libraries. On a supported distribution, use
Playwright's dependency installer, reviewing any requested elevated access:

```sh
"$SAGASU_DIR/.venv/bin/python" -m playwright install-deps chromium
```

Then rerun the failed check. Do not rerun the entire new-install block. If you
open another terminal, set `SAGASU_DIR` again to your existing checkout's absolute
path before using the following commands.

### Sign in privately

For initial interactive login, run:

```sh
"$SAGASU_DIR/.venv/bin/python" -m sagasu --env-file "$SAGASU_DIR/.env" login
```

The default configuration opens a browser for you to complete Microsoft sign-in
and MFA. Optional `SMU_LOGIN_EMAIL` and `SMU_LOGIN_PASSWORD` autofill settings
belong in your private `.env`, edited locally. Never paste credentials, cookies,
browser profiles or MFA secrets into an agent conversation or commit them.
Missing autofill credentials do not mean an existing saved session is expired.
For headless login, configure autofill privately and follow Sagasu's MFA flow.

`config init` defaults to `SAGASU_MCP_CONFIRMATION=agent`: the agent can submit
reviewed actions you request. For a separate native confirmation form, choose
`human` privately if your client supports MCP form elicitation. Check advertised
capabilities rather than assuming every client supports the same interaction.
Setup itself does not authorize bookings, cancellations or task actions.

## The launch contract for any MCP client

The server command is **`sagasu mcp`**. Using the virtual environment's Python
also works and avoids depending on shell activation or a GUI application's PATH:

```sh
"$SAGASU_DIR/.venv/bin/python" -m sagasu --env-file "$SAGASU_DIR/.env" mcp
```

Configure your client to launch that process, rather than leaving it running in
an unrelated terminal. A manually started stdio server waits for MCP protocol
input.

| Setting | Value |
| --- | --- |
| Transport | Local stdio |
| Executable / command | Absolute path to `sagasu/.venv/bin/python` |
| Arguments, in order | `-m`, `sagasu`, `--env-file`, absolute path to `.env`, `mcp` |
| Working directory | Not required when the executable and `.env` paths are absolute |
| Credentials | Sagasu's private local configuration/session, not an HTTP bearer token |

There is no Sagasu HTTP endpoint to put in a remote-connector URL field. The
GitHub repository URL is an installation source, not the MCP server address.
FBS login is handled by `sagasu login` or Sagasu's `login` tool, not a client's
remote-MCP OAuth login command.

The examples below follow client documentation checked on 16 November 2026 and
are alternatives for registering the same launch command. Verify the actual
connection afterward. Use your installed client's supported syntax. Keep its
version, model and unrelated settings unchanged. Back up configuration locally before editing it and
merge only the Sagasu entry and preserve every existing server.

### Pi

Current Pi has built-in MCP support. After checking for an existing `sagasu`
entry, register it at user scope:

```sh
pi mcp add sagasu -- "$SAGASU_DIR/.venv/bin/python" -m sagasu --env-file "$SAGASU_DIR/.env" mcp
pi mcp list
```

Use `/reload` in an existing session after external configuration changes, then
inspect `/mcp`. Older Pi versions or an installed MCP replacement extension may
have different configuration so use that version's adapter instructions instead
of assuming the built-in commands exist. See [Pi's MCP guide](https://docs.pi.dev/latest/mcp).

### OpenCode

For the currently documented CLI, register globally so Sagasu is available
outside this repository:

```sh
opencode mcp add sagasu --global -- "$SAGASU_DIR/.venv/bin/python" -m sagasu --env-file "$SAGASU_DIR/.env" mcp
opencode mcp list
```

Inspect `/mcps` in OpenCode. Its current V2 file format places entries under
`mcp.servers` and note that earlier formats differ. Prefer the installed CLI's supported
registration flow rather than copying a configuration from another version.
See [OpenCode's MCP guide](https://opencode.ai/docs/mcp-servers).

### Codex

Inspect any existing entry with `codex mcp get sagasu`, then add only if absent:

```sh
codex mcp add sagasu -- "$SAGASU_DIR/.venv/bin/python" -m sagasu --env-file "$SAGASU_DIR/.env" mcp
codex mcp get sagasu
```

Start a fresh session or use the client's supported MCP refresh mechanism, then
inspect `/mcp`. See [Codex's MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### Hermes

Merge this entry into `mcp_servers` in your Hermes profile's `config.yaml`
(`~/.hermes/config.yaml` by default). Replace **both** `/absolute/path/to/sagasu`
placeholders with your actual checkout path, note that these are not shell variables.

```yaml
mcp_servers:
  sagasu:
    command: /absolute/path/to/sagasu/.venv/bin/python
    args: ["-m", "sagasu", "--env-file", "/absolute/path/to/sagasu/.env", "mcp"]
```

Start a new Hermes chat and check tool discovery. See [Hermes' MCP configuration
reference](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference).

### Claude Code

Inspect any existing entry with `claude mcp get sagasu`, then add only if absent:

```sh
claude mcp add --transport stdio --scope user sagasu -- "$SAGASU_DIR/.venv/bin/python" -m sagasu --env-file "$SAGASU_DIR/.env" mcp
claude mcp get sagasu
```

Inspect `/mcp` in Claude Code. See [Claude Code's MCP guide](https://code.claude.com/docs/en/mcp).

### Claude Desktop and clients using `mcpServers` JSON

On macOS, Claude Desktop's manual configuration file is
`~/Library/Application Support/Claude/claude_desktop_config.json`. Its Developer
settings provide an Edit Config route. Merge the following entry into the
existing `mcpServers` object and do not replace the entire file. Other clients may
use the same JSON shape in a different location.

Replace **both** `/absolute/path/to/sagasu` placeholders with the actual absolute
checkout path, such as `/Users/alex/sagasu`. Do not leave `~`, `$HOME` or shell
quotes inside the executable path. Each array element is one literal argument.

```json
{
  "mcpServers": {
    "sagasu": {
      "command": "/absolute/path/to/sagasu/.venv/bin/python",
      "args": ["-m", "sagasu", "--env-file", "/absolute/path/to/sagasu/.env", "mcp"]
    }
  }
}
```

Fully quit and reopen Claude Desktop, then inspect its local MCP connection
status. UI locations vary by version. See the [MCP local-server connection
guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers) and
[Claude's local MCP help](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop).

If the chat's shell is a remote Linux sandbox, it cannot edit the Mac's config
through that shell. Run the installation locally and merge this JSON yourself,
or ask an agent with authorized local filesystem access to do it. Do not move
your browser profile or credentials into the sandbox to work around that limit.

### Other local agents

Use the launch contract above with the client's documented stdio registration
mechanism. If it takes a command array, place the executable first followed by
each argument. If it takes separate `command` and `args`, keep them separate.
Do not assume every client uses an `mcpServers` key or expands environment
variables in JSON. If that client/version lacks MCP support, identify its
supported adapter or report the limitation and do not invent a native endpoint.

## Verify through the client

Once the client discovers Sagasu's tools, ask it to perform this read-only check:

```text
Verify Sagasu setup without booking or changing any saved data.
Call server_status with {}, then configuration_status with {}.
If operational, check host_status with {} when advertised and verify the saved
session using session_status with {}. Poll that same operation with
wait_operation until terminal and use the advertised limits and do not restart
work merely because a poll times out.
Report current SGT time, host/configuration readiness, configured headless
mode, saved-session validity and any blockers. Keep credentials redacted.
Do not renew authentication, restart the host or perform writes automatically.
```

Installation is verified only when this client's MCP connection is operational
and a fresh session check succeeds. A CLI registration command succeeding or
`doctor` passing does not independently establish those results. An optional
user-requested room search can demonstrate a read and note that no booking is required.

## If setup is blocked

| Symptom | Next step |
| --- | --- |
| Agent only has a remote sandbox | Use the local Terminal/client-config steps, report the manual handoff clearly. |
| Executable or module not found | Check the absolute virtual-environment Python path and installation, do not rely on shell activation in GUI clients. |
| Configuration fails | Read `configuration_status`, correct only the reported local setting. Do not collect `.env` contents in chat. |
| Host/code/configuration mismatch | Read `host_status` and [host recovery guidance](docs/host-recovery.md). Preserve retained evidence, obtain authorization before stopping a host or discarding results. |
| Missing autofill credentials | Check the saved session separately, configure autofill privately only if needed. |
| Saved session expired | Report the authentication blocker and request login/MFA renewal, do not infer expiry from missing credentials. |
| Tools absent after configuration | Inspect that client's connection errors and refresh/reopen the client, registration alone is not a successful connection test. |
| Human confirmation unavailable | Report the client capability limitation, do not silently change submission policy or synthesize approval. |

For failed FBS operations, retain the operation ID and collect `support_bundle`
before its TTL/retention expiry. Avoid sharing unrelated private booking data.
Search/filter and pagination semantics are documented in
[search and booking semantics](docs/search-and-booking-semantics.md).

An installing agent should finish with the install location, client entry and
launch arguments, checks actually performed, and any remaining user action.
Do not report setup complete while local installation, registration, login/MFA
or an operational MCP connection remains unverified. No product code changes
or real FBS writes are needed to complete onboarding.

## Hosting for a group

Sagasu runs two ways. On the **local** path (everything above) each person
installs it on their own computer for their own agent. On the **hosted** path
one person runs it on a server for a group of friends. Each person gets their
own isolated copy, with its own Linux account, browser, saved SMU sign-in and
API key, behind one shared address. Their agent connects over HTTPS, and only
the person hosting installs anything.

Each copy signs in as exactly one SMU account. Never share a key: everything
done with it happens as that copy's owner.

### For the person hosting

The full operator guide is [deploy/server/README.md](deploy/server/README.md).
In short:

1. Get an Ubuntu server with a public address and a host name that resolves to
   it. Without a domain, `A-B-C-D.sslip.io` resolves to the address `A.B.C.D`.
2. Create your API key on your own computer, copy the source tree to the
   server, and run `deploy/server/install.sh --host HOST --key owner:<digest>`.
   Then allow inbound TCP 443 in the cloud provider's firewall.
3. Give each friend their own copy:
   `sudo sagasu-ctl instance-add NAME [THEIR_SMU_EMAIL]`. It prints a key once.
   Send them the shared address `https://HOST/mcp` and that key privately.
4. Updating restarts every copy and ends everyone's SMU session. Each person
   then approves one Microsoft Authenticator number on their next request.

Saved SMU passwords are encrypted on disk, but whoever administers the server
can read them. Only host copies for people who trust you.

### Connect your agent app

You need the address (`https://HOST/mcp`) and your key from the person
hosting.

- **Apps that can send a header** (Claude Code and most MCP clients): add a
  streamable HTTP server at `https://HOST/mcp` with the header
  `Authorization: Bearer <key>`, or `X-API-Key: <key>` if the app reserves
  `Authorization`. For Claude Code, read the key from a file so it stays out of
  your shell history:

  ```bash
  claude mcp add --transport http sagasu https://HOST/mcp --header "Authorization: Bearer $(cat ~/sagasu-key)"
  ```

- **Apps that only support OAuth** (ChatGPT, Claude.ai connectors): add the
  URL `https://HOST/mcp`, choose OAuth (dynamic or automatic registration if
  asked), and paste your key on the sign-in page the app opens. Only do this
  for a connection you started yourself.

If tools are missing or the app reports an error, remove the connector and add
it again. Some apps keep the tool list from when the connector was first added.

### Sign in to SMU

1. On your first request, Sagasu answers with a one-time sign-in link. Open it
   in your browser and save your SMU email and password on your copy's own
   page. Then approve the sign-in in Microsoft Authenticator.
2. After that, when SMU asks again, your agent signs in for you and shows a
   number in the chat. Enter it in Authenticator within about three minutes
   and reply "done".
3. If you missed the number, ask your agent to sign you in again (the `login`
   tool) for a fresh one. Sagasu sends nothing else to your phone until you ask.

Never give your SMU password to an agent or type it into a chat.

### Give this to your agent (hosted)

```text
Sagasu is already connected as a hosted MCP server; do not install anything.
Verify it without booking or changing any saved data: call server_status with
{}, then session_status with {} and poll that operation with wait_operation
until it is terminal. If the result has a sign_in field, give me its number
and words, end your turn, and continue after I reply "done". If it reports
sign_in_setup_needed, give me the sign_in_url to open myself; never open it
yourself and never ask for my password. Report the server's readiness and
whether my SMU session is valid.
```

### If a hosted setup is blocked

| Symptom | Next step |
| --- | --- |
| `401` from the server | Check the URL ends in `/mcp` and the app sends exactly one key header, or reconnect through OAuth. |
| The agent cannot send a fresh sign-in number | The app kept an old tool list without `login`. Remove and re-add the connector. |
| `sign_in_setup_needed` or a `sign_in_url` | Open the link yourself and save or update your SMU sign-in there. Links last 15 minutes. |
| `sign_in_not_approved` | The number expired or was denied. Ask your agent to sign you in again. |
| `saved_password_rejected` | Your SMU password changed. Save the new one on your sign-in page. |
| A certificate warning on the sign-in page | Do not type your password. Some managed networks intercept HTTPS; use another network. |
