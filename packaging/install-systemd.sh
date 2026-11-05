#!/usr/bin/env bash
# Install or update SyncMusic from this repo (app + venv) and its systemd unit: the repo's counterpart of the
# binary's `syncmusic --install` (packaging/install.sh stays as an alias for older installs' self-update).
# Dependencies, pygpod included, come from the package index as pinned in uv.lock.
# Usage: sudo packaging/install-systemd.sh [--prefix DIR] [--unit-dir DIR] [--unit-name NAME] [--env KEY=VALUE]...
#                                  [--user NAME]...
#   --prefix    install root (default /opt/syncmusic): DIR/app, DIR/.venv
#   --unit-dir  where to write NAME.service (default /etc/systemd/system; any other dir, e.g. a
#               test prefix, skips `systemctl daemon-reload`)
#   --unit-name name of the unit (default syncmusic)
#   --env       an extra Environment= line in the unit (repeatable), e.g. SYNCMUSIC_PORT=8080
#   --user      let this normal desktop user mount / format / eject / time-sync devices through the root helper
#               DIR/.venv/bin/syncmusic-priv (repeatable): a sudoers drop-in allows exactly that
#               program, nothing else (SUDOERS_DIR, default /etc/sudoers.d)
# Idempotent: a second run updates the copies and the venv. The service is never started or restarted;
# after an update run `systemctl restart <unit>` yourself. uv: $UV, `uv` on PATH, or the invoking user's
# ~/.local/bin/uv.
# Examples:
#   sudo packaging/install-systemd.sh
#   sudo packaging/install-systemd.sh --env SYNCMUSIC_PORT=8080 --user alice
#   sudo packaging/install-systemd.sh --prefix /opt/sm-test --unit-name sm-test --unit-dir /var/tmp/units
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/syncmusic
UNIT_DIR=/etc/systemd/system
UNIT_NAME=syncmusic
ENVS=()
USERS=()
SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USERS+=("$2"); shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --unit-dir) UNIT_DIR="$2"; shift 2 ;;
    --unit-name) UNIT_NAME="$2"; shift 2 ;;
    --env) ENVS+=("$2"); shift 2 ;;
    -h|--help) awk 'NR > 1 && !/^#/ {exit} NR > 1 {sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
PREFIX="$(mkdir -p "$PREFIX" && cd "$PREFIX" && pwd)"
USER_HOME="$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)"
UV="${UV:-$(command -v uv || echo "$USER_HOME/.local/bin/uv")}"
[ -x "$UV" ] || { echo "uv not found (set UV=/path/to/uv)" >&2; exit 1; }

# never copy secrets (.env*), local development files, build output or local caches
EXCLUDES=(--exclude .venv --exclude .git --exclude '.env*' --exclude /internal --exclude /.taskmaster
  --exclude /PROGRESS.md --exclude /CLAUDE.local.md --exclude /.claude --exclude /.mcp.json --exclude /tests/artifacts
  --exclude /.debug --exclude /dist --exclude __pycache__
  --exclude .pytest_cache --exclude .mypy_cache --exclude .ruff_cache)
rsync -a --delete "${EXCLUDES[@]}" "$SRC/" "$PREFIX/app/"
(cd "$PREFIX/app" && UV_PROJECT_ENVIRONMENT="$PREFIX/.venv" "$UV" sync --no-dev --frozen --quiet)

# the root helper must not be changeable by the users it serves: the prefix, venv and app stay root-owned
for u in ${USERS[@]+"${USERS[@]}"}; do
  id "$u" >/dev/null 2>&1 || { echo "no such user: $u" >&2; exit 2; }
  [[ "$u" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "unusual user name: $u" >&2; exit 2; }
  if [ "$SUDOERS_DIR" = /etc/sudoers.d ] && [ "$(stat -c %u "$PREFIX/.venv/bin")" != 0 ]; then
    echo "$PREFIX/.venv is not owned by root: run the installer with sudo" >&2; exit 1
  fi
  mkdir -p "$SUDOERS_DIR"
  rule="$SUDOERS_DIR/syncmusic-$u"
  printf '# SyncMusic root helper for %s (packaging/install-systemd.sh --user)\n%s ALL=(root) NOPASSWD: %s\n' \
    "$u" "$u" "$PREFIX/.venv/bin/syncmusic-priv" > "$rule.tmp"
  chmod 0440 "$rule.tmp"
  if command -v visudo >/dev/null && ! visudo -cqf "$rule.tmp"; then
    rm -f "$rule.tmp"; echo "visudo rejected the rule for $u" >&2; exit 1
  fi
  mv "$rule.tmp" "$rule"
  echo "User $u may now use devices through $PREFIX/.venv/bin/syncmusic-priv ($rule)"
done

if [ -d "$PREFIX/pygpod" ]; then
  echo "note: $PREFIX/pygpod (pygpod copy of older installs) is no longer used; you may delete it"
fi

mkdir -p "$UNIT_DIR"
UNIT="$UNIT_DIR/$UNIT_NAME.service"
# the --env lines go right after the unit's first Environment= line
added=
while IFS= read -r line; do
  printf '%s\n' "${line//\/opt\/syncmusic/$PREFIX}"
  if [[ "$line" == Environment=* && -z "$added" ]]; then
    for e in ${ENVS[@]+"${ENVS[@]}"}; do printf 'Environment=%s\n' "$e"; done
    added=1
  fi
done < "$SRC/packaging/syncmusic.service" > "$UNIT.tmp"
chmod 0644 "$UNIT.tmp"
mv "$UNIT.tmp" "$UNIT"
if [ "$UNIT_DIR" = /etc/systemd/system ]; then
  systemctl daemon-reload
  echo "Installed in $PREFIX. Start with: systemctl enable --now $UNIT_NAME"
else
  echo "Installed in $PREFIX; unit written to $UNIT"
fi
