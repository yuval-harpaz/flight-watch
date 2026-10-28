#!/usr/bin/env python3
"""A small local control page: start / stop the monitor, browse a run's alerts by kind, make a map of one.

  python tools/dashboard.py            # opens http://localhost:8766/ in the browser
  python tools/dashboard.py --port 9000 --no-open

Start it from a terminal whose environment has the monitor's variables (TELEGRAM_*, RELAY_TOKEN): a
monitor started from the page inherits them. It runs `./monitor.sh` in its own session, so closing the
page or stopping this server leaves it running; "stop" sends it SIGTERM (as Ctrl+C, a clean exit). Both
tell the Telegram channel (TELEGRAM_BOT_TOKEN, and TELEGRAM_CHANNEL_ID or with SENDTO=chat TELEGRAM_CHAT_ID), so
subscribers know when alerts stop and resume - only when it stays stopped longer than QUIET_S (10 s): a stop and a start
straight after (a restart for a code change) tell nobody. Tabs show the monitor's printout and its log.
Maps are made by `tools/plot_alert.py --map` and open in the browser by themselves. Only this machine can
reach the page (127.0.0.1), and requests from other sites' pages are refused. Standard library only.
"""
from __future__ import annotations

import argparse
import glob
import http.server
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import parse_qs, urlparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONSOLE = os.path.join(ROOT, "tmp_monitor_console.txt")  # the monitor's own output (tmp*: git-ignored)
PAGES = "https://yuval-harpaz.github.io/flight-watch/"
LINKS = [  # (group, label, url)
    ("ours", "Flights board (with warnings)", PAGES + "flights.html?alerts=true"),
    ("ours", "Incidents page", PAGES + "incidents.html"),
    ("ours", "Telegram channel @flight_watch_tlv", "https://t.me/flight_watch_tlv"),
    ("ours", "GitHub repository", "https://github.com/yuval-harpaz/flight-watch"),
    ("ours", "GitHub Actions (callsign sharing, tests)", "https://github.com/yuval-harpaz/flight-watch/actions"),
    ("ours", "Relay status (worker)", "https://flight-watch-relay.yuvharpaz.workers.dev/status"),
    ("ours", "Cloudflare dashboard", "https://dash.cloudflare.com/"),
    ("maps", "Flightradar24 over Israel", "https://www.flightradar24.com/32.01,34.88/8"),
    ("maps", "ADS-B Exchange over Israel", "https://globe.adsbexchange.com/?lat=32.01&lon=34.88&zoom=8"),
    ("maps", "adsb.lol map (our feed)", "https://adsb.lol/?lat=32.01&lon=34.88&zoom=8"),
    ("maps", "adsb.fi map (second feed)", "https://globe.adsb.fi/?lat=32.01&lon=34.88&zoom=8"),
    ("maps", "GPS jamming map (gpsjam.org)", "https://gpsjam.org/?lat=32&lon=35&z=6"),
    ("squawks", "AeroSignals on Bluesky (squawks we read)", "https://bsky.app/profile/spotterintel.bsky.social"),
    ("squawks", "Squawk Alert on X", "https://x.com/Squawk_Alert"),
    ("board", "Ben Gurion flight board (IAA)",
     "https://www.iaa.gov.il/airports/ben-gurion/flight-board/"),
    ("board", "Board data (data.gov.il)",
     "https://data.gov.il/dataset/flydata/resource/e83f763b-b7d7-479e-b172-ae981ddc6de5"),
    ("board", "Board history (over.org.il)", "https://over.org.il/"),
    ("data", "Route data (VRS standing data)", "https://github.com/vradarserver/standing-data"),
    ("data", "adsb.lol daily archive (archive_replay)", "https://github.com/adsblol"),
]


def monitor_proc() -> tuple[int, list[str]] | None:
    """The running monitor (python flight_watch.py, not --once) started in this checkout: (pid, argv)."""
    for d in glob.glob("/proc/[0-9]*"):
        try:
            argv = open(d + "/cmdline", "rb").read().decode(errors="replace").split("\0")
            if any(a.endswith("flight_watch.py") for a in argv[:3]) and "--once" not in argv \
                    and os.path.realpath(d + "/cwd") == ROOT:
                return int(d[6:]), argv
        except OSError:
            continue
    return None


