# SPDX-License-Identifier: GPL-3.0-or-later
"""Network discovery on demand: SMB and FTP servers on the LAN for Add library and the network
device's register form, and the shares of one SMB server. Never periodic: the UI asks when the user presses
"Find servers on the network".

Servers come from mDNS (`_smb._tcp`, `_ftp._tcp`; zeroconf), with WS-Discovery (Windows / wsdd hosts) and a NetBIOS
broadcast (Samba / Windows name tables) as fallbacks for SMB. Shares are listed with `smbclient -L` (smbprotocol,
which the SMB folder library uses, cannot enumerate shares; pysmb speaks no SMB2.1+). Everything is read-only."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

log = logging.getLogger(__name__)

Kind = Literal["smb", "ftp"]
MDNS_TYPES: dict[Kind, str] = {"smb": "_smb._tcp.local.", "ftp": "_ftp._tcp.local."}
DEFAULT_PORTS: dict[Kind, int] = {"smb": 445, "ftp": 21}
WSD_ADDR = ("239.255.255.250", 3702)


class Server(BaseModel):
    kind: Kind
    name: str  # what the server calls itself, e.g. "nas"
    host: str  # a name to connect to ("nas.local"), else the address
    address: str  # IPv4
    port: int
    source: Literal["mdns", "wsd", "netbios"]


class Share(BaseModel):
    name: str
    comment: str = ""


class DiscoveryError(Exception):
    pass


async def find_servers(kinds: list[Kind], timeout_s: float = 3.0) -> list[Server]:
    """The servers that answer within `timeout_s`, one per (kind, address), mDNS first."""
    tasks = [_mdns(kinds, timeout_s)]
    if "smb" in kinds:
        tasks += [_wsd(timeout_s), _netbios(timeout_s)]
    found: list[Server] = []
    for result in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(result, BaseException):
            log.info(
                "discovery: %s", result
            )  # one way failing (no multicast route) is not an error
            continue
        found += result
    seen: set[tuple[str, str]] = set()
    out = []
    for s in found:
        if (s.kind, s.address) not in seen:
            seen.add((s.kind, s.address))
            out.append(s)
    return sorted(out, key=lambda s: (s.kind, s.name.lower(), s.address))


async def _mdns(kinds: list[Kind], timeout_s: float) -> list[Server]:
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    names: dict[str, Kind] = {}

    def on_change(
        zeroconf: object, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        if state_change is not ServiceStateChange.Removed:
            names[name] = next(k for k, t in MDNS_TYPES.items() if t == service_type)

    azc = AsyncZeroconf()
    try:
        browser = AsyncServiceBrowser(
            azc.zeroconf, [MDNS_TYPES[k] for k in kinds], handlers=[on_change]
        )
        await asyncio.sleep(timeout_s)
        await browser.async_cancel()
        out = []
        for name, kind in names.items():
            info = AsyncServiceInfo(MDNS_TYPES[kind], name)
            if not await info.async_request(azc.zeroconf, 1500):
                continue
            addresses = info.parsed_addresses()
            v4 = [a for a in addresses if ":" not in a]
            if not v4:
                continue
            label = name.removesuffix("." + MDNS_TYPES[kind])
            host = (info.server or "").rstrip(".") or v4[0]
            out.append(
                Server(
                    kind=kind,
                    name=label,
                    host=host,
                    address=v4[0],
                    port=info.port or DEFAULT_PORTS[kind],
                    source="mdns",
                )
            )
        return out
    finally:
        await azc.async_close()


_WSD_PROBE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof">
<soap:Header><wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
<wsa:MessageID>urn:uuid:{id}</wsa:MessageID></soap:Header>
<soap:Body><wsd:Probe><wsd:Types>wsdp:Device</wsd:Types></wsd:Probe></soap:Body></soap:Envelope>"""


