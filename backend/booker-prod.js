//
// --- CONFIGURATION ---
//

const { chromium } = require('playwright');
const fs = require('fs');
require('dotenv').config();

//
// --- HELPER FUNCTIONS (Reused from scraper-prod.js) ---
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
// --- BOOKING-SPECIFIC FUNCTIONS ---
//

/**
 * Find the first room that contains the desired booking time within a free slot
 * @param {Array} rooms - Array of room objects with timeslots
 * @param {string} desiredStartTime - Desired booking start time (e.g., "14:00")
 * @param {string} desiredEndTime - Desired booking end time (e.g., "16:00")
 * @returns {Object|null} - Matching room object or null if no match
 */
function findMatchingRoom(rooms, desiredStartTime, desiredEndTime) {
  const desiredStart = toMinutes(desiredStartTime);
  const desiredEnd = toMinutes(desiredEndTime);

  for (const room of rooms) {
    for (const slot of room.timeslots) {
      if (slot.status === "free") {
        const slotStart = toMinutes(slot.start);
        const slotEnd = toMinutes(slot.end);

        // Check if desired time is contained within this free slot
        if (slotStart <= desiredStart && desiredEnd <= slotEnd) {
          console.log(`✅ Found matching room: ${room.name}`);
          console.log(`   Free slot: ${slot.start} - ${slot.end}`);
          console.log(`   Desired booking: ${desiredStartTime} - ${desiredEndTime}`);
          return { room, matchingSlot: slot };
        }
      }
    }
  }

  console.log(`❌ No room found with availability for ${desiredStartTime} - ${desiredEndTime}`);
  return null;
}

/**
 * Perform the booking automation
 * @param {Object} frameContent - Playwright frame object for the booking interface
 * @param {Object} page - Playwright page object
 * @param {string} roomName - Name of the room to book
 * @param {string} desiredStartTime - Desired booking start time
 * @param {string} desiredEndTime - Desired booking end time
 * @param {string} purpose - Booking purpose
 * @param {string} cobookerName - Co-booker name
 */
