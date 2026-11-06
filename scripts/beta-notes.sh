#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Release notes of a beta build of main: the "## [Unreleased]" section of CHANGELOG.md (when it has entries), then
# the commit subjects since the last stable release. Used by .github/workflows/release.yml.
# Usage: scripts/beta-notes.sh BUILD [LAST_TAG]
# Examples:
#   scripts/beta-notes.sh 5 v0.2.0 > notes.md
set -euo pipefail
case "${1:-}" in -h|--help) awk 'NR > 2 && !/^#/ {exit} NR > 2 {sub(/^# ?/, ""); print}' "$0"; exit 0 ;; esac
[ $# -ge 1 ] || { echo "usage: $0 BUILD [LAST_TAG]" >&2; exit 2; }
build="$1" last="${2:-}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
echo "Test build $build of main - may have bugs."
if unreleased="$(scripts/changelog-section.sh Unreleased)"; then
  printf '\n## Changes since %s\n\n%s\n' "${last:-the start}" "$unreleased"
  printf '\n## Commits\n\n'
else
  printf '\nCommits since %s:\n\n' "${last:-the start}"
fi
git log --no-merges --max-count=50 --format='- %s' ${last:+"$last..HEAD"}
