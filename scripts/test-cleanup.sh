#!/usr/bin/env bash
# Remove what an aborted test run of this checkout left behind: compose stack and volumes, emulated loop
# devices and their mounts, test daemons and browsers, temp dirs. Safe to run any time; idempotent.
#   scripts/test-cleanup.sh [--dry-run] [--all] [--force]
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --quiet python -m tests.cleanup "$@"
