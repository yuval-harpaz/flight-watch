#!/usr/bin/env python3
"""One flight as a picture, for its Telegram update (Monitor.telegram_followups, --telegram-images): the owner wants a record
that does not depend on any site's history, 3 Nov 2026. The track coloured by altitude, the alerts where they were raised, an
altitude strip, a key. Stretches with no report for > GAP_S are dashed: a straight guess, not a track.

The map is drawn from tools/basemap.json.gz (Natural Earth, public domain; tools/make_basemap.py), no tile requests: Esri's
terms do not speak of automated pictures in a public channel, and OpenStreetMap's tile policy is for people viewing a map.
The track is adsb.lol's (ODbL: credited on the picture). Not ADS-B Exchange's: it sells its data, this is posted publicly.
Needs Pillow, imported here only, so neither the monitor's checks nor the map's Pyodide depend on it.

  python tools/flight_image.py <hex> <unix time in the flight> out.jpg "Title" "Subtitle"
"""
from __future__ import annotations

import gzip
import io
import json
import math
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

FT_M = 0.3048
GAP_S = 120
LEG_GAP_S, LEG_GAP_KT = 600, 200  # a gap the aircraft spent stopped: FB.legs in docs/flightboard.js
W, H, SS, TOP, STRIP = 1280, 900, 2, 92, 196
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf"
BG, SEA, LAND, BORDER, TEXT, DIM, RED = (22, 28, 38), (205, 224, 236), (243, 240, 230), (165, 158, 145), (255, 255, 255), (200, 210, 222), (225, 30, 40)
RAMP = [(0, (230, 60, 30)), (3000, (240, 150, 20)), (6000, (40, 170, 90)), (9000, (20, 130, 200)), (13000, (90, 30, 170))]
TZ = ZoneInfo("Asia/Jerusalem")
TRACES = "https://adsb.lol/data/traces/{xx}/trace_{which}_{hex}.json"


def hm(t: float) -> str:
    return datetime.fromtimestamp(t, TZ).strftime("%H:%M")


def colour(m: float) -> tuple:
    for (a, ca), (b, cb) in zip(RAMP, RAMP[1:]):
        if m <= b:
            f = max(0, (m - a) / (b - a))
            return tuple(round(x + (y - x) * f) for x, y in zip(ca, cb))
    return RAMP[-1][1]


def read_trace(hexid: str, get) -> list:
    """(time, lat, lon, feet or "ground") of the aircraft's last day: trace_full, and trace_recent for its last minutes."""
    rows = {}
    for which in ("full", "recent"):
        tr = get(TRACES.format(xx=hexid[-2:], which=which, hex=hexid))
        for r in tr.get("trace", []):
            if r[1] is not None and r[2] is not None and r[3] is not None:
                rows[tr["timestamp"] + r[0]] = (tr["timestamp"] + r[0], r[1], r[2], r[3])
    return [rows[k] for k in sorted(rows)]


def nm(a: tuple, b: tuple) -> float:
    la, lb = math.radians(a[1]), math.radians(b[1])
    h = math.sin((lb - la) / 2) ** 2 + math.cos(la) * math.cos(lb) * math.sin(math.radians(b[2] - a[2]) / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(h))


def leg_at(rows: list, t: float) -> list:
    """The flight at time t, with the ground report that ends it: (time, lat, lon, metres). FB.flightAt's legs, nearest t."""
    legs, leg = [], []
    for r in rows:
        q = leg[-1] if leg else None
        stopped = q and r[0] - q[0] > LEG_GAP_S and nm(q, r) / ((r[0] - q[0]) / 3600) < LEG_GAP_KT
        if r[3] == "ground":
            if q:
                legs.append(leg + [r])
            leg = []
        elif stopped:
            legs.append(leg)
            leg = [r]
        else:
            leg.append(r)
    if leg:
        legs.append(leg)
    legs = [x for x in legs if len(x) > 1]
    off = lambda x: x[0][0] - t if t < x[0][0] else t - x[-1][0] if t > x[-1][0] else 0  # noqa: E731
    best = min(legs, key=off, default=None)
    if best is None or off(best) > 1800:
        return []
    return [(r[0], r[1], r[2], 0 if r[3] == "ground" else r[3] * FT_M) for r in best]


