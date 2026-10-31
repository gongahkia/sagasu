/* No credentials, booking endpoints, or live FBS requests exist in this client. */
(function (root) {
  "use strict";
  function minutes(time) {
    if (!/^\d{2}:\d{2}$/.test(time)) return NaN;
    const [h, m] = time.split(":").map(Number);
    return h < 24 && m < 60 ? h * 60 + m : NaN;
  }
  function isFree(room, start, end) {
    const a = minutes(start), b = minutes(end);
    return a < b && room.timeslots.some(slot => slot.status === "free" && minutes(slot.start) <= a && minutes(slot.end) >= b);
  }
  function filterRooms(day, start, end, query) {
    return day.rooms.filter(room => room.name.toLowerCase().includes(query.trim().toLowerCase()) && isFree(room, start, end));
  }
  function isStale(snapshot, now = Date.now()) {
    const stamp = Date.parse(snapshot.updated_at);
    return !Number.isFinite(stamp) || now - stamp > 36 * 3600000;
  }
  const helpers = { minutes, isFree, filterRooms, isStale };
  if (typeof module !== "undefined") module.exports = helpers;
  root.Sagasu = helpers;
  if (typeof document === "undefined") return;

  const $ = id => document.getElementById(id);
  function randomizeSearchExample() {
    const locations = ["SOA", "LKCSB", "SCIS1", "SOE"];
    const dates = ["today", "tomorrow", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
    const times = ["9-11am", "10am-12pm", "2-4pm", "4-6pm", "7-9pm", "from 2pm for 2 hours"];
    const equipment = ["", " with a projector", " with a whiteboard", " with a screen", " with power outlets"];
    const examples = locations.flatMap(location => dates.flatMap(date => times.flatMap(time => equipment.map(extra => `${location} ${date} ${time}${extra}`))));
    const storageKey = "sagasu:last-search-example";
    let previous = $("search-query").placeholder;
    try { previous = localStorage.getItem(storageKey) || previous; } catch (_) { /* Storage may be disabled. */ }
    const candidates = examples.filter(example => example !== previous);
    const example = candidates[Math.floor(Math.random() * candidates.length)];
    $("search-query").placeholder = example;
    try { localStorage.setItem(storageKey, example); } catch (_) { /* The example still works without persistence. */ }
  }
  randomizeSearchExample();
  const formatDate = date => new Intl.DateTimeFormat("en-SG", { weekday: "short", day: "numeric", month: "short", timeZone: "Asia/Singapore" }).format(new Date(date + "T12:00:00+08:00"));
  const formatStamp = value => new Intl.DateTimeFormat("en-SG", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Singapore" }).format(new Date(value));
  let snapshot = null;
  let lastMapRooms = [];
  const mapData = root.SagasuMapData;
  function updateFloor() {
    const key = $("floor-select").value;
    window.dispatchEvent(new CustomEvent("sagasu:floor", { detail: { key, rooms: lastMapRooms, selectedRoom, focusRequest, showOtherFloors: $("show-other-floors").checked, showExterior: $("show-exterior").checked } }));
    const imageLink = $("floor-image-link");
    if (!key || !mapData.floorPlans[key]) {
      imageLink.hidden = true;
      $("model-reference").replaceChildren();
      $("floor-summary").textContent = "Choose a floor to compare the plan with available rooms.";
      return;
    }
    const [building, floor] = key.split(":");
    const sources = {
      'LKCSB': ['Business school exterior', 'https://news.smu.edu.sg/news/2022/07/18/training-helps-entrepreneurs-achieve-72-increase-revenue-growth-new-research-finds'],
      'SOA': ['Accountancy building exterior', 'https://accountancy.smu.edu.sg/news/smu-school-accountancy-takes-asias-top-spot-byu-accounting-research-rankings-2019'],
      'SCIS1': ['Campus architect model and plans', 'https://www.knta-architects.com/smu'],
      'SOE/SCIS2': ['Economics building exterior', 'https://commons.wikimedia.org/wiki/File:SchoolofEconomics-SchoolofSocialSciences-SingaporeManagementUniversity-20070127.jpg'],
      'LKS': ['Library exterior and architecture', 'https://library.smu.edu.sg/about-us/overview'],
    };
    const reference = $("model-reference"); reference.replaceChildren();
    if (sources[building]) { const link = document.createElement("a"); link.href = sources[building][1]; link.textContent = `Exterior reference: ${sources[building][0]}`; link.target = "_blank"; link.rel = "noopener noreferrer"; reference.append(link); }
    const matches = lastMapRooms.filter(room => mapData.floorFor(room.name) === key);
    imageLink.hidden = true;
    imageLink.href = mapData.floorPlans[key];
    const image = $("floor-image");
    image.alt = `${building} level ${floor} floor plan`;
    image.onload = () => { if ($("floor-select").value === key && image.currentSrc === imageLink.href) imageLink.hidden = false; };
    image.onerror = () => { if ($("floor-select").value === key) imageLink.hidden = true; };
    image.src = mapData.floorPlans[key];
    $("floor-summary").textContent = building === "LKS" ? `Library project room availability is not included in the current GSR snapshot. Rooms on the plan: ${(root.SagasuFloorGeometry?.library?.[key]?.rooms || []).map(room => `RM${floor}-${room[0]}`).join(", ")}.` : matches.length ? `Available on ${building} level ${floor}: ${matches.map(room => room.name).join(", ")}` : `No matching available rooms on ${building} level ${floor} for the current filters.`;
  }
  if (mapData) {
    const buildingSelect = $("building-select"), floorSelect = $("floor-select");
    for (const building of [...mapData.buildings, {id:"LKS", name:"Li Ka Shing Library"}]) {
      const option = document.createElement("option"); option.value = building.id; option.textContent = building.name; buildingSelect.append(option);
    }
    buildingSelect.addEventListener("change", () => {
      const keys = Object.keys(mapData.floorPlans).filter(key => key.split(":")[0] === buildingSelect.value).sort((a,b) => Number(a.split(":")[1])-Number(b.split(":")[1]));
      floorSelect.replaceChildren(...keys.map(key => { const option = document.createElement("option"); option.value = key; option.textContent = `Level ${key.split(":")[1]}`; return option; }));
      floorSelect.disabled = !keys.length;
      if (!keys.length) { const option = document.createElement("option"); option.value=""; option.textContent="Choose a building first"; floorSelect.append(option); }
      updateFloor();
    });
    floorSelect.addEventListener("change", updateFloor);
    $("show-other-floors").addEventListener("change", updateFloor);
    $("show-exterior").addEventListener("change", updateFloor);
  }
  function updateMap(rooms) {
    if (!mapData) return;
    lastMapRooms = rooms;
    updateFloor();
    const counts = mapData.counts(rooms);
    $("map-unmapped").hidden = !counts.unmapped;
    $("map-unmapped").textContent = `${counts.unmapped} available room(s) could not be assigned to a building; they remain in the results.`;
  }
  window.addEventListener("sagasu:floor-ready", updateFloor);
  let selectedRoom = null, focusRequest = 0;
  function showTimeline() {
    const day = snapshot.days.find(day => day.date === $("timeline-date").value);
    const room = day?.rooms.find(room => room.name === selectedRoom);
    $("room-timeline").replaceChildren(); $("room-intervals").replaceChildren();
    if (!room) { $("room-timeline").textContent = "No room data for this date."; return; }
    for (const slot of room.timeslots) {
      const segment = document.createElement("span"); segment.className = `timeline-slot ${slot.status}`;
      segment.style.flexGrow = String(minutes(slot.end) - minutes(slot.start));
      segment.title = `${slot.start}–${slot.end}: ${slot.status}`;
      segment.setAttribute("aria-label", segment.title); $("room-timeline").append(segment);
      const item = document.createElement("li"); item.textContent = segment.title; item.className = slot.status; $("room-intervals").append(item);
    }
  }
  function openRoom(room, date) {
    selectedRoom = room.name; focusRequest++; $("room-title").textContent = room.name;
    $("timeline-date").replaceChildren(...snapshot.days.map(day => { const option=document.createElement("option");option.value=day.date;option.textContent=formatDate(day.date);return option; }));
    $("timeline-date").value = date; showTimeline(); $("room-dialog").showModal();
    const building=mapData?.buildingFor(room.name), floor=mapData?.floorFor(room.name);
    if (building && floor && mapData.floorPlans[floor]) {
      $("building-select").value=building.id; $("building-select").dispatchEvent(new Event("change"));
      $("floor-select").value=floor; updateFloor();
    } else { $("building-select").value=""; $("building-select").dispatchEvent(new Event("change")); $("floor-3d-message").textContent="This room has no mapped 3D position."; }

  }
  $("timeline-date").addEventListener("change", showTimeline);
  $("close-room").addEventListener("click", () => $("room-dialog").close());
  $("search-form").addEventListener("submit", event => {
    event.preventDefault(); if (!snapshot) return;
    document.body.classList.add("has-results"); $("search-output").hidden=false;
    const today = new Intl.DateTimeFormat("en-CA", {timeZone:"Asia/Singapore",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
    const parsed = root.SagasuSearch.parseSearch($("search-query").value, snapshot.days.map(day=>day.date), today);
    $("search-warning").hidden = !parsed.warnings.length;
    $("search-warning").textContent = parsed.warnings.join(" ");
    $("search-summary").textContent = `${parsed.date} · ${parsed.start}–${parsed.end} SGT · ${parsed.query || "All buildings"}${parsed.defaultTime ? " · Default time" : ""}`;
    if (parsed.error) { $("filter-error").hidden=false; $("filter-error").textContent=parsed.error; $("results-count").textContent="Adjust your search"; empty("Couldn't interpret that search",parsed.error);updateMap([]);return; }
    $("date").value=parsed.date; $("start").value=parsed.start; $("end").value=parsed.end;
    // Natural queries may use minute values between the half-hour options.
    for (const [id,value] of [["start",parsed.start],["end",parsed.end]]) if (!$(id).value) {const option=document.createElement("option");option.value=value;option.textContent=value;$(id).append(option);$(id).value=value;}
    $("room").value=parsed.query; render();
  });
  function empty(title, message) {
    const panel = document.createElement("div"); panel.className = "empty";
    const icon = document.createElement("span"); icon.textContent = "◷"; icon.setAttribute("aria-hidden", "true");
    const heading = document.createElement("h3"); heading.textContent = title;
    const text = document.createElement("p"); text.textContent = message;
    panel.append(icon, heading, text); $("results").replaceChildren(panel);
  }
  function render() {
    if (!snapshot) return;
    const start = $("start").value, end = $("end").value;
    const error = $("filter-error"); error.hidden = true;
    if (!(minutes(start) < minutes(end))) {
      error.textContent = "Choose an end time after the start time."; error.hidden = false;
      $("results-count").textContent = "Check your session times";
      updateMap([]);
      empty("A little time adjustment", "Your session must end after it starts."); return;
    }
    const day = snapshot.days.find(item => item.date === $("date").value);
    if (!day) return;
    const rooms = filterRooms(day, start, end, $("room").value);
    updateMap(rooms);
    $("results-count").textContent = `${rooms.length} room${rooms.length === 1 ? "" : "s"} for your session`;
    if (!rooms.length) {
      empty("No rooms for this session", day.rooms.length ? "Try a different time, date, or room name. These are saved results; check FBS for current availability." : "FBS returned no matching group study rooms for this date. Try another date or check FBS directly."); return;
    }
    const cards = rooms.map((room, index) => {
      const card = document.createElement("article"); card.className = "room-card";
      const top = document.createElement("div"); top.className = "card-top";
      const number = document.createElement("span"); number.textContent = String(index + 1).padStart(2, "0") + " / STUDY SPACE";
      const available = document.createElement("span"); available.className = "free-label"; available.textContent = "● AVAILABLE";
      top.append(number, available);
      const title = document.createElement("h3"); title.textContent = room.name;
      const time = document.createElement("p"); time.textContent = `${formatDate(day.date)} · ${start}–${end} SGT`;
      const foot = document.createElement("div"); foot.className = "card-foot"; foot.textContent = "Snapshot availability · Book through SMU FBS";
      const plan = document.createElement("a");
      plan.href = "https://paulchensc.blogspot.com/2011/06/smu-gsr-and-sr-location-plan.html";
      plan.target = "_blank"; plan.rel = "noopener noreferrer";
      plan.textContent = "View GSR floor plans (2011)";
      foot.append(document.createElement("br"), plan);
      const details = document.createElement("button"); details.type="button"; details.className="room-details"; details.textContent="View availability timeline";
      details.setAttribute("aria-label", `View ${room.name} availability timeline`);details.addEventListener("click",()=>openRoom(room,day.date));
      card.addEventListener("click", event => { if (!event.target.closest("button")) openRoom(room, day.date); });
      card.append(top, title, time, details); return card;
    });
    $("results").replaceChildren(...cards);
  }
  for (let value = 8 * 60; value <= 22 * 60; value += 30) {
    const time = `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
    for (const id of ["start", "end"]) {
      const option = document.createElement("option"); option.value = time; option.textContent = time; $(id).append(option);
    }
  }
  $("start").value = "10:00"; $("end").value = "12:00";
  $("filters").addEventListener("submit", event => { event.preventDefault(); render(); });
  for (const id of ["date", "start", "end"]) $(id).addEventListener("change", render);
  $("room").addEventListener("input", render);
  async function fetchData(path) {
    const response = await fetch(path, { cache: "no-cache" });
    if (!response.ok) throw new Error("Data unavailable");
    return response.json();
  }
  async function load() {
    const [dataResult, statusResult] = await Promise.allSettled([fetchData("data/snapshot.json"), fetchData("data/status.json")]);
    const status = statusResult.status === "fulfilled" ? statusResult.value : null;
    if (dataResult.status !== "fulfilled" || !dataResult.value || dataResult.value.version !== 1 || !Array.isArray(dataResult.value.days) || !dataResult.value.days.length) {
      $("freshness").textContent = status?.attempted_at ? `Last attempt at ${formatStamp(status.attempted_at)} SGT` : "Waiting for the first availability snapshot";
      $("status-dot").classList.add("warn");
      $("update-detail").textContent = status && status.status !== "never" ? `${status.message} All times in Singapore (SGT).` : "No successful scrape has been published yet. All times in Singapore (SGT).";
      empty("The noticeboard is getting ready", "Availability will appear after the first successful daily refresh. You can still use SMU FBS or set up your own Telegram bot."); return;
    }
    snapshot = dataResult.value;
    const stale = isStale(snapshot);
    const failed = status && !["ok", "never"].includes(status.status);
    $("freshness").textContent = failed && status.attempted_at ? `Last attempt at ${formatStamp(status.attempted_at)} SGT` : `Last updated at ${formatStamp(snapshot.updated_at)} SGT`;
    $("status-dot").classList.toggle("warn", stale || Boolean(failed));
    $("update-detail").textContent = `Last successful update: ${formatStamp(snapshot.updated_at)} SGT. ` + (stale ? "This snapshot is over 36 hours old. " : "") + (failed ? `${status.message} ` : "") + (status && status.attempted_at ? `Last attempt: ${formatStamp(status.attempted_at)} SGT.` : "Updated daily.");
    $("date").replaceChildren(...snapshot.days.map(day => {
      const option = document.createElement("option"); option.value = day.date; option.textContent = formatDate(day.date); return option;
    }));
    $("date").disabled = false;
    $("search-submit").disabled = false;
    $("search-summary").textContent = `${$("date").value} · 10:00–12:00 SGT · All buildings`;
    render();
  }
  load().catch(() => {
    $("freshness").textContent = "Availability could not be loaded";
    $("status-dot").classList.add("warn");
    $("update-detail").textContent = "Please refresh or check SMU FBS directly. All times in Singapore (SGT).";
    empty("The noticeboard is unavailable", "Please try again later or check SMU FBS directly.");
  });
})(typeof globalThis === "undefined" ? this : globalThis);
