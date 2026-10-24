#!/usr/bin/env python3
"""A day of the monitor in one short Markdown page, for the nightly review (tools/nightly.sh) or a person.

The day is the 24 h before `--end` (default: the last 03:00 UTC). It reads the dated files monitor.sh writes
(alerts_*, posts_telegram_*, flight_watch_*.log; a run's files are named by the day it started, so every file
touched in the window is read and its rows kept by their own time):

- what Telegram got (the public channel's messages, follow-ups and AeroSignals posts included), each with its map;
- alerts by kind, all and sent to Telegram;
- the aircraft with the most alerts;
- monitor starts, gaps in the log, the share of local polls refused, and the errors.

  python tools/daily_digest.py                     # the day that ended at the last 03:00 UTC
  python tools/daily_digest.py --end 2026-10-24T03:00Z
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
IL = ZoneInfo("Asia/Jerusalem")


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def il(t: datetime) -> str:
    return f"{t.astimezone(IL):%d %b %H:%M}"


def rows(pattern: str, start: datetime, end: datetime) -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
        if datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < start:
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("time") and start <= utc(r["time"]) < end:
                    out.append(r)
    return sorted(out, key=lambda r: r["time"])


def log_lines(start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "flight_watch_*.log"))):
        if datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < start:
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:  # the log's times are the machine's local time
                    t = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc)
                except ValueError:
                    continue
                if start <= t < end:
                    out.append((t, line[20:].rstrip()))
    return sorted(out)


def text_of(post: dict) -> str:
    return " ".join("".join(s.get("text", "") for s in post.get("segments", [])).split()).rstrip(" ·")


def map_of(post: dict) -> str:
    return next((s["url"] for s in post.get("segments", []) if s.get("link") == "Map"), "")


def digest(end: datetime) -> str:
    start = end - timedelta(hours=24)
    alerts = rows("alerts_*.jsonl", start, end)
    tg = [p for p in rows("posts_telegram_*.jsonl", start, end)]
    log = log_lines(start, end)
    out = [f"# flight-watch day {il(start)} - {il(end)} Israel ({start:%Y-%m-%dT%H:%MZ} - {end:%H:%MZ})", ""]

    # the monitor itself
    starts = [t for t, m in log if " watching " in m and " every " in m]
    status = [t for t, m in log if " local | " in m]
    skipped = sum(1 for _, m in log if m.startswith("INFO    local poll skipped"))
    gaps = [(a, b) for (a, _), (b, _) in zip(log, log[1:]) if (b - a).total_seconds() > 300]
    errors = collections.Counter(re.sub(r"\d+", "N", m[8:90]) for _, m in log if m.startswith("ERROR"))
    out += ["## Monitor", "",
            f"- started {len(starts)} times: " + ", ".join(il(t) for t in starts) if starts else "- no start in the window",
            f"- local polls answered {len(status)}, skipped (refused / cooling down) {skipped}"
            + (f" ({100 * skipped / (len(status) + skipped):.0f}% skipped)" if status or skipped else ""),
            "- log gaps > 5 min: " + (", ".join(f"{il(a)} - {b.astimezone(IL):%H:%M}" for a, b in gaps[:10]) or "none"),
            "- errors: " + ("; ".join(f"{n} x {m}" for m, n in errors.most_common(8)) or "none"), ""]

    # what the public saw
    sent = [p for p in tg if p.get("sent")]
    out += [f"## Telegram: {len(sent)} messages ({len(tg) - len(sent)} refused)", ""]
    for p in tg:
        thread = " (reply)" if p.get("reply_to") else ""
        out.append(f"- {il(utc(p['time']))} **{p.get('kind', '?')}**{thread} {p.get('hex', '')}: "
                   f"{text_of(p)[:300]} {map_of(p)}".rstrip())
    out.append("")

    # all alerts
    by_kind = collections.Counter(a["kind"] for a in alerts)
    tg_keys = {(p.get("time"), p.get("hex"), p.get("kind")) for p in sent}
    tg_kind = collections.Counter(a["kind"] for a in alerts if (a["time"], a["hex"], a["kind"]) in tg_keys)
    out += [f"## Alerts: {len(alerts)}", "", "| kind | all | Telegram |", "|---|---|---|"]
    out += [f"| {k} | {n} | {tg_kind.get(k, 0)} |" for k, n in by_kind.most_common()]
    planes = collections.Counter((a["hex"], a.get("aircraft") or a["hex"]) for a in alerts)
    out += ["", "Most alerts per aircraft:", ""]
    for (h, name), n in planes.most_common(10):
        kinds = collections.Counter(a["kind"] for a in alerts if a["hex"] == h)
        out.append(f"- {name}: {n} ({', '.join(f'{k} {c}' for k, c in kinds.most_common())})")
    return "\n".join(out) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--end", help="end of the day (UTC, ISO); default: the last 03:00 UTC")
    a = p.parse_args()
    now = datetime.now(timezone.utc)
    end = utc(a.end) if a.end else now.replace(hour=3, minute=0, second=0, microsecond=0)
    if end > now:
        end -= timedelta(days=1)
    print(digest(end), end="")


if __name__ == "__main__":
    main()
