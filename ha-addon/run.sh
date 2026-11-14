#!/usr/bin/env bash
# Entry point of the add-on: prepare the options, start the Supervisor helper, run the bot.
set -euo pipefail
python3 /app/ha-addon/addon-setup.py /data/options.json /data
# shellcheck source=/dev/null
. /data/app/env
python3 -u /app/ha-addon/supervisor-helper.py &
exec python3 -u /app/telegram-bot/bot.py
