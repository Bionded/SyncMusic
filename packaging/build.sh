#!/usr/bin/env bash
# Build SyncMusic as one single-file Linux binary per target (called by packaging/build-desktop and build-web).
# Usage: packaging/build-desktop|build-web [--target TARGETS]... [--out DIR] [--python 3.13]
#   --target       linux-x86_64, linux-aarch64 (aliases: x86_64, amd64, aarch64, arm64), several comma-separated
#                  or the option repeated, or `all`. Default: the host's OS/arch. (No linux-armv7: some locked
#                  dependencies have no armv7 wheel on PyPI, see the developer notes.)
#   --out          output directory (default <repo>/dist)
#   --python       CPython minor version to bundle (default 3.13)
#   --mode         desktop|web: the mode the binary starts in without arguments (set by the wrapper scripts)
#   SYNCMUSIC_BUILD_NUMBER (environment): a build of main; its number goes into the version and the file name
#
# The binary syncmusic-<mode>-<version>-linux-<arch> is a shell launcher with a tar.gz payload appended: a
# standalone CPython (python-build-standalone, checked against its SHA256SUMS), the locked dependencies of uv.lock
# as prebuilt wheels for the target (hash-checked, released packages only, no source builds) and the app wheel. On
# first run it unpacks itself to ~/.cache/syncmusic/runtime/<id> (or $SYNCMUSIC_RUNTIME_DIR) and then starts from
# there. Nothing is compiled, so every target builds on any host. Every ELF file must match the target
# architecture, otherwise the build fails. The host's target is smoke-run (`<binary> --help`).
# Output: <out>/syncmusic-<mode>-<version>-linux-<arch> and .sha256 (paths and checksums printed).
# Needs: uv, curl, tar, gzip, sha256sum, python3 (host). Network access to PyPI, GitHub and releases.astral.sh.
# Nothing is published or uploaded.
# Examples:
#   packaging/build-web
#   packaging/build-web --target all
#   packaging/build-desktop --target linux-x86_64,linux-aarch64 --out /var/tmp/dist
#   packaging/build-desktop --target x86_64 --python 3.12
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/dist"
MODE=desktop
PY_MINOR=3.13
ARCHES=()

usage() { awk 'NR > 1 && !/^#/ {exit} NR > 1 {sub(/^# ?/, ""); print}' "$0"; }
die() { echo "build: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --target) [ $# -ge 2 ] || die "--target needs a value"
      for t in ${2//,/ }; do
        case "$t" in
          linux-x86_64|x86_64|amd64) ARCHES+=(x86_64) ;;
          linux-aarch64|aarch64|arm64) ARCHES+=(aarch64) ;;
          all) ARCHES+=(x86_64 aarch64) ;;
          *) die "unsupported target: $t (linux-x86_64, linux-aarch64, all)" ;;
        esac
      done
      shift 2 ;;
    --mode) [ $# -ge 2 ] || die "--mode needs a value"
      case "$2" in desktop|web) MODE="$2" ;; *) die "unsupported mode: $2 (desktop, web)" ;; esac
      shift 2 ;;
    --out) [ $# -ge 2 ] || die "--out needs a value"; OUT="$2"; shift 2 ;;
    --python) [ $# -ge 2 ] || die "--python needs a value"; PY_MINOR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
if [ ${#ARCHES[@]} -eq 0 ]; then
  [ "$(uname -s)" = Linux ] || die "no --target given and the host is not Linux"
  case "$(uname -m)" in
    x86_64|aarch64) ARCHES=("$(uname -m)") ;;
    *) die "no --target given and the host's arch $(uname -m) is not supported (linux-x86_64, linux-aarch64)" ;;
  esac
fi
# each target once, in the given order
mapfile -t ARCHES < <(printf '%s\n' "${ARCHES[@]}" | awk '!seen[$0]++')

UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
[ -x "$UV" ] || die "uv not found (set UV=/path/to/uv)"
for tool in curl tar gzip sha256sum python3; do
  command -v "$tool" >/dev/null || die "$tool not found"
done

VERSION="$(python3 -c 'import sys,tomllib;print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' "$ROOT/pyproject.toml")"
# a build of main (a beta pre-release) carries its build number: syncmusic-web-0.2.1-5-linux-aarch64
[ -z "${SYNCMUSIC_BUILD_NUMBER:-}" ] || VERSION="$VERSION-$SYNCMUSIC_BUILD_NUMBER"
# reproducible archive timestamps: the last commit, else the lockfile
EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || stat -c %Y "$ROOT/uv.lock")}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
DL="$OUT/downloads"
mkdir -p "$DL"

