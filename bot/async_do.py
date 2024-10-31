"""
FUA 

* add buttons for user to specify their configurations (school, floor etc.) after the username and password has been specified under settings, consider splitting settings into 2 buttons, configuration and authentication then save config locally to be referenced later
* consider including a returned screenshot of the generated timetable to be sent to the user as an additional feature if they request it? maybe an additional button
"""

import os
import re
import json
import time
import asyncio
import itertools
from dateutil.parser import parse
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright

def pretty_print_json(json_object):
    """
    pretty prints json data to 
    the cli for easy viewing
    """
    print(json.dumps(json_object, indent=4)) 

def write_json(json_object, filename):
    """
    write a python dictionary to a 
    local JSON file
    """
    with open(filename, 'w') as json_file:
        json.dump(json_object, json_file, indent=4) 
        print(f"json file written to filepath: {filename}")

def read_credentials(credentials_filepath):
    """
    read locally stored
    credentials json file
    """
    try:
        with open(credentials_filepath, 'r') as file:
            data = json.load(file)
        return data
    except FileNotFoundError:
        print("File not found. Please check the file path.")
    except json.JSONDecodeError:
        print("Error decoding JSON. Please check the file format.")

def convert_room_capacity(room_capacity_raw):
    """
    convert integer room capacity
    to the fbs string required value
    """
    if room_capacity_raw < 5:
        return "LessThan5Pax"
    elif 5 <= room_capacity_raw <= 10:
        return "From6To10Pax"
    elif 11 <= room_capacity_raw <= 15:
        return "From11To15Pax"
    elif 16 <= room_capacity_raw <= 20:
        return "From16To20Pax"
    elif 21 <= room_capacity_raw <= 50:
        return "From21To50Pax"
    elif 51 <= room_capacity_raw <= 100:
        return "From51To100Pax"
    else:
        return "MoreThan100Pax"

