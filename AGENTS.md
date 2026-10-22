# AGENTS.md — guidance for AI coding agents

Read this before changing code. It records *why* the project works the way it does;
several choices were learned the hard way and should not be undone without asking.

## Purpose

Early warning for irregular activity on flights to/from an airport (default TLV / LLBG):
lost contact, extreme vertical rate, sharp course change, emergency squawks, GPS-spoofing
position jumps, diversions. Speed of warning matters more than completeness of data.

## Owner's preferences

- Runs from a terminal in a Python **venv**, not Docker. Later as a systemd service on a VPS.
- **Store almost nothing.** No flight tracks, no web pages. Each alert is one JSON line in
  `alerts.jsonl` with **links to existing sites** (FR24, ADS-B Exchange, airplanes.live), and
  one announcement in `posts.jsonl`.
- Report findings and proposals **before** large changes; deliver changes as pull requests.
- Never commit secrets (ntfy topic, Telegram token) or `alerts.jsonl` / `posts.jsonl`; they come from CLI
  args or environment variables.

## Architecture (single file: `flight_watch.py`)

Three data layers per cycle, in priority order:

1. **local**: all aircraft within `--radius` nm of the airport (`/v2/point`). Every
   `--interval` (10 s). This is the early warning and must stay first and fast.
2. **follow**: flights confirmed as TLV arrivals/departures, tracked anywhere by ICAO hex
   (`/v2/hex/a,b,c`). Slower cadence is acceptable (remote lost-contact limit is 600 s).
3. **discover**: the Ben Gurion flight board (data.gov.il, resource
   `e83f763b-b7d7-479e-b172-ae981ddc6de5`) → expected callsigns → global search
   (`/v2/callsign/A,B,C`), so inbound flights are found far from TLV. Slowest cadence.

Each alert goes through `Monitor.alert()` (cooldowns, hot flights) to `Notifier`: a console row
(the only output besides the first aircraft count and errors, unless `-v`), a log line,
`alerts.jsonl`, the `Announcer` (social-media-style post, threaded per flight, printed as a feed
and appended to `posts.jsonl`), and ntfy / Telegram when configured. `tests/replay.py` and
`tools/` (incident capture, turn-zone learning, `plot_alert.py`
3D replay of an alert, `squawk_cases.py`) are support code, not part of the monitor.
`tools/squawk_cases.py` (17 Oct 2026): the owner wanted real emergencies to measure the checks against,
without keeping our own list. The AeroSignals bot on Bluesky (spotterintel.bsky.social, since 11 Apr 2026,
~2 squawks a day; public API, no login) posts each 7700 / 7600 / 7500 a few minutes after it starts, and the
outcome as a reply. Each post -> callsign (airline ICAO + number, or a route-data callsign with the post's
route) -> hex from ADS-B Exchange's heatmap -> its trace, which must squawk the code -> the leg replayed
from the origin, the destination and the squawk point (`tools/track_alerts.py`; away from TLV there is no
board, so the replay follows the aircraft from the start, fixture meta `follow`, as the monitor follows TLV's
board flights anywhere - `--radius` is capped at 250 nm). Results in `squawks/` (git-ignored), including
`squawk_cases.xlsx`. Never asks adsb.lol (the monitor's feed); ADS-B Exchange one request at a time.

`docs/` is published with GitHub Pages (e.g. `docs/fz1073_map.html`, made by `tools/plot_alert.py
--map`); `examples/` holds the FZ1073 alerts and 3D plot. Plot pages are stand-alone (data embedded,
libraries from CDNs), metric, Israel time with a UTC / viewer-zone switch, alerts drawn by group (`GROUPS`: colour, marker),
a "show" menu per alert kind (marker, text; contact labels start hidden when there are many) and for the
steep changes / altitude data glitches (shown unless switched off), an "alerts at" switch between the alert
time and the first detection (the monitor's VERTICAL_RATE check run on every trace report: when it could
first have alerted; the lag is in the tooltip; dives that pass it with no alert are drawn as "not alerted", or
"no monitor" (another marker) outside the alert file's span - 4XDAN 10 Oct 2026: 4 dives while the monitor
ran, 2 alerted, polls refused with HTTP 429 half the time). The map is flat at the height of the airfield nearest the alert (within 40 nm, else sea level) and
darkened so the yellow track shows; "show terrain" / `terrain=1` draws the ground's heights (AWS terrarium tiles,
slower: off at first, 16 Oct 2026). Heights are exaggerated above that airfield, not sea level (ET302: Addis
Ababa at 2,326 m), and a track lower than its own airfield (pressure altitude) is raised to it (`on_terrain`).
Map pages take link options (`view=lat,lon,zoom,pitch,bearing,height` (m above sea level),
`x=` altitude ×, `terrain=1`, `go=alerts|first|top`, `zone=UTC|local`, `embed=1`: the map and credits only, for iframes) and a "link to
this view" button that copies them; `docs/incidents.html` (Hebrew, 14 Oct 2026, by date) embeds the incident maps that way, with each flight's data source
and a share image (`incidents_preview.png`, 1200x630, og / twitter tags; absolute Pages URLs). Map labels are HTML laid out on screen after
every frame (beside their marker, moved apart with a line back where they would overlap), and must keep the credits:
"Powered by Esri" + the tile service's sources, the flight-data source, the route sources.

ADS-B carries no origin/destination. Arrival/departure comes from the flight board first,
then the callsign's route in the VRS standing data, then a heuristic near the airport
(`ARR?` / `DEP?`).

**The flight of an alert** (`flight_map.html?hex=..&t=<unix time>`, `alertView`): the owner wants links that
show the same flight whenever they are opened, live while it flies and a replay afterwards (11 Oct 2026). The page
reads the aircraft's trace (`/data/traces` reaches back about a day; older: adsb.lol's free
`/globe_history/YYYY/MM/DD/traces/<xx>/trace_full_<hex>.json`, relayed by the worker from 0.5 and by `serve.py`),
takes the leg around `t` (`FB.flightAt`; the title adds the callsign the aircraft sent when it is not the board's, and the
ICAO hex, so our map and ADS-B Exchange's can be compared; an aircraft off the board takes its ends from the route data
by that callsign) and shows it live (heard in the last 15 min, nothing after it) or as a
drawn track with the alert's point; "our map / ADS-B Exchange / both" puts ADS-B Exchange's map in an iframe
(it and adsb.lol send no X-Frame-Options; airplanes.live, adsb.fi and FR24 refuse), live or `FB.replayURL`:
`showTrace` + `timestamp`, with `startTime` / `endTime` only at ends known to be real (ground, or < 200 kt),
because adsb.lol's coverage is thinner than ADS-B Exchange's (ET-AWJ, 10 Oct 2026: 15 KB against 104 KB for the
day; adsb.lol heard 2 min of the Amman departure, then nothing for 7 h, and `FB.legs` joins that gap). Without
a trace: live for 20 min after the alert, then the replay. A replay shows one UTC day. With no track of ours to draw,
ADS-B Exchange's replay is put in view (beside ours on a wide screen, alone on a phone; the viewer's remembered choice is
kept): since 18 Oct 2026 adsb.lol answers its trace files with a browser check (403), also through the worker. So when adsb.lol
gives no flight the page reads ADS-B Exchange's history file of that UTC day (today's too; and the day before for a leg over
midnight) through the relay's `/adsbx/globe_history/...` (`FB.traceDayADSBX`; worker 0.6, `tools/serve.py`): it answers 403
without ADS-B Exchange's own Referer and sends no CORS header. The owner chose this (19 Oct 2026) knowing ADS-B Exchange sells
its data and may block it; credited on the page when used.