def arg(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv[:-1] else None


def tail(path: str | None, lines: int = 15) -> list[str]:
    if not path:
        return []
    try:
        with open(os.path.join(ROOT, path), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16384))
            return f.read().decode(errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def status() -> dict:
    p = monitor_proc()
    if not p:
        return {"running": False, "console": tail(CONSOLE, 30), "log": []}
    pid, argv = p
    ticks = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19])  # starttime, after boot
    boot = next(int(x.split()[1]) for x in open("/proc/stat") if x.startswith("btime"))
    started = boot + ticks / os.sysconf("SC_CLK_TCK")
    return {"running": True, "pid": pid, "started": started, "alerts": arg(argv, "--jsonl"),
            "log": [x for x in tail(arg(argv, "--log-file"), 200) if " local | " not in x][-15:],  # not the poll counts
            "console": tail(CONSOLE, 30), "telegram_to": arg(argv, "--telegram-to")}


def runs() -> list[str]:
    """Alert files, newest first."""
    files = glob.glob(os.path.join(ROOT, "alerts*.jsonl")) + glob.glob(os.path.join(ROOT, "tmp*alerts*.jsonl"))
    return [os.path.basename(f) for f in sorted(set(files), key=os.path.getmtime, reverse=True)]


def safe_run(name: str) -> str:
    if name not in runs():
        raise ValueError("unknown alerts file")
    return os.path.join(ROOT, name)


def alerts(name: str) -> list[dict]:
    """The file's alerts, each with `pick` (plot_alert.py's number: 1 = newest) and `telegram` (sent there)."""
    path = safe_run(name)
    with open(path, encoding="utf-8") as f:
        recs = [json.loads(line) for line in f if line.strip()]
    sent = set()
    tg = path.replace("alerts", "posts_telegram", 1) if os.path.basename(path).startswith("alerts_") else None
    if tg and os.path.exists(tg):
        with open(tg, encoding="utf-8") as f:
            for line in f:
                try:
                    p = json.loads(line)
                except ValueError:
                    continue
                if p.get("sent"):
                    sent.add((p.get("time"), p.get("hex"), p.get("kind")))
    for i, r in enumerate(recs):
        r["pick"] = len(recs) - i
        r["telegram"] = (r.get("time"), r.get("hex"), r.get("kind")) in sent
    return recs


def map_path(out: str) -> str:
    """A map file name the page may write: .html inside this checkout (a bare name: the checkout's root)."""
    out = out.strip()
    if not re.fullmatch(r"[\w./-]+", out) or ".." in out:
        raise ValueError("use letters, digits, _ - . / only")
    if not out.endswith(".html"):
        out += ".html"
    path = os.path.realpath(os.path.join(ROOT, out))
    if not path.startswith(ROOT + os.sep):
        raise ValueError("the map must be inside the checkout")
    return path


JOB = {"state": "idle", "out": "", "text": ""}


def make_map(run: str, pick: int, path: str) -> None:
    JOB.update(state="running", out=os.path.relpath(path, ROOT), text="")
    cmd = [sys.executable, os.path.join(ROOT, "tools", "plot_alert.py"), "--alerts", run, "--pick", str(pick),
           "--map", "--out", path]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    JOB.update(state="done" if r.returncode == 0 else "failed", text=(r.stdout + r.stderr).strip()[-2000:])


