# Sagasu 4 - Deployment Guide

Complete guide for deploying Sagasu 4 frontend and configuring GitHub Actions for automated scraping.

## Table of Contents

1. [Backend Scraping Setup](#backend-scraping-setup)
2. [Frontend Deployment](#frontend-deployment)
3. [GitHub Secrets Configuration](#github-secrets-configuration)
4. [Vercel Deployment](#vercel-deployment)
5. [Testing](#testing)
6. [Troubleshooting](#troubleshooting)

---

## Backend Scraping Setup

### GitHub Actions Configuration

The scraper runs daily at 8:00 AM SGT via GitHub Actions.

#### 1. Ensure `.github/workflows/scrape.yml` exists

This file is already created in your repository. It handles:
- Daily scheduled scraping (8 AM SGT)
- Manual trigger option
- Automated commit and push of updated data

#### 2. Configure GitHub Secrets

Go to your repository: `Settings` → `Secrets and variables` → `Actions` → `New repository secret`

Add the following secrets:

**Required (for scraper authentication):**
```
SMU_EMAIL=your.email@smu.edu.sg
SMU_PASSWORD=your_password
```

**Optional (scraper configuration - can be adjusted in frontend UI):**
```
SCRAPE_DATE=20-Nov-2025 (or whatever date format you use)
SCRAPE_START_TIME=08:00
SCRAPE_END_TIME=22:00
SCRAPE_ROOM_CAPACITY=From6To10Pax
SCRAPE_BUILDING_NAMES=Yong Pung How School of Law/Kwa Geok Choo Law Library
SCRAPE_FLOOR_NAMES=Level 4,Level 5
SCRAPE_FACILITY_TYPES=Project Room
SCRAPE_EQUIPMENT=TV Panel
```

**Notes:**
- `SMU_EMAIL` and `SMU_PASSWORD` are **required** for the scraper to authenticate
- Without these credentials, the frontend will display an overlay indicating env variables are not configured
- Use comma-separated values for multi-value fields
- Date format: `DD-MMM-YYYY` (e.g., `20-Nov-2025`)
- Time format: `HH:MM` (24-hour)

#### 3. Enable Actions

1. Go to `Actions` tab in your repository
2. Enable GitHub Actions if not already enabled
3. The workflow will run automatically at 8 AM SGT daily
4. You can also trigger manually: `Actions` → `Daily Room Scraper` → `Run workflow`

#### 4. Grant Write Permissions

1. Go to `Settings` → `Actions` → `General`
2. Scroll to "Workflow permissions"
3. Select "Read and write permissions"
4. Check "Allow GitHub Actions to create and approve pull requests"
5. Save

---

## Frontend Deployment

### Option 1: Vercel (Recommended)

#### Quick Deploy

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https://github.com/gongahkia/sagasu-4)

#### Manual Deployment

1. **Push to GitHub**
   ```bash
   git add .
   git commit -m "feat: add React frontend"
   git push origin main
   ```

2. **Import to Vercel**
   - Go to [vercel.com](https://vercel.com)
   - Click "New Project"
   - Import your `sagasu-4` repository
   - Configure project:
     ```
     Framework Preset: Vite
     Root Directory: frontend-react
     Build Command: npm run build
     Output Directory: dist
     Install Command: npm install
     ```

3. **Deploy**
   - Click "Deploy"
   - Wait for build to complete
   - Your app will be live at `https://your-project.vercel.app`

4. **Custom Domain (Optional)**
   - Go to project `Settings` → `Domains`
   - Add your custom domain
   - Follow DNS configuration instructions

#### Environment Variables (Frontend)

**For Production (Vercel):**

No frontend environment variables are required for production deployment. The app will:
- Fetch scraped data directly from GitHub raw URLs
- Display an overlay if GitHub Secrets (`SMU_EMAIL` and `SMU_PASSWORD`) are not configured
- Show a link to configure secrets at: `https://github.com/gongahkia/sagasu-4/settings/secrets/actions`

**For Local Development:**

If you want to test the environment variable detection locally:

1. Create a `.env.local` file in the `frontend-react` directory:
   ```bash
   cd frontend-react
   cp .env.example .env.local
   ```

2. Edit `.env.local` with your credentials:
   ```env
   VITE_SMU_EMAIL=your.email@smu.edu.sg
   VITE_SMU_PASSWORD=your_password
   ```

3. The frontend will check for these variables and:
   - Show an overlay if they're missing
   - Display "Env configured" badge if present
   - Link to the local `.env.local` file location

**Note:** `.env.local` is gitignored and won't be committed to the repository.

---

### Option 2: Netlify

1. **Push to GitHub**

2. **Create New Site**
   - Go to [netlify.com](https://netlify.com)
   - Click "Add new site" → "Import an existing project"
   - Connect to GitHub and select `sagasu-4`

3. **Build Settings**
   ```
   Base directory: frontend-react
   Build command: npm run build
   Publish directory: frontend-react/dist
   ```

4. **Deploy**

---

### Option 3: GitHub Pages

1. **Install gh-pages**
   ```bash
   cd frontend-react
   npm install -D gh-pages
   ```

2. **Add Scripts to package.json**
   ```json
   {
     "scripts": {
       "predeploy": "npm run build",
       "deploy": "gh-pages -d dist"
     }
   }
   ```

3. **Deploy**
   ```bash
   npm run deploy
   ```

4. **Enable GitHub Pages**
   - Go to `Settings` → `Pages`
   - Source: Deploy from a branch
   - Branch: `gh-pages` / `root`
   - Save

---

## Testing

### Test Backend Scraper Locally

```bash
cd backend

# Install dependencies
npm install

# Set up environment variables
cp .env.example .env
# Edit .env with your credentials

# Run scraper
node scraper-prod.js

# Check output
cat log/scraped_log.json
```

### Test Frontend Locally

```bash
cd frontend-react

# Install dependencies
npm install

# Start dev server
npm run dev

# Open http://localhost:5173
```

### Test Production Build

```bash
cd frontend-react

# Build
npm run build

# Preview
npm run preview

# Open http://localhost:4173
```

---

## GitHub Secrets Configuration

Required secrets for GitHub Actions:

| Secret Name | Example Value | Description |
|-------------|---------------|-------------|
| `SMU_EMAIL` | `student@smu.edu.sg` | Your SMU email |
| `SMU_PASSWORD` | `your_password` | Your SMU password |
| `SCRAPE_DATE` | `20-Nov-2025` | Date to scrape (DD-MMM-YYYY) |
| `SCRAPE_START_TIME` | `08:00` | Start time (HH:MM) |
| `SCRAPE_END_TIME` | `22:00` | End time (HH:MM) |
| `SCRAPE_ROOM_CAPACITY` | `From6To10Pax` | Room capacity filter |
| `SCRAPE_BUILDING_NAMES` | `Yong Pung How School of Law` | Comma-separated buildings |
| `SCRAPE_FLOOR_NAMES` | `Level 4,Level 5` | Comma-separated floors |
| `SCRAPE_FACILITY_TYPES` | `Project Room` | Comma-separated facility types |
| `SCRAPE_EQUIPMENT` | `TV Panel` | Comma-separated equipment |

---

## Vercel Configuration

The `vercel.json` file is already configured:

```json
{
  "buildCommand": "npm run build",
  "outputDirectory": "dist",
  "framework": "vite",
  "rewrites": [
    {
      "source": "/(.*)",
      "destination": "/index.html"
    }
  ]
}
```

This handles:
- Build command
- Output directory
- Client-side routing (SPA)

---

## Monitoring

### Check Scraper Status

1. Go to `Actions` tab
2. Click on latest "Daily Room Scraper" run
3. View logs for any errors
4. Check if `scraped_log.json` was updated

### Check Frontend Status

1. Visit your deployed URL
2. Check if data loads
3. Verify last updated timestamp
4. Test filters and search

---

## Troubleshooting

### Scraper Issues

**Problem: Scraper fails with "Login failed"**
- Check GitHub Secrets are set correctly
- Verify SMU credentials are valid
- Check if SMU's FBS is accessible

**Problem: No data in JSON file**
- Check scrape configuration (dates, times, filters)
- Verify at least one room matches your filters
- Check workflow logs for errors

**Problem: Workflow doesn't run**
- Verify Actions are enabled
- Check workflow file syntax
- Ensure write permissions are granted

### Frontend Issues

**Problem: Build fails**
- Check Node.js version (20+)
- Clear `node_modules` and reinstall
- Check for syntax errors in components

**Problem: Data doesn't load**
- Verify `scraped_log.json` exists in main branch
- Check browser console for fetch errors
- Ensure GitHub repo is public or Vercel has access

**Problem: Dark mode doesn't work**
- Check localStorage in browser DevTools
- Clear browser cache
- Verify theme toggle button works

---

## Next Steps

1. ✅ Deploy frontend to Vercel
2. ✅ Configure GitHub Secrets
3. ✅ Test scraper manually
4. ✅ Wait for first scheduled run (8 AM SGT)
5. ✅ Verify frontend shows live data
6. 🚀 Share with SMU students!

---

## Support

- **Issues**: https://github.com/gongahkia/sagasu-4/issues
- **Discussions**: https://github.com/gongahkia/sagasu-4/discussions

---

## Security Notes

- Never commit `.env` files
- Keep GitHub Secrets secure
- Regularly rotate SMU passwords
- Monitor workflow runs for suspicious activity
- Use Dependabot for dependency updates

---

## Performance

- Frontend bundle: ~220KB (~67KB gzipped)
- JSON data: ~20-50KB per scrape
- Load time: <2s on 4G
- Mobile-optimized
- Fully cached by Vercel CDN

---

## License

MIT © gongahkia
