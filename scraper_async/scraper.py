import os
import re
import json
import time
import itertools
import yaml
from dateutil.parser import parse
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# Load configuration from config.yaml
def load_config(config_path='config.yaml'):
    try:
        with open(config_path, 'r') as file:
            config = yaml.safe_load(file)
        return config
    except FileNotFoundError:
        print(f"Configuration file {config_path} not found.")
        exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file: {e}")
        exit(1)

config = load_config()

# Function Definitions (unchanged, but you can further optimize if needed)

def generate_30_min_intervals():
    """
    Generate all possible 30-minute intervals from 00:00 to 23:59
    """
    intervals = []
    start = datetime.strptime("00:00", "%H:%M")
    end = datetime.strptime("23:59", "%H:%M")
    while start <= end:
        interval_end = (start + timedelta(minutes=30)).strftime("%H:%M")
        intervals.append(f"{start.strftime('%H:%M')}-{interval_end}")
        start += timedelta(minutes=30)
    return intervals

def remove_duplicates_preserve_order(lst):
    """
    Removes all duplicates while preserving order of list elements
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
    Uses the benefits of a sorted queue data structure to handle
    missing intervals in a range of timings
    """
    target_timeslot_array = []
    new_schedule = []
    timeline_overview = remove_duplicates_preserve_order(
        list(itertools.chain.from_iterable([slot["timeslot"].split("-") for slot in room_schedule]))
    )
    for i in range(len(timeline_overview) - 1):
        start = timeline_overview[i]
        end = timeline_overview[i + 1]
        target_timeslot = f"{start}-{end}"
        target_timeslot_array.append(target_timeslot)
    
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
    """
    Pretty prints JSON data to the CLI for easy viewing
    """
    print(json.dumps(json_object, indent=4)) 

def write_json(json_object, filename):
    """
    Write a Python dictionary to a local JSON file
    """
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, 'w') as json_file:
        json.dump(json_object, json_file, indent=4) 
        print(f"JSON file written to filepath: {filename}")

def read_credentials(credentials_filepath):
    """
    Read locally stored credentials JSON file
    """
    try:
        with open(credentials_filepath, 'r') as file:
            data = json.load(file)
        return data
    except FileNotFoundError:
        print("Credentials file not found. Please check the file path.")
        exit(1)
    except json.JSONDecodeError:
        print("Error decoding JSON. Please check the credentials file format.")
        exit(1)

def convert_room_capacity(room_capacity_raw, capacity_mapping):
    """
    Convert integer room capacity to the formatted string required value
    """
    for key, value in capacity_mapping.items():
        if key(room_capacity_raw):
            return value
    return "MoreThan100Pax"

def calculate_end_time(valid_time_array, start_time, duration_hrs):
    """
    Calculate end time based on a provided start and duration time
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
    Converts various date formats to DD-MMM-YYYY format
    """
    try:
        date_obj = parse(date_input)
        return date_obj.strftime("%d-%b-%Y")
    except ValueError:
        return "Invalid date format"

def split_bookings_by_day(bookings):
    """
    Splits scraped bookings by day
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
    Finds all available timeslots and accounts for these periods with a 30-minute interval
    """
    start_time = "00:00"
    end_time = "23:30"
    time_format = "%H:%M"
    start_dt = datetime.strptime(start_time, time_format)
    end_dt = datetime.strptime(end_time, time_format)
    existing_timeslots = [
        (
            datetime.strptime(slot["timeslot"].split('-')[0], time_format), 
            datetime.strptime(slot["timeslot"].split('-')[1], time_format)
        ) 
        for slot in booking_details
    ]
    complete_booking_details = []
    current_time = start_dt
    while current_time <= end_dt:
        for start, end in existing_timeslots:
            if start <= current_time < end:
                complete_booking_details.append({
                    "timeslot": f"{current_time.strftime(time_format)}-{end.strftime(time_format)}", 
                    "available": False, 
                    "status": "Booked", 
                    "details": None
                })
                break
        else:
            next_time = current_time + timedelta(minutes=30)
            complete_booking_details.append({
                "timeslot": f"{current_time.strftime(time_format)}-{next_time.strftime(time_format)}", 
                "available": True, 
                "status": "Unbooked", 
                "details": None
            })
        current_time += timedelta(minutes=30)
    return complete_booking_details

