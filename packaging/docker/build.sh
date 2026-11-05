#!/usr/bin/env bash
# Build the SyncMusic Docker image locally. Never pushes.
# Usage: packaging/docker/build.sh [--platform linux/arm64,linux/amd64|all] [--tag NAME]
#   --platform   one or more of linux/arm64, linux/amd64 (comma-separated) or `all`; default: the host's
#   --tag        image name (default syncmusic:local); with several platforms each also gets NAME-<arch>
# Each platform is built and loaded into the local Docker separately (`docker buildx build --load`). A foreign
# platform needs binfmt emulation for the image's apt step (e.g. `docker run --privileged --rm tonistiigi/binfmt
# --install amd64`); the Python dependencies never run emulated.
# Examples:
#   packaging/docker/build.sh
#   packaging/docker/build.sh --platform all
#   packaging/docker/build.sh --platform linux/amd64 --tag syncmusic:test
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG=syncmusic:local
PLATFORMS=()
die() { echo "docker/build: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) [ $# -ge 2 ] || die "--platform needs a value"
      for p in ${2//,/ }; do
        case "$p" in
          linux/arm64|linux/amd64) PLATFORMS+=("$p") ;;
          all) PLATFORMS+=(linux/arm64 linux/amd64) ;;
          *) die "unsupported platform: $p (linux/arm64, linux/amd64, all)" ;;
        esac
      done
      shift 2 ;;
    --tag) [ $# -ge 2 ] || die "--tag needs a value"; TAG="$2"; shift 2 ;;
    -h|--help) awk 'NR > 1 && !/^#/ {exit} NR > 1 {sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
if [ ${#PLATFORMS[@]} -eq 0 ]; then
  case "$(uname -m)" in aarch64) PLATFORMS=(linux/arm64) ;; x86_64) PLATFORMS=(linux/amd64) ;;
    *) die "host arch $(uname -m) not supported: give --platform" ;; esac
fi
command -v docker >/dev/null || die "docker not found"
# the Dockerfile cross-builds with $BUILDPLATFORM/TARGETARCH: BuildKit through the buildx plugin
docker buildx version >/dev/null 2>&1 \
  || die "docker buildx not found: install the plugin (docker-buildx on Arch, docker-buildx-plugin on Debian/Ubuntu)"
for p in "${PLATFORMS[@]}"; do
  tags=(-t "$TAG-${p#linux/}")
  [ "$p" = "${PLATFORMS[0]}" ] && tags+=(-t "$TAG")
  echo "== $p"
  docker buildx build --platform "$p" --load "${tags[@]}" -f "$ROOT/packaging/docker/Dockerfile" "$ROOT"
done
echo "built: ${PLATFORMS[*]} as $TAG (nothing pushed)"
