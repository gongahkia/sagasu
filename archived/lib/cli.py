# --- REQUIRED IMPORTS ---

import os
from dotenv import dotenv_values, set_key
import questionary
from datetime import datetime

# --- CONSTANT DEFINITIONS ---

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
    "23:30",
]
VALID_ROOM_CAPACITY = [
    "LessThan5Pax",
    "From6To10Pax",
    "From11To15Pax",
    "From16To20Pax",
    "From21To50Pax",
    "From51To100Pax",
    "MoreThan100Pax",
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
    "Yong Pung How School of Law/Kwa Geok Choo Law Library",
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
    "Level 14",
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
    "Study Booth",
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
    "Wireless Projection",
]

ENV_FILE = "./backend/.env"

# --- HELPER FUNCTIONS ---

def get_existing_env():
    if os.path.exists(ENV_FILE):
        return dotenv_values(ENV_FILE)
    return {}

def write_env_variable(key, value):
    set_key(ENV_FILE, key, value)

def prompt_date():
    while True:
        answer = questionary.text(
            "Enter booking date (any common format, e.g. 29 Jul 2025, 2025-07-29):"
        ).ask()
        if not answer:
            print("Date is required.")
            continue
        try:
            dt = datetime.strptime(answer, "%d-%b-%Y")
            return answer
        except Exception:
            from dateutil.parser import parse

            try:
                dt = parse(answer)
                formatted = dt.strftime("%d-%b-%Y")
                return formatted
            except Exception:
                print("Invalid date format, please try again.")

def prompt_time(label):
    choice = questionary.select(
        label,
        choices=VALID_TIME,
    ).ask()
    return choice

def prompt_room_capacity():
    return questionary.select(
        "Select room capacity:",
        choices=VALID_ROOM_CAPACITY,
    ).ask()

def prompt_multiple(label, choices):
    return questionary.checkbox(label, choices=choices).ask() or []

def main():
    print("LOG: Loading existing .env data...")
    env_data = get_existing_env()
    email = env_data.get("SMU_EMAIL", "")
    password = env_data.get("SMU_PASSWORD", "")
    if not email or not password:
        print(
            "WARNING: SMU_EMAIL and SMU_PASSWORD not found in your .env file. Please add them before using this script.\nLOG: Closing script for now."
        )
        return
    else:
        print("LOG: SMU_EMAIL and SMU_PASSWORD found in .env file.")
    print(f"LOG: SMU_EMAIL loaded as: {email}")
    print("LOG: (SMU_PASSWORD is hidden for security reasons)")
    scrape_date = prompt_date()
    start_time = prompt_time("Select scrape start time:")
    start_index = VALID_TIME.index(start_time)
    end_time_choices = VALID_TIME[start_index + 1 :] or VALID_TIME
    end_time = questionary.select(
        "Select scrape end time:",
        choices=end_time_choices,
    ).ask()
    room_capacity = prompt_room_capacity()
    building_names = prompt_multiple("Select building(s):", VALID_BUILDING)
    floor_names = prompt_multiple("Select floor(s):", VALID_FLOOR)
    facility_types = prompt_multiple("Select facility type(s):", VALID_FACILITY_TYPE)
    equipment = prompt_multiple("Select equipment(s):", VALID_EQUIPMENT)
    write_env_variable("SMU_EMAIL", email)
    write_env_variable("SMU_PASSWORD", password)
    write_env_variable("SCRAPE_DATE", scrape_date)
    write_env_variable("SCRAPE_START_TIME", start_time)
    write_env_variable("SCRAPE_END_TIME", end_time)
    write_env_variable("SCRAPE_ROOM_CAPACITY", room_capacity)
    write_env_variable("SCRAPE_BUILDING_NAMES", ",".join(building_names))
    write_env_variable("SCRAPE_FLOOR_NAMES", ",".join(floor_names))
    write_env_variable("SCRAPE_FACILITY_TYPES", ",".join(facility_types))
    write_env_variable("SCRAPE_EQUIPMENT", ",".join(equipment))
    print("\nLOG: Updated .env with selected configuration!")

# --- EXECUTION CODE ---

if __name__ == "__main__":
    main()