# Updated scrape_smu_fbs function to use configurations
def scrape_smu_fbs(config):
    """
    Handle automated login to SMU FBS based on personal credentials.json and scrapes all booked
    timeslots for the filtered rooms
    """
    VALID_TIME = config['valid_time']
    VALID_ROOM_CAPACITY_FORMATTED = config['valid_room_capacity']
    VALID_BUILDING = config['valid_buildings']
    VALID_FLOOR = config['valid_floors']
    VALID_FACILITY_TYPE = config['valid_facility_types']
    VALID_EQUIPMENT = config['valid_equipment']
    
    DATE_RAW = config['date_raw']
    DATE_FORMATTED = format_date(DATE_RAW) 
    DURATION_HRS = config['duration_hours']
    START_TIME = config['start_time']
    END_TIME = calculate_end_time(VALID_TIME, START_TIME, DURATION_HRS)[0]
    ROOM_CAPACITY_RAW = config.get('room_capacity_raw', 7)  # Optional: specify in config
    # Define room capacity mapping
    capacity_mapping = {
        lambda x: x < 5: "LessThan5Pax",
        lambda x: 5 <= x <= 10: "From6To10Pax",
        lambda x: 11 <= x <= 15: "From11To15Pax",
        lambda x: 16 <= x <= 20: "From16To20Pax",
        lambda x: 21 <= x <= 50: "From21To50Pax",
        lambda x: 51 <= x <= 100: "From51To100Pax",
    }
    ROOM_CAPACITY_FORMATTED = convert_room_capacity(ROOM_CAPACITY_RAW, capacity_mapping)
    BUILDING_ARRAY = config['building_names']
    FLOOR_ARRAY = config['floors']
    FACILITY_TYPE_ARRAY = config['facility_types']
    EQUIPMENT_ARRAY = config['equipment']
    SCREENSHOT_FILEPATH = config['screenshot_filepath']
    BOOKING_LOG_FILEPATH = config['booking_log_filepath']
    
    # Ensure directories exist
    os.makedirs(SCREENSHOT_FILEPATH, exist_ok=True)
    os.makedirs(BOOKING_LOG_FILEPATH, exist_ok=True)
    
    errors = []
    local_credentials = read_credentials(config['credentials_filepath'])
    
    try:
        p = sync_playwright().start() 
        browser = p.chromium.launch(headless=False, slow_mo=1000)  # for easier debugging
        # browser = p.chromium.launch(headless=True) 
        page = browser.new_page()

        try:
            # ---------- LOGIN CREDENTIALS ----------
            page.goto(config['target_url'])

            page.wait_for_selector('input#userNameInput')
            page.wait_for_selector('input#passwordInput')
            page.wait_for_selector('span#submitButton')

            print(f"Navigating to {config['target_url']}")

            username_input = page.query_selector("input#userNameInput")
            password_input = page.query_selector("input#passwordInput")
            signin_button = page.query_selector("span#submitButton")

            page.fill("input#userNameInput", local_credentials["username"])
            page.fill("input#passwordInput", local_credentials["password"])
            signin_button.click()

            page.wait_for_timeout(6000)
            page.wait_for_load_state('networkidle')

            # ---------- NAVIGATE TO GIVEN DATE ----------
            frame = page.frame(name="frameBottom") 
            if not frame:
                errors.append("Frame 'frameBottom' could not be found.")
            else:
                frame = page.frame(name="frameContent")
                while True:
                    current_date_value = frame.query_selector("input#DateBookingFrom_c1_textDate").get_attribute("value")
                    if current_date_value == DATE_FORMATTED:
                        print(f"Final day is {current_date_value}")
                        break
                    else:
                        print(f"Current day is {current_date_value}")
                        print("Navigating to the next day...")
                        frame.query_selector("a#BtnDpcNext.btn").click()
                        frame.wait_for_timeout(1500)

                # ---------- EXTRACT PAGE DATA ----------

                # ----- SELECT START TIME -----
                select_start_time_input = frame.query_selector("select#TimeFrom_c1_ctl04")
                if select_start_time_input:
                    frame.evaluate(f'document.querySelector("select#TimeFrom_c1_ctl04").value = "{START_TIME}"')
                    print(f"Selected start time to be {START_TIME}")
                else:
                    print("Select element for start time not found")

                # ----- SELECT END TIME -----
                select_end_time_input = frame.query_selector_all("select#TimeTo_c1_ctl04")
                if select_end_time_input:
                    frame.evaluate(f'document.querySelector("select#TimeTo_c1_ctl04").value = "{END_TIME}"')
                    print(f"Selected end time to be {END_TIME}")
                else:
                    print("Select element for end time not found")

                frame.wait_for_timeout(3000)

                # ----- SELECT BUILDINGS -----
                if BUILDING_ARRAY:
                    if frame.is_visible('#DropMultiBuildingList_c1_textItem'):
                        frame.click('#DropMultiBuildingList_c1_textItem')  # Opens the dropdown list
                        for building_name in BUILDING_ARRAY:
                            frame.click(f'text="{building_name}"')
                            print(f"Selecting {building_name}...")
                        frame.evaluate("popup.hide()")  # Closes the dropdown list
                        page.wait_for_load_state('networkidle')
                        frame.wait_for_timeout(3000)

                # ----- SELECT FLOORS -----
                if FLOOR_ARRAY:
                    if frame.is_visible('#DropMultiFloorList_c1_textItem'):
                        frame.click('#DropMultiFloorList_c1_textItem')  # Opens the dropdown list
                        for floor_name in FLOOR_ARRAY:
                            frame.click(f'text="{floor_name}"')
                            print(f"Selecting {floor_name}...")
                        frame.evaluate("popup.hide()")  # Closes the dropdown list
                        page.wait_for_load_state('networkidle')
                        frame.wait_for_timeout(3000)

                # ----- SELECT FACILITY TYPE -----
                if FACILITY_TYPE_ARRAY:
                    if frame.is_visible('#DropMultiFacilityTypeList_c1_textItem'):
                        frame.click('#DropMultiFacilityTypeList_c1_textItem')  # Opens the dropdown list
                        for facility_type_name in FACILITY_TYPE_ARRAY:
                            frame.click(f'text="{facility_type_name}"')
                            print(f"Selecting {facility_type_name}...")
                        frame.evaluate("popup.hide()")  # Closes the dropdown list
                        page.wait_for_load_state('networkidle')
                        frame.wait_for_timeout(3000)

                # ----- SELECT ROOM CAPACITY -----
                select_capacity_input = frame.query_selector("select#DropCapacity_c1")
                if select_capacity_input:
                    frame.evaluate(f'document.querySelector("select#DropCapacity_c1").value = "{ROOM_CAPACITY_FORMATTED}"')
                    print(f"Selected room capacity to be {ROOM_CAPACITY_FORMATTED}")
                else:
                    print("Select element for room capacity not found")

                frame.wait_for_timeout(3000)

                # ----- SELECT EQUIPMENT -----
                if EQUIPMENT_ARRAY:
                    if frame.is_visible('#DropMultiEquipmentList_c1_textItem'):
                        frame.click('#DropMultiEquipmentList_c1_textItem')  # Opens the dropdown list
                        for equipment_name in EQUIPMENT_ARRAY:
                            frame.click(f'text="{equipment_name}"')
                            print(f"Selecting {equipment_name}...")
                        frame.evaluate("popup.hide()")  # Closes the dropdown list
                        page.wait_for_load_state('networkidle')
                        frame.wait_for_timeout(3000)

                page.screenshot(path=os.path.join(SCREENSHOT_FILEPATH, "0.png"))

                # ----- ROOM EXTRACTION -----
                frame.wait_for_selector("table#GridResults_gv")
                matching_rooms = []
                rows = frame.query_selector_all("table#GridResults_gv tbody tr")
                for row in rows:
                    tds = row.query_selector_all("td")
                    if len(tds) > 1: 
                        matching_rooms.append(tds[1].inner_text().strip())  
                if not matching_rooms:
                    print("No rooms fitting description found.")
                    print("Closing browser...")
                    browser.close() 

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

                    write_json(final_booking_log, os.path.join(BOOKING_LOG_FILEPATH, "scraped_log.json"))

                    return errors

                else:
                    print(f"{len(matching_rooms)} rooms fitting description found:")
                    for room in matching_rooms:
                        print(f"- {room}")

                    # ----- SEARCH AVAILABILITY -----
                    frame.query_selector("a#CheckAvailability").click()
                    print("Submitting search availability request...")
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(6000)

                    # ---------- VIEW TIMESLOTS ----------

                        # ----- CAPTURE SCREENSHOT OF TIMESLOTS -----
                    page.screenshot(path=os.path.join(SCREENSHOT_FILEPATH, "1.png"))

                        # ----- SCRAPE TIMESLOTS -----
                    frame = page.frame(name="frameBottom")
                    frame = page.frame(name="frameContent")
                    room_names_array_raw = [room.inner_text() for room in frame.query_selector_all("div.scheduler_bluewhite_rowheader_inner")]
                    room_names_array_sanitised = [el for el in room_names_array_raw if el not in VALID_BUILDING]
                    bookings_array_raw = [active_bookings.get_attribute("title") for active_bookings in frame.query_selector_all("div.scheduler_bluewhite_event.scheduler_bluewhite_event_line0")]
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

                        room_timeslot_map[room_names_array_sanitised[index]] = fill_missing_timeslots(booking_details)

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
                    
                    write_json(final_booking_log, os.path.join(BOOKING_LOG_FILEPATH, "scraped_log.json"))

        except Exception as e:
            errors.append(f"Error processing {config['target_url']}: {e}")

        finally:
            print("Closing browser...")
            browser.close() 

    except Exception as e:
        errors.append(f"Failed to initialize Playwright: {e}")

    return errors

# ----- SAMPLE EXECUTION CODE -----

if __name__ == "__main__":
    print(f"errors: {scrape_smu_fbs(config)}")
