# flight-watch

Early-warning monitor for irregular activity on flights to/from an airport (default TLV / LLBG),
both near the airport and anywhere en route. Polls free ADS-B feeds every 10 s and alerts on lost
contact, too-steep climbs/descents, sharp turns and course reversals, emergency squawks
(7500/7600/7700), GPS-spoofing-style position jumps, arrivals flying away from TLV, departures
turning back, diversions, and flights not bound for Israel turning toward it.

## See it

- **Alerts on Telegram**: [@flight_watch_tlv](https://t.me/flight_watch_tlv), a public read-only channel where the
  monitor's bot posts its alerts as they happen, threaded per flight. These are automatic alerts from an experimental
  hobby system, and most have a routine explanation (training, approach turns, GPS jamming in the region).
- **Incidents** (Hebrew): [incidents.html](https://yuval-harpaz.github.io/flight-watch/incidents.html), real incidents
  from 2001 to today (9/11, JT610, ET302, MU5735, AS1282, FZ1073 and more) replayed through the monitor as if it had
  watched them live: which alerts it would have raised, and when.
- **TLV flight board**: [flights.html](https://yuval-harpaz.github.io/flight-watch/flights.html), Ben Gurion arrivals
  and departures from the official board, with past boards from the archive. The monitor's warnings are hidden unless
  "show warnings" is ticked (`?alerts=true`). See [Flight pages](#flight-pages-board--live-map).

## Alerts

| Kind | When | Priority |
|---|---|---|
| `EMERGENCY` | squawk 7500/7600/7700 set or changed, or an ADS-B emergency status | 5 |
| `TOWARD_ISRAEL` | a flight not bound for Israel turns >= 45 deg so that Israeli airspace is <= 12 min ahead (or is about to enter it), above 8000 ft | 5 |
| `OFF_COURSE` | a TLV arrival > 60 nm out flies >= 100 deg away from TLV for 150 s, distance opening; also any flight with a known route flying away from its destination | 5 |
| `LOST_CONTACT` / `DIVERSION` | silent too long (also low down, when last heard falling at `--vrate` or faster); a TLV arrival on the ground elsewhere, or silent while descending near another airport | 5 |
| `TURNING_BACK` / `RETURNED` | a TLV departure heads back toward TLV for 150 s while closing / lands back after reaching 40 nm or 10,000 ft | 4 |
| `VERTICAL_RATE` | flight-path angle steeper than 10 deg descending or 12 deg climbing (18 deg climbing near airports), or > 8000 ft/min | 4 |
| `ALTITUDE_WOBBLE` | an airliner's altitude up and down 3+ times (150 ft or more each way) within 3 min below 25,000 ft, its vertical rate swinging both ways (JT610, ET302) | 5 |
| `EARLY_DESCENT` | an airliner descending 1,000 ft below the height it climbed to within 10 min of take-off (AS1282) | 5 |
| `SHARP_TURN` | turn tighter than ~35 deg of bank (airliners stay below ~25-30), anywhere, also near airports | 4 |
| `COURSE_CHANGE` | >= 70 deg in 2 min above 12000 ft, new track held 2 min - not S-turns, holding patterns / orbits, routine turns near airports or learned route corners, nor a turn onto the route's destination; an aircraft with no route that is not an airliner gets one turn alert per 2 h (survey / patrol lines) | 4 |
| `SKYDIVE_PATTERN` | an airliner or large aircraft flying a jump run (slow at height) then diving; a jump plane's first dive is alerted ("probably a skydiving ride"), then nothing on it but an emergency squawk or a fall beyond `--vrate` until 3 h after its last lift | 5 |
| `HOLDING` | an airliner circling over 30 min (again every 30 min) instead of landing; military and non-airline traffic not reported | 4 |
| `GPS_SPOOFING` | aircraft reported motionless in the air (impossible for a fixed-wing plane unless falling fast) at the same point: one alert per episode (`spoof-YYYYMMDDTHHMMZ`) and one when it ends; the position-based alerts it causes are logged under that label instead | 3 |
| `MASS_SILENCE` | 3+ aircraft silent within 2 min of each other (reception, jamming, the onset of spoofing): one alert (`silence-YYYYMMDDTHHMMZ`) instead of a `LOST_CONTACT` each. Every loss now waits `--lost-confirm` (60 s) for this; heard again meanwhile = nothing | 3 |
| `GPS_DEGRADED` | 3+ aircraft report they no longer trust their GPS position (NIC 0) within 2 min and 100 nm: one alert (`gps-YYYYMMDDTHHMMZ`) and one when it ends. A NIC-0 position the aircraft could not have reached is dropped without a jump alert | 3 |
| `POSITION_JUMP` | impossible jump, reappearing too far after a gap, or one position off the track and back (GPS spoofing or bad data); not for position-only (MLAT) tracks, nor their losses of contact | 3 |
| `CONTACT_RESTORED` | an alerted loss of contact ended | 2 |

Any alert makes the flight followed and "hot" for `--hot-minutes` (20): it is then queried every
cycle wherever it is, and every further anomaly is reported. Two different anomalies on one flight
within that time are tagged `[also: ...]` and sent at priority 5.

Thresholds come from the FZ1073 replay (below): normal traffic never descended steeper than ~8 deg
or climbed steeper than ~13 deg (bizjet near TLV), and its turns implied <= 25 deg of bank; FZ1073
dived at 26-28 deg. Routine turns are learned per 0.5 deg cell and out-heading from a day of
regional traffic (`turn_zones.json`, from `tools/learn_turn_zones.py`; statistics only, no tracks),
so route corners and detours around closed airspace do not alert, while sharp turns there still do.

## Announcements

Every alert is also written as a short social-media-style post (<= 280 characters, Israel time,
`Map` / `Live` / `Replay` / `FR24` links), printed to the terminal as a feed and appended to
`posts.jsonl` (`--posts`). Posts are threaded per flight: later alerts reply to the flight's
thread for `--thread-hours` (6); a more serious event (priority 5, or squawk 7500 above all)
starts a new top-level post. Restored contact only appears as a reply. At most
`--announce-max-per-hour` (20) posts, never holding back priority 5.

```
━━ 🚨 Hijack code 7500: FZ1073 (A6-FKF, B38M) DXB → TLV - FL152, 163 nm SE of TLV. Also: steep climb/descent 08:35 IDT
   Live · Replay · FR24
  ↳ 🔄 Course reversal: FZ1073 - FL150, 130 nm SE of TLV. track 298° → 213° in 127 s. Also: emergency 08:42 IDT
  ↳ ↩️ Flying away from its destination: FZ1073 - FL150, 141 nm SE of TLV. heading 104°, 169° away from TLV 08:45 IDT
```

Each post is stored as segments (text, or link label + URL) with `root` / `reply_to` ids, so it can
be published to Bluesky as is (atproto `TextBuilder` + `ReplyRef`): with `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD`
(an app password) in the environment and `pip install atproto`, every announcement is posted there too, threaded per
flight, from a background thread that never holds up the poll (`--bluesky-skip`, `--bluesky-posts`).
`python tests/replay.py --feed` shows the FZ1073 incident as it would have been posted.

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
actually answered, so throttled requests never produce a false `LOST_CONTACT`. A remote flight
going silent at cruise altitude is only logged (a coverage gap), unless it is already alerting
or descending.

## Rate limits

The feeds' limits are undocumented and much stricter than ~1 request/s, especially from shared
cloud IPs. Each feed host gets an adaptive budget (`--rate`, default 8 requests/min, up to
`--rate-max` 60): it grows by 0.5 req/min per success and halves on HTTP 429, followed by a
cooldown of 10 s doubling per consecutive 429 (max 120 s), with ±50% jitter. The local poll keeps
its `--interval` and always comes first; follow, discovery and route lookups only use spare
budget and stop at the first 429. On a very strict IP the remote layers can starve - a warning
says so. Route lookups (standing data on GitHub) have their own, larger budget and are turned off
for the run after 3 failures in a row.

## Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python flight_watch.py --once -v                        # one poll with the full log, then exit
python flight_watch.py --log-file flight_watch.log      # quiet console, full log in a file
python flight_watch.py                                  # TLV, every 10 s, 150 nm radius; Ctrl+C stops
python flight_watch.py --airport ETM --descent-angle 8 --interval 15
python flight_watch.py --airport-traffic-only           # only flights to/from the airport
python flight_watch.py --lost-after-remote 900          # quieter remote lost-contact alerts
python flight_watch.py --ntfy-topic my-random-topic-8f3k   # phone push via ntfy app
python flight_watch.py --help                           # all thresholds
```

It runs until stopped (Ctrl+C). By default the terminal shows the first aircraft count, then one
line per alert (Israel time, flight and airline, route when known, alert type), plus errors:

```
22:52:11  watching TLV: 22 aircraft within 150 nm, 487 scheduled flights; Telegram off (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID not in the environment)
19:53:16  SHUFL (military)  POSITION_JUMP
08:22:10  FZ1073 Flydubai  Dubai (DXB) → TLV  VERTICAL_RATE
```

`-v` shows the full log instead (a status line per cycle, rate limiting, flights followed) and
the announcement feed; `-vv` adds debug detail. Alerts are appended to `alerts.jsonl` and posts to `posts.jsonl` in the current directory
(`--jsonl ''` / `--posts ''` to disable). Telegram: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
(a chat with the bot), or with `--telegram-to channel` (`monitor.sh`: `SENDTO=channel`, the default there) `TELEGRAM_CHANNEL_ID` (a channel where the
bot is an admin: `@name` or its `-100...` id; only admins can write there). `--telegram-skip`
(`TELEGRAM_SKIP`; default `LOST_CONTACT,CONTACT_RESTORED,POSITION_JUMP,GPS_DEGRADED,MASS_SILENCE,TOWARD_ISRAEL`) leaves those kinds out of Telegram;
it gets one `GPS_SPOOFING` start in 6 h and never an episode's end
unless the flight also has an alert of another kind. Each message sent is also appended to
`posts_telegram.jsonl` (`--telegram-posts`; `monitor.sh`: `posts_telegram_YYYY-MM-DD.jsonl`), `"sent": false` when Telegram refused it.
Only one monitor runs at a time: a second one exits with an error naming the first's pid (`--lock-file`, `.flight_watch.lock`; `--once` is not checked).

A flight's later messages are replies to its first one; once it lands (or 45 min after its last message) one more
reply sums it up: how it ended, its alerts, and a map link with the whole flight.

**Squawks from AeroSignals.** Every 2 min (`--aerosignals-interval`, `0` = off) the monitor reads the
[AeroSignals](https://bsky.app/profile/spotterintel.bsky.social) bot on Bluesky, which posts each 7700 / 7600 / 7500
worldwide a few minutes after it starts. A post about a flight to or from an Israeli airport is a second source:
the aircraft is looked up in the feeds by its callsigns. If the monitor already alerted its squawk, nothing more is sent.
If we have the aircraft, the alert is an EMERGENCY on it, with the post's link, our map and the rest. If it is not in our
feeds within 10 min, the post alone goes to Telegram with the FR24 link. Posts over 3 h old (after a restart) are skipped,
and the ones already sent are read back from the day's files.

## Storage and links

No flight tracks are stored. Each alert is one line in `alerts.jsonl` with links to existing sites:

- `map` - our flight map page (`docs/flight_map.html?hex=...&t=<alert time>`, on GitHub Pages; `--viewer-url`,
  `''` leaves it out): the flight of the alert whenever the link is opened - live while it flies, its track
  afterwards - on our map, on ADS-B Exchange's (embedded: live, or its replay cut to that flight), or both
  side by side; plus its board data when it is a TLV flight. `a=` lists this alert and the flight's earlier
  ones (`KIND~<seconds after t>`, joined by `.`), drawn as orange markers on the track. "analyse track" (or
  `&analyze` in the link) runs the monitor's own checks on the drawn track in the browser (`flight_watch.py`
  under Pyodide, ~10 MB the first time; a few seconds a flight) and marks every alert they raise with a purple
  ring; the button then hides / shows them.
- `fr24_flight` - Flightradar24 history for the flight number (e.g. `/data/flights/fz1073`); pick the
  date to open its playback. Free FR24 accounts only see recent history.
- `fr24_aircraft` - Flightradar24 history for the registration (works when callsign != flight number).
- `live_adsbx` / `live` - the aircraft right now on ADS-B Exchange / airplanes.live.
- `replay_adsbx` / `replay` - full track of that exact aircraft for the UTC day of the alert, with the
  aircraft placed where it was at the alert (`&timestamp=`). The same at any later time, unlike the live links.
  ADS-B Exchange keeps older days more reliably; airplanes.live may show "No data" later.

Tapping an ntfy notification opens the ADS-B Exchange live view; its buttons are
Live, Replay (ADSBx) and FR24 flight. Telegram messages read like the posts (Map · Replay · FR24; Map is live while the flight flies).

Distant flights are queried on several networks (`--remote-providers`, default `adsb.lol,adsb.fi`)
because coverage outside Israel differs between them. A network that answers 401/403 is dropped for
the rest of the run.

## Data source

Default is adsb.lol (also: `--provider adsb.fi`); when it does not answer a local poll (HTTP 429, an
error) the circle is read from [adsb.fi](https://adsb.fi) instead (`--fallback-provider`, `''` = none).
These are free and need no key; see [Rate limits](#rate-limits). adsb.fi's open data is for personal,
non-commercial use at up to 1 request a second, crediting adsb.fi
([terms](https://github.com/adsbfi/opendata)); its answers do not mark military aircraft, so its
military list is read every 10 minutes. airplanes.live is feeder-only now (HTTP 403). OpenSky was not used as default: its daily credit
budget doesn't cover 8,640 requests/day.

ADS-B carries no origin/destination. Arrival/departure comes from the TLV flight board first,
then the callsign's route in the Virtual Radar Server standing data (CC0, crowd-sourced, may be
missing or wrong; adsb.lol's own route API now redirects to it), then a heuristic near the
airport (marked `ARR?` / `DEP?`). With a local copy in `standing-data/` (git-ignored, 38 MB) it
is read from disk; without one, files are fetched per airline from GitHub on first use (at most 3
per cycle, so the poll is not held up) and kept in memory only. `--standing-data` picks another
checkout or URL. The data changes daily:

```bash
git clone --depth 1 https://github.com/vradarserver/standing-data standing-data
git -C standing-data pull --depth 1     # daily (cron / systemd timer); pulling needs no write access
```

The route is what identifies Wizz/easyJet-style alphanumeric callsigns (`WZZ3W` = Budapest ->
TLV) that never match the board. It also gives each flight a destination: `TOWARD_ISRAEL` is not
raised while a flight points at its own destination (or origin) beyond Israel, and `OFF_COURSE`
covers any flight flying away from its destination. Because routes can be stale or stored the
wrong way round, a route is ignored while the aircraft is far off the corridor between its ends,
and the direction of travel is learned from the flight itself (away from both ends) before it is
used.

Flight number -> callsign mapping is imperfect: some airlines (e.g. Wizz, easyJet) use
alphanumeric callsigns that don't match the flight number; the route data above classifies them
instead, and private flights without a route are still caught once they enter the local radius. Unmapped airline codes are logged;
add them with `--airline-map extra.json` (`{"XX": "XXX"}`). If data.gov.il is unreachable the
script keeps running on the other layers.

## Tests

```bash
python -m unittest discover -s tests
```

Offline simulations with a fake feed and clock: every alert kind (steepness by angle, sharp
turns vs routine turns, off-course, turning back, toward-Israel, lost contact / probable landing,
diversion of a distant arrival), codeshare merging, discovery order, board paging, follow
cadence, route lookups, announcements and threading, and 429 handling.

### Incident replays

`tests/data/` holds real traffic captured around past incidents; the real `Monitor` replays it
every 10 s through simulated `/point`, `/hex` and `/callsign` answers, so thresholds and logic
can be checked against what actually happened:

- `fz1073_2026-09-30.json.gz` - FZ1073 (A6-FKF, DXB->TLV) over Jordan, 05:00-06:15 UTC: sudden
  descent from FL340, 8.5 min silence, squawk 7700 then 7500, U-turn 130 nm from TLV, lost heading
  south-east. 161 other aircraft (150 nm circle + flights to/from TLV) for false-alarm counting.
- `fr4978_2021-05-23.json.gz` - Ryanair FR4978 (RYR1TZ, SP-RSM, Athens->Vilnius) forced to land at
  Minsk: squawk 7700 over Belarus, U-turn, off course, 9 min silent in the descent. 55 other aircraft
  within 150 nm of Vilnius. Expected: EMERGENCY, COURSE_CHANGE, OFF_COURSE, LOST_CONTACT, DIVERSION.
- `as1282_2024-01-06.json.gz` - Alaska AS1282 (N704AL) door plug blown out at 16,000 ft after
  leaving Portland, back on the ground 13 min later; no emergency squawk in ADS-B and never more than
  29 nm out, so RETURNED on landing is the only alert. 75 other aircraft within 60 nm.
- `mu5735_2022-03-21.json.gz` - China Eastern MU5735 (B-1791) dive from FL291, from Flightradar24's
  published granular data (no ADS-B Exchange coverage there; imported with `tools/import_track.py`).
  Expected: VERTICAL_RATE, and LOST_CONTACT although the last report is at 3,225 ft.

Incidents away from TLV are watched from their own point (`airport_coords` in the fixture's
`meta`, passed to the monitor as `--lat` / `--lon`); the Israel-only checks are off there.

```bash
python tests/replay.py                          # alert timeline (* = the incident aircraft)
python tests/replay.py --descent-angle 8 --max-bank 30   # try other thresholds (any option)
python tests/replay.py tests/data/fr4978_2021-05-23.json.gz   # another incident
```

`tests/test_replay_fz1073.py` and `tests/test_replay_incidents.py` require the incidents' alerts and
cap alerts on the other flights (`NOISE_BUDGET`).

**Capture** another incident with `tools/capture_incident.py` (see its `--help`); it uses the ADS-B
Exchange globe history (5 s positions worldwide since May 2020; earlier days answer 404), so it brings
the surrounding traffic too. Board rows are derived from the traces plus `--board` rows given by hand;
the board as it was on a past day (since 10 Apr 2026) is in the over.org.il archive (see Flight pages)
and could replace the hand-written rows. Elsewhere than at a known airport, give the watched point
(`--airport VNO --icao EYVI --lat 54.6341 --lon 25.2858`), not where the aircraft came down: losses
near the "airport" read as landings. `--find RYR1TZ` lists the hex ids that sent a callsign in the
window. Routes come from today's route data and callsigns are reused (RYR1TZ is Brussels-Rome now):
give the incident flight's route of the day with `--route RYR1TZ=LGAV-EYVI`.

```bash
python tools/capture_incident.py --date 2021-05-23 --start 09:00 --end 10:30 --find RYR1TZ
python tools/capture_incident.py --date 2021-05-23 --start 09:35 --end 10:20 --airport VNO --icao EYVI \
    --lat 54.6341 --lon 25.2858 --before-h 0.5 --after-h 0.5 --focus 48c22c \
    --route RYR1TZ=LGAV-EYVI --name "FR4978 ..." --out tests/data/fr4978_2021-05-23.json.gz
```

**Import** one aircraft's track from a file with `tools/import_track.py`, for what ADS-B Exchange does
not have (before 2020, no receivers there, data others published): Flightradar24's granular or
downloaded CSV, KML (gx:Track or time-stamped placemarks, e.g. FlightAware's export), or any CSV with
time, lat, lon and optionally altitude, speed, track, vertical rate, squawk, callsign. Missing speeds,
tracks and rates are worked out from the positions (radar plots have none); a transponder switched
off is where the points stop. `--add-to` puts several aircraft in one fixture (e.g. four flights of
one day). Only the incident aircraft is in such a fixture, so it tests detection, not false alarms.

```bash
python tools/import_track.py granular.csv --name "MU5735 ..." --reg B-1791 --type B738 \
    --route ZPPP-ZGGG --out tests/data/mu5735_2022-03-21.json.gz
python tools/import_track.py radar_ua175.csv --callsign UAL175 --add-to tests/data/some_day.json.gz
```

## Plot an alert

```bash
python tools/plot_alert.py                       # all alerts, newest first, in a pager (q quits); pick one
python tools/plot_alert.py --filter LY347        # only rows containing LY347 (any case; several words: all)
python tools/plot_alert.py --filter RETURNED Zurich
python tools/plot_alert.py --pick 1 --minutes 120   # 2 h around the alert, across landings and gaps
python tools/plot_alert.py --pick 1 --map        # over a street map instead of km axes
python tools/plot_alert.py --pick 1 --sources    # GPS (ADS-B) and MLAT positions as two lines
python tools/plot_alert.py --fixture tests/data/fr4978_2021-05-23.json.gz --map   # an incident, offline
```

With `--fixture` nothing is fetched: the fixture is replayed through the monitor, its alerts are
listed (the incident aircraft's first one is plotted unless `--pick` / `--filter` say otherwise) and
the positions come from the fixture; the km axes are measured from its watched point.

Fetches the aircraft's full-rate trace (adsb.lol for the last day, plus its "recent" trace for the
latest minutes, else the ADS-B Exchange history of that UTC day) and writes `tmp_plot.html`
(git-ignored). List numbers are those of the full list, also when filtered; the row shows the
flight number and callsign (`LY347/ELY347`), airline, route and alert, so any of them can be filtered.

- **3D (default):** rotatable plotly chart in km east / north of TLV (`--airport`), altitude in ft;
  the first view is at least 100 km across and 0-40,000 ft, so a short track looks short.
- **`--map`:** the same over a street map (deck.gl; Esri World Street Map tiles, `--tiles` for
  another `{z}/{x}/{y}` URL; tile.openstreetmap.org blocks pages opened from a file). Drag to pan,
  right-drag or Ctrl+drag to tilt and rotate, scroll to zoom toward the middle of the view at the
  alerts' height (the track is drawn high above the map, so zooming toward the ground would fly
  under it); "? how to move" explains it all, and buttons fly to the alerts or the first sign.
  Altitude exaggeration slider.

Both show the track coloured by time, ADS-B and MLAT positions, the ground track, nearby airports
(and Israel's outline in 3D), position jumps (orange, the monitor's `POSITION_JUMP` rules), altitude
changing faster than 8,000 ft/min (purple; the one in the 10 min before the first alert is labelled
"first sign" - FZ1073's wobble 20 s before its dive alert), stretches without positions (dashed
grey) and every alert on the flight (red, labelled, with the squawk code). Buttons zoom to the
alerts or back to the whole flight; the map has a "? how to move" help and a top view. Below: altitude and ground
speed over time, alerts / jumps / steep changes / gaps marked, with buttons to zoom it to the alerts
or to each steep change; hovering or clicking it puts a black marker on that position above.
Times are Israel time with a switch to UTC or the viewer's zone; units are metric (m, km, km/h,
m/s), including the alert texts. Credits (map tiles, flight data, route sources) are at the bottom.
`examples/` has FZ1073 (30 Sep 2026) in 3D; its map is `docs/fz1073_map.html` (GitHub Pages). By default the plot covers the alert's flight leg (between ground stops or 30 min
silences); `--minutes` widens it. Nothing else is stored.

## Flight pages (board + live map)

`docs/flights.html` lists Ben Gurion arrivals and departures from the live flight board
(data.gov.il, fetched by the page itself, refreshed every 5 minutes): scheduled and estimated
time, delay, the board's status plus a plain one (delayed landing, late departure, not landed /
not departed N min past the estimate, landed late, cancelled), terminal and check-in counters.
"show" picks now (−3 h … +12 h, the default), any single day the board still holds (it keeps
about a day back and a few days ahead) or everything; filter by direction, search, hide
completed flights; Israel time with a UTC / own-zone switch.

"board: as it was at…" shows the board at any past moment since 10 Apr 2026 (Israel time), and
`flights.html?at=2026-09-30T08:45` links to one. The page rebuilds it in the browser from the
archive of [גרסאות לעם / over.org.il](https://www.over.org.il/versions/31c812a6-9b0c-4f32-8317-e5f268c28f60),
which records every change of every board row (checked about every 15 minutes); nothing is
stored here. Loading takes ~10 s (four pages of 1,000 rows). The archive does not record when a
row left the board, so a past board covers flights scheduled from a day before to three days
after that moment. Map links are hidden for a past board, as the map is live.

Clicking a flight opens `docs/flight_map.html` for it: today's track, the route (great circle
between the two airports), where it will be in 5 minutes at its speed, altitude (m), speed
(km/h), vertical speed, squawk, distance and time to TLV, updated every 10 s. Opened without a
flight it shows the arrival closest to landing (the shortest time to TLV among airborne
arrivals; one already on the ground is skipped) and moves on to the next one after it lands.
"next arrival" skips to the next, "← flight list" goes back, "↻ refresh" asks for the latest data
now, and "ADS-B Exchange ↗" / "FR24 ↗" open the flight live on those sites. When the live feed is
busy (adsb.lol answers 429 to the relay), the map shows our data so far: the aircraft's track from
adsb.lol's trace files up to its last point, with its time. `flight_map.html?hex=<icao hex>`
shows one aircraft, also one that is not on the board (alert posts link this way). The live feed is
not asked for a cancelled flight, an arrival landed over 30 minutes ago, or a flight hours from its
time: the page then shows the board data alone.

Live positions come from adsb.lol, which does not allow other web sites to read it (no CORS
headers), so the map needs the local helper, which serves the pages and relays the feed with the
monitor's request budget and 429 backoff:

```bash
python tools/serve.py          # then open http://localhost:8765/  (Ctrl+C stops)
```

The flight list also works straight from GitHub Pages or a file. The map there shows the board
data and links (ADS-B Exchange live, FR24) with a short note, as the browser refuses the feed; it
is live through the HTTPS relay named by `FB.RELAY` in `docs/flightboard.js`, which adds CORS:
`tools/cors_worker.js`, a Cloudflare Worker (free plan; paste it into a new Worker, see its header),
or `tools/serve.py` on a server (`--host 0.0.0.0` behind an HTTPS proxy). The worker relays only
the feed paths the pages use, for the GitHub Pages and localhost origins, with a 5-30 s cache,
and serves its last answer of up to 2 minutes when adsb.lol answers 429. With a KV namespace
(binding `CALLSIGNS`; no Cron Trigger needed) it also keeps which aircraft flew each
callsign near TLV in the last 36 hours, so the map shows the flown track of a landed flight.
adsb.lol refuses the worker's own requests, so a GitHub workflow fills it every 10 minutes
(`.github/workflows/share_callsigns.yml`): set one random value as the worker's secret
`LEARN_TOKEN` and as the repository secret `RELAY_TOKEN` (Settings -> Secrets and variables ->
Actions). The monitor also sends what it hears when it runs with `RELAY_TOKEN` in its environment
(`--share-interval`, `--share-url ''` to stop). The worker's `/status` shows the last send. `docs/flightboard.js` holds the logic both pages share; it
mirrors the monitor's flight-board code (codeshares, operating carrier, callsigns, Israel time)
and `tests/test_pages.py` runs it under node against the Python to keep them identical.
Alerts are not shown on the pages yet.

## Caveats

- GNSS jamming/spoofing is common in the Eastern Mediterranean. It causes position jumps, fake
  course changes, and aircraft dropping out of position-based feeds — so many `LOST_CONTACT`
  alerts here will be interference, not a real loss. Real ATC radar contact is not visible to you.
- Coverage depends on volunteer receivers; low altitudes and areas over neighbouring countries
  are patchy. Tune `--lost-min-alt` and `--edge-margin` accordingly.
- A holding pattern with legs of 2 min or more can still give one `COURSE_CHANGE` on entry
  (`--turn-confirm` sets how long the new track must hold).

## Hosting (GitHub Actions cron is ≥5 min and often delayed)

Run it as a long-lived process from its venv: in `tmux` while testing, later as a `systemd` service
on a VPS (`ExecStart=/path/to/flight-watch/.venv/bin/python flight_watch.py`, `Restart=always`,
`WorkingDirectory=` where `alerts.jsonl` / `posts.jsonl` should go). The console lines go to the
journal (`journalctl -u flight-watch`); `--log-file flight_watch.log` also keeps the full log.
The server only ever pulls (this repo and `standing-data/`); nothing it writes is pushed.

Network use is small: measured compressed sizes are ~2 KB per local poll at night (more aircraft
by day), ~30 KB per flight-board refresh (10 min), under 1 KB per follow/search request: roughly
20-80 MB a day, most of it the 10-second local poll.
Cheap always-on options: Oracle Cloud free-tier VM, a ~€4/month Hetzner VPS, Fly.io or Railway.
A Raspberry Pi with an RTL-SDR receiver gives your own local feed with zero API dependency.