async function performBooking(frameContent, page, roomName, desiredStartTime, desiredEndTime, purpose, cobookerName) {
  console.log(`\n🎯 Starting booking process for room: ${roomName}`);

  // Step 1: Find and click on an available cell for the target room
  console.log(`LOG: Finding room row for "${roomName}"`);

  // Get all room header divs
  const roomHeaders = await frameContent.locator('div.scheduler_bluewhite_rowheader_inner').all();
  let targetRoomIndex = -1;

  for (let i = 0; i < roomHeaders.length; i++) {
    const headerText = await roomHeaders[i].innerText();
    if (headerText.trim() === roomName) {
      targetRoomIndex = i;
      console.log(`LOG: Found room "${roomName}" at index ${i}`);
      break;
    }
  }

  if (targetRoomIndex === -1) {
    throw new Error(`Room "${roomName}" not found in booking grid`);
  }

  // Find and click a single cell for this room at the desired booking start time
  console.log(`LOG: Finding clickable cell for room index ${targetRoomIndex} at time ${desiredStartTime}`);

  // Get all cells (they are rendered column-by-column, top to bottom)
  const allCells = await frameContent.locator('div.scheduler_bluewhite_cell').all();
  const totalRooms = roomHeaders.length;

  console.log(`LOG: Total rooms: ${totalRooms}, Total cells: ${allCells.length}`);

  // Calculate which column corresponds to the desired start time
  // Each column is a 30-minute slot
  const desiredStartMinutes = toMinutes(desiredStartTime);
  const columnIndex = Math.floor(desiredStartMinutes / 30); // 30 minutes per column

  console.log(`LOG: Desired time ${desiredStartTime} = ${desiredStartMinutes} minutes = column ${columnIndex}`);

  // Calculate the cell index: (column * totalRooms) + roomIndex
  const targetCellIndex = (columnIndex * totalRooms) + targetRoomIndex;

  console.log(`LOG: Attempting to click cell at index ${targetCellIndex} (room ${targetRoomIndex}, column ${columnIndex})`);

  if (targetCellIndex >= allCells.length) {
    throw new Error(`Target cell ${targetCellIndex} out of bounds (max: ${allCells.length})`);
  }

  // Use force: true to bypass any overlays
  await allCells[targetCellIndex].click({ force: true });
  console.log(`LOG: Successfully clicked cell for ${desiredStartTime}`);

  await frameContent.waitForTimeout(2000);

  // Step 2: Click "Make Booking" button
  console.log(`LOG: Clicking "Make Booking" button`);
  await frameContent.locator('a#btnMakeBooking').click();
  console.log(`LOG: Waiting for page navigation after clicking Make Booking...`);

  // Wait for navigation to complete
  await page.waitForLoadState('networkidle', { timeout: 10000 });
  await page.waitForTimeout(5000); // Wait for full page load

  // Step 2.5: Wait for booking form to actually load
  console.log(`LOG: Waiting for booking form to load in iframe...`);

  // Wait for a specific element that only exists on the booking form page
  // This ensures the iframe has actually navigated to the booking form
  try {
    // Wait up to 15 seconds for the booking form purpose input to appear
    await frameContent.waitForSelector('input#bookingFormControl1_TextboxPurpose_c1', { timeout: 15000 });
    console.log(`LOG: Booking form has loaded (found purpose input field)`);
  } catch (e) {
    console.log(`WARN: Booking form purpose field not found in original iframe context, refreshing iframe references...`);

    // The iframe content has changed, get fresh references
    await page.waitForTimeout(2000);

    const newFrameBottomElement = await page.$('iframe#frameBottom');
    if (!newFrameBottomElement) throw new Error('iframe#frameBottom not found after navigation');
    const newFrameBottom = await newFrameBottomElement.contentFrame();
    if (!newFrameBottom) throw new Error('Frame object for frameBottom not available after navigation');

    const newFrameContentElement = await newFrameBottom.$('iframe#frameContent');
    if (!newFrameContentElement) throw new Error('iframe#frameContent not found after navigation');
    const newFrameContent = await newFrameContentElement.contentFrame();
    if (!newFrameContent) throw new Error('Frame object for frameContent not available after navigation');

    console.log(`LOG: Iframe references refreshed`);

    // Use the NEW frame reference
    frameContent = newFrameContent;

    // Try waiting for the purpose field again
    await frameContent.waitForSelector('input#bookingFormControl1_TextboxPurpose_c1', { timeout: 10000 });
    console.log(`LOG: Booking form has loaded after refresh`);
  }

  // Step 3: Wait for booking form to load
  console.log(`LOG: Booking form confirmed loaded`);
  console.log(`LOG: Current URL: ${page.url()}`);

  // Step 3.5: Click the confirm button to force page recognition/initialization
  console.log(`LOG: Clicking confirm button to initialize page...`);
  try {
    await frameContent.locator('a#panel_UIButton2').click({ timeout: 5000 });
    console.log(`LOG: Confirm button clicked, waiting for page to respond...`);
    await frameContent.waitForTimeout(2000);
  } catch (e) {
    console.log(`WARN: Could not click confirm button (might not exist yet): ${e.message}`);
  }

  // Search for the specific start time dropdown by name or ID
  console.log(`LOG: Looking for start time dropdown by name...`);

  // Try multiple selectors to find the dropdowns
  let startTimeDropdown = null;
  let endTimeDropdown = null;
  let foundContext = null;

  // Try by name attribute first in the iframe
  try {
    await frameContent.waitForSelector('select[name="bookingFormControl1$DropDownStartTime_c1"]', { timeout: 5000 });
    startTimeDropdown = frameContent.locator('select[name="bookingFormControl1$DropDownStartTime_c1"]');
    endTimeDropdown = frameContent.locator('select[name="bookingFormControl1$DropDownEndTime_c1"]');
    foundContext = 'iframe (by name)';
    console.log(`LOG: Found dropdowns in iframe by name attribute`);
  } catch (e) {
    console.log(`WARN: Could not find dropdowns by name in iframe, trying by ID...`);

    // Try by ID in iframe
    try {
      await frameContent.waitForSelector('select#bookingFormControl1_DropDownStartTime_c1', { timeout: 3000 });
      startTimeDropdown = frameContent.locator('select#bookingFormControl1_DropDownStartTime_c1');
      endTimeDropdown = frameContent.locator('select#bookingFormControl1_DropDownEndTime_c1');
      foundContext = 'iframe (by ID)';
      console.log(`LOG: Found dropdowns in iframe by ID`);
    } catch (e2) {
      console.log(`WARN: Could not find in iframe, trying main page context...`);

      // Try in main page context (not iframe)
      try {
        await page.waitForSelector('select[name="bookingFormControl1$DropDownStartTime_c1"]', { timeout: 3000 });
        startTimeDropdown = page.locator('select[name="bookingFormControl1$DropDownStartTime_c1"]');
        endTimeDropdown = page.locator('select[name="bookingFormControl1$DropDownEndTime_c1"]');
        foundContext = 'main page (by name)';
        console.log(`LOG: Found dropdowns in MAIN PAGE (not iframe) by name`);
      } catch (e3) {
        console.log(`ERROR: Could not find time dropdowns anywhere`);

        // Debug: list all select elements in both contexts
        console.log(`\n=== DEBUG: Select elements in IFRAME ===`);
        const iframeSelects = await frameContent.locator('select').all();
        console.log(`Found ${iframeSelects.length} select elements in iframe:`);
        for (let i = 0; i < iframeSelects.length; i++) {
          const name = await iframeSelects[i].getAttribute('name');
          const id = await iframeSelects[i].getAttribute('id');
          console.log(`  [${i}] name="${name}", id="${id}"`);
        }

        console.log(`\n=== DEBUG: Select elements in MAIN PAGE ===`);
        const pageSelects = await page.locator('select').all();
        console.log(`Found ${pageSelects.length} select elements in main page:`);
        for (let i = 0; i < pageSelects.length; i++) {
          const name = await pageSelects[i].getAttribute('name');
          const id = await pageSelects[i].getAttribute('id');
          console.log(`  [${i}] name="${name}", id="${id}"`);
        }

        await page.screenshot({ path: './log/screenshot/debug_booking_form.png', fullPage: true });
        throw new Error(`Could not find start/end time dropdowns on booking form`);
      }
    }
  }

  console.log(`LOG: Using dropdowns from: ${foundContext}`);

  // Step 4: Select start time
  console.log(`LOG: Setting start time to ${desiredStartTime}`);
  await startTimeDropdown.selectOption(desiredStartTime);
  await frameContent.waitForTimeout(500);

  // Step 5: Select end time
  console.log(`LOG: Setting end time to ${desiredEndTime}`);
  await endTimeDropdown.selectOption(desiredEndTime);
  await frameContent.waitForTimeout(500);

  // Step 6: Fill in booking purpose
  console.log(`LOG: Filling booking purpose: "${purpose}"`);
  await frameContent.locator('input#bookingFormControl1_TextboxPurpose_c1').fill(purpose);
  await frameContent.waitForTimeout(500);

  // Step 6: Select "Meeting" from usage dropdown
  console.log(`LOG: Selecting "Meeting" from usage dropdown`);
  await frameContent.selectOption('select#bookingFormControl1_DropDownSpaceBookingUsage_c1', 'Meeting');
  await frameContent.waitForTimeout(500);

  // Step 7: Add co-booker
  console.log(`LOG: Opening co-booker dialog`);
  await frameContent.locator('a#bookingFormControl1_GridCoBookers_ctl14').click();
  await frameContent.waitForTimeout(2000);

  // Step 8: Search for co-booker
  console.log(`LOG: Searching for co-booker: "${cobookerName}"`);
  await frameContent.locator('input.textbox.watermark').fill(cobookerName);
  await frameContent.waitForTimeout(500);

  await frameContent.locator('a#bookingFormControl1_DialogSearchCoBooker_searchPanel_buttonSearch').click();
  await frameContent.waitForTimeout(2000);

  // Step 9: Find and click the checkbox (first click - selection context)
  console.log(`LOG: Selecting co-booker from search results`);
  const checkboxes = await frameContent.locator('input[type="checkbox"]').all();

  if (checkboxes.length === 0) {
    throw new Error(`No co-booker found with name "${cobookerName}"`);
  }

  // Click the first checkbox and store its ID
  const checkbox = checkboxes[0];
  const checkboxId = await checkbox.getAttribute('id');
  console.log(`LOG: Found checkbox with ID: ${checkboxId}`);

  await checkbox.click();
  await frameContent.waitForTimeout(1000);

  // Step 10: Click confirm button in dialog
  console.log(`LOG: Confirming co-booker selection`);
  await frameContent.locator('a#bookingFormControl1_DialogSearchCoBooker_dialogBox_b1').click();
  await frameContent.waitForTimeout(2000);

  // Step 11: Click the checkbox again (second click - confirmation context)
  console.log(`LOG: Confirming co-booker in main form`);
  await frameContent.locator(`input#${checkboxId}`).click();
  await frameContent.waitForTimeout(1000);

  // Step 12: Accept terms and conditions
  console.log(`LOG: Accepting terms and conditions`);
  await frameContent.locator('input#bookingFormControl1_TermsAndConditionsCheckbox_c1').click();
  await frameContent.waitForTimeout(500);

  // Step 13: Submit booking
  console.log(`LOG: Submitting booking...`);
  await frameContent.locator('a#panel_UIButton2').click();
  await page.waitForLoadState('networkidle');
  await frameContent.waitForTimeout(3000);

  // Step 14: Take screenshot as confirmation
  console.log(`LOG: Taking confirmation screenshot`);
  const screenshotPath = `./log/screenshot/booking_confirmation_${Date.now()}.png`;
  await page.screenshot({ path: screenshotPath, fullPage: true });
  console.log(`✅ Booking confirmation screenshot saved: ${screenshotPath}`);

  return {
    success: true,
    room: roomName,
    start_time: desiredStartTime,
    end_time: desiredEndTime,
    screenshot: screenshotPath
  };
}

