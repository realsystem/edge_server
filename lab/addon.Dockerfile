# The lab's add-on image: the add-on built from this checkout (ha-addon/Dockerfile, built first as
# BASE) plus autonomy-core from the LOCAL checkout (build context "autonomy"), like bot.Dockerfile.
ARG BASE=alab-ha-addon-base:lab
FROM ${BASE}
COPY --from=autonomy core/python /tmp/autonomy-core
RUN pip install --no-cache-dir /tmp/autonomy-core && rm -rf /tmp/autonomy-core
