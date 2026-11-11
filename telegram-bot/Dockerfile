# Telegram bot for the edge server. Only OS tools are installed here: the bot
# is standard-library Python and is mounted from ./telegram-bot at run time, so
# updating bot.py needs a restart, not a rebuild.
FROM python:3.12-slim

# Tools used by collect-diagnostics.sh (/diag)
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps pciutils usbutils kmod curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Fleets only (deploy passes WITH_CRYPTO=true): the signing libraries for the control
# plane - cryptography and autonomy-core (realsystem/autonomy, pinned to a commit; keep
# in step with pyproject.toml). Without them everything else works and the bot says so
# in its log.
ARG WITH_CRYPTO=false
ARG AUTONOMY_CORE_REF=4a73ddc30d94054fff5e3db5fd2316d71ac3a392
RUN if [ "$WITH_CRYPTO" = "true" ]; then pip install --no-cache-dir "cryptography>=41" \
        "autonomy-core @ https://github.com/realsystem/autonomy/archive/${AUTONOMY_CORE_REF}.tar.gz#subdirectory=core/python"; \
    fi

WORKDIR /app
CMD ["python3", "-u", "/app/bot.py"]
