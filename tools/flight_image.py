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
# ... and long enough to come down from the altitude before it, stop and climb to the one after (FB.stopped): BDK27, 5 Nov
# 2026, unheard 15 min between 18,000 and 10,625 ft (13 nm) and 16 min at 7,000 ft (9 nm) was cut into three "flights"
STOP_FPM, STOP_MIN_S = 2000, 600
# A position out of reach of the last good one (3 nm + 750 kt, flight_watch's --max-gap-speed) is a bad one, drawn orange (the
# map's position jump) and left out of the map's extent: LY859 (4X-EKZ, TLV -> FRA, 9 Nov 2026) was over Romania, then 30 s
# at Lima, Peru, and its picture showed half the globe with a line that looked like a track. Still away after OFF_MAX_S: real.
OFF_NM, OFF_KT, OFF_MAX_S, JUMP = 3, 750, 600, (255, 153, 0)
W, H, SS, TOP, STRIP = 1280, 900, 2, 92, 196
MIN_SPAN = 0.03  # the map shows at least this (radians of longitude, ~100 nm)
# A close-up when the alerts' part of the flight would be small on the whole flight's map: FZ1073's dive and turn were a
# squiggle in a corner of its 1,000 nm. ROI_PAD_S of the track around the alerts, in a box INSET_W x INSET_H.
ROI_PAD_S, ROI_GAP_S, INSET_SMALL, INSET_W, INSET_H = 600, 1800, 0.25, 560, 370
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


def stopped(q: tuple, r: tuple) -> bool:
    """The aircraft spent the gap between reports q and r (time, lat, lon, feet) on the ground somewhere: slow on average,
    and long enough to descend from q's altitude, stop and climb to r's."""
    dt = r[0] - q[0]
    alt = sum(x[3] for x in (q, r) if x[3] != "ground")
    return (dt > LEG_GAP_S and nm(q, r) / (dt / 3600) < LEG_GAP_KT
            and dt >= alt / STOP_FPM * 60 + STOP_MIN_S)


def leg_at(rows: list, t: float) -> list:
    """The flight at time t, with the ground report that ends it: (time, lat, lon, metres). FB.flightAt's legs, nearest t."""
    legs, leg = [], []
    for r in rows:
        q = leg[-1] if leg else None
        if r[3] == "ground":
            if q:
                legs.append(leg + [r])
            leg = []
        elif q and stopped(q, r):
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


def off_track(pts: list) -> set:
    """Indices of the positions (time, lat, lon, ..) the aircraft could not have reached from its last good one: OFF_NM +
    OFF_KT. A run of them that lasts over OFF_MAX_S is where it really went. FB.offTrack in docs/flightboard.js alike."""
    bad, run, good = set(), [], None
    for i, p in enumerate(pts):
        if good is None or nm(good, p) <= OFF_NM + OFF_KT * (p[0] - good[0]) / 3600:
            good, run = p, []
        elif run and p[0] - pts[run[0]][0] > OFF_MAX_S:
            bad -= set(run)
            good, run = p, []
        else:
            run.append(i)
            bad.add(i)
    return bad


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


def merc_y(lat: float) -> float:
    return math.asinh(math.tan(math.radians(max(-85, min(85, lat)))))


