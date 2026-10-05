# flight-watch

Early-warning monitor for irregular activity on flights to/from an airport (default TLV / LLBG),
both near the airport and anywhere en route. Polls free ADS-B feeds every 10 s and alerts on lost
contact, extreme climb/descent, sharp course changes, emergency squawks (7500/7600/7700),
GPS-spoofing-style position jumps, diversions and returns to TLV.

## How distant flights are found

1. **local** - every aircraft within `--radius` nm (default 150) of TLV.
2. **discover** - active flights on the Ben Gurion flight board (Israel Airports Authority open
   data on data.gov.il) are converted to callsigns (e.g. LY 002 -> `ELY002`) and searched
   worldwide every 60 s, so an inbound flight from New York is picked up over the Atlantic.
3. **follow** - once a flight is confirmed as a TLV arrival/departure (flight board or route DB),
   it is tracked by its hex id every cycle, wherever it is, until it lands.

Remote alerts are tagged `[REMOTE]`. Remote lost-contact uses a longer limit
(`--lost-after-remote`, default 600 s) because volunteer coverage has big gaps over seas,
deserts and some countries.

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

Distant flights are queried on several networks (`--remote-providers`, default
`airplanes.live,adsb.lol`) because coverage outside Israel differs a lot between them.

## Data source

Default is airplanes.live (also: `--provider adsb.lol` / `adsb.fi`). These are free, need no key and
allow ~1 request/s, so 10 s polling is fine. OpenSky was not used as default: its daily credit
budget doesn't cover 8,640 requests/day.

ADS-B carries no origin/destination. Arrival/departure comes from the TLV flight board first,
then adsb.lol's crowd-sourced callsign route database (may be missing or wrong), then a heuristic
near the airport (marked `ARR?` / `DEP?`).

Flight number -> callsign mapping is imperfect: some airlines (e.g. Wizz, easyJet) use
alphanumeric callsigns that don't match the flight number. Those flights are still caught once
they enter the local radius and are then followed outbound. Unmapped airline codes are logged;
add them with `--airline-map extra.json` (`{"XX": "XXX"}`). If data.gov.il is unreachable the
script keeps running on the other layers.

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
