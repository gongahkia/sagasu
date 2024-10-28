"""
FUA

debug main event loop from line 113 onwards

reference new.py as required

work on helper functions specified with FUA at the 
top of the function

continue working on the below logic from 
the base of the function

allow users to specify configs via json

specify all possible values for each select option tag 
at the top of the function underneath its corresponding
array
"""

import os
import re
import json
import time
from playwright.sync_api import sync_playwright

def read_credentials(credentials_filepath):
    try:
        with open(credentials_filepath, 'r') as file:
            data = json.load(file)
        return data
    except FileNotFoundError:
        print("File not found. Please check the file path.")
    except json.JSONDecodeError:
        print("Error decoding JSON. Please check the file format.")

def scrape_smu_fbs(base_url, credentials_filepath):

    """
    handle automated login to SMU FBS based on
    personal credentials.json and scrapes the desired pages
    """

    # FUA these values below are to be recieved as parameters to the function with optional parameters as well
    DATE = "02-Nov-2024" # FUA to add a function that converts this date input so users can specify date input to the function in any number of formats
    DURATION_HRS = "2"
    START_TIME = "11:00"
    END_TIME = "13:00" # FUA to add a function that calculates this based on the duration_hrs fed in
    ROOM_CAPACITY = 0 # FUA to add a function that converts the int room_capacity to one of the accepted selector values
    BUILDING_ARRAY = ["School of Accountancy", "School of Computing & Information Systems 1"]
    FLOOR_ARRAY = []
    FACILITY_TYPE_ARRAY = []
    EQUIPMENT_ARRAY = []
    SCREENSHOT_FILEPATH = "./screenshot_log/"

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

            # ----- LOGIN CREDENTIALS -----

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

            # ----- NAVIGATE TO GIVEN DATE -----

            page.screenshot(path=f"{SCREENSHOT_FILEPATH}0.png")

            frame = page.frame(name="frameBottom") 
            if not frame:
                errors.append("Framebottom could not be found.")
            else:
                frame = page.frame(name="frameContent")
                current_date_input = frame.query_selector("input#DateBookingFrom_c1_textDate") # might need to get the value attribute from here
                while current_date_input != DATE:
                    current_date_input = frame.query_selector("input#DateBookingFrom_c1_textDate").get_attribute("value") 
                    print(f"current day is {current_date_input}, going to next day...")
                    next_day_button_input = frame.query_selector("a#BtnDpcNext.btn") # click the button until desired date, which by default is the next day
                    next_day_button_input.click()
                    frame.wait_for_timeout(3000)

            # ----- EXTRACT PAGE DATA -----

                # start_time = frame.query_selector("span#TimeFrom_c1").inner_text()
                # end_time = frame.query_selector("span#TimeTo_c1").inner_text()
                # print(f"current start time: {start_time}")
                # print(f"current end time: {end_time}")

                select_start_time_input = frame.query_selector("select#TimeFrom_c1_ctl04") # options tags can then be selected by value, values range from 00:00 to 23:30
                print(select_start_time_input)
                if select_start_time_input:
                    page.select('select#TimeFrom_c1_ctl04', START_TIME)
                    selected_value = select_start_time_input.get_attribute("value")
                    print(f"Selected start time to be {selected_value}")
                else:
                    print("Select element for start time not found")

                select_end_time_input = frame.query_selector("select#TimeTo_c1_ctl04") # options tags can then be selected by value, values range from 00:00 to 23:30
                print(select_end_time_input)
                if select_end_time_input:
                    page.select('select#TimeTo_c1_ctl04', END_TIME)  
                    selected_value = select_end_time_input.get_attribute("value")
                    print(f"Selected end time to be {selected_value}")
                else:
                    print("Select element for end time not found")

                start_time = frame.query_selector("span#TimeFrom_c1").inner_text()
                end_time = frame.query_selector("span#TimeTo_c1").inner_text()
                print(f"new start time: {start_time}")
                print(f"new end time: {end_time}")

            if BUILDING_ARRAY:

                select_building_input = page.query_selector("input#DropMultiBuildingList_c1_textItem") # FUA is this necessary then since the bototm line of code already does the same thing
                select_building_option_array = page.query_selector_all("div#DropMultiBuildingList_c1::ddl:: label") # then read the inner_text fo the span and if the text 
                for building in select_building_option_array:
                    if building.inner_text in BUILDING_ARRAY: 
                        building.query_selector("input").click() # click the checkbox
                page.click('div#DropMultiBuildingList_c1_panelTreeView input[type="button"][value="OK"]') # click the OK button
                print(f"{len(BUILDING_ARRAY)} buildings selected")

            if FLOOR_ARRAY:

                # FUA is this necessary since the bottom line of code already achieves the same thing 
                    # input#DropMultiFloorList_c1_textItem

                select_floor_option_array = page.query_selector_all("div#DropMultiFloorList_c1::ddl:: label")
                for floor in select_floor_option_array:
                    if floor.inner_text() in FLOOR_ARRAY:
                        floor.query_selector("input").click()  # click the checkbox
                page.click('div#DropMultiFloorList_c1_panelTreeView input[type="button"][value="OK"]')  # click the OK button
                print(f"{len(FLOOR_ARRAY)} floors selected")

            if FACILITY_TYPE_ARRAY:
                
                # FUA is this necessary since the bottom line of code already achieves the same thing 
                    # input#DropMultiFacilityTypeList_c1_textItem                

                select_facility_option_array = page.query_selector_all("div#DropMultiFacilityTypeList_c1::ddl:: label")
                for facility in select_facility_option_array:
                    if facility.inner_text() in FACILITY_TYPE_ARRAY:
                        facility.query_selector("input").click()  # click the checkbox
                page.click('div#DropMultiFacilityTypeList_c1_panelTreeView input[type="button"][value="OK"]')  # click the OK button
                print(f"{len(FACILITY_TYPE_ARRAY)} facilities selected")

            if ROOM_CAPACITY != 0:

                select_capacity_input = page.query_selector("select#DropCapacity_c1 option") # options tags can then be selected by value, values range from LessThan5Pax, From6To10Pax, From11To15Pax, From16To20Pax, From21To50Pax, From51To100Pax, MoreThan100Pax
                for capacity in select_capacity_input:
                    if capacity.get_attribute("value") == ROOM_CAPACITY:
                        capacity.click()
                
            if EQUIPMENT_ARRAY:

                # FUA is this necessary since the bottom line of code already achieves the same thing 
                    # input#DropMultiEquipmentList_c1_textItem 

                select_equipment_option_array = page.query_selector_all("div#DropMultiEquipmentList_c1::ddl:: label")
                for equipment in select_equipment_option_array:
                    if equipment.inner_text() in EQUIPMENT_ARRAY:
                        equipment.query_selector("input").click()  # click the checkbox
                page.click('div#DropMultiEquipmentList_c1_panelTreeView input[type="button"][value="OK"]')  # click the OK button
                print(f"{len(EQUIPMENT_ARRAY)} equipment selected")

            page.wait_for_timeout(6000)
            page.wait_for_selector("table#GridResults_gv")
            print("rooms loaded in...")

            rows = page.query_selector_all("table#GridResults_gv tbody tr")
            tem = []
            for row in rows:
                tds = row.query_selector_all("td")
                if len(tds) > 1: 
                    tem.append(tds[1].inner_text().strip())  

            print("Rooms fitting description are...")
            for el in tem:
                print(f"-{el}")

            page.query_selector("a#CheckAvailability").click()
            print("submitting search availability request...")

            page.wait_for_load_state("networkidle")

            page.screenshot(path=f"{SCREENSHOT_FILEPATH}1.png")
            print(f"saving screenshot of rooms to filepath {SCREENSHOT_FILEPATH}")

            """
            FUA 
            
            continue adding scraping code here once the available timeslots 
            have been loaded in or make a screenshot or curl the HTML and use 
            a OCR library to extract that data instead, that works as well
            """

        except Exception as e:
            errors.append(f"Error processing {base_url}: {e}")

        finally:
            print("closing browser...")
            browser.close() 

    except Exception as e:
        errors.append(f"Failed to initialize Playwright: {e}")

    return errors

if __name__ == "__main__":
    TARGET_URL = "https://fbs.intranet.smu.edu.sg/home"
    CREDENTIALS_FILEPATH = "credentials.json"
    scrape_smu_fbs(TARGET_URL, CREDENTIALS_FILEPATH)
