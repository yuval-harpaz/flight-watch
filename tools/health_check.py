#!/usr/bin/env python3
"""Is the monitor doing its job? Run every 15 min (tools/health.sh, a systemd user timer). It reads only the monitor's own
log and process list - no request to any feed - and tells the owner's private chat (TELEGRAM_CHAT_ID, never the channel) when
something is badly wrong: the monitor stopped, no local poll answered, a source of data gone or refusing us. Not false
alarms, not single errors: the owner (25 Nov 2026) stopped the nightly analysis and wants a message only then.

A problem is told when it starts, again every REMIND_S while it lasts, and once when it is over. State in
health_state.json (git-ignored). `--dry-run` prints what it would send and changes nothing.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "health_state.json")
REMIND_S = 12 * 3600

DOWN_MIN = 20          # the monitor's process gone, or its log silent, this long
NO_POLL_MIN = 20       # no local poll answered (adsb.lol nor adsb.fi)
EMPTY_MIN = 30         # every answered poll had no aircraft around TLV
BOARD_MIN = 90         # the flight board (data.gov.il) not read (refreshed every 10 min)
ADSBLOL_LOCAL_H = 6    # the local poll on adsb.fi alone
WINDOW_H = 2           # counts below are over the last WINDOW_H hours
AERO_FAILS = 40        # of ~60 reads (every 120 s) of AeroSignals / Squawk Alert
RELAY_FAILS = 20       # of ~24 shares (every 300 s)
TELEGRAM_FAILS = 3
STARVED = 6
START_RUNS = 4         # starts within an hour: crashing again and again
SQUAWK_DEAD_MIN = 60
DISK_MIN_GB = 1.0
AVSTACK_AIRLINES = ("W4", "W6", "W9")  # flight_watch.AVSTACK_AIRLINES: one request each a day

TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ")


def read_log(now: datetime, hours: float = 8) -> list[tuple[datetime, str]]:
    """The last `hours` of the monitor's log, from the newest two files (one runs past midnight until the 05:00 restart)."""
    files = sorted(glob.glob(os.path.join(ROOT, "flight_watch_*.log")), key=os.path.getmtime)[-2:]
    out = []
    since = now - timedelta(hours=hours)
    for name in files:
        with open(name, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = TS.match(line)
                if not m:
                    continue
                t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                if t >= since:
                    out.append((t, line.rstrip("\n")))
    out.sort(key=lambda x: x[0])
    return out


def running() -> bool:
    try:
        return subprocess.run(["pgrep", "-f", "flight_watch.py"], capture_output=True).returncode == 0
    except OSError:
        return True  # (cannot tell: the log's age decides)


def problems(now: datetime, lines: list[tuple[datetime, str]], alive: bool, free_gb: float) -> dict[str, str]:
    """{key: text} of what is wrong now."""
    out = {}
    last_t = lines[-1][0] if lines else None
    age = (now - last_t).total_seconds() / 60 if last_t else None
    if not alive:
        out["down"] = "The monitor is not running (no flight_watch.py process)" + (
            f"; its log's last line is {age:.0f} min old." if age is not None else ".")
    elif age is None or age >= DOWN_MIN:
        out["down"] = ("The monitor runs but its log has been silent for "
                       + (f"{age:.0f} min." if age is not None else "hours."))
    if "down" in out:
        return out  # (the rest would only repeat it)

    def last(pattern: str) -> datetime | None:
        rx = re.compile(pattern)
        return next((t for t, line in reversed(lines) if rx.search(line)), None)

    def count(pattern: str, hours: float = WINDOW_H) -> int:
        rx, since = re.compile(pattern), now - timedelta(hours=hours)
        return sum(1 for t, line in lines if t >= since and rx.search(line))

    starts = [t for t, line in lines if " watching " in line and " via " in line and "r=" in line]
    started = starts[-1] if starts else lines[0][0]
    run_min = (now - started).total_seconds() / 60

    status = [(t, line) for t, line in lines if re.search(r" INFO +\d+ local \| ", line)]
    polled = status[-1][0] if status else None
    if run_min >= NO_POLL_MIN and (polled is None or (now - polled).total_seconds() / 60 >= NO_POLL_MIN):
        out["no_polls"] = (f"No local poll answered for {((now - polled).total_seconds() / 60) if polled else run_min:.0f} "
                           "min: neither adsb.lol nor adsb.fi gives the aircraft around TLV.")
    recent = [line for t, line in status if (now - t).total_seconds() / 60 <= EMPTY_MIN]
    if run_min >= EMPTY_MIN and recent and all(re.search(r" INFO +0 local \| ", line) for line in recent):
        out["empty"] = f"Every local poll of the last {EMPTY_MIN} min answered with no aircraft around TLV."
    if recent and run_min >= EMPTY_MIN and all("| 0 scheduled flights" in line for line in recent):
        out["board"] = "No scheduled flights: the flight board (data.gov.il) gives nothing."
    board = last(r"flight board: \d+ active flights")
    if run_min >= BOARD_MIN and (board is None or (now - board).total_seconds() / 60 >= BOARD_MIN):
        out["board"] = (f"The flight board (data.gov.il) has not been read for "
                        f"{((now - board).total_seconds() / 60) if board else run_min:.0f} min: no discovery of "
                        "TLV flights far away.")

    # the private feeder (--feeder) keeps the cycle going without them: a status line every poll, its circle ~50 nm
    alone = last(r"local poll from the private feeder alone")
    agg_back = max([t for t in (last(r"local poll from adsb\.lol again"), last(r"local poll from adsb\.fi \(")) if t],
                   default=None)
    if alone and (agg_back is None or agg_back < alone) and (now - alone).total_seconds() / 60 >= NO_POLL_MIN:
        out["no_polls"] = (f"Neither adsb.lol nor adsb.fi has answered a local poll for "
                           f"{(now - alone).total_seconds() / 60:.0f} min: the circle comes from the private feeder alone "
                           "(~50 nm low down).")
    on_fi, back = last(r"local poll from adsb\.fi \("), last(r"local poll from adsb\.lol again")
    if on_fi and (back is None or back < on_fi) and (now - on_fi).total_seconds() / 3600 >= ADSBLOL_LOCAL_H:
        out["adsblol_local"] = (f"adsb.lol has not answered a local poll for {(now - on_fi).total_seconds() / 3600:.0f} h: "
                                "the circle comes from adsb.fi alone.")
    for prov in ("adsb.lol", "adsb.fi"):
        n = count(rf"{re.escape(prov)} refuse[sd] access", 3)
        if n >= 2:
            out[f"refused_{prov}"] = f"{prov} refuses us (HTTP 401 / 403), {n} times in 3 h."
    off = last(r"route lookups failed \d+ times in a row - disabled")
    if off and off >= started:
        out["routes"] = "Route data (standing data) lookups failed and are off until the next start."
    dead, alive_again = last(r"squawk search not to be trusted"), last(r"squawk search works again")
    if dead and (alive_again is None or alive_again < dead) and dead >= started \
            and (now - dead).total_seconds() / 60 >= SQUAWK_DEAD_MIN:
        out["squawk_search"] = (f"The feeds' squawk search has not been trustworthy for "
                                f"{(now - dead).total_seconds() / 60:.0f} min.")
    for key, name, pattern in (("aerosignals", "AeroSignals (Bluesky)", r"AeroSignals \(Bluesky\) not read"),
                               ("squawkalert", "Squawk Alert (Mastodon)", r"Squawk Alert \(Mastodon\) not read")):
        n = count(pattern)
        if n >= AERO_FAILS:
            out[key] = f"{name} could not be read {n} times in {WINDOW_H} h: squawk reports from it are missed."
    n = count(r"could not share callsigns with the relay")
    if n >= RELAY_FAILS:
        out["relay"] = f"The relay (Cloudflare worker) refused or failed {n} callsign shares in {WINDOW_H} h."
    n = count(r"notification failed \(api\.telegram\.org\)")
    if n >= TELEGRAM_FAILS:
        out["telegram"] = f"{n} Telegram messages failed in {WINDOW_H} h."
    n = count(r"follow requests starved")
    if n >= STARVED:
        out["starved"] = f"Followed flights are not being asked for (\"follow requests starved\", {n} times in {WINDOW_H} h)."
    n = len([t for t in starts if (now - t).total_seconds() <= 3600])
    if n >= START_RUNS:
        out["restarts"] = f"The monitor started {n} times in the last hour: it may be crashing."
    # "N reads (M failed)": N the answered reads (none while it refuses us: no failure counted then either)
    shadow = [line for t, line in lines if "feeder shadow, last hour:" in line][-3:]
    reads = [re.search(r"(\d+) reads \((\d+) failed\)", line) for line in shadow]
    if len(reads) == 3 and all(m and int(m.group(1)) == 0 for m in reads):
        out["feeder"] = "The private feeder has not answered for 3 h."
    n = count(r"WARNING aviationstack ", 24)
    if n >= len(AVSTACK_AIRLINES):
        out["avstack"] = (f"aviationstack failed {n} times today: the board's Wizz arrivals are not looked for by their "
                          "aircraft.")
    if free_gb < DISK_MIN_GB:
        out["disk"] = f"Only {free_gb:.1f} GB free on the disk."
    return out


def tell_owner(text: str) -> bool:
    """The owner's private chat with the bot: TELEGRAM_CHAT_ID only, never the channel. The token is never printed."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID: the owner was not told")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30) as r:
            return r.status == 200
    except OSError as e:
        print("Telegram:", str(e).replace(token, "<token>"))
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print what would be sent; change nothing")
    a = p.parse_args(argv)
    now = datetime.now()
    found = problems(now, read_log(now), running(), shutil.disk_usage(ROOT).free / 1e9)
    try:
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    ts = now.timestamp()
    new = {k: v for k, v in found.items() if k not in state}
    due = {k: v for k, v in found.items() if k in state and ts - state[k]["told"] >= REMIND_S}
    over = [k for k in state if k not in found]
    parts = []
    if new or due:
        parts.append("⚠️ flight-watch: something is wrong")
        parts += [f"• {v}" for v in new.values()]
        parts += [f"• still: {v} (since {datetime.fromtimestamp(state[k]['since']):%d %b %H:%M})" for k, v in due.items()]
        others = [v for k, v in found.items() if k not in new and k not in due]
        parts += [f"• (still, told before) {v}" for v in others]
    if over:
        parts.append(("" if not parts else "\n") + "✅ flight-watch: over - " + ", ".join(
            f"{state[k]['text']}" for k in over))
    text = "\n".join(parts)
    if a.dry_run:
        print(text or (f"nothing new to tell; already told: {', '.join(found)}" if found else "all well"))
        return 0
    if text and not tell_owner(text):
        return 1  # (state unchanged: told again next time)
    for k, v in found.items():
        if k in new or k in due:
            state[k] = {"since": state.get(k, {}).get("since", ts), "told": ts, "text": v}
    for k in over:
        del state[k]
    with open(STATE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(STATE + ".tmp", STATE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
