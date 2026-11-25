#!/usr/bin/env bash
# The nightly restart, at 03:00 UTC (a systemd user timer: `tools/nightly.sh install` once; `tools/nightly.sh` runs it now):
# main brought up to GitHub's, and the monitor restarted (quietly, no Telegram notice) so the new day's files begin.
# `tools/nightly.sh health` (its own timer, every 15 min): tools/health_check.py tells the owner's private chat when something
# is badly wrong (the monitor stopped, a source of data gone).
# The unattended Claude review and its publishing (tools/nightly_prompt.md, daily_digest.py, nightly_publish.py) were stopped on
# 25 Nov 2026 (the owner: false alarms are acceptable); the scripts stay. The run's output: reviews/<date>.nightly.log.
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT=$PWD
D=$(date -u +%F)
UNIT=flight-watch-nightly

if [ "${1:-}" = install ]; then
    mkdir -p ~/.config/systemd/user
    cat > ~/.config/systemd/user/$UNIT.service <<UNITEOF
[Unit]
Description=flight-watch nightly restart and review

[Service]
Type=oneshot
ExecStart=$ROOT/tools/nightly.sh
# the monitor it restarts must outlive this run
KillMode=process
UNITEOF
    cat > ~/.config/systemd/user/$UNIT.timer <<UNITEOF
[Unit]
Description=flight-watch nightly at 03:00 UTC

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
UNITEOF
    cat > ~/.config/systemd/user/$UNIT-health.service <<UNITEOF
[Unit]
Description=flight-watch health check (tells the owner's private chat when something is badly wrong)

[Service]
Type=oneshot
ExecStart=$ROOT/tools/nightly.sh health
UNITEOF
    cat > ~/.config/systemd/user/$UNIT-health.timer <<UNITEOF
[Unit]
Description=flight-watch health check every 15 min

[Timer]
OnCalendar=*:0/15
Persistent=true

[Install]
WantedBy=timers.target
UNITEOF
    systemctl --user daemon-reload && systemctl --user enable --now $UNIT.timer $UNIT-health.timer
    systemctl --user list-timers $UNIT.timer $UNIT-health.timer --no-pager
    exit
fi
if [ "${1:-}" = health ]; then
    eval "$(grep -E '^export TELEGRAM_[A-Z_]+=' ~/.bashrc)"
    exec .venv/bin/python tools/health_check.py
fi

mkdir -p reviews
exec >>"reviews/$D.nightly.log" 2>&1
echo "=== $(date -u '+%F %T') UTC"
# the monitor's variables: ~/.bashrc stops early when not interactive, so only its export lines are read
eval "$(grep -E '^export (TELEGRAM_[A-Z_]+|RELAY_TOKEN|SENDTO|FEEDER1_[A-Z]+|AVSTACK_KEY)=' ~/.bashrc)"
NODE=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | tail -1)  # the page tests run flightboard.js in node
export PATH="$HOME/.local/bin:${NODE:+$NODE:}/usr/local/bin:/usr/bin:/bin"

# what the owner pushed or merged on GitHub comes first (a local main that differs is reported by nightly_publish.py)
git fetch -q origin && git merge -q --ff-only origin/main || echo "main not brought up to origin/main"
.venv/bin/python tools/dashboard.py --restart
echo "restarted; main at $(git rev-parse --short HEAD)"