**Flight pages** (`docs/flights.html`, `docs/flight_map.html`, shared `docs/flightboard.js`;
local helper `tools/serve.py`): the board table and a live map of one flight, processed in the
browser. `flight_map.html` is not the incident pages' viewer (`plot_alert.py`), but draws the track at its
height the same way (x 3 above sea level, `x=` / slider), tilts to 85 deg, and has "show terrain" (`terrain=1`,
the same terrarium tiles and decoding; 16 Oct 2026: the owner missed tilting low to see heights). It draws the link's
`a=` alerts (orange) and, with `analyze` or "analyse track", the alerts our checks raise on the drawn trace (teal
rings, labelled only where they add to `a=`; the button hides / shows them) and the track's position jumps (orange),
steep altitude changes (purple), altitude data glitches (light purple) and gaps > 2 min (grey), the blue line broken
at jumps and gaps: `tools/track_segments.py`, shared with `plot_alert.py` (its incident maps draw the same; 21 Oct
2026, DNA541 over Lebanon: 4 jumps, the monitor had dropped the NIC-0 positions). Opened from an alert (`t=`) the
analysis starts by itself once the track is drawn, except on a phone (<= 600 px or touch), with Save-Data, or
`analyze=0` (the owner: optional there, the button stays); started by itself it does not put `analyze` in the
address (a copied link would start it on a phone). The page and the Python files reach Pages / raw.githubusercontent
with the same push: a new Python file must be committed with the page that loads it. The owner wanted one procedure, not a
JS copy of the checks (17 Oct 2026): the page runs `tools/track_alerts.py` - the trace replayed through
`flight_watch.Monitor` by `tests/replay.py`, watched from the origin, the destination and the point at `t` - under
Pyodide (CDN, in a worker; `requests` replaced by a stub, the replay never reaches the network). The Python files
come from raw.githubusercontent.com (main), locally from `tools/serve.py`'s `/src/`: a change to the checks
reaches the map with the next push. `tools/squawk_cases.py` uses the same module. adsb.lol's trace is not what
the monitor received live (thinner coverage, or denser in a dive than our polls), so the two layers can differ:
FR6789 17 Oct 2026, ADS-B Exchange's trace: the same 5 alerts, 2-4 s in Chrome. The owner wants the browser (JS) and the monitor (Python) to process alike: the JS
board logic is a port of `Schedule.load` (slot grouping - one airline with two numbers in a slot is two flights:
ENT238 and ENT368 from KTW were both due 04:00 on 14 Oct 2026, and SP-ESD flying ENT368 was taken as ENT238 -, lowest flight number = operating carrier,
callsign variants), `FB.AIRLINE_ICAO` is a copy of `AIRLINE_ICAO`, `FB.israelEpoch` reads board
times as Python does (fold=0 at DST changes), and `tests/test_pages.py` runs the JS under node on
the same rows and fails when they differ - change both together. `FB.resolveAirlines` is `Schedule.resolve`
(airline codes missing from the table, via the standing data; 19 Oct 2026, the same 9 codes as the monitor on
that day's board, ~10 s, so `flights.html` shows the board first and redraws, the map waits at most 4 s); its CSV
reading is quote-aware like Python's. The alert checks are not ported: the map runs the Python (see below). The pages load
`flightboard.js?v=<first 8 hex of its sha256>`: GitHub Pages lets browsers cache files for 10 min, and a
new page with the old script failed ("FB.blocked is not a function"); a test fails until `v` is updated.

## Data sources: decisions and history

- **adsb.lol** is the default feed (free, no key *for now*; it plans feeder-issued keys).
- **adsb.fi** (opendata.adsb.fi, 19 Oct 2026) is the second feed: the local circle when adsb.lol does not
  answer (`--fallback-provider`, `Monitor.second_feed`; 18 Oct 2026 adsb.lol answered ~110 of 360 polls an
  hour, then none for 2 h twice) and a remote provider (`--remote-providers adsb.lol,adsb.fi`). Same readsb
  fields, several ids per request; the circle is `/api/v3/lat/../lon/../dist/..` (v2's is deprecated, max
  250 nm). Terms: personal, non-commercial, 1 request/s (`PROVIDERS` `rate_max` 30/min), credit adsb.fi with a
  link; many invalid requests (400-429) can get the IP blocked for a while. No `dbFlags`: military aircraft
  from its `/v2/mil` list (~140 worldwide, 16 KB) every `MIL_LIST_S` (10 min). The two feeds' coverage differs,
  so an aircraft's silence inside the circle is judged only on polls of the feed that last heard it
  (`Track.feed`; see LOST_CONTACT). No trace files there: backfill stays adsb.lol's (refused since 18 Oct).
  Tests and replays pin one feed (`--fallback-provider '' --remote-providers adsb.lol`).
- **airplanes.live** became feeder-only in 2026 and returns **HTTP 403**. Do not make it a
  default or add it as a provider unless the owner becomes a feeder.
- **OpenSky** was rejected: its daily credit budget can't cover 10-second polling.
- **ADS-B Exchange API** is paid; not used for data. Its *website* is used for links.
- **Rate limits are the main constraint.** adsb.lol returns 429 (plain nginx page, no
  `Retry-After`). The limit observed from Claude's cloud sandbox (shared IP) was only a few
  requests per minute; a home/VPS IP is likely more generous. Design adaptively: react to
  429 with backoff and jitter, never hammer, protect the local poll's budget first.
- Batch endpoints (`/v2/hex/a,b`, `/v2/callsign/A,B`) work on adsb.lol. A probe failing with
  429 or another error is **not** evidence that batching is unsupported.
- **Recent trace** (`TRACE_RECENT`, `--trace-url`): `adsb.lol/data/traces/<last 2 hex>/trace_recent_<hex>.json`,
  one aircraft's last ~10-20 min (~90 rows, ~19 KB; every change, sub-second in a dive; newest 4-16 s
  old). Another host than the API: 12 of 12 quick requests answered from a home IP on 10 Oct 2026 while
  api.adsb.lol allowed ~3.5 req/min (half the polls refused, follow requests starved for 9 h). Read with
  LOW priority, at most `--trace-per-cycle` (3) per poll, once per aircraft per poll (`Monitor.recent_trace`):
  to fill a vertical rate's missing history and before a lost-contact alert (see those), and as the
  **fallback**: when the local poll fails (HTTP 429, cooling down) the traces of a watch list (`watch_list`:
  hot, emergency squawk, a steep climb / descent or slow flight at height at the last report, a skydiving
  lift) are read and their new reports run through every check (`backfill`); hot flights outside the
  circle are read every 30 s (the follow request gives one report per `--follow-interval`). 4XDAN,
  10 Oct 2026, replayed with the live run's polls (276 successful, the rest refused) and the trace 8 s
  behind: dives raised at 08:55:25, 09:37:34 (from the trace, during refusals), 10:17:54, 10:58:04
  (live: none, 09:37:47, none, 10:58:38); ~1 trace request a minute. Trace 16 s behind: 09:37:47.
  **Refused since 18 Oct 2026 ~15:30 IDT**: adsb.lol answers its trace files (`/data/traces/*` and
  `/globe_history/*`) with HTTP 403 and a browser check ("Verification", a proof-of-work page; not to be
  worked around). The fallback then asked 3 a poll, ~1,000 an hour, while the API refused our polls. After a
  401 / 403 trace requests pause `TRACE_REFUSED_S` (30 min), doubling up to 6 h. Without the trace: no
  backfill, and the map (`docs/flight_map.html`) cannot draw or analyse past flights (it says so).
