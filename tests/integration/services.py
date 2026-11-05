"""The integration test services (Navidrome, Audiobookshelf) as a docker compose stack.

`tests/integration/compose.yaml` runs both servers on 127.0.0.1 with the test media mounted read-only. `up()`
starts the stack if it is not running and bootstraps it like the old host services: the admin users from
`.env.test`, Audiobookshelf's settings (nothing is written next to the media), its "Audiobooks" and "Podcasts"
libraries and an API key, then waits until both servers have scanned the media. The values the servers generate
(ABS API key, library ids) go into `.compose.env` next to this file (not tracked) and into `ENV`.

pytest brings the stack up on first use (`services` fixture, tests/conftest.py). What happens to it when the
last test using it ends is `SD_COMPOSE_AFTER` (or `pytest --compose-after`): `clean` (default) removes the
containers and their volumes, `stop` stops the containers and keeps their state (the next run starts them again
and skips the bootstrap scans), `keep` leaves the stack running. A stack started by hand is never touched. By hand:

    uv run python -m tests.integration.services up | down | status
"""

from __future__ import annotations

import contextlib
import fcntl
import ftplib
import functools
import hashlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx

HERE = Path(__file__).parent
COMPOSE = HERE / "compose.yaml"
ENV_FILE = HERE.parents[1] / ".env.test"
GENERATED = HERE / ".compose.env"
SERVICES = ("navidrome", "audiobookshelf", "ftp", "samba")
ABS_LIBRARIES = (
    ("ABS_LIBRARY_BOOKS", "Audiobooks", "book", "/srv/test-media/Audiobooks"),
    ("ABS_LIBRARY_PODCASTS", "Podcasts", "podcast", "/srv/test-media/Podcasts"),
    # tests/integration/abs-samples: a generated 3-part book
    ("ABS_LIBRARY_SAMPLES", "Samples", "book", "/srv/samples"),
)
TIMEOUT = 300.0


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in pairs if not k.strip().startswith("#")}


# Static credentials (.env.test) plus the generated values of the running stack; updated in place by `up()`.
ENV: dict[str, str] = {**_read_env(ENV_FILE), **_read_env(GENERATED)}


@functools.cache
def docker() -> tuple[str, ...]:
    """The docker command this user may run (`docker`, else `sudo -n docker`), or () without Docker."""
    if shutil.which("docker") is None:
        return ()
    for cmd in (("docker",), ("sudo", "-n", "docker")):
        if subprocess.run([*cmd, "info"], capture_output=True).returncode == 0:
            return cmd
    return ()


# the test media: always the generated set (synthetic_media.py), never real recordings
SYNTHETIC = Path(os.environ.get("SD_SYNTHETIC_MEDIA", "/var/tmp/syncmusic-it-media"))
MEDIA_LOCK = SYNTHETIC.parent / f"{SYNTHETIC.name}.lock"


def media_root() -> Path:
    """The media the servers scan (read-only): the generated set, written on first use. One folder per version
    of the set, so a stack still scanning an older one no longer matches (`media_matches`) and is restarted."""
    from tests.integration.synthetic_media import MARKER, VERSION, generate

    root = SYNTHETIC / f"v{VERSION}"
    if (root / MARKER).is_file():
        return root
    # the xdist workers (two queues start at once) all get here: one writes the set, the others wait for it
    SYNTHETIC.mkdir(parents=True, exist_ok=True)
    with MEDIA_LOCK.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        if not (root / MARKER).is_file():
            # an older version or a half-written set of a killed run - not while a running test session
            # holds the stack: its servers still scan the older version
            in_use = USERS.exists() and bool(_users()[0])
            for old in [] if in_use else SYNTHETIC.glob("*"):
                if old == root:
                    continue
                if old.is_dir():
                    shutil.rmtree(old, ignore_errors=True)
                else:
                    old.unlink(missing_ok=True)
            generate(root)
    return root


def media_listing() -> list[str]:
    """Every path of the test media with its size and mtime: equal before and after a test = nothing wrote into it."""
    return sorted(f"{p} {p.stat().st_size} {p.stat().st_mtime_ns}" for p in media_root().rglob("*"))


