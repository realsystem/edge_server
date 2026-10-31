# Telegram bot for the edge server. Only OS tools are installed here: the bot
# is standard-library Python and is mounted from ./telegram-bot at run time, so
# updating bot.py needs a restart, not a rebuild.
FROM python:3.12-slim

# Tools used by collect-diagnostics.sh (/diag)
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps pciutils usbutils kmod curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
CMD ["python3", "-u", "/app/bot.py"]
