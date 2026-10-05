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

ADS-B carries no origin/destination. Arrival/departure comes from the flight board first,
then the callsign's route in the VRS standing data, then a heuristic near the airport
(`ARR?` / `DEP?`).

## Data sources: decisions and history

- **adsb.lol** is the default feed (free, no key *for now*; it plans feeder-issued keys).
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
- Flight-board quirks: codeshare rows (e.g. DAL7441 on an El Al flight) never transmit, so
  search one callsign per physical flight. Departures stay "DEPARTED" long after landing.
  The board has >3000 rows, so paginate.

## Alert logic and why

- **LOST_CONTACT**: silent ≥ `--lost-after` (60 s) locally, `--lost-after-remote` (600 s)
  for followed flights outside the circle, where coverage has large gaps. Ignored below
  `--lost-min-alt`, near the airport (landing), and near the circle edge for unfollowed
  aircraft (simply leaving coverage). Skipped entirely after our own outage or when the
  feed suddenly returns far fewer aircraft (feed glitch, not mass disappearance).
  Silence while descending below 12,000 ft within 30 nm of another airport is a probable
  landing there (DIVERSION for a TLV arrival, otherwise only logged). Remote silence at
  cruise (or a departure descending into its destination) is a coverage gap and only
  logged, unless the flight is hot (see below): in the FZ1073 replay all 6 such alerts on
  other flights were 20-40 min gaps over the Saudi/Iraqi desert and the sea.
- **DIVERSION**: a TLV arrival on the ground far from TLV, **or** going silent while
  descending low and far away (low-altitude coverage is poor, so the landing is often
  never seen). **RETURNED**: a TLV departure landing back at TLV after leaving.
- **VERTICAL_RATE**: judged as a **flight-path angle** (rate vs ground speed), because a
  rate threshold flagged every normal jet climb-out (+4000-4800 ft/min at 8000 ft) and
  speed-brake descent. Limits: descent 10 deg everywhere (normal max seen ~8, glide slope 3),
  climb 12 deg en route and 18 deg within 30 nm of an airport below 15,000 ft (bizjets reach
  ~13). Near-airport traffic is still checked - a too-steep approach alerts. `--vrate`
  (8000 ft/min) alerts whatever the angle. The reported rate must be backed by the altitude
  history (rejects single bad values). Climb and descent have separate cooldowns, and a reading ≥1.5× the
  last alerted severity bypasses the cooldown. Without this, a small wobble suppressed the
  real 14,000 ft dive in the FZ1073 test.
- **EMERGENCY**: alerts on squawk *transition* into 7500/7600/7700, not every cooldown.
- **COURSE_CHANGE**: a large reversal (>= 70 deg in 2 min) above `--turn-min-alt`. Not
  reported within ~35 nm of a regional airport below FL250 (all 10 replay false alarms were
  departure/arrival routings at Beirut, Amman, Damascus, TLV) nor at learned route corners
  (`turn_zones.json`: cells where >= 4 aircraft turned onto that heading, e.g. detours around
  closed airspace) - unless the flight is already hot. Holding patterns can still trigger it.
- **SHARP_TURN**: implied bank angle from the turn rate and speed >= `--max-bank` (35 deg),
  everywhere including near airports. Airliners stay below ~25-30; FZ1073's U-turn was a
  normal-rate turn (~25 deg), which is why reversals and sharpness are separate alerts.
  Positions must agree with the speeds, so GNSS glitches don't count as turns.
- **OFF_COURSE / TURNING_BACK**: long before landing, an arrival > 60 nm out flying >= 100 deg
  away from TLV for 150 s while the distance opens, or a departure flying back toward TLV
  (within 60 deg) while closing. OFF_COURSE also covers any flight with a plausible route
  flying away from its (learned-direction) destination; without the direction and corridor
  checks this gave 20 false alarms on Beirut departures in the replay. Route bends (Gulf flights via Saudi Arabia and Jordan) stayed
  below 65 deg in the replay; FZ1073 reached 165 deg.
- **TOWARD_ISRAEL**: the hijack scenario of a flight *not* bound for Israel turned toward it.
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
- **Hot flights**: any alert makes a flight followed and hot for `--hot-minutes` (20): queried
  every cycle instead of every `--follow-interval`, every anomaly reported (no terminal or
  route-corner exemptions), and two different anomalies within that time are sent at
  priority 5 with `[also: ...]`.
- **POSITION_JUMP**: implausible speed between consecutive positions **within 120 s**.
  Longer gaps are just movement while unheard. The Eastern Mediterranean has heavy GNSS
  jamming and spoofing, which causes jumps, fake turns and position dropouts.
- Cooldowns and link dates use the **data timestamp**, not wall-clock time.
- Any flight that alerts becomes **followed**, so its fate stays visible.

## Links in alerts (no local storage)

`fr24_flight` (`/data/flights/fz1073`, flight number from the board or callsign → IATA),
`fr24_aircraft` (by registration), `live_adsbx` / `live`, `replay_adsbx` / `replay`
(`?icao=<hex>&showTrace=<UTC date>`). ADS-B Exchange replays proved more reliable for older
days than airplanes.live, which showed "No data". Tapping an ntfy notification opens live ADSBx.

## Testing

No network access is assumed in tests: subclass `Monitor`, override `fetch`, and feed
scripted aircraft states. Keep or create at least these scenarios:

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

Quick live check: `python flight_watch.py --once -v`. Keep live experiments short.

## Open issues

- **Short events between polls (the FZ1073 altitude wobble, 05:21:44-49Z).** It lasted ~5 s
  while FZ1073 was 224 nm out, so it was sampled only by the 30 s follow request; `/v2/hex`
  returns only the latest state, and the flight became hot only at its first alert (the dive
  after the wobble). Detection currently works without it. Idea to return to: for hot (and
  perhaps all followed) flights, pull the last ~30 s of full-rate positions once per 30 s from
  readsb's recent-trace files (`globe.adsb.lol/data/traces/<last 2 hex>/trace_recent_<hex>.json`
  redirects to `adsb.lol`; not reachable from the cloud sandbox, untested), and feed every
  point to the checks. Budget: one extra request per hot flight per 30 s.

- **Next step: publish announcements to Bluesky.** `Announcer` already builds posts as
  segments + `root`/`reply_to`. The owner's `yuval-harpaz/astro` bots show the pattern:
  `from atproto import Client, client_utils, models`; `Client().login(os.environ['Bluehandle'],
  os.environ['Blueword'])` (app password from env, never committed); `client_utils.TextBuilder()`
  with `.text()` / `.link(label, url)`; `send_post(builder)` (or `send_post(text=builder,
  reply_to=models.AppBskyFeedPost.ReplyRef(parent=models.create_strong_ref(parent_post),
  root=models.create_strong_ref(root_post)))` for thread replies); post URL
  `https://bsky.app/profile/<handle>/post/<uri.split('/')[-1]>`. astro keeps text to ~250 chars
  ("300 limit but failed once"); posts here are <= 280 incl. link labels. Keep a map of local
  post id -> returned post refs to thread replies, log failures, and never let posting block
  the poll loop.

## Out of scope unless asked

Web pages or dashboards, databases, storing tracks, Docker-first deployment, paid APIs.