- **Routes**: adsb.lol's `routeset` POST now answers 201 with no body and its per-callsign route
  endpoint redirects to `vrs-standing-data.adsb.lol`; the same Virtual Radar Server standing
  data (CC0) is on GitHub as `vradarserver/standing-data` (`routes/schema-01/W/WZZ-3.csv`:
  `Callsign,Code,Number,AirlineCode,AirportCodes`; airports in `airports/schema-01/L/LL.csv`).
  It is the only source that identifies alphanumeric callsigns (Wizz `WZZ3W` = LHBP-LLBG;
  the board's W6 number cannot be mapped). Crowd-sourced: in the FZ1073 replay RJA810 was
  listed OJAI-ORBI while flying into Amman, and THY6685 LTBA-LEMD while departing Beirut for
  Saudi Arabia. Hence: ignore a route while the aircraft is far off its corridor, and learn the
  direction of travel from the flight (> 60 nm from both ends) before using the destination.
  Never treat a route as proof that a flight is harmless beyond those checks.
- The board is fetched with only the columns used (`FLYDATA_FIELDS`), half the size.
- **CORS** (what a page on another site may read in the browser): data.gov.il and
  raw.githubusercontent.com answer `Access-Control-Allow-Origin: *`; **api.adsb.lol does not**
  (no header, OPTIONS -> 405, Oct 2026). Hence `tools/serve.py`: it serves `docs/` and relays
  `/v2/*` (api.adsb.lol) and `/data/traces/*` (adsb.lol) on the same origin, through
  `flight_watch.Http` (budget, 429 cooldown) with a short cache. The pages use the relay when
  served from localhost, otherwise try adsb.lol directly (`?feed=` overrides).
- Flight-board quirks: codeshare rows (e.g. DAL7441 on an El Al flight) never transmit, so
  search one callsign per physical flight. Departures stay "DEPARTED" long after landing.
  The board has >3000 rows, so paginate.
- **Board history**: over.org.il ("גרסאות לעם", github.com/zomer-g/ckan-version-tracker) archives
  the board every ~15 min since 2026-04-10T22:08Z. Its append table `append_flydata_31c812a6`
  holds each distinct row state with `first_seen`; the board at time T is
  `SELECT DISTINCT ON (CHOPER,CHFLTN,CHAORD,CHSTOL) ... WHERE first_seen <= T ORDER BY ...,
  first_seen DESC` via `/api/append/31c812a6-9b0c-4f32-8317-e5f268c28f60/datastore_search_sql`
  (CORS `*`, at most 1,000 rows per answer, so page with LIMIT/OFFSET; ~2 s each, an occasional
  failure is retried once). Removed rows are not marked, hence the CHSTOL window T-1 d ... T+3 d
  (`FB.snapshotSQL`). In it FZ1073 on 30 Sep read FINAL est 09:24 at 05:45Z, then DELAYED from
  07:03Z (13:00, 14:00, 18:15) and LANDED 18:34 with the replacement aircraft: the board showed
  the diversion only as a delay.

## Alert logic and why

- **LOST_CONTACT**: silent ≥ `--lost-after` (60 s) locally, `--lost-after-remote` (600 s)
  for followed flights outside the circle, where coverage has large gaps. Ignored below
  `--lost-min-alt`, near the airport (landing), and near the circle edge for unfollowed
  aircraft (simply leaving coverage) - except when last heard falling (`Monitor.plunging`: reported
  rate <= -`--vrate`, backed by the altitude lost since the report before): that is a crash, not a
  landing nor low coverage, and none of the landing rules apply. MU5735 (21 Mar 2022, replay) was
  last heard at 3,225 ft falling 31,000 ft/min and gave no LOST_CONTACT before.
  Skipped when the feed suddenly returns far fewer aircraft (feed glitch, not mass disappearance), and for an
  aircraft whose silence we did not see begin: no answered local poll (of the feed that last heard it, inside the
  circle) within `BLIND_S` (180 s) after its last report (`Monitor.ok_polls`; it is marked lost, not alerted). 18 Oct 2026: no poll answered 13:48-15:30 IDT, and 6
  aircraft heard before it were "silent 6182s" at once. This replaced "no check after a poll > 45 s after the
  previous one", which with half the polls refused (HTTP 429) skipped the check most of the time.
  Before alerting, the aircraft's recent trace is read (`backfill`): reports our polls missed (HTTP 429,
  between polls) go through every check at their own time (alerts marked `from_trace`, "[from the feed's
  recent trace: missed by our polls]") and the silence is judged again from its last report; a trace that
  ends where we lost it too is said in the alert ("the feed's own trace ends there too").
  Silence while descending below 12,000 ft within 30 nm of another airport is a probable
  landing there (DIVERSION for a TLV arrival, otherwise only logged). Remote silence at
  cruise (or a departure descending into its destination) is a coverage gap and only
  logged, unless the flight is hot (see below): in the FZ1073 replay all 6 such alerts on
  other flights were 20-40 min gaps over the Saudi/Iraqi desert and the sea.
