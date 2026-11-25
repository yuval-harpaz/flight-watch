#!/usr/bin/env bash
# Run the monitor with dated output files (alerts_YYYY-MM-DD.jsonl, posts_..., posts_telegram_..., flight_watch_....log;
# ignored by git). Extra arguments go to flight_watch.py, e.g. ./monitor.sh --ntfy-topic my-topic
# Ctrl+C stops it. To keep it running after closing the terminal: nohup ./monitor.sh &
# Telegram messages go to: channel (TELEGRAM_CHANNEL_ID) or chat (TELEGRAM_CHAT_ID, a chat with the bot).
# Change it here, or for one run: SENDTO=chat ./monitor.sh
SENDTO=${SENDTO:-channel}
# The private feeder (FEEDER1_* in the environment): merged into the local poll (--feeder, 26 Nov 2026), and its shadow
# record goes on (feeder_shadow_<date>.jsonl: what it heard that adsb.lol / adsb.fi did not). AVSTACK_KEY: aviationstack, once a day.
# adsb.lol's trace files (backfill, the Telegram pictures, tools/read_trace.py): off since 16 Nov 2026 - adsb.lol blocks this
# network from them ("automated scraping"; refused since 11 Nov). Back to on only when adsb.lol allows it (a key, or the block lifted).
ADSBLOL_TRACES=${ADSBLOL_TRACES:-off}
TRACES=(--telegram-images)
[ "$ADSBLOL_TRACES" = on ] || TRACES=(--trace-url '')
cd "$(dirname "$0")" || exit 1
D=$(date +%F)
exec .venv/bin/python flight_watch.py --jsonl "alerts_$D.jsonl" --posts "posts_$D.jsonl" \
    --telegram-posts "posts_telegram_$D.jsonl" --log-file "flight_watch_$D.log" \
    --telegram-to "$SENDTO" "${TRACES[@]}" --state flight_watch_state.json \
    --feeder --feeder-shadow "feeder_shadow_$D.jsonl" "$@"
