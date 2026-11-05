"""HiBy R1 - Compás Beta stub: probe, Podcasts marker, Wipe keeps the player's files."""

import asyncio
import sqlite3

from syncmusic.core.models import DeviceConfig
from syncmusic.devices.HibyR1.compas import HibyCompas
from syncmusic.devices.HibyR1.sonixos import Hiby


def test_probe_needs_the_compas_folder(tmp_path):
    assert HibyCompas.probe(None, tmp_path) is None
    (tmp_path / ".compas").mkdir()
    assert HibyCompas.probe(None, tmp_path).type_name == "hiby_compas"


def test_compas_outranks_sonixos_on_a_card_with_both(tmp_path):
    (tmp_path / ".compas").mkdir()
    (tmp_path / ".local").mkdir()
    sqlite3.connect(tmp_path / ".local" / "library.db").close()
    assert HibyCompas.probe(None, tmp_path).confidence > Hiby.probe(None, tmp_path).confidence


def _dev(tmp_path) -> HibyCompas:
    (tmp_path / ".compas").mkdir(exist_ok=True)
    return HibyCompas(DeviceConfig(id="c", type="hiby_compas", name="C"), tmp_path, None)


def test_begin_marks_podcasts_ignored(tmp_path):
    dev = _dev(tmp_path)
    asyncio.run(dev.begin())
    assert (tmp_path / "Podcasts" / "database.ignore").is_file()


def test_wipe_keeps_the_players_files(tmp_path):
    dev = _dev(tmp_path)
    for name in (".plugins/p.lua", "themes/t.json", "radio.txt", "r1.upt", "Music/a.mp3", "x.txt"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")
    (tmp_path / ".compas" / "device.json").write_text("{}")
    assert asyncio.run(dev.wipe()) == 2
    left = {p.name for p in tmp_path.iterdir()}
    assert left == {".compas", ".plugins", "themes", "radio.txt", "r1.upt"}


def test_not_registered():
    """Placeholder only: not offered as a device type."""
    from syncmusic.core import registry

    assert "hiby_compas" not in registry.DEVICES