def project() -> str:
    """The compose project: one per checkout, so two checkouts never share server users or volumes."""
    if os.environ.get("SD_COMPOSE_PROJECT"):
        return os.environ["SD_COMPOSE_PROJECT"]
    root = HERE.parents[1].resolve()
    return "syncmusic-it-" + hashlib.sha1(str(root).encode()).hexdigest()[:8]


# the SMB device's share [devices]: a host folder, so tests see what arrived
SMB_DEVICES = Path(os.environ.get("SD_SMB_DEVICES", "/var/tmp/syncmusic-it-smbdev"))


class ComposeError(subprocess.CalledProcessError):
    def __str__(self) -> str:
        return f"{super().__str__()}\n{(self.stderr or '').strip()[-2000:]}"


def compose(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    cmd = [*docker(), "compose", "-f", str(COMPOSE), "-p", project(), *args]
    env = {
        **os.environ,
        "SD_UID": str(os.getuid()),
        "SD_GID": str(os.getgid()),
        "SM_TEST_MEDIA": str(media_root()),
        # the Samba user is created from .env.test, like the other servers' users
        "SMB_USER": ENV.get("SMB_USER", "samba"),
        "SMB_PASSWORD": ENV.get("SMB_PASSWORD", "secret"),
    }
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, env=env, **kwargs)  # type: ignore[call-overload,no-any-return]
    except subprocess.CalledProcessError as e:
        # surface docker's own message: a bare exit status 1 does not say why `up --wait` failed
        raise ComposeError(e.returncode, e.cmd, e.output, e.stderr) from e


def running() -> bool:
    if not docker():
        return False
    out = compose("ps", "--status", "running", "--services").stdout.split()
    return all(s in out for s in SERVICES)


