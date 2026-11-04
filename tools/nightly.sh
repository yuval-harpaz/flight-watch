#!/usr/bin/env bash
# The nightly cycle, at 03:00 UTC (a systemd user timer: `tools/nightly.sh install` once; `tools/nightly.sh` runs it now):
#  1. restart the monitor (quietly, no Telegram notice) so the new day's files begin;
#  2. tools/daily_digest.py: the day that ended in one page, reviews/<date>_digest.md;
#  3. an unattended Claude Code review (tools/nightly_prompt.md) in a worktree ../flight-watch-nightly on branch
#     nightly/<date>: a verdict per Telegram message, its recommendations implemented and committed there with tests,
#     reviews/<date>.md, reviews/ISSUES.md;
#  4. tools/nightly_publish.py: its own checks, then merged into main and pushed, and the monitor restarted on the new code.
#     Held back (the branch stays: `git merge nightly/<date>`) when a fix may lose an alert or the review suspects a new
#     category of issue: the owner's private Telegram chat (TELEGRAM_CHAT_ID, never the channel) says why.
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

# what the owner pushed or merged on GitHub comes first (a local main that differs is reported by nightly_publish.py)
git fetch -q origin && git merge -q --ff-only origin/main || echo "main not brought up to origin/main"
.venv/bin/python tools/dashboard.py --restart
.venv/bin/python tools/daily_digest.py > "reviews/${D}_digest.md" || exit 1
touch reviews/ISSUES.md

WT=$(dirname "$ROOT")/flight-watch-nightly
git worktree remove --force "$WT" 2>/dev/null
git worktree prune
BASE=$(git rev-parse main)
git worktree add -B "nightly/$D" "$WT" main || exit 1
PROMPT=$(sed "s#{DATE}#$D#g; s#{ROOT}#$ROOT#g" tools/nightly_prompt.md)
PY="$ROOT/.venv/bin/python"
cd "$WT" || exit 1
timeout 50m claude -p "$PROMPT" ${MODEL:+--model "$MODEL"} --add-dir "$ROOT" --permission-mode acceptEdits \
    --allowedTools Read Edit Write Grep Glob "Bash(git status*)" "Bash(git diff*)" "Bash(git log*)" "Bash(git add*)" \
        "Bash(git commit*)" "Bash(git checkout -- *)" "Bash($PY *)" \
        "Bash(grep *)" "Bash(head *)" "Bash(tail *)" "Bash(sed -n *)" "Bash(wc *)" "Bash(ls *)" \
    --disallowedTools "Bash(git push*)" > "$ROOT/reviews/$D.claude.txt"
CLAUDE_EXIT=$?
echo "claude exit $CLAUDE_EXIT; commits: $(git rev-list --count "$BASE"..HEAD)"
cd "$ROOT" || exit 1
"$PY" tools/nightly_publish.py --date "$D" --root "$ROOT" --worktree "$WT" --base "$BASE" --claude-exit "$CLAUDE_EXIT"
if [ $? = 10 ]; then  # the checks changed: the monitor runs them from now, not tomorrow
    .venv/bin/python tools/dashboard.py --restart
fi
