# Contributing to SyncMusic

Thanks for helping! Bug reports, reports about a player model, docs fixes and code are all welcome.

## Set up
```sh
sudo apt install ffmpeg util-linux fdisk parted udev dosfstools exfatprogs e2fsprogs ntfs-3g sg3-utils eject rsync
git clone https://github.com/Bionded/SyncMusic.git SyncMusic && cd SyncMusic
uv sync                                   # Python ≥ 3.12, locked dependencies in .venv
uv run syncmusic run --mode web --port 8765   # daemon + web UI on http://localhost:8765
```
Mounting and formatting devices needs root, or passwordless `sudo` for the block-device tools.

## Tests
| Command | What runs |
|---|---|
| `uv run pytest -n auto -m "not hardware and not slow and not integration"` | the fast suite: unit, contract and fake-service tests (run it before a PR) |
| `uv run pytest -m integration` | Navidrome, Audiobookshelf, Samba, FTP in Docker compose (`tests/integration/compose.yaml`, needs Docker) |
| `uv run pytest -m emulated` | device scenarios on loop images that look like USB players (`tests/emulated.py`, needs `sudo -n`) |
| `SM_EMU_SPEED=real uv run pytest -m emulated` | the same at the speed of real players: every emulated disk gets its kind's bandwidth limit (`SPEED_PROFILES` in `tests/emulated.py`, cgroup v2 `io.max`); `SM_EMU_SPEED=fast` turns all limits off |
| `uv run pytest -m ui` | Playwright web UI tests (`uv run playwright install chromium` once) |
| `uv run pytest -m slow` | everything slow: UI, builds, soak tests |
| `uv run pytest -m hardware` | real USB devices - **only** the ones you list in `tests/hardware.toml` (git-ignored; copy `tests/hardware.example.toml` or generate it from the players your SyncMusic knows: `syncmusic devices allowlist --all -o tests/hardware.toml`; without it nothing is collected) |

`-n auto` runs all CPUs but one. Tests on the compose stack or on emulated devices are
always marked `integration` (also when they only use the stack through a fixture), so the fast suite never needs
Docker. Under `-n` the compose-stack and emulated tests run on every worker at once (each worker is a server user
of its own, each emulated disk has its worker's USB serial); only what is really shared runs in order. A test
looks at its own mounts, loop devices and playlists, never at all of them: other workers' are there too.

`scripts/test.py` runs the suites with a live display (`--help` lists them): `scripts/test.py nohw` is every test
except hardware, `--changed` only what the changes since the last commit can affect, `--failed` what failed last.

All media the servers and tests use is generated (`tests/integration/synthetic_media.py`: noise audio, real
Ukrainian artist, book and podcast names); no test reads real recordings.

What happens to the compose stack a run started, once its last test is done: `--compose-after MODE` or
`SD_COMPOSE_AFTER=MODE`:

| Mode | After the run | Next run |
|---|---|---|
| `clean` (default) | containers and volumes removed, the host folders of the stack deleted | starts from scratch (bootstrap and server scans, a few minutes) |
| `stop` | containers stopped, their state kept | starts the same containers again, no fresh scans |
| `keep` | stack left running | uses it at once |

For a quick edit-test loop: `export SD_COMPOSE_AFTER=keep`, then `SD_COMPOSE_AFTER=clean` (or
`uv run python -m tests.integration.services down`) when done. `stop` and `keep` reuse what earlier tests left in
the servers; use `clean` before a PR and in CI. A stack started by hand (`uv run python -m tests.integration.services
up`) is never stopped by a test run.

A test run cleans up after itself (compose stack and volumes, loop devices, mounts, temp daemons, browsers, the host folders of the stack), also when
tests fail. Only pytest's own temp root (`/tmp/pytest-of-$USER`) stays: it keeps the temp dirs of failed tests for
debugging (`tmp_path_retention_policy = "failed"`) and pytest prunes it itself. A run that was killed (SIGKILL, a crashed worker) cannot clean up; pytest then says so in its header. Run
`scripts/test-cleanup.sh` (`--dry-run` shows what it would remove, `--all` takes every `syncmusic-it-*` stack, not only
this checkout's) - it only touches loop devices on the emulated images and the tests' own temp dirs.

Every operation that mounts, writes or formats a disk goes through the block-device guard
(`syncmusic/infra/blockdev/guard.py`): in tests it refuses every disk that is not in the allowlist. Never weaken it,
and never point a test at your system disk.

Every implementation of a base class must pass the shared **contract suites** in `tests/contracts/` - register your
class in `tests/contracts/harness.py` and the suites run against it.

## Code style
- `uv run ruff check && uv run ruff format --check`, `uv run mypy syncmusic/core syncmusic/base` (strict).
- Type hints everywhere, Pydantic v2 models for settings and data, asyncio throughout; blocking work (file copies,
  mutagen, ffmpeg, pygpod) goes through `asyncio.to_thread` or the job runner.
- **Simplicity is a requirement.** A new device, library or processor is one class in one file (or one folder for
  a device with helpers) plus one line in `syncmusic/core/registry.py`. No plugin systems, no metaprogramming, at
  most base → implementation plus an optional shared mixin.
- English everywhere; UI strings go through the i18n table in `syncmusic/webui/static/js/i18n.js`.
- New files start with `# SPDX-License-Identifier: GPL-3.0-or-later`.
- **Database changes are migrations.** Add `syncmusic/infra/migrations/NNN_what.sql` (the next number; never edit
  `baseline.sql` or a released migration). The upgrade test in `tests/unit/test_db_schema.py` must still pass: it
  upgrades the databases of released versions in `tests/fixtures/upgrade/` and compares the schema with a fresh
  install; extend its checks when your migration moves data.

## Commits and pull requests
- Small commits with conventional messages: `feat(devices): …`, `fix(sync): …`, `test: …`, `docs: …`.
- A PR says what changed and how you tested it (which suites, which device or emulated image). UI changes come with a
  screenshot at desktop and phone width.
- By contributing you agree that your contribution is licensed under GPL-3.0-or-later.

## Releases
`scripts/release.sh X.Y.Z` tags a stable release and moves `main` to the next patch version; every push to `main` is
published as a beta pre-release `vX.Y.Z-N` by `.github/workflows/release.yml` (details: docs/02-installation.md,
*Building a release*). Never tag a version that `main` already moved past. Every user-visible change gets a line in
`CHANGELOG.md` under `## [Unreleased]`: the release turns that section into the version's notes, and the betas
show it too.

## Reporting a bug or a player
Attach the diagnostics bundle (*Settings → System → Collect diagnostics*, or `syncmusic diagnostics`): secrets are
masked. For a player that is not detected, also attach `http://<host>:8765/api/v1/devices/diagnostics`. Use the
issue templates; security problems go privately, see [SECURITY.md](SECURITY.md).

## Developer docs
- [Architecture](docs/dev/architecture.md)
- [Add a device](docs/dev/add-a-device.md) · [Add a library](docs/dev/add-a-library.md) ·
  [Add a processor](docs/dev/add-a-processor.md)