- **DIVERSION**: a TLV arrival on the ground far from TLV, **or** going silent while
  descending low and far away (low-altitude coverage is poor, so the landing is often
  never seen). **RETURNED**: a TLV departure landing back at TLV after reaching 40 nm or
  `RETURN_MIN_ALT` (10,000 ft): AS1282 (door plug, 6 Jan 2024, replay) climbed to 16,000 ft and landed
  back at Portland never more than 29 nm out, inside `--off-course-min-dist` (60 nm), so neither
  TURNING_BACK nor the 40 nm rule saw it, and it squawked nothing. Under an emergency squawk (or an EMERGENCY
  alert in the last 3 h) any return after a climb above 1,500 ft counts (DL2692, MSP -> JFK, 7700 at 3,775 ft).
  A TLV **departure** landing more than `DIVERTED_NM` (30) from its destination (route data) is a DIVERSION too
  (`Monitor.destination` / `other_end`; LY357 TLV -> FRA, 7700, landed at Belgrade: only EMERGENCY before). It
  must be seen leaving (`Track.left_home`: on the ground or low within 8 nm, or first heard climbing within 30 nm;
  kept across a callsign change in the air - LOT6ME -> LOT6212) or be on the board: today's routes on 2023
  callsigns made four "PDX departures" land at Seattle in the QX2059 replay.
  Found with the AeroSignals squawk cases (19 Oct 2026): of 251 replayed, 74 had only EMERGENCY; 15 diversions and
  2 returns among them, now alerted. The other 54: 53 landed at their destination (nothing to see beyond the
  squawk; descents <= ~5,500 ft/min) and one left coverage at cruise.
  A landing (on the ground at taxi speed) ends the leg: `was_airborne`, `max_dist` and the
  learned route direction reset. Before this, an arrival parked overnight and given its morning
  departure's callsign (LOT7MA -> LOT4CG) was RETURNED: 70 false alarms in one night (6 Oct 2026),
  and an aircraft on the ground repeated DIVERSION / RETURNED after every cooldown.
  A landing we did not see ends it too (`MISSED_LANDING_S` / `_KT`: heard again after 40 min having averaged
  < 150 kt; 18 Oct 2026, API refusals for hours: ELY353 TLV -> MUC unheard 100 min, then climbing out of Munich
  still as ELY353, and HYS236 TLV -> OTP unheard 3.7 h, back on its way to TLV - both "TLV departure heading
  back"). `Monitor.end_leg`: a departure landing away from TLV, or an arrival at it, has flown that callsign
  (`Track.flown_cs`): neither the board row nor the route data's direction applies to it again until the
  callsign changes.
  The far end comes from the board's airport first (`Monitor.other_end`, IATA -> ICAO by `StandingData.airport_by_iata`), the
  route data only where it agrees: ELY5417 TLV -> TBS, listed LLBG-LCLK, landed at Tbilisi as "bound for LCLK" (19 Oct 2026).
  An arrival coming down at its own origin is its previous flight, not a DIVERSION, when it was first heard > 60 nm from it, never
  farther, and never on the ground before take-off or climbing near it (`at_own_origin`; 9A-BTH changed ISR193 -> ISR182 in the air and "went
  silent probably landing elsewhere" on approach to Madrid, 19 Oct 2026). Squawk replay: the real returns (SWA512 to Honolulu,
  LAN454 to Buenos Aires, first heard 46 nm out with its departure unseen) still alert.
  Its own landing there does not count as "seen leaving" (4X-ABT, 20 Oct 2026: TLV -> Madrid as 6H183, then Madrid
  -> Malaga already as ISR152, 6H152 AGP -> TLV on time: OFF_COURSE "heading away from TLV, 1,928 nm out" and
  DIVERSION on the ground at Malaga). Such an arrival flying toward its origin is not OFF_COURSE either. Both need
  where it came from to be seen (`Track.came_from`: on the ground away from its origin before take-off, or its
  callsign changed in the air) and no emergency (`Monitor.positioning`): SWA512 (HNL -> SAN, 7700) watched from San
  Diego is first heard at FL360 on its way back to Honolulu - a return, alerted (the squawk replay relied on the
  landing being "seen leaving" before; 352 cases unchanged). One that flew away from its origin first and turns back
  still alerts (FZ1073 toward Dubai). Nor in its departure routing, `CLIMB_OUT_S` (20 min) after take-off, whatever
  its heading: 9A-BTM flew 6H717 TLV -> BUD, then left Budapest for Tivat as ISR178 (6H178 TIV -> TLV) heading 290 deg,
  "1,174 nm out and opening", and 9A-BTI as ISR174 out of Rome (22 Oct 2026).
  A landing within `DIVERTED_NM` of a stop listed in the route data (`planned_stop`: an airport between the ends,
  when the route's far end agrees with the board) is logged, not a DIVERSION: KLM461 AMS -> TLV is EHAM-LCLK-LLBG
  and "went silent ... probably landing at LCA" (20 Oct 2026). 113 routes through TLV list stops (crowd-sourced,
  some stale); cost: a real diversion to a listed stop is only logged.
  A tag flight is a stop too: when the board's origin (destination) differs from the route data's and the route data's
  leg ends (starts) at our airport, the route data's other end is the stop. BZ131 BGY -> TLV (board), BBG131 LIRF-LLBG,
  and BZ703 AMS -> TLV, BBG703 LGAV-LLBG, landed at Rome and Athens: two DIVERSIONs each (20 Oct 2026).
  A landing elsewhere must be believable (13 Oct 2026; all 4 DIVERSIONs of the run of 11-12 Oct were
  arrivals on approach to TLV with spoofed GPS - ETD3QF, AIZ282, ISR972, ELY5462): "on the ground" within
  `LANDING_AIRPORT_NM` (5) of an airport of the local `standing-data/` checkout (`StandingData.airfield_near`;
  the three fake places were 16-41 nm from any; without the checkout: not during a spoofing / GPS episode), and
  ground reports are dropped when faster than `GROUND_MAX_KT` (250; AIZ282 "on the ground" at 575 kt) or out of
  reach of the last trusted position (`unreal_ground`: the air / ground switch is right while the GPS is not,
  and `spoofed()` / `integrity()` never judged ground reports). The list lacks some military fields (nothing
  near Azraq): a real landing at one is only logged ("not taken as a landing").
  The spoofer's place is itself an airport (Amman, 6 and 13-14 Oct 2026), and arrivals on final are too low for the
  motionless-at-height test: 10 DIVERSIONs "landing at AMM" in 75 min on 14 Oct, all landed at TLV (ET404 passed
  Amman at 15,750 ft; its GPS then gave the Amman point at 0.7 kt while MLAT had it over Israel). So no DIVERSION
  (on the ground, or silent low) for a flight in an episode or within `SPOOF_PLACE_NM` (30) of a confirmed spoofing
  point (`fake_place`); logged instead. Cost: a real landing at Amman during an episode is only logged.
- **Callsign changes** (`Monitor.new_callsign`, 13 Oct 2026; 9 false alerts in the run of 11-12 Oct): a changed
  callsign is taken up after `CALLSIGN_HOLD` (120 s) and refused while another tracked aircraft sends it -
  adsb.lol gave 4X-ABG the callsign ISR723 for 55 s (4X-ABF's; ADS-B Exchange's trace never had it) and ISR050
  for 100 s, turning an Eilat arrival into a "departure turning back" and a departure into an arrival flying
  away. A change starts a new leg: `sched` (it used to stay when the new callsign was not on the board:
  TVF3452 left TLV as TVF55BJ still an "arrival"), `route_dir` (QTR79Q -> QTR52X, MEA305 -> MEA312 at Beirut),
  `was_airborne` / `max_dist` (ELY5120 landed unseen, ELY5101 on the ground 2 h later: RETURNED) are reset
  and OFF_COURSE / TURNING_BACK use only the track flown since (`leg_start`).
- **VERTICAL_RATE**: judged as a **flight-path angle** (rate vs ground speed), because a
  rate threshold flagged every normal jet climb-out (+4000-4800 ft/min at 8000 ft) and
  speed-brake descent. Limits: descent 10 deg everywhere (normal max seen ~8, glide slope 3),
  climb 12 deg en route and 18 deg within 30 nm of an airport below 15,000 ft (bizjets reach
  ~13; "an airport": the region's list, or any of the standing data's within 30 nm - ELY353 climbing out
  of Munich at 12.1 deg alerted, 18 Oct 2026). Near-airport traffic is still checked - a too-steep approach alerts. `--vrate`
  (8000 ft/min) alerts whatever the angle. The reported rate must be backed by the altitude
  history (rejects single bad values): the report nearest 30 s old within 15-90 s (a longer span only
  averages a real dive down), else reports 90 s back from the recent trace (`fill_history`); with none
  it waits for the next reports - FZ1073's dive now alerts at 05:22:13 instead of 05:22:04, when the
  history (-8,065 ft/min) confirms it. 4XDAN, 10 Oct 2026, replayed with the live run's successful polls:
  4 of 4 dives alerted (live: 2, 25-32 s late; the 30-45 s window found no report after refused polls). The angle uses only a credible ground speed (`trusted_gs`: >= 120 kt,
  >= 150 kt for an airliner above 10,000 ft, within x1.6 of the speed from positions, taken between the speeds reported at both ends - 4XDAN, a
  P750 speeding up from 55 to 138 kt into a dive, 10 Oct 2026, alerted 30 s late without that); otherwise
  only the rate limit applies. On 6 Oct spoofed speeds (B789s at FL380 "at" 117-147 kt) made
  normal climbs read as 40-60 deg and also "confirmed" glitched rates: 10 alerts, 0 now. Climb and descent have separate cooldowns, and a reading ≥1.5× the
  last alerted severity bypasses the cooldown. Without this, a small wobble suppressed the
  real 14,000 ft dive in the FZ1073 test.
  An MLAT report's speed is the receivers' estimate: the military GLF5 "684", 12 Oct 2026, descending
  5,115 ft/min at ~455 kt (6.4 deg) was given 257 kt on one poll and alerted at 11.1 deg. For MLAT reports
  `trusted_gs` takes the last speed the aircraft itself reported (<= 5 min), else the speed between its
  positions over 1-3 min, and the alert says so. One set of limits for all (the owner: the simpler the
  better); alerts on military and on light / private aircraft carry a note that steep profiles are common
  there (4 of the 15 VERTICAL_RATE alerts on disk: GLF5 x2, C295, P750).
  A military aircraft (large too, since 20 Oct 2026) gets one alert of any kind, then only EMERGENCY until
  `MILITARY_QUIET_S` (2 h) after the last alert it would have had (`Track.mil_quiet`; those are kept in
  `Track.mil_held`, once per cooldown), and a Telegram follow-up as a reply when it lands or has been silent
  `--lost-after-remote` (`telegram_followups`): how it ended, the alert sent, the ones held back (counted) and the
  map with all of them. The owner: "one alert when issues start, and one more when the flight has landed /
  disappeared". 18 Oct 2026, Jordan: CHLE1B (PC21) 15 alerts in 31 min of aerobatics (14 on Telegram, "skydiving
  pattern" among them - such types no longer count as jump planes), 7436ac (AT8T) 17 with its position jumping
  28 nm back and forth; 20 Oct: IAM4188 (Italian Air Force AT76, patrolling west of Israel) 7. Cost: a real accident
  of a military aircraft after its first alert is only logged (and in its follow-up).
  **Light / private aircraft** the same (22 Oct 2026, the owner's wish; `Monitor.light`, `Track.quiet_why` "light"):
  not military, not large, not on the board, no route data - whatever the callsign looks like (BDK12, an Israeli
  survey jet with no type in the feed, sent 18 Telegram messages in 3 h of survey lines on 22 Oct; HEZ652, Hawker
  4X-CZF, 4 in GPS jamming: their airline-style callsigns escaped the pattern-flying rule). Two differences: a fall at
  `--vrate` or faster is still alerted, and the one alert is one Telegram gets - a `--telegram-skip` kind (lost
  contact, position jump) passes as usual without starting the quiet. Jump planes (`JUMP_PLANES` types, or a lift
  seen) keep their own rule: the owner wants the weekend skydiving's one steep-descent alert, which an earlier alert
  must not use up. In the run of 21-22 Oct: 29 of 61 Telegram messages from six such aircraft, ~6 now plus follow-ups.
- **ALTITUDE_WOBBLE / EARLY_DESCENT** (16 Oct 2026, from the incident fixtures; airline callsign and large
  aircraft only). Wobble: the altitude reverses 3+ times (each leg >= 150 ft, `reversals`) within 3 min below
  FL250, with the aircraft's own vertical rate both >= +500 and <= -500 (a lone bad altitude is 2 reversals with a
  steady rate). JT610 23:25:05 UTC (crash 23:31), ET302 05:40:06 (data ends 05:41:02), FZ1073, AA77, UA93; no
  other of 619 large aircraft reached 3 at 10 s polls or at trace resolution. The owner suggested the spread of
  the altitude's second difference: it ranked single glitches first (an A20N at FL380) and missed ET302. A 30 s
  window is too short (only JT610 reaches 3 in 60 s); the check uses the track's own samples, no extra requests.
  Early descent: 1,000 ft below the climb's peak (held on two reports) within 10 min of take-off
  (`Track.takeoff`: ground -> air within `TAKEOFF_MAX_GAP_S` (3 min) below 10,000 ft, or first heard climbing
  below 2,000 ft; 18 Oct 2026: on the ground before hours of refused polls and descending into its destination
  after them was "1-8 min after take-off", 11 EARLY_DESCENTs). AS1282 01:14:24, 13 min before
  RETURNED; of 184 large-aircraft take-offs the nearest was a 400 ft step by an E75L at 9.6 min.
- **EMERGENCY**: alerts on squawk *transition* into 7500/7600/7700, not every cooldown.
- **COURSE_CHANGE**: a large reversal (>= 70 deg in 2 min) above `--turn-min-alt`. Not
  reported within ~35 nm of a regional airport below FL250 (all 10 replay false alarms were
  departure/arrival routings at Beirut, Amman, Damascus, TLV) nor at learned route corners
  (`turn_zones.json`: cells where >= 4 aircraft turned onto that heading, e.g. detours around
  closed airspace) - unless the flight is already hot. Otherwise a reversal is reported only once
  the new track has held `--turn-confirm` (120 s): 4 of the 5 overnight alerts on 6 Oct 2026 were an
  S-turn (6H254, 148 -> 76 -> 175 deg, vectoring) and holding / delay orbits of three TLV arrivals at
  02:01-02:06 (BBG251 flew one 360). Dropped: turning back near the old course (also compared with
  4 min ago, and with the pre-manoeuvre course for 10 min after a drop) and >= 300 deg one way
  (circling). Circling is never a reversal, also when hot. OFF_COURSE / TURNING_BACK wait while a
  turn is pending and skip circling (the outbound half of an orbit flies away from TLV), unless hot.
  Cost: a real reversal on a flight that has not alerted yet is reported ~2 min later.
  Nor below FL250 within 35 nm of the flight's own origin / destination far away (`own_airport_near`: board, route data;
  19 Oct 2026: ELY552 out of Sofia, ELY386 / ISR362 out of Rome, ELY318 out of Heathrow, SWR5MP into Zurich, WMT3PH into Milan),
  nor within 35 nm of where it took off in this leg (`Track.takeoff_pos`; ISR178 out of Budapest, above).
  `routine_turn_place` judges these when the turn starts; when it is reported, 2 min later, the destination is judged
  again (ETD238, OMAA-VOBL, began its approach turn at Bangalore just over 35 nm out and was reported at 17 nm, 22 Oct
  2026) - not the origin: AA1009 (ORD -> LAX, 6 Oct 2026) turning back to Chicago ended its turn within 35 nm of it,
  12 min before squawking 7700 (squawk replay: the only case of 354 that changed with both).
  Not reported either when the held new track points at the route's destination (`turned_to_destination`,
  `--toward-dest-tol`; MEA312 Beirut - Amman, 16 Oct 2026: east past Damascus, then 186 deg with Amman at 193).
  Not after the flight was seen flying its route backwards (`route_dir` -1); a route stored the wrong way round
  and not yet learned would hide a turn back to the origin.
