[![](https://img.shields.io/badge/sagasu_v1.0.0-active-brightgreen)](https://github.com/gongahkia/sagasu/releases/tag/1.0.0)
![](https://github.com/gongahkia/sagasu/actions/workflows/ci.yml/badge.svg)

# `Sagasu`

<p align="center">
<img src="./asset/logo/icon_with_words.png" width="50%" alt="Sagasu">
</p>

A [MCP Server](https://modelcontextprotocol.io/docs/2026-07-28/getting-started/intro) to help your [agents](https://cursor.com/help/ai-features/coding-agents) work with [SMU FBS](https://www.smubondue.com/facility-booking-system-fbs).

## Rationale

[SMU's Facility Booking System](https://fbs.intranet.smu.edu.sg/home) isn't an inherently slow website. Booking facilities in itself is quick.

If anything, the sluggish impression it gives off results from the overly convoluted system users must navigate to search for available rooms.

`Sagasu` is a MCP Server that gives your agents access to SMU FBS' search, book, cancel and all other user-specified interactions.

## Stack

- [Python](https://www.python.org/) — server and browser automation runtime.
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — standard MCP tools, resources, prompts and stdio transport.
- [Playwright](https://playwright.dev/python/) and [Chromium](https://www.chromium.org/) — SMU FBS browser automation.
- [Pydantic](https://docs.pydantic.dev/) — typed tool inputs and validation.
- [SQLite](https://www.sqlite.org/) — private contacts, search preferences and durable action history.
- [python-dotenv](https://github.com/theskumar/python-dotenv) — private host configuration.
- [uv](https://docs.astral.sh/uv/) — Python environments and dependency installation.

## Usage

> [!NOTE]  
> `Sagasu` relies on Python 3.11+.

The below are for locally running `Sagasu` on your machine.

1. First run the below set-up instructions using [uv](https://docs.astral.sh/uv/getting-started/installation/).

```console
$ git clone https://github.com/gongahkia/sagasu && cd sagasu
$ uv venv
$ source .venv/bin/activate
$ uv pip install -e .
$ python -m playwright install chromium
$ sagasu config init
$ sagasu doctor --timeout 30
$ sagasu login
```

2. Then talk to your agent to ask them to interact with `Sagasu`. Below are some examples.

```txt
Find an SCIS1 group study room tomorrow from 2–4pm.

Book any available room in that window for project work with my saved co-booker Alex.

Show my bookings and cancel the one I no longer need.
```

## Architecture

```mermaid
flowchart TD
    User[User] <--> Client[Any MCP client]
    Client <-->|MCP over stdio| Server[Sagasu MCP server]
    Server <--> Operations[Operations, reviews and recovery]
    Operations <--> Preferences[(SQLite contacts and preferences)]
    Operations <--> History[(SQLite action history)]
    Operations <--> Browser[Playwright and Chromium]
    Config[Private .env] -.-> Server
    Profile[Private browser profile] <--> Browser
    Browser <--> FBS[SMU FBS]
    Browser <--> Microsoft[Microsoft sign-in]
    User -->|Approve MFA| Authenticator[Microsoft Authenticator]
    Authenticator --> Microsoft
```

## Contributors

<table>
	<tbody>
        <tr>
	    <td align="center">
                <a href="https://github.com/gongahkia">
                    <img src="https://avatars.githubusercontent.com/u/117062305?v=4" width="100;" alt="gongahkia"/>
                    <br/>
                    <sub><b>gongahkia</b></sub>
                </a>
            </td>
            <td align="center">
                <a href="https://github.com/SpringOrca69">
                    <img src="https://avatars.githubusercontent.com/u/159885540?v=4" width="100;" alt="SpringOrca69"/>
                    <br/>
                    <sub><b>SpringOrca69</b></sub>
                </a>
            </td>
			<td align="center">
                <a href="https://github.com/injaneity">
                    <img src="https://avatars.githubusercontent.com/u/44902825?v=4" width="100;" alt="injaneity"/>
                    <br/>
                    <sub><b>injaneity</b></sub>
                </a>
            </td>
        </tr>
	<tbody>
</table>
