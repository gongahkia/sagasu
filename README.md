[![](https://img.shields.io/badge/sagasu_4.0.0-passing-green)](https://github.com/gongahkia/sagasu-4/releases/tag/1.0.0)
![](https://github.com/gongahkia/sagasu-4/actions/workflows/scrape.yml/badge.svg)

# `Sagasu 4`

<p align="center">
    <img src="./asset/logo/logo-four.png" width=55% height=55%>
</p>

...

## Stack

* *Frontend*: [React](https://react.dev/), [Vite](https://vite.dev/), [Cloudflare Pages](https://pages.cloudflare.com/)
* *Backend*: [Playwright](https://github.com/microsoft/playwright), [Node.js](https://nodejs.org/en), [Github Actions](https://docs.github.com/en/actions), [Cron](https://www.ibm.com/docs/en/db2/11.5.x?topic=task-unix-cron-format)

## Rationale

...

## Architecture

![](./asset/reference/architecture.png)

## Screenshots

...

## Usage

...

## Configuration

### Environment Variables

Sagasu 4 uses environment variables for configuration. You can configure these either locally in a `.env` file (for development) or as GitHub Secrets (for the automated workflow).

#### Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `SMU_EMAIL` | Your SMU email address | `your.email@smu.edu.sg` |
| `SMU_PASSWORD` | Your SMU password | `your_password` |

#### Scraper Configuration

These variables control what rooms are scraped and when:

| Variable | Description | Possible Values | Example |
|----------|-------------|-----------------|---------|
| `SCRAPE_DATE` | Date to check room availability | `TODAY` for current date, or any valid date in `DD-MMM-YYYY` format | `TODAY` or `20-Nov-2025` |
| `SCRAPE_START_TIME` | Start of time window to check | Time in `HH:MM` format (24-hour) | `08:00` |
| `SCRAPE_END_TIME` | End of time window to check | Time in `HH:MM` format (24-hour) | `22:00` |
| `SCRAPE_ROOM_CAPACITY` | Filter rooms by capacity | `From2To5Pax`, `From6To10Pax`, `From11To15Pax`, `From16To20Pax`, `Above20Pax` | `From6To10Pax` |
| `SCRAPE_BUILDING_NAMES` | Buildings to search (comma-separated) | See [Buildings List](#buildings) below | `Yong Pung How School of Law/Kwa Geok Choo Law Library` |
| `SCRAPE_FLOOR_NAMES` | Floors to search (comma-separated) | See [Floors List](#floors) below | `Level 4,Level 5` |
| `SCRAPE_FACILITY_TYPES` | Types of facilities to search (comma-separated) | See [Facility Types](#facility-types) below | `Project Room` |
| `SCRAPE_EQUIPMENT` | Required equipment (comma-separated) | See [Equipment List](#equipment) below | `TV Panel` |

#### Buildings

Available building options (exact names, case-sensitive):

- `School of Accountancy`
- `School of Economics`
- `School of Information Systems`
- `Lee Kong Chian School of Business`
- `Yong Pung How School of Law/Kwa Geok Choo Law Library`
- `School of Social Sciences`
- `Administration Building`
- `Li Ka Shing Library`
- `Sports & Recreation Centre`
- `Campus Centre`

**Note:** Use exact building names as they appear in SMU FBS. Multiple buildings can be specified by comma-separation.

#### Floors

Available floor options (exact names, case-sensitive):

- `Basement 1`
- `Basement 2`
- `Level 1`
- `Level 2`
- `Level 3`
- `Level 4`
- `Level 5`
- `Level 6`
- `Level 7`

**Note:** Not all floors exist in all buildings. The scraper will only return results for valid floor-building combinations.

#### Facility Types

Available facility type options:

- `Project Room`
- `Discussion Room`
- `Seminar Room`
- `Meeting Room`
- `Study Room`
- `Computer Lab`
- `Auditorium`
- `Classroom`

#### Equipment

Available equipment options:

- `TV Panel`
- `Projector`
- `Whiteboard`
- `Video Conference System`
- `Computer`
- `Audio System`

**Note:** Equipment availability varies by room and building. Specifying equipment will filter results to only rooms with that equipment.

### Local Setup (Development)

1. Navigate to the backend directory:
   ```bash
   cd backend
   ```

2. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

3. Edit `.env` with your configuration:
   ```bash
   nano .env  # or use your preferred editor
   ```

4. Run the scrapers:
   ```bash
   # Run all scrapers (rooms, bookings, tasks)
   npm run scrape:all

   # Or run individual scrapers
   npm run scrape           # Rooms only
   npm run scrape:bookings  # Bookings only
   npm run scrape:tasks     # Tasks only
   ```

### GitHub Actions Setup (Production)

1. Go to your repository settings → Secrets and variables → Actions

2. Add the following secrets:
   - `SMU_EMAIL` (required)
   - `SMU_PASSWORD` (required)
   - `SCRAPE_START_TIME` (required)
   - `SCRAPE_END_TIME` (required)
   - `SCRAPE_ROOM_CAPACITY` (required)
   - `SCRAPE_BUILDING_NAMES` (required)
   - `SCRAPE_FLOOR_NAMES` (required)
   - `SCRAPE_FACILITY_TYPES` (required)
   - `SCRAPE_EQUIPMENT` (required)

   **Note:** `SCRAPE_DATE` is not needed as a secret - the workflow automatically uses the current date (TODAY) for daily scheduled runs.

3. The workflow will automatically run daily at 8:00 AM SGT (00:00 UTC) and scrape data for that day

4. You can also trigger it manually from the Actions tab

### Important Notes

- **Security:** Never commit your `.env` file or expose your SMU credentials
- **Date Format:** Must use `DD-MMM-YYYY` format (e.g., `20-Nov-2025`, not `20-11-2025`)
- **Time Format:** Must use 24-hour format `HH:MM` (e.g., `08:00`, `22:00`)
- **Comma-separated values:** No spaces after commas unless the value itself contains spaces
- **Case sensitivity:** Building names, floor names, and facility types are case-sensitive and must match exactly as shown above

## Other notes

`Sagasu 4` is where it is today because of the below projects. 

* [Sagasu](https://github.com/gongahkia/sagasu)
* [Sagasu 2](https://github.com/gongahkia/sagasu-2)
* [Sagasu 3](https://github.com/gongahkia/sagasu-3)
