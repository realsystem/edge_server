# The lab's bot image: the production image (telegram-bot/Dockerfile, built first as BASE)
# plus autonomy-core from the LOCAL checkout (build context "autonomy"), so uncommitted
# changes in both repos are what runs. With --pinned the production image is used as is.
ARG BASE=edge-telegram-bot:lab-base
FROM ${BASE}
RUN pip install --no-cache-dir "cryptography>=41"
COPY --from=autonomy core/python /tmp/autonomy-core
RUN pip install --no-cache-dir /tmp/autonomy-core && rm -rf /tmp/autonomy-core
