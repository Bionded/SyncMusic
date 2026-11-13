# SPDX-License-Identifier: GPL-3.0-or-later
"""The server's mDNS / DNS-SD announcement for Wi-Fi clients: `_syncmusic._tcp.local.` with the HTTP port and TXT
records `v` (protocol major), `id` (instance id), `name`, `path` (URL prefix), `tls` and `pair` (a pairing code is
open). Only while Wi-Fi clients are allowed and the announcement is on; a failure (no multicast) is logged, never
fatal."""

from __future__ import annotations

import logging
import socket
from typing import Any

log = logging.getLogger(__name__)

SERVICE = "_syncmusic._tcp.local."
PROTOCOL_MAJOR = 1


def txt_record(
    instance_id: str, name: str, pairing_open: bool, tls: bool = False
) -> dict[str, str]:
    return {
        "v": str(PROTOCOL_MAJOR),
        "id": instance_id,
        "name": name,
        "path": "/",
        "tls": "1" if tls else "0",
        "pair": "1" if pairing_open else "0",
    }


def _addresses() -> list[bytes]:
    """The host's IPv4 addresses that are not loopback (the default-route one first)."""
    out: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent: only picks the outgoing address
            out.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = str(info[4][0])
            if addr not in out and not addr.startswith("127."):
                out.append(addr)
    except OSError:
        pass
    return [socket.inet_aton(a) for a in out]


class Announcer:
    """Keeps the announcement in line with `update()`; `zeroconf_factory` is for tests."""

    def __init__(self, port: int, zeroconf_factory: Any = None) -> None:
        self.port = port
        self._factory = zeroconf_factory
        self._zc: Any = None
        self._info: Any = None
        self._txt: dict[str, str] | None = None

    @property
    def active(self) -> bool:
        return self._info is not None

    def _service_info(self, txt: dict[str, str]) -> Any:
        from zeroconf import ServiceInfo

        host = socket.gethostname().split(".")[0] or "syncmusic"
        label = (txt["name"] or host)[:50].replace(".", " ")
        return ServiceInfo(
            SERVICE,
            f"{label} ({txt['id'][:6]}).{SERVICE}",
            port=self.port,
            properties=txt,
            server=f"{host}.local.",
            addresses=_addresses(),
        )

    async def update(self, on: bool, txt: dict[str, str]) -> None:
        """Announce with these TXT records (re-announced when they changed), or withdraw when `on` is false."""
        if not on:
            await self.stop()
            return
        if self._info is not None and txt == self._txt:
            return
        try:
            if self._zc is None:
                if self._factory is not None:
                    self._zc = self._factory()
                else:
                    from zeroconf.asyncio import AsyncZeroconf

                    self._zc = AsyncZeroconf()
            info = self._service_info(txt)
            if self._info is not None and self._info.name == info.name:
                self._info = info
                await self._zc.async_update_service(info)
            else:
                if self._info is not None:
                    await self._zc.async_unregister_service(self._info)
                self._info = info
                await self._zc.async_register_service(info, allow_name_change=True)
            self._txt = txt
        except Exception as e:  # noqa: BLE001 - discovery is optional
            log.warning("mDNS announcement failed: %s", e)
            self._info = None
            self._txt = None

    async def stop(self) -> None:
        zc, info = self._zc, self._info
        self._zc = self._info = self._txt = None
        if zc is None:
            return
        try:
            if info is not None:
                await zc.async_unregister_service(info)
            await zc.async_close()
        except Exception as e:  # noqa: BLE001
            log.info("mDNS withdraw: %s", e)
