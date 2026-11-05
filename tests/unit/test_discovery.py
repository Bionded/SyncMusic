"""Network discovery pieces without a network."""

from __future__ import annotations

from syncmusic.infra.discovery import _nbstat_query, parse_nbstat


def _entry(name: str, suffix: int, group: bool = False) -> bytes:
    return name.ljust(15).encode() + bytes([suffix, 0x84 if group else 0x04, 0])


def test_nbstat_answer_names_the_file_server():
    head = _nbstat_query()[: 12 + 34 + 4]  # header + name + type/class, as the answer repeats them
    entries = [_entry("WORKGROUP", 0x00, group=True), _entry("NAS", 0x00), _entry("NAS", 0x20)]
    answer = (
        head
        + b"\0\0\0\0"
        + len(entries * 18).to_bytes(2, "big")
        + bytes([len(entries)])
        + b"".join(entries)
    )
    assert parse_nbstat(answer) == "NAS"
    no_server = head + b"\0" * 6 + bytes([1]) + _entry("PRINTER", 0x00)
    assert parse_nbstat(no_server) is None
    assert parse_nbstat(b"short") is None


def test_nbstat_query_asks_for_the_wildcard():
    q = _nbstat_query()
    assert len(q) == 12 + 34 + 4 and q[-4:] == b"\x00\x21\x00\x01"  # NBSTAT, IN
    assert q[13:15] == b"CK"  # '*' = 0x2A → 'C','K'
