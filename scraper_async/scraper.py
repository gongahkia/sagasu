import os
import json
import asyncio
import itertools
import aiofiles
from dateutil.parser import parse
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

# Helper Functions
def generate_30_min_intervals():
    intervals = []
    start = datetime.strptime("00:00", "%H:%M")
    end = datetime.strptime("23:59", "%H:%M")
    while start <= end:
        interval_end = (start + timedelta(minutes=30)).strftime("%H:%M")
        intervals.append(f"{start.strftime('%H:%M')}-{interval_end}")
        start += timedelta(minutes=30)
    return intervals

def remove_duplicates_preserve_order(lst):
    seen = set()
    result = []
    for item in lst:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result

def fill_missing_timeslots(room_schedule, target_timeslot_array):
    new_schedule = []
    for slot in room_schedule:
        if slot["timeslot"] == target_timeslot_array[0]:  # already exists
            new_schedule.append(slot)
            del target_timeslot_array[0]
        else:  # does not yet exist
            new_timeslot = target_timeslot_array.pop(0)
            new_schedule.append({
                "timeslot": new_timeslot,
                "available": True,
                "status": "Available for booking",
                "details": None
            })
            new_schedule.append(slot)  # Insert the existing slot
            del target_timeslot_array[0]
    return new_schedule

def pretty_print_json(json_object):
    print(json.dumps(json_object, indent=4)) 

async def write_json(json_object, filename):
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    async with aiofiles.open(filename, 'w') as json_file:
        await json_file.write(json.dumps(json_object, indent=4))
    print(f"JSON file written to filepath: {filename}")

async def convert_room_capacity(room_capacity_raw, capacity_mapping):
    for condition, value in capacity_mapping.items():
        if condition(room_capacity_raw):
            return value
    return "MoreThan100Pax"

