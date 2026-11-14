# Home Assistant add-on image: the edge_server bot with the Supervisor helper. Build context is the
# repository root (docker build -f ha-addon/Dockerfile .), which the Supervisor also does for a local
# add-on folder that contains a copy of these files (see ha-addon/DOCS.md).
ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}

# autonomy-core pinned to a commit (keep in step with telegram-bot/Dockerfile and pyproject.toml)
ARG AUTONOMY_CORE_REF=fde8f4749b7c3def408fc478cf25d3248d8f6db4
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "cryptography>=41" "PyYAML>=6" \
        "autonomy-core @ https://github.com/realsystem/autonomy/archive/${AUTONOMY_CORE_REF}.tar.gz#subdirectory=core/python"

WORKDIR /app
COPY telegram-bot/ /app/telegram-bot/
COPY tools/fleet.py /app/tools/fleet.py
COPY ha-addon/ /app/ha-addon/
COPY collect-diagnostics.sh /app/collect-diagnostics.sh
CMD ["/app/ha-addon/run.sh"]
