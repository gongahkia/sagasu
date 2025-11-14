//
// --- CONFIGURATION ---
//

const { chromium } = require('playwright');
const fs = require('fs');
require('dotenv').config();

//
// --- HELPER FUNCTIONS ---
//

function requireEnv(key) {
  if (!process.env[key]) throw new Error(`Missing ${key} in .env`);
  return process.env[key];
}

//
// --- CONFIGURATION ---
//

const EMAIL = requireEnv('SMU_EMAIL');
const PASSWORD = requireEnv('SMU_PASSWORD');

const url = "https://www.smubondue.com/facility-booking-system-fbs";
const outputLog = './log/scraped_tasks.json';

//
// --- MAIN SCRIPT ---
//

(async () => {
  const scrapeStartTime = Date.now();
  let browser;

  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();

    // 1. Go to the initial site
    console.log(`LOG: Navigating to ${url}`);
    await page.goto(url, {
      waitUntil: 'domcontentloaded',
      timeout: 60000
    });
    console.log(`LOG: Successfully loaded ${url}`);

    // 2. Open Microsoft login in new tab
    const [newPage] = await Promise.all([
      context.waitForEvent('page', { timeout: 30000 }),
      page.click('a[aria-label="SMU FBS"]'),
    ]);

    // 3. Wait for Microsoft login URL to appear
    await newPage.waitForURL(/login\.microsoftonline\.com/, { timeout: 30000 });
    await newPage.waitForSelector('input[type="email"], #i0116', { timeout: 30000 });
    console.log(`LOG: Navigating to ${newPage.url()}`);

    // 4. Fill email and proceed
    let emailInput = await newPage.$('input[type="email"]') || await newPage.$('#i0116');
    if (!emailInput) throw new Error('ERROR: Email input not found');
    await emailInput.fill(EMAIL);
    let nextButton = await newPage.$('input[type="submit"]') || await newPage.$('button[type="submit"]') || await newPage.$('#idSIButton9');
    if (!nextButton) throw new Error('ERROR: Next button not found');
    await Promise.all([
      nextButton.click(),
      newPage.waitForLoadState('networkidle'),
    ]);
    console.log(`LOG: Filled in email ${EMAIL} and clicked next`);

    // 5. Wait for SMU redirect or click fallback
    try {
      await newPage.waitForURL(/login2\.smu\.edu\.sg/, { timeout: 10000 });
      console.log('LOG: Redirected to SMU SSO');
    } catch (e) {
      const redirectLink = await newPage.$('a#redirectToIdpLink');
      if (redirectLink) {
        console.log('Redirect took too long, clicking #redirectToIdpLink...');
        await Promise.all([
          redirectLink.click(),
        ]);
      } else {
        console.log('Redirect delay detected, but #redirectToIdpLink not found.');
      }
      await newPage.waitForURL(/login2\.smu\.edu\.sg/, { timeout: 30000 });
    }
    console.log(`LOG: Navigated to ${newPage.url()}`);

    // 6. Wait for password input, fill in password
    await newPage.waitForSelector('input#passwordInput', { timeout: 30000 });
    const passwordInput = await newPage.$('input#passwordInput');
    if (!passwordInput) throw new Error('ERROR: Password input not found');
    await passwordInput.fill(PASSWORD);
    console.log(`LOG: Filled in password`);

    // 7. Find and click the submit button
    await newPage.waitForSelector('div#submissionArea span#submitButton', { timeout: 30000 });
    const submitButton = await newPage.$('div#submissionArea span#submitButton');
    if (!submitButton) throw new Error('ERROR: Submit button not found');
    await Promise.all([
      submitButton.click(),
      newPage.waitForLoadState('networkidle')
    ]);
    console.log(`LOG: Clicked submit button`);

    // 8. Wait for dashboard and validate correct site
    await newPage.waitForURL(/https:\/\/fbs\.intranet\.smu\.edu\.sg\//, { timeout: 30000 });

    const finalUrl = newPage.url();
    const fbsPage = newPage;
    console.log(`LOG: Arrived at dashboard at url ${finalUrl}`);

    // ---- NAVIGATE TO TASK LIST ---- //

    // 1. Navigate to home first
    console.log(`LOG: Navigating to home page`);
    await fbsPage.goto('https://fbs.intranet.smu.edu.sg/home', {
      waitUntil: 'domcontentloaded',
      timeout: 60000
    });
    console.log(`LOG: Successfully loaded home page`);

    // 2. Wait for frameBottom to load
    await fbsPage.waitForSelector('iframe#frameBottom', { timeout: 20000 });
    const frameBottomElement = await fbsPage.$('iframe#frameBottom');
    if (!frameBottomElement) throw new Error('iframe#frameBottom not found');
    const frameBottom = await frameBottomElement.contentFrame();
    if (!frameBottom) throw new Error('Frame object for frameBottom not available');
    console.log(`LOG: Frame bottom loaded`);

    // 3. Click on Task List Tab inside frameBottom
    console.log(`LOG: Looking for div#TaskListTab in frameBottom`);
    await frameBottom.waitForSelector('div#TaskListTab', { timeout: 30000 });
    await frameBottom.click('div#TaskListTab');
    console.log(`LOG: Clicked on Task List tab`);

    // 4. Wait for a long period to allow content to load (10-15 seconds)
    console.log(`LOG: Waiting 15 seconds for content to load`);
    await fbsPage.waitForTimeout(15000);

    // 5. Switch to frameContent inside frameBottom
    await frameBottom.waitForSelector('iframe#frameContent', { timeout: 20000 });
    const frameContentElement = await frameBottom.$('iframe#frameContent');
    if (!frameContentElement) throw new Error('iframe#frameContent not found');
    const frameContent = await frameContentElement.contentFrame();
    if (!frameContent) throw new Error('Frame object for frameContent not available');
    console.log(`LOG: Content frame loaded`);

    // 6. Wait for the tasks table
    await frameContent.waitForSelector('div#GridViewTask', { timeout: 20000 });
    console.log(`LOG: Found div#GridViewTask`);

    // 7. Parse the table
    const tasks = [];
    const rows = await frameContent.locator('div#GridViewTask table tbody tr.row').all();
    console.log(`LOG: Found ${rows.length} task rows in table`);

    for (const row of rows) {
      const cells = await row.locator('td').all();
      if (cells.length > 0) {
        const rowData = [];
        for (const cell of cells) {
          const text = (await cell.innerText()).trim();
          rowData.push(text);
        }
        tasks.push(rowData);
        console.log(`LOG: Row data: ${JSON.stringify(rowData)}`);
      }
    }

    // 8. Create log data
    const scrapeEndTime = Date.now();
    const logData = {
      metadata: {
        version: "1.0.0",
        scraped_at: (new Date()).toISOString(),
        scrape_duration_ms: scrapeEndTime - scrapeStartTime,
        success: true,
        error: null,
        scraper_version: "tasks-v1.0.0"
      },
      raw_tasks: tasks,
      tasks_count: tasks.length
    };

    fs.writeFileSync(outputLog, JSON.stringify(logData, null, 2));
    console.log('✅ Task list scraping complete. Data written to:', outputLog);

    if (browser) await browser.close();

  } catch (error) {
    const scrapeEndTime = Date.now();
    console.error('❌ Task list scraping failed:', error.message);

    // Write error log
    const errorLogData = {
      metadata: {
        version: "1.0.0",
        scraped_at: (new Date()).toISOString(),
        scrape_duration_ms: scrapeEndTime - scrapeStartTime,
        success: false,
        error: error.message,
        scraper_version: "tasks-v1.0.0"
      },
      raw_tasks: [],
      tasks_count: 0
    };

    fs.writeFileSync(outputLog, JSON.stringify(errorLogData, null, 2));
    console.log('Error log written to:', outputLog);

    if (browser) await browser.close();
    process.exit(1);
  }
})();
