#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Print one section of CHANGELOG.md without its heading and leading blank lines.
# Usage: scripts/changelog-section.sh NAME [FILE]
#   NAME   a version (0.2.0) or Unreleased; FILE defaults to CHANGELOG.md
# Exit status 1 when the section is missing or has no entries (only blank lines and ### group headings).
# Examples:
#   scripts/changelog-section.sh Unreleased
#   scripts/changelog-section.sh 0.2.0 > notes.md
set -euo pipefail
case "${1:-}" in -h|--help) awk 'NR > 2 && !/^#/ {exit} NR > 2 {sub(/^# ?/, ""); print}' "$0"; exit 0 ;; esac
[ $# -ge 1 ] || { echo "usage: $0 NAME [FILE]" >&2; exit 2; }
text="$(awk -v v="$1" '
  index($0, "## [" v "]") == 1 {on = 1; next}
  on && /^## \[/ {exit}
  on {print}' "${2:-CHANGELOG.md}" | sed -e '/./,$!d')"
printf '%s' "$text" | grep -qv -e '^[[:space:]]*$' -e '^###' || exit 1
printf '%s\n' "$text"