- **SKYDIVE_PATTERN**: a jump run (>= 2 reports <= 80 kt at >= 6,000 ft over >= 15 s, positions
  agreeing) followed within 5 min by a descent of >= 3,000 ft/min and >= 1,000 ft. 4XDAN (P750) over
  the Dead Sea, 10 Oct 2026: a lift every ~40 min (climb to ~12,000 ft, ~50 kt while jumpers exit,
  -5,600 ft/min at 150 kt, silent below ~7,000 ft), each giving VERTICAL_RATE + LOST_CONTACT. The owner
  wanted those alerts kept: they carry `[skydiving pattern, <model>: ...; lift N]` and the record's
  `skydive` for 30 min (model from `JUMP_PLANES`, else the type code). 16 Oct 2026, 4XDAN again: 13 alerts in 3
  lifts, 6 on Telegram; the owner now wants one: a jump plane's first VERTICAL_RATE ("probably a skydiving ride",
  a sanity check that the monitor sees it), then until `SKYDIVE_QUIET_S` (3 h) after its last lift only EMERGENCY
  and a fall at `--vrate` or faster (VERTICAL_RATE, LOST_CONTACT "falling": a spin, not its -4,200 to -5,700 ft/min
  dives). Cost: an accident at a normal rate or below coverage (it goes silent below ~7,000 ft) is only logged.
  A jump-plane type (`JUMP_PLANES`) may run in at up to 110 kt (`SKYDIVE_RUN_KT_JUMP_PLANE`; others 80): on 17 Oct
  4XDAN ran in at 87-104 kt and its first lift went out as a plain steep descent + lost contact. Dives suppressed
  in the quiet time extend it (later lifts whose run was not seen). The Telegram text says "Probably a skydiving
  flight (sport parachuting ...)". A restart reads the rides already announced in `--jsonl` (`Monitor.past_rides`; monitor.sh:
  today's file), so they stay quiet; the bot cannot read the channel's history, and the file is the record. An airline callsign or a large
  aircraft (category A3-A5 or `LARGE_TYPES`) flying it is alerted at priority 5 (near-stall flight at
  height, then a dive); military transports drop paratroopers and are only described.
- **HOLDING**: an airliner (airline callsign, not military) circling - >= 330 deg of turn one way
  in 12 min within 25 nm - for `--holding-minutes` (30), again after each further 30 min. Military
  aircraft and non-airline traffic orbit as their job (tankers, patrols, training) and are silent, and so is
  circling below `HOLDING_LOCAL_ALT` (10,000 ft) with no board row nor route (circuits, training: RJA017, A321
  JY-AYV, 18 Oct 2026, 4,300 ft 15 nm east of Amman 2 h after taking off there; a flight that can't land is a
  scheduled one, holding higher). Nor circling that reaches its period descending below `HOLDING_LANDING_ALT`
  (3,000 ft): the approach (FR6789 "circling for 30 min ... at 275 ft" on final at Venice, squawk replay).
- **SHARP_TURN**: implied bank angle from the turn rate and speed >= `--max-bank` (35 deg),
  everywhere including near airports. Airliners stay below ~25-30; FZ1073's U-turn was a
  normal-rate turn (~25 deg), which is why reversals and sharpness are separate alerts.
  Positions must agree with the speeds, so GNSS glitches don't count as turns, and the speed must be one the
  climb / descent angle would trust (`trusted_gs`). A light aircraft (not large, not military) faster than
  `SMALL_MAX_KT` (600) over the ground has a bad speed: HEZ652, a Hawker 800, "134 -> 50 deg in 30 s at 728 kt
  (~62 deg bank)" at FL190 in GPS jamming (22 Oct 2026; the same aircraft had a corrupted velocity on 6 Oct).
- **OFF_COURSE / TURNING_BACK**: long before landing, an arrival > 60 nm out flying >= 100 deg
  away from TLV for 150 s while the distance opens, or a departure flying back toward TLV
  (within 60 deg) while closing. OFF_COURSE also covers any flight with a plausible route
  flying away from its (learned-direction) destination; without the direction and corridor
  checks this gave 20 false alarms on Beirut departures in the replay. Route bends (Gulf flights via Saudi Arabia and Jordan) stayed
  below 65 deg in the replay; FZ1073 reached 165 deg. A route-based OFF_COURSE is not raised while the flight descends
  (<= -500 ft/min, below FL300) toward a regional airport <= 150 nm ahead (`descending_toward`): THY6264, listed LTBA-WSSS,
  flew Istanbul - Amman on 14 Oct 2026 and alerted at FL254 descending, 90 nm from Amman.
  An aircraft seen taking off from our airport in this leg (`Track.rose_home`, kept across a callsign change in the
  air) is not an arrival, whatever the board or route data say of its callsign (`Monitor.arriving`; classified `DEP?`):
  4X-AGY landed at TLV as AIZ804, became AIZ802 (route LLER-LLBG) on the ground and flew to Ramon over the Dead Sea
  with it, "TLV arrival ... 151 deg away from TLV" (20 Oct 2026; AIZ807 26 min later). Then, as AIZ807, it went
  silent at 12,900 ft 37 nm from Ramon (LOST_CONTACT): a departure silent while descending below 15,000 ft within
  50 nm of its destination is logged as probably landing there.
  Neither within `--off-course-min-dist` of the far end (route data: an arrival's origin, a departure's
  destination): climbing out "away from" the destination and approach turns "back" (squawk replay: TO4755 out of
  Barcelona, AA2045 on approach to SFO "heading back to PHL, 2175 nm out").
- **TOWARD_ISRAEL**: the hijack scenario of a flight *not* bound for Israel turned toward it.
  Military aircraft (readsb dbFlags) are exempt: on 6 Oct 29 of 33 alerts were USAF KC-135
  tankers, a military Gulfstream (IRON) and a Canadian C-130 operating over Israel for hours.
  Above 8000 ft, a turn of >= 45 deg after which Israeli airspace (rough polygon) is <= 12 min
  ahead on two samples in a row, from a track that did not point there; or about to enter it
  within 3 min without looking like an arrival. Israel traffic is exempt: board/route-DB
  TLV traffic, 4X- registrations, Israeli airline callsigns, aircraft seen low at an Israeli
  airport, low traffic near one, a route through an Israeli airport. Also exempt: descending
  into its own non-Israeli airport, pointing (within 25 deg) at its own route destination or
  origin beyond Israel, and learned route corners. The main false-alarm risk is TLV arrivals
  not recognised as such (Wizz-style callsigns, private jets): they fly straight in, so the
  turn requirement keeps them quiet; route data now recognises most of them. Jordan is ~40 nm from the border, so a looser rule (30 min,
  30 deg) flagged 12 routine Amman/Damascus movements in the replay.
  Amman's own traffic over Israel is exempt (`amman_traffic`, 13 Oct 2026: Royal Jordanian crosses at
  31.8-32.1 N daily, 5 alerts in each of two runs): inbound from the west pointing at Amman, on approach from
  the east (<= 60 nm, low or descending), or climbing out in the 20 min after being on the ground there /
  first heard low and climbing. Not either: a turn toward Israel by an aircraft with no route that has stayed
  within 40 nm of Amman below 15,000 ft for 20 min (`around_amman`; RJA017 above), though about to enter still
  is. Kept on the owner's wish: a flight from the east flying on past Amman, and
  anything else about to cross. The surroundings of Eilat (`EILAT_ALLOW_NM`, 50 km = 27 nm) are not counted
  (`over_israel`): the airways over Taba / Aqaba pass a few km south and a straight line from them clipped the
  tip (5 alerts, none entered). 25 km at first; widened because NMA445 from Jordan toward Sinai pointed 14-27 nm
  north of Eilat. The owner may drop this alert kind altogether.
