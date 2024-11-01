from typing import List, Optional
from fastapi import FastAPI, HTTPException
from scraper import scrape_smu_fbs
from pydantic import BaseModel
import asyncio
import yaml
from playwright.async_api import async_playwright

app = FastAPI()

def load_constants(config_path='constants.yaml'):
    try:
        with open(config_path, 'r') as file:
            constants = yaml.safe_load(file)
        return constants
    except FileNotFoundError:
        print(f"Configuration file {config_path} not found.")
        exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file: {e}")
        exit(1)

constants = load_constants()

class Credentials(BaseModel):
    username: str
    password: str

class ScrapeRequest(BaseModel):
    credentials: Credentials
    date_raw: str
    duration_hours: float
    start_time: str
    building_names: Optional[List[str]] = []
    floors: Optional[List[str]] = []
    facility_types: Optional[List[str]] = []
    equipment: Optional[List[str]] = []


@app.post("/scrape")
async def scrape_endpoint(request: ScrapeRequest):
    try:
        # Pass both user inputs and server-side constants to the scraping function
        data = await scrape_smu_fbs(request, constants)
        if data:
            return {"status": "success", "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))