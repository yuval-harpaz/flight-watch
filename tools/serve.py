#!/usr/bin/env python3
"""Serve the flight pages (docs/) locally and relay the live ADS-B feed for them.

api.adsb.lol sends no CORS headers, so a page from another site (GitHub Pages, a file) cannot
read it in the browser. Served from here the pages ask this server instead (same origin):

  /v2/...           -> https://api.adsb.lol/v2/...        (callsign / hex lookups)
  /data/traces/...  -> https://adsb.lol/data/traces/...   (today's positions of one aircraft)
  anything else     -> the files in docs/ (flights.html, flight_map.html, ...)

Requests go through flight_watch.Http: the same adaptive per-host budget and 429 backoff as
the monitor, and a short cache so several open pages don't multiply requests. Nothing is stored.

  python tools/serve.py              # then open http://localhost:8765/flights.html
  python tools/serve.py --port 8080 --open
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import threading
import time
import webbrowser

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flight_watch as fw  # noqa: E402

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
UPSTREAM = {"/v2/": ("https://api.adsb.lol", 5),          # prefix -> (host, cache seconds)
            "/data/traces/": ("https://adsb.lol", 30)}


class Relay:
    def __init__(self, rate: float, rate_max: float):
        self.http = fw.Http(rate=rate, rate_max=rate_max)
        self.http.s.headers["User-Agent"] = "flight-watch-pages/0.1"
        self.cache: dict[str, tuple[float, int, bytes, str]] = {}
        self.lock = threading.Lock()

    def get(self, path: str) -> tuple[int, bytes, str]:
        for prefix, (host, ttl) in UPSTREAM.items():
            if path.startswith(prefix):
                break
        else:
            return 404, b"{}", "application/json"
        with self.lock:  # one upstream request at a time; the budget is not thread-safe
            hit = self.cache.get(path)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1:]
            try:
                r = self.http.request("GET", host + path, fw.HIGH, timeout=20)
                out = (200, r.content, r.headers.get("Content-Type", "application/json"))
            except fw.Throttled as e:  # cooling down after a 429: tell the page, don't hammer
                return 429, json.dumps({"error": str(e)}).encode(), "application/json"
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 502
                out = (code, json.dumps({"error": f"HTTP {code}"}).encode(), "application/json")
            except requests.RequestException as e:
                return 502, json.dumps({"error": str(e)}).encode(), "application/json"
            self.cache[path] = (time.monotonic(), *out)
            if len(self.cache) > 500:
                self.cache = {k: v for k, v in self.cache.items() if time.monotonic() - v[0] < 60}
            return out


def handler(relay: Relay):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=DOCS, **kw)

        def do_GET(self):
            path = self.path.split("#")[0]
            if any(path.startswith(p) for p in UPSTREAM):
                code, body, ctype = relay.get(path)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path in ("", "/"):
                self.path = "/flights.html"
            super().do_GET()

        def end_headers(self):
            if not any(self.path.startswith(p) for p in UPSTREAM):
                self.send_header("Cache-Control", "no-cache")  # edited pages show up on reload
            super().end_headers()

        def log_message(self, fmt, *args):  # quiet: only relayed requests and errors
            request = str(args[0]) if args else ""  # "GET /v2/hex/abc HTTP/1.1"
            relayed = any(f" {p}" in request for p in UPSTREAM)
            failed = len(args) > 1 and str(args[1])[:1] in "45"
            if relayed or failed:
                sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
    return Handler


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: this computer only)")
    p.add_argument("--rate", type=float, default=8, help="starting request budget per feed host, req/min")
    p.add_argument("--rate-max", type=float, default=60)
    p.add_argument("--open", action="store_true", help="open the flight list in the browser")
    a = p.parse_args()
    server = http.server.ThreadingHTTPServer((a.host, a.port), handler(Relay(a.rate, a.rate_max)))
    url = f"http://localhost:{a.port}/flights.html"
    print(f"serving {os.path.normpath(DOCS)} and relaying adsb.lol at {url} (Ctrl+C stops)", flush=True)
    if a.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
