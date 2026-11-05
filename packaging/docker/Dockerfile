# syntax=docker/dockerfile:1
# SyncMusic web image for linux/arm64 and linux/amd64. Build with packaging/docker/build.sh (local
# only, never pushed). The Python dependencies are installed for the TARGET arch from prebuilt wheels on the BUILD
# platform (uv --python-platform, as packaging/build.sh does), so only the apt step runs under emulation.
ARG PYTHON=3.13

FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM --platform=$BUILDPLATFORM python:${PYTHON}-slim-trixie AS build
ARG PYTHON
ARG TARGETARCH
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE THIRD-PARTY.md ./
COPY syncmusic ./syncmusic
RUN set -eu; \
    case "$TARGETARCH" in amd64) arch=x86_64 ;; arm64) arch=aarch64 ;; *) echo "unsupported: $TARGETARCH"; exit 1 ;; esac; \
    uv export --frozen --no-dev --no-emit-project --format requirements-txt --quiet -o /tmp/req.txt; \
    uv pip install --quiet --target /opt/site --python-version "$PYTHON" --python-platform "$arch-manylinux_2_28" \
      --only-binary :all: --require-hashes --no-deps -r /tmp/req.txt; \
    uv build --wheel --out-dir /tmp/wheel --quiet; \
    uv pip install --quiet --target /opt/site --python-version "$PYTHON" --python-platform "$arch-manylinux_2_28" \
      --no-deps /tmp/wheel/syncmusic-*.whl

FROM python:${PYTHON}-slim-trixie
# ffmpeg for converting; the block-device tools only matter in the usb variant
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fdisk parted udev dosfstools exfatprogs e2fsprogs ntfs-3g \
      sg3-utils eject smbclient \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/site /opt/site
COPY packaging/docker/entrypoint.sh /usr/local/bin/syncmusic-entrypoint
COPY LICENSE THIRD-PARTY.md /usr/share/doc/syncmusic/
ENV PYTHONPATH=/opt/site \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    SYNCMUSIC_IN_DOCKER=1 \
    SYNCMUSIC_MODE=web \
    SYNCMUSIC_HOST=0.0.0.0 \
    SYNCMUSIC_PORT=8080 \
    SYNCMUSIC_CONFIG=/config/config.toml \
    SYNCMUSIC_DATA_DIR=/data \
    SYNCMUSIC_CACHE_DIR=/cache
VOLUME ["/config", "/data", "/cache"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python3 -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/v1/health' % os.environ.get('SYNCMUSIC_PORT', '8080'), timeout=4)"
ENTRYPOINT ["syncmusic-entrypoint"]
CMD ["run"]
