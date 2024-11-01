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

### Secret

Specify your credentials within a `.env` file that follows the below format.

```env
USERNAME="fill_your_placeholder_username"
PASSWORD="fill_your_placeholder_password"
```

### Scraping

Scraping returns `scraped_log.json`, which contains the following.

* Scraping metadata

```json
"metrics": {
    "scraping_date": "current_date"
},
```

* Scraping configuration

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
