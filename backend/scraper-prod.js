//
// --- CONFIGURATION ---
//

const { chromium } = require('playwright');
const fs = require('fs');
require('dotenv').config();
const {
  toMinutes, minutesToTimeStr, timeslotStr, getTodayDate,
  parseTimeRange, extractBookingTime, parseBookingDetails,
  extractRoomMetadata, normalizeStatus, calculateAvailabilitySummary,
  generateTimeslotsForRoom, mapTimeslotsToRooms,
} = require('./utils');
const { withRetry } = require('./retry');
const { logger } = require('./logger');

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

// Required credentials
const EMAIL = requireEnv('SMU_EMAIL');
const PASSWORD = requireEnv('SMU_PASSWORD');

// Get scrape date - use TODAY if env var is not set or is set to "TODAY"
const scrapeDate = process.env.SCRAPE_DATE && process.env.SCRAPE_DATE !== 'TODAY'
  ? process.env.SCRAPE_DATE
  : getTodayDate();

const SCRAPE_CONFIG = {
  date: scrapeDate,
  startTime: requireEnv('SCRAPE_START_TIME'),
  endTime: requireEnv('SCRAPE_END_TIME'),
  roomCapacity: process.env.SCRAPE_ROOM_CAPACITY || '',
  buildingNames: process.env.SCRAPE_BUILDING_NAMES
    ? process.env.SCRAPE_BUILDING_NAMES.split(',').map(s => s.trim())
    : [],
  floorNames: process.env.SCRAPE_FLOOR_NAMES
    ? process.env.SCRAPE_FLOOR_NAMES.split(',').map(s => s.trim())
    : [],
  facilityTypes: process.env.SCRAPE_FACILITY_TYPES
    ? process.env.SCRAPE_FACILITY_TYPES.split(',').map(s => s.trim())
    : [],
  equipment: process.env.SCRAPE_EQUIPMENT
    ? process.env.SCRAPE_EQUIPMENT.split(',').map(s => s.trim())
    : [],
}

const url = "https://www.smubondue.com/facility-booking-system-fbs";
const outputLog = './log/scraped_log.json';

//
// --- MAIN SCRIPT ---
//

