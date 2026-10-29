/**
 * Cloudflare Worker: a read-only relay of the adsb.lol feed with CORS headers, so the flight
 * pages on GitHub Pages (docs/flight_map.html) can show live data. adsb.lol sends no CORS headers
 * (Oct 2026), and neither does opendata.adsb.fi, so a browser refuses them from another site.
 *
 *   /v2/hex/<hex>[,<hex>...]          -> https://api.adsb.lol/v2/...    (fresh 5 s)
 *   /v2/callsign/<cs>[,<cs>...]       -> https://api.adsb.lol/v2/...    (fresh 5 s)
 *   /data/traces/<xx>/trace_{full,recent}_<hex>.json -> https://adsb.lol/data/traces/... (30 s)
 *   /globe_history/<yyyy>/<mm>/<dd>/traces/<xx>/trace_full_<hex>.json -> https://adsb.lol/... (1 h; a past day)
 *   /adsbx/globe_history/<yyyy>/<mm>/<dd>/traces/<xx>/trace_full_<hex>.json
 *                                     -> https://globe.adsbexchange.com/globe_history/... (10 min; a UTC day of one
 *                                        aircraft, today's too). Its history files answer 403 without ADS-B Exchange's
 *                                        own Referer and send no CORS header. Since 18 Oct 2026 adsb.lol refuses its
 *                                        trace files (a browser check), so the map's tracks come from here.
 *   /adsbx/data/traces/<xx>/trace_{full,recent}_<hex>.json
 *                                     -> https://globe.adsbexchange.com/data/traces/... (30 s; its last ~24 h of one
 *                                        aircraft). Since 29 Oct 2026 its history files answer 403 "Historical data
 *                                        requires a verified session" (a Cloudflare check); these still answer with its
 *                                        Referer.
 *   /hexof/<cs>[,<cs>...]             -> which aircraft (hex) flew each callsign in the last 36 h
 *   /alerts                           -> the monitor's alerts of the last 24 h (the kinds Telegram gets) and the time
 *                                        of its last /learn, for the flight board's "show warnings" (hidden by default)
 *
 * Nothing else is relayed (not an open proxy), only GET, only for the origins below. Answers are
 * cached: many viewers of one flight cost one upstream request per 5 s, and when adsb.lol answers
 * 429 (it limits Cloudflare's shared addresses) the last answer of up to 2 min is served instead.
 *
 * The callsign map (/hexof) keeps callsign -> [hex, last seen] for 36 h, in one KV value. It is filled
 * through POST /learn with "Authorization: Bearer <LEARN_TOKEN>": every 10 min by the GitHub workflow
 * .github/workflows/share_callsigns.yml (tools/share_callsigns.py, secret RELAY_TOKEN), and also by
 * the monitor (flight_watch.py) while it runs with RELAY_TOKEN set. adsb.lol answers 429 to Cloudflare, so the
 * optional Cron Trigger (one /v2/point request around TLV every 5 min) failed every time (8 Oct 2026). The board does
 * not name the aircraft, and once a flight has landed the live feed no longer finds it by callsign;
 * with the hex the map page draws the flown track of a landed flight. Nothing else is stored.
 *
 * Deploy: dash.cloudflare.com -> Workers & Pages -> Create -> Worker -> "Hello World" -> Edit code,
 * paste this file, Deploy. Then set FB.RELAY in docs/flightboard.js to the worker's address.
 * For /hexof: Storage & Databases -> KV -> Create namespace (e.g. "flight-watch-callsigns");
 * the worker -> Settings -> Bindings -> Add -> KV namespace, variable name CALLSIGNS;
 * Settings -> Variables and Secrets -> Add -> type Secret, name LEARN_TOKEN, a long random value (the
 * same value as RELAY_TOKEN for the monitor). A Cron Trigger is optional (expression below).
 * Without these the relay still works and /hexof answers {}. /status shows what is set.
 * Free plan: 100,000 requests a day; KV 1,000 writes a day (288 for pushes every 5 min, 288 more
 * with the Cron Trigger: remove it when the monitor pushes) and 100,000 reads.
 */
// Cron Trigger expression: */5 * * * *
const ORIGINS = [/^https:\/\/yuval-harpaz\.github\.io$/, /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/];
const ROUTES = [  // [path pattern, upstream host, fresh seconds, prefix left out upstream, request headers]
  [/^\/v2\/(hex|callsign)\/[0-9A-Za-z~,]{1,1000}$/, "https://api.adsb.lol", 5],
  [/^\/data\/traces\/[0-9a-f]{2}\/trace_(full|recent)_~?[0-9a-f]{6}\.json$/, "https://adsb.lol", 30],
  [/^\/globe_history\/\d{4}\/\d\d\/\d\d\/traces\/[0-9a-f]{2}\/trace_full_~?[0-9a-f]{6}\.json$/, "https://adsb.lol", 3600],
  [/^\/adsbx\/globe_history\/\d{4}\/\d\d\/\d\d\/traces\/[0-9a-f]{2}\/trace_full_~?[0-9a-f]{6}\.json$/,
   "https://globe.adsbexchange.com", 600, "/adsbx", {"Referer": "https://globe.adsbexchange.com/"}],
  [/^\/adsbx\/data\/traces\/[0-9a-f]{2}\/trace_(full|recent)_~?[0-9a-f]{6}\.json$/,
   "https://globe.adsbexchange.com", 30, "/adsbx", {"Referer": "https://globe.adsbexchange.com/"}],
];
const STALE_S = 120;              // serve an answer this old when upstream fails
const HOME = [32.0114, 34.8867];  // TLV
const KEEP_S = 36 * 3600;         // callsign map: forget entries older than this
const ALERTS_KEEP_S = 24 * 3600; // board alerts: forget older ones
const VERSION = "0.8";            // shown at / and /status, to check which code is deployed
const UA = {"User-Agent": "flight-watch-pages-relay/" + VERSION};

