#!/usr/bin/env python3
"""tools/basemap.json.gz from Natural Earth 1:10m (public domain; naturalearthdata.com):
land, country borders and the larger places, simplified and rounded so the file stays small.
  python tools/make_basemap.py <dir with ne_10m_land, ne_10m_admin_0_boundary_lines_land, ne_10m_populated_places_simple .geojson> <out>"""
import gzip, json, os, sys


def simplify(pts, tol):
    """Douglas-Peucker, without recursion."""
    if len(pts) < 3:
        return pts
    keep, stack = {0, len(pts) - 1}, [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) or 1e-12
        best, at = tol * tol, None
        for i in range(a + 1, b):
            px, py = pts[i][0] - ax, pts[i][1] - ay
            u = max(0, min(1, (px * dx + py * dy) / norm))
            d = (px - u * dx) ** 2 + (py - u * dy) ** 2
            if d > best:
                best, at = d, i
        if at is not None:
            keep.add(at)
            stack += [(a, at), (at, b)]
    return [pts[i] for i in sorted(keep)]


def rings(geom):
    """(outer ring or line, its holes)."""
    t, c = geom["type"], geom["coordinates"]
    if t == "Polygon":
        return [(c[0], c[1:])]
    if t == "MultiPolygon":
        return [(p[0], p[1:]) for p in c]
    return [(c, [])] if t == "LineString" else [(x, []) for x in c]


def flat(ring, tol):
    s = simplify([tuple(p[:2]) for p in ring], tol)
    return [round(v, 2) for p in s for v in p] if len(s) >= 2 else None


def layer(path, tol):
    out, holes = [], []
    for f in json.load(open(path, encoding="utf-8"))["features"]:
        for outer, inner in rings(f["geometry"]):
            out.append(flat(outer, tol))
            holes += [flat(r, tol) for r in inner]
    return [x for x in out if x], [x for x in holes if x]


src, dest = sys.argv[1:3]
land, holes = layer(os.path.join(src, "ne_10m_land.geojson"), 0.012)
borders = layer(os.path.join(src, "ne_10m_admin_0_boundary_lines_land.geojson"), 0.012)[0]
places = sorted(([f["properties"]["name"], round(f["geometry"]["coordinates"][1], 2), round(f["geometry"]["coordinates"][0], 2),
                  f["properties"]["scalerank"]]
                 for f in json.load(open(os.path.join(src, "ne_10m_populated_places_simple.geojson"), encoding="utf-8"))["features"]
                 if f["properties"]["scalerank"] <= 7), key=lambda p: p[3])
with gzip.open(dest, "wt", encoding="utf-8", compresslevel=9) as f:
    json.dump({"land": land, "holes": holes, "borders": borders, "places": places}, f, separators=(",", ":"), ensure_ascii=False)
print(len(land), len(holes), len(borders), len(places), os.path.getsize(dest))
