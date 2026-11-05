#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Prepare a SyncMusic release: checks, the version, the tag. Nothing is pushed or published: pushing main and the tag
# (git push origin main vVERSION) starts the GitHub release workflow, which builds the binaries for every target,
# the Docker image (amd64 and arm64) and SHA256SUMS, and publishes the release with the CHANGELOG entry as notes.
# Usage: scripts/release.sh VERSION [--dry-run] [--build [--docker] [--out DIR]]
#   VERSION      e.g. 0.2.0 (CHANGELOG.md must have a "## [VERSION]" section)
#   --dry-run    run the checks and print every step without changing anything
#   --build      also build what the workflow will publish, locally, to look at it first: the web and desktop
#                binaries for every target, SHA256SUMS and RELEASE-NOTES.md
#   --docker     with --build: the arm64 Docker image syncmusic:VERSION too (needs the docker buildx plugin)
#   --out        with --build: the output directory (default <repo>/dist/release-VERSION)
# Steps: checks (on main, clean tree, CHANGELOG entry, tag vVERSION not there yet), the version in pyproject.toml
# (+ uv.lock, committed when it changes), [the local build], the annotated git tag vVERSION (local), then main moves
# on to the next patch version (committed) so the beta builds of main (v<next>-<build>, the release workflow) sort
# after this release and before the next one: 0.2.0 → main says 0.2.1 → betas 0.2.1-1, 0.2.1-2, … → release 0.2.1.
# Examples:
#   scripts/release.sh 0.2.0 --dry-run
#   scripts/release.sh 0.2.0 && git push origin main v0.2.0
#   scripts/release.sh 0.2.1 --build --out /var/tmp/release
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
usage() { awk 'NR > 2 && !/^#/ {exit} NR > 2 {sub(/^# ?/, ""); print}' "$0"; }
die() { echo "release: $*" >&2; exit 1; }

VERSION="" DRY=0 BUILD=0 DOCKER=0 OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --build) BUILD=1; shift ;;
    --docker) DOCKER=1; shift ;;
    --out) [ $# -ge 2 ] || die "--out needs a value"; OUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) [ -z "$VERSION" ] || die "one VERSION only"; VERSION="$1"; shift ;;
  esac
done
[ -n "$VERSION" ] || { usage; exit 2; }
[ "$BUILD" = 1 ] || { [ "$DOCKER" = 0 ] && [ -z "$OUT" ]; } || die "--docker and --out need --build"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-]?(a|b|rc)[0-9]+)?$ ]] || die "not a version: $VERSION"
OUT="${OUT:-$ROOT/dist/release-$VERSION}"
TAG="v$VERSION"
cd "$ROOT"

run() { # a step: printed, and run unless --dry-run
  echo "+ $*"
  [ "$DRY" = 1 ] || "$@"
}

# --- checks -----------------------------------------------------------------------------------------------------
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "not on main"
[ -z "$(git status --porcelain)" ] || die "the working tree is not clean"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG exists already"
NOTES="$(awk -v v="$VERSION" '
  index($0, "## [" v "]") == 1 {on = 1; next}
  on && /^## \[/ {exit}
  on {print}' CHANGELOG.md | sed -e '/./,$!d')"
[ -n "$NOTES" ] || die "CHANGELOG.md has no \"## [$VERSION]\" section"
echo "release: $TAG from $(git rev-parse --short HEAD)$([ "$BUILD" = 1 ] && echo " built into $OUT")$([ "$DRY" = 1 ] && echo ' (dry run)')"

# --- version ----------------------------------------------------------------------------------------------------
CURRENT="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)"
if [ "$CURRENT" != "$VERSION" ]; then
  run sed -i "0,/^version = \".*\"$/s//version = \"$VERSION\"/" pyproject.toml
  run sed -i "s/^__version__ = \".*\"$/__version__ = \"$VERSION\"/" syncmusic/__init__.py
  run uv lock
  run git commit -q -m "chore: release $VERSION" -- pyproject.toml uv.lock syncmusic/__init__.py
fi

# --- local build (optional: the release workflow builds and publishes these) ------------------------------------
if [ "$BUILD" = 1 ]; then
  run mkdir -p "$OUT"
  run packaging/build-web --target all --out "$OUT"
  run packaging/build-desktop --target all --out "$OUT"
  if [ "$DOCKER" = 1 ]; then
    run packaging/docker/build.sh --platform linux/arm64 --tag "syncmusic:$VERSION"
  fi
  if [ "$DRY" = 1 ]; then
    echo "+ SHA256SUMS of $OUT/syncmusic-*"
  else
    (cd "$OUT" && rm -f ./*.sha256 && sha256sum syncmusic-* > SHA256SUMS)
    printf '# SyncMusic %s\n\n%s\n' "$VERSION" "$NOTES" > "$OUT/RELEASE-NOTES.md"
  fi
fi
if [ "$DRY" = 1 ]; then
  echo "+ release notes (the CHANGELOG entry):"
  printf '%s\n' "$NOTES" | sed 's/^/    /'
fi

# --- tag --------------------------------------------------------------------------------------------------------
run git tag -a "$TAG" -m "SyncMusic $VERSION"
IFS=. read -r MAJOR MINOR PATCH <<< "${VERSION%%[!0-9.]*}"
NEXT="$MAJOR.$MINOR.$((PATCH + 1))"
run sed -i "0,/^version = \".*\"$/s//version = \"$NEXT\"/" pyproject.toml
run sed -i "s/^__version__ = \".*\"$/__version__ = \"$NEXT\"/" syncmusic/__init__.py
run uv lock
run git commit -q -m "chore: main is $NEXT (betas until the next release)" -- pyproject.toml uv.lock syncmusic/__init__.py
echo "release: done. Nothing was pushed$([ "$BUILD" = 1 ] && echo ": look at $OUT")."
echo "Publish: git push origin main $TAG - the release workflow builds every target and the Docker image."
