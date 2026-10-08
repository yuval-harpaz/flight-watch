/* flight-watch: code shared by flights.html and flight_map.html (and run under node by
   tests/test_pages.py). The board logic mirrors flight_watch.Schedule.load and the geometry
   flight_watch's helpers; keep them in step - the test compares both on the same rows. */
(function (root) {
"use strict";
const FB = {};

FB.BOARD_URL = "https://data.gov.il/api/3/action/datastore_search";
FB.RESOURCE_URL = "https://data.gov.il/api/3/action/resource_show";
FB.RESOURCE = "e83f763b-b7d7-479e-b172-ae981ddc6de5";
FB.FIELDS = "CHOPER,CHOPERD,CHFLTN,CHSTOL,CHPTOL,CHAORD,CHLOC1,CHLOC1D,CHLOC1T,CHLOCCT,CHTERM,CHCINT,CHCKZN,CHRMINE";
FB.HOME = {iata: "TLV", icao: "LLBG", name: "Ben Gurion", lat: 32.0114, lon: 34.8867};
FB.TZ = "Asia/Jerusalem";
FB.STANDING = "https://raw.githubusercontent.com/vradarserver/standing-data/main";
FB.DELAY_MIN = 15;     // minutes behind schedule that count as delayed
FB.OVERDUE_MIN = 20;   // minutes past the estimate without landing / departing
// IATA airline code -> ICAO callsign prefix: a copy of flight_watch.AIRLINE_ICAO (kept equal by the test)
FB.AIRLINE_ICAO = {"4Y":"OCN","5C":"ICL","5F":"FIA","6E":"IGO","6H":"ISR","9U":"MLD","A3":"AEE","AA":"AAL","AC":"ACA","AF":"AFR","AI":"AIC","AM":"AMX","AR":"ARG","AZ":"ITY","B2":"BRU","BA":"BAW","BT":"BTI","BY":"TOM","BZ":"BBG","CA":"CCA","CY":"CYP","DE":"CFG","DL":"DAL","DS":"EZS","EC":"EJU","EK":"UAE","EN":"DLA","ET":"ETH","EW":"EWG","EY":"ETD","FB":"LZB","FR":"RYR","FZ":"FDB","GF":"GFA","GQ":"SEH","HU":"CHH","HV":"TRA","HY":"UZB","IB":"IBE","IZ":"AIZ","J2":"AHY","JU":"ASL","KC":"KZR","KL":"KLM","LH":"DLH","LO":"LOT","LS":"EXS","LX":"SWR","LY":"ELY","MS":"MSR","MU":"CES","OA":"OAL","OS":"AUA","OU":"CTN","PC":"PGT","PS":"AUI","QF":"QFA","QS":"TVS","RJ":"RJA","RO":"ROT","SK":"SAS","SN":"BEL","SU":"AFL","TG":"THA","TK":"THY","TO":"TVF","TP":"TAP","U2":"EZY","UA":"UAL","UL":"ALK","UX":"AEA","VN":"HVN","VS":"VIR","VY":"VLG","W4":"WMT","W6":"WZZ","W9":"WUK","WZ":"RWZ","XC":"CAI","XQ":"SXS"};

// ------------------------------------------------------------------ time
function zoneParts(ms, tz) {
  const o = {timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
             hour: "2-digit", minute: "2-digit", second: "2-digit"};
  return Object.fromEntries(new Intl.DateTimeFormat("en-GB", o).formatToParts(new Date(ms))
    .filter(p => p.type !== "literal").map(p => [p.type, +p.value]));
}
function offsetMs(ms, tz) {
  const p = zoneParts(ms, tz);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(ms / 1000) * 1000;
}
/** Board time ("2026-10-08T10:30:00", Israel local) -> epoch seconds, like Python's
 *  datetime.fromisoformat(s).replace(tzinfo=ZoneInfo("Asia/Jerusalem")).timestamp(). */
FB.israelEpoch = function (s) {
  const m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?/.exec(String(s || ""));
  if (!m) return NaN;
  const naive = Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  // As Python's fold=0: a time that occurs twice (end of summer time) is the first one, a time
  // skipped (start of summer time) is read with the offset before the change.
  const before = offsetMs(naive - 14 * 3600e3, FB.TZ), after = offsetMs(naive + 14 * 3600e3, FB.TZ);
  const fits = o => offsetMs(naive - o, FB.TZ) === o;
  const o = fits(before) ? before : fits(after) ? after : before;
  return (naive - o) / 1000;
};
/** Format epoch seconds in a zone ("Asia/Jerusalem", "UTC" or "local"). style: hm, dhm, hms, date, z */
FB.fmt = function (t, zone, style) {
  if (!isFinite(t)) return "";
  const tz = zone === "local" ? Intl.DateTimeFormat().resolvedOptions().timeZone : (zone || FB.TZ);
  const p = zoneParts(t * 1000, tz), two = n => String(n).padStart(2, "0");
  const hm = two(p.hour) + ":" + two(p.minute);
  if (style === "hms") return hm + ":" + two(p.second);
  if (style === "date") return p.year + "-" + two(p.month) + "-" + two(p.day);
  if (style === "dhm") return two(p.day) + "/" + two(p.month) + " " + hm;
  if (style === "z") {
    const off = Math.round(offsetMs(t * 1000, tz) / 60000);
    return off === 0 ? "UTC" : "UTC" + (off > 0 ? "+" : "-") + Math.floor(Math.abs(off) / 60) +
      (Math.abs(off) % 60 ? ":" + two(Math.abs(off) % 60) : "");
  }
  return hm;
};

// ------------------------------------------------------------------ board
FB.fetchBoard = async function (fetchFn) {
  const f = fetchFn || fetch, records = [];
  for (let page = 0; page < 20; page++) {
    const u = FB.BOARD_URL + "?resource_id=" + FB.RESOURCE + "&fields=" + FB.FIELDS +
              "&limit=3000&offset=" + records.length;
    const r = await f(u);
    if (!r.ok) throw new Error("flight board: HTTP " + r.status);
    const d = (await r.json()).result;
    records.push(...d.records);
    if (!d.records.length || records.length >= +(d.total || 0)) break;
  }
  return records;
};
// History: "גרסאות לעם" (over.org.il) keeps every version of the board since 10 Apr 2026
// (about every 15 min) as an append-only table of row states with the time each was first seen.
FB.HISTORY_SQL = "https://www.over.org.il/api/append/31c812a6-9b0c-4f32-8317-e5f268c28f60/datastore_search_sql";
FB.HISTORY_TABLE = "append_flydata_31c812a6";
FB.HISTORY_START = Date.parse("2026-04-10T22:10:00Z") / 1000;
/** SQL for the board as it was at epoch `t`: per flight (airline, number, direction, scheduled
 *  time) the last row state first seen by then, for flights scheduled from a day before to three
 *  days after (what the live board holds). Rows dropped from the board are not marked in the
 *  archive, so the window stands in for the board's own trimming. */
FB.snapshotSQL = function (t, limit, offset) {
  const local = x => FB.fmt(x, FB.TZ, "date") + "T" + FB.fmt(x, FB.TZ, "hms");
  const cols = FB.FIELDS.split(",").map(c => '"' + c + '"').join(",");
  const key = '"CHOPER","CHFLTN","CHAORD","CHSTOL"';
  return `SELECT DISTINCT ON (${key}) ${cols},first_seen FROM "${FB.HISTORY_TABLE}" ` +
    `WHERE first_seen <= '${new Date(t * 1000).toISOString()}' AND "CHSTOL" >= '${local(t - 86400)}' ` +
    `AND "CHSTOL" < '${local(t + 3 * 86400)}' ORDER BY ${key},first_seen DESC LIMIT ${limit} OFFSET ${offset}`;
};
FB.fetchBoardAt = async function (t, fetchFn) {
  if (!(t >= FB.HISTORY_START)) throw new Error("the archive starts on 10 Apr 2026");
  const f = fetchFn || fetch, records = [], page = 1000;  // the archive returns at most 1000 rows
  for (let i = 0; i < 20; i++) {
    const url = FB.HISTORY_SQL + "?sql=" + encodeURIComponent(FB.snapshotSQL(t, page, records.length));
    let rows;
    for (let attempt = 0; ; attempt++) {  // one retry: a page sometimes fails once (~2 s per query)
      try {
        const r = await f(url);
        if (!r.ok) throw new Error("board archive: HTTP " + r.status);
        rows = (await r.json()).result.records;
        break;
      } catch (e) {
        if (attempt) throw e;
        await new Promise(ok => setTimeout(ok, 2000));
      }
    }
    records.push(...rows);
    if (rows.length < page) break;
  }
  return records;
};
FB.boardUpdated = async function (fetchFn) {
  try {
    const r = await (fetchFn || fetch)(FB.RESOURCE_URL + "?id=" + FB.RESOURCE);
    const m = (await r.json()).result.last_modified;   // UTC, without a zone
    return Date.parse(m.replace(/(\.\d{3})\d*$/, "$1") + "Z") / 1000;
  } catch (e) { return NaN; }
};

/** Board rows -> one object per physical flight, as flight_watch.Schedule.load groups them:
 *  rows sharing direction, scheduled time and other airport are one flight (codeshares); the
 *  lowest flight number is the operating carrier, whose callsign the aircraft sends. Unlike the
 *  monitor nothing is filtered out (cancelled, landed, far in time): the page shows all. */
FB.flights = function (records, airlineMap) {
  const map = airlineMap || FB.AIRLINE_ICAO, slots = new Map();
  for (const r of records) {
    const key = [String(r.CHAORD || "").toUpperCase().slice(0, 1), r.CHSTOL, r.CHLOC1].join("|");
    if (!slots.has(key)) slots.set(key, []);
    slots.get(key).push(r);
  }
  const out = [];
  for (const [key, rows] of slots) {
    const r = rows[0];
    const dir = String(r.CHAORD || "").toUpperCase().startsWith("A") ? "ARR" : "DEP";
    const numbers = rows.map(row => {
      const iata = String(row.CHOPER || "").trim().toUpperCase();
      const num = String(row.CHFLTN || "").trim().replace(/^0+/, "") || "0";
      const icao = map[iata] || (iata.length === 3 ? iata : null);
      return {iata, num, icao, airline: String(row.CHOPERD || "").trim()};
    });
    const order = (a, b) => (a.num.length - b.num.length) || (a.num < b.num ? -1 : a.num > b.num ? 1 : 0);
    const mapped = numbers.filter(n => n.icao).sort(order);
    const all = mapped.length ? mapped.concat(numbers.filter(n => !n.icao).sort(order)) : numbers.slice().sort(order);
    const variants = n => n.icao ? [...new Set([n.icao + n.num, n.icao + n.num.padStart(3, "0")])] : [];
    const p = all[0];
    out.push({
      id: key, dir, flight: p.iata + p.num, airline: p.airline || p.iata,
      codeshares: all.slice(1).map(n => n.iata + n.num),
      callsigns: mapped.length ? variants(mapped[0]) : [],
      altCallsigns: mapped.slice(1).flatMap(variants),
      other: String(r.CHLOC1 || "?"), otherName: String(r.CHLOC1D || ""),
      city: String(r.CHLOC1T || r.CHLOC1D || ""), country: String(r.CHLOCCT || ""),
      terminal: r.CHTERM, counters: r.CHCINT || "", zone: r.CHCKZN || "",
      status: String(r.CHRMINE || "").toUpperCase(),
      schedText: String(r.CHSTOL || ""), estText: String(r.CHPTOL || r.CHSTOL || ""),
      sched: FB.israelEpoch(r.CHSTOL), est: FB.israelEpoch(r.CHPTOL || r.CHSTOL),
    });
  }
  return out;
};

/** What a passenger would want to know: {label, cls, delay (min)}. cls: cancel, done, late,
 *  overdue, early, ok, soon. Statuses on the board: ON TIME, NOT FINAL, DELAYED, EARLY, FINAL,
 *  LANDING, LANDED, DEPARTED, CANCELED. */
FB.situation = function (f, now) {
  const delay = isFinite(f.est) && isFinite(f.sched) ? Math.round((f.est - f.sched) / 60) : 0;
  const late = delay >= FB.DELAY_MIN, early = delay <= -10, past = (now - f.est) / 60;
  const by = m => (m > 0 ? "+" : "") + m + " min";
  if (f.status.includes("CANCEL")) return {label: "Cancelled", cls: "cancel", delay};
  if (f.dir === "ARR") {
    if (f.status === "LANDED") return {label: late ? "Landed late " + by(delay) : early ? "Landed early" : "Landed",
                                       cls: late ? "done late" : "done", delay};
    if (f.status === "LANDING") return {label: "Landing now", cls: "soon", delay};
    if (past >= FB.OVERDUE_MIN) return {label: "Not landed, " + Math.round(past) + " min past estimate",
                                        cls: "overdue", delay};
    if (late) return {label: "Delayed landing " + by(delay), cls: "late", delay};
    if (early) return {label: "Early " + by(delay), cls: "early", delay};
    return {label: "On time", cls: "ok", delay};
  }
  if (f.status === "DEPARTED") return {label: late ? "Departed late " + by(delay) : "Departed",
                                       cls: late ? "done late" : "done", delay};
  if (past >= FB.OVERDUE_MIN) return {label: "Late to depart, " + Math.round(past) + " min past estimate",
                                      cls: "overdue", delay};
  if (late) return {label: "Late departure " + by(delay), cls: "late", delay};
  if (f.status === "FINAL") return {label: "Final call", cls: "soon", delay};
  if (early) return {label: "Early " + by(delay), cls: "early", delay};
  return {label: "On time", cls: "ok", delay};
};

// ------------------------------------------------------------------ live feed
// api.adsb.lol sends no CORS headers, so a page on another site cannot read it; served by
// tools/serve.py (http://localhost:8765/...) the pages use its relay on the same origin.
FB.local = function () {
  return typeof location !== "undefined" && /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname);
};
// FB.RELAY: an HTTPS address of tools/serve.py (or any relay of /v2/* and /data/traces/* that
// adds CORS headers), for the pages on GitHub Pages; "" = none, so live data works only locally.
FB.RELAY = "https://flight-watch-relay.yuvharpaz.workers.dev";  // tools/cors_worker.js
FB.feedBase = function () {
  const q = typeof location !== "undefined" ? new URLSearchParams(location.search).get("feed") : null;
  return q ? q.replace(/\/$/, "") : FB.local() ? "" : FB.RELAY || "https://api.adsb.lol";
};
FB.traceBase = function () { return FB.local() ? "" : FB.RELAY || "https://adsb.lol"; };
/** True when the browser could not read the feed at all (CORS refused, offline): fetch throws a
 *  TypeError then, while an HTTP error status is an Error with "HTTP nnn". */
FB.blocked = e => e instanceof TypeError;
/** Is asking the live feed worth it? Not for a cancelled flight, an arrival landed over 30 min
 *  ago, or a flight far from its time; the page then shows the board alone. */
FB.liveUseful = function (f, now) {
  if (/CANCEL/.test(f.status)) return false;
  if (f.dir === "ARR") return f.status === "LANDED" ? now - f.est < 1800 : f.est - now < 16 * 3600 && now - f.est < 6 * 3600;
  if (f.status === "DEPARTED") return now - f.est < 16 * 3600;  // may still be on its way
  return f.est - now < 2 * 3600;                                  // a departure from 2 h before its time
};
FB.getJSON = async function (url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url.split("?")[0] + ": HTTP " + r.status);
  return r.json();
};
/** Aircraft now flying these callsigns ([] if none). Up to ~100 per request. */
FB.byCallsign = async function (callsigns) {
  const out = [];
  for (let i = 0; i < callsigns.length; i += 100) {
    const d = await FB.getJSON(FB.feedBase() + "/v2/callsign/" + callsigns.slice(i, i + 100).join(","));
    out.push(...(d.ac || d.aircraft || []));
  }
  return out;
};
FB.byHex = async function (hex) {
  const d = await FB.getJSON(FB.feedBase() + "/v2/hex/" + hex);
  return {now: d.now ? d.now / 1000 : Date.now() / 1000, ac: (d.ac || d.aircraft || [])[0] || null};
};
/** Today's positions of one aircraft (readsb trace_full; rows [dt, lat, lon, alt, gs, track, ...]). */
FB.trace = async function (hex) {
  const xx = hex.slice(-2);
  const out = [];
  for (const kind of ["full", "recent"]) {
    try {
      const d = await FB.getJSON(FB.traceBase() + "/data/traces/" + xx + "/trace_" + kind + "_" + hex + ".json");
      for (const p of d.trace || []) out.push({t: d.timestamp + p[0], lat: p[1], lon: p[2], alt: p[3]});
    } catch (e) { /* not available: the trail is built from the live positions instead */ }
  }
  out.sort((a, b) => a.t - b.t);
  return out.filter((p, i) => !i || p.t > out[i - 1].t);
};
/** The points of the current leg: after the last time on the ground, or after a gap the aircraft
 *  could not have spent flying (> 10 min at under 200 kt on average): a stop where coverage is
 *  missing, e.g. LY5064 8 Oct 2026, TLV 03:05Z - unseen at Heraklion - next heard 06:41Z inbound. */