//
// --- CONFIGURATION ---
//

const EMAIL = requireEnv('SMU_EMAIL');
const PASSWORD = requireEnv('SMU_PASSWORD');
const SCRAPE_CONFIG = {
  date: requireEnv('SCRAPE_DATE'),
  startTime: requireEnv('SCRAPE_START_TIME'),
  endTime: requireEnv('SCRAPE_END_TIME'),
  roomCapacity: requireEnv('SCRAPE_ROOM_CAPACITY'),
  buildingNames: requireEnv('SCRAPE_BUILDING_NAMES')
    ? requireEnv('SCRAPE_BUILDING_NAMES').split(',').map(s => s.trim())
    : [],
  floorNames: requireEnv('SCRAPE_FLOOR_NAMES')
    ? requireEnv('SCRAPE_FLOOR_NAMES').split(',').map(s => s.trim())
    : [],
  facilityTypes: requireEnv('SCRAPE_FACILITY_TYPES')
    ? requireEnv('SCRAPE_FACILITY_TYPES').split(',').map(s => s.trim())
    : [],
  equipment: requireEnv('SCRAPE_EQUIPMENT')
    ? requireEnv('SCRAPE_EQUIPMENT').split(',').map(s => s.trim())
    : [],
};

const BOOKING_CONFIG = {
  desiredStartTime: requireEnv('BOOKING_DESIRED_START_TIME'),
  desiredEndTime: requireEnv('BOOKING_DESIRED_END_TIME'),
  purpose: requireEnv('BOOKING_PURPOSE'),
  cobookerName: requireEnv('BOOKING_COBOOKER_NAME'),
};

