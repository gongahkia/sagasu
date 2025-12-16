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

function toMinutes(timeStr) {
  const [h, m] = timeStr.split(":").map(Number);
  return h * 60 + m;
}

function minutesToTimeStr(minutes) {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function timeslotStr(start, end) {
  return `${minutesToTimeStr(start)}-${minutesToTimeStr(end)}`;
}

function getTodayDate() {
  const today = new Date();
  const day = String(today.getDate()).padStart(2, '0');
  const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const month = monthNames[today.getMonth()];
  const year = today.getFullYear();
  return `${day}-${month}-${year}`;
}

function parseTimeRange(timeRangeStr) {
  const [startStr, endStr] = timeRangeStr.split("-");
  return [toMinutes(startStr), toMinutes(endStr)];
}

function extractBookingTime(rawStr) {
  const match = rawStr.match(/Booking Time: (\d{2}:\d{2}-\d{2}:\d{2})/);
  return match ? match[1] : null;
}

function parseBookingDetails(detailsStr) {
  if (!detailsStr || detailsStr === "") return null;

  const extractField = (fieldName) => {
    const regex = new RegExp(`${fieldName}:\\s*(.*)`, 'm');
    const match = detailsStr.match(regex);
    return match ? match[1].trim() : "";
  };

  return {
    reference: extractField("Booking Reference Number"),
    status: extractField("Booking Status"),
    booker_name: extractField("Booked for User Name"),
    booker_email: extractField("Booked for User Email Address"),
    booker_org: extractField("Booked for User Org Unit"),
    purpose: extractField("Purpose of Booking"),
    use_type: extractField("Use Type")
  };
}

function extractRoomMetadata(roomName, buildingFilter, floorFilter, facilityFilter, equipmentFilter) {
  // Extract building code (e.g., "KGC" from "KGC-4.02-PR")
  const buildingCode = roomName.split('-')[0] || "";

  // Map building codes to full names (partial mapping, expand as needed)
  const buildingMap = {
    "KGC": "Kwa Geok Choo Law Library",
    "YPHSL": "Yong Pung How School of Law",
    "LKCSB": "Lee Kong Chian School of Business",
    "SOA": "School of Accountancy",
    "SCIS": "School of Computing & Information Systems",
    "SOE": "School of Economics",
    "SOSS": "School of Social Sciences",
    "CIS": "College of Integrative Studies",
    "LKSL": "Li Ka Shing Library",
    "AB": "Administration Building",
    "SMUC": "SMU Connexion"
  };

  // Extract floor from room name (e.g., "4" from "KGC-4.02-PR")
  const floorMatch = roomName.match(/-(\d+|B\d+)\./i);
  let floor = "Unknown";
  if (floorMatch) {
    const floorNum = floorMatch[1];
    if (floorNum.startsWith('B')) {
      floor = `Basement ${floorNum.substring(1)}`;
    } else {
      floor = `Level ${floorNum}`;
    }
  }

  // Use filters as fallback if extraction fails
  const building = buildingMap[buildingCode] || (buildingFilter.length > 0 ? buildingFilter[0] : "Unknown");

  return {
    building_code: buildingCode,
    building: building,
    floor: floor,
    facility_type: facilityFilter.length > 0 ? facilityFilter[0] : "Unknown",
    equipment: equipmentFilter
  };
}

function normalizeStatus(status) {
  if (status === "not available due to timeslot") return "unavailable";
  if (status === "free") return "free";
  if (status === "booked") return "booked";
  return "unknown";
}

function calculateAvailabilitySummary(timeslots) {
  const now = new Date();
  const currentMinutes = now.getHours() * 60 + now.getMinutes();

  let freeCount = 0;
  let freeDuration = 0;
  let isAvailableNow = false;
  let nextAvailableAt = null;

  for (const slot of timeslots) {
    if (slot.status === "free") {
      freeCount++;
      const start = toMinutes(slot.start);
      const end = toMinutes(slot.end);
      const duration = end - start;
      freeDuration += duration;

      // Check if currently available
      if (start <= currentMinutes && currentMinutes < end) {
        isAvailableNow = true;
      }

      // Find next available slot
      if (!isAvailableNow && start > currentMinutes && !nextAvailableAt) {
        const nextDate = new Date(now);
        nextDate.setHours(Math.floor(start / 60), start % 60, 0, 0);
        nextAvailableAt = nextDate.toISOString();
      }
    }
  }

  return {
    is_available_now: isAvailableNow,
    next_available_at: nextAvailableAt,
    free_slots_count: freeCount,
    free_duration_minutes: freeDuration
  };
}

function generateTimeslotsForRoom(rawTimeslotsForRoom) {
  const DAY_START = 0;
  const DAY_END = 24 * 60;
  const slots = [];
  for (const ts of rawTimeslotsForRoom) {
    if (ts.includes("(not available)")) {
      const match = ts.match(/\((\d{2}:\d{2}-\d{2}:\d{2})\) \(not available\)/);
      if (!match) throw new Error(`Unexpected not available format: ${ts}`);
      const timeRangeStr = match[1];
      const [startMin, endMin] = parseTimeRange(timeRangeStr);
      slots.push({
        timeslot: timeRangeStr,
        status: "not available due to timeslot",
        details: "",
        startMin,
        endMin
      });
    } else if (ts.startsWith("Booking Time:")) {
      const bookingTime = extractBookingTime(ts);
      if (!bookingTime) throw new Error(`Cannot extract booking time from: ${ts}`);
      const [startMin, endMin] = parseTimeRange(bookingTime);
      slots.push({
        timeslot: bookingTime,
        status: "booked",
        details: ts,
        startMin,
        endMin
      });
    } else {
      throw new Error(`Unexpected raw_timeslot format: ${ts}`);
    }
  }
  slots.sort((a, b) => a.startMin - b.startMin);
  const fullSlots = [];
  let cursor = DAY_START;
  for (const slot of slots) {
    if (slot.startMin > cursor) {
      const [freeStart, freeEnd] = [minutesToTimeStr(cursor), minutesToTimeStr(slot.startMin)];
      fullSlots.push({
        start: freeStart,
        end: freeEnd,
        status: "free"
      });
    }

    const [slotStart, slotEnd] = slot.timeslot.split("-");
    const normalized = normalizeStatus(slot.status);
    const timeslotObj = {
      start: slotStart,
      end: slotEnd,
      status: normalized
    };

    if (normalized === "unavailable") {
      timeslotObj.reason = "Outside scrape window";
    } else if (normalized === "booked") {
      const booking = parseBookingDetails(slot.details);
      if (booking) {
        timeslotObj.booking = booking;
      }
    }

    fullSlots.push(timeslotObj);
    cursor = slot.endMin;
  }
  if (cursor < DAY_END) {
    fullSlots.push({
      start: minutesToTimeStr(cursor),
      end: minutesToTimeStr(DAY_END),
      status: "free"
    });
  }
  return fullSlots;
}

function mapTimeslotsToRooms(rawRooms, rawTimeslots) {
  const result = {};
  const roomCount = rawRooms.length;
  const roomStartPattern = /^\(00:00-\d{2}:\d{2}\) \(not available\)$/;
  let currentRoomIndex = 0;
  let acc = [];
  for (const ts of rawTimeslots) {
  if (roomStartPattern.test(ts) && acc.length > 0) {
      if (currentRoomIndex >= roomCount) {
        throw new Error("More timeslot blocks than rooms");
      }
      result[rawRooms[currentRoomIndex]] = generateTimeslotsForRoom(acc);
      currentRoomIndex++;
      acc = [];
    }
    acc.push(ts);
  }
  if (acc.length > 0) {
    if (currentRoomIndex >= roomCount) {
      throw new Error("More timeslot blocks than rooms");
    }
    result[rawRooms[currentRoomIndex]] = generateTimeslotsForRoom(acc);
  }
  while (currentRoomIndex + 1 < roomCount) {
    currentRoomIndex++;
    result[rawRooms[currentRoomIndex]] = [{
      timeslot: "00:00-24:00",
      status: "free",
      details: ""
    }];
  }
  return result;
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
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();

    // 1. Go to the initial site
    console.log(`LOG: Navigating to ${url}`);
    await page.goto(url, {
      waitUntil: 'domcontentloaded',  // Less strict than networkidle
      timeout: 60000  // 60 second timeout
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
  console.log(`LOG: Arrived at dashboard at url ${finalUrl} and saved screenshot`);

  // ---- SCRAPING & FILTERING ---- //

  // 1. Switch to core frame
  await fbsPage.waitForSelector('iframe#frameBottom', { timeout: 20000 });
  const frameBottomElement = await fbsPage.$('iframe#frameBottom');
  if (!frameBottomElement) throw new Error('iframe#frameBottom not found');
  const frameBottom = await frameBottomElement.contentFrame();
  if (!frameBottom) throw new Error('Frame object for frameBottom not available');
  console.log(`LOG: Content frame bottom loaded`);

  // 2. Switch to core content frame
  await frameBottom.waitForSelector('iframe#frameContent', { timeout: 20000 });
  const frameContentElement = await frameBottom.$('iframe#frameContent');
  if (!frameContentElement) throw new Error('iframe#frameContent not found inside frameBottom');
  const frameContent = await frameContentElement.contentFrame();
  if (!frameContent) throw new Error('Frame object for frameContent not available');
  console.log(`LOG: Core content frame loaded`);

  // 3. Wait for and set the date picker
  await frameContent.waitForSelector('input#DateBookingFrom_c1_textDate', { timeout: 20000 });
  await frameContent.click('input#DateBookingFrom_c1_textDate');
  const desiredDate = SCRAPE_CONFIG.date;
  const initialDate = await frameContent.$eval(
    'input#DateBookingFrom_c1_textDate',
    el => el.value
  );
  if (initialDate === desiredDate) {
    console.log(`LOG: Initial date already ${desiredDate}, clicking forward and backward once to refresh`);
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
      console.log(`LOG: Date picker set to desired date: ${currentDate}`);
      break;
    }
    console.log(`LOG: Date is ${currentDate} and desired date is ${desiredDate}. Clicking next to try to reach ${desiredDate}`);
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
  console.log(`LOG: Set start and end time dropdowns to ${SCRAPE_CONFIG.startTime} and ${SCRAPE_CONFIG.endTime}`);
  await frameContent.waitForTimeout(3000); 
  console.log(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

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
      console.log('LOG: Clicked OK button in building selection');
    } else {
      console.warn('ERROR: OK button not found in building selection, fallback to pressing Escape');
      await fbsPage.keyboard.press('Escape');
    }
  }
  console.log(`LOG: Set building(s) to ${SCRAPE_CONFIG.buildingNames}`);

  await frameContent.waitForTimeout(3000); 
  console.log(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

  // 6. Set floor(s)
  if (SCRAPE_CONFIG.floorNames?.length) {
    await frameContent.locator('#DropMultiFloorList_c1_textItem').click();
    for (const floor of SCRAPE_CONFIG.floorNames) {
      await frameContent.locator(`text="${floor}"`).click();
    }
    const okButtonFloorContainer = await frameContent.locator('#DropMultiFloorList_c1_panelContainer input[type="button"][value="OK"]');
    if (await okButtonFloorContainer.count() > 0) {
      await okButtonFloorContainer.click();
      console.log('LOG: Clicked OK button in floor selection');
    } else {
      console.warn('ERROR: OK button not found in floor selection, fallback to pressing Escape');
      await fbsPage.keyboard.press('Escape');
    }
  }
  console.log(`LOG: Set floor(s) to ${SCRAPE_CONFIG.floorNames}`);

  await frameContent.waitForTimeout(3000); 
  console.log(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

  // 7. Set facility type(s)
  if (SCRAPE_CONFIG.facilityTypes?.length) {
    await frameContent.locator('#DropMultiFacilityTypeList_c1_textItem').click();
    for (const facType of SCRAPE_CONFIG.facilityTypes) {
      await frameContent.locator(`text="${facType}"`).click();
    }
    const okButtonFacilityContainer = await frameContent.locator('#DropMultiFacilityTypeList_c1_panelContainer input[type="button"][value="OK"]');
    if (await okButtonFacilityContainer.count() > 0) {
      await okButtonFacilityContainer.click();
      console.log('LOG: Clicked OK button in facility type selection');
    } else {
      console.warn('ERROR: OK button not found in facility type selection, fallback to pressing Escape');
      await fbsPage.keyboard.press('Escape');
    }
  }
  console.log(`LOG: Set facility type(s) to ${SCRAPE_CONFIG.facilityTypes}`);

  await frameContent.waitForTimeout(3000); 
  console.log(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

  // 8. Set room capacity (optional)
  if (SCRAPE_CONFIG.roomCapacity) {
    await frameContent.locator('select#DropCapacity_c1').selectOption({ value: SCRAPE_CONFIG.roomCapacity });
    console.log(`LOG: Set room capacity to ${SCRAPE_CONFIG.roomCapacity}`);
  } else {
    console.log(`LOG: Skipping room capacity filter (not specified)`);
  }

  await frameContent.waitForTimeout(3000);
  console.log(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

  // 9. Set equipment (optional)
  if (SCRAPE_CONFIG.equipment?.length) {
    await frameContent.locator('#DropMultiEquipmentList_c1_textItem').click();
    for (const eq of SCRAPE_CONFIG.equipment) {
      await frameContent.locator(`text="${eq}"`).click();
    }
    const okButtonEquipmentContainer= await frameContent.locator('#DropMultiEquipmentList_c1_panelContainer input[type="button"][value="OK"]');
    if (await okButtonEquipmentContainer.count() > 0) {
      await okButtonEquipmentContainer.click();
      console.log('LOG: Clicked OK button in equipment selection');
    } else {
      console.warn('ERROR: OK button not found in equipment selection, fallback to pressing Escape');
      await fbsPage.keyboard.press('Escape');
    }
  }
  console.log(`LOG: Set equipment to ${SCRAPE_CONFIG.equipment}`);

  await frameContent.waitForTimeout(3000); 
  console.log(`LOG: Forcing a timeout of 3000ms to allow the page to update`);

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
    console.log('LOG: No rooms found.');
    await browser.close();
    return;
  }
  console.log(`LOG: Matched ${matchingRooms.length} rooms (${matchingRooms})`);

  // 11. Click "Check Availability" 
  await frameContent.locator('a#CheckAvailability').click();
  await fbsPage.waitForLoadState('networkidle');
  console.log(`LOG: Clicked "Check Availability" button`);

  // 12. Navigate to results page
  await frameContent.waitForTimeout(10000); 
  console.log(`LOG: Forcing a timeout of 10000ms to allow the page to update`);

  // 13. Scrape time slots (room and timeslot booking state)
  const eventDivs = await frameContent.locator('div.scheduler_bluewhite_event.scheduler_bluewhite_event_line0').all();
  let rawBookings = [];
  for (const slotDiv of eventDivs) {
    const timeslotInfo = await slotDiv.getAttribute('title');
    rawBookings.push(timeslotInfo);
    // console.log(`LOG: Found raw timeslot info ${timeslotInfo}`);
  }
  console.log(`LOG: Found ${rawBookings.length} timeslots (${rawBookings})`);

  // 14. Map rooms to timeslots
  const scrapeEndTime = Date.now();
  mapping = mapTimeslotsToRooms(matchingRooms, rawBookings);
  console.log(`LOG: Mapped rooms to timeslots`);

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
    console.log('✅ Scraping complete. Data written to:', outputLog);

    // await fbsPage.pause(); // debug pause for manual inspection
    if (browser) await browser.close();

  } catch (error) {
    const scrapeEndTime = Date.now();
    console.error('❌ Scraping failed:', error.message);

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
    console.log('Error log written to:', outputLog);

    if (browser) await browser.close();
    process.exit(1);
  }
})();