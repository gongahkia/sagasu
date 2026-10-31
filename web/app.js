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
  const formatDate = date => new Intl.DateTimeFormat("en-SG", { weekday: "short", day: "numeric", month: "short", timeZone: "Asia/Singapore" }).format(new Date(date + "T12:00:00+08:00"));
  const formatStamp = value => new Intl.DateTimeFormat("en-SG", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Singapore" }).format(new Date(value));
  let snapshot = null;
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
      empty("A little time adjustment", "Your session must end after it starts."); return;
    }
    const day = snapshot.days.find(item => item.date === $("date").value);
    if (!day) return;
    const rooms = filterRooms(day, start, end, $("room").value);
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
      card.append(top, title, time, foot); return card;
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
      $("freshness").textContent = "Waiting for the first availability snapshot";
      $("status-dot").classList.add("warn");
      $("update-detail").textContent = status && status.status !== "never" ? `${status.message} All times in Singapore (SGT).` : "No successful scrape has been published yet. All times in Singapore (SGT).";
      empty("The noticeboard is getting ready", "Availability will appear after the first successful daily refresh. You can still use SMU FBS or set up your own Telegram bot."); return;
    }
    snapshot = dataResult.value;
    const stale = isStale(snapshot);
    const failed = status && !["ok", "never"].includes(status.status);
    $("freshness").textContent = stale ? "This snapshot is over 36 hours old" : failed ? "Latest refresh needs attention" : "Your daily campus snapshot";
    $("status-dot").classList.toggle("warn", stale || Boolean(failed));
    $("update-detail").textContent = `Last successful update: ${formatStamp(snapshot.updated_at)} SGT. ` + (failed ? `${status.message} ` : "") + (status && status.attempted_at ? `Last attempt: ${formatStamp(status.attempted_at)} SGT.` : "Updated daily.");
    $("date").replaceChildren(...snapshot.days.map(day => {
      const option = document.createElement("option"); option.value = day.date; option.textContent = formatDate(day.date); return option;
    }));
    $("date").disabled = false;
    render();
  }
  load().catch(() => {
    $("freshness").textContent = "Availability could not be loaded";
    $("status-dot").classList.add("warn");
    $("update-detail").textContent = "Please refresh or check SMU FBS directly. All times in Singapore (SGT).";
    empty("The noticeboard is unavailable", "Please try again later or check SMU FBS directly.");
  });
})(typeof globalThis === "undefined" ? this : globalThis);
