![](https://img.shields.io/badge/sagasu_1.0-build-orange)

>[!IMPORTANT]  
> Features are being added per recent user feedback.  
> `sagasu` will be deployed again afterward.
>  
> *\~ Gabriel*

# `sagasu`

<p align="center">
<img src="./asset/logo/icon_with_words.png" width=50% height=50%>
</p>

Telegram bot that finds available rooms in SMU.

## Rationale

[SMU's Facility Booking System](https://fbs.intranet.smu.edu.sg/home) isn't an inherently slow website. Booking facilities in itself is quick.  
  
If anything, the sluggish impression it gives off results from the overly convaluted system users must navigate to search for available rooms.
  
`sagasu` is a Telegram bot that scrapes SMU FBS per user-specified filters for available rooms, flagging any vacant facilities so users can quickly secure them on FBS.

![](./asset/screenshot/1.png)

## How to build

Don't.  
  
Access the telegram bot [`sagasu_bot`](https://t.me/sagasu_bot).

## Actually how to build

```cd scraper_async```

1. Setup Virtual Environment

```python3.12 -m venv venv``` (in root directory)

```source venv/bin/activate``` (for Unix-based systems)
```.\venv\Scripts\activate``` (for Windows)

2. Install Dependencies

```pip install -r requirements.txt```

```playwright install```

3. Build & Test scraper_async

```docker build --platform linux/amd64 -t <username>/<docker-repo-name> .```

```docker run --platform=linux/amd64 -p 80:8000 -d injaneity/sagasu-scraper```

4. Clean Up scraper_async

```docker ps```

```docker kill <prefix>```

```docker remove <prefix>```

### Local Testing

1. (If not running on Docker) Run scraper.py in scraper_async

```make start```

2. Encrypt your credentials 

```localhost:8000/encrypt_credentials```
This is purely to mock the behaviour of the frontend encryption, and will be removed in production.

```json
"credentials": {
    "username": "[INSERT-SMU-USERNAME]",
    "password": "[INSERT-SMU-PASSWORD]"
}
```

3. Send a POST request

```localhost:8000/scrape```
Pass your encrypted details into the credentials of the scraper

```json
{
    "credentials": {
        "username": "[INSERT-SMU-USERNAME]",
        "password": "[INSERT-SMU-PASSWORD]"
    },
    "date_raw": "4 November 2024",
    "duration_hours": 2.5,
    "start_time": "11:00",
    "building_names": [
        "School of Accountancy",
        "School of Computing & Information Systems 1"
    ],
    "floors": [
        "Basement 1",
        "Level 1",
        "Level 2",
        "Level 4"
    ],
    "facility_types": [
        "Meeting Pod",
        "Group Study Room"
    ],
    "equipment": []
}
```

3. api.py returns `scraped_log.json`:
```json
"config": {
    "date": "specified_date",
    "start_time": "specified_start_time",
    "end_time": "specified_end_time",
    "duration": 0, /* any integer value */
    "building_names": [
        "specified_schools"
    ],
    "floors": [
        "specified_floors"
    ],
    "facility_types": [
        "specified_facility_types"
    ],
    "room_capacity": 0, /* any integer value */
    "equipment": [
        "specified_equipment"
    ]
},
```

* Scraped results

```json
"result": {
    "room_1": [
        {
            "timeslot": "timeslot_1",
            "available": false,
            "status": "Not available",
            "details": null
        },
        {
            "timeslot": "timeslot_2",
            "available": false,
            "status": "Booked",
            "details": {
                "Booking Time": "...",
                "Booking Status": "...",
                "Booking Reference Number": "...",
                "Booked for User Name": "...",
                "Booked for User Org Unit": "",
                "Booked for User Email Address": "...",
                "Use Type": "...",
                "Purpose of Booking": "..."
            }
        }
    ],
    "room_2": [
        /* ... */
    ],
    /* ... */
}
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
	    <td align="center">
                <a href="https://github.com/weisintai">
                    <img src="https://avatars.githubusercontent.com/u/59339889?v=4" width="100;" alt="weisintai"/>
                    <br/>
                    <sub><b>weisintai</b></sub>
                </a>
            </td>
        </tr>
	<tbody>
</table>
