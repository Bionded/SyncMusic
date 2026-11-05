import os
from pathlib import Path

import pytest

# plain help and error texts in CI: Typer forces a colour terminal when GITHUB_ACTIONS or FORCE_COLOR is set
# (read when typer is imported - before the imports below; `syncmusic` subprocesses inherit it)
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

from syncmusic.infra import config as _config
from syncmusic.infra import install  # noqa: F401 - imported first: keeps the real default path
from syncmusic.infra.config import load_config
from tests.integration import services as compose_services

HARDWARE_TOML = Path(__file__).parent / "hardware.toml"

# Every app a test starts - also `syncmusic run` subprocesses, which inherit the environment - follows no udev
# events and only accepts allowlisted disks (the safety rules in CONTRIBUTING.md; a daemon test with the defaults
# probe-mounted the real USB devices). Hardware tests opt in through their own config.
os.environ["SYNCMUSIC_DETECT_DEVICES"] = "0"
os.environ["SYNCMUSIC_DEVICE_ALLOWLIST"] = str(HARDWARE_TOML)
# no test asks GitHub for releases (the update tests give their own transport)
os.environ["SYNCMUSIC_UPDATE_REPO"] = "none"
# nor reads the config of a SyncMusic installed on this host (/etc/syncmusic/config.toml, root-only): an empty one
# for the daemons tests start, no file for load_config() here; the installer still writes to the real default path
os.environ["SYNCMUSIC_CONFIG"] = os.devnull
_config.DEFAULT_CONFIG_PATH = Path(os.devnull)


# Parallel runs: `pytest -n auto` with `--dist loadgroup` (pyproject addopts). The tests on the compose stack and
# on emulated devices are spread over the workers like the rest: every worker is a Navidrome and Audiobookshelf user
# of its own (services.worker_account), an emulated device is a loop image of its own test, and its application
# only learns the disks the test plugs in. Only what is really shared runs one test after another (an xdist group):
# - SMB_SHARE_GROUP: the tests that replace the files of the SMB server's one share (services.smb_files);
# - PLAYLISTS_GROUP: the tests that create Navidrome playlists or expect a certain set of them (the workers' users
#   are admins, and an admin sees every user's playlists);
# - JELLYFIN_GROUP: the tests that start and remove the one Jellyfin container;
# - HARDWARE_GROUP: real devices, one at a time.
SMB_SHARE_GROUP = "smb-share"
JELLYFIN_GROUP = "jellyfin"
HARDWARE_GROUP = "hardware"
PLAYLISTS_GROUP = "navidrome-playlists"
SMB_SHARE_MODULES = {"test_browse.py", "test_smb_folder.py"}
PLAYLISTS_MODULES = {"test_navidrome_local.py", "test_emulated_playlist_reorder.py"}
MIN_AVAILABLE_MB = 1536  # below this `-n auto` runs serially
COMPOSE_DEVICES = {"smb"}  # contract cases whose device lives on the compose stack


def _available_mb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def pytest_addoption(parser):
    parser.addoption(
        "--compose-after",
        choices=compose_services.AFTER_MODES,
        default=None,
        help="what the run does with the compose stack it started: clean (default: remove it and its volumes), "
        "stop (stop it, keep its state for the next run) or keep (leave it running); also SD_COMPOSE_AFTER",
    )


def pytest_configure(config):
    from tests.cleanup import leftovers

    if config.getoption("compose_after"):
        # the xdist workers inherit the environment and parse the same option
        os.environ["SD_COMPOSE_AFTER"] = config.getoption("compose_after")
    compose_services.after_mode()  # a wrong SD_COMPOSE_AFTER fails before any test

    if not hasattr(config, "workerinput") and (found := leftovers()):
        config.issue_config_time_warning(
            pytest.PytestWarning(
                f"leftovers of an aborted test run: {', '.join(found)} - run scripts/test-cleanup.sh"
            ),
            stacklevel=2,
        )


def pytest_sessionfinish(session, exitstatus):
    """The compose stack is released by every process that still holds it (also an xdist worker, which runs
    no atexit handlers): the last one stops it if this run started it."""
    compose_services.release_mine()
    from tests.contracts.harness import stop_ftp_server

    stop_ftp_server()


def auto_workers(cpus: int) -> int:
    """All CPUs but one: 3 on a 4-core Pi 5, 11 on 12 cores."""
    return max(cpus - 1, 1)


