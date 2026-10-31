FROM python:3.11-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    ca-certificates curl tini eject util-linux dvdbackup libexpat1 libssl3 zlib1g \
    cd-paranoia libcdio-utils flac lame opus-tools vorbis-tools libarchive-tools ffmpeg \
    && rm -rf /var/lib/apt/lists/*

FROM base AS build
WORKDIR /build
COPY tools/makemkv-install.sh packaging/dvdtools-install.sh packaging/makemkv-manifest.json ./
ENV RIPARR_MAKEMKV_MANIFEST=/build/makemkv-manifest.json
RUN bash makemkv-install.sh --accept-eula --jobs 2 \
    && (bash dvdtools-install.sh || { cat /run/riparr/dvdtools.log; exit 1; })
RUN mkdir -p /runtime/usr/bin /runtime/usr/lib /runtime/usr/share /runtime/usr/local/lib/riparr \
    && cp "$(command -v makemkvcon)" /runtime/usr/bin/ \
    && find /usr/lib -name 'libmakemkv.so*' -exec cp -a --parents '{}' /runtime/ ';' \
    && find /usr/lib -name 'libdriveio.so*' -exec cp -a --parents '{}' /runtime/ ';' \
    && find /usr/lib -name 'libmmbd.so*' -exec cp -a --parents '{}' /runtime/ ';' \
    && cp -a /usr/local/lib/libdvdcss.so* /runtime/usr/local/lib/ \
    && cp /usr/local/lib/riparr/makemkv.version /runtime/usr/local/lib/riparr/ \
    && find /usr/share -maxdepth 1 -iname '*makemkv*' -exec cp -a --parents '{}' /runtime/ ';'
COPY server/requirements.txt ./requirements.txt
RUN python -m venv /opt/riparr/.venv \
    && /opt/riparr/.venv/bin/pip install --no-cache-dir -r requirements.txt

FROM base AS runtime
ARG RIPARR_VERSION=dev
ARG RIPARR_REVISION=unknown
LABEL org.opencontainers.image.title="Riparr" \
      org.opencontainers.image.version="$RIPARR_VERSION" \
      org.opencontainers.image.revision="$RIPARR_REVISION" \
      org.opencontainers.image.source="https://github.com/jackharvest/riparr"
COPY --from=build /runtime/ /
COPY --from=build /opt/riparr/.venv /opt/riparr/.venv
COPY server/riparr /opt/riparr/server/riparr
COPY server/static /opt/riparr/server/static
COPY server/requirements.txt /opt/riparr/server/requirements.txt
COPY LICENSE /opt/riparr/LICENSE
COPY packaging/makemkv-manifest.json /opt/riparr/packaging/makemkv-manifest.json
COPY --chmod=0755 packaging/docker/entrypoint.sh /usr/local/bin/riparr-entrypoint
RUN ldconfig \
    && groupadd --gid 1000 riparr \
    && useradd --uid 1000 --gid 1000 --home-dir /var/lib/riparr --no-create-home riparr \
    && mkdir -p /var/lib/riparr/.MakeMKV /srv/staging /srv/library /run/riparr \
    && chown -R 1000:1000 /var/lib/riparr /srv/staging /srv/library /run/riparr \
    && ldd /usr/bin/makemkvcon | tee /tmp/makemkv-libraries \
    && ! grep -q 'not found' /tmp/makemkv-libraries \
    && rm /tmp/makemkv-libraries
ENV RIPARR_DEPLOYMENT=container RIPARR_APPLIANCE=1 RIPARR_PORT=9797 \
    RIPARR_DB=/var/lib/riparr/riparr.db HOME=/var/lib/riparr \
    PYTHONPATH=/opt/riparr/server PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/riparr/server
USER 1000:1000
EXPOSE 9797
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD /opt/riparr/.venv/bin/python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['RIPARR_PORT']+'/api/setup/state',timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/riparr-entrypoint"]