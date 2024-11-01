import os
import re
import json
import time
import itertools
from dotenv import load_dotenv
from dateutil.parser import parse
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

def generate_30_min_intervals():
    """
    generate all possible 30 minute
    intervals from 00:00 to 23:59
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
    # print(f"EXISTING INTERVALS: {[el['timeslot'] for el in room_schedule]}")
    target_timeslot_array = []
    new_schedule = []
    timeline_overview = remove_duplicates_preserve_order(list(itertools.chain.from_iterable([slot["timeslot"].split("-") for slot in room_schedule])))
    for i in range(len(timeline_overview)-1):
        start = timeline_overview[i]
        end = timeline_overview[i+1]
        target_timeslot = f"{start}-{end}"
        target_timeslot_array.append(target_timeslot)
    # print(f"GENERATED INTERVAL ARRAY: {target_timeslot_array}")
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
            new_schedule.append(slot) # can afford to be coded this way due to the nature of sorted arrays
            del target_timeslot_array[0] # can afford to be coded this way due to the nature of sorted arrays
    return new_schedule

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

def read_credentials():
    """
    read credentials from a .env file
    """
    load_dotenv()  
    username = os.getenv("USERNAME")
    password = os.getenv("PASSWORD")
    if username and password:
        return {
            "username": username,
            "password": password
        }
    else:
        print("One or more credentials are missing in the .env file")

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

def scrape_smu_fbs(base_url, credentials_filepath):

    """
    handle automated login to SMU FBS based on
    personal credentials.json and scrapes all booked
    timeslots for the filtered rooms
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
    local_credentials = read_credentials(credentials_filepath)
    # print(local_credentials["username"])
    # print(local_credentials["password"])

    try:

        p = sync_playwright().start() 
        browser = p.chromium.launch(headless=False, slow_mo=1000) # for easier debugging
        # browser = p.chromium.launch(headless=True) 
        page = browser.new_page()

        try:

            # ---------- LOGIN CREDENTIALS ----------

            page.goto(base_url)

            page.wait_for_selector('input#userNameInput')
            page.wait_for_selector('input#passwordInput')
            page.wait_for_selector('span#submitButton')

            print(f"navigating to {base_url}")

            username_input = page.query_selector("input#userNameInput") # for debugging
            password_input = page.query_selector("input#passwordInput") # for debugging
            signin_button = page.query_selector("span#submitButton")

            page.fill("input#userNameInput", local_credentials["username"])
            page.fill("input#passwordInput", local_credentials["password"])
            signin_button.click()

            page.wait_for_timeout(6000)
            page.wait_for_load_state('networkidle')

            # ---------- NAVIGATE TO GIVEN DATE ----------

            # page.screenshot(path=f"{SCREENSHOT_FILEPATH}0.png")

            frame = page.frame(name="frameBottom") 
            if not frame:
                errors.append("Framebottom could not be found.")
            else:
                frame = page.frame(name="frameContent")
                while True:
                    current_date_value = frame.query_selector("input#DateBookingFrom_c1_textDate").get_attribute("value")
                    if current_date_value == DATE_FORMATTED:
                        print(f"final day is {current_date_value}")
                        break
                    else:
                        print(f"current day is {current_date_value}")
                        print("navigating to the next day...")
                        frame.query_selector("a#BtnDpcNext.btn").click() # click the button until desired date, which by default is the next day
                        frame.wait_for_timeout(1500)

            # ---------- EXTRACT PAGE DATA ----------

                # ----- SELECT START TIME -----

                select_start_time_input = frame.query_selector("select#TimeFrom_c1_ctl04") # options tags can then be selected by value, values range from 00:00 to 23:30
                if select_start_time_input:
                    frame.evaluate(f'document.querySelector("select#TimeFrom_c1_ctl04").value = "{START_TIME}"')
                    print(f"Selected start time to be {START_TIME}")
                else:
                    print("Select element for start time not found")

                # ----- SELECT END TIME -----

                select_end_time_input = frame.query_selector_all("select#TimeTo_c1_ctl04") # options tags can then be selected by value, values range from 00:00 to 23:30
                if select_end_time_input:
                    frame.evaluate(f'document.querySelector("select#TimeTo_c1_ctl04").value = "{END_TIME}"')
                    print(f"Selected end time to be {END_TIME}")
                else:
                    print("Select element for end time not found")

                # page.screenshot(path=f"{SCREENSHOT_FILEPATH}1.png")
                frame.wait_for_timeout(3000)

                # ----- SELECT BUILDINGS -----

                if BUILDING_ARRAY:

                    # select_building_option_array = page.query_selector_all("div#DropMultiBuildingList_c1\:\:ddl\:\: label") # then read the inner_text fo the span and if the text 
                    # for building in select_building_option_array:
                    #     if building.inner_text in BUILDING_ARRAY: 
                    #         building.query_selector("input").click() # click the checkbox
                    # page.click('div#DropMultiBuildingList_c1_panelTreeView input[type="button"][value="OK"]') # click the OK button
                    # print(f"{len(BUILDING_ARRAY)} buildings selected")

                    if frame.is_visible('#DropMultiBuildingList_c1_textItem'):
                        frame.click('#DropMultiBuildingList_c1_textItem') # opens the dropdown list
                        for building_name in BUILDING_ARRAY:
                            frame.click(f'text="{building_name}"')
                            print(f"selecting {building_name}...")
                        frame.evaluate("popup.hide()") # closes the dropdown list
                        page.wait_for_load_state('networkidle')
                        # page.screenshot(path=f"{SCREENSHOT_FILEPATH}2.png")
                        frame.wait_for_timeout(3000)

                # ----- SELECT FLOORS -----

                if FLOOR_ARRAY:

                    # select_floor_option_array = page.query_selector_all("div#DropMultiFloorList_c1::ddl:: label")
                    # for floor in select_floor_option_array:
                    #     if floor.inner_text() in FLOOR_ARRAY:
                    #         floor.query_selector("input").click()  # click the checkbox
                    # page.click('div#DropMultiFloorList_c1_panelTreeView input[type="button"][value="OK"]')  # click the OK button
                    # print(f"{len(FLOOR_ARRAY)} floors selected")

                    if frame.is_visible('#DropMultiFloorList_c1_textItem'):
                        frame.click('#DropMultiFloorList_c1_textItem') # opens the dropdown list
                        for floor_name in FLOOR_ARRAY:
                            frame.click(f'text="{floor_name}"')
                            print(f"selecting {floor_name}...")
                        frame.evaluate("popup.hide()") # closes the dropdown list
                        page.wait_for_load_state('networkidle')
                        # page.screenshot(path=f"{SCREENSHOT_FILEPATH}3.png")
                        frame.wait_for_timeout(3000)

                # ----- SELECT FACILITY TYPE -----

                if FACILITY_TYPE_ARRAY:

                    # select_facility_option_array = page.query_selector_all("div#DropMultiFacilityTypeList_c1::ddl:: label")
                    # for facility in select_facility_option_array:
                    #     if facility.inner_text() in FACILITY_TYPE_ARRAY:
                    #         facility.query_selector("input").click()  # click the checkbox
                    # page.click('div#DropMultiFacilityTypeList_c1_panelTreeView input[type="button"][value="OK"]')  # click the OK button
                    # print(f"{len(FACILITY_TYPE_ARRAY)} facilities selected")

                    if frame.is_visible('#DropMultiFacilityTypeList_c1_textItem'):
                        frame.click('#DropMultiFacilityTypeList_c1_textItem') # opens the dropdown list
                        for facility_type_name in FACILITY_TYPE_ARRAY:
                            frame.click(f'text="{facility_type_name}"')
                            print(f"selecting {facility_type_name}...")
                        frame.evaluate("popup.hide()") # closes the dropdown list
                        page.wait_for_load_state('networkidle')
                        # page.screenshot(path=f"{SCREENSHOT_FILEPATH}4.png")
                        frame.wait_for_timeout(3000)

                # ----- SELECT ROOM CAPACITY -----

                select_capacity_input = frame.query_selector("select#DropCapacity_c1") # options tags can then be selected by value, values range from LessThan5Pax, From6To10Pax, From11To15Pax, From16To20Pax, From21To50Pax, From51To100Pax, MoreThan100Pax
                if select_capacity_input:
                    frame.evaluate(f'document.querySelector("select#DropCapacity_c1").value = "{ROOM_CAPACITY_FORMATTED}"')
                    print(f"Selected room capacity to be {ROOM_CAPACITY_FORMATTED}")
                else:
                    print("Select element for room capacity not found")
                # page.screenshot(path=f"{SCREENSHOT_FILEPATH}5.png")
                frame.wait_for_timeout(3000)

                if EQUIPMENT_ARRAY:

                    # select_equipment_option_array = page.query_selector_all("div#DropMultiEquipmentList_c1::ddl:: label")
                    # for equipment in select_equipment_option_array:
                    #     if equipment.inner_text() in EQUIPMENT_ARRAY:
                    #         equipment.query_selector("input").click()  # click the checkbox
                    # page.click('div#DropMultiEquipmentList_c1_panelTreeView input[type="button"][value="OK"]')  # click the OK button
                    # print(f"{len(EQUIPMENT_ARRAY)} equipment selected")

                    if frame.is_visible('#DropMultiEquipmentList_c1_textItem'):
                        frame.click('#DropMultiEquipmentList_c1_textItem') # opens the dropdown list
                        for equipment_name in EQUIPMENT_ARRAY:
                            frame.click(f'text="{equipment_name}"')
                            print(f"selecting {equipment_name}...")
                        frame.evaluate("popup.hide()") # closes the dropdown list
                        page.wait_for_load_state('networkidle')
                        # page.screenshot(path=f"{SCREENSHOT_FILEPATH}6.png")
                        frame.wait_for_timeout(3000)

                page.screenshot(path=f"{SCREENSHOT_FILEPATH}0.png")

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
                    print("closing browser...")
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

                    write_json(final_booking_log, f"{BOOKING_LOG_FILEPATH}scraped_log.json")

                    return errors

                else:
                    print(f"{len(matching_rooms)} rooms fitting description found.")
                    for room in matching_rooms:
                        print(f"-{room}")

                    # ----- SEARCH AVAILABILITY -----

                    frame.query_selector("a#CheckAvailability").click()
                    print("Submitting search availability request...")
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(6000)

                    # ---------- VIEW TIMESLOTS ----------

                        # ----- CAPTURE SCREENSHOT OF TIMESLOTS -----

                    page.screenshot(path=f"{SCREENSHOT_FILEPATH}1.png")

                        # ----- SCRAPE TIMESLOTS -----

                    frame = page.frame(name="frameBottom")
                    frame = page.frame(name="frameContent")
                    room_names_array_raw = [room.inner_text() for room in frame.query_selector_all("div.scheduler_bluewhite_rowheader_inner")]
                    room_names_array_sanitised = [el for el in room_names_array_raw if el not in VALID_BUILDING]
                    bookings_array_raw = [active_bookings.get_attribute("title") for active_bookings in frame.query_selector_all("div.scheduler_bluewhite_event.scheduler_bluewhite_event_line0")]
                    bookings_array_sanitised = split_bookings_by_day(bookings_array_raw)
                    
                    room_timeslot_map = {}

                    # print(room_names_array)
                    # print(bookings_array) 

                    for index, booking_array in enumerate(bookings_array_sanitised):

                        # print(index)
                        # print(room_names_array_sanitised[index])

                        booking_details = []

                        for booking in booking_array:

                            # print(booking)

                            if booking.startswith("Booking Time:"): # existing booking
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
                                # pretty_print_json(active_booking_details)

                                booking_details.append(active_booking_details)

                            elif booking.endswith("(not available)"): # not available booking

                                time = booking.split(") (")[0]
                                na_booking_details = {
                                    "timeslot": time.lstrip("("),
                                    "available": False,
                                    "status": "Not available",
                                    "details": None
                                }
                                # pretty_print_json(na_booking_details)

                                booking_details.append(na_booking_details)

                                # room_timeslots["bookings"].append(na_booking_details)

                            else: 
                                # edge case checking
                                print(f"Unrecognised timeslot format, logged here: {booking}")

                        # print(f"original: {booking_details}")
                        # print(f"filled: {fill_missing_timeslots(booking_details)}")

                        room_timeslot_map[room_names_array_sanitised[index]] = fill_missing_timeslots(booking_details)

                    # pretty_print_json(room_timeslot_map)
                    
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
                    
                    # pretty_print_json(final_booking_log)

                    write_json(final_booking_log, f"{BOOKING_LOG_FILEPATH}scraped_log.json")

        except Exception as e:
            errors.append(f"Error processing {base_url}: {e}")

        finally:
            print("closing browser...")
            browser.close() 

    except Exception as e:
        errors.append(f"Failed to initialize Playwright: {e}")

    return errors

# ----- SAMPLE EXECUTION CODE -----

if __name__ == "__main__":
    TARGET_URL = "https://fbs.intranet.smu.edu.sg/home"
    CREDENTIALS_FILEPATH = "credentials.json"
    print(f"errors: {scrape_smu_fbs(TARGET_URL, CREDENTIALS_FILEPATH)}")