def _media_mounts() -> set[str]:
    """The host folders the stack's containers (also stopped ones) mount as /srv/test-media."""
    ids = compose("ps", "-a", "-q").stdout.split()
    if not ids:
        return set()
    fmt = '{{range .Mounts}}{{if eq .Destination "/srv/test-media"}}{{.Source}}\n{{end}}{{end}}'
    out = subprocess.run(
        [*docker(), "inspect", "-f", fmt, *ids], capture_output=True, text=True
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def media_matches() -> bool:
    """False when the running stack scans another media root than this run wants (a stack left by a run with an older
    version of the generated set would serve its books)."""
    want = str(media_root().resolve())
    return all(str(Path(m).resolve()) == want for m in _media_mounts())


def up(reuse: bool = False) -> bool:
    """Start (if needed) and bootstrap the stack; True if this call started it. `reuse`: a stack stopped on
    purpose (SD_COMPOSE_AFTER=stop) is started again with its volumes, if it scans the same media."""
    phase("checking the stack")
    if not media_matches():
        down()
    started = not running()
    if started:
        if not reuse:
            # a stopped stack (killed run, reboot) keeps its named volumes with the state of its last tests -
            # fresh volumes unless it was stopped on purpose
            down()
        phase("starting the containers")
        SMB_DEVICES.mkdir(parents=True, exist_ok=True)
        compose("up", "-d", "--wait")
    try:
        _bootstrap()
    except (TimeoutError, httpx.HTTPError, subprocess.CalledProcessError):
        # a container killed mid-bootstrap (ABS OOM during its scan) keeps a half-initialised state
        # that never becomes ready: start once more from scratch instead of failing every test
        down()
        phase("starting the containers again (the bootstrap failed)")
        SMB_DEVICES.mkdir(parents=True, exist_ok=True)
        compose("up", "-d", "--wait")
        _bootstrap()
        started = True
    finally:
        phase(None)
    return started


def _bootstrap() -> None:
    _navidrome_bootstrap()
    _abs_bootstrap()
    _ftp_ready()
    _samba_ready()


# pytest-xdist workers share the stack: `up` is serialised and counted, the last user stops it (
# three workers racing `up -d --wait` collided on the container names)
LOCK = Path(os.environ.get("SD_COMPOSE_LOCK", "/var/tmp/syncmusic-it-compose.lock"))
USERS = LOCK.with_suffix(".users")
# the stack was left on purpose: "test" by a run with SD_COMPOSE_AFTER=stop|keep, "hand" by `services up`
KEPT = LOCK.with_suffix(".kept")
AFTER_MODES = ("clean", "stop", "keep")
# what the stack is doing right now ("<epoch> <text>"), for the progress display of scripts/test.py
STATUS = LOCK.with_suffix(".status")


def phase(text: str | None) -> None:
    """Note what the stack is busy with (None: ready / idle)."""
    try:
        if text is None:
            STATUS.unlink(missing_ok=True)
        else:
            STATUS.write_text(f"{time.time():.0f} {text}\n")
    except OSError:
        pass


def after_mode() -> str:
    """What the last user does with the stack this run started (SD_COMPOSE_AFTER, see the module docstring)."""
    mode = os.environ.get("SD_COMPOSE_AFTER", "clean").strip().lower() or "clean"
    if mode not in AFTER_MODES:
        raise ValueError(f"SD_COMPOSE_AFTER={mode!r}: one of {', '.join(AFTER_MODES)}")
    return mode


def _kept_by() -> str:
    """ "test", "hand" or "" (nothing left on purpose)."""
    if not KEPT.exists():
        return ""
    return KEPT.read_text().strip() or "hand"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    while True:
        with LOCK.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                # the last user deletes the lock file: a lock taken on the deleted file guards nothing
                if LOCK.exists() and os.stat(LOCK).st_ino == os.fstat(fh.fileno()).st_ino:
                    yield
                    return
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _users() -> tuple[set[int], bool]:
    """The live user pids and the started-by-us flag - the file holds 'started pid pid …'. Pids of killed runs
    are dropped, so a stale entry never keeps the stack (and its state) alive."""
    parts = USERS.read_text().split() if USERS.exists() else []
    started = bool(parts) and parts[0] == "1"
    return {int(p) for p in parts[1:] if _alive(int(p))}, started


def _write_users(pids: set[int], started: bool) -> None:
    USERS.write_text(" ".join([str(int(started)), *map(str, sorted(pids))]) + "\n")


def acquire() -> None:
    """`up()` for one of several concurrent users (xdist workers)."""
    with _locked():
        pids, started = _users()
        kept = _kept_by()
        if not pids:  # first user
            # a stack a test run left on purpose is this run's to stop; one started by hand is not
            started = kept == "test"
            if running() and not kept:
                # left over by a killed run: its tests' state leaks into ours - start from scratch
                down()
        if pids:
            # a later user: the first one started and bootstrapped the stack (it wrote the users file only after
            # that, under this lock) - take the values the servers generated (tokens, ids), no second bootstrap
            ENV.update(_read_env(GENERATED))
            ENV.update(_WORKER)
        else:
            started = up(reuse=bool(kept)) or started
        if kept == "test":
            KEPT.unlink()
        _write_users(pids | {os.getpid()}, started)


def release() -> None:
    """Drop one user; the last one handles the stack after `after_mode()` if this run started it."""
    with _locked():
        pids, started = _users()
        pids.discard(os.getpid())
        if pids:
            _write_users(pids, started)
            return
        USERS.unlink(missing_ok=True)
        if not started:
            return
        mode = after_mode()
        if mode == "clean":
            down()
            _remove_temp()
            return
        if mode == "stop":
            phase("stopping the stack")
            compose("stop")
            phase(None)
        KEPT.write_text("test\n")


def _remove_temp() -> None:
    """The host folders and files this run made for the stack (a green run leaves nothing behind)."""
    for path in (SMB_DEVICES, JELLYFIN_MEDIA, SYNTHETIC):
        if path.is_dir() and not path.is_mount():
            shutil.rmtree(path, ignore_errors=True)
    KEPT.unlink(missing_ok=True)
    LOCK.unlink(missing_ok=True)
    MEDIA_LOCK.unlink(missing_ok=True)
    STATUS.unlink(missing_ok=True)


def release_mine() -> None:
    """`release()` if this process still holds the stack (at the end of a test session: users that are no
    fixture - the contract harness - never get a teardown, and xdist workers run no atexit handlers)."""
    # nobody holds the stack: don't recreate the lock file the last user deleted
    if not USERS.exists():
        return
    with _locked():
        mine = os.getpid() in _users()[0]
    if mine:
        release()


def down() -> None:
    """Stop the stack and delete its volumes (the next `up()` starts from scratch)."""
    phase("removing the stack")
    compose("down", "--volumes", "--remove-orphans")
    phase(None)
    GENERATED.unlink(missing_ok=True)
    for key, *_ in ABS_LIBRARIES:
        ENV.pop(key, None)
    ENV.pop("ABS_TOKEN", None)
    _WORKER.clear()


# Under xdist the tests on the stack run on every worker at once. Each worker process is its own Navidrome and
# Audiobookshelf user ("<user>-gw3"): plays, ratings, stars, playlists and book progress are per user, so no test
# sees what another worker's test writes. The bootstrap stays the admin's of .env.test (ADMIN).
ADMIN: dict[str, str] = _read_env(ENV_FILE)
_WORKER: dict[
    str, str
] = {}  # this process's account, applied over ENV (also after a later bootstrap)


def worker_name() -> str:
    """The account of this process: <admin>-gwN under xdist, the admin itself in a single process."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    return f"{ADMIN['NAVIDROME_USER']}-{worker}" if worker else ADMIN["NAVIDROME_USER"]


def worker_account() -> dict[str, str]:
    """Create (once per stack) and use this process's Navidrome and Audiobookshelf user; returns the ENV values."""
    name = worker_name()
    if name == ADMIN["NAVIDROME_USER"]:
        return {}
    password = ADMIN["NAVIDROME_PASSWORD"]
    key = f"ABS_TOKEN_{name.replace('-', '_').upper()}"
    with _locked():
        generated = _read_env(GENERATED)
        with httpx.Client(base_url=ENV["NAVIDROME_URL"], timeout=30) as c:
            ping = {"u": name, "p": password, "v": "1.16.1", "c": "sd-test", "f": "json"}
            # a Subsonic ping first: Navidrome rate-limits /auth/login, so the first worker that needs it logs in
            # once and creates the users of all workers
            if c.get("/rest/ping", params=ping).json()["subsonic-response"]["status"] != "ok":
                login = {
                    "username": ADMIN["NAVIDROME_USER"],
                    "password": ADMIN["NAVIDROME_PASSWORD"],
                }
                token = c.post("/auth/login", json=login).raise_for_status().json()["token"]
                auth = {"x-nd-authorization": f"Bearer {token}"}
                have = {u["userName"] for u in c.get("/api/user", headers=auth).json()}
                count = int(os.environ.get("PYTEST_XDIST_WORKER_COUNT") or 1)
                admin = ADMIN["NAVIDROME_USER"]
                for user in [name, *(f"{admin}-gw{i}" for i in range(count))]:
                    if user in have:
                        continue
                    c.post(
                        "/api/user",
                        headers=auth,
                        # admins see every library (a plain user needs its libraries assigned)
                        json={
                            "userName": user,
                            "name": user,
                            "password": password,
                            "isAdmin": True,
                        },
                    ).raise_for_status()
                    have.add(user)
        with httpx.Client(base_url=ENV["ABS_URL"], timeout=30) as c:
            token = generated.get(key)
            if (
                token is None
                or c.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code != 200
            ):
                c.headers["Authorization"] = f"Bearer {ENV['ABS_TOKEN']}"
                users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
                if name not in users:
                    r = c.post(
                        "/api/users",
                        json={
                            "username": name,
                            "password": password,
                            "type": "admin",
                            "isActive": True,
                        },
                    )
                    r.raise_for_status()
                    users[name] = r.json()["user"]
                r = c.post(
                    "/api/api-keys",
                    json={"name": name, "userId": users[name]["id"], "isActive": True},
                )
                r.raise_for_status()
                token = generated[key] = r.json()["apiKey"]["apiKey"]
                GENERATED.write_text("".join(f"{k}={v}\n" for k, v in generated.items()))
    _WORKER.update({"NAVIDROME_USER": name, "ABS_USER": name, "ABS_TOKEN": token})
    ENV.update(_WORKER)
    return dict(_WORKER)


def _wait(what: str, ready: object) -> None:
    deadline = time.monotonic() + TIMEOUT
    phase(f"waiting for {what}")
    while True:
        try:
            if ready():  # type: ignore[operator]
                return
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            OSError,
            ftplib.Error,
            subprocess.CalledProcessError,
        ):
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} not ready after {TIMEOUT:.0f} s")
        time.sleep(2)


