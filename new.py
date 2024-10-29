"""
Bharath's reference code
"""

import json
import os
import re
from playwright.sync_api import sync_playwright

def login_smu_fbs_booking(base_url):
    """
    Logs into FBS
    """

    errors = []

    # Start Playwright and manage the lifecycle manually
    try:
        p = sync_playwright().start()  # Initialize Playwright
        browser = p.chromium.launch(headless=True)  # Launch Chromium browser
        context = browser.new_context(
        viewport={"width": 3000, "height": 1800},)  # You can increase/decrease width/height to simulate zoom

    
        
        page = context.new_page()

        try:
            # Navigate to the base URL
            page.goto(base_url)

            # Wait for the username input to appear
            page.wait_for_selector('#userNameInput')
            
            # Fill in the username and password fields
            page.fill('#userNameInput', 'USERNAMEEE')
            page.fill('#passwordInput', 'PASWORDDDD')

            # Click the submit button
            page.click('#submitButton')

            page.wait_for_timeout(6000)  # Waits for 6 seconds


            if "MAKE A NEW BOOKING" in page.text_content('body'): 
                page.click('text="MAKE A NEW BOOKING"')
            
            
            page.screenshot(path="example1.png")

            # Wait for the page and iframe to load
            page.wait_for_load_state('networkidle')

            # Wait for the iframe to be available
            frame = page.frame(name="frameBottom")  # Access the iframe by name or id

            
            if frame is None:
                errors.append("Could not find frameBottom.")
            else:
                frame = page.frame(name="frameContent")
                # Wait for the dropdown to appear inside the iframe
                frame.wait_for_selector('#DropMultiBuildingList_c1_textItem', timeout=60000)

                # Check if the element is visible
                if frame.is_visible('#DropMultiBuildingList_c1_textItem'):
                    # Click on the input field to activate the dropdown
                    frame.click('#DropMultiBuildingList_c1_textItem')

                    # Select the "KGC" option by visible text
                    frame.click('text="Yong Pung How School of Law/Kwa Geok Choo Law Library"')
                    frame.evaluate("popup.hide()")

                    page.wait_for_load_state('networkidle')
                    page.wait_for_timeout(6000)
                    page.screenshot(path="example2.png")

                
                
                if frame.is_visible('#DropMultiFacilityTypeList_c1_textItem'):
                    print("Hello world")
                    # Click on the input field to activate the dropdown
                    frame.click('#DropMultiFacilityTypeList_c1_textItem')
                    page.wait_for_load_state('networkidle')

                    
                    # Check the "Project Room" checkbox
                    page.screenshot(path="example3.png")
                    frame.check('label:has-text("Project Room") input[type="checkbox"]')
                    page.wait_for_load_state('networkidle')

                    frame.evaluate("popup.hide()")
                    page.screenshot(path="example4.png")

                    page.wait_for_timeout(6000)
                    page.screenshot(path="example5.png")

                    # This is used to select the PR that you want to book in KGC. The numbers correspond to the PR numbers. 
                    # Currently it is set to book for Pr4.02

                    # frame.query_selector('#GridResults_gv_ctl02_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl03_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl04_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl05_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl06_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl07_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl08_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl09_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl10_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl11_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl12_checkMultiple').click()
                    frame.query_selector('#GridResults_gv_ctl13_checkMultiple').click()
                    
                    page.wait_for_timeout(3000)



                    frame.wait_for_selector('#CheckAvailability')

                    # Click the "Check Availability" button
                    frame.click('#CheckAvailability')
                    
                    
                    

                    # page.wait_for_load_state('networkidle')

                    # frame.click('#CheckAvailability')

                    # page.screenshot(path="example2.png")

                    # page.wait_for_timeout(10000)  # Waits for 6 seconds

                    # page.screenshot(path="example3.png")
                    
                    
                    page.wait_for_timeout(6000)

                    
                    #######################
                    #PAGE: CHOOSE TIMESLOT#
                    #######################

                    frame = page.frame(name="frameBottom")
                    frame = page.frame(name="frameContent")

                    page.screenshot(path="example6.png")


                    # Query all elements that match the specific selector
                    elements = frame.query_selector_all('div.scheduler_bluewhite_cell')  # Replace the CSS selector with your desired selector

                    # Iterate through the elements and print their information
                    for index, element in enumerate(elements):
                        bounding_box = element.bounding_box()
                        print(f"Element {index}: Bounding box: {bounding_box}")



                    start_element  = elements[73]
                    end_element = elements[75]

                    print(start_element)
                    print(end_element)
                    print(start_element.bounding_box())
                    print(end_element.bounding_box())
                    



                    # Get the bounding box of the start and end elements
                    start_box = start_element.bounding_box()
                    end_box = end_element.bounding_box()

                    # Perform the drag action by simulating mouse actions in the frame
                    # Move to the start element and press the mouse button down
                    page.mouse.move(start_box['x'] + start_box['width'] / 2, start_box['y'] + start_box['height'] / 2)
                    page.mouse.down()



                    # Drag to the end element by moving the mouse
                    page.mouse.move(end_box['x'] + end_box['width'] / 2, end_box['y'] + end_box['height'] / 2, steps=5)

                    # Release the mouse button (completes the drag operation)


                    page.screenshot(path="example7.png", full_page=True)

                    

                    
                    
                    page.mouse.up()

                    page.screenshot(path="example8.png", full_page=True)


                    page.wait_for_timeout(6000)
                    
                    frame.click("#btnMakeBooking")
                    page.wait_for_timeout(6000)
                    

                    frame = page.frame(name="frameBookingDetails")


                    frame.fill('input[name="bookingFormControl1$TextboxPurpose_c1"]', 'STDY')


                    frame.click('#bookingFormControl1_GridCoBookers_ctl14')
                    page.wait_for_timeout(3000)

                    #Replace with the first part of the approver's email
                    frame.fill('.textbox.watermark', 'gabriel.ong.2023')
                    frame.click('id=bookingFormControl1_DialogSearchCoBooker_searchPanel_buttonSearch')
                    
                    page.screenshot(path="example9.png", full_page=True)


                    page.wait_for_timeout(3000)

                    frame.check('id=bookingFormControl1_DialogSearchCoBooker_searchPanel_gridView_gv_ctl02_checkMultiple')
                    frame.click('id=bookingFormControl1_DialogSearchCoBooker_dialogBox_b1')
                    
                    
                    

                    page.screenshot(path="example10.png", full_page=True)

                    page.wait_for_timeout(3000)

                    frame.check('id=bookingFormControl1_TermsAndConditionsCheckbox_c1')

                    frame.click('id=panel_UIButton2')

                    page.screenshot(path="example11.png", full_page=True)

                    page.wait_for_timeout(3000)

                    page.screenshot(path="example12.png", full_page=True)

    
        except Exception as e:
            errors.append(f"Error processing {base_url}: {e}")

        finally:
            browser.close()  # Ensure the browser is closed after use

    except Exception as e:
        errors.append(f"Failed to initialize Playwright: {e}")

    finally:
        p.stop()  # Ensure Playwright is stopped after use
    
    return errors


