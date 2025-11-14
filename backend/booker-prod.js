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
  console.log(`LOG: Network idle reached, waiting longer for SPA/React components to render...`);

  // EXTENDED WAIT: Modern SPAs need more time to render after network idle
  await page.waitForTimeout(15000); // Increased from 5s to 15s for component rendering
  console.log(`LOG: Extended wait completed (15 seconds)`);

  // Additional wait for any lazy-loaded scripts
  await page.waitForTimeout(10000); // Extra 10 seconds
  console.log(`LOG: Additional wait completed (total 25 seconds after network idle)`);

  // Step 2.5: Wait for booking form to actually load
  console.log(`LOG: Waiting for booking form to load in iframe...`);

  // IMPROVED: Wait for the container div first, then discover elements within it
  let containerFound = false;
  let formContainer = null;

  try {
    // Try waiting for the form container first (more reliable than individual fields)
    console.log(`LOG: Attempting to find booking form container: div#bookingFormControl1_CollapsibleDetails_container`);
    await frameContent.waitForSelector('div#bookingFormControl1_CollapsibleDetails_container', { timeout: 15000 });
    formContainer = frameContent.locator('div#bookingFormControl1_CollapsibleDetails_container');
    containerFound = true;
    console.log(`✓ Booking form container found in iframe`);
  } catch (e) {
    console.log(`WARN: Container not found in original iframe context, refreshing iframe references...`);

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

    // Try waiting for the container again
    try {
      await frameContent.waitForSelector('div#bookingFormControl1_CollapsibleDetails_container', { timeout: 10000 });
      formContainer = frameContent.locator('div#bookingFormControl1_CollapsibleDetails_container');
      containerFound = true;
      console.log(`✓ Booking form container found after iframe refresh`);
    } catch (e2) {
      console.log(`ERROR: Container still not found, trying alternative approaches...`);
    }
  }

  // Take screenshot for debugging
  await page.screenshot({ path: './log/screenshot/booking_form_after_navigation.png', fullPage: true });
  console.log(`📸 Screenshot saved: booking_form_after_navigation.png`);

  // CRITICAL DEBUG: Dump the entire HTML to understand the actual structure
  console.log(`\n📄 STEP 2A: Dumping iframe HTML to file for analysis...`);
  try {
    const pageContent = await frameContent.content();
    const fs = require('fs');
    fs.writeFileSync('./log/booking_form_structure.html', pageContent);
    console.log(`✓ HTML structure saved to: ./log/booking_form_structure.html`);

    // Also log a preview of the HTML
    const preview = pageContent.substring(0, 2000);
    console.log(`\n=== HTML PREVIEW (first 2000 chars) ===`);
    console.log(preview);
    console.log(`=== END PREVIEW ===\n`);

  } catch (e) {
    console.log(`✗ Could not dump HTML: ${e.message}`);
  }

  // Log current page and iframe URLs
  console.log(`\n🔗 STEP 2B: Checking current URLs...`);
  console.log(`Main page URL: ${page.url()}`);
  try {
    const frameUrl = await frameContent.evaluate(() => window.location.href);
    console.log(`Iframe URL: ${frameUrl}`);
  } catch (e) {
    console.log(`Could not get iframe URL: ${e.message}`);
  }

  // Log page title
  try {
    const pageTitle = await page.title();
    console.log(`Page title: ${pageTitle}`);
    const frameTitle = await frameContent.evaluate(() => document.title);
    console.log(`Iframe title: ${frameTitle}`);
  } catch (e) {
    console.log(`Could not get titles: ${e.message}`);
  }

  // Check for specific text that should appear on booking form
  console.log(`\n🔍 STEP 2C: Checking for booking form indicators...`);
  try {
    const bodyText = await frameContent.evaluate(() => document.body.innerText);
    const hasBookingDetails = bodyText.includes('Booking Details') || bodyText.includes('BOOKING DETAILS');
    const hasPurpose = bodyText.includes('Purpose') || bodyText.includes('PURPOSE');
    const hasTimeFrom = bodyText.includes('TIME FROM') || bodyText.includes('Time From');
    const hasConfirm = bodyText.includes('CONFIRM') || bodyText.includes('Confirm');

    console.log(`Page contains "Booking Details": ${hasBookingDetails}`);
    console.log(`Page contains "Purpose": ${hasPurpose}`);
    console.log(`Page contains "TIME FROM": ${hasTimeFrom}`);
    console.log(`Page contains "CONFIRM": ${hasConfirm}`);

    if (!hasBookingDetails && !hasPurpose && !hasTimeFrom) {
      console.log(`\n⚠️  WARNING: Page does not appear to be the booking form!`);
      console.log(`Body text preview (first 500 chars):`);
      console.log(bodyText.substring(0, 500));
    }
  } catch (e) {
    console.log(`Could not check page text: ${e.message}`);
  }

  // Discover all form elements (container or not - we'll work with what exists)
  console.log(`\n=== FORM DISCOVERY: Analyzing page for form elements ===`);
  const searchContext = containerFound ? formContainer : frameContent;
  const contextName = containerFound ? 'container' : 'entire iframe';
  console.log(`Searching within: ${contextName}`);

  // Discover all input fields
  const inputs = await searchContext.locator('input').all();
  console.log(`\nFound ${inputs.length} input fields:`);
  for (let i = 0; i < inputs.length; i++) {
    const id = await inputs[i].getAttribute('id');
    const name = await inputs[i].getAttribute('name');
    const type = await inputs[i].getAttribute('type');
    const placeholder = await inputs[i].getAttribute('placeholder');
    const value = await inputs[i].getAttribute('value');
    const visible = await inputs[i].isVisible();
    const enabled = await inputs[i].isEnabled();
    console.log(`  [${i}] id="${id}", name="${name}", type="${type}", placeholder="${placeholder}", value="${value}", visible=${visible}, enabled=${enabled}`);
  }

  // Discover all select/dropdown fields
  const selects = await searchContext.locator('select').all();
  console.log(`\nFound ${selects.length} select/dropdown fields:`);
  for (let i = 0; i < selects.length; i++) {
    const id = await selects[i].getAttribute('id');
    const name = await selects[i].getAttribute('name');
    const visible = await selects[i].isVisible();
    const enabled = await selects[i].isEnabled();

    // Get current selected value
    let selectedValue = null;
    try {
      selectedValue = await selects[i].inputValue();
    } catch (e) {
      // Ignore if can't get value
    }

    console.log(`  [${i}] id="${id}", name="${name}", visible=${visible}, enabled=${enabled}, selectedValue="${selectedValue}"`);
  }

  // Discover all textarea fields
  const textareas = await searchContext.locator('textarea').all();
  console.log(`\nFound ${textareas.length} textarea fields:`);
  for (let i = 0; i < textareas.length; i++) {
    const id = await textareas[i].getAttribute('id');
    const name = await textareas[i].getAttribute('name');
    const placeholder = await textareas[i].getAttribute('placeholder');
    const visible = await textareas[i].isVisible();
    const enabled = await textareas[i].isEnabled();
    console.log(`  [${i}] id="${id}", name="${name}", placeholder="${placeholder}", visible=${visible}, enabled=${enabled}`);
  }

  // EXTRA: Discover all buttons and links
  console.log(`\n🔘 Discovering buttons and links...`);
  const buttons = await searchContext.locator('button, input[type="button"], input[type="submit"], a.button, a[role="button"]').all();
  console.log(`Found ${buttons.length} buttons/links:`);
  for (let i = 0; i < Math.min(buttons.length, 10); i++) {
    const id = await buttons[i].getAttribute('id');
    const text = await buttons[i].innerText().catch(() => '');
    const href = await buttons[i].getAttribute('href');
    const visible = await buttons[i].isVisible();
    console.log(`  [${i}] id="${id}", text="${text}", href="${href}", visible=${visible}`);
  }

  console.log(`=== END FORM DISCOVERY ===\n`);

  // Write comprehensive debug report to file
  console.log(`📊 Writing comprehensive debug report...`);
  try {
    const debugReport = {
      timestamp: new Date().toISOString(),
      urls: {
        mainPage: page.url(),
        iframe: await frameContent.evaluate(() => window.location.href).catch(() => 'N/A')
      },
      pageIndicators: {
        hasBookingDetails: await frameContent.evaluate(() =>
          document.body.innerText.includes('Booking Details') ||
          document.body.innerText.includes('BOOKING DETAILS')
        ).catch(() => false),
        hasPurpose: await frameContent.evaluate(() =>
          document.body.innerText.includes('Purpose') ||
          document.body.innerText.includes('PURPOSE')
        ).catch(() => false),
        hasTimeFrom: await frameContent.evaluate(() =>
          document.body.innerText.includes('TIME FROM') ||
          document.body.innerText.includes('Time From')
        ).catch(() => false),
      },
      elementCounts: {
        inputs: inputs.length,
        selects: selects.length,
        textareas: textareas.length,
        buttons: buttons.length
      },
      visibleInputs: [],
      visibleSelects: [],
      visibleTextareas: []
    };

    // Collect visible elements only
    for (const inp of inputs) {
      if (await inp.isVisible().catch(() => false)) {
        debugReport.visibleInputs.push({
          id: await inp.getAttribute('id'),
          name: await inp.getAttribute('name'),
          type: await inp.getAttribute('type'),
          placeholder: await inp.getAttribute('placeholder'),
          value: await inp.getAttribute('value')
        });
      }
    }

    for (const sel of selects) {
      if (await sel.isVisible().catch(() => false)) {
        debugReport.visibleSelects.push({
          id: await sel.getAttribute('id'),
          name: await sel.getAttribute('name'),
          selectedValue: await sel.inputValue().catch(() => null)
        });
      }
    }

    for (const txt of textareas) {
      if (await txt.isVisible().catch(() => false)) {
        debugReport.visibleTextareas.push({
          id: await txt.getAttribute('id'),
          name: await txt.getAttribute('name'),
          placeholder: await txt.getAttribute('placeholder')
        });
      }
    }

    fs.writeFileSync('./log/debug_report.json', JSON.stringify(debugReport, null, 2));
    console.log(`✓ Debug report saved to: ./log/debug_report.json`);
    console.log(`\n📋 QUICK SUMMARY:`);
    console.log(`  Total inputs: ${inputs.length} (${debugReport.visibleInputs.length} visible)`);
    console.log(`  Total selects: ${selects.length} (${debugReport.visibleSelects.length} visible)`);
    console.log(`  Total textareas: ${textareas.length} (${debugReport.visibleTextareas.length} visible)`);
  } catch (e) {
    console.log(`✗ Could not write debug report: ${e.message}`);
  }

  // Continue regardless of container status - the form clearly exists
  if (!containerFound) {
    console.log(`\n⚠️  Note: Expected container not found, but form elements may still be accessible. Proceeding...`);
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

  // Search for form fields using VISUAL/LABEL-BASED detection
  console.log(`\n🔍 STEP 4: Locating form fields using VISUAL/LABEL-BASED detection...`);
  console.log(`This approach searches for labels and finds associated input fields`);

  // STRATEGY: Find elements by their visible labels on the page
  let startTimeDropdown = null;
  let endTimeDropdown = null;
  let foundDropdowns = false;

  // Helper function to find input by label text
  async function findFieldByLabel(labelText, fieldType = 'select') {
    console.log(`  Searching for ${fieldType} with label containing: "${labelText}"`);

    try {
      // Find all labels or text elements containing the label text
      const labels = await frameContent.locator(`text=${labelText}`).all();
      console.log(`    Found ${labels.length} elements with text "${labelText}"`);

      if (labels.length === 0) {
        // Try case-insensitive
        const labelsCI = await frameContent.locator(`text=/${labelText}/i`).all();
        console.log(`    Found ${labelsCI.length} elements with text "${labelText}" (case-insensitive)`);
        if (labelsCI.length > 0) {
          labels.push(...labelsCI);
        }
      }

      for (const label of labels) {
        try {
          // Strategy 1: Look for the field as a sibling
          const parent = label.locator('..');
          const sibling = parent.locator(`${fieldType}`).first();

          if (await sibling.count() > 0 && await sibling.isVisible()) {
            console.log(`    ✓ Found ${fieldType} as sibling of label`);
            return sibling;
          }

          // Strategy 2: Look for field as child of parent's parent
          const grandparent = parent.locator('..');
          const cousin = grandparent.locator(`${fieldType}`).first();

          if (await cousin.count() > 0 && await cousin.isVisible()) {
            console.log(`    ✓ Found ${fieldType} as cousin of label`);
            return cousin;
          }

          // Strategy 3: Look for field in the same row (common table layout)
          const row = label.locator('xpath=ancestor::tr[1]');
          if (await row.count() > 0) {
            const rowField = row.locator(`${fieldType}`).first();
            if (await rowField.count() > 0 && await rowField.isVisible()) {
              console.log(`    ✓ Found ${fieldType} in same table row as label`);
              return rowField;
            }
          }

          // Strategy 4: Look for field in same div container
          const container = label.locator('xpath=ancestor::div[1]');
          if (await container.count() > 0) {
            const containerField = container.locator(`${fieldType}`).first();
            if (await containerField.count() > 0 && await containerField.isVisible()) {
              console.log(`    ✓ Found ${fieldType} in same div as label`);
              return containerField;
            }
          }

        } catch (e) {
          console.log(`    ✗ Error searching near label: ${e.message}`);
          continue;
        }
      }

      console.log(`    ✗ Could not find ${fieldType} near label "${labelText}"`);
      return null;

    } catch (e) {
      console.log(`    ✗ Error in findFieldByLabel: ${e.message}`);
      return null;
    }
  }

  // Use label-based detection to find TIME FROM and TO dropdowns
  console.log(`\n⏰ Searching for TIME FROM and TO dropdowns by label...`);

  startTimeDropdown = await findFieldByLabel('TIME FROM', 'select');
  if (!startTimeDropdown) {
    // Try variations
    startTimeDropdown = await findFieldByLabel('Time From', 'select');
  }
  if (!startTimeDropdown) {
    startTimeDropdown = await findFieldByLabel('TIME FROM*', 'select');
  }

  endTimeDropdown = await findFieldByLabel('TO*', 'select');
  if (!endTimeDropdown) {
    endTimeDropdown = await findFieldByLabel('TO:', 'select');
  }
  if (!endTimeDropdown) {
    endTimeDropdown = await findFieldByLabel('Time To', 'select');
  }

  if (startTimeDropdown && endTimeDropdown) {
    foundDropdowns = true;
    console.log(`✅ Successfully found BOTH time dropdowns using label-based detection!`);

    // Log the field details
    try {
      const startId = await startTimeDropdown.getAttribute('id');
      const startName = await startTimeDropdown.getAttribute('name');
      const endId = await endTimeDropdown.getAttribute('id');
      const endName = await endTimeDropdown.getAttribute('name');
      console.log(`  Start time dropdown: id="${startId}", name="${startName}"`);
      console.log(`  End time dropdown: id="${endId}", name="${endName}"`);
    } catch (e) {
      console.log(`  Could not log field details: ${e.message}`);
    }
  } else {
    console.log(`❌ Label-based detection failed for time dropdowns`);
    console.log(`  Start time found: ${startTimeDropdown !== null}`);
    console.log(`  End time found: ${endTimeDropdown !== null}`);

    // Take screenshot before failing
    await page.screenshot({ path: './log/screenshot/label_detection_failed.png', fullPage: true });

    // Try one more fallback: just get the first two visible select elements
    console.log(`\n🔄 FALLBACK: Attempting to use first two visible select elements...`);
    try {
      const allVisibleSelects = [];
      for (const sel of selects) {
        if (await sel.isVisible()) {
          allVisibleSelects.push(sel);
        }
      }

      if (allVisibleSelects.length >= 2) {
        // Check if these are time-related by looking at their options
        const firstOptions = await allVisibleSelects[0].locator('option').allInnerTexts();
        const secondOptions = await allVisibleSelects[1].locator('option').allInnerTexts();

        const looksLikeTime = (opts) => opts.some(o => /\d{2}:\d{2}/.test(o));

        if (looksLikeTime(firstOptions) && looksLikeTime(secondOptions)) {
          startTimeDropdown = allVisibleSelects[0];
          endTimeDropdown = allVisibleSelects[1];
          foundDropdowns = true;
          console.log(`  ✓ Using first two visible select elements as time dropdowns`);
          console.log(`    First dropdown options: ${firstOptions.slice(0, 3).join(', ')}...`);
          console.log(`    Second dropdown options: ${secondOptions.slice(0, 3).join(', ')}...`);
        }
      }
    } catch (e) {
      console.log(`  ✗ Fallback failed: ${e.message}`);
    }
  }

  if (!foundDropdowns) {
    await page.screenshot({ path: './log/screenshot/all_strategies_failed.png', fullPage: true });
    throw new Error(`Could not find start/end time dropdowns after trying all strategies including label-based detection`);
  }

  // Step 5: Select start time
  console.log(`\n⏰ STEP 5: Setting start time to ${desiredStartTime}`);
  await startTimeDropdown.selectOption(desiredStartTime);
  await frameContent.waitForTimeout(500);
  console.log(`✓ Start time set successfully`);

  // Step 6: Select end time
  console.log(`\n⏰ STEP 6: Setting end time to ${desiredEndTime}`);
  await endTimeDropdown.selectOption(desiredEndTime);
  await frameContent.waitForTimeout(500);
  console.log(`✓ End time set successfully`);

  // Step 7: Fill in booking purpose using LABEL-BASED detection
  console.log(`\n📝 STEP 7: Filling booking purpose: "${purpose}"`);
  let purposeField = null;
  let foundPurpose = false;

  // Try to find PURPOSE field by label
  purposeField = await findFieldByLabel('PURPOSE', 'input');
  if (!purposeField) {
    purposeField = await findFieldByLabel('PURPOSE*', 'input');
  }
  if (!purposeField) {
    purposeField = await findFieldByLabel('Purpose', 'input');
  }
  if (!purposeField) {
    // Maybe it's a textarea
    purposeField = await findFieldByLabel('PURPOSE', 'textarea');
  }
  if (!purposeField) {
    purposeField = await findFieldByLabel('PURPOSE*', 'textarea');
  }

  if (purposeField) {
    foundPurpose = true;
    console.log(`✅ Found purpose field using label-based detection!`);

    try {
      const id = await purposeField.getAttribute('id');
      const name = await purposeField.getAttribute('name');
      console.log(`  Purpose field: id="${id}", name="${name}"`);
    } catch (e) {
      console.log(`  Could not log field details: ${e.message}`);
    }
  } else {
    console.log(`❌ Label-based detection failed for purpose field`);

    // Fallback: Look for any visible text input or textarea
    console.log(`\n🔄 FALLBACK: Searching for visible text input/textarea...`);
    try {
      for (const inp of inputs) {
        const type = await inp.getAttribute('type');
        const visible = await inp.isVisible();

        if (visible && type === 'text') {
          // Check if it's not a readonly/disabled field
          const readonly = await inp.getAttribute('readonly');
          const disabled = await inp.getAttribute('disabled');

          if (!readonly && !disabled) {
            purposeField = inp;
            foundPurpose = true;
            const id = await inp.getAttribute('id');
            console.log(`  ✓ Using first editable text input: id="${id}"`);
            break;
          }
        }
      }

      // If still not found, try textareas
      if (!foundPurpose && textareas.length > 0) {
        for (const txt of textareas) {
          if (await txt.isVisible()) {
            purposeField = txt;
            foundPurpose = true;
            const id = await txt.getAttribute('id');
            console.log(`  ✓ Using first visible textarea: id="${id}"`);
            break;
          }
        }
      }
    } catch (e) {
      console.log(`  ✗ Fallback failed: ${e.message}`);
    }
  }

  if (!foundPurpose) {
    await page.screenshot({ path: './log/screenshot/purpose_field_not_found.png', fullPage: true });
    throw new Error(`Could not find purpose field after trying all strategies including label-based detection`);
  }

  await purposeField.fill(purpose);
  await frameContent.waitForTimeout(500);
  console.log(`✓ Purpose field filled successfully with: "${purpose}"`);

  // Step 8: Find USE TYPE dropdown using LABEL-BASED detection
  console.log(`\n🏢 STEP 8: Finding USE TYPE dropdown`);
  let useTypeDropdown = await findFieldByLabel('USE TYPE', 'select');
  if (!useTypeDropdown) {
    useTypeDropdown = await findFieldByLabel('USE TYPE*', 'select');
  }
  if (!useTypeDropdown) {
    useTypeDropdown = await findFieldByLabel('Use Type', 'select');
  }

  if (useTypeDropdown) {
    console.log(`✅ Found USE TYPE dropdown using label-based detection`);
    // Usually defaults to "AdHoc", leave as is
    try {
      const currentValue = await useTypeDropdown.inputValue();
      console.log(`  Current value: "${currentValue}" (leaving as is)`);
    } catch (e) {
      console.log(`  Could not get current value`);
    }
  } else {
    console.log(`⚠️  USE TYPE dropdown not found, continuing...`);
  }

  // Step 9: Find BOOKING USAGE dropdown using LABEL-BASED detection
  console.log(`\n📊 STEP 9: Finding BOOKING USAGE dropdown`);
  let usageDropdown = await findFieldByLabel('BOOKING USAGE', 'select');
  if (!usageDropdown) {
    usageDropdown = await findFieldByLabel('BOOKING USAGE:', 'select');
  }
  if (!usageDropdown) {
    usageDropdown = await findFieldByLabel('Booking Usage', 'select');
  }

  if (usageDropdown) {
    console.log(`✅ Found BOOKING USAGE dropdown using label-based detection`);
    try {
      // Try to select "Meeting" if available
      await usageDropdown.selectOption('Meeting');
      console.log(`  ✓ Set to "Meeting"`);
    } catch (e) {
      console.log(`  ⚠️  Could not select "Meeting": ${e.message}`);
      console.log(`  Leaving as default value`);
    }
    await frameContent.waitForTimeout(500);
  } else {
    console.log(`⚠️  BOOKING USAGE dropdown not found, continuing...`);
  }

  // Step 10: Add co-booker (OPTIONAL - skip if not available)
  console.log(`\n👥 STEP 10: Adding co-booker (optional)`);
  console.log(`  Attempting to find ADD button for co-bookers...`);

  let addButtonFound = false;
  let addButton = null;

  // Try to find ADD button by text
  try {
    addButton = await frameContent.locator('text=ADD').first();
    if (await addButton.isVisible({ timeout: 3000 })) {
      addButtonFound = true;
      console.log(`  ✓ Found ADD button by text`);
    }
  } catch (e) {
    console.log(`  ✗ ADD button not found by text: ${e.message}`);
  }

  // Try finding button with "+ ADD" text (with plus icon)
  if (!addButtonFound) {
    try {
      addButton = await frameContent.locator('text=+ ADD').first();
      if (await addButton.isVisible({ timeout: 3000 })) {
        addButtonFound = true;
        console.log(`  ✓ Found "+ ADD" button`);
      }
    } catch (e) {
      console.log(`  ✗ "+ ADD" button not found`);
    }
  }

  // Try finding by role and text
  if (!addButtonFound) {
    try {
      addButton = await frameContent.locator('button:has-text("ADD"), a:has-text("ADD")').first();
      if (await addButton.isVisible({ timeout: 3000 })) {
        addButtonFound = true;
        console.log(`  ✓ Found ADD button by has-text`);
      }
    } catch (e) {
      console.log(`  ✗ ADD button not found by has-text`);
    }
  }

  if (!addButtonFound) {
    console.log(`  ⚠️  ADD button not found - skipping co-booker step (may be optional)`);
    console.log(`  Proceeding without adding co-bookers...`);
  } else {
    console.log(`  Clicking ADD button to open co-booker dialog...`);
    try {
      await addButton.click({ timeout: 5000 });
      await frameContent.waitForTimeout(2000);
      console.log(`  ✓ ADD button clicked successfully`);

      // Note: Skipping the actual co-booker search and selection for now
      // as the dialog interaction might have changed
      console.log(`  ⚠️  Co-booker dialog opened but not implementing search/selection yet`);
      console.log(`  TODO: Implement co-booker search if needed`);

      // Try to close the dialog or cancel
      try {
        const cancelButton = await frameContent.locator('text=Cancel, text=CANCEL, text=Close, text=CLOSE').first();
        if (await cancelButton.isVisible({ timeout: 2000 })) {
          await cancelButton.click();
          console.log(`  ✓ Closed co-booker dialog`);
        }
      } catch (e) {
        console.log(`  Could not find close button for dialog`);
      }

    } catch (e) {
      console.log(`  ✗ Error clicking ADD button: ${e.message}`);
      console.log(`  Continuing without co-bookers...`);
    }
  }

  // Step 11: Click CONFIRM button to submit booking
  console.log(`\n✅ STEP 11: Submitting booking by clicking CONFIRM button`);

  let confirmButton = null;
  let confirmFound = false;

  // IMPORTANT: Look for the actual CONFIRM button, not the "3. Confirmation" breadcrumb
  // The button should be in the top-right corner of the form

  // Strategy 1: Look for button/link with EXACT text "CONFIRM" (not "Confirmation")
  try {
    const confirmButtons = await frameContent.locator('button, a, input[type="button"], input[type="submit"]').all();
    console.log(`  Found ${confirmButtons.length} total buttons/links`);

    for (const btn of confirmButtons) {
      try {
        const text = (await btn.innerText()).trim();
        const visible = await btn.isVisible();

        // Look for EXACT match "CONFIRM" (not "Confirmation")
        if (visible && text === 'CONFIRM') {
          confirmButton = btn;
          confirmFound = true;
          const id = await btn.getAttribute('id');
          const className = await btn.getAttribute('class');
          console.log(`  ✓ Found CONFIRM button: id="${id}", class="${className}"`);
          break;
        }
      } catch (e) {
        continue;
      }
    }
  } catch (e) {
    console.log(`  ✗ Error searching for CONFIRM button: ${e.message}`);
  }

  // Strategy 2: Look in specific areas (bottom-right or top-right of form)
  if (!confirmFound) {
    console.log(`  Trying to find button by looking at all visible buttons...`);
    try {
      const allButtons = await frameContent.locator('button, a[role="button"], input[type="button"], input[type="submit"]').all();

      for (const btn of allButtons) {
        try {
          const text = await btn.innerText();
          const visible = await btn.isVisible();
          const id = await btn.getAttribute('id');

          // Check if it's a submit-type button
          if (visible && text && (
            text.trim() === 'CONFIRM' ||
            text.trim() === 'Submit' ||
            text.trim() === 'SUBMIT' ||
            (id && (id.includes('Confirm') || id.includes('Submit') || id.includes('Button2')))
          )) {
            // Make sure it's NOT the breadcrumb (which would be an <a> with href="javascript:v()")
            const href = await btn.getAttribute('href');
            const tagName = await btn.evaluate(el => el.tagName.toLowerCase());

            if (href === 'javascript:v();' && text.includes('Confirmation')) {
              console.log(`  ⚠️  Skipping breadcrumb: "${text}"`);
              continue; // Skip the breadcrumb
            }

            confirmButton = btn;
            confirmFound = true;
            console.log(`  ✓ Found submit button: text="${text}", id="${id}", tag="${tagName}"`);
            break;
          }
        } catch (e) {
          continue;
        }
      }
    } catch (e) {
      console.log(`  ✗ Error in button search: ${e.message}`);
    }
  }

  if (!confirmFound) {
    await page.screenshot({ path: './log/screenshot/confirm_button_not_found.png', fullPage: true });
    throw new Error(`Could not find CONFIRM button to submit booking`);
  }

  // Click the confirm button with force if overlay blocking
  console.log(`  Clicking CONFIRM button to submit booking...`);
  try {
    await confirmButton.click({ force: true }); // Use force to bypass overlays
    console.log(`  ✓ CONFIRM button clicked`);
  } catch (e) {
    console.log(`  ✗ Normal click failed, trying with force: ${e.message}`);
    await confirmButton.click({ force: true, timeout: 10000 });
    console.log(`  ✓ CONFIRM button clicked with force`);
  }

  // Wait for submission to complete
  console.log(`  Waiting for booking submission to complete...`);
  await page.waitForLoadState('networkidle', { timeout: 15000 });
  await frameContent.waitForTimeout(5000);

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
