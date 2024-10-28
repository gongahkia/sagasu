"""
FUA

continue working on the below logic for the code tonight

add credentials.json

allow users to specify configs via json
"""

import json
import os
import re
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

def login_smu_fbs(base_url, credentials_filepath):

    """
    handle automated login to SMU FBS based on
    personal credentials.json
    """

    errors = []
    local_credentials = read_credentials(credentials_filepath)

    try:

        p = sync_playwright().start() 
        browser = p.chromium.launch(headless=True) 
        page = browser.new_page()

        try:

            page.goto(base_url)
            page.wait_for_selector('input#userNameInput')
            page.wait_for_selector('input#passwordInput')
            page.wait_for_selector('span#submitButton')
            print(f"navigating to {base_url}")

            username_input = page.queryselector("input#userNameInput")
            password_input = page.queryselector("input#passwordInput")
            signin_button = page.queryselector("span#submitButton")

            page.fill(username_input, local_credentials["username"])
            page.fill(password_input, local_credentials["password"])
            page.click(signin_button) 

            # page.wait_for_timeout(6000)

            page.wait_for_selector("div.announcementGreyBar span.white-font-span")

            if "MAKE A NEW BOOKING" in page.text_content('body'): 
                page.click('text="MAKE A NEW BOOKING"')
            page.screenshot(path="example1.png")
            page.wait_for_load_state('networkidle')
            frame = page.frame(name="frameBottom") 
            if frame is None:
                errors.append("Could not find frameBottom.")
            else:
                frame = page.frame(name="frameContent")
                frame.wait_for_selector('#DropMultiBuildingList_c1_textItem', timeout=60000)
                if frame.is_visible('#DropMultiBuildingList_c1_textItem'):
                    frame.click('#DropMultiBuildingList_c1_textItem')
                    frame.click('text="Yong Pung How School of Law/Kwa Geok Choo Law Library"')
                    frame.click('input[type="button"][value="OK"]')
                if frame.is_visible('#DropMultiFacilityTypeList_c1_textItem'):
                    print("Hello world")
                    frame.click('#DropMultiFacilityTypeList_c1_textItem')
                    frame.wait_for_selector('label:has-text("Project Room") input[type="checkbox"]')
                    frame.check('label:has-text("Project Room") input[type="checkbox"]')
                    page.wait_for_load_state('networkidle')
                    frame.click('#CheckAvailability')
                    page.screenshot(path="example2.png")
                    page.wait_for_timeout(10000) 
                    page.screenshot(path="example3.png")
        except Exception as e:
            errors.append(f"Error processing {base_url}: {e}")
        finally:
            browser.close() 
    except Exception as e:
        errors.append(f"Failed to initialize Playwright: {e}")
    finally:
        p.stop() 
    return errors

if __name__ == "__main__":
    TARGET_URL = "https://fbs.intranet.smu.edu.sg/home"
    login_smu_fbs(TARGET_URL)
