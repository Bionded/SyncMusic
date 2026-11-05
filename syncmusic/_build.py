# SPDX-License-Identifier: GPL-3.0-or-later
"""Build-time constants, rewritten by packaging/build.sh: the mode a binary starts in, its build number (a build
of main, None for a release) and the kind of install (None: found out at run time, infra/updates.py)."""

DEFAULT_MODE = "web"
BUILD_NUMBER: int | None = None
INSTALL_TYPE: str | None = None