# fetch URL FILE SHA256: download once into downloads/, verify the checksum every time
fetch() {
  local url="$1" file="$DL/$2" sha="$3"
  if [ ! -f "$file" ]; then
    curl -fsSL --retry 3 -o "$file.part" "$url" || die "download failed: $url"
    mv "$file.part" "$file"
  fi
  echo "$sha  $file" | sha256sum -c --quiet - || { rm -f "$file"; die "checksum mismatch: $url"; }
}

# the app wheel and the locked requirements are the same for every architecture
WORK="$OUT/work"
rm -rf "$WORK"
mkdir -p "$WORK"
(cd "$ROOT" && "$UV" export --frozen --no-dev --no-emit-project --format requirements-txt --quiet \
  -o "$WORK/requirements.txt")
"$UV" build "$ROOT" --wheel --out-dir "$WORK/wheel" --quiet
APP_WHEEL="$(ls "$WORK"/wheel/syncmusic-*.whl)"

build_one() {
  local arch="$1" name stage py_key py_url py_file py_sha sums site
  name="syncmusic-$MODE-$VERSION-linux-$arch"
  stage="$WORK/$name"
  echo "== $name (python $PY_MINOR)"

  # standalone CPython, newest patch release of PY_MINOR that uv knows a download for
  read -r py_key py_url < <("$UV" python list --all-platforms --all-arches --only-downloads \
    --output-format json | python3 -c '
import json, sys
minor, arch = sys.argv[1], sys.argv[2]
c = [p for p in json.load(sys.stdin) if p["implementation"] == "cpython" and p["os"] == "linux"
     and p["arch"] == arch and p["libc"] == "gnu" and p["variant"] == "default" and p.get("url")
     and ".".join(p["version"].split(".")[:2]) == minor and p["version_parts"].get("patch") is not None
     and not any(x in p["version"] for x in "abrc")]
c.sort(key=lambda p: (p["version_parts"]["patch"],), reverse=True)
print(c[0]["key"], c[0]["url"]) if c else sys.exit(1)' "$PY_MINOR" "$arch") \
    || die "no CPython $PY_MINOR download for linux-$arch"
  py_file="$(basename "$py_url" | sed 's/%2B/+/g')"
  sums="$DL/SHA256SUMS-$(basename "$(dirname "$py_url")")"
  [ -f "$sums" ] || curl -fsSL --retry 3 -o "$sums" "$(dirname "$py_url")/SHA256SUMS" \
    || die "cannot fetch SHA256SUMS for $py_url"
  py_sha="$(awk -v f="$py_file" '$2 == f {print $1}' "$sums")"
  [ -n "$py_sha" ] || die "$py_file is not listed in $(dirname "$py_url")/SHA256SUMS"
  fetch "$py_url" "$py_file" "$py_sha"

  rm -rf "$stage"
  mkdir -p "$stage"
  tar -xzf "$DL/$py_file" -C "$stage"            # -> python/
  site="$(echo "$stage"/python/lib/python3*/site-packages)"
  [ -d "$site" ] || die "no site-packages in the CPython archive"

  # locked dependencies as wheels for the target, then the app itself
  "$UV" pip install --quiet --target "$site" --python-version "$PY_MINOR" \
    --python-platform "$arch-manylinux_2_28" --only-binary :all: --require-hashes --no-deps \
    -r "$WORK/requirements.txt"
  "$UV" pip install --quiet --target "$site" --python-version "$PY_MINOR" \
    --python-platform "$arch-manylinux_2_28" --no-deps "$APP_WHEEL"
  # reproducible: drop uv's install-time cache key and the local wheel path from the app's metadata
  local info
  info="$(echo "$site"/syncmusic-*.dist-info)"
  rm -f "$info/uv_cache.json" "$info/direct_url.json"
  sed -i '/\/uv_cache\.json,/d; /\/direct_url\.json,/d' "$info/RECORD"
  # the binary's default mode, kind of install and build number (SYNCMUSIC_BUILD_NUMBER: a build of main)
  local kind=standalone build="${SYNCMUSIC_BUILD_NUMBER:-}"
  [ "$MODE" = desktop ] && kind=desktop
  [ -z "$build" ] || [[ "$build" =~ ^[0-9]+$ ]] || die "SYNCMUSIC_BUILD_NUMBER must be a number"
  sed -i -e "s/^DEFAULT_MODE = .*/DEFAULT_MODE = \"$MODE\"/" \
    -e "s/^INSTALL_TYPE: .*/INSTALL_TYPE: str | None = \"$kind\"/" \
    -e "s/^BUILD_NUMBER: .*/BUILD_NUMBER: int | None = ${build:-None}/" "$site/syncmusic/_build.py"
  grep -q "^DEFAULT_MODE = \"$MODE\"\$" "$site/syncmusic/_build.py" || die "could not set DEFAULT_MODE"
  grep -q "^INSTALL_TYPE: str | None = \"$kind\"\$" "$site/syncmusic/_build.py" || die "could not set INSTALL_TYPE"
  python3 - "$site" "$info/RECORD" <<'PY'
import base64, hashlib, sys
site, record = sys.argv[1], sys.argv[2]
data = open(f"{site}/syncmusic/_build.py", "rb").read()
digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
lines = open(record).read().splitlines()
lines = [f"syncmusic/_build.py,sha256={digest},{len(data)}" if l.startswith("syncmusic/_build.py,") else l
         for l in lines]
open(record, "w").write("\n".join(lines) + "\n")
PY

  # the root helper next to the interpreter, where the app looks for it (infra/blockdev/mount.py)
  printf '#!/bin/sh\nexec "$(dirname "$0")/python3" -E -s -m syncmusic.infra.blockdev.priv_helper "$@"\n' \
    > "$stage/python/bin/syncmusic-priv"
  chmod 0755 "$stage/python/bin/syncmusic-priv"
  printf 'SyncMusic %s %s binary for linux-%s\npython: %s\nbuilt from: %s\n' \
    "$VERSION" "$MODE" "$arch" "$py_key" "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
    > "$stage/BUILD-INFO.txt"

  # every ELF file must be for the target architecture
  python3 - "$stage" "$arch" <<'EOF' || die "architecture check failed for $name"
import os, struct, sys
root, arch = sys.argv[1], sys.argv[2]
want = {"x86_64": 62, "aarch64": 183}[arch]
bad, n = [], 0
for d, _, files in os.walk(root):
    for f in files:
        p = os.path.join(d, f)
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        with open(p, "rb") as fh:
            h = fh.read(20)
        if len(h) < 20 or h[:4] != b"\x7fELF":
            continue
        n += 1
        machine = struct.unpack(("<" if h[5] == 1 else ">") + "H", h[18:20])[0]
        if machine != want:
            bad.append(f"{os.path.relpath(p, root)} (e_machine {machine})")
print(f"  {n} ELF files, all {arch}" if not bad else "\n".join(["  wrong architecture:"] + bad))
sys.exit(1 if bad or n == 0 else 0)
EOF

  find "$stage" -name __pycache__ -prune -exec rm -rf {} +
  local payload="$WORK/$name.tar.gz" bin="$OUT/$name" id
  tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$EPOCH" -C "$stage" -cf - . \
    | gzip -n -9 > "$payload"
  id="$VERSION-$MODE-$arch-$(sha256sum "$payload" | cut -c1-12)"
  sed -e "s/@ID@/$id/; s/@ARCH@/$arch/; s/@MODE@/$MODE/; s/@VERSION@/$VERSION/" "$ROOT/packaging/launcher.in" > "$bin"
  grep -q '^__PAYLOAD__$' "$bin" || die "launcher has no __PAYLOAD__ marker"
  cat "$payload" >> "$bin"
  chmod 0755 "$bin"

  local sysroot="/usr/$arch-linux-gnu" loader home="$WORK/smoke"
  case "$arch" in x86_64) loader=ld-linux-x86-64.so.2 ;; aarch64) loader=ld-linux-aarch64.so.1 ;; esac
  if [ "$(uname -m)" = "$arch" ]; then
    local usage
    # plain text: Typer forces colours when GITHUB_ACTIONS or FORCE_COLOR is set (the release workflow)
    usage=$(SYNCMUSIC_RUNTIME_DIR="$home" _TYPER_FORCE_DISABLE_TERMINAL=1 "$bin" --help) \
      || die "smoke run failed: $name --help"
    grep -q '^ *Usage: syncmusic ' <<<"$usage" || die "smoke run: $name --help has no 'Usage: syncmusic' line"
    echo "  smoke run ok ($name --help)"
  elif command -v "qemu-$arch" >/dev/null && [ -f "$sysroot/lib/$loader" ]; then
    # user-mode emulation through the target's loader (a 16 KiB-page host cannot map the non-PIE python3)
    QEMU_LD_PREFIX="$sysroot" "qemu-$arch" "$sysroot/lib/$loader" --library-path "$sysroot/lib:$stage/python/lib" \
      "$(readlink -f "$stage/python/bin/python3")" -E -s -m syncmusic --help >/dev/null 2>&1 \
      || die "emulated smoke run failed: $name (qemu-$arch)"
    echo "  smoke run ok (syncmusic --help emulated by qemu-$arch)"
  else
    echo "  not smoke-run: host is $(uname -m) (install qemu-user and the $arch glibc cross runtime to emulate)"
  fi

  (cd "$OUT" && sha256sum "$name" > "$name.sha256")
  echo "  artifact: $bin"
  echo "  sha256:   $(cut -d' ' -f1 "$OUT/$name.sha256")"
}

for arch in "${ARCHES[@]}"; do
  build_one "$arch"
done
rm -rf "$WORK"