class _Collect(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: list[tuple[bytes, str]] = []

    def datagram_received(self, data: bytes, addr: tuple[str | object, ...]) -> None:
        self.replies.append((data, str(addr[0])))


async def _udp(
    payload: bytes, target: tuple[str, int], timeout_s: float, broadcast: bool = False
) -> list[tuple[bytes, str]]:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST if broadcast else socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.bind(("0.0.0.0", 0))
    transport, proto = await loop.create_datagram_endpoint(_Collect, sock=sock)
    try:
        transport.sendto(payload, target)
        await asyncio.sleep(timeout_s)
        return proto.replies
    finally:
        transport.close()


async def _wsd(timeout_s: float) -> list[Server]:
    """WS-Discovery ProbeMatches: Windows and wsdd hosts offer SMB (the computer's name is not in the reply; the
    address is shown and resolved by reverse DNS where possible)."""
    replies = await _udp(_WSD_PROBE.format(id=uuid.uuid4()).encode(), WSD_ADDR, timeout_s)
    out = []
    for data, addr in replies:
        if b"ProbeMatches" not in data:
            continue
        name = await _reverse(addr)
        out.append(
            Server(
                kind="smb",
                name=addr if name == addr else name.split(".")[0],
                host=name,
                address=addr,
                port=445,
                source="wsd",
            )
        )
    return out


def _nbstat_query() -> bytes:
    """A NetBIOS node status request (NBSTAT) for the wildcard name `*`: every host answers with its name table."""
    name = b"*" + b"\x00" * 15
    encoded = bytearray([32])
    for ch in name:
        encoded += bytes([((ch >> 4) & 0x0F) + 0x41, (ch & 0x0F) + 0x41])
    encoded += b"\x00"
    header = bytes(
        [0x13, 0x37, 0x00, 0x10, 0, 1, 0, 0, 0, 0, 0, 0]
    )  # id, broadcast flag, one question
    return header + bytes(encoded) + b"\x00\x21\x00\x01"  # NBSTAT, IN


def parse_nbstat(data: bytes) -> str | None:
    """The file server name (suffix 0x20) of a node status response; None if the host runs none."""
    pos = 12 + 34 + 4 + 4 + 2  # header, name, type/class, TTL, rdlength
    if len(data) <= pos:
        return None
    count = data[pos]
    pos += 1
    for i in range(count):
        entry = data[pos + 18 * i : pos + 18 * (i + 1)]
        if len(entry) < 18:
            break
        group = bool(entry[16] & 0x80)
        if entry[15] == 0x20 and not group:
            return entry[:15].decode("ascii", "replace").strip()
    return None


async def _netbios(timeout_s: float) -> list[Server]:
    """A NetBIOS node status broadcast: hosts with the file server service (Samba, Windows) answer with their name."""
    replies = await _udp(_nbstat_query(), ("255.255.255.255", 137), timeout_s, broadcast=True)
    out = []
    for data, addr in replies:
        name = parse_nbstat(data)
        if name:
            out.append(
                Server(kind="smb", name=name, host=addr, address=addr, port=445, source="netbios")
            )
    return out


async def _reverse(addr: str) -> str:
    try:
        name, *_ = await asyncio.wait_for(asyncio.to_thread(socket.gethostbyaddr, addr), 1.0)
    except (OSError, TimeoutError):
        return addr
    return str(name)


async def list_shares(
    host: str, port: int = 445, username: str = "", password: str = "", timeout_s: float = 15.0
) -> list[Share]:
    """The disk shares of an SMB server (empty user: guest), via `smbclient -L` (apt `smbclient`; any SMB dialect).
    The login goes in a 0600 authentication file, never on the command line."""
    exe = shutil.which("smbclient")
    if exe is None:
        raise DiscoveryError("listing shares needs smbclient (apt install smbclient)")
    with tempfile.TemporaryDirectory(prefix="syncmusic-smb-") as tmp:
        args = [exe, "-L", f"//{host}", "-p", str(port), "-g", "-t", str(int(timeout_s))]
        if username:
            auth = Path(tmp) / "auth"
            auth.touch(mode=0o600)
            auth.write_text(f"username={username}\npassword={password}\n")
            args += ["-A", str(auth)]
        else:
            args.append("-N")
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout_s + 5)
        except TimeoutError:
            proc.kill()
            raise DiscoveryError(f"{host}:{port} did not answer") from None
    text = out.decode("utf-8", "replace")
    shares = []
    for line in text.splitlines():
        parts = line.split("|")
        # disk shares only: no IPC$, printers or hidden admin shares
        if len(parts) >= 2 and parts[0] == "Disk" and not parts[1].endswith("$"):
            shares.append(Share(name=parts[1], comment="|".join(parts[2:])))
    if proc.returncode != 0 and not shares:
        status = re.search(r"NT_STATUS_[A-Z_]+", text)
        reason = {
            "NT_STATUS_LOGON_FAILURE": "wrong user name or password",
            "NT_STATUS_ACCESS_DENIED": "access denied",
            "NT_STATUS_CONNECTION_REFUSED": "connection refused",
            "NT_STATUS_IO_TIMEOUT": "no answer",
            "NT_STATUS_HOST_UNREACHABLE": "host unreachable",
        }.get(status.group(0) if status else "", status.group(0) if status else text.strip()[-200:])
        raise DiscoveryError(f"cannot list the shares of {host}:{port}: {reason}")
    return sorted(shares, key=lambda s: s.name.lower())