FB.currentLeg = function (pts) {
  pts = pts.filter(p => p.lat != null);
  let start = 0;
  pts.forEach((p, i) => {
    const q = pts[i - 1];
    if (p.alt === "ground") start = i + 1;
    else if (q && p.t - q.t > 600 && FB.haversineNm(q.lat, q.lon, p.lat, p.lon) / ((p.t - q.t) / 3600) < 200) start = i;
  });
  return pts.slice(start);
};
FB.onGround = ac => ac && (ac.alt_baro === "ground" || ((ac.gs || 0) < 50 && (+ac.alt_baro || 0) < 500));

// ------------------------------------------------------------------ standing data (routes, airports)
const csvCache = new Map();
async function csvFile(path) {
  if (!csvCache.has(path)) csvCache.set(path, (async () => {
    const rows = new Map();
    try {
      const r = await fetch(FB.STANDING + "/" + path);
      if (r.ok) for (const line of (await r.text()).replace(/^﻿/, "").split(/\r?\n/).slice(1)) {
        const c = line.split(",");
        if (c[0]) rows.set(c[0], c);
      }
    } catch (e) { /* offline: no route data */ }
    return rows;
  })());
  return csvCache.get(path);
}
/** ["LHBP", "LLBG"] for a callsign, as flight_watch.StandingData.route; null when unknown. */
FB.route = async function (callsign) {
  const m = /^([A-Z]{3})([0-9][0-9A-Z]*)$/.exec(callsign || "");
  if (!m) return null;
  for (const name of [m[1] + "-" + m[2][0] + ".csv", m[1] + "-all.csv"]) {
    const rows = await csvFile("routes/schema-01/" + m[1][0] + "/" + name);
    if (rows.size) { const row = rows.get(callsign); return row && row[4] ? row[4].split("-") : null; }
  }
  return null;
};
/** {icao, iata, city, lat, lon} or null. */
FB.airport = async function (icao) {
  if (!/^[A-Z0-9]{4}$/.test(icao || "")) return null;
  const row = (await csvFile("airports/schema-01/" + icao[0] + "/" + icao.slice(0, 2) + ".csv")).get(icao);
  return row ? {icao, iata: row[3], city: row[4], lat: +row[6], lon: +row[7]} : null;
};

