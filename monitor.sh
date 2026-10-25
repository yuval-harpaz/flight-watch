#!/usr/bin/env bash
# Run the monitor with dated output files (alerts_YYYY-MM-DD.jsonl, posts_..., posts_telegram_..., flight_watch_....log;
# ignored by git). Extra arguments go to flight_watch.py, e.g. ./monitor.sh --ntfy-topic my-topic
# Ctrl+C stops it. To keep it running after closing the terminal: nohup ./monitor.sh &
# Telegram messages go to: channel (TELEGRAM_CHANNEL_ID) or chat (TELEGRAM_CHAT_ID, a chat with the bot).
# Change it here, or for one run: SENDTO=chat ./monitor.sh
SENDTO=${SENDTO:-channel}
cd "$(dirname "$0")" || exit 1
D=$(date +%F)
exec .venv/bin/python flight_watch.py --jsonl "alerts_$D.jsonl" --posts "posts_$D.jsonl" \
    --telegram-posts "posts_telegram_$D.jsonl" --log-file "flight_watch_$D.log" \
    --telegram-to "$SENDTO" --state flight_watch_state.json "$@"
