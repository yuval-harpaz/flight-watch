# Example: FZ1073, 30 Sep 2026

flydubai FZ1073 (FDB1073), Dubai → Tel Aviv, A6-FKF (B38M, hex `8965d1`): a sudden dive,
squawk 7700 then 7500 (hijack), a U-turn over Jordan and a diversion to Tabuk.

- `fz1073_alerts.jsonl`: the alerts `flight_watch.py` raises for FZ1073 when the captured real
  traffic (`tests/data/fz1073_2026-09-30.json.gz`) is replayed through it (`tests/replay.py`).
  Times are UTC; the plots show Israel time (UTC+3).
- `fz1073_3d.html`: rotatable 3D plot (km from TLV, altitude in ft) of the whole flight from
  take-off at Dubai (03:04:54 UTC), from ADS-B Exchange history (831 positions: a point whenever
  something changes, ~8 s apart, sub-second during the wobble). Alerts in red with the squawk
  code, steep altitude changes in purple, stretches without positions dashed. Altitude and speed
  over time below, with buttons to zoom the chart to the alerts or to each steep change.
- `fz1073_map.html`: the same over a street map. "? how to move" explains the mouse; "zoom to
  alerts", "top view" and "whole flight" buttons, altitude exaggeration slider.

The pages load plotly.js / deck.gl from CDNs and map tiles from Esri, so they need a connection.
The track data is embedded.

| UTC | Alert |
|---|---|
| 05:21:44 | *(no alert)* first sign: altitude 32,850 → 31,600 → 33,475 ft in 6 s, ground speed 440 → 380 kt. Purple in the plots |
| 05:22:04 | VERTICAL_RATE: descent -19,456 ft/min (-26°) at 32,325 ft |
| 05:31:28 | EMERGENCY: squawk 7700, after 9 min without positions (back at 15,000 ft) |
| 05:35:20 | EMERGENCY: squawk 7500 (hijack) |
| 05:42:42 | COURSE_CHANGE: track 298° → 213° in 127 s at 15,000 ft |
| 05:45:30 | OFF_COURSE: heading 104°, 169° away from TLV, 141 nm out and opening |
| 05:50:50 | OFF_COURSE: heading 129°, 167 nm out and opening |
| 05:53:33 | LOST_CONTACT: last seen at 15,025 ft, 30.7539 N 38.0642 E, ~155 nm north of Tabuk |

The landing at Tabuk is not in the recorded data, hence LOST_CONTACT rather than DIVERSION.
The recorded dive is 33,475 → 27,950 ft in 24 s; then 8.5 min without positions until 15,025 ft.
There is also a 27-min gap at FL340 over Saudi Arabia (04:48-05:15 UTC) and a one-point altitude
glitch at 03:28 UTC (purple, harmless).

Regenerate (the alerts file from the replay, then the plots):

```bash
python -c "
import sys, json; sys.path.insert(0, 'tests'); import replay
fx = replay.load(); alerts, *_ = replay.run(fx)
open('examples/fz1073_alerts.jsonl', 'w').writelines(
    json.dumps(a, ensure_ascii=False) + '\n' for a in alerts if a['hex'] in fx['meta']['focus'])"
python tools/plot_alert.py --alerts examples/fz1073_alerts.jsonl --pick 5 --out examples/fz1073_3d.html
python tools/plot_alert.py --alerts examples/fz1073_alerts.jsonl --pick 5 --map --out examples/fz1073_map.html
```
