# Telegram bot for the edge server. Only OS tools are installed here: the bot
# is standard-library Python and is mounted from ./telegram-bot at run time, so
# updating bot.py needs a restart, not a rebuild.
FROM python:3.12-slim

# Tools used by collect-diagnostics.sh (/diag)
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps pciutils usbutils kmod curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Fleets only (deploy passes WITH_CRYPTO=true): the signing library for the control
# plane. Without it everything else works and the bot says so in its log.
ARG WITH_CRYPTO=false
RUN if [ "$WITH_CRYPTO" = "true" ]; then pip install --no-cache-dir "cryptography>=41"; fi

WORKDIR /app
CMD ["python3", "-u", "/app/bot.py"]
