"""Network discovery on demand: an mDNS-announced SMB / FTP server is found (a fake responder
announces the compose servers, which do not announce themselves), and the compose Samba's shares are listed with
the test user and as a guest; a closed port says why."""

from __future__ import annotations

import socket

import pytest
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from syncmusic.infra import discovery
from tests.integration import services as svc

pytestmark = pytest.mark.integration


def _lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # no packet is sent: picks the outgoing interface
        return s.getsockname()[0]


async def test_mdns_announced_servers_are_found():
    ip = _lan_ip()
    azc = AsyncZeroconf(interfaces=[ip])
    infos = [
        ServiceInfo(
            "_smb._tcp.local.",
            "SyncMusic Test NAS._smb._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=int(svc.ENV["SMB_PORT"]),
            server="syncmusic-test-nas.local.",
        ),
        ServiceInfo(
            "_ftp._tcp.local.",
            "SyncMusic Test FTP._ftp._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=2121,
            server="syncmusic-test-nas.local.",
        ),
    ]
    for info in infos:
        await azc.async_register_service(info)
    try:
        found = await discovery.find_servers(["smb", "ftp"], timeout_s=3)
    finally:
        for info in infos:
            await azc.async_unregister_service(info)
        await azc.async_close()
    mine = {(s.kind, s.name): s for s in found if s.address == ip and s.source == "mdns"}
    smb = mine[("smb", "SyncMusic Test NAS")]
    assert smb.host == "syncmusic-test-nas.local" and smb.port == int(svc.ENV["SMB_PORT"])
    assert mine[("ftp", "SyncMusic Test FTP")].port == 2121


async def test_shares_of_the_compose_samba():
    host, port = svc.ENV["SMB_HOST"], int(svc.ENV["SMB_PORT"])
    shares = await discovery.list_shares(host, port, svc.ENV["SMB_USER"], svc.ENV["SMB_PASSWORD"])
    assert [s.name for s in shares] == [
        "devices",
        "music",
        "public",
    ]  # no IPC$ ([devices]: the SMB device)
    assert [s.name for s in await discovery.list_shares(host, port)] == [
        "devices",
        "music",
        "public",
    ]


async def test_a_closed_port_says_why():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    with pytest.raises(discovery.DiscoveryError, match=f"127.0.0.1:{free}"):
        await discovery.list_shares("127.0.0.1", free, timeout_s=3)