def calculate_end_time(valid_time_array, start_time, duration_hrs):
    """
    calculate end time based on 
    a provided start and 
    duration time
    """
    start_hours, start_minutes = map(int, start_time.split(":"))
    total_minutes = (start_hours * 60 + start_minutes) + int(duration_hrs * 60)
    end_hours = (total_minutes // 60) % 24
    end_minutes = total_minutes % 60
    end_time = f"{end_hours:02}:{end_minutes:02}"
    closest_time = min(valid_time_array, key=lambda t: abs((int(t.split(":")[0]) * 60 + int(t.split(":")[1])) - (end_hours * 60 + end_minutes)))
    return [closest_time, end_time]

def format_date(date_input):
    """
    receives a date string of the below formats
    
    YYYY-MM-DD
    DD-MM-YYYY
    MM/DD/YYYY
    DD Month YYYY
    Month DD, YYYY

    and converts it to the DD-MMM-YYYY format 
    accepted by SMU FBS
    """
    try:
        date_obj = parse(date_input)
        return date_obj.strftime("%d-%b-%Y")
    except ValueError:
        return "Invalid date format"

def split_bookings_by_day(bookings):
    """
    splits scraped bookings by day
    """
    days = []
    current_day = []
    for booking in bookings:
        if '(not available)' in booking:
            if not current_day:
                current_day.append(booking)
            else:
                current_day.append(booking)
                days.append(current_day)
                current_day = [] 
        else:
            if current_day:
                current_day.append(booking)
    return days

def add_missing_timeslots(booking_details):
    """
    finds all available timeslots and
    accounts for all these periods 
    with a 30 minute interval
    """
    start_time = "00:00"
    end_time = "23:30"
    time_format = "%H:%M"
    start_dt = datetime.strptime(start_time, time_format)
    end_dt = datetime.strptime(end_time, time_format)
    existing_timeslots = [(datetime.strptime(slot["timeslot"].split('-')[0], time_format), datetime.strptime(slot["timeslot"].split('-')[1], time_format)) for slot in booking_details]
    complete_booking_details = []
    current_time = start_dt
    while current_time <= end_dt:
        for start, end in existing_timeslots:
            if start <= current_time < end:
                complete_booking_details.append({"timeslot": f"{current_time.strftime(time_format)}-{end.strftime(time_format)}", "available": False, "status": "Not available", "details": None})
                break
        else:
            next_time = current_time + timedelta(minutes=30)
            complete_booking_details.append({"timeslot": f"{current_time.strftime(time_format)}-{next_time.strftime(time_format)}", "available": True, "status": "Unbooked", "details": None})
        current_time += timedelta(minutes=30)
    return complete_booking_details

def remove_duplicates_preserve_order(lst):
    """
    removes all duplicates while preserving 
    order of list elements
    """
    seen = set()
    result = []
    for item in lst:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result

def fill_missing_timeslots(room_schedule):
    """
    uses the benefits of a sorted 
    queue data structure to handle
    missing intervals in a range of timings
    """
    target_timeslot_array = []
    new_schedule = []
    timeline_overview = remove_duplicates_preserve_order(list(itertools.chain.from_iterable([slot["timeslot"].split("-") for slot in room_schedule])))
    for i in range(len(timeline_overview)-1):
        start = timeline_overview[i]
        end = timeline_overview[i+1]
        target_timeslot = f"{start}-{end}"
        target_timeslot_array.append(target_timeslot)
    for slot in room_schedule:
        # print(slot)
        if slot["timeslot"] == target_timeslot_array[0]: # already exists
            new_schedule.append(slot)
            del target_timeslot_array[0]
        else: # does not yet exist
            new_timeslot = target_timeslot_array.pop(0)
            new_schedule.append({
                "timeslot": new_timeslot,
                "available": True,
                "status": "Available for booking",
                "details": None
            })
            new_schedule.append(slot) 
            del target_timeslot_array[0] 
    return new_schedule

async def scrape_smu_fbs(base_url, user_email, user_password):
    """
    Handle automated login to SMU FBS based on
    personal credentials.json and scrapes all booked
    timeslots for the filtered rooms.
    """

    VALID_TIME = [
        "00:00", 
        "00:30", 
        "01:00", 
        "01:30", 
        "02:00", 
        "02:30", 
        "03:00", 
        "03:30",
        "04:00", 
        "04:30", 
        "05:00", 
        "05:30", 
        "06:00", 
        "06:30", 
        "07:00", 
        "07:30",
        "08:00", 
        "08:30", 
        "09:00", 
        "09:30", 
        "10:00", 
        "10:30", 
        "11:00", 
        "11:30",
        "12:00", 
        "12:30", 
        "13:00", 
        "13:30", 
        "14:00", 
        "14:30", 
        "15:00", 
        "15:30",
        "16:00", 
        "16:30", 
        "17:00", 
        "17:30", 
        "18:00", 
        "18:30", 
        "19:00", 
        "19:30",
        "20:00", 
        "20:30", 
        "21:00", 
        "21:30", 
        "22:00", 
        "22:30", 
        "23:00", 
        "23:30"
    ]
    VALID_ROOM_CAPACITY_FORMATTED = [
        "LessThan5Pax", 
        "From6To10Pax", 
        "From11To15Pax", 
        "From16To20Pax", 
        "From21To50Pax", 
        "From51To100Pax", 
        "MoreThan100Pax"
    ] 
    VALID_BUILDING = [
        "Administration Building", 
        "Campus Open Spaces - Events/Activities", 
        "Concourse - Room/Lab", 
        "Lee Kong Chian School of Business", 
        "Li Ka Shing Library", 
        "Prinsep Street Residences", 
        "School of Accountancy", 
        "School of Computing & Information Systems 1", 
        "School of Economics/School of Computing & Information Systems 2", 
        "School of Social Sciences/College of Integrative Studies", 
        "SMU Connexion", 
        "Yong Pung How School of Law/Kwa Geok Choo Law Library"
    ]
    VALID_FLOOR = [
        "Basement 0", 
        "Basement 2", 
        "Level 1", 
        "Level 2", 
        "Level 3", 
        "Level 4", 
        "Level 5", 
        "Level 6", 
        "Level 7", 
        "Level 8", 
        "Level 9", 
        "Level 10", 
        "Level 11", 
        "Level 12", 
        "Level 13", 
        "Level 14"
    ]
    VALID_FACILITY_TYPE = [
        "Chatterbox", 
        "Classroom", 
        "Group Study Room", 
        "Hostel Facilities", 
        "Meeting Pod", 
        "MPH / Sports Hall", 
        "Phone Booth", 
        "Project Room", 
        "Project Room (Level 5)", 
        "Seminar Room", 
        "SMUC Facilities", 
        "Student Activities Area", 
        "Study Booth"
    ]
    VALID_EQUIPMENT = [
        "Classroom PC", 
        "Classroom Prompter", 
        "Clip-on Mic", 
        "Doc Camera", 
        "DVD Player", 
        "Gooseneck Mic", 
        "Handheld Mic", 
        "Hybrid (USB connection)", 
        "In-room VC System", 
        "Projector", 
        "Rostrum Mic", 
        "Teams Room", 
        "Teams Room NEAT Board", 
        "TV Panel", 
        "USB Connection VC room", 
        "Video Recording", 
        "Wired Mic", 
        "Wireless Projection"
    ]

    DATE_RAW = "1 november 2024"
    DATE_FORMATTED = format_date(DATE_RAW)
    DURATION_HRS = 2.5
    START_TIME = "11:00"
    END_TIME = calculate_end_time(VALID_TIME, START_TIME, DURATION_HRS)[0]
    ROOM_CAPACITY_RAW = 7
    ROOM_CAPACITY_FORMATTED = convert_room_capacity(ROOM_CAPACITY_RAW)
    BUILDING_ARRAY = ["School of Accountancy", "School of Computing & Information Systems 1"]
    FLOOR_ARRAY = ["Basement 1", "Level 1", "Level 2", "Level 4"]
    FACILITY_TYPE_ARRAY = ["Meeting Pod", "Group Study Room"]
    EQUIPMENT_ARRAY = []
    SCREENSHOT_FILEPATH = "./screenshot_log/"
    BOOKING_LOG_FILEPATH = "./booking_log/"

    errors = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, slow_mo=1000)  # for easier debugging
            page = await browser.new_page()

            try:
                # ---------- LOGIN CREDENTIALS ----------
                await page.goto(base_url)
                await page.wait_for_selector('input#userNameInput')
                await page.wait_for_selector('input#passwordInput')
                await page.wait_for_selector('span#submitButton')

                print(f"navigating to {base_url}")

                await page.fill("input#userNameInput", user_email)
                await page.fill("input#passwordInput", user_password)
                await page.click("span#submitButton")

                await page.wait_for_timeout(1000)
                await page.wait_for_load_state('networkidle')

                # ---------- NAVIGATE TO GIVEN DATE ----------
                frame = page.frame(name="frameBottom")
                if not frame:
                    errors.append("Frame bottom could not be found.")
                else:
                    frame = page.frame(name="frameContent")
                    while True:
                        current_date_value = await frame.query_selector("input#DateBookingFrom_c1_textDate")
                        current_date_value = await current_date_value.get_attribute("value")
                        if current_date_value == DATE_FORMATTED:
                            print(f"final day is {current_date_value}")
                            break
                        else:
                            print(f"current day is {current_date_value}")
                            print("navigating to the next day...")
                            await frame.click("a#BtnDpcNext.btn")
                            await page.wait_for_timeout(1000)

                # ---------- EXTRACT PAGE DATA ----------
                select_start_time_input = await frame.query_selector("select#TimeFrom_c1_ctl04")
                if select_start_time_input:
                    await frame.evaluate(f'document.querySelector("select#TimeFrom_c1_ctl04").value = "{START_TIME}"')
                    print(f"Selected start time to be {START_TIME}")
                else:
                    print("Select element for start time not found")

                select_end_time_input = await frame.query_selector_all("select#TimeTo_c1_ctl04")
                if select_end_time_input:
                    await frame.evaluate(f'document.querySelector("select#TimeTo_c1_ctl04").value = "{END_TIME}"')
                    print(f"Selected end time to be {END_TIME}")
                else:
                    print("Select element for end time not found")

                await page.wait_for_timeout(1000)

                # ----- SELECT BUILDINGS -----
                if BUILDING_ARRAY:
                    if await frame.is_visible('#DropMultiBuildingList_c1_textItem'):
                        await frame.click('#DropMultiBuildingList_c1_textItem')  # opens the dropdown list
                        for building_name in BUILDING_ARRAY:
                            await frame.click(f'text="{building_name}"')
                            print(f"selecting {building_name}...")
                        await frame.evaluate("popup.hide()")  # closes the dropdown list
                        await page.wait_for_load_state('networkidle')
                        await page.wait_for_timeout(1000)

                # ----- SELECT FLOORS -----
                if FLOOR_ARRAY:
                    if await frame.is_visible('#DropMultiFloorList_c1_textItem'):
                        await frame.click('#DropMultiFloorList_c1_textItem')  # opens the dropdown list
                        for floor_name in FLOOR_ARRAY:
                            await frame.click(f'text="{floor_name}"')
                            print(f"selecting {floor_name}...")
                        await frame.evaluate("popup.hide()")  # closes the dropdown list
                        await page.wait_for_load_state('networkidle')
                        await page.wait_for_timeout(1000)

                # ----- SELECT FACILITY TYPE -----
                if FACILITY_TYPE_ARRAY:
                    if await frame.is_visible('#DropMultiFacilityTypeList_c1_textItem'):
                        await frame.click('#DropMultiFacilityTypeList_c1_textItem')  # opens the dropdown list
                        for facility_type_name in FACILITY_TYPE_ARRAY:
                            await frame.click(f'text="{facility_type_name}"')
                            print(f"selecting {facility_type_name}...")
                        await frame.evaluate("popup.hide()")  # closes the dropdown list
                        await page.wait_for_load_state('networkidle')
                        await page.wait_for_timeout(1000)

                # ----- SELECT ROOM CAPACITY -----
                select_capacity_input = await frame.query_selector("select#DropCapacity_c1")
                if select_capacity_input:
                    await frame.evaluate(f'document.querySelector("select#DropCapacity_c1").value = "{ROOM_CAPACITY_FORMATTED}"')
                    print(f"Selected room capacity to be {ROOM_CAPACITY_FORMATTED}")
                else:
                    print("Select element for room capacity not found")
                await page.wait_for_timeout(1000)

                # ----- ROOM EXTRACTION -----
                await frame.wait_for_selector("table#GridResults_gv")
                matching_rooms = []
                rows = await frame.query_selector_all("table#GridResults_gv tbody tr")
                for row in rows:
                    tds = await row.query_selector_all("td")
                    if len(tds) > 1: 
                        matching_rooms.append(await tds[1].inner_text())
                if not matching_rooms:
                    print("No rooms fitting description found.")
                    print("closing browser...")
                    await browser.close()

                    current_datetime = datetime.now()
                    formatted_datetime = current_datetime.strftime('%Y-%m-%d %H:%M:%S')

                    final_booking_log = {
                        "metrics": {
                            "scraping_date": formatted_datetime,
                        },
                        "scraped": {
                            "config": {
                                "date": DATE_FORMATTED,
                                "start_time": START_TIME,
                                "end_time": END_TIME,
                                "duration": DURATION_HRS,
                                "building_names": BUILDING_ARRAY,
                                "floors": FLOOR_ARRAY,
                                "facility_types": FACILITY_TYPE_ARRAY,
                                "room_capacity": ROOM_CAPACITY_FORMATTED,
                                "equipment": EQUIPMENT_ARRAY
                            },
                            "result": {}
                        }
                    }

                    # pretty_print_json(final_booking_log)
                    # write_json(final_booking_log, f"{BOOKING_LOG_FILEPATH}scraped_log.json")

                    return [errors, final_booking_log]

                else:
                    print(f"{len(matching_rooms)} rooms fitting description found.")
                    for room in matching_rooms:
                        print(f"-{room}")

                    # ----- SEARCH AVAILABILITY -----
                    await frame.click("a#CheckAvailability")
                    print("Submitting search availability request...")
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(1000)

                    # ---------- VIEW TIMESLOTS ----------
                    # await page.screenshot(path=f"{SCREENSHOT_FILEPATH}1.png")

                    frame = page.frame(name="frameBottom")
                    frame = page.frame(name="frameContent")
                    room_names_array_raw = [await room.inner_text() for room in await frame.query_selector_all("div.scheduler_bluewhite_rowheader_inner")]
                    room_names_array_sanitised = [el for el in room_names_array_raw if el not in VALID_BUILDING]
                    bookings_array_raw = [await active_bookings.get_attribute("title") for active_bookings in await frame.query_selector_all("div.scheduler_bluewhite_event.scheduler_bluewhite_event_line0")]
                    bookings_array_sanitised = split_bookings_by_day(bookings_array_raw)

                    # print(room_names_array_sanitised)
                    # print(bookings_array_sanitised)

                    room_timeslot_map = {}

                    for index, booking_array in enumerate(bookings_array_sanitised):
                        booking_details = []

                        for booking in booking_array:
                            if booking.startswith("Booking Time:"):  # existing booking
                                room_details = {}
                                for el in booking.split("\n"):
                                    if el.startswith("Booking Time:"):
                                        local_timeslot = el.lstrip("Booking Time: ")
                                    room_details[el.split(": ")[0]] = el.split(": ")[1]
                                active_booking_details = {
                                    "timeslot": local_timeslot,
                                    "available": False,
                                    "status": "Booked",
                                    "details": room_details
                                }
                                booking_details.append(active_booking_details)

                            elif booking.endswith("(not available)"):  # not available booking
                                time = booking.split(") (")[0]
                                na_booking_details = {
                                    "timeslot": time.lstrip("("),
                                    "available": False,
                                    "status": "Not available",
                                    "details": None
                                }
                                booking_details.append(na_booking_details)

                            else:
                                # edge case checking
                                print(f"Unrecognised timeslot format {booking}")

                        room_timeslot_map[room_names_array_sanitised[index]] = booking_details

                    # print(room_timeslot_map)

                    current_datetime = datetime.now()
                    formatted_datetime = current_datetime.strftime('%Y-%m-%d %H:%M:%S')

                    final_booking_log = {
                        "metrics": {
                            "scraping_date": formatted_datetime,
                        },
                        "scraped": {
                            "config": {
                                "date": DATE_FORMATTED,
                                "start_time": START_TIME,
                                "end_time": END_TIME,
                                "duration": DURATION_HRS,
                                "building_names": BUILDING_ARRAY,
                                "floors": FLOOR_ARRAY,
                                "facility_types": FACILITY_TYPE_ARRAY,
                                "room_capacity": ROOM_CAPACITY_FORMATTED,
                                "equipment": EQUIPMENT_ARRAY
                            },
                            "result": room_timeslot_map
                        }
                    }

                    # print(final_booking_log)
                    print("finished scraping")

                    # write_json(final_booking_log, f"{BOOKING_LOG_FILEPATH}booking_log.json")
                    # await page.screenshot(path=f"{SCREENSHOT_FILEPATH}2.png")

            except Exception as e:
                errors.append(f"Error occurred during scraping process: {e}")

            finally:
                print("Closing browser...")
                await browser.close()

    except Exception as e:
        errors.append(f"Failed to launch browser: {e}")

    return [errors, final_booking_log]