function cors(origin) {
  const h = {"Access-Control-Allow-Methods": "GET", "Vary": "Origin"};
  if (origin && ORIGINS.some(o => o.test(origin))) h["Access-Control-Allow-Origin"] = origin;
  return h;
}

/** Ask adsb.lol for all aircraft near TLV and merge callsign -> [hex, seen] into KV. The run's
 *  outcome is kept in the same value ("_run": [time, upstream HTTP status, aircraft]; one write
 *  per run either way) and shown at /status. */
async function learnCallsigns(env, now) {
  if (!env.CALLSIGNS) { console.log("no KV binding named CALLSIGNS"); return 0; }
  let r, status;
  try {
    r = await fetch(`https://api.adsb.lol/v2/point/${HOME[0]}/${HOME[1]}/250`, {headers: UA});
    status = r.status;
  } catch (e) { status = 0; }
  const pairs = {};
  if (r && r.ok)  // 429 etc.: only the outcome is noted; the next run tries again
    for (const ac of (await r.json()).ac || []) pairs[String(ac.flight || "")] = [ac.hex, now - (ac.seen || 0)];
  const n = await merge(env, pairs, now, "_run", status);
  console.log(`adsb.lol /v2/point: HTTP ${status}, ${n} aircraft`);
  return n;
}

/** Merge {callsign: [hex, seen]} into the KV map (one write), forget entries older than KEEP_S
 *  and note the outcome under `key` ("_run": the cron, "_push": the monitor's /learn). */
async function merge(env, pairs, now, key, status, alerts) {
  const map = (await env.CALLSIGNS.get("map", "json")) || {};
  if (alerts) {  // [hex, flight, callsign, kind, epoch] from the monitor: kept a day, in the same value (no extra write)
    const ok = a => Array.isArray(a) && /^~?[0-9a-f]{6}$/.test(String(a[0]).toLowerCase()) && /^[A-Z0-9]{0,8}$/.test(String(a[1] || ""))
      && /^[A-Z0-9]{0,8}$/.test(String(a[2] || "")) && /^[A-Z_]{3,24}$/.test(String(a[3])) && now - (+a[4] || 0) <= ALERTS_KEEP_S && +a[4] <= now + 60;
    const seen = new Set(), keep = [];
    for (const r of [...(map._alerts || []), ...alerts.filter(ok)]) {
      const a = [String(r[0]).toLowerCase(), r[1] || "", r[2] || "", r[3], Math.round(+r[4])];
      const k = [a[0], a[3], a[4]].join("|");  // a batch resent after a lost answer is stored once
      if (!seen.has(k) && now - a[4] <= ALERTS_KEEP_S) { seen.add(k); keep.push(a); }
    }
    map._alerts = keep.slice(-500);
  }
  let n = 0;
  for (const [raw, v] of Object.entries(pairs || {})) {
    const cs = raw.trim().toUpperCase(), hex = Array.isArray(v) ? String(v[0] || "").toLowerCase() : "";
    const seen = Array.isArray(v) ? Math.min(+v[1] || 0, now + 60) : 0;
    if (!/^[A-Z0-9]{2,8}$/.test(cs) || !/^~?[0-9a-f]{6}$/.test(hex) || now - seen > KEEP_S) continue;
    if (!map[cs] || map[cs][1] <= seen) map[cs] = [hex, Math.round(seen)];
    n++;
  }
  for (const [cs, v] of Object.entries(map)) if (!cs.startsWith("_") && now - v[1] > KEEP_S) delete map[cs];
  map[key] = [Math.round(now), status, n];
  await env.CALLSIGNS.put("map", JSON.stringify(map));
  return n;
}

