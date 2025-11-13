# Local Development Guide

Complete guide for running Sagasu 4 locally for development.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Backend Setup](#backend-setup)
3. [Frontend Setup](#frontend-setup)
4. [Running the Full Stack](#running-the-full-stack)
5. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- Node.js (v18 or higher)
- npm or yarn
- SMU email and password for scraping

---

## Backend Setup

### 1. Install Dependencies

```bash
cd backend
npm install
```

This will install:
- `playwright` - Browser automation for scraping
- `dotenv` - Environment variable management
- `fs` - File system operations

### 2. Install Playwright Browsers

```bash
npx playwright install chromium
```

This downloads the Chromium browser needed for scraping.

### 3. Configure Environment Variables

Create a `.env` file in the `backend` directory:

```bash
cd backend
cp .env.example .env
```

Edit the `.env` file with your credentials and preferences:

```env
# REQUIRED - SMU Login Credentials
SMU_EMAIL=your.email@smu.edu.sg
SMU_PASSWORD=your_password

# OPTIONAL - Scraping Configuration (adjust as needed)
SCRAPE_DATE=20-Nov-2025
SCRAPE_START_TIME=08:00
SCRAPE_END_TIME=22:00
SCRAPE_ROOM_CAPACITY=From6To10Pax
SCRAPE_BUILDING_NAMES=Yong Pung How School of Law/Kwa Geok Choo Law Library
SCRAPE_FLOOR_NAMES=Level 4,Level 5
SCRAPE_FACILITY_TYPES=Project Room
SCRAPE_EQUIPMENT=TV Panel
```

**Important Notes:**
- Replace `your.email@smu.edu.sg` and `your_password` with your actual SMU credentials
- The `.env` file is gitignored and won't be committed to the repository
- Date format: `DD-MMM-YYYY` (e.g., `20-Nov-2025`)
- Time format: `HH:MM` (24-hour format)
- Use comma-separated values for multi-value fields (buildings, floors, etc.)

### 4. Run the Scraper

```bash
npm run scrape
```

The scraper will:
1. Open a browser window (you'll see it running)
2. Navigate to SMU FBS website
3. Authenticate with your credentials
4. Configure filters and scrape room data
5. Save results to `backend/log/scraped_log.json`
6. Close the browser

**Expected Output:**

```
✓ Browser launched
✓ Authenticated with SMU
✓ Configured filters
✓ Scraped 45 rooms
✓ Saved to backend/log/scraped_log.json
```

### 5. Verify Scraped Data

Check that the data was saved correctly:

```bash
ls -la backend/log/
# You should see scraped_log.json and bookings_log.json
```

---

## Frontend Setup

### 1. Install Dependencies

```bash
cd frontend-react
npm install
```

### 2. No Frontend Environment Variables Needed

The frontend doesn't require any environment variables. It simply displays data scraped by the backend.

### 3. Sync Backend Data to Frontend

Before running the frontend, copy the scraped data:

```bash
npm run sync:data
```

This copies `backend/log/*.json` to `frontend-react/public/data/`

**Note:** Run this command whenever you re-scrape data in the backend.

### 4. Run the Development Server

```bash
npm run dev
```

The frontend will:
- Start on `http://localhost:5173`
- Fetch data from `public/data/scraped_log.json`
- Hot-reload when you make changes

---

## Running the Full Stack

### Option 1: Manual (Recommended for Development)

**Terminal 1 - Run Backend Scraper (when needed):**
```bash
cd backend
npm run scrape
```

**Terminal 2 - Sync Data & Run Frontend:**
```bash
cd frontend-react
npm run sync:data  # Copy scraped data to public directory
npm run dev
```

Visit `http://localhost:5173` in your browser.

### Option 2: One-Time Scrape + Frontend

If you don't need to scrape repeatedly:

```bash
# Run scraper once
cd backend
npm run scrape

# Sync data and start frontend (in same or different terminal)
cd ../frontend-react
npm run sync:data
npm run dev
```

---

## Troubleshooting

### "Failed to load data" Error

**Problem:** Frontend can't find scraped data.

**Solution:**
1. Make sure you've run the backend scraper at least once:
   ```bash
   cd backend
   npm run scrape
   ```

2. Verify the data file exists:
   ```bash
   ls backend/log/scraped_log.json
   ```

3. Check that Vite is configured to serve parent directory files:
   - The `vite.config.js` should have `server.fs.allow: ['..']`

### "Missing SMU_EMAIL in .env" Error

**Problem:** Backend `.env` file not configured.

**Solution:**
1. Create `.env` file in `backend` directory:
   ```bash
   cd backend
   cp .env.example .env
   ```

2. Edit with your credentials

### Playwright Browser Download Issues

**Problem:** Chromium browser not installed.

**Solution:**
```bash
cd backend
npx playwright install chromium
```

### SMU Authentication Fails

**Problem:** Scraper can't log in.

**Solutions:**
1. Verify your SMU email and password are correct in `backend/.env`
2. Check if SMU has changed their login flow
3. The scraper runs with a visible browser window - watch for any login prompts
4. Check if you need to approve MFA/2FA manually

### Environment Variable Overlay Won't Disappear

**Problem:** Frontend still shows "env variables not configured" overlay.

**Solution:**
1. **Frontend `.env.local`**: Make sure variables don't start with example values like `your.email@...`
   ```env
   # ❌ Wrong
   VITE_SMU_EMAIL=your.email@smu.edu.sg

   # ✅ Correct
   VITE_SMU_EMAIL=john.doe@smu.edu.sg
   ```

2. **Backend scraper**: Ensure you've run the scraper successfully and data is recent (within 48 hours)

3. **Restart dev server**: Sometimes you need to restart Vite:
   ```bash
   # Press Ctrl+C to stop, then:
   npm run dev
   ```

### Port 5173 Already in Use

**Problem:** Another process is using the default Vite port.

**Solution:**
```bash
# Kill the process using the port
lsof -ti:5173 | xargs kill -9

# Or use a different port
npm run dev -- --port 3000
```

---

## Development Workflow

### Typical Daily Workflow

1. **Morning**: Run scraper to get fresh data
   ```bash
   cd backend && npm run scrape
   ```

2. **Development**: Work on frontend with live reload
   ```bash
   cd frontend-react && npm run dev
   ```

3. **Testing changes**: Frontend automatically reloads on code changes

4. **Re-scrape as needed**: Run scraper again if you need updated room data

### Data Freshness

- The frontend shows an overlay if data is older than 48 hours
- Run the scraper daily (or as needed) to keep data fresh
- In production, GitHub Actions runs the scraper daily at 8 AM SGT

---

## Next Steps

- Read [DEPLOYMENT.md](./DEPLOYMENT.md) for production deployment
- Check [README.md](./README.md) for project overview
- See `.github/workflows/scrape.yml` for automated scraping setup

---

## Notes

- **Backend `.env`**: Contains SMU credentials for scraper authentication
- **Frontend has no env variables**: Frontend just displays scraped data
- **Local data**: Frontend fetches from `public/data/` (synced from backend)
- **Production data**: Frontend fetches from GitHub raw URLs in production
- **Archived scrapers**: Old scraper versions are in `backend/archived/` for reference
