# flight-watch

Early-warning monitor for irregular activity on flights to/from an airport (default TLV / LLBG),
both near the airport and anywhere en route. Polls free ADS-B feeds every 10 s and alerts on lost
contact, extreme climb/descent, sharp course changes, emergency squawks (7500/7600/7700),
GPS-spoofing-style position jumps, diversions and returns to TLV.

## How distant flights are found

1. **local** - every aircraft within `--radius` nm (default 150) of TLV.
2. **discover** - active flights on the Ben Gurion flight board (Israel Airports Authority open
   data on data.gov.il) are converted to callsigns (e.g. LY 002 -> `ELY002`) and searched
   worldwide in rounds every `--discovery-interval` (180 s), so an inbound flight from New York
   is picked up over the Atlantic. Codeshare rows (LY25 / DL7441) are merged into one flight and
   only the operating carrier's callsign is searched first (lowest flight number; the others are a
   fallback). Arrivals due soon and departures that left within 3 h are searched first; departures
   still at the gate (the local poll sees them) or gone more than `--departed-max-h` (6 h) are not
   searched.
3. **follow** - once a flight is confirmed as a TLV arrival/departure (flight board or route DB),
   it is tracked by its hex id wherever it is, until it lands: one combined request for all
   followed flights outside the local circle every `--follow-interval` (30 s). Light aircraft and
   registration callsigns (e.g. `4XHSC`) are never followed on a guess.

Remote alerts are tagged `[REMOTE]`. Remote lost-contact uses a longer limit
(`--lost-after-remote`, default 600 s) because volunteer coverage has big gaps over seas,
deserts and some countries. Silence is only counted up to the last follow request that was
actually answered, so throttled requests never produce a false `LOST_CONTACT`.

## Rate limits

The feeds' limits are undocumented and much stricter than ~1 request/s, especially from shared
cloud IPs. Each feed host gets an adaptive budget (`--rate`, default 8 requests/min, up to
`--rate-max` 60): it grows by 0.5 req/min per success and halves on HTTP 429, followed by a
cooldown of 10 s doubling per consecutive 429 (max 120 s), with ±50% jitter. The local poll keeps
its `--interval` and always comes first; follow, discovery and route lookups only use spare
budget and stop at the first 429. On a very strict IP the remote layers can starve - a warning
says so. adsb.lol's route database lookup is turned off after 3 failures in a row.

## Run

```bash
pip install -r requirements.txt
python flight_watch.py                                  # TLV, every 10 s, 150 nm radius
python flight_watch.py --airport ETM --vrate 3000 --interval 15
python flight_watch.py --airport-traffic-only           # only flights to/from the airport
python flight_watch.py --lost-after-remote 900          # quieter remote lost-contact alerts
python flight_watch.py --ntfy-topic my-random-topic-8f3k   # phone push via ntfy app
python flight_watch.py --help                           # all thresholds
```

Telegram: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` env vars. Alerts are also appended to `alerts.jsonl`.

## Storage and links

No flight tracks are stored. Each alert is one line in `alerts.jsonl` with links to existing sites:

- `fr24_flight` - Flightradar24 history for the flight number (e.g. `/data/flights/fz1073`); pick the
  date to open its playback. Free FR24 accounts only see recent history.
- `fr24_aircraft` - Flightradar24 history for the registration (works when callsign != flight number).
- `live_adsbx` / `live` - the aircraft right now on ADS-B Exchange / airplanes.live.
- `replay_adsbx` / `replay` - full track of that exact aircraft for the UTC day of the alert.
  ADS-B Exchange keeps older days more reliably; airplanes.live may show "No data" later.

Tapping an ntfy notification opens the ADS-B Exchange live view; its buttons are
Live, Replay (ADSBx) and FR24 flight. Telegram messages list all links.

Distant flights can be queried on several networks (`--remote-providers`, default `adsb.lol`;
e.g. `adsb.lol,adsb.fi`) because coverage outside Israel differs between them. A network that
answers 401/403 is dropped for the rest of the run.

## Data source

Default is adsb.lol (also: `--provider adsb.fi`). These are free and need no key; see
[Rate limits](#rate-limits). airplanes.live is feeder-only now (HTTP 403). OpenSky was not used as default: its daily credit
budget doesn't cover 8,640 requests/day.

ADS-B carries no origin/destination. Arrival/departure comes from the TLV flight board first,
then adsb.lol's crowd-sourced callsign route database (may be missing or wrong), then a heuristic
near the airport (marked `ARR?` / `DEP?`).

Flight number -> callsign mapping is imperfect: some airlines (e.g. Wizz, easyJet) use
alphanumeric callsigns that don't match the flight number. Those flights are still caught once
they enter the local radius and are then followed outbound. Unmapped airline codes are logged;
add them with `--airline-map extra.json` (`{"XX": "XXX"}`). If data.gov.il is unreachable the
script keeps running on the other layers.

## Tests

```bash
python -m unittest discover -s tests
```

Offline simulations with a fake feed and clock: alerts, codeshare merging, discovery order,
board paging, follow cadence, and 429 handling.

### Incident replays

`tests/data/` holds real traffic captured around past incidents; the real `Monitor` replays it
every 10 s through simulated `/point`, `/hex` and `/callsign` answers, so thresholds and logic
can be checked against what actually happened:

- `fz1073_2026-09-30.json.gz` - FZ1073 (A6-FKF, DXB->TLV) over Jordan, 05:00-06:15 UTC: sudden
  descent from FL340, 8.5 min silence, squawk 7700 then 7500, U-turn 130 nm from TLV, lost heading
  south-east. 161 other aircraft (150 nm circle + flights to/from TLV) for false-alarm counting.

```bash
python tests/replay.py                          # alert timeline (* = the incident aircraft)
python tests/replay.py --vrate 6000 --turn 90   # try other thresholds (any flight_watch option)
```

`tests/test_replay_fz1073.py` requires the incident's alerts and caps alerts on the other
flights (`NOISE_BUDGET`). Capture another incident with `tools/capture_incident.py` (see its
`--help`); it uses the ADS-B Exchange globe history. The flight board of a past day is not
online, so board rows are derived from the traces plus `--board` rows given by hand.

## Caveats

- GNSS jamming/spoofing is common in the Eastern Mediterranean. It causes position jumps, fake
  course changes, and aircraft dropping out of position-based feeds — so many `LOST_CONTACT`
  alerts here will be interference, not a real loss. Real ATC radar contact is not visible to you.
- Coverage depends on volunteer receivers; low altitudes and areas over neighbouring countries
  are patchy. Tune `--lost-min-alt` and `--edge-margin` accordingly.
- Holding patterns can trigger `COURSE_CHANGE`; raise `--turn-min-alt` or `--turn` if noisy.

## Hosting (GitHub Actions cron is ≥5 min and often delayed)

Run it as a long-lived process: locally with `systemd`/`tmux`, or `docker build -t flight-watch . && docker run -d --restart=always flight-watch`.
Cheap always-on options: Oracle Cloud free-tier VM, a ~€4/month Hetzner VPS, Fly.io or Railway.
A Raspberry Pi with an RTL-SDR receiver gives your own local feed with zero API dependency.