def pytest_xdist_auto_num_workers(config):
    """`-n auto`: `auto_workers()`, or 0 (serial) while less than MIN_AVAILABLE_MB of memory is available."""
    available = _available_mb()
    workers = auto_workers(os.cpu_count() or 1) if available >= MIN_AVAILABLE_MB else 0
    if workers == 0:
        print(f"pytest -n auto: only {available} MB available, running serially")
    return workers


# per-test time budget: a stuck test fails fast instead of eating the session. Includes fixture
# setup (the compose bootstrap, an emulated image); `@pytest.mark.timeout(n)` on a test overrides it.
TIMEOUT_FAST_S = 60
TIMEOUT_SLOW_S = 300  # slow / Playwright tests
TIMEOUT_SERVICES_S = 600  # compose stack or emulated devices
TIMEOUT_HARDWARE_S = 1800  # real syncs on real devices
TIMEOUT_HEAVY_S = 900  # live-scale soaks (`heavy`): 5-7 min alone on a Pi 5, more under -n 3


def _timeout(item) -> int:
    if item.get_closest_marker("hardware"):
        return TIMEOUT_HARDWARE_S
    if item.get_closest_marker("heavy"):
        return TIMEOUT_HEAVY_S
    if (
        "services" in item.fixturenames
        or item.get_closest_marker("integration")
        or item.get_closest_marker("emulated")
        or "contracts" in item.path.parts  # network devices start the compose stack lazily
    ):
        return TIMEOUT_SERVICES_S
    if item.get_closest_marker("slow") or item.get_closest_marker("ui"):
        return TIMEOUT_SLOW_S
    return TIMEOUT_FAST_S


def _uses_compose(item) -> bool:
    """The test needs the compose stack: the `services` fixture, or a contract case on a compose device."""
    if {"services", "compose_stack"} & set(item.fixturenames):
        return True
    params = getattr(getattr(item, "callspec", None), "params", {})
    return "contracts" in item.path.parts and bool(COMPOSE_DEVICES & set(map(str, params.values())))


def group_of(item) -> str | None:
    if item.get_closest_marker("hardware"):
        return HARDWARE_GROUP
    if "jellyfin" in item.fixturenames:
        return JELLYFIN_GROUP
    if "integration" in item.path.parts and item.path.name in SMB_SHARE_MODULES:
        return SMB_SHARE_GROUP
    if "integration" in item.path.parts and item.path.name in PLAYLISTS_MODULES:
        return PLAYLISTS_GROUP
    return None


# tryfirst: the markers added here count for `-m` (its deselection runs after this hook)
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        if (
            _uses_compose(item)
            and not item.get_closest_marker("integration")
            and not item.get_closest_marker("hardware")
        ):
            # the fast suite (`-m "... and not integration"`) never starts Docker
            item.add_marker(pytest.mark.integration)
        if item.get_closest_marker("timeout") is None and not config.getoption("timeout", None):
            item.add_marker(pytest.mark.timeout(_timeout(item)))
        if group := group_of(item):
            item.add_marker(pytest.mark.xdist_group(group))


@pytest.fixture
def config(tmp_path):
    return load_config(
        env={},
        overrides={
            "data_dir": tmp_path / "data",
            "cache_dir": tmp_path / "cache",
            "mount_root": tmp_path / "mnt",
            "device_allowlist": HARDWARE_TOML,
            "mode": "daemon",
            "allow_directory_devices": True,
            "detect_devices": False,
            "update_repo": "none",
        },
    )


@pytest.fixture(scope="session")
def compose_stack():
    """The compose stack (tests/integration/services.py), bootstrapped. Started on first use; what happens to
    it at the end is `--compose-after` / SD_COMPOSE_AFTER (clean, stop or keep)."""
    if not compose_services.docker():
        pytest.skip("Docker not available for the integration services ")
    compose_services.acquire()  # shared by the xdist workers: the last one stops it
    compose_services.worker_account()  # this worker's own Navidrome and Audiobookshelf user
    yield compose_services.ENV
    compose_services.release()


@pytest.fixture
def services(compose_stack):
    """The stack's addresses and this worker's credentials. The worker's plays, ratings and stars in Navidrome
    are put back after the test: Subsonic can add plays but never remove them, and the worker's next test would
    sync them onto its device."""
    from tests.integration import navidrome_db

    user = compose_stack["NAVIDROME_USER"]
    saved = navidrome_db.save(user)
    yield compose_stack
    navidrome_db.restore(saved, user)
