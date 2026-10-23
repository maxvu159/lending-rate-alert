#!/bin/sh
# Cron wrapper: run a bot script with the repo's venv. Usage: deploy/run.sh alert|digest [args]
# Put TELEGRAM_BOT_TOKEN (and optionally TELEGRAM_CHAT_ID / TELEGRAM_THREAD_ID) in .env next to config.json.
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
mkdir -p data
script="$1"; shift
exec .venv/bin/python "bot/$script.py" "$@"