- **Hot flights**: any alert makes a flight followed, and any anomaly except a lone LOST_CONTACT makes it hot
  (silence alone is mostly a coverage gap and asking for a silent aircraft every cycle finds nothing; it is hot
  when another anomaly came within `--hot-minutes`; 11 Oct 2026. Refusals do not come from this: in the run of
  10-11 Oct each hour had ~120 answered and ~240 refused local polls whatever the alerts - the 10 s poll against
  ~2 req/min allowed; the status line now counts hot flights and those followed only for lost contact).
  Hot lasts `--hot-minutes` (20): queried
  every cycle instead of every `--follow-interval`, every anomaly reported (no terminal or
  route-corner exemptions), and two different anomalies within that time are sent at
  priority 5 with `[also: ...]`.
- **Position-only tracks** (`Track.position_only`: no speed nor track of its own for 10 min - MLAT): no
  POSITION_JUMP and no LOST_CONTACT (unless hot or falling), only logged. On 14 Oct 2026 an Israeli military
  GLF5 ("676", callsign "@@@@@@@@") at FL350 and Jordanian military trainers (hex 7434xx, callsigns "BK", "DE",
  "0") over the Jordan valley jumped 10-56 nm and dropped out: 59 of 63 POSITION_JUMPs, 46 of 82 LOST_CONTACTs.
- **Pattern flying**: a COURSE_CHANGE / SHARP_TURN on an aircraft with no route that is not large, within 2 h
  of its last turn alert and with no other anomaly, is only logged. 14 Oct 2026: G-FPLD (CLB230, BE20) and
  4X-CVH (BDK09, G280) flew survey lines west of Israel for hours: 15 of 17 turn alerts (now 3). SHARP_TURN
  ignores speeds above `--max-gap-speed` (THY6219 "843 kt"). TOWARD_ISRAEL skips aircraft below 10,000 ft
  within 15 nm of a non-Israeli airport (DNA541 turning onto its approach at Damascus, route unknown).
- **POSITION_JUMP**: implausible speed between consecutive positions **within 120 s**
  (`--max-speed` 1200 kt, loose because of position noise). After a longer gap the plane may
  just have moved while unheard, so only an impossible *average* counts: reappearing farther than
  `--max-gap-speed` (750 kt; airliner ground-speed record ~700) allows. Also one position off the
  track and the next back on it (A -> B -> C needs > 750 kt, A -> C does not; >= 5 nm off).
  Military aircraft (readsb dbFlags) keep 1200 kt for both. Seen live: THY1KG 5 Oct 2026
  reappeared 94 nm after 408 s (833 kt) then three MLAT jumps of ~90 nm; LY5134 6 Oct reappeared
  139 nm away after 3.4 min. The FZ1073 replay gave no new alerts with these rules. The Eastern
  Mediterranean has heavy GNSS jamming and spoofing, which causes jumps, fake turns and position
  dropouts.
- **GPS_SPOOFING**: only physically impossible reports count as fake: a fixed-wing aircraft (not
  rotorcraft / balloon / light / military, which can hover) at >= 5,000 ft under 50 kt over the
  ground while its reported (barometric, air-data) vertical rate is under 6,000 ft/min
  (`SPOOF_MAX_RATE`; a stall or spin falls 10,000+ ft/min and stays fully checked). Such a report
  never enters the track. Reports within 2 km of each other form an episode labelled by its start,
  `spoof-YYYYMMDDTHHMMZ`; a second aircraft confirms it (one alert), 15 min without fake reports
  ends it (one summary alert). Position-based alerts (`SPOOF_KINDS`: jumps, turns, off-course,
  toward Israel, lost contact, holding) on a flight in an episode, for 30 min after its last fake
  report, and jumps landing within 100 nm of an active spoofing point, are logged under the label
  instead of alerted - unless the flight is hot from an alert that does not rest on GPS (emergency
  squawk, vertical rate, diversion). Squawk and barometric altitude are never suppressed. Every
  alert record carries `spoof` (the label) when the flight was in an episode.
  6 Oct 2026, 15:42-17:25 IDT: three bursts (~12 min each, ~30 min apart) placed 43 aircraft at
  31.717 N 35.999 E (Amman airport) at cruise with ~0.7 kt, track 0; ~190 of that day's 562 alerts.
  Replaying those 43 aircraft together: 171 alerts -> 71 (6 of them the episodes). Left: lost
  contact at each burst's onset (aircraft go silent ~1 min before showing up frozen, before a
  second aircraft confirms the point) and their "restored" follow-ups.
  Barometric altitude (`alt_baro`, `baro_rate`) comes from the aircraft's air data and survives
  GPS spoofing. GPS altitude (`alt_geom`) differs from it by hundreds of feet (weather; 4XDAN +500-600
  ft on 10 Oct 2026), so a report with only `alt_geom` is converted with that aircraft's last offset
  (`Track.geo_offset`); readsb traces do the same swap (flag 8), and `plot_alert.py` converts them too
  and draws altitude steps that the aircraft's own vertical rate does not back as a data glitch; lat/lon, ground speed and track are GNSS. **MLAT** positions (ground receivers'
  timing; the feed's `type: mlat` / `mlat: [fields]`) are independent of the aircraft's GPS: never
  judged spoofed, and their gs / track are dropped unless MLAT computed them too (MLAT records
  often carry the aircraft's spoofed 0.7 kt / 0 deg). For a flight in an episode, a GPS report
  farther from its latest MLAT fix (<= 3 min) than it could have flown is fake. Measured 6 Oct:
  MLAT vs clean GPS median 0.2 km (78% within 1 km); while GPS was spoofed MLAT followed the real
  path (AEE925 climbing out of TLV), but MLAT zig-zags up to ~20 km low near TLV (poor geometry),
  and the feed gives one position per aircraft (GPS preferred), so MLAT appears only intermittently.
  `tools/plot_alert.py --sources` draws both.
- **GPS_DEGRADED / NIC**: `nic` is the aircraft's own position integrity (0 = it does not trust
  its position: GPS jammed, then it flies on inertial and positions stay roughly right, or a spoof
  it noticed - AEE925: NIC 0 / NACp 0 / SIL 0 on every fake position, 8 again a minute after).
  A NIC-0 report is used only if reachable from the last trusted position (3 nm + 650 kt), else
  dropped without a jump alert. The reach grows with time, so a report that continues the dropped ones
  (reachable from the last of them, `Track.nic_bad`) is dropped too: ISR972's fake place 73 nm away came
  within reach after 3 min and its silence there was a DIVERSION (12 Oct 2026). Healthy NIC proves nothing: on 6 Oct 61% of the reports frozen
  at the Amman point said NIC 8 (only 34% NIC 0), and 5.5% of ordinary reports were NIC 0. Drops
  to NIC 0 by 3+ aircraft within 2 min and 100 nm = one alert labelled `gps-YYYYMMDDTHHMMZ`
  (labels get -2, -3 when episodes start the same minute) and one when it ends (15 min without
  NIC 0). Joint replay of the 43 spoofed aircraft: 38 alerts without NIC -> 36 with it (jumps
  13 -> 8); the second burst's GPS_DEGRADED came 2 min before its spoofing was confirmed. An
  MLAT position on an ADS-B aircraft also means its GPS report was rejected (low NIC), but no
  MLAT means nothing (most fake reports kept NIC 8; MLAT needs receiver coverage).
- **MASS_SILENCE / lost-contact wait**: a loss waits `--lost-confirm` (60 s) after `--lost-after`;
  heard again = nothing; 3+ aircraft silent within 2 min of each other = one alert labelled
  `silence-YYYYMMDDTHHMMZ` (reception, jamming, spoofing onset: on 6 Oct aircraft went silent ~1 min
  before showing up frozen). Hot flights are not delayed. The owner accepted the minute's delay.
  Joint replay of the 43 spoofed aircraft: 171 alerts -> 38 (9 of them episode / silence alerts).
- Conditions that persist (`PERSISTING`: TOWARD_ISRAEL, OFF_COURSE, TURNING_BACK) repeat every
  `--repeat-minutes` (30), not every `--cooldown` (5 min); a clearly worse reading still escalates.
  (One tanker over Israel for 75 min gave 15 alerts.) The FZ1073 replay now has one OFF_COURSE.
