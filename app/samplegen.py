"""Build and tamper with synthetic 512-byte-sector GPT disks.

Shared by the bundled scenarios, the sample generator tool and the tests, so
it ships inside the application image.
"""

from __future__ import annotations

import os
import struct
import uuid
from pathlib import Path

from .gpt import (
    GPT_ENTRY_ARRAY_SIZE,
    GPTLayout,
    build_layout,
    crc32,
    encode_entry,
    layout_for_sector_size,
    pread_exact,
    pwrite_all,
    refresh_header_crc,
    write_layout,
)

SECTOR = 512
MIB = 1024 * 1024

ESP_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX_TYPE = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"


def _guid(seed: int) -> str:
    return f"{seed:08x}-1111-4222-8333-{seed:012x}"


def _backup_offsets(path: Path) -> tuple[int, int]:
    size = path.stat().st_size
    last_lba, _, _, es = layout_for_sector_size(size, SECTOR)
    return (last_lba - es) * SECTOR, last_lba * SECTOR


def patch_bytes(path: Path, offset: int, data: bytes) -> None:
    fd = os.open(path, os.O_RDWR)
    try:
        pwrite_all(fd, data, offset)
        os.fsync(fd)
    finally:
        os.close(fd)


def make_disk(
    path: Path,
    *,
    disk_mib: int = 64,
    partitions: list[dict] | None = None,
    disk_guid: str = "8b0e5f1a-2c4d-4e8f-9a3b-1c2d3e4f5a6b",
    extra_bytes: int = 0,
) -> GPTLayout:
    """Create a valid dual-GPT disk whose partitions are 1 MiB aligned."""
    size = disk_mib * MIB + extra_bytes
    if partitions is None:
        partitions = [
            dict(index=1, type=ESP_TYPE, unique=_guid(1), name="EFI",
                 start=1 * MIB, end=4 * MIB, fill=b"\x11"),
            dict(index=2, type=LINUX_TYPE, unique=_guid(2), name="data",
                 start=5 * MIB, end=size - 2 * MIB - extra_bytes, fill=b"\x22"),
        ]
    specs = []
    for p in partitions:
        raw = encode_entry(
            p["type"], p["unique"], 0, 0,
            attributes=p.get("attributes", 0), name=p.get("name", ""),
        )
        specs.append((p["index"], raw, p["start"] // SECTOR, p["end"] // SECTOR - 1))
    layout = build_layout(size, SECTOR, uuid.UUID(disk_guid).bytes_le, specs)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, size)
        for p in partitions:
            fill = p.get("fill", b"\x22")
            offset = p["start"]
            while offset < p["end"]:
                block = min(MIB, p["end"] - offset)
                pwrite_all(fd, fill * block, offset)
                offset += block
            pwrite_all(
                fd,
                f"partition {p['index']} payload at {p['start']}\n".encode(),
                p["start"] + 1024,
            )
        write_layout(fd, layout)
        os.fsync(fd)
    finally:
        os.close(fd)
    return layout


def corrupt_primary_header(path: Path) -> None:
    patch_bytes(path, SECTOR + 16, b"\xde\xad\xbe\xef")


def corrupt_backup_entries(path: Path) -> None:
    entries_lba, _ = _backup_offsets(path)
    patch_bytes(path, entries_lba + 40, b"\xff\xff\xff\xff")


def rewrite_header_field(
    path: Path, which: str, offset: int, value: bytes
) -> None:
    """Patch a header field and recompute HeaderCRC32."""
    size = path.stat().st_size
    last_lba = size // SECTOR - 1
    lba = 1 if which == "primary" else last_lba
    fd = os.open(path, os.O_RDWR)
    try:
        header = bytearray(pread_exact(fd, 92, lba * SECTOR))
        header[offset : offset + len(value)] = value
        pwrite_all(fd, refresh_header_crc(bytes(header)), lba * SECTOR)
        os.fsync(fd)
    finally:
        os.close(fd)


def patch_backup_entries(
    path: Path, mutate, *, fix_header: bool = True
) -> None:
    """Apply ``mutate(bytearray)`` to the backup entry array.

    When ``fix_header`` is true, the backup header's EntriesCRC32 is updated
    so the copy stays structurally valid — used to test semantic conflicts
    rather than CRC failures.
    """
    entries_lba, header_lba = _backup_offsets(path)
    fd = os.open(path, os.O_RDWR)
    try:
        entries = bytearray(pread_exact(fd, GPT_ENTRY_ARRAY_SIZE, entries_lba))
        mutate(entries)
        pwrite_all(fd, bytes(entries), entries_lba)
        if fix_header:
            header = bytearray(pread_exact(fd, 92, header_lba))
            header[88:92] = struct.pack("<I", crc32(bytes(entries)))
            pwrite_all(fd, refresh_header_crc(bytes(header)), header_lba)
        os.fsync(fd)
    finally:
        os.close(fd)


def patch_primary_entries(path: Path, mutate) -> None:
    """Apply ``mutate(bytearray)`` to the primary array, fixing its header."""
    fd = os.open(path, os.O_RDWR)
    try:
        entries = bytearray(pread_exact(fd, GPT_ENTRY_ARRAY_SIZE, 2 * SECTOR))
        mutate(entries)
        pwrite_all(fd, bytes(entries), 2 * SECTOR)
        header = bytearray(pread_exact(fd, 92, SECTOR))
        header[88:92] = struct.pack("<I", crc32(bytes(entries)))
        pwrite_all(fd, refresh_header_crc(bytes(header)), SECTOR)
        os.fsync(fd)
    finally:
        os.close(fd)


def set_backup_disk_guid(path: Path, new_guid: str) -> None:
    rewrite_header_field(path, "backup", 56, uuid.UUID(new_guid).bytes_le)


def add_hybrid_mbr_entry(path: Path) -> None:
    patch_bytes(path, 446 + 16 + 4, b"\x83")


def wipe_mbr_entries(path: Path) -> None:
    patch_bytes(path, 446, b"\x00" * 66)
