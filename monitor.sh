#!/usr/bin/env bash
# Run the monitor with dated output files (alerts_YYYY-MM-DD.jsonl, posts_..., flight_watch_....log;
# ignored by git). Extra arguments go to flight_watch.py, e.g. ./monitor.sh --ntfy-topic my-topic
# Ctrl+C stops it. To keep it running after closing the terminal: nohup ./monitor.sh &
cd "$(dirname "$0")" || exit 1
D=$(date +%F)
exec .venv/bin/python flight_watch.py --jsonl "alerts_$D.jsonl" --posts "posts_$D.jsonl" \
    --log-file "flight_watch_$D.log" "$@"