(async () => {
  const scrapeStartTime = Date.now();
  let browser;

  try {
    await withRetry(async () => { // retry wrapper with exponential backoff
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();

    // 1. Go to the initial site
    logger.info(`LOG: Navigating to ${url}`);
    await page.goto(url, {
      waitUntil: 'domcontentloaded',  // Less strict than networkidle
      timeout: 60000  // 60 second timeout
    });
    logger.info(`LOG: Successfully loaded ${url}`);

  // 2. Open Microsoft login in new tab
  const [newPage] = await Promise.all([
    context.waitForEvent('page', { timeout: 30000 }),
    page.click('a[aria-label="SMU FBS"]'),
  ]);

  // 3. Wait for Microsoft login URL to appear
  await newPage.waitForURL(/login\.microsoftonline\.com/, { timeout: 30000 });
  await newPage.waitForSelector('input[type="email"], #i0116', { timeout: 30000 });
  logger.info(`LOG: Navigating to ${newPage.url()}`);

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
  logger.info(`LOG: Filled in email ${EMAIL} and clicked next`);

  // 5. Wait for SMU redirect or click fallback
  try {
    await newPage.waitForURL(/login2\.smu\.edu\.sg/, { timeout: 10000 });
    logger.info('LOG: Redirected to SMU SSO');
  } catch (e) {
    const redirectLink = await newPage.$('a#redirectToIdpLink');
    if (redirectLink) {
      logger.info('Redirect took too long, clicking #redirectToIdpLink...');
      await Promise.all([
        redirectLink.click(),
      ]);
    } else {
      logger.info('Redirect delay detected, but #redirectToIdpLink not found.');
    }
    await newPage.waitForURL(/login2\.smu\.edu\.sg/, { timeout: 30000 });
  }
  logger.info(`LOG: Navigated to ${newPage.url()}`);

  // 6. Wait for password input, fill in password
  await newPage.waitForSelector('input#passwordInput', { timeout: 30000 });
  const passwordInput = await newPage.$('input#passwordInput');
  if (!passwordInput) throw new Error('ERROR: Password input not found');
  await passwordInput.fill(PASSWORD);
  logger.info(`LOG: Filled in password`);

  // 7. Find and click the submit button
  await newPage.waitForSelector('div#submissionArea span#submitButton', { timeout: 30000 });
  const submitButton = await newPage.$('div#submissionArea span#submitButton');
  if (!submitButton) throw new Error('ERROR: Submit button not found');
  await Promise.all([
    submitButton.click(),
    newPage.waitForLoadState('networkidle')
  ]);
  logger.info(`LOG: Clicked submit button`);

  // 8. Wait for dashboard and validate correct site
  await newPage.waitForURL(/https:\/\/fbs\.intranet\.smu\.edu\.sg\//, { timeout: 30000 });

  const finalUrl = newPage.url();
  const fbsPage = newPage;
  logger.info(`LOG: Arrived at dashboard at url ${finalUrl} and saved screenshot`);

  // ---- SCRAPING & FILTERING ---- //

  // 1. Switch to core frame
  await fbsPage.waitForSelector('iframe#frameBottom', { timeout: 20000 });
  const frameBottomElement = await fbsPage.$('iframe#frameBottom');
  if (!frameBottomElement) throw new Error('iframe#frameBottom not found');
  const frameBottom = await frameBottomElement.contentFrame();
  if (!frameBottom) throw new Error('Frame object for frameBottom not available');
  logger.info(`LOG: Content frame bottom loaded`);

  // 2. Switch to core content frame
  await frameBottom.waitForSelector('iframe#frameContent', { timeout: 20000 });
  const frameContentElement = await frameBottom.$('iframe#frameContent');
  if (!frameContentElement) throw new Error('iframe#frameContent not found inside frameBottom');
  const frameContent = await frameContentElement.contentFrame();
  if (!frameContent) throw new Error('Frame object for frameContent not available');
  logger.info(`LOG: Core content frame loaded`);

  // 3. Wait for and set the date picker
  await frameContent.waitForSelector('input#DateBookingFrom_c1_textDate', { timeout: 20000 });
  await frameContent.click('input#DateBookingFrom_c1_textDate');
  const desiredDate = SCRAPE_CONFIG.date;
  const initialDate = await frameContent.$eval(
    'input#DateBookingFrom_c1_textDate',
    el => el.value
  );
  if (initialDate === desiredDate) {
    logger.info(`LOG: Initial date already ${desiredDate}, clicking forward and backward once to refresh`);
    await frameContent.click('a#BtnDpcNext');
    await frameContent.waitForTimeout(500);
    await frameContent.click('a#BtnDpcPrev');
    await frameContent.waitForTimeout(500);
  }
  for (let tries = 0; tries < 20; tries++) { 
    const currentDate = await frameContent.$eval(
      'input#DateBookingFrom_c1_textDate',
      el => el.value
    );
    if (currentDate === desiredDate) {
      logger.info(`LOG: Date picker set to desired date: ${currentDate}`);
      break;
    }
    logger.info(`LOG: Date is ${currentDate} and desired date is ${desiredDate}. Clicking next to try to reach ${desiredDate}`);
    await frameContent.click('a#BtnDpcNext');
    await frameContent.waitForTimeout(500);
  }
  const finalDate = await frameContent.$eval(
    'input#DateBookingFrom_c1_textDate',
    el => el.value
  );
  if (finalDate !== desiredDate) {
    throw new Error(`ERROR: Could not reach desired date "${desiredDate}". Final date was: "${finalDate}"`);
  }

  // 4. Set start and end time dropdowns
  await frameContent.selectOption('select#TimeFrom_c1_ctl04', SCRAPE_CONFIG.startTime);
  await frameContent.selectOption('select#TimeTo_c1_ctl04', SCRAPE_CONFIG.endTime);
  logger.info(`LOG: Set start and end time dropdowns to ${SCRAPE_CONFIG.startTime} and ${SCRAPE_CONFIG.endTime}`);
  await frameContent.waitForTimeout(3000); 
  logger.info(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

  // 5. Set building(s)
  if (SCRAPE_CONFIG.buildingNames?.length) {
    await frameContent.locator('#DropMultiBuildingList_c1_textItem').click();
    for (const building of SCRAPE_CONFIG.buildingNames) {
      await frameContent.locator(`text="${building}"`).click();
    }
    const okButtonBuildingContainer = frameContent.locator('#DropMultiBuildingList_c1_panelContainer input[type="button"][value="OK"]');
    await okButtonBuildingContainer.waitFor({ state: 'visible', timeout: 5000 });
    if (await okButtonBuildingContainer.count() > 0) {
      await okButtonBuildingContainer.click();
      logger.info('LOG: Clicked OK button in building selection');
    } else {
      logger.warn('ERROR: OK button not found in building selection, fallback to pressing Escape');
      await fbsPage.keyboard.press('Escape');
    }
  }
  logger.info(`LOG: Set building(s) to ${SCRAPE_CONFIG.buildingNames}`);

  await frameContent.waitForTimeout(3000); 
  logger.info(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

  // 6. Set floor(s)
  if (SCRAPE_CONFIG.floorNames?.length) {
    await frameContent.locator('#DropMultiFloorList_c1_textItem').click();
    for (const floor of SCRAPE_CONFIG.floorNames) {
      await frameContent.locator(`text="${floor}"`).click();
    }
    const okButtonFloorContainer = await frameContent.locator('#DropMultiFloorList_c1_panelContainer input[type="button"][value="OK"]');
    if (await okButtonFloorContainer.count() > 0) {
      await okButtonFloorContainer.click();
      logger.info('LOG: Clicked OK button in floor selection');
    } else {
      logger.warn('ERROR: OK button not found in floor selection, fallback to pressing Escape');
      await fbsPage.keyboard.press('Escape');
    }
  }
  logger.info(`LOG: Set floor(s) to ${SCRAPE_CONFIG.floorNames}`);

  await frameContent.waitForTimeout(3000); 
  logger.info(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

  // 7. Set facility type(s)
  if (SCRAPE_CONFIG.facilityTypes?.length) {
    await frameContent.locator('#DropMultiFacilityTypeList_c1_textItem').click();
    for (const facType of SCRAPE_CONFIG.facilityTypes) {
      await frameContent.locator(`text="${facType}"`).click();
    }
    const okButtonFacilityContainer = await frameContent.locator('#DropMultiFacilityTypeList_c1_panelContainer input[type="button"][value="OK"]');
    if (await okButtonFacilityContainer.count() > 0) {
      await okButtonFacilityContainer.click();
      logger.info('LOG: Clicked OK button in facility type selection');
    } else {
      logger.warn('ERROR: OK button not found in facility type selection, fallback to pressing Escape');
      await fbsPage.keyboard.press('Escape');
    }
  }
  logger.info(`LOG: Set facility type(s) to ${SCRAPE_CONFIG.facilityTypes}`);

  await frameContent.waitForTimeout(3000); 
  logger.info(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

  // 8. Set room capacity (optional)
  if (SCRAPE_CONFIG.roomCapacity) {
    await frameContent.locator('select#DropCapacity_c1').selectOption({ value: SCRAPE_CONFIG.roomCapacity });
    logger.info(`LOG: Set room capacity to ${SCRAPE_CONFIG.roomCapacity}`);
  } else {
    logger.info(`LOG: Skipping room capacity filter (not specified)`);
  }

  await frameContent.waitForTimeout(3000);
  logger.info(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

  // 9. Set equipment (optional)
  if (SCRAPE_CONFIG.equipment?.length) {
    await frameContent.locator('#DropMultiEquipmentList_c1_textItem').click();
    for (const eq of SCRAPE_CONFIG.equipment) {
      await frameContent.locator(`text="${eq}"`).click();
    }
    const okButtonEquipmentContainer= await frameContent.locator('#DropMultiEquipmentList_c1_panelContainer input[type="button"][value="OK"]');
    if (await okButtonEquipmentContainer.count() > 0) {
      await okButtonEquipmentContainer.click();
      logger.info('LOG: Clicked OK button in equipment selection');
    } else {
      logger.warn('ERROR: OK button not found in equipment selection, fallback to pressing Escape');
      await fbsPage.keyboard.press('Escape');
    }
  }
  logger.info(`LOG: Set equipment to ${SCRAPE_CONFIG.equipment}`);

  await frameContent.waitForTimeout(3000); 
  logger.info(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

  // 10. Retrieve available rooms
  await frameContent.locator('table#GridResults_gv').waitFor({ timeout: 20000 });
  const roomRows = await frameContent.locator('table#GridResults_gv tbody tr').all();
  let matchingRooms = [];
  for (const row of roomRows) {
    const tds = await row.locator('td').all();
    if (tds.length > 1) {
      const roomName = (await tds[1].innerText()).trim();
      matchingRooms.push(roomName);
    }
  }
  if (matchingRooms.length === 0) {
    logger.info('LOG: No rooms found.');
    await browser.close();
    return;
  }
  logger.info(`LOG: Matched ${matchingRooms.length} rooms (${matchingRooms})`);

  // 11. Click "Check Availability" 
  await frameContent.locator('a#CheckAvailability').click();
  await fbsPage.waitForLoadState('networkidle');
  logger.info(`LOG: Clicked "Check Availability" button`);

  // 12. Navigate to results page
  await frameContent.waitForTimeout(10000); 
  logger.info(`LOG: Forcing a timeout of 10000ms to allow the page to update`);

  // 13. Scrape time slots (room and timeslot booking state)
  const eventDivs = await frameContent.locator('div.scheduler_bluewhite_event.scheduler_bluewhite_event_line0').all();
  let rawBookings = [];
  for (const slotDiv of eventDivs) {
    const timeslotInfo = await slotDiv.getAttribute('title');
    rawBookings.push(timeslotInfo);
    // logger.info(`LOG: Found raw timeslot info ${timeslotInfo}`);
  }
  logger.info(`LOG: Found ${rawBookings.length} timeslots (${rawBookings})`);

  // 14. Map rooms to timeslots
  const scrapeEndTime = Date.now();
  mapping = mapTimeslotsToRooms(matchingRooms, rawBookings);
  logger.info(`LOG: Mapped rooms to timeslots`);

  // 15. Transform to enhanced format
  const rooms = [];
  let totalAvailable = 0;
  let totalBooked = 0;
  let totalPartiallyAvailable = 0;

  for (const roomName of Object.keys(mapping)) {
    const timeslots = mapping[roomName];
    const metadata = extractRoomMetadata(
      roomName,
      SCRAPE_CONFIG.buildingNames,
      SCRAPE_CONFIG.floorNames,
      SCRAPE_CONFIG.facilityTypes,
      SCRAPE_CONFIG.equipment
    );
    const availabilitySummary = calculateAvailabilitySummary(timeslots);

    // Update statistics
    if (availabilitySummary.free_slots_count > 0) {
      totalPartiallyAvailable++;
      if (availabilitySummary.is_available_now) {
        totalAvailable++;
      }
    } else {
      totalBooked++;
    }

    rooms.push({
      id: roomName,
      name: roomName,
      building: metadata.building,
      building_code: metadata.building_code,
      floor: metadata.floor,
      facility_type: metadata.facility_type,
      equipment: metadata.equipment,
      timeslots: timeslots,
      availability_summary: availabilitySummary
    });
  }

  // 16. Create enhanced log data
  const logData = {
    metadata: {
      version: "4.0.0",
      scraped_at: (new Date()).toISOString(),
      scrape_duration_ms: scrapeEndTime - scrapeStartTime,
      success: true,
      error: null,
      scraper_version: "prod-v1.0.0"
    },
    config: {
      date: SCRAPE_CONFIG.date,
      start_time: SCRAPE_CONFIG.startTime,
      end_time: SCRAPE_CONFIG.endTime,
      filters: {
        buildings: SCRAPE_CONFIG.buildingNames,
        floors: SCRAPE_CONFIG.floorNames,
        facility_types: SCRAPE_CONFIG.facilityTypes,
        equipment: SCRAPE_CONFIG.equipment,
        capacity: SCRAPE_CONFIG.roomCapacity
      }
    },
    statistics: {
      total_rooms: matchingRooms.length,
      available_rooms: totalAvailable,
      booked_rooms: totalBooked,
      partially_available_rooms: totalPartiallyAvailable
    },
    rooms: rooms
  };

    fs.writeFileSync(outputLog, JSON.stringify(logData, null, 2));
    logger.info('✅ Scraping complete. Data written to:', outputLog);

    // await fbsPage.pause(); // debug pause for manual inspection
    if (browser) await browser.close();
    }); // end withRetry

  } catch (error) {
    const scrapeEndTime = Date.now();
    logger.error('❌ Scraping failed:', error.message);

    // Write error log
    const errorLogData = {
      metadata: {
        version: "4.0.0",
        scraped_at: (new Date()).toISOString(),
        scrape_duration_ms: scrapeEndTime - scrapeStartTime,
        success: false,
        error: error.message,
        scraper_version: "prod-v1.0.0"
      },
      config: {
        date: SCRAPE_CONFIG.date,
        start_time: SCRAPE_CONFIG.startTime,
        end_time: SCRAPE_CONFIG.endTime,
        filters: {
          buildings: SCRAPE_CONFIG.buildingNames,
          floors: SCRAPE_CONFIG.floorNames,
          facility_types: SCRAPE_CONFIG.facilityTypes,
          equipment: SCRAPE_CONFIG.equipment,
          capacity: SCRAPE_CONFIG.roomCapacity
        }
      },
      statistics: {
        total_rooms: 0,
        available_rooms: 0,
        booked_rooms: 0,
        partially_available_rooms: 0
      },
      rooms: []
    };

    fs.writeFileSync(outputLog, JSON.stringify(errorLogData, null, 2));
    logger.info('Error log written to:', outputLog);

    if (browser) await browser.close();
    process.exit(1);
  }
})();