// ------------------------------------------------------------------ geometry (as flight_watch)
const R = Math.PI / 180;
FB.haversineNm = function (lat1, lon1, lat2, lon2) {
  const p1 = lat1 * R, p2 = lat2 * R, dp = p2 - p1, dl = (lon2 - lon1) * R;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * 3440.065 * Math.asin(Math.sqrt(a));
};
FB.bearing = function (lat1, lon1, lat2, lon2) {
  const p1 = lat1 * R, p2 = lat2 * R, dl = (lon2 - lon1) * R;
  const b = Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl));
  return ((b / R) % 360 + 360) % 360;
};
FB.destination = function (lat, lon, track, nm) {
  const d = nm / 3440.065, b = track * R, p1 = lat * R, l1 = lon * R;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [p2 / R, l2 / R];
};
/** Great-circle path [[lon, lat], ...] for drawing. */
FB.greatCircle = function (a, b, n) {
  const d = FB.haversineNm(a.lat, a.lon, b.lat, b.lon), brg = FB.bearing(a.lat, a.lon, b.lat, b.lon);
  const out = [], steps = n || 64;
  for (let i = 0; i <= steps; i++) {
    const p = FB.destination(a.lat, a.lon, brg, d * i / steps);  // along the great circle from a
    out.push([p[1], p[0]]);
  }
  return out;
};
FB.KM = 1.852; FB.M = 0.3048;   // nm -> km, ft -> m (pages are metric)

if (typeof module !== "undefined" && module.exports) module.exports = FB; else root.FB = FB;
})(this);
