# Example: FZ1073, 30 Sep 2026

flydubai FZ1073 (FDB1073), Dubai → Tel Aviv, A6-FKF (B38M, hex `8965d1`): a sudden dive,
squawk 7700 then 7500 (hijack), a U-turn over Jordan and a diversion to Tabuk.

- `fz1073_alerts.jsonl`: the alerts `flight_watch.py` raises for FZ1073 when the captured real
  traffic (`tests/data/fz1073_2026-09-30.json.gz`) is replayed through it (`tests/replay.py`).
  Times are UTC; the plots show Israel time (UTC+3).
- `fz1073_3d.html`: rotatable 3D plot (km from TLV, altitude in ft) of the whole flight from
  take-off at Dubai (03:04:54 UTC), from ADS-B Exchange history (831 positions: a point whenever
  something changes, ~8 s apart, sub-second during the wobble). Alerts by group, each with its
  own colour and marker (emergency squawk, vertical rate, course, lost contact...); the "show" menu at
  the top hides any alert kind, or only its text, and the steep changes / data glitches; "alerts at" moves
  the vertical-rate markers to the first report of the trace that passes the monitor's check (the wobble
  passes it but was not alerted: the monitor saw it only at the 30 s follow cadence). Steep altitude changes in purple, stretches without
  positions dashed. Altitude and speed
  over time below, with buttons to zoom the chart to the alerts or to each steep change.
- `../docs/fz1073_map.html` (published with GitHub Pages): the same over a street map. "? how to
  move" explains the mouse; "zoom to alerts", "first sign", "top view" and "whole flight" buttons,
  altitude exaggeration slider.

Both pages show times in Israel time by default, with a switch to UTC or the viewer's own time zone,
and metric units (m, km, km/h, m/s); the monitor's alert texts are converted too. Credits for the
map tiles (Esri), the flight data (ADS-B Exchange) and the route sources are at the bottom.

The pages load plotly.js / deck.gl from CDNs and map tiles from Esri, so they need a connection.
The track data is embedded.

| UTC | Alert |
|---|---|
| 05:21:44 | *(no alert)* first sign: altitude 10,013 → 9,632 → 10,203 m in 6 s, ground speed 815 → 704 km/h. Purple in the plots |
| 05:22:13 | VERTICAL_RATE: descent -108 m/s (-28°) at 8,519 m, backed by the altitude history (-41 m/s) |
| 05:31:28 | EMERGENCY: squawk 7700, after 9 min without positions (back at 4,572 m) |
| 05:35:20 | EMERGENCY: squawk 7500 (hijack) |
| 05:42:42 | COURSE_CHANGE: track 298° → 213° in 127 s at 4,572 m |
| 05:45:30 | OFF_COURSE: heading 104°, 169° away from TLV, 261 km out and opening |
| 05:53:33 | LOST_CONTACT: last seen at 4,580 m, 30.7539 N 38.0642 E, ~287 km north of Tabuk |

The landing at Tabuk is not in the recorded data, hence LOST_CONTACT rather than DIVERSION.
The recorded dive is 10,203 → 8,519 m in 24 s; then 8.5 min without positions until 4,580 m.
There is also a 27-min gap at 10,400 m over Saudi Arabia (04:48-05:15 UTC) and a one-point altitude
glitch at 03:28 UTC (pale purple: the aircraft's own vertical rate was 0 while the reported altitude
stepped 586 m, so the plots show it as a data glitch).

Regenerate (the alerts file from the replay, then the plots):

```bash
python -c "
import sys, json; sys.path.insert(0, 'tests'); import replay
fx = replay.load(); alerts, *_ = replay.run(fx)
open('examples/fz1073_alerts.jsonl', 'w').writelines(
    json.dumps(a, ensure_ascii=False) + '\n' for a in alerts if a['hex'] in fx['meta']['focus'])"
python tools/plot_alert.py --alerts examples/fz1073_alerts.jsonl --pick 6 --out examples/fz1073_3d.html
python tools/plot_alert.py --alerts examples/fz1073_alerts.jsonl --pick 6 --map --out docs/fz1073_map.html
```
