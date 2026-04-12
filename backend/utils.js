function toMinutes(timeStr) { // "HH:MM" -> total minutes
  const [h, m] = timeStr.split(":").map(Number);
  return h * 60 + m;
}
function minutesToTimeStr(minutes) { // total minutes -> "HH:MM"
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
  const buildingCode = roomName.split('-')[0] || "";
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
      if (start <= currentMinutes && currentMinutes < end) {
        isAvailableNow = true;
      }
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
      slots.push({ timeslot: timeRangeStr, status: "not available due to timeslot", details: "", startMin, endMin });
    } else if (ts.startsWith("Booking Time:")) {
      const bookingTime = extractBookingTime(ts);
      if (!bookingTime) throw new Error(`Cannot extract booking time from: ${ts}`);
      const [startMin, endMin] = parseTimeRange(bookingTime);
      slots.push({ timeslot: bookingTime, status: "booked", details: ts, startMin, endMin });
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
      fullSlots.push({ start: freeStart, end: freeEnd, status: "free" });
    }
    const [slotStart, slotEnd] = slot.timeslot.split("-");
    const normalized = normalizeStatus(slot.status);
    const timeslotObj = { start: slotStart, end: slotEnd, status: normalized };
    if (normalized === "unavailable") {
      timeslotObj.reason = "Outside scrape window";
    } else if (normalized === "booked") {
      const booking = parseBookingDetails(slot.details);
      if (booking) timeslotObj.booking = booking;
    }
    fullSlots.push(timeslotObj);
    cursor = slot.endMin;
  }
  if (cursor < DAY_END) {
    fullSlots.push({ start: minutesToTimeStr(cursor), end: minutesToTimeStr(DAY_END), status: "free" });
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
      if (currentRoomIndex >= roomCount) throw new Error("More timeslot blocks than rooms");
      result[rawRooms[currentRoomIndex]] = generateTimeslotsForRoom(acc);
      currentRoomIndex++;
      acc = [];
    }
    acc.push(ts);
  }
  if (acc.length > 0) {
    if (currentRoomIndex >= roomCount) throw new Error("More timeslot blocks than rooms");
    result[rawRooms[currentRoomIndex]] = generateTimeslotsForRoom(acc);
  }
  while (currentRoomIndex + 1 < roomCount) {
    currentRoomIndex++;
    result[rawRooms[currentRoomIndex]] = [{ timeslot: "00:00-24:00", status: "free", details: "" }];
  }
  return result;
}

module.exports = {
  toMinutes,
  minutesToTimeStr,
  timeslotStr,
  getTodayDate,
  parseTimeRange,
  extractBookingTime,
  parseBookingDetails,
  extractRoomMetadata,
  normalizeStatus,
  calculateAvailabilitySummary,
  generateTimeslotsForRoom,
  mapTimeslotsToRooms,
};