def _navidrome_bootstrap() -> None:
    url, user, password = ENV["NAVIDROME_URL"], ADMIN["NAVIDROME_USER"], ADMIN["NAVIDROME_PASSWORD"]
    with httpx.Client(base_url=url, timeout=30) as c:
        _wait("Navidrome", lambda: c.get("/ping").status_code < 500)
        # Creates the first (admin) user; refused once a user exists.
        c.post("/auth/createAdmin", json={"username": user, "password": password})
        params = {"u": user, "p": password, "v": "1.16.1", "c": "sd-test", "f": "json"}
        stable: list[int] = []

        def scanned() -> bool:
            body = c.get("/rest/getScanStatus", params=params).json()["subsonic-response"]
            if body["status"] != "ok":
                raise RuntimeError(f"Navidrome login failed: {body.get('error')}")
            status = body["scanStatus"]
            stable.append(status["count"] if not status["scanning"] else -1)
            return len(stable) >= 2 and stable[-1] == stable[-2] > 0

        _wait("Navidrome scan", scanned)


FTP_HOME = "/home/sdtest"


def _ftp_ready() -> None:
    """The FTP user of .env.test (a pure-ftpd virtual user, uid 1000), then a login."""
    user, password = ENV["FTP_USER"], ENV["FTP_PASSWORD"]
    _wait(
        "FTP server",
        lambda: (
            compose("exec", "-T", "ftp", "pure-pw", "list", "-f", "/data/pureftpd.passwd")
            is not None
        ),
    )
    listed = compose(
        "exec", "-T", "ftp", "sh", "-c", "pure-pw list -f /data/pureftpd.passwd || true"
    ).stdout
    if not any(line.split()[:1] == [user] for line in listed.splitlines()):
        script = (
            f"mkdir -p {FTP_HOME} && chown 1000:1000 {FTP_HOME} && "
            f"pure-pw useradd {user} -u 1000 -g 1000 -d {FTP_HOME} -f /data/pureftpd.passwd -m"
        )
        compose("exec", "-T", "ftp", "sh", "-c", script, input=f"{password}\n{password}\n")

    def login() -> bool:
        with ftplib.FTP() as ftp:
            # pure-ftpd checks the argon2 hash pure-pw made for a 128 MB container: a login takes 4-7 s
            ftp.connect(ENV["FTP_HOST"], int(ENV["FTP_PORT"]), timeout=30)
            ftp.login(ENV["FTP_USER"], ENV["FTP_PASSWORD"])
        return True

    _wait("FTP server", login)