def region_of_interest(pts: list, alerts: list) -> tuple | None:
    """(from, to) of the part of the flight the alerts are about: alerts within ROI_GAP_S of each other belong together;
    of several such groups the one with an emergency, else the most alerts, else the first."""
    if not alerts:
        return None
    groups = []
    for name, t in sorted(alerts, key=lambda a: a[1]):
        if groups and t - groups[-1][-1][1] <= ROI_GAP_S:
            groups[-1].append((name, t))
        else:
            groups.append([(name, t)])
    best = max(groups, key=lambda g: (any(n == "emergency" or n[-4:] in ("7500", "7600", "7700") for n, _ in g),
                                      len(g), -g[0][1]))
    return best[0][1] - ROI_PAD_S, best[-1][1] + ROI_PAD_S


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
    data = basemap()
    hit = lambda a, b: a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]  # noqa: E731

    def view(fit, w, h, margin_x, margin_y):
        """x, y in pixels (of a w x h picture, x SS) of a lat, lon: Mercator, `fit` filling it but for the margins."""
        xs, ys = [math.radians(p[2]) for p in fit], [merc_y(p[1]) for p in fit]
        scale = 1 / max(max(max(xs) - min(xs), MIN_SPAN) / (w - margin_x), max(max(ys) - min(ys), MIN_SPAN) / (h - margin_y))
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        return lambda lat, lon: ((w / 2 + (math.radians(lon) - cx) * scale) * SS, (h / 2 - (merc_y(lat) - cy) * scale) * SS)

    def dashed(d, a, b, colour_, width):
        n = max(2, int(math.dist(a, b) / (14 * SS)))
        for i in range(0, n, 2):
            d.line([(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n),
                    (a[0] + (b[0] - a[0]) * (i + 1) / n, a[1] + (b[1] - a[1]) * (i + 1) / n)], fill=colour_, width=width)

    def draw_map(w, h, px, labelled, marked, keep_out=(), places=22):
        """The land, the track, the alerts: labels for `labelled` ((time, text)), a dot alone for the times in `marked`."""
        img = Image.new("RGB", (w * SS, h * SS), SEA)
        d = ImageDraw.Draw(img, "RGBA")

        def ring(flat):
            pp = [px(flat[i + 1], flat[i]) for i in range(0, len(flat), 2)]
            x, y = [p[0] for p in pp], [p[1] for p in pp]
            return pp if max(x) > 0 and min(x) < w * SS and max(y) > 0 and min(y) < h * SS else None

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
        ok = [i for i in range(len(pts)) if i not in bad]
        # the alerts' labels, where they fit: the track and the other labels are in the way
        busy = list(keep_out) + [(x - 8 * SS, y - 8 * SS, x + 8 * SS, y + 8 * SS) for x, y in line[::max(1, len(line) // 400)]
                                 if 0 < x < w * SS and 0 < y < h * SS]
        labels = []
        for t, text in labelled:
            x, y = px(*position_at(good, t))
            l, u, r, b = d.textbbox((0, 0), text, font=f_label)
            lw, lh = r - l + 16 * SS, b - u + 14 * SS
            best = None
            for dist in (34, 70, 120):
                for ang in range(0, 360, 30):
                    cxl = x + math.cos(math.radians(ang)) * (dist * SS + lw / 2)
                    cyl = y + math.sin(math.radians(ang)) * (dist * SS + lh / 2)
                    box = (cxl - lw / 2, cyl - lh / 2, cxl + lw / 2, cyl + lh / 2)
                    inside = box[0] > 8 * SS and box[2] < (w - 8) * SS and box[1] > 8 * SS and box[3] < (h - 8) * SS
                    cost = (0 if inside else 1000) + sum(hit(box, q) for q in busy) + dist / 1000
                    if best is None or cost < best[0]:
                        best = (cost, box)
            busy.append(best[1])
            labels.append((x, y, best[1], text, (l, u)))
        # places, largest first, where nothing else is
        shown = 0
        for name, lat, lon, _rank in data["places"]:
            x, y = px(lat, lon)
            if not (0 < x < w * SS and 0 < y < h * SS):
                continue
            box = (x - 5 * SS, y - 10 * SS, x + d.textlength(name, font=f_place) + 12 * SS, y + 10 * SS)
            if any(hit(box, q) for q in busy) or box[2] > w * SS or box[1] < 0 or box[3] > h * SS:
                continue
            d.ellipse((x - 3 * SS, y - 3 * SS, x + 3 * SS, y + 3 * SS), fill=(90, 90, 90, 255))
            d.text((x + 7 * SS, y - 9 * SS), name, font=f_place, fill=(70, 70, 70, 255))
            busy.append(box)
            shown += 1
            if shown >= places:
                break
        d.line([line[i] for i in ok], fill=(255, 255, 255, 235), width=9 * SS, joint="curve")
        for i, (a, b, p, q) in enumerate(zip(line, line[1:], pts, pts[1:])):
            if i in bad or i + 1 in bad:  # to and from a bad position: orange, as the map's position jump
                d.line([a, b], fill=JUMP + (255,), width=4 * SS)
                continue
            d.line([a, b], fill=colour(p[3]) + (255,), width=5 * SS)
            if q[0] - p[0] > GAP_S:
                dashed(d, a, b, (255, 255, 255, 255), 5 * SS)
        for x, y in (line[ok[0]], line[ok[-1]]):
            r = 7 * SS
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, 255), outline=(20, 20, 20, 255), width=2 * SS)
        r = 9 * SS
        for t in marked:
            x, y = px(*position_at(good, t))
            d.ellipse((x - r, y - r, x + r, y + r), fill=RED + (255,), outline=(255, 255, 255, 255), width=3 * SS)
        for x, y, box, text, (l, u) in labels:
            ex, ey = min(max(x, box[0]), box[2]), min(max(y, box[1]), box[3])
            d.line([(x, y), (ex, ey)], fill=(20, 20, 20, 220), width=2 * SS)
            d.ellipse((x - r, y - r, x + r, y + r), fill=RED + (255,), outline=(255, 255, 255, 255), width=3 * SS)
            d.rounded_rectangle(box, 6 * SS, fill=(255, 255, 255, 240), outline=RED + (255,), width=2 * SS)
            d.text((box[0] + 8 * SS - l, box[1] + 7 * SS - u), text, font=f_label, fill=(20, 20, 20, 255))
        return img

    f_label, f_place = font(17, True), font(13)
    bad = off_track(pts)
    good = [p for i, p in enumerate(pts) if i not in bad]
    px = view(good, W, mh, 220, 170)
    groups = group_alerts(alerts)
    window = region_of_interest(pts, alerts)
    inset = None
    if window:
        near = [p for p in good if window[0] <= p[0] <= window[1]] or good
        xs_, ys_ = zip(*(px(p[1], p[2]) for p in near))
        x0, x1, y0, y1 = min(xs_), max(xs_), min(ys_), max(ys_)
        # small on the whole map: worth a closer look
        if max(x1 - x0, 1) < INSET_SMALL * W * SS and max(y1 - y0, 1) < INSET_SMALL * mh * SS:
            iw, ih = INSET_W, INSET_H
            line_ = [px(p[1], p[2]) for p in good]

            def corner_cost(c):
                # how much of the track it would hide (length, not reports: they crowd where it flies low)
                return sum(math.dist(a, b) for a, b in zip(line_, line_[1:]) if c[0] < a[0] < c[2] and c[1] < a[1] < c[3]) + (
                    1e9 * hit(c, (x0, y0, x1, y1)))

            m = 14 * SS
            corners = [(m, m, m + iw * SS, m + ih * SS), ((W - iw) * SS - m, m, W * SS - m, m + ih * SS),
                       (m, mh * SS - m - ih * SS, m + iw * SS, mh * SS - m), ((W - iw) * SS - m, mh * SS - m - ih * SS, W * SS - m, mh * SS - m)]
            inset = (min(corners, key=corner_cost), near, (x0, y0, x1, y1))
    if inset:
        corner, near, region = inset
        px_i = view(near, INSET_W, INSET_H, 120, 110)
        in_window = [g for g in groups if window[0] <= g[0] <= window[1]]
        out_window = [g for g in groups if g not in in_window]
        main = draw_map(W, mh, px, out_window, [g[0] for g in in_window], keep_out=[corner, region])
        small_map = draw_map(INSET_W, INSET_H, px_i, in_window, [], places=8)
        dm = ImageDraw.Draw(main, "RGBA")
        pad = 12 * SS
        dm.rectangle((region[0] - pad, region[1] - pad, region[2] + pad, region[3] + pad), outline=(20, 20, 20, 255), width=2 * SS)
        # a line from the region to the close-up: which part it is
        cx_, cy_ = (corner[0] + corner[2]) / 2, (corner[1] + corner[3]) / 2
        rx_, ry_ = min(max(cx_, region[0] - pad), region[2] + pad), min(max(cy_, region[1] - pad), region[3] + pad)
        ex_, ey_ = min(max(rx_, corner[0]), corner[2]), min(max(ry_, corner[1]), corner[3])
        dm.line((rx_, ry_, ex_, ey_), fill=(20, 20, 20, 255), width=2 * SS)
        main.paste(small_map, (round(corner[0]), round(corner[1])))
        dm.rectangle(corner, outline=(20, 20, 20, 255), width=3 * SS)
    else:
        main = draw_map(W, mh, px, groups, [])
    img = Image.new("RGB", (W * SS, H * SS), SEA)
    img.paste(main, (0, TOP * SS))
    d = ImageDraw.Draw(img, "RGBA")
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
            dashed(d, (tx(a[0]), ay(a[3])), (tx(b[0]), ay(b[3])), (255, 255, 255, 255), 3 * SS)
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
    dashed(d, (x, ky + 6 * SS), (x + 56 * SS, ky + 6 * SS), (255, 255, 255, 255), 4 * SS)
    x += 66 * SS
    d.text((x, ky - 2 * SS), "not heard: a straight guess", font=small, fill=TEXT)
    x += d.textlength("not heard: a straight guess", font=small) + 36 * SS
    r = 7 * SS
    d.ellipse((x, ky - r + 6 * SS, x + 2 * r, ky + r + 6 * SS), fill=RED + (255,), outline=(255, 255, 255, 255), width=2 * SS)
    d.line((x + 2 * r + 14 * SS, ky - 4 * SS, x + 2 * r + 14 * SS, ky + 16 * SS), fill=RED + (255,), width=2 * SS)
    d.text((x + 2 * r + 26 * SS, ky - 2 * SS), "alert", font=small, fill=TEXT)
    if bad:
        x += 2 * r + 26 * SS + d.textlength("alert", font=small) + 36 * SS
        d.line((x, ky + 6 * SS, x + 56 * SS, ky + 6 * SS), fill=JUMP + (255,), width=4 * SS)
        d.text((x + 66 * SS, ky - 2 * SS), "bad position", font=small, fill=TEXT)
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
