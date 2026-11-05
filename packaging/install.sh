#!/usr/bin/env bash
# Old name of packaging/install-systemd.sh, kept because older installed copies run
# packaging/install.sh of the commit they update to (self-update). Same options; see install-systemd.sh --help.
# Examples:
#   sudo packaging/install.sh
#   sudo packaging/install.sh --env SYNCMUSIC_PORT=8080 --user alice
exec "$BASH" "$(dirname "${BASH_SOURCE[0]}")/install-systemd.sh" "$@"