def _samba_ready() -> None:
    """A login on the [music] share (user of .env.test) - the folder library over SMB."""
    from syncmusic.libraries.remote import SmbSource

    def login() -> bool:
        src = SmbSource(
            host=ENV["SMB_HOST"],
            port=int(ENV["SMB_PORT"]),
            username=ENV["SMB_USER"],
            password=ENV["SMB_PASSWORD"],
            path="/music",
            mirror=Path("/nonexistent"),
        )
        src.check()
        return True

    _wait("SMB server", login)


def smb_files(src: Path) -> None:
    """Put the files of `src` into the SMB shares' folder (replacing what was there), readable by guests too."""
    compose("exec", "-T", "samba", "sh", "-c", "rm -rf /shared/* && mkdir -p /shared")
    compose("cp", f"{src}/.", "samba:/shared/")
    compose("exec", "-T", "samba", "sh", "-c", "chmod -R a+rX /shared")


def _abs_bootstrap() -> None:
    url, user, password = ENV["ABS_URL"], ADMIN["ABS_USER"], ADMIN["ABS_PASSWORD"]
    with httpx.Client(base_url=url, timeout=30) as c:
        _wait("Audiobookshelf", lambda: c.get("/status").status_code == 200)
        if not c.get("/status").json().get("isInit"):
            r = c.post("/init", json={"newRoot": {"username": user, "password": password}})
            r.raise_for_status()
        r = c.post("/login", json={"username": user, "password": password})
        r.raise_for_status()
        me = r.json()["user"]
        c.headers["Authorization"] = f"Bearer {me.get('accessToken') or me['token']}"
        c.patch(
            "/api/settings",
            json={
                "storeCoverWithItem": False,
                "storeMetadataWithItem": False,
                "scannerFindCovers": False,
            },
        ).raise_for_status()
        libraries = {lib["name"]: lib for lib in c.get("/api/libraries").json()["libraries"]}
        generated = _read_env(GENERATED)
        for key, name, media_type, folder in ABS_LIBRARIES:
            lib = libraries.get(name)
            if lib is None:
                body = {"name": name, "mediaType": media_type, "folders": [{"fullPath": folder}]}
                r = c.post("/api/libraries", json=body)
                r.raise_for_status()
                lib = r.json()
                c.post(f"/api/libraries/{lib['id']}/scan")
            elif not c.get(f"/api/libraries/{lib['id']}/items", params={"limit": 1}).json()[
                "total"
            ]:
                # restarted mid-scan: the scan does not resume by itself
                c.post(f"/api/libraries/{lib['id']}/scan")
            generated[key] = lib["id"]
            _wait(
                f"Audiobookshelf scan of {name}",
                lambda lib_id=lib["id"]: (
                    c.get(f"/api/libraries/{lib_id}/items", params={"limit": 1}).json()["total"] > 0
                ),
            )
        token = generated.get("ABS_TOKEN")
        if (
            token is None
            or c.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code != 200
        ):
            r = c.post(
                "/api/api-keys", json={"name": "sdtest", "userId": me["id"], "isActive": True}
            )
            r.raise_for_status()
            generated["ABS_TOKEN"] = r.json()["apiKey"]["apiKey"]
    GENERATED.write_text("".join(f"{k}={v}\n" for k, v in generated.items()))
    ENV.update(generated)
    ENV.update(_WORKER)  # this process's account stays its own (`worker_account`)


