"""The Wi-Fi client device: outbox, inventory, queued deletes, reported capabilities."""

from __future__ import annotations

from syncmusic.core.models import DeviceConfig, Manifest, ManifestEntry, ManifestHeader
from syncmusic.devices.client import ClientDevice, client_formats
from tests.contracts.harness import work_item


def _client(tmp_path) -> ClientDevice:
    dev = ClientDevice(DeviceConfig(id="c1", type="client", name="Phone"), tmp_path / "box", None)
    dev.report(
        {
            "formats": ["mp3", "aac", "vorbis"],
            "max_sample_rate": 48000,
            "media_kinds": ["music", "podcast"],
            "storage": {"total": 1_000_000, "free": 800_000},
            "features": {"playlists": False},
        },
        {},
    )
    return dev


async def _put(dev: ClientDevice, tmp_path, title: str):
    s = await dev.begin()
    item = work_item(tmp_path, title, size=10_000)
    ref = await dev.put_track(s, item)
    m = await dev.read_manifest() or Manifest(header=ManifestHeader(device_id=dev.id))
    m.items[item.track.ref.key()] = ManifestEntry(
        ref=item.track.ref, device_ref=ref.device_ref, path=ref.path, size=ref.size
    )
    await dev.commit(s, m)
    return item, ref, m


def test_capabilities_from_the_report(tmp_path):
    caps = _client(tmp_path).effective_capabilities()
    assert caps.formats == {"mp3", "aac", "ogg"}
    assert caps.max_sample_rate == 48000
    assert caps.supports_podcasts and not caps.supports_audiobooks
    assert not caps.supports_playlists
    assert client_formats([]) is None and client_formats(["flac", "nonsense"]) == {"flac"}


async def test_state_survives_a_new_instance(tmp_path):
    dev = _client(tmp_path)
    again = ClientDevice(dev.config, dev.mount, None)
    assert again.state.storage_free == 800_000 and await again.capacity() == 1_000_000


async def test_outbox_counts_against_free_space_until_acked(tmp_path):
    dev = _client(tmp_path)
    _, ref, m = await _put(dev, tmp_path, "One")
    assert dev.outbox() == {ref.path: 10_000}
    assert await dev.free_space() == 790_000
    assert await dev.missing(m) == set()  # waiting in the outbox, not missing
    dev.delivered(ref.path)
    assert dev.outbox() == {} and dev.state.inventory == {ref.path: 10_000}
    assert not (dev.mount / "Music").exists()  # its empty folders went too
    assert await dev.missing(m) == set()  # on the client
    dev.report(None, {}, [ref.path], full=False)  # the user deleted it on the phone
    assert await dev.missing(m) == set(m.items)


async def test_removed_track_held_by_the_client_is_queued(tmp_path):
    dev = _client(tmp_path)
    item, ref, m = await _put(dev, tmp_path, "One")
    dev.delivered(ref.path)
    s = await dev.begin()
    await dev.remove_track(s, ref)
    m.items.pop(item.track.ref.key())
    await dev.commit(s, m)
    again = ClientDevice(dev.config, dev.mount, None)
    assert again.state.deletes == [ref.path]
    assert ref.path not in {i.path for i in (await again.read_contents()).items}
    again.deleted(ref.path)
    assert again.state.deletes == [] and again.state.inventory == {}


async def test_removed_track_still_in_the_outbox_is_not_queued(tmp_path):
    dev = _client(tmp_path)
    item, ref, m = await _put(dev, tmp_path, "One")
    s = await dev.begin()
    await dev.remove_track(s, ref)
    await dev.commit(s, m)
    assert dev.state.deletes == [] and dev.outbox() == {}


async def test_new_file_never_takes_a_path_the_client_holds(tmp_path):
    dev = _client(tmp_path / "a")
    _, ref, _ = await _put(dev, tmp_path / "a", "One")
    other = _client(tmp_path / "b")
    other.report(
        None, {ref.path: 5}
    )  # a file the client holds there (another app's, or ours from before)
    _, ref2, _ = await _put(other, tmp_path / "b", "One")
    assert ref2.path != ref.path


async def test_wipe_queues_everything_the_client_holds(tmp_path):
    dev = _client(tmp_path)
    _, ref, _ = await _put(dev, tmp_path, "One")
    dev.delivered(ref.path)
    await dev.wipe()
    assert dev.state.deletes == [ref.path] and await dev.read_manifest() is None
    assert ClientDevice(dev.config, dev.mount, None).state.deletes == [ref.path]


def test_client_type_is_not_registered_by_hand():
    from syncmusic.core import registry

    assert registry.device_class("client").paired
    assert not any(c.paired for c in registry.device_classes() if c.type_name != "client")


def _ev(eid: str, type_: str, key: str, **kw):
    from syncmusic.devices.client import ClientEvent

    return ClientEvent(eid=eid, seq=int(eid[1:]), type=type_, key=key, **kw)


async def test_events_become_feedback_in_time_order(tmp_path):
    dev = _client(tmp_path)
    item, _, m = await _put(dev, tmp_path, "One")
    key = item.track.ref.key()
    dev.add_events(
        [
            # sent out of order: the later rating and heart win
            _ev("e3", "rating", key, value=2, at="2026-01-01T12:00:00Z", clock="ntp"),
            _ev("e1", "rating", key, value=5, at="2026-01-01T10:00:00Z", clock="ntp"),
            _ev("e4", "favourite", key, on=False, at="2026-01-01T13:00:00Z", clock="rtc"),
            _ev("e2", "favourite", key, on=True, at="2026-01-01T11:00:00Z", clock="rtc"),
            _ev("e5", "play", key, played_s=10),  # too short
            _ev("e6", "play", key, played_s=300),
            _ev("e7", "play", key, completed=True, at="2025-12-31T09:00:00Z", clock="ntp"),
            _ev("e8", "skip", key, played_s=3),
            _ev("e9", "position", key, position_s=42.5),
            _ev("e10", "rating", "lib/unknown", value=1),
        ]
    )
    fb = await dev.read_feedback(m)
    assert fb.ratings == {key: 2} and fb.stars == {key: False}
    assert len(fb.plays) == 2 and fb.unmapped == 1
    assert fb.plays[0].played_at.year == 2025 and not fb.plays[0].played_at_estimated
    assert fb.plays[1].played_at_estimated  # clock unknown: the receive time
    assert fb.positions[key].position_s == 42.5
    # what changes nothing is consumed at once; the rest waits for the reverse sync's commit
    assert {e.eid for e in dev.load_events().pending} == {f"e{n}" for n in (1, 2, 3, 4, 6, 7, 9)}
    dev.add_events([_ev("e11", "rating", key, value=0)])  # arrives during the run
    await dev.reset_feedback(await dev.begin(), fb)
    assert [e.eid for e in dev.load_events().pending] == ["e11"]
    fb = await dev.read_feedback(m)
    assert fb.ratings == {key: None}  # 0 = cleared


async def test_a_consumed_event_sent_again_is_ignored(tmp_path):
    dev = _client(tmp_path)
    item, _, m = await _put(dev, tmp_path, "One")
    key = item.track.ref.key()
    assert dev.add_events([_ev("e1", "play", key, completed=True)])["accepted"] == 1
    fb = await dev.read_feedback(m)
    await dev.reset_feedback(await dev.begin(), fb)
    again = ClientDevice(dev.config, dev.mount, None)
    assert again.add_events([_ev("e1", "play", key, completed=True)]) == {
        "acked_seq": 1,
        "accepted": 0,
        "duplicates": 1,
    }
    assert (await again.read_feedback(m)).is_empty()
