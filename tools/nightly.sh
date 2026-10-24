#!/usr/bin/env bash
# The nightly cycle, at 03:00 UTC (a systemd user timer: `tools/nightly.sh install` once; `tools/nightly.sh` runs it now):
#  1. restart the monitor (quietly, no Telegram notice) so the new day's files begin;
#  2. tools/daily_digest.py: the day that ended in one page, reviews/<date>_digest.md;
#  3. an unattended Claude Code review (tools/nightly_prompt.md) in a worktree ../flight-watch-nightly on branch
#     nightly/<date>: a verdict per Telegram message, obvious fixes committed there with tests, reviews/<date>.md, open
#     questions for the owner in reviews/ISSUES.md. Nothing is pushed: merge a night's branch with
#     `git merge nightly/<date>` (after a look: `git log -p main..nightly/<date>`), then restart the monitor.
# reviews/ is git-ignored. The run's own output: reviews/<date>.nightly.log. MODEL=sonnet picks a cheaper model.
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
    systemctl --user daemon-reload && systemctl --user enable --now $UNIT.timer
    systemctl --user list-timers $UNIT.timer --no-pager
    exit
fi

mkdir -p reviews
exec >>"reviews/$D.nightly.log" 2>&1
echo "=== $(date -u '+%F %T') UTC"
# the monitor's variables: ~/.bashrc stops early when not interactive, so only its export lines are read
eval "$(grep -E '^export (TELEGRAM_[A-Z_]+|RELAY_TOKEN|SENDTO)=' ~/.bashrc)"
NODE=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | tail -1)  # the page tests run flightboard.js in node
export PATH="$HOME/.local/bin:${NODE:+$NODE:}/usr/local/bin:/usr/bin:/bin"

.venv/bin/python tools/dashboard.py --restart
.venv/bin/python tools/daily_digest.py > "reviews/${D}_digest.md" || exit 1
touch reviews/ISSUES.md

WT=$(dirname "$ROOT")/flight-watch-nightly
git worktree remove --force "$WT" 2>/dev/null
git worktree prune
git worktree add -B "nightly/$D" "$WT" main || exit 1
PROMPT=$(sed "s#{DATE}#$D#g; s#{ROOT}#$ROOT#g" tools/nightly_prompt.md)
PY="$ROOT/.venv/bin/python"
cd "$WT" || exit 1
timeout 50m claude -p "$PROMPT" ${MODEL:+--model "$MODEL"} --add-dir "$ROOT" --permission-mode acceptEdits \
    --allowedTools Read Edit Write Grep Glob "Bash(git status*)" "Bash(git diff*)" "Bash(git log*)" "Bash(git add*)" \
        "Bash(git commit*)" "Bash(git checkout -- *)" "Bash($PY *)" "Bash(rm -rf tmp_squawks*)" "Bash(cp -r *)" \
        "Bash(grep *)" "Bash(head *)" "Bash(tail *)" "Bash(sed -n *)" "Bash(wc *)" "Bash(ls *)" \
    --disallowedTools "Bash(git push*)" > "$ROOT/reviews/$D.claude.txt"
echo "claude exit $?; commits: $(git rev-list --count main..HEAD)"