export default {
  async scheduled(event, env, ctx) {
    await learnCallsigns(env, Date.now() / 1000);
  },

  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin");
    if (request.method === "OPTIONS") return new Response(null, {status: 204, headers: cors(origin)});
    const json = (status, obj, extra) => new Response(JSON.stringify(obj), {status,
      headers: {...cors(origin), "Content-Type": "application/json", "Cache-Control": "no-store", ...extra}});
    const url = new URL(request.url);
    if (url.pathname === "/learn" && request.method === "POST") {  // the monitor sends what it hears near TLV
      if (!env.CALLSIGNS || !env.LEARN_TOKEN) return json(503, {error: "KV binding CALLSIGNS or secret LEARN_TOKEN missing"});
      if (request.headers.get("Authorization") !== "Bearer " + env.LEARN_TOKEN) return json(403, {error: "wrong token"});
      const body = await request.text();
      if (body.length > 300000) return json(413, {error: "too large"});
      let pairs;
      try { pairs = JSON.parse(body); } catch (e) { return json(400, {error: "not JSON"}); }
      const alerts = Array.isArray(pairs && pairs._alerts) ? pairs._alerts : [];
      return json(200, {stored: await merge(env, pairs, Date.now() / 1000, "_push", 200, alerts)});
    }
    if (request.method !== "GET") return json(405, {error: "GET only"});
    if (origin && !ORIGINS.some(o => o.test(origin))) return json(403, {error: "origin not allowed"});
    if (url.pathname === "/") return new Response(  // a person opening the address: what it is
      `flight-watch relay ${VERSION}: live ADS-B data from adsb.lol with CORS, for the flight-watch map pages.\n` +
      "Try /v2/callsign/ELY315 or /v2/hex/738071 (JSON); /status shows the callsign map.\n" +
      "https://github.com/yuval-harpaz/flight-watch\n",
      {headers: {...cors(origin), "Content-Type": "text/plain; charset=utf-8"}});

    if (url.pathname === "/status") {  // is the callsign map being filled? (for the owner)
      const map = env.CALLSIGNS ? (await env.CALLSIGNS.get("map", "json")) || {} : null;
      const at = x => x ? new Date(x[0] * 1000).toISOString() : null, run = map && map._run, push = map && map._push;
      return json(200, {version: VERSION, kv: !!env.CALLSIGNS, learnToken: !!env.LEARN_TOKEN,
        callsigns: map ? Object.keys(map).filter(k => !k.startsWith("_")).length : 0,
        lastPush: push ? {at: at(push), callsigns: push[2]} : null,
        lastRun: run ? {at: at(run), upstreamHTTP: run[1], aircraft: run[2]} : null,
        alerts: map && map._alerts ? map._alerts.length : 0});
    }
    if (url.pathname === "/alerts") {  // the flight board's warnings; `updated`: the monitor's last share (null: never)
      const map = env.CALLSIGNS ? (await env.CALLSIGNS.get("map", {type: "json", cacheTtl: 60})) || {} : {};
      const now = Date.now() / 1000;
      return json(200, {version: VERSION, updated: map._push ? map._push[0] : null,
        alerts: (map._alerts || []).filter(a => now - a[4] <= ALERTS_KEEP_S)
          .map(a => ({hex: a[0], flight: a[1], callsign: a[2], kind: a[3], t: a[4]}))},
        {"Cache-Control": "public, max-age=60"});
    }
    const hexof = /^\/hexof\/([0-9A-Za-z,]{1,1000})$/.exec(url.pathname);
    if (hexof) {
      const map = env.CALLSIGNS ? (await env.CALLSIGNS.get("map", {type: "json", cacheTtl: 60})) || {} : {};
      const out = {};
      for (const cs of hexof[1].toUpperCase().split(",")) if (map[cs]) out[cs] = {hex: map[cs][0], seen: map[cs][1]};
      return json(200, out, {"Cache-Control": "public, max-age=60"});
    }

    const route = ROUTES.find(([re]) => re.test(url.pathname));
    if (!route) return json(404, {error: "not relayed"});
    const [, host, fresh, strip, headers] = route;
    const upstream = host + (strip ? url.pathname.slice(strip.length) : url.pathname);
    const cache = typeof caches !== "undefined" ? caches.default : null;
    const key = new Request(upstream);
    const hit = cache && await cache.match(key);
    const age = hit ? Date.now() / 1000 - +(hit.headers.get("X-Fetched") || 0) : Infinity;
    const send = (r, extra) => {
      const out = new Response(r.body, r);
      for (const [k, v] of Object.entries({...cors(origin), ...extra})) out.headers.set(k, v);
      return out;
    };
    if (hit && age < fresh) return send(hit);
    let up;
    try {
      up = await fetch(upstream, {headers: {...UA, ...headers}, redirect: "follow"});
    } catch (e) {
      up = null;
    }
    if (!up || !up.ok) {  // 429 etc. are not cached; an answer of up to STALE_S is better than none
      if (hit && age < STALE_S) return send(hit, {"X-Stale": String(Math.round(age))});
      return json(up ? up.status : 502, {error: up ? "HTTP " + up.status : "upstream unreachable"});
    }
    const r = new Response(up.body, {status: 200, headers: {"Content-Type": up.headers.get("Content-Type") || "application/json",
      "Cache-Control": "public, max-age=" + STALE_S, "X-Fetched": String(Date.now() / 1000)}});
    if (cache) ctx.waitUntil(cache.put(key, r.clone()));
    return send(r);
  },
};