def calculate_end_time(valid_time_array, start_time, duration_hrs):
    start_hours, start_minutes = map(int, start_time.split(":"))
    total_minutes = (start_hours * 60 + start_minutes) + int(duration_hrs * 60)
    end_hours = (total_minutes // 60) % 24
    end_minutes = total_minutes % 60
    end_time = f"{end_hours:02}:{end_minutes:02}"
    closest_time = min(valid_time_array, key=lambda t: abs((int(t.split(":")[0]) * 60 + int(t.split(":")[1])) - (end_hours * 60 + end_minutes)))
    return [closest_time, end_time]

def format_date(date_input):
    try:
        date_obj = parse(date_input)
        return date_obj.strftime("%d-%b-%Y")
    except ValueError:
        return "Invalid date format"

def split_bookings_by_day(bookings):
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

def add_missing_timeslots(booking_details, target_timeslot_array):
    current_time = "00:00"
    complete_booking_details = []
    for timeslot in target_timeslot_array:
        found = False
        for booking in booking_details:
            if booking["timeslot"] == timeslot:
                complete_booking_details.append(booking)
                found = True
                break
        if not found:
            complete_booking_details.append({
                "timeslot": timeslot,
                "available": True,
                "status": "Unbooked",
                "details": None
            })
    return complete_booking_details

# Scraping Function
async def scrape_smu_fbs(request, constants):
    """
    Asynchronously handle automated login to SMU FBS and scrape booked timeslots.
    """
    VALID_TIME = constants['valid_time']
    VALID_ROOM_CAPACITY_FORMATTED = constants['valid_room_capacity']
    VALID_BUILDING = constants['valid_buildings']
    VALID_FLOOR = constants['valid_floors']
    VALID_FACILITY_TYPE = constants['valid_facility_types']
    VALID_EQUIPMENT = constants['valid_equipment']
    
    DATE_RAW = request.date_raw
    DATE_FORMATTED = format_date(DATE_RAW) 
    DURATION_HRS = request.duration_hours
    START_TIME = request.start_time
    END_TIME = calculate_end_time(VALID_TIME, START_TIME, DURATION_HRS)[0]
    ROOM_CAPACITY_RAW = 7  # Assuming default, or modify as needed

    # Define room capacity mapping
    capacity_mapping = {
        lambda x: x < 5: "LessThan5Pax",
        lambda x: 5 <= x <= 10: "From6To10Pax",
        lambda x: 11 <= x <= 15: "From11To15Pax",
        lambda x: 16 <= x <= 20: "From16To20Pax",
        lambda x: 21 <= x <= 50: "From21To50Pax",
        lambda x: 51 <= x <= 100: "From51To100Pax",
    }
    ROOM_CAPACITY_FORMATTED = await convert_room_capacity(ROOM_CAPACITY_RAW, capacity_mapping)
    BUILDING_ARRAY = request.building_names  # Optional
    FLOOR_ARRAY = request.floors           # Optional
    FACILITY_TYPE_ARRAY = request.facility_types  # Optional
    EQUIPMENT_ARRAY = request.equipment    # Optional
    SCREENSHOT_FILEPATH = constants['screenshot_filepath']
    BOOKING_LOG_FILEPATH = constants['booking_log_filepath']
    
    # Ensure directories exist
    os.makedirs(SCREENSHOT_FILEPATH, exist_ok=True)
    os.makedirs(BOOKING_LOG_FILEPATH, exist_ok=True)
    
    errors = []
    local_credentials = request.credentials  # Directly use credentials from request
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, slow_mo=1000)  # for easier debugging
            # browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
    
            try:
                # ---------- LOGIN CREDENTIALS ----------
                await page.goto(constants['target_url'])
    
                await page.wait_for_selector('input#userNameInput')
                await page.wait_for_selector('input#passwordInput')
                await page.wait_for_selector('span#submitButton')
    
                print(f"Navigating to {constants['target_url']}")
    
                await page.fill("input#userNameInput", local_credentials.username)
                await page.fill("input#passwordInput", local_credentials.password)
                await page.click("span#submitButton")
    
                await page.wait_for_timeout(6000)
                await page.wait_for_load_state('networkidle')
    
                # ---------- NAVIGATE TO GIVEN DATE ----------
                frame = page.frame(name="frameBottom") 
                if not frame:
                    errors.append("Frame 'frameBottom' could not be found.")
                else:
                    frame = page.frame(name="frameContent")
                    while True:
                        current_date_element = await frame.query_selector("input#DateBookingFrom_c1_textDate")
                        current_date_value = await current_date_element.get_attribute("value")
                        if current_date_value == DATE_FORMATTED:
                            print(f"Final day is {current_date_value}")
                            break
                        else:
                            print(f"Current day is {current_date_value}")
                            print("Navigating to the next day...")
                            await frame.click("a#BtnDpcNext.btn")
                            await frame.wait_for_timeout(1500)
    
                    # ---------- EXTRACT PAGE DATA ----------
    
                    # ----- SELECT START TIME -----
                    select_start_time_input = await frame.query_selector("select#TimeFrom_c1_ctl04")
                    if select_start_time_input:
                        await frame.evaluate(f'document.querySelector("select#TimeFrom_c1_ctl04").value = "{START_TIME}"')
                        print(f"Selected start time to be {START_TIME}")
                    else:
                        print("Select element for start time not found")
    
                    # ----- SELECT END TIME -----
                    select_end_time_input = await frame.query_selector_all("select#TimeTo_c1_ctl04")
                    if select_end_time_input:
                        await frame.evaluate(f'document.querySelector("select#TimeTo_c1_ctl04").value = "{END_TIME}"')
                        print(f"Selected end time to be {END_TIME}")
                    else:
                        print("Select element for end time not found")
    
                    await frame.wait_for_timeout(3000)
    
                    # ----- APPLY FILTERS -----
                    
                    # ----- SELECT BUILDINGS -----
                    if BUILDING_ARRAY:
                        if await frame.is_visible('#DropMultiBuildingList_c1_textItem'):
                            await frame.click('#DropMultiBuildingList_c1_textItem')  # Opens the dropdown list
                            for building_name in BUILDING_ARRAY:
                                await frame.click(f'text="{building_name}"')
                                print(f"Selecting {building_name}...")
                            await frame.evaluate("popup.hide()")  # Closes the dropdown list
                            await page.wait_for_load_state('networkidle')
                            await frame.wait_for_timeout(3000)
    
                    # ----- SELECT FLOORS -----
                    if FLOOR_ARRAY:
                        if await frame.is_visible('#DropMultiFloorList_c1_textItem'):
                            await frame.click('#DropMultiFloorList_c1_textItem')  # Opens the dropdown list
                            for floor_name in FLOOR_ARRAY:
                                await frame.click(f'text="{floor_name}"')
                                print(f"Selecting {floor_name}...")
                            await frame.evaluate("popup.hide()")  # Closes the dropdown list
                            await page.wait_for_load_state('networkidle')
                            await frame.wait_for_timeout(3000)
    
                    # ----- SELECT FACILITY TYPE -----
                    if FACILITY_TYPE_ARRAY:
                        if await frame.is_visible('#DropMultiFacilityTypeList_c1_textItem'):
                            await frame.click('#DropMultiFacilityTypeList_c1_textItem')  # Opens the dropdown list
                            for facility_type_name in FACILITY_TYPE_ARRAY:
                                await frame.click(f'text="{facility_type_name}"')
                                print(f"Selecting {facility_type_name}...")
                            await frame.evaluate("popup.hide()")  # Closes the dropdown list
                            await page.wait_for_load_state('networkidle')
                            await frame.wait_for_timeout(3000)
    
                    # ----- SELECT ROOM CAPACITY -----
                    select_capacity_input = await frame.query_selector("select#DropCapacity_c1")
                    if select_capacity_input:
                        await frame.evaluate(f'document.querySelector("select#DropCapacity_c1").value = "{ROOM_CAPACITY_FORMATTED}"')
                        print(f"Selected room capacity to be {ROOM_CAPACITY_FORMATTED}")
                    else:
                        print("Select element for room capacity not found")
    
                    await frame.wait_for_timeout(3000)
    
                    # ----- SELECT EQUIPMENT -----
                    if EQUIPMENT_ARRAY:
                        if await frame.is_visible('#DropMultiEquipmentList_c1_textItem'):
                            await frame.click('#DropMultiEquipmentList_c1_textItem')  # Opens the dropdown list
                            for equipment_name in EQUIPMENT_ARRAY:
                                await frame.click(f'text="{equipment_name}"')
                                print(f"Selecting {equipment_name}...")
                            await frame.evaluate("popup.hide()")  # Closes the dropdown list
                            await page.wait_for_load_state('networkidle')
                            await frame.wait_for_timeout(3000)
    
                    await page.screenshot(path=os.path.join(SCREENSHOT_FILEPATH, "0.png"))
    
                    # ----- ROOM EXTRACTION -----
                    await frame.wait_for_selector("table#GridResults_gv")
                    matching_rooms = []
                    rows = await frame.query_selector_all("table#GridResults_gv tbody tr")
                    for row in rows:
                        tds = await row.query_selector_all("td")
                        if len(tds) > 1: 
                            room_text = (await tds[1].inner_text()).strip()
                            matching_rooms.append(room_text)
                    if not matching_rooms:
                        print("No rooms fitting description found.")
                        print("Closing browser...")
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
                        
                        pretty_print_json(final_booking_log)
    
                        await write_json(final_booking_log, os.path.join(BOOKING_LOG_FILEPATH, "scraped_log.json"))
    
                        return errors
    
                    else:
                        print(f"{len(matching_rooms)} rooms fitting description found:")
                        for room in matching_rooms:
                            print(f"- {room}")
    
                        # ----- SEARCH AVAILABILITY -----
                        await frame.click("a#CheckAvailability")
                        print("Submitting search availability request...")
                        await page.wait_for_load_state("networkidle")
                        await page.wait_for_timeout(6000)
    
                        # ---------- VIEW TIMESLOTS ----------
    
                            # ----- CAPTURE SCREENSHOT OF TIMESLOTS -----
                        await page.screenshot(path=os.path.join(SCREENSHOT_FILEPATH, "1.png"))
    
                            # ----- SCRAPE TIMESLOTS -----
                        frame = page.frame(name="frameBottom")
                        frame = page.frame(name="frameContent")
                        room_names_array_raw = [await room.inner_text() for room in await frame.query_selector_all("div.scheduler_bluewhite_rowheader_inner")]
                        room_names_array_sanitised = [el for el in room_names_array_raw if el not in VALID_BUILDING]
                        bookings_elements = await frame.query_selector_all("div.scheduler_bluewhite_event.scheduler_bluewhite_event_line0")
                        bookings_array_raw = [await active_booking.get_attribute("title") for active_booking in bookings_elements]
                        bookings_array_sanitised = split_bookings_by_day(bookings_array_raw)
                        
                        room_timeslot_map = {}
    
                        for index, booking_array in enumerate(bookings_array_sanitised):
                            booking_details = []
    
                            for booking in booking_array:
                                if booking.startswith("Booking Time:"):  # Existing booking
                                    room_details = {}
                                    for el in booking.split("\n"):
                                        if el.startswith("Booking Time:"):
                                            local_timeslot = el.lstrip("Booking Time: ")
                                        else:
                                            key, value = el.split(": ", 1)
                                            room_details[key] = value
                                    active_booking_details = {
                                        "timeslot": local_timeslot,
                                        "available": False,
                                        "status": "Booked",
                                        "details": room_details
                                    }
                                    booking_details.append(active_booking_details)
    
                                elif booking.endswith("(not available)"):  # Not available booking
                                    time = booking.split(") (")[0]
                                    na_booking_details = {
                                        "timeslot": time.lstrip("("),
                                        "available": False,
                                        "status": "Not available",
                                        "details": None
                                    }
                                    booking_details.append(na_booking_details)
    
                                else: 
                                    # Edge case checking
                                    print(f"Unrecognised timeslot format, logged here: {booking}")
    
                            room_timeslot_map[room_names_array_sanitised[index]] = fill_missing_timeslots(booking_details, generate_30_min_intervals())
    
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
                        
                        await write_json(final_booking_log, os.path.join(BOOKING_LOG_FILEPATH, "scraped_log.json"))
    
            except Exception as e:
                errors.append(f"Error processing {constants['target_url']}: {e}")
    
            finally:
                print("Closing browser...")
                await browser.close() 
    
    except Exception as e:
        errors.append(f"Failed to initialize Playwright: {e}")
    
    return errors