def login_smu_fbs_accepting(base_url):
    """
    Logs into FBS
    """

    errors = []

    # Start Playwright and manage the lifecycle manually
    try:
        p = sync_playwright().start()  # Initialize Playwright
        browser = p.chromium.launch(headless=True)  # Launch Chromium browser
        context = browser.new_context(
        viewport={"width": 3000, "height": 1800},)  # You can increase/decrease width/height to simulate zoom

    
        
        page = context.new_page()

        try:
            # Navigate to the base URL
            page.goto(base_url)

            # Wait for the username input to appear
            page.wait_for_selector('#userNameInput')
            
            # Fill in the username and password fields
            page.fill('#userNameInput', 'USERNAMEEEE')
            page.fill('#passwordInput', 'PASSWORDDDD')

            # Click the submit button
            page.click('#submitButton')

            page.wait_for_timeout(6000)  # Waits for 6 seconds

            page.screenshot(path="example1.png")

            
            
            
            # Wait for the page and iframe to load
            page.wait_for_load_state('networkidle')
            page.screenshot(path="example2.png")

            frame = page.frame(name="frameBottom")

            frame.click('id=TaskListTab')
            
            page.wait_for_timeout(20000)


            frame = page.frame(name="frameContent")
            
            page.screenshot(path="example3.png")

            # Check the checkbox using its 'id'
            
            frame.check('id=GridViewTask_gv_ctl02_checkMultiple')
            
            page.screenshot(path="example4.png")

            # Wait for the 'Approve Selected' button to be available
            

            # Click the 'Approve Selected' button
            frame.click('id=GridViewTask_ctl13')

            page.screenshot(path="example5.png")

            page.wait_for_timeout(6000)

            page.screenshot(path="example6.png")

            page.wait_for_timeout(6000)

            frame.click('id=popupDialog_b1')

            page.screenshot(path="example7.png")





            
            
    
        except Exception as e:
            errors.append(f"Error processing {base_url}: {e}")

        finally:
            browser.close()  # Ensure the browser is closed after use

    except Exception as e:
        errors.append(f"Failed to initialize Playwright: {e}")

    finally:
        p.stop()  # Ensure Playwright is stopped after use
    
    return errors





# ----- Execution Code -----

# TARGET_URL = "https://www.smu.edu.sg/campus-life/visiting-smu/food-beverages-listing"
# TARGET_FILEPATH = "./../output/smu_dining_details.json"

# details_list, errors = scrape_smu(TARGET_URL)

# if errors:
#     print(f"Errors encountered: {errors}")
# print("Scraping complete.")
# delete_file(TARGET_FILEPATH)

# with open(TARGET_FILEPATH, 'w') as f:
#     json.dump(details_list, f, indent=4)


errors = login_smu_fbs_accepting("https://fbs.intranet.smu.edu.sg/")
if errors:
    print(f"Errors encountered: {errors}")

