[![](https://img.shields.io/badge/sagasu_v1.0.0-active-brightgreen)](https://github.com/gongahkia/sagasu/releases/tag/1.0.0)
![](https://github.com/gongahkia/sagasu/actions/workflows/ci.yml/badge.svg)

# `Sagasu`

<p align="center">
<img src="./asset/logo/icon_with_words.png" width=50% height=50%>
</p>

Telegram bot that finds and books available rooms in SMU.

Access [`sagasu_bot`](https://t.me/sagasu_bot) ***live***.

## Rationale

[SMU's Facility Booking System](https://fbs.intranet.smu.edu.sg/home) isn't an inherently slow website. Booking facilities in itself is quick.

If anything, the sluggish impression it gives off results from the overly convoluted system users must navigate to search for available rooms.

`Sagasu` is a Telegram bot that searches SMU FBS using user-specified filters, flags vacant facilities, and can book a selected or automatically chosen room after explicit confirmation.

## Usage

The below instructions are for running `Sagasu` locally on your machine.

```console
$ git clone https://github.com/gongahkia/sagasu && cd sagasu
$ python3 -m venv .venv
$ source .venv/bin/activate
$ python -m pip install -e .
$ python -m playwright install chromium
$ sagasu config init
```

Edit `.env` locally before starting: set `BOT_TOKEN` to your token from Telegram
[@BotFather](https://t.me/BotFather), and `TELEGRAM_OWNER_ID` to your numeric user ID
(available from [@userinfobot](https://t.me/userinfobot)). Keep the token private.
Python 3.11 or later is required.

```console
$ sagasu doctor
$ sagasu login
$ sagasu run
```

Complete SMU Microsoft sign-in and MFA in the browser opened by `sagasu login`.
Then open your own bot in Telegram, send `/start`, and choose **Find available
rooms**. Use **Search settings** to change the date, time, or room filters. To
book, select a room, enter a purpose and co-booker, and confirm the review.
Keep `sagasu run` running while using the bot; Ctrl+C stops it.

On subsequent runs, use `source .venv/bin/activate` followed by `sagasu run`.
See the [CLI guide](docs/cli.md) for configuration and troubleshooting, or the
[MCP guide](docs/mcp.md) for the optional agent interface.

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