def position_at(pts: list, t: float) -> tuple:
    return min(pts, key=lambda p: abs(p[0] - t))[1:3]


def group_alerts(alerts: list) -> list:
    """(time, text) of alerts within 3 min of each other as one label: "10:32  course reversal, off course"."""
    out = []
    for name, t in sorted(alerts, key=lambda a: a[1]):
        if out and t - out[-1][1] <= 180:
            if name not in out[-1][0]:
                out[-1][0].append(name)
        else:
            out.append([[name], t])
    return [(t, f"{hm(t)}  {', '.join(names)}") for names, t in out]


def basemap() -> dict:
    with gzip.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "basemap.json.gz"), "rt", encoding="utf-8") as f:
        return json.load(f)


def render(pts: list, alerts: list, title: str, sub: str, source: str = "adsb.lol (ODbL)") -> bytes:
    """A JPEG of the flight. pts: leg_at's; alerts: (name, time)."""
    from PIL import Image, ImageDraw, ImageFont
    def font(size, bold=False):
        try:
            return ImageFont.truetype(FONT % ("-Bold" if bold else ""), size * SS)
        except OSError:  # (no DejaVu on this machine)
            return ImageFont.load_default(size * SS)
    mh = H - TOP - STRIP
    mx = lambda lon: math.radians(lon)  # noqa: E731
    my = lambda lat: math.asinh(math.tan(math.radians(max(-85, min(85, lat)))))  # noqa: E731
    xs, ys = [mx(p[2]) for p in pts], [my(p[1]) for p in pts]
    span = max(max(xs) - min(xs), 0.03) / (W - 220), max(max(ys) - min(ys), 0.03) / (mh - 170)
    scale = 1 / max(span)  # pixels a radian
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    px = lambda lat, lon: ((W / 2 + (mx(lon) - cx) * scale) * SS, (TOP + mh / 2 - (my(lat) - cy) * scale) * SS)  # noqa: E731
    img = Image.new("RGB", (W * SS, H * SS), SEA)
    d = ImageDraw.Draw(img, "RGBA")
    data = basemap()

    def ring(flat):
        pp = [px(flat[i + 1], flat[i]) for i in range(0, len(flat), 2)]
        x, y = [p[0] for p in pp], [p[1] for p in pp]
        return pp if max(x) > 0 and min(x) < W * SS and max(y) > TOP * SS and min(y) < (H - STRIP) * SS else None

    for layer, fill in (("land", LAND), ("holes", SEA)):
        for flat in data[layer]:
            pp = ring(flat)
            if pp and len(pp) > 2:
                d.polygon(pp, fill=fill)
    for flat in data["borders"]:
        pp = ring(flat)
        if pp:
            d.line(pp, fill=BORDER + (255,), width=2 * SS)
    line = [px(p[1], p[2]) for p in pts]
    # alerts' labels, and places where they fit: the track and the labels are in the way
    busy = [(x - 8 * SS, y - 8 * SS, x + 8 * SS, y + 8 * SS) for x, y in line[::max(1, len(line) // 400)]]
    hit = lambda a, b: a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]  # noqa: E731
    f_label, labels = font(17, True), []
    for t, text in group_alerts(alerts):
        lat, lon = position_at(pts, t)
        x, y = px(lat, lon)
        l, u, r, b = d.textbbox((0, 0), text, font=f_label)
        w, h = r - l + 16 * SS, b - u + 14 * SS
        best = None
        for dist in (34, 70, 120):
            for ang in range(0, 360, 30):
                cxl = x + math.cos(math.radians(ang)) * (dist * SS + w / 2)
                cyl = y + math.sin(math.radians(ang)) * (dist * SS + h / 2)
                box = (cxl - w / 2, cyl - h / 2, cxl + w / 2, cyl + h / 2)
                inside = box[0] > 8 * SS and box[2] < (W - 8) * SS and box[1] > (TOP + 8) * SS and box[3] < (H - STRIP - 8) * SS
                cost = (0 if inside else 1000) + sum(hit(box, q) for q in busy) + dist / 1000
                if best is None or cost < best[0]:
                    best = (cost, box)
        busy.append(best[1])
        labels.append((x, y, best[1], text, (l, u)))
    # places, largest first, where nothing else is
    f_place, shown = font(13), 0
    for name, lat, lon, _rank in data["places"]:
        x, y = px(lat, lon)
        if not (0 < x < W * SS and TOP * SS < y < (H - STRIP) * SS):
            continue
        w = d.textlength(name, font=f_place)
        box = (x - 5 * SS, y - 10 * SS, x + w + 12 * SS, y + 10 * SS)
        if any(hit(box, q) for q in busy) or box[2] > W * SS or box[1] < TOP * SS or box[3] > (H - STRIP) * SS:
            continue
        d.ellipse((x - 3 * SS, y - 3 * SS, x + 3 * SS, y + 3 * SS), fill=(90, 90, 90, 255))
        d.text((x + 7 * SS, y - 9 * SS), name, font=f_place, fill=(70, 70, 70, 255))
        busy.append(box)
        shown += 1
        if shown >= 22:
            break

    def dashed(a, b, colour_, width):
        n = max(2, int(math.dist(a, b) / (14 * SS)))
        for i in range(0, n, 2):
            d.line([(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n),
                    (a[0] + (b[0] - a[0]) * (i + 1) / n, a[1] + (b[1] - a[1]) * (i + 1) / n)], fill=colour_, width=width)

    d.line(line, fill=(255, 255, 255, 235), width=9 * SS, joint="curve")
    for a, b, p, q in zip(line, line[1:], pts, pts[1:]):
        d.line([a, b], fill=colour(p[3]) + (255,), width=5 * SS)
        if q[0] - p[0] > GAP_S:
            dashed(a, b, (255, 255, 255, 255), 5 * SS)
    for x, y in (line[0], line[-1]):
        r = 7 * SS
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, 255), outline=(20, 20, 20, 255), width=2 * SS)
    for x, y, box, text, (l, u) in labels:
        r = 9 * SS
        ex, ey = min(max(x, box[0]), box[2]), min(max(y, box[1]), box[3])
        d.line([(x, y), (ex, ey)], fill=(20, 20, 20, 220), width=2 * SS)
        d.ellipse((x - r, y - r, x + r, y + r), fill=RED + (255,), outline=(255, 255, 255, 255), width=3 * SS)
        d.rounded_rectangle(box, 6 * SS, fill=(255, 255, 255, 240), outline=RED + (255,), width=2 * SS)
        d.text((box[0] + 8 * SS - l, box[1] + 7 * SS - u), text, font=f_label, fill=(20, 20, 20, 255))
    # the title
    d.rectangle((0, 0, W * SS, TOP * SS), fill=BG)
    d.text((24 * SS, 13 * SS), title, font=font(29, True), fill=TEXT)
    d.text((24 * SS, 55 * SS), sub, font=font(18), fill=DIM)
    # the altitude strip
    top = (H - STRIP) * SS
    d.rectangle((0, top, W * SS, H * SS), fill=BG)
    L, R, T, B = 86 * SS, (W - 28) * SS, top + 16 * SS, top + 112 * SS
    t0, t1 = pts[0][0], pts[-1][0]
    amax = max(6000, math.ceil(max(p[3] for p in pts) / 3000) * 3000)
    tx = lambda t: L + (t - t0) / max(1, t1 - t0) * (R - L)  # noqa: E731
    ay = lambda m: B - m / amax * (B - T)  # noqa: E731
    small = font(14)
    for m in range(0, amax + 1, 3000):
        d.line((L, ay(m), R, ay(m)), fill=(255, 255, 255, 38), width=SS)
        lab = f"{m / 1000:.0f} km"
        d.text((L - 12 * SS - d.textlength(lab, font=small), ay(m) - 9 * SS), lab, font=small, fill=DIM)
    for a, b in zip(pts, pts[1:]):
        if b[0] - a[0] > GAP_S:
            dashed((tx(a[0]), ay(a[3])), (tx(b[0]), ay(b[3])), (255, 255, 255, 255), 3 * SS)
            continue
        d.polygon([(tx(a[0]), ay(a[3])), (tx(b[0]), ay(b[3])), (tx(b[0]), B), (tx(a[0]), B)], fill=colour(a[3]) + (150,))
        d.line((tx(a[0]), ay(a[3]), tx(b[0]), ay(b[3])), fill=colour(a[3]) + (255,), width=3 * SS)
    step = next((s for s in (300, 600, 900, 1800, 3600, 7200, 14400, 28800) if (t1 - t0) / s <= 10), 57600)
    for t in range(int(t0 // step + 1) * step, int(t1), step):
        d.line((tx(t), B, tx(t), B + 5 * SS), fill=DIM, width=SS)
        d.text((tx(t) - 20 * SS, B + 7 * SS), hm(t), font=small, fill=DIM)
    for t, _ in group_alerts(alerts):
        d.line((tx(t), T, tx(t), B), fill=RED + (255,), width=2 * SS)
    # the key: what the colours, the dashes and the red marks mean
    ky = B + 34 * SS
    x = L
    d.text((x, ky - 2 * SS), "Altitude", font=small, fill=TEXT)
    x += d.textlength("Altitude", font=small) + 12 * SS
    bar = 300 * SS
    for i in range(bar):
        d.line((x + i, ky, x + i, ky + 12 * SS), fill=colour(i / bar * 13000) + (255,))
    for km in (0, 3, 6, 9, 12):
        lab = f"{km}" + (" km" if km == 12 else "")
        d.text((x + km * 1000 / 13000 * bar - 4 * SS, ky + 14 * SS), lab, font=font(12), fill=DIM)
    x += bar + 44 * SS
    dashed((x, ky + 6 * SS), (x + 56 * SS, ky + 6 * SS), (255, 255, 255, 255), 4 * SS)
    x += 66 * SS
    d.text((x, ky - 2 * SS), "not heard: a straight guess", font=small, fill=TEXT)
    x += d.textlength("not heard: a straight guess", font=small) + 36 * SS
    r = 7 * SS
    d.ellipse((x, ky - r + 6 * SS, x + 2 * r, ky + r + 6 * SS), fill=RED + (255,), outline=(255, 255, 255, 255), width=2 * SS)
    d.line((x + 2 * r + 14 * SS, ky - 4 * SS, x + 2 * r + 14 * SS, ky + 16 * SS), fill=RED + (255,), width=2 * SS)
    d.text((x + 2 * r + 26 * SS, ky - 2 * SS), "alert", font=small, fill=TEXT)
    d.text((L, (H - 19) * SS), f"Flight data: {source}  ·  Map: Natural Earth  ·  Israel time  ·  Flight Watch TLV", font=font(12), fill=(150, 160, 175))
    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def flight_jpeg(hexid: str, t: float, alerts: list, title: str, sub: str, get) -> bytes | None:
    """The picture of the flight at time t, or None when the feed has no such flight. get(url) -> its JSON."""
    pts = leg_at(read_trace(hexid, get), t)
    return render(pts, alerts, title, sub) if len(pts) >= 2 else None


def http_get(url: str) -> dict:
    import requests
    r = requests.get(url, timeout=30, headers={"User-Agent": "flight-watch/0.3"})
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    hexid, when, path, title_, sub_ = sys.argv[1:6]
    jpeg = flight_jpeg(hexid, float(when), [], title_, sub_, http_get)
    if jpeg is None:
        sys.exit("no such flight in the feed")
    with open(path, "wb") as out_file:
        out_file.write(jpeg)