# --- Jellyfin: a compose profile of its own, started by its tests only ----------------------
JELLYFIN_MEDIA = Path(os.environ.get("SD_JELLYFIN_MEDIA", "/var/tmp/syncmusic-it-jellyfin"))
_JF_AUTH = 'MediaBrowser Client="sd-test", Device="sd-test", DeviceId="sd-test", Version="1"'


# made-up titles: no metadata provider may match them (internet fetchers are off anyway)
JELLYFIN_VIDEOS = (
    ("movies/Тестовий фільм (2001)/Тестовий фільм (2001).mp4", "Тестовий фільм"),
    ("shows/Тестовий серіал/Season 01/Тестовий серіал S01E01.mp4", "Пілот"),
    ("shows/Тестовий серіал/Season 01/Тестовий серіал S01E02.mp4", "Друга серія"),
)
_JF_FOLDERS = (("Movies", "movies", "/srv/video/movies"), ("Shows", "tvshows", "/srv/video/shows"))
_JF_OFFLINE = {
    "LibraryOptions": {
        "EnableRealtimeMonitor": False,
        "MetadataSavers": [],
        "TypeOptions": [
            {"Type": kind, "MetadataFetchers": [], "ImageFetchers": []}
            for kind in ("Movie", "Series", "Season", "Episode")
        ],
    }
}


