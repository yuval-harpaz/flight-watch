/**
 * Cloudflare Worker: a read-only relay of the adsb.lol feed with CORS headers, so the flight
 * pages on GitHub Pages (docs/flight_map.html) can show live data. adsb.lol sends no CORS headers
 * (Oct 2026), and neither does opendata.adsb.fi, so a browser refuses them from another site.
 *
 *   /v2/hex/<hex>[,<hex>...]          -> https://api.adsb.lol/v2/...    (cached 5 s)
 *   /v2/callsign/<cs>[,<cs>...]       -> https://api.adsb.lol/v2/...    (cached 5 s)
 *   /data/traces/<xx>/trace_{full,recent}_<hex>.json -> https://adsb.lol/data/traces/... (30 s)
 *
 * Nothing else is relayed (not an open proxy), only GET, only for the origins below. The
 * short cache means many viewers of one flight cost one upstream request per 5 s. Nothing is stored.
 *
 * Deploy: dash.cloudflare.com -> Workers & Pages -> Create -> Worker -> "Hello World" -> Edit code,
 * paste this file, Deploy. Then set FB.RELAY in docs/flightboard.js to the worker's address,
 * e.g. "https://flight-watch-relay.<account>.workers.dev". Free plan: 100,000 requests a day.
 */
const ORIGINS = [/^https:\/\/yuval-harpaz\.github\.io$/, /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/];
const ROUTES = [  // [path pattern, upstream host, cache seconds]
  [/^\/v2\/(hex|callsign)\/[0-9A-Za-z~,]{1,1000}$/, "https://api.adsb.lol", 5],
  [/^\/data\/traces\/[0-9a-f]{2}\/trace_(full|recent)_~?[0-9a-f]{6}\.json$/, "https://adsb.lol", 30],
];

function cors(origin) {
  const h = {"Access-Control-Allow-Methods": "GET", "Vary": "Origin"};
  if (origin && ORIGINS.some(o => o.test(origin))) h["Access-Control-Allow-Origin"] = origin;
  return h;
}

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin");
    if (request.method === "OPTIONS") return new Response(null, {status: 204, headers: cors(origin)});
    const json = (status, obj) => new Response(JSON.stringify(obj), {status,
      headers: {...cors(origin), "Content-Type": "application/json", "Cache-Control": "no-store"}});
    if (request.method !== "GET") return json(405, {error: "GET only"});
    if (origin && !ORIGINS.some(o => o.test(origin))) return json(403, {error: "origin not allowed"});
    const url = new URL(request.url);
    if (url.pathname === "/") return new Response(  // a person opening the address: what it is
      "flight-watch relay: live ADS-B data from adsb.lol with CORS, for the flight-watch map pages.\n" +
      "Try /v2/callsign/ELY315 or /v2/hex/738071 (JSON).\nhttps://github.com/yuval-harpaz/flight-watch\n",
      {headers: {...cors(origin), "Content-Type": "text/plain; charset=utf-8"}});
    const route = ROUTES.find(([re]) => re.test(url.pathname));
    if (!route) return json(404, {error: "not relayed"});
    const [, host, ttl] = route;

    const cache = typeof caches !== "undefined" ? caches.default : null;
    const key = new Request(host + url.pathname);
    let r = cache && await cache.match(key);
    if (!r) {
      let up;
      try {
        up = await fetch(host + url.pathname, {headers: {"User-Agent": "flight-watch-pages-relay/0.1"},
                                               redirect: "follow"});
      } catch (e) {
        return json(502, {error: String(e)});
      }
      if (!up.ok) return json(up.status, {error: "HTTP " + up.status});  // 429 etc. are not cached
      r = new Response(up.body, {status: 200, headers: {"Content-Type": up.headers.get("Content-Type") || "application/json",
                                                         "Cache-Control": "public, max-age=" + ttl}});
      if (cache) ctx.waitUntil(cache.put(key, r.clone()));
    }
    const out = new Response(r.body, r);
    for (const [k, v] of Object.entries(cors(origin))) out.headers.set(k, v);
    return out;
  },
};