- Cooldowns and link dates use the **data timestamp**, not wall-clock time.
- Any flight that alerts becomes **followed**, so its fate stays visible - until its flight is over: a landing
  (seen, or missed: `end_leg`) or a new callsign on the ground ends the following unless the flight is hot
  (`Monitor.unfollow`); the board or the route data follow its next leg again if it is ours. Before, nothing ended it
  but 3 h of silence: A6-AEO flew EY596 TLV -> AUH, then ETD238 to Bangalore still followed, and its approach turn
  there was a "course reversal 2,594 nm E of TLV" (22 Oct 2026); 156 aircraft were followed that afternoon.

## Links in alerts (no local storage)

`map` (our `docs/flight_map.html?hex=<hex>&f=<flight>&d=<A|D>&t=<alert time>` on GitHub Pages, `--viewer-url`; first
link in posts, which then leave out Live; with `t` the page shows the flight of the alert whenever it is opened, see Flight pages;
`a=KIND~<s after t>.KIND~..`: this alert and the flight's earlier ones of the last 12 h, `Track.sent`, `map_alerts` - 17 Oct 2026, the owner
wanted every alert on the map, not just the one marker at `t`), `fr24_flight` (`/data/flights/fz1073`, flight number from the board or callsign → IATA),
`fr24_aircraft` (by registration), `live_adsbx` / `live`, `replay_adsbx` / `replay`
(`?icao=<hex>&showTrace=<UTC date>&timestamp=<alert time>`: that day's whole track with the aircraft
placed at the alert; a live link opened later shows whatever the aircraft flies then). ADS-B Exchange replays proved more reliable for older
days than airplanes.live, which showed "No data". Tapping an ntfy notification opens live ADSBx.
Telegram gets the post's wording as HTML (`Notifier.telegram_text`), without `--telegram-skip` kinds
(LOST_CONTACT, CONTACT_RESTORED, POSITION_JUMP: 355 of 429 alerts in the run of 10-11 Oct 2026; GPS_DEGRADED) unless
the message's `[also: ...]` names another kind; 83 of the 429 would have been sent. Jamming / spoofing is daily here:
NIC 0 is the aircraft saying it does not trust its GPS, while it flies on inertial and its track looks normal (16 Oct
2026: an episode near Amman went out as a start and an end with nothing to see on the map). So GPS_DEGRADED is skipped,
and GPS_SPOOFING sends one start in `TG_SPOOF_S` (6 h) and never an end; all stay in the files. MASS_SILENCE is
skipped too (18 Oct 2026: 9 of 66 messages, nothing to see on a map, mostly while the API refused our polls), and
TOWARD_ISRAEL (19 Oct 2026, the owner's wish; still checked, logged and in the files).
`--telegram-to chat|channel` picks `TELEGRAM_CHAT_ID` (default) or `TELEGRAM_CHANNEL_ID` (the public, read-only
channel); `monitor.sh` sets it from `SENDTO` (channel).
Each message is also appended to `--telegram-posts` (`posts_telegram.jsonl`, with `sent`), apart from `posts.jsonl`. **Threads** (20 Oct 2026, IZ992 JFK -> TLV, 7700: three
messages in two minutes, each map showing only the track flown by then; the owner: post early, then integrate): a flight's
later messages reply to its first (`Notifier.tg_threads`, rebuilt from `--telegram-posts` at start: `message_id`, `reply_to`),
and `Monitor.telegram_followups` sends one reply, `FOLLOW_UP`, when it lands or `TG_FOLLOWUP_S` (45 min) after its last
message - for a flight with 2+ messages or an emergency / diversion / return: where it ended (or is), its alerts with times,
and the map of the whole flight (`t` = its last report). None after a landing alert (DIVERSION / RETURNED raised on the ground): LY353 TLV -> MUC landed at Athens on 21 Oct 2026, and the follow-up 2 min later repeated it. The same squawk alerted within 3 h is not alerted again (AeroSignals'
post, then our feed's 7700), and an AeroSignals EMERGENCY on an aircraft last heard before the post is placed at the post's time.

**AeroSignals as a second source of squawks** (19 Oct 2026, the owner: "so we won't miss a squawk"). OS82 TLV -> VIE squawked
7700 on 18 Oct over Bulgaria and diverted to Sofia while adsb.lol refused nearly every poll: nothing alerted. The monitor reads
the bot's posts (`Monitor.aerosignals`, `--aerosignals-interval` 120 s, LOW priority, public Bluesky API; it answers 504 now and
then: logged, read again next time). A post (`aero_case`, the template `tools/squawk_cases.py` parses) of a flight from / to
an Israeli airport (`ISRAEL_IATA`) is looked for by callsign (the board's for that flight, else airline ICAO + number, also
zero-padded: AUA082) in the remote feeds; the answers join the poll's batches, so the aircraft gets a normal track. Then
`aero_alerts`: our own EMERGENCY within 3 h -> only logged; the aircraft found -> EMERGENCY on it "reported by AeroSignals"
(map, links, Telegram as usual); not found within `AERO_FIND_S` (10 min) -> `Notifier.send_post` (log, console, Telegram with
the post and FR24, `--telegram-posts`; not in `--jsonl`, no position). Posts > `AERO_MAX_AGE_S` (3 h) old are skipped; the post
ids and EMERGENCY callsigns in the day's files are read at start (`past_aero`), so a restart does not repeat one. Tests and
replays pass `--aerosignals-interval 0`. Not announced: squawks elsewhere (the bot posts ~2 a day) and its "Update:" replies.

## Testing

No network access is assumed in tests: `tests/test_flight_watch.py` replaces the HTTP session
with a fake feed (`/point`, `/hex`, `/callsign`, standing-data files, scripted 429s) and the
clocks with a fake clock, then feeds physically consistent paths (`path()` / `fly()`).
Incident replays (`tests/replay.py`) serve captured real traffic the same way. Keep or create at least these scenarios:

- **Basic**: course reversal, steep descent, a position jump, squawk 7700, loss of contact
  away from the edge. Each must alert exactly once.
- **Distant arrival**: a scheduled `ELY002` arrival found by global callsign search,
  followed by hex, lost remotely, restored, then on the ground ~1200 nm away → DIVERSION.
  No POSITION_JUMP after the long gap.
- **FZ1073 reconstruction** (flydubai DXB→TLV, 30 Sep 2026, A6-FKF, hex `8965d1`, diverted
  to Tabuk). Altitude wobble 05:21Z, drop of >14,000 ft in <30 s at 05:22Z, squawk 7700
  05:31Z then 7500 05:38Z, turn toward Tabuk, silent while descending near Tabuk.
  Expected: VERTICAL_RATE (wobble), VERTICAL_RATE (dive, not suppressed), COURSE_CHANGE,
  EMERGENCY 7700, EMERGENCY 7500, DIVERSION.
  The real-traffic replay (`tests/test_replay_fz1073.py`, ADS-B Exchange data) differs: 7500
  from 05:35:20, the trace ends at 05:53:33 at 15,025 ft ~155 nm north of Tabuk (so LOST_CONTACT
  instead of DIVERSION), and the 5-second wobble is seen only at the 30 s follow cadence, where
  it reads as a glitch. It does yield the dive, 7700, 7500, the U-turn, OFF_COURSE ~141 nm out
  at 05:45 and LOST_CONTACT. The dive-not-suppressed property has a synthetic unit test.
- **429 handling**: rate-limited responses back off without stopping the local poll and
  without permanently marking a provider or endpoint as unsupported.
- **Incidents elsewhere** (`tests/test_replay_incidents.py`): FR4978 forced to Minsk (2021, ADS-B
  Exchange, 56 aircraft), AS1282 door plug (2024, 76 aircraft), MU5735 dive (2022, Flightradar24's
  published data, the aircraft alone), AA77 / UA93 (11 Sep 2001, `tools/import_911.py` from the 11-septembre.com
  Google Earth file `911maps.kmz`, not committed: the flight recorder path, 1 point a second, timed by the radar
  returns, whose times are EDT although marked Z; kept as if the transponder stayed on; the switch-off is the
  fixture's meta `events`, drawn by `plot_alert.py` as ⊘ TRANSPONDER_OFF). Watched from a point near each (`airport_coords` -> `--lat` /
  `--lon`; the Israel-only checks are off). ADS-B Exchange's history starts in May 2020 (404 before)
  and had nothing of MU5735 over China nor of AZAL 8243 after 04:16 UTC over Dagestan; older or
  uncovered incidents come in through `tools/import_track.py` (one aircraft from a CSV / KML; no
  surrounding traffic, so detection only; 0,0 "no position" rows and exact earlier positions re-sent later are dropped:
  FR24's JT610 CSV had 37 + 93, drawn as jumps across the world and as spikes). Route data is today's and callsigns are reused: a captured
  incident keeps its flight's route of the day in `meta.route_overrides`. Pre-ADS-B incidents (e.g.
  9/11: NTSB flight path studies and FAA radar records at the National Archives) would be radar
  plots without speeds, and transponders switched off show as the end of the points.

**Measuring a change on past alerts** (`tools/archive_replay.py`, 21 Oct 2026, PR by daniel24ka): the alerts of an
`alerts.jsonl` replayed on the checkout, per alert kind "then" (in the file) and "now". The traces come from adsb.lol's
daily archive on GitHub (`adsblol/globe_history_YYYY` releases, ~3.5 GB of tar parts a day, ODbL; plain HTTPS after the
18 Oct browser check on the trace files), streamed once per day with only the alerted aircraft kept (`archive_traces/`,
git-ignored); a day is published the next day. One-aircraft replays: MASS_SILENCE, GPS_* and the board-based kinds are not
judged. Measured on the channel's posts of 16-20 Oct 2026: 84 replayable alerts then, 37 with the code of 21 Oct.

Quick live check: `python flight_watch.py --once -v`. Keep live experiments short.
A local `standing-data/` checkout is used when present (tests pin `--standing-data` to the URL so
they keep using the fake feed). Otherwise route/airline files are fetched at most `ROUTE_FILES_PER_CYCLE` (3) per poll: they are 1.1 s apart,
and fetching a cold start's ~60 files in one cycle held up the local poll for over a minute.

## Open issues

- **Short events between polls (the FZ1073 altitude wobble, 05:21:44-49Z).** It lasted ~5 s
  while FZ1073 was 224 nm out, so it was sampled only by the 30 s follow request; `/v2/hex`
  returns only the latest state, and the flight became hot only at its first alert (the dive
  after the wobble). Detection currently works without it. The recent trace (see Data sources)
  now covers hot flights every 30 s, but a flight that has not alerted yet is still sampled only by
  the follow request.

- **Bluesky** (`Bluesky`, 21 Oct 2026, PR by daniel24ka): on when `BLUESKY_HANDLE` + `BLUESKY_APP_PASSWORD` are in the
  environment (only these: the astro bots' `Bluehandle` / `Blueword` are set in the owner's shell, and that account must not
  post aviation; 21 Oct 2026 the owner only reads Bluesky - AeroSignals - and posts nothing); `atproto` is imported only then. The Announcer's posts go through
  a queue to one worker thread (never the poll loop); a failed login waits 10 min, a failed post is logged and the next one
  goes on (a reply whose parent never made it is posted top-level). Local post id -> strong ref threads the replies for the
  run. `--bluesky-skip` (default as `--telegram-skip`), `--bluesky-posts` (`posts_bluesky.jsonl`, with the bsky.app link or
  `sent: false`). Tests: `BlueskyTests` with a fake client. The pattern it follows, from the owner's
  `yuval-harpaz/astro` bots:
  `from atproto import Client, client_utils, models`; `Client().login(os.environ['Bluehandle'],
  os.environ['Blueword'])` (app password from env, never committed); `client_utils.TextBuilder()`
  with `.text()` / `.link(label, url)`; `send_post(builder)` (or `send_post(text=builder,
  reply_to=models.AppBskyFeedPost.ReplyRef(parent=models.create_strong_ref(parent_post),
  root=models.create_strong_ref(root_post)))` for thread replies); post URL
  `https://bsky.app/profile/<handle>/post/<uri.split('/')[-1]>`. astro keeps text to ~250 chars
  ("300 limit but failed once"); posts here are <= 280 incl. link labels. Keep a map of local
  post id -> returned post refs to thread replies, log failures, and never let posting block
  the poll loop.

- **Board airline codes missing from `AIRLINE_ICAO`** are resolved from the standing data's
  airline list (`StandingData.airline_by_iata`). IATA codes are reused (NO = Neos and Aus-Air, about
  a third of codes have several airlines), so the candidate with routes through TLV wins; still
  ambiguous means unmapped. Oct 2026: 3F = FIE, H4 = HYS, H7 = HYM, NO = NOS, U8 = CYF. The list
  disagrees with two built-in entries: GQ listed as `BSY` (a defunct US airline), but GQ720 / 721 fly as SEH720 / 721
  (Sky Express, SX- A20Ns; logs of 10-18 Oct 2026), so `SEH` is right; 9U `MLD` (Air Moldova) is absent and was never seen.

- **Alerts on the board** (`flights.html`, issue #26, 21 Oct 2026): the owner's conditions - no store of anyone
  else's, hidden by default ("show warnings", `?alerts=true`: naive users must not take an amateur system's warnings
  for news), the time of the last update and "not available" when the monitor is not sharing. The monitor's alerts
  of the kinds Telegram gets (`Notifier.telegram_wanted`, not GPS_SPOOFING: no flight) ride on its `/learn` POST as
  `"_alerts"` ([hex, flight, callsign, kind, epoch]; kept for the next POST when one fails), and the worker (0.7)
  keeps 24 h of them in the callsign map's KV value - no write of its own - and serves them at `/alerts` with
  `updated` (the monitor's last share). `FB.alertsFor` puts them on a row by flight number (any codeshare) or
  callsign within 12 h of its time. Older than 15 min: "not available since". GitHub Actions' share sends none.
- **Flight pages, next steps.** **Live data on GitHub
  Pages needs a relay with CORS** (posts link the Pages map): adsb.lol sends no CORS header on any
  endpoint (`/v2/*`, `/data/traces/*`, Oct 2026), so a page there can't read it whatever it tries.
  `FB.RELAY` is the owner's Cloudflare Worker (https://flight-watch-relay.yuvharpaz.workers.dev, code
  `tools/cors_worker.js`; redeploy by pasting it after changes; workers.dev is not reachable from the
  cloud sandbox). It relays only `/v2/hex|callsign/*` and `/data/traces/*`, only for the Pages /
  localhost origins, with a 5-30 s cache (tested under node in `tests/test_pages.py`). adsb.lol does
  answer 429 to Cloudflare's shared addresses (seen 8 Oct 2026 on `/v2/callsign/CFG4308`), so the
  worker serves its last answer of up to 2 min on an upstream error and the map retries a 429 after
  20 s. Fallback if that is not enough: an HTTPS `tools/serve.py` on the VPS.
  **Callsign map** (`/hexof/<cs,...>`): the board does not name the aircraft and the feed finds a
  callsign only while it flies, so a landed flight had no track. The worker keeps callsign -> [hex,
  last seen] for 36 h in one KV value (binding `CALLSIGNS`). Its Cron Trigger (`/v2/point/<TLV>/250`
  every 5 min) got 429 on every run from Cloudflare (8 Oct 2026). It is filled through `POST /learn`
  (`Authorization: Bearer <token>`; the worker's secret `LEARN_TOKEN`, `RELAY_TOKEN` elsewhere):
  - **GitHub Actions** `.github/workflows/share_callsigns.yml` every 10 min runs
    `tools/share_callsigns.py` (stdlib only): one `/v2/point/<TLV>/250` request, then `/learn`.
    The owner did not want the map to depend on a local Python process (8 Oct 2026). Scheduled runs
    can start late or be skipped; an arrival is within 250 nm for ~35-40 min, so it is usually seen.
    adsb.lol busy = warning only; a refused `/learn` = red run. GitHub disables schedules after 60
    days without commits. 144 KV writes a day.
  - **The monitor**, while it runs with `RELAY_TOKEN` set: `Monitor.share_callsigns` POSTs the pairs
    heard since the last send every `--share-interval` (300 s), LOW priority, never stopping the poll.
  The Cron Trigger was removed (it only got 429 and cost 288 writes a day of the free 1,000). The owner agreed to this small,
  expiring store (8 Oct 2026). The map then draws that aircraft's leg to / from TLV from today's
  trace (`FB.legs` / `FB.legAt`). The route database is ignored when it disagrees with the board's
  airport (ELY5064 listed BCN, board HER). opendata.adsb.fi sends no CORS
  header either (checked Oct 2026: 200 without `Access-Control-Allow-Origin`, OPTIONS -> 405).
  The worker's `/status` shows its version, whether KV is bound, the number of callsigns and the
  last cron run's upstream HTTP status (kept as `_run` in the same KV value, so still one write per
  run): a run answered 429 changes no callsigns. Without a relay the map skips
  pointless lookups (`FB.liveUseful`: landed, cancelled, far from its time) and turns a refused
  feed (`FB.blocked`, a fetch TypeError) into a quiet note with ADSBx / FR24 links. Past boards come from the
  over.org.il archive (nothing stored here); past tracks from adsb.lol's `/globe_history` files (see "The
  flight of an alert"; ADS-B Exchange's fuller history is not free to read from a page - its replay is embedded).

## Out of scope unless asked

Web pages or dashboards, databases, storing tracks, Docker-first deployment, paid APIs.