def jellyfin_up() -> None:
    """Generate the short videos, start Jellyfin, run its startup wizard (first admin of .env.test), add a movie
    and a series folder and wait until its scan has found every file."""
    from tests.media import make_video

    for rel, title in JELLYFIN_VIDEOS:
        path = JELLYFIN_MEDIA / rel
        if not path.exists():
            make_video(path, size="320x240", seconds=2.0, faststart=True, title=title)
    for p in [JELLYFIN_MEDIA, *JELLYFIN_MEDIA.rglob("*")]:
        p.chmod(p.stat().st_mode | 0o055)  # the container's user reads it
    compose("--profile", "jellyfin", "up", "-d", "jellyfin")
    url, user, password = ENV["JELLYFIN_URL"], ENV["JELLYFIN_USER"], ENV["JELLYFIN_PASSWORD"]
    with httpx.Client(base_url=url, timeout=30) as c:
        _wait("Jellyfin", lambda: c.get("/System/Info/Public").status_code == 200)
        if not c.get("/System/Info/Public").json().get("StartupWizardCompleted"):
            _wait("Jellyfin wizard", lambda: c.get("/Startup/User").status_code == 200)
            c.post(
                "/Startup/Configuration", json={"UICulture": "en-US", "MetadataCountryCode": "US"}
            )
            c.post("/Startup/User", json={"Name": user, "Password": password})
            c.post("/Startup/Complete").raise_for_status()
        login = c.post(
            "/Users/AuthenticateByName",
            json={"Username": user, "Pw": password},
            headers={"Authorization": _JF_AUTH},
        ).json()
        auth = {"Authorization": f'{_JF_AUTH}, Token="{login["AccessToken"]}"'}
        if not c.get("/Library/VirtualFolders", headers=auth).json():
            for name, kind, path in _JF_FOLDERS:
                params = {
                    "name": name,
                    "collectionType": kind,
                    "paths": path,
                    "refreshLibrary": "true",
                }
                c.post(
                    "/Library/VirtualFolders", params=params, json=_JF_OFFLINE, headers=auth
                ).raise_for_status()
        query = {
            "userId": login["User"]["Id"],
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Limit": 0,
        }

        def scanned() -> bool:
            # items appear named after their files first; the metadata is read before the scan task goes idle
            tasks = c.get("/ScheduledTasks", headers=auth).json()
            scan = next(t for t in tasks if t.get("Key") == "RefreshLibrary")
            done = scan.get("State") == "Idle" and scan.get("LastExecutionResult") is not None
            count = c.get("/Items", params=query, headers=auth).json()["TotalRecordCount"]
            return done and count >= len(JELLYFIN_VIDEOS)

        _wait("Jellyfin scan", scanned)


def jellyfin_api_key() -> str:
    """An API key of the test server (made once)."""
    url, user, password = ENV["JELLYFIN_URL"], ENV["JELLYFIN_USER"], ENV["JELLYFIN_PASSWORD"]
    with httpx.Client(base_url=url, timeout=30) as c:
        login = c.post(
            "/Users/AuthenticateByName",
            json={"Username": user, "Pw": password},
            headers={"Authorization": _JF_AUTH},
        ).json()
        auth = {"Authorization": f'{_JF_AUTH}, Token="{login["AccessToken"]}"'}
        keys = c.get("/Auth/Keys", headers=auth).json()["Items"]
        if not keys:
            c.post("/Auth/Keys", params={"app": "sd-test"}, headers=auth).raise_for_status()
            keys = c.get("/Auth/Keys", headers=auth).json()["Items"]
        return str(keys[0]["AccessToken"])


def jellyfin_down() -> None:
    """Stop Jellyfin and remove its state (the volumes)."""
    compose("--profile", "jellyfin", "rm", "-sfv", "jellyfin")
    for volume in (
        "jellyfin-config",
        "jellyfin-cache",
    ):  # only its own: the rest of the stack may be in use
        subprocess.run(
            [*docker(), "volume", "rm", "-f", f"{project()}_{volume}"], capture_output=True
        )
    shutil.rmtree(JELLYFIN_MEDIA, ignore_errors=True)
    if not USERS.exists():  # compose() generated the synthetic set for its env: nobody else uses it
        shutil.rmtree(SYNTHETIC, ignore_errors=True)


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if not docker():
        print("docker is not available (install it) or this user may not run it")
        return 1
    if cmd == "up":
        print("started" if up() else "already running", "and bootstrapped")
        KEPT.write_text("hand\n")  # started by hand: pytest runs reuse it and never stop it
    elif cmd == "down":
        down()
        KEPT.unlink(missing_ok=True)
        print("down")
    elif cmd == "status":
        print("running" if running() else "not running")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
