/**
 * Cloudflare Worker: a read-only relay of the adsb.lol feed with CORS headers, so the flight
 * pages on GitHub Pages (docs/flight_map.html) can show live data. adsb.lol sends no CORS headers
 * (Oct 2026), and neither does opendata.adsb.fi, so a browser refuses them from another site.
 *
 *   /v2/hex/<hex>[,<hex>...]          -> https://api.adsb.lol/v2/...    (fresh 5 s)
 *   /v2/callsign/<cs>[,<cs>...]       -> https://api.adsb.lol/v2/...    (fresh 5 s)
 *   /data/traces/<xx>/trace_{full,recent}_<hex>.json -> https://adsb.lol/data/traces/... (30 s)
 *   /hexof/<cs>[,<cs>...]             -> which aircraft (hex) flew each callsign in the last 36 h
 *
 * Nothing else is relayed (not an open proxy), only GET, only for the origins below. Answers are
 * cached: many viewers of one flight cost one upstream request per 5 s, and when adsb.lol answers
 * 429 (it limits Cloudflare's shared addresses) the last answer of up to 2 min is served instead.
 *
 * The callsign map (/hexof): every 5 min a scheduled run asks adsb.lol once for all aircraft within
 * 250 nm of TLV and keeps callsign -> [hex, last seen] for 36 h, in one KV value. The board does
 * not name the aircraft, and once a flight has landed the live feed no longer finds it by callsign;
 * with the hex the map page draws the flown track of a landed flight. Nothing else is stored.
 *
 * Deploy: dash.cloudflare.com -> Workers & Pages -> Create -> Worker -> "Hello World" -> Edit code,
 * paste this file, Deploy. Then set FB.RELAY in docs/flightboard.js to the worker's address.
 * For /hexof: Storage & Databases -> KV -> Create namespace (e.g. "flight-watch-callsigns");
 * the worker -> Settings -> Bindings -> Add -> KV namespace, variable name CALLSIGNS;
 * Settings -> Trigger events -> Add -> Cron Triggers -> every 5 minutes (the expression is below).
 * Without them the relay still works and /hexof answers {}.
 * Free plan: 100,000 requests a day; KV 1,000 writes a day (this uses 288) and 100,000 reads.
 */
// Cron Trigger expression: */5 * * * *
const ORIGINS = [/^https:\/\/yuval-harpaz\.github\.io$/, /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/];
const ROUTES = [  // [path pattern, upstream host, fresh seconds]
  [/^\/v2\/(hex|callsign)\/[0-9A-Za-z~,]{1,1000}$/, "https://api.adsb.lol", 5],
  [/^\/data\/traces\/[0-9a-f]{2}\/trace_(full|recent)_~?[0-9a-f]{6}\.json$/, "https://adsb.lol", 30],
];
const STALE_S = 120;              // serve an answer this old when upstream fails
const HOME = [32.0114, 34.8867];  // TLV
const KEEP_S = 36 * 3600;         // callsign map: forget entries older than this
const UA = {"User-Agent": "flight-watch-pages-relay/0.2"};

function cors(origin) {
  const h = {"Access-Control-Allow-Methods": "GET", "Vary": "Origin"};
  if (origin && ORIGINS.some(o => o.test(origin))) h["Access-Control-Allow-Origin"] = origin;
  return h;
}

/** Ask adsb.lol for all aircraft near TLV and merge callsign -> [hex, seen] into KV. */
async function learnCallsigns(env, now) {
  if (!env.CALLSIGNS) return 0;
  const r = await fetch(`https://api.adsb.lol/v2/point/${HOME[0]}/${HOME[1]}/250`, {headers: UA});
  if (!r.ok) return 0;  // 429 etc.: the next run tries again
  const map = (await env.CALLSIGNS.get("map", "json")) || {};
  let n = 0;
  for (const ac of (await r.json()).ac || []) {
    const cs = String(ac.flight || "").trim().toUpperCase();
    if (/^[A-Z0-9]{2,8}$/.test(cs) && ac.hex) { map[cs] = [ac.hex, Math.round(now - (ac.seen || 0))]; n++; }
  }
  for (const [cs, [, seen]] of Object.entries(map)) if (now - seen > KEEP_S) delete map[cs];
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
    if (request.method !== "GET") return json(405, {error: "GET only"});
    if (origin && !ORIGINS.some(o => o.test(origin))) return json(403, {error: "origin not allowed"});
    const url = new URL(request.url);
    if (url.pathname === "/") return new Response(  // a person opening the address: what it is
      "flight-watch relay: live ADS-B data from adsb.lol with CORS, for the flight-watch map pages.\n" +
      "Try /v2/callsign/ELY315 or /v2/hex/738071 (JSON).\nhttps://github.com/yuval-harpaz/flight-watch\n",
      {headers: {...cors(origin), "Content-Type": "text/plain; charset=utf-8"}});

    const hexof = /^\/hexof\/([0-9A-Za-z,]{1,1000})$/.exec(url.pathname);
    if (hexof) {
      const map = env.CALLSIGNS ? (await env.CALLSIGNS.get("map", {type: "json", cacheTtl: 60})) || {} : {};
      const out = {};
      for (const cs of hexof[1].toUpperCase().split(",")) if (map[cs]) out[cs] = {hex: map[cs][0], seen: map[cs][1]};
      return json(200, out, {"Cache-Control": "public, max-age=60"});
    }

    const route = ROUTES.find(([re]) => re.test(url.pathname));
    if (!route) return json(404, {error: "not relayed"});
    const [, host, fresh] = route;
    const cache = typeof caches !== "undefined" ? caches.default : null;
    const key = new Request(host + url.pathname);
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
      up = await fetch(host + url.pathname, {headers: UA, redirect: "follow"});
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