const url = "https://www.smubondue.com/facility-booking-system-fbs";
const outputLog = './log/bookings_log.json';

//
// --- MAIN SCRIPT ---
//

(async () => {
  const startTime = Date.now();
  let browser;

  try {
    browser = await chromium.launch({ headless: false }); // Set to false for visibility during testing
    const context = await browser.newContext();
    const page = await context.newPage();

    // ============================================
    // AUTHENTICATION (Same as scraper-prod.js)
    // ============================================

    console.log(`LOG: Navigating to ${url}`);
    await page.goto(url, {
      waitUntil: 'domcontentloaded',
      timeout: 60000
    });
    console.log(`LOG: Successfully loaded ${url}`);

    // Open Microsoft login in new tab
    const [newPage] = await Promise.all([
      context.waitForEvent('page', { timeout: 30000 }),
      page.click('a[aria-label="SMU FBS"]'),
    ]);

    // Wait for Microsoft login URL to appear
    await newPage.waitForURL(/login\.microsoftonline\.com/, { timeout: 30000 });
    await newPage.waitForSelector('input[type="email"], #i0116', { timeout: 30000 });
    console.log(`LOG: Navigating to ${newPage.url()}`);

    // Fill email and proceed
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

    // Wait for SMU redirect or click fallback
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

    // Wait for password input, fill in password
    await newPage.waitForSelector('input#passwordInput', { timeout: 30000 });
    const passwordInput = await newPage.$('input#passwordInput');
    if (!passwordInput) throw new Error('ERROR: Password input not found');
    await passwordInput.fill(PASSWORD);
    console.log(`LOG: Filled in password`);

    // Find and click the submit button
    await newPage.waitForSelector('div#submissionArea span#submitButton', { timeout: 30000 });
    const submitButton = await newPage.$('div#submissionArea span#submitButton');
    if (!submitButton) throw new Error('ERROR: Submit button not found');
    await Promise.all([
      submitButton.click(),
      newPage.waitForLoadState('networkidle')
    ]);
    console.log(`LOG: Clicked submit button`);

    // Wait for dashboard and validate correct site
    await newPage.waitForURL(/https:\/\/fbs\.intranet\.smu\.edu\.sg\//, { timeout: 30000 });

    const finalUrl = newPage.url();
    const fbsPage = newPage;
    console.log(`LOG: Arrived at dashboard at url ${finalUrl}`);

    // ============================================
    // SCRAPING (Same as scraper-prod.js)
    // ============================================

    // Switch to core frame
    await fbsPage.waitForSelector('iframe#frameBottom', { timeout: 20000 });
    const frameBottomElement = await fbsPage.$('iframe#frameBottom');
    if (!frameBottomElement) throw new Error('iframe#frameBottom not found');
    const frameBottom = await frameBottomElement.contentFrame();
    if (!frameBottom) throw new Error('Frame object for frameBottom not available');
    console.log(`LOG: Content frame bottom loaded`);

    // Switch to core content frame
    await frameBottom.waitForSelector('iframe#frameContent', { timeout: 20000 });
    const frameContentElement = await frameBottom.$('iframe#frameContent');
    if (!frameContentElement) throw new Error('iframe#frameContent not found inside frameBottom');
    const frameContent = await frameContentElement.contentFrame();
    if (!frameContent) throw new Error('Frame object for frameContent not available');
    console.log(`LOG: Core content frame loaded`);

    // Wait for and set the date picker
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

    // Set start and end time dropdowns
    await frameContent.selectOption('select#TimeFrom_c1_ctl04', SCRAPE_CONFIG.startTime);
    await frameContent.selectOption('select#TimeTo_c1_ctl04', SCRAPE_CONFIG.endTime);
    console.log(`LOG: Set start and end time dropdowns to ${SCRAPE_CONFIG.startTime} and ${SCRAPE_CONFIG.endTime}`);
    await frameContent.waitForTimeout(3000);

    // Set building(s)
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

    // Set floor(s)
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

    // Set facility type(s)
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

    // Set room capacity
    await frameContent.locator('select#DropCapacity_c1').selectOption({ value: SCRAPE_CONFIG.roomCapacity });
    console.log(`LOG: Set room capacity to ${SCRAPE_CONFIG.roomCapacity}`);
    await frameContent.waitForTimeout(3000);

    // Set equipment (optional)
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

    // Retrieve available rooms
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

    // Click "Check Availability"
    await frameContent.locator('a#CheckAvailability').click();
    await fbsPage.waitForLoadState('networkidle');
    console.log(`LOG: Clicked "Check Availability" button`);

    // Navigate to results page
    await frameContent.waitForTimeout(10000);

    // Scrape time slots
    const eventDivs = await frameContent.locator('div.scheduler_bluewhite_event.scheduler_bluewhite_event_line0').all();
    let rawBookings = [];
    for (const slotDiv of eventDivs) {
      const timeslotInfo = await slotDiv.getAttribute('title');
      rawBookings.push(timeslotInfo);
    }
    console.log(`LOG: Found ${rawBookings.length} timeslots`);

    // Map rooms to timeslots
    mapping = mapTimeslotsToRooms(matchingRooms, rawBookings);
    console.log(`LOG: Mapped rooms to timeslots`);

    // Transform to enhanced format
    const rooms = [];
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

    console.log(`✅ Scraping complete. Found ${rooms.length} rooms.`);

    // ============================================
    // BOOKING MATCHING & AUTOMATION
    // ============================================

    console.log(`\n🔍 Searching for room matching booking criteria...`);
    console.log(`   Desired time: ${BOOKING_CONFIG.desiredStartTime} - ${BOOKING_CONFIG.desiredEndTime}`);

    const match = findMatchingRoom(rooms, BOOKING_CONFIG.desiredStartTime, BOOKING_CONFIG.desiredEndTime);

    if (!match) {
      console.log(`\n⚠️  No rooms available for the desired time slot.`);

      // Write notification log
      const notificationLog = {
        metadata: {
          version: "4.0.0",
          timestamp: new Date().toISOString(),
          duration_ms: Date.now() - startTime,
          success: false,
          message: "No matching rooms found"
        },
        booking_config: BOOKING_CONFIG,
        scrape_config: SCRAPE_CONFIG,
        bookings: []
      };

      fs.writeFileSync(outputLog, JSON.stringify(notificationLog, null, 2));
      console.log(`📝 Notification log written to: ${outputLog}`);

      if (browser) await browser.close();
      return;
    }

    // Perform the booking
    const bookingResult = await performBooking(
      frameContent,
      fbsPage,
      match.room.name,
      BOOKING_CONFIG.desiredStartTime,
      BOOKING_CONFIG.desiredEndTime,
      BOOKING_CONFIG.purpose,
      BOOKING_CONFIG.cobookerName
    );

    // Write success log
    const successLog = {
      metadata: {
        version: "4.0.0",
        timestamp: new Date().toISOString(),
        duration_ms: Date.now() - startTime,
        success: true,
        message: "Booking completed successfully"
      },
      booking_config: BOOKING_CONFIG,
      scrape_config: SCRAPE_CONFIG,
      bookings: [{
        ...bookingResult,
        timestamp: new Date().toISOString()
      }]
    };

    fs.writeFileSync(outputLog, JSON.stringify(successLog, null, 2));
    console.log(`\n✅ Booking complete! Log written to: ${outputLog}`);

    if (browser) await browser.close();

  } catch (error) {
    console.error('\n❌ Booking failed:', error.message);
    console.error(error.stack);

    // Write error log
    const errorLog = {
      metadata: {
        version: "4.0.0",
        timestamp: new Date().toISOString(),
        duration_ms: Date.now() - startTime,
        success: false,
        message: error.message,
        error_stack: error.stack
      },
      booking_config: BOOKING_CONFIG,
      scrape_config: SCRAPE_CONFIG,
      bookings: []
    };

    fs.writeFileSync(outputLog, JSON.stringify(errorLog, null, 2));
    console.log(`📝 Error log written to: ${outputLog}`);

    if (browser) await browser.close();
    process.exit(1);
  }
})();
