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
  `alerts.jsonl` with **links to existing sites** (FR24, ADS-B Exchange, airplanes.live).
- Report findings and proposals **before** large changes; deliver changes as pull requests.
- Never commit secrets (ntfy topic, Telegram token) or `alerts.jsonl`; they come from CLI
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
then the adsb.lol callsign route DB, then a heuristic near the airport (`ARR?` / `DEP?`).

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
- Flight-board quirks: codeshare rows (e.g. DAL7441 on an El Al flight) never transmit, so
  search one callsign per physical flight. Departures stay "DEPARTED" long after landing.
  The board has >3000 rows, so paginate.

## Alert logic and why

- **LOST_CONTACT**: silent ≥ `--lost-after` (60 s) locally, `--lost-after-remote` (600 s)
  for followed flights outside the circle, where coverage has large gaps. Ignored below
  `--lost-min-alt`, near the airport (landing), and near the circle edge for unfollowed
  aircraft (simply leaving coverage). Skipped entirely after our own outage or when the
  feed suddenly returns far fewer aircraft (feed glitch, not mass disappearance).
- **DIVERSION**: a TLV arrival on the ground far from TLV, **or** going silent while
  descending low and far away (low-altitude coverage is poor, so the landing is often
  never seen). **RETURNED**: a TLV departure landing back at TLV after leaving.
- **VERTICAL_RATE**: the reported rate must be backed by the altitude history (rejects
  single bad values). Climb and descent have separate cooldowns, and a reading ≥1.5× the
  last alerted severity bypasses the cooldown. Without this, a small wobble suppressed the
  real 14,000 ft dive in the FZ1073 test.
- **EMERGENCY**: alerts on squawk *transition* into 7500/7600/7700, not every cooldown.
- **COURSE_CHANGE**: only above `--turn-min-alt` and away from the airport (approach turns
  are normal). Holding patterns can still trigger it.
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
- **429 handling**: rate-limited responses back off without stopping the local poll and
  without permanently marking a provider or endpoint as unsupported.

Quick live check: `python flight_watch.py --once -v`. Keep live experiments short.

## Out of scope unless asked

Web pages or dashboards, databases, storing tracks, Docker-first deployment, paid APIs.