def telegram(text: str) -> str:
    """Tell the channel (or the chat: SENDTO as monitor.sh) that the monitor stopped / started. The token
    never appears in what this returns."""
    chat = os.getenv("TELEGRAM_CHANNEL_ID" if os.getenv("SENDTO", "channel") == "channel" else "TELEGRAM_CHAT_ID")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not (token and chat):
        return "Telegram not told (its variables are not in this terminal's environment)"
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json.dumps({"chat_id": chat, "text": text}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
        return "Telegram told"
    except urllib.error.HTTPError as e:
        return f"Telegram refused the notice (HTTP {e.code})"
    except OSError:
        return "Telegram not reached"


QUIET_S = 10  # stopped this long before the channel hears of it (the owner: no messages for a quick restart)
DOWN = {"timer": None, "told": False}  # a stop's pending notice, and whether the channel was told of the stop
DOWN_LOCK = threading.Lock()


def tell_down() -> None:
    with DOWN_LOCK:
        if DOWN["timer"] is None or monitor_proc():
            return
        DOWN["timer"] = None
        DOWN["told"] = "Telegram told" == telegram("🔴 flight-watch is stopped for maintenance: no alerts until it is "
                                                     "running again (a message will say so).")


def start(notify: bool = True) -> str:
    if monitor_proc():
        return "already running"
    with DOWN_LOCK:  # back within QUIET_S: the stop's notice is not sent, and no "running again" either
        pending, DOWN["timer"] = DOWN["timer"], None
        if pending:
            pending.cancel()
        told, DOWN["told"] = DOWN["told"], False
    notify = notify and (told or not pending)
    console = open(CONSOLE, "ab")
    console.write(f"--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
    console.flush()
    # unbuffered: print() to a file is otherwise held in 8 KB blocks and the Monitor tab shows nothing
    subprocess.Popen(["./monitor.sh"], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
                     start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))
    for _ in range(25):
        time.sleep(0.2)
        if monitor_proc():
            return "started" + ("; " + telegram("🟢 flight-watch is running again: alerts on TLV flights resume.")
                                if notify else "; back within 10 s: Telegram not told" if pending else "")
    return "did not start - see the Monitor tab"


def stop(notify: bool = True) -> str:
    p = monitor_proc()
    if not p:
        return "not running"
    os.kill(p[0], signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.2)
        if not monitor_proc():
            if not notify:
                return "stopped"
            with DOWN_LOCK:
                if DOWN["timer"]:
                    DOWN["timer"].cancel()
                DOWN["timer"] = threading.Timer(QUIET_S, tell_down)
                DOWN["timer"].daemon = True
                DOWN["timer"].start()
            return f"stopped; Telegram is told if it is still stopped in {QUIET_S} s"
    return "still stopping"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, code=200, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def ours(self) -> bool:
        """Only this page: another site's page in the browser could otherwise start / stop the monitor."""
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in (f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}") \
            and origin in (None, f"http://{host}")

    def do_GET(self):
        if not self.ours():
            return self.send({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self.send(PAGE.replace("__LINKS__", json.dumps(LINKS)), ctype="text/html")
            if u.path == "/api/status":
                return self.send(dict(status(), runs=runs(), job=JOB))
            if u.path == "/api/alerts":
                return self.send(alerts(q["file"]))
            if u.path == "/api/exists":
                return self.send({"exists": os.path.exists(map_path(q["out"]))})
        except (KeyError, ValueError) as e:
            return self.send({"error": str(e)}, 400)
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        if not self.ours():
            return self.send({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/start":
                return self.send({"result": start()})
            if self.path == "/api/stop":
                return self.send({"result": stop()})
            if self.path == "/api/map":
                if JOB["state"] == "running":
                    return self.send({"error": "a map is being made"}, 409)
                run, path = safe_run(body["file"]), map_path(body["out"])
                threading.Thread(target=make_map, args=(run, int(body["pick"]), path), daemon=True).start()
                return self.send({"result": "making " + os.path.relpath(path, ROOT)})
        except (KeyError, ValueError) as e:
            return self.send({"error": str(e)}, 400)
        self.send({"error": "not found"}, 404)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>flight-watch control</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#667085;--line:#d9dde5;--accent:#1f6feb;--ok:#1a7f37;--bad:#c62828;--sel:#e8f0fe}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--text:#e6e9ef;--muted:#9aa3b2;--line:#323844;--accent:#58a6ff;--ok:#3fb950;--bad:#f85149;--sel:#22314a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,sans-serif}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:12px 16px;background:var(--card);border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0 12px 0 0}button,select,input{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:5px 10px}
button{cursor:pointer}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button:disabled{opacity:.5;cursor:default}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;background:var(--bad)}.dot.on{background:var(--ok)}
main{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:14px;padding:14px 16px}
@media (max-width:800px){main{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;min-width:0}
h2{font-size:15px;margin:0 0 8px}pre{white-space:pre-wrap;word-break:break-word;font-size:12px;margin:0;color:var(--muted);max-height:220px;overflow:auto}
details{border-top:1px solid var(--line)}summary{cursor:pointer;padding:6px 2px;font-weight:600}
.row{display:grid;grid-template-columns:52px 74px minmax(0,1fr);gap:8px;padding:4px 6px;cursor:pointer;border-radius:4px}
.row:hover,.row.sel{background:var(--sel)}.row .m{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}
.tg{color:var(--accent);font-size:11px;margin-left:4px}.muted{color:var(--muted)}
table{border-collapse:collapse;width:100%}td{padding:3px 6px;vertical-align:top;border-bottom:1px solid var(--line)}td:first-child{color:var(--muted);white-space:nowrap;width:1%}
.links a{margin-right:10px;color:var(--accent)}label{margin-right:10px;white-space:nowrap}
.tabs{margin-top:14px;margin-bottom:6px}.tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.mapbox{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}.mapbox input[type=text]{width:100%;margin-top:6px}
</style></head><body>
<header>
  <h1>flight-watch</h1>
  <span id="st"><span class="dot"></span>…</span>
  <button id="start" class="primary">Start monitor</button><button id="stop">Stop monitor</button>
  <button onclick="window.open('https://yuval-harpaz.github.io/flight-watch/flights.html?alerts=true')">Flights board</button>
  <select id="links"><option value="">Useful links…</option></select>
</header>
<main>
  <section>
    <h2>Alerts <select id="run"></select> <label><input type="checkbox" id="tgonly"> Telegram only</label>
      <input id="find" placeholder="filter" size="10"></h2>
    <div id="groups" class="muted">loading…</div>
  </section>
  <section>
    <h2>Details</h2><div id="detail" class="muted">Pick an alert.</div>
    <div class="mapbox"><h2>Map of this alert</h2>
      <label><input type="radio" name="nm" value="tmp" checked> tmp_map.html</label>
      <label><input type="radio" name="nm" value="incident"> incident: <span id="incname">docs/…_map.html</span></label>
      <label><input type="radio" name="nm" value="custom"> my own name</label>
      <input type="text" id="custom" placeholder="e.g. tmp_kl463 or docs/kl463_otp_map">
      <p><button id="mk" class="primary" disabled>Create map</button> <span id="job" class="muted"></span></p>
    </div>
    <div class="tabs"><button data-tab="console" class="on">Monitor</button><button data-tab="log">Log</button>
      <span id="note" class="muted"></span></div><pre id="log"></pre>
  </section>
</main>
<script>
const LINKS = __LINKS__, $ = id => document.getElementById(id);
const GROUPS = {ours: "Ours", maps: "Live maps", squawks: "Squawk sources", board: "Flight board", data: "Data"};
for (const g of Object.keys(GROUPS)) {
  const og = document.createElement("optgroup"); og.label = GROUPS[g];
  for (const [gg, label, url] of LINKS) if (gg === g) og.append(new Option(label, url));
  $("links").append(og);
}
$("links").onchange = e => { if (e.target.value) window.open(e.target.value); e.target.value = ""; };
const api = (p, body) => fetch(p, body ? {method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify(body)} : {}).then(r => r.json());
const il = t => new Date(t).toLocaleString("en-GB", {timeZone: "Asia/Jerusalem", day: "2-digit", month: "short",
  hour: "2-digit", minute: "2-digit"});
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
let ALERTS = [], SEL = null, RUN = null, TAB = "console";
for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => {
  TAB = b.dataset.tab;
  for (const o of document.querySelectorAll(".tabs button")) o.classList.toggle("on", o === b);
  refresh();
};

async function refresh() {
  const s = await api("/api/status");
  $("st").innerHTML = s.running
    ? `<span class="dot on"></span>running since ${il(s.started * 1000)} (pid ${s.pid}, Telegram: ${esc(s.telegram_to)})`
    : `<span class="dot"></span>stopped`;
  $("start").disabled = s.running; $("stop").disabled = !s.running;
  const box = $("log"), end = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  box.textContent = (TAB === "log" && s.running ? s.log : s.console).join("\n") || "(nothing yet)";
  if (end) box.scrollTop = box.scrollHeight;
  const cur = $("run").value;
  if ($("run").options.length !== s.runs.length) {
    $("run").replaceChildren(...s.runs.map(r => new Option(r, r)));
    $("run").value = cur && s.runs.includes(cur) ? cur : (s.running && s.alerts) || s.runs[0];
  }
  const j = s.job;
  $("job").textContent = j.state === "running" ? `making ${j.out} … (fetching the trace)` : j.state === "done"
    ? `${j.out} opened in the browser. ${j.text}` : j.state === "failed" ? `failed: ${j.text}` : "";
  $("mk").disabled = !SEL || j.state === "running";
  if ($("run").value !== RUN || s.running) loadAlerts();
}
async function loadAlerts() {
  RUN = $("run").value;
  if (!RUN) return;
  ALERTS = await api("/api/alerts?file=" + encodeURIComponent(RUN));
  draw();
}
function draw() {
  const f = $("find").value.toLowerCase(), tg = $("tgonly").checked, open = new Set(
    [...document.querySelectorAll("#groups details[open]")].map(d => d.dataset.kind));
  const by = {};
  for (const a of ALERTS) {
    if (tg && !a.telegram) continue;
    if (f && !JSON.stringify(a).toLowerCase().includes(f)) continue;
    (by[a.kind] ||= []).push(a);
  }
  const kinds = Object.keys(by).sort((x, y) => by[y].filter(a => a.telegram).length - by[x].filter(a => a.telegram).length
    || by[y].length - by[x].length);
  $("groups").replaceChildren(...kinds.map(k => {
    const d = document.createElement("details"); d.dataset.kind = k; d.open = open.has(k);
    const n = by[k].filter(a => a.telegram).length;
    d.innerHTML = `<summary>${esc(k)} <span class="muted">${by[k].length}${n ? `, ${n} on Telegram` : ""}</span></summary>`;
    for (const a of by[k].slice().reverse()) {
      const r = document.createElement("div"); r.className = "row" + (SEL && SEL.pick === a.pick && RUN === SEL.run ? " sel" : "");
      r.innerHTML = `<span>${il(a.time).split(" ").pop()}</span><span>${esc(a.flight || a.callsign || a.hex)}${a.telegram ? '<span class="tg">TG</span>' : ""}</span>` +
        `<span class="m">${esc(a.route_text || "")} - ${esc(a.message)}</span>`;
      r.onclick = () => { SEL = Object.assign({run: RUN}, a); draw(); detail(); };
      d.append(r);
    }
    return d;
  }));
  if (!kinds.length) $("groups").textContent = "No alerts.";
}
function detail() {
  const a = SEL, rows = [
    ["time", `${il(a.time)} Israel (${a.time.slice(11, 16)} UTC)`], ["kind", `${a.kind} (priority ${a.priority})`],
    ["flight", [a.flight, a.callsign, a.airline].filter(Boolean).join(" / ")],
    ["route", a.route_text || a.route || "unknown"], ["traffic", `${a.traffic || ""}${a.scheduled ? ", board time " + a.scheduled : ""}`],
    ["aircraft", [a.reg, a.type, a.hex, a.military ? "military" : ""].filter(Boolean).join(" · ")],
    ["where", `${a.alt ?? "?"} ft, ${a.gs ?? "?"} kt, ${a.dist_nm ?? "?"} nm from TLV${a.near ? ", near " + a.near : ""}`],
    ["message", a.message], ["Telegram", a.telegram ? "sent" : "not sent"],
  ];
  const links = Object.entries(a.links || {}).map(([k, u]) => `<a href="${esc(u)}" target="_blank">${esc(k)}</a>`).join("");
  $("detail").innerHTML = `<table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join("")}` +
    `<tr><td>links</td><td class="links">${links}</td></tr></table>`;
  $("incname").textContent = incident();
  $("mk").disabled = false;
}
const incident = () => `docs/${String(SEL.flight || SEL.callsign || SEL.hex).toLowerCase().replace(/[^a-z0-9]/g, "")}_map.html`;
$("mk").onclick = async () => {
  const how = document.querySelector("input[name=nm]:checked").value;
  const out = how === "tmp" ? "tmp_map.html" : how === "incident" ? incident() : $("custom").value.trim();
  if (!out) return alert("Type a name for the map.");
  const ex = await api("/api/exists?out=" + encodeURIComponent(out));
  if (ex.error) return alert(ex.error);
  if (ex.exists && how !== "tmp" && !confirm(out + " exists. Replace it?")) return;
  const r = await api("/api/map", {file: SEL.run, pick: SEL.pick, out});
  if (r.error) alert(r.error);
  refresh();
};
$("start").onclick = async () => { $("start").disabled = true; $("note").textContent = (await api("/api/start", {})).result; refresh(); };
$("stop").onclick = async () => {
  if (!confirm("Stop the monitor? The Telegram channel will be told.")) return;
  $("stop").disabled = true; $("note").textContent = (await api("/api/stop", {})).result; refresh();
};
$("run").onchange = loadAlerts; $("tgonly").onchange = draw; $("find").oninput = draw;
refresh(); setInterval(refresh, 10000);
</script></body></html>
"""


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--restart", action="store_true", help="restart the monitor without telling Telegram (the "
                   "nightly new day's files: tools/nightly.sh), then exit")
    a = p.parse_args()
    if a.restart:
        print(stop(notify=False), "/", start(notify=False))
        return
    url = f"http://localhost:{a.port}/"
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError:  # (the page already runs - another terminal - or the port is taken)
        print(f"flight-watch control already running on {url} (or port {a.port} is taken: --port)")
        if not a.no_open:
            webbrowser.open(url)
        return
    if not a.no_open:
        webbrowser.open(url)  # (the browser's start-up messages land in this terminal: the link is printed after them)
        time.sleep(1)
    print(f"flight-watch control on {url} (Ctrl+C stops this page, not the monitor)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
