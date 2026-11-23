"""Low-level GPT building, parsing and verification.

This module is independent of the web layer.  It understands both 512-byte
and 4096-byte logical sectors, always assumes a 128-entry table with
128-byte entries, and never reads more than a bounded chunk at a time.
"""

from __future__ import annotations

import hashlib
import os
import struct
import uuid
import zlib
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

GPT_SIGNATURE = b"EFI PART"
GPT_REVISION = 0x00010000
GPT_HEADER_SIZE = 92
GPT_ENTRY_COUNT = 128
GPT_ENTRY_SIZE = 128
GPT_ENTRY_ARRAY_SIZE = GPT_ENTRY_COUNT * GPT_ENTRY_SIZE  # 16384
MBR_SIGNATURE = b"\x55\xaa"
ZERO_GUID = b"\x00" * 16
PROTECTIVE_MBR_TYPE = 0xEE
COPY_CHUNK = 4 * 1024 * 1024

_HEADER = struct.Struct("<8sIIIIQQQQ16sQIII")
_ENTRY_HEADER = struct.Struct("<16s16sQQQ72s")


class GPTValidationError(Exception):
    """One GPT copy is unusable; ``reasons`` is a list of readable causes."""

    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def guid_from_str(value: str) -> bytes:
    return uuid.UUID(value).bytes_le


def guid_to_str(value: bytes) -> str:
    return str(uuid.UUID(bytes_le=value)).upper()


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def refresh_header_crc(raw_header: bytes) -> bytes:
    """Return a header with HeaderCRC32 recomputed from its own contents.

    Used by tests and tools that intentionally patch a GPT field.
    """
    if len(raw_header) < GPT_HEADER_SIZE:
        raise ValueError("GPT header must be at least 92 bytes")
    out = bytearray(raw_header)
    header_size = struct.unpack_from("<I", out, 12)[0]
    if header_size < GPT_HEADER_SIZE or header_size > len(out):
        raise ValueError("invalid header size field")
    out[16:20] = b"\x00\x00\x00\x00"
    struct.pack_into("<I", out, 16, crc32(bytes(out[:header_size])))
    return bytes(out)


def entry_sectors(sector_size: int) -> int:
    return (GPT_ENTRY_ARRAY_SIZE + sector_size - 1) // sector_size


def layout_for_sector_size(
    device_size: int, sector_size: int
) -> tuple[int, int, int, int]:
    """Return (last_lba, first_usable_lba, last_usable_lba, entry_sectors)."""
    if device_size <= 0 or device_size % sector_size:
        raise ValueError("device size is not a whole number of sectors")
    last_lba = device_size // sector_size - 1
    es = entry_sectors(sector_size)
    first_usable = 2 + es
    last_usable = last_lba - es - 1
    return last_lba, first_usable, last_usable, es


def reserved_head_bytes(device_size: int, sector_size: int) -> int:
    return layout_for_sector_size(device_size, sector_size)[1] * sector_size


def reserved_tail_bytes(device_size: int, sector_size: int) -> int:
    _, _, _, es = layout_for_sector_size(device_size, sector_size)
    return (es + 1) * sector_size


def pread_exact(fd: int, length: int, offset: int) -> bytes:
    if length < 0 or offset < 0:
        raise ValueError("negative read")
    parts: list[bytes] = []
    done = 0
    while done < length:
        chunk = os.pread(fd, length - done, offset + done)
        if not chunk:
            raise EOFError(f"unexpected EOF at byte {offset + done}")
        parts.append(chunk)
        done += len(chunk)
    return b"".join(parts)


def pwrite_all(fd: int, data: bytes, offset: int) -> None:
    view = memoryview(data)
    done = 0
    while done < len(view):
        written = os.pwrite(fd, view[done:], offset + done)
        if written <= 0:
            raise OSError("short write")
        done += written


def zero_range(fd: int, offset: int, length: int) -> None:
    if length <= 0:
        return
    chunk = bytes(min(length, 1024 * 1024))
    done = 0
    while done < length:
        piece = chunk[: min(len(chunk), length - done)]
        pwrite_all(fd, piece, offset + done)
        done += len(piece)


def encode_entry(
    type_guid: str | bytes,
    unique_guid: str | bytes,
    start_lba: int = 0,
    end_lba: int = 0,
    *,
    attributes: int = 0,
    name: str = "",
) -> bytes:
    if isinstance(type_guid, str):
        type_guid = guid_from_str(type_guid)
    if isinstance(unique_guid, str):
        unique_guid = guid_from_str(unique_guid)
    if len(type_guid) != 16 or len(unique_guid) != 16:
        raise ValueError("GUIDs must be 16 bytes")
    name_bytes = name.encode("utf-16-le")
    if len(name_bytes) > 72:
        raise ValueError("partition name is longer than 36 UTF-16 code units")
    return _ENTRY_HEADER.pack(
        type_guid,
        unique_guid,
        start_lba,
        end_lba,
        attributes,
        name_bytes.ljust(72, b"\x00"),
    )


def patch_entry_lbas(raw: bytes, start_lba: int, end_lba: int) -> bytes:
    if len(raw) != GPT_ENTRY_SIZE:
        raise ValueError("partition entry must be 128 bytes")
    out = bytearray(raw)
    struct.pack_into("<Q", out, 32, start_lba)
    struct.pack_into("<Q", out, 40, end_lba)
    return bytes(out)


@dataclass(frozen=True)
class Partition:
    index: int  # 1-based table index
    entry_offset: int
    raw: bytes
    type_guid: bytes
    unique_guid: bytes
    start_lba: int
    end_lba: int
    attributes: int
    name: str

    @property
    def type_guid_text(self) -> str:
        return guid_to_str(self.type_guid)

    @property
    def unique_guid_text(self) -> str:
        return guid_to_str(self.unique_guid)

    def byte_range(self, sector_size: int) -> tuple[int, int]:
        """Return the [start, end) byte range for a given logical sector size."""
        return self.start_lba * sector_size, (self.end_lba + 1) * sector_size


def parse_entries(entries: bytes) -> list[Partition]:
    if len(entries) != GPT_ENTRY_ARRAY_SIZE:
        raise ValueError("expected a 16384-byte partition entry array")
    result: list[Partition] = []
    for i in range(GPT_ENTRY_COUNT):
        off = i * GPT_ENTRY_SIZE
        raw = entries[off : off + GPT_ENTRY_SIZE]
        type_guid = raw[0:16]
        if type_guid == ZERO_GUID:
            continue
        start_lba, end_lba, attributes = struct.unpack_from("<QQQ", raw, 32)
        name = (
            raw[56:128].decode("utf-16-le", errors="replace").split("\x00", 1)[0]
        )
        result.append(
            Partition(
                index=i + 1,
                entry_offset=off,
                raw=raw,
                type_guid=type_guid,
                unique_guid=raw[16:32],
                start_lba=start_lba,
                end_lba=end_lba,
                attributes=attributes,
                name=name,
            )
        )
    return result


@dataclass(frozen=True)
class GPTHeader:
    which: str  # "primary" | "backup"
    header_size: int
    revision: int
    header_crc: int
    my_lba: int
    alternate_lba: int
    first_usable_lba: int
    last_usable_lba: int
    disk_guid: bytes
    partition_entry_lba: int
    number_of_entries: int
    size_of_entry: int
    entries_crc: int


def read_gpt_copy(
    fd: int, device_size: int, sector_size: int, which: str
) -> tuple[GPTHeader, bytes]:
    """Parse and strictly validate one GPT copy (header CRC + entry CRC)."""
    if which not in ("primary", "backup"):
        raise ValueError("which must be primary or backup")
    if device_size % sector_size:
        raise GPTValidationError(
            [f"盘长 {device_size} 字节不是 {sector_size} 字节扇区的整数倍"]
        )
    last_lba, first_usable_min, last_usable_max, es = layout_for_sector_size(
        device_size, sector_size
    )
    if first_usable_min > last_usable_max:
        raise GPTValidationError([f"盘长 {device_size} 字节不足以容纳 GPT"])

    expected_my = 1 if which == "primary" else last_lba
    expected_alt = last_lba if which == "primary" else 1
    expected_entry_lba = 2 if which == "primary" else last_lba - es
    label = "主 GPT" if which == "primary" else "备 GPT"
    reasons: list[str] = []

    try:
        block = pread_exact(fd, sector_size, expected_my * sector_size)
    except EOFError:
        raise GPTValidationError(
            [f"{label}表头超出盘尾（LBA {expected_my}）"]
        ) from None

    if block[:8] != GPT_SIGNATURE:
        raise GPTValidationError(
            [f"{label}在 LBA {expected_my} 处没有 EFI PART 签名"]
        )
    if len(block) < GPT_HEADER_SIZE:
        raise GPTValidationError([f"{label}表头长度不足 92 字节"])

    (
        _sig,
        revision,
        header_size,
        header_crc,
        reserved,
        my_lba,
        alternate_lba,
        first_usable,
        last_usable,
        disk_guid,
        entry_lba,
        num_entries,
        size_entry,
        entries_crc,
    ) = _HEADER.unpack_from(block, 0)

    if header_size < GPT_HEADER_SIZE or header_size > sector_size:
        raise GPTValidationError(
            [
                f"{label}表头长度 {header_size} 非法"
                f"（应在 92..{sector_size} 之间）"
            ]
        )

    header_bytes = bytearray(block[:header_size])
    header_bytes[16:20] = b"\x00\x00\x00\x00"
    actual_header_crc = crc32(bytes(header_bytes))
    if actual_header_crc != header_crc:
        reasons.append(
            f"{label}表头 CRC32 不符（存储 {header_crc:08x}，"
            f"实算 {actual_header_crc:08x}）"
        )
    if revision != GPT_REVISION:
        reasons.append(f"{label}版本号 0x{revision:08x} 不是 UEFI GPT 1.0")
    if reserved != 0:
        reasons.append(f"{label}表头 Reserved 字段不为 0")
    if disk_guid == ZERO_GUID:
        reasons.append(f"{label}磁盘 GUID 为空")
    if my_lba != expected_my:
        reasons.append(f"{label} MyLBA={my_lba}，应为 {expected_my}")
    if alternate_lba != expected_alt:
        reasons.append(f"{label} AlternateLBA={alternate_lba}，应为 {expected_alt}")
    if entry_lba != expected_entry_lba:
        reasons.append(
            f"{label}分区项位置 LBA={entry_lba}，标准布局应为 {expected_entry_lba}"
        )
    if num_entries != GPT_ENTRY_COUNT or size_entry != GPT_ENTRY_SIZE:
        reasons.append(
            f"{label}分区表为 {num_entries} 项 × {size_entry} 字节，"
            f"只支持 {GPT_ENTRY_COUNT} 项 × {GPT_ENTRY_SIZE} 字节"
        )
        raise GPTValidationError(reasons)
    if first_usable < first_usable_min:
        reasons.append(
            f"{label} FirstUsableLBA={first_usable} 落入分区项保留区"
            f"（最小 {first_usable_min}）"
        )
    if last_usable > last_usable_max:
        reasons.append(
            f"{label} LastUsableLBA={last_usable} 落入备份 GPT 保留区"
            f"（最大 {last_usable_max}）"
        )
    if first_usable > last_usable:
        reasons.append(
            f"{label}可用区间为空：FirstUsableLBA={first_usable} > "
            f"LastUsableLBA={last_usable}"
        )

    array_offset = entry_lba * sector_size
    if array_offset + GPT_ENTRY_ARRAY_SIZE > device_size:
        reasons.append(f"{label}分区项数组超出盘尾")
        raise GPTValidationError(reasons)
    try:
        entries = pread_exact(fd, GPT_ENTRY_ARRAY_SIZE, array_offset)
    except EOFError:
        reasons.append(f"{label}分区项数组读取失败")
        raise GPTValidationError(reasons) from None
    actual_entries_crc = crc32(entries)
    if actual_entries_crc != entries_crc:
        reasons.append(
            f"{label}分区项 CRC32 不符（存储 {entries_crc:08x}，"
            f"实算 {actual_entries_crc:08x}）"
        )

    if reasons:
        raise GPTValidationError(reasons)

    header = GPTHeader(
        which=which,
        header_size=header_size,
        revision=revision,
        header_crc=header_crc,
        my_lba=my_lba,
        alternate_lba=alternate_lba,
        first_usable_lba=first_usable,
        last_usable_lba=last_usable,
        disk_guid=disk_guid,
        partition_entry_lba=entry_lba,
        number_of_entries=num_entries,
        size_of_entry=size_entry,
        entries_crc=entries_crc,
    )
    return header, entries


@dataclass
class MBRInfo:
    signature_ok: bool
    ee_count: int
    non_ee_types: list[int]
    protective_start_lba: int | None
    protective_size_lbas: int | None
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def hybrid(self) -> bool:
        return self.ee_count > 1 or bool(self.non_ee_types)


def inspect_mbr(fd: int, device_size: int, sector_size: int) -> MBRInfo:
    """Inspect the protective MBR.

    Everything that deviates from the canonical single-0xEE layout lands in
    ``problems`` (fatal when checking the produced image) and is echoed in
    ``warnings`` (informational for the source, where it gets rebuilt).
    ``hybrid`` is the one condition the converter rejects outright on the
    source side.
    """
    block = pread_exact(fd, 512, 0)
    signature_ok = block[510:512] == MBR_SIGNATURE
    info = MBRInfo(
        signature_ok=signature_ok,
        ee_count=0,
        non_ee_types=[],
        protective_start_lba=None,
        protective_size_lbas=None,
    )

    def problem(message: str) -> None:
        info.problems.append(message)
        info.warnings.append(message)

    if not signature_ok:
        problem("保护 MBR 缺少 0x55AA 结束标志")

    expected_size = min(device_size // sector_size - 1, 0xFFFFFFFF)
    for i in range(4):
        off = 446 + i * 16
        ptype = block[off + 4]
        start = struct.unpack_from("<I", block, off + 8)[0]
        count = struct.unpack_from("<I", block, off + 12)[0]
        if ptype == 0:
            if start or count:
                info.warnings.append(
                    f"MBR 第 {i + 1} 项类型为 0 但起始/大小非零"
                )
            continue
        if ptype == PROTECTIVE_MBR_TYPE:
            info.ee_count += 1
            info.protective_start_lba = start
            info.protective_size_lbas = count
        else:
            info.non_ee_types.append(ptype)

    if info.hybrid:
        info.problems.append(
            "保护 MBR 含非 0xEE 分区项或多于一个 0xEE 项（混合 MBR）"
        )
    elif info.ee_count == 0:
        problem("保护 MBR 没有 0xEE 保护分区")
    else:
        if info.protective_start_lba != 1:
            problem(
                f"保护 MBR 起始 LBA 为 {info.protective_start_lba}，标准应为 1"
            )
        if info.protective_size_lbas != expected_size:
            problem(
                f"保护 MBR 大小 {info.protective_size_lbas} 个扇区，应为 "
                f"{expected_size}（盘长减 1，或 0xFFFFFFFF 饱和值）"
            )
    return info


@dataclass
class PartitionIssues:
    warnings: list[str] = field(default_factory=list)
    alignment: list[str] = field(default_factory=list)
    overlap: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)

    @property
    def fatal(self) -> list[str]:
        return self.alignment + self.overlap + self.invalid


def check_partitions(
    header: GPTHeader,
    entries: bytes,
    device_size: int,
    source_sector_size: int,
    target_sector_size: int,
) -> tuple[list[Partition], PartitionIssues]:
    """Semantic checks plus the 4 KiB alignment / new-GPT overlap rules."""
    partitions = parse_entries(entries)
    issues = PartitionIssues()

    for i in range(GPT_ENTRY_COUNT):
        off = i * GPT_ENTRY_SIZE
        raw = entries[off : off + GPT_ENTRY_SIZE]
        if raw[0:16] == ZERO_GUID and any(raw):
            issues.warnings.append(f"分区项 {i + 1} 类型为空但含残留数据")

    seen_ids: dict[bytes, int] = {}
    for p in partitions:
        label = f"分区 {p.index}"
        if p.unique_guid == ZERO_GUID:
            issues.invalid.append(f"{label} 的唯一标识 GUID 为空")
        elif p.unique_guid in seen_ids:
            issues.invalid.append(
                f"{label} 与分区 {seen_ids[p.unique_guid]} 的唯一标识 GUID 重复"
            )
        else:
            seen_ids[p.unique_guid] = p.index
        if p.start_lba > p.end_lba:
            issues.invalid.append(
                f"{label} 起始 LBA {p.start_lba} 大于结束 LBA {p.end_lba}"
            )
            continue
        if p.start_lba < header.first_usable_lba or p.end_lba > header.last_usable_lba:
            issues.invalid.append(
                f"{label} 范围 LBA {p.start_lba}..{p.end_lba} 超出 GPT 可用区 "
                f"{header.first_usable_lba}..{header.last_usable_lba}"
            )

    used = sorted(
        (p for p in partitions if p.start_lba <= p.end_lba),
        key=lambda p: p.start_lba,
    )
    for previous, current in zip(used, used[1:]):
        if current.start_lba <= previous.end_lba:
            issues.invalid.append(
                f"分区 {previous.index} 与分区 {current.index} 的 LBA 范围重叠"
            )

    disk_aligned = device_size % target_sector_size == 0
    if disk_aligned:
        head_end = reserved_head_bytes(device_size, target_sector_size)
        tail_start = device_size - reserved_tail_bytes(
            device_size, target_sector_size
        )
    else:
        # The converter reports the disk-length problem separately.
        head_end = tail_start = None

    for p in partitions:
        if p.start_lba > p.end_lba:
            continue
        label = f"分区 {p.index}"
        start_byte = p.start_lba * source_sector_size
        end_byte_exclusive = (p.end_lba + 1) * source_sector_size
        if start_byte % target_sector_size:
            issues.alignment.append(
                f"{label} 起始字节 {start_byte} 未对齐到 {target_sector_size} "
                f"字节（余 {start_byte % target_sector_size}）"
            )
        if end_byte_exclusive % target_sector_size:
            issues.alignment.append(
                f"{label} 结束边界（不含）{end_byte_exclusive} 未对齐到 "
                f"{target_sector_size} 字节（余 "
                f"{end_byte_exclusive % target_sector_size}）"
            )
        if head_end is None:
            continue
        if start_byte < head_end:
            issues.overlap.append(
                f"{label} 起始于字节 {start_byte}，与 4 KiB 新主 GPT 保留区 "
                f"[0, {head_end}) 重叠"
            )
        if end_byte_exclusive > tail_start:
            issues.overlap.append(
                f"{label} 结束于字节 {end_byte_exclusive}（不含），与 4 KiB "
                f"新备 GPT 保留区 [{tail_start}, {device_size}) 重叠"
            )
    return partitions, issues


@dataclass(frozen=True)
class LayoutPartition:
    index: int
    raw: bytes
    start_lba: int
    end_lba: int

    @property
    def type_guid(self) -> bytes:
        return self.raw[0:16]

    @property
    def unique_guid(self) -> bytes:
        return self.raw[16:32]

    @property
    def attributes(self) -> int:
        return struct.unpack_from("<Q", self.raw, 48)[0]

    @property
    def name(self) -> str:
        return (
            self.raw[56:128]
            .decode("utf-16-le", errors="replace")
            .split("\x00", 1)[0]
        )


@dataclass
class GPTLayout:
    device_size: int
    sector_size: int
    last_lba: int
    first_usable_lba: int
    last_usable_lba: int
    disk_guid: bytes
    primary_entries_lba: int
    backup_entries_lba: int
    entries: bytes
    entries_crc: int
    primary_header: bytes
    backup_header: bytes
    mbr: bytes
    partitions: list[LayoutPartition]

    def expected_reserved_bytes(self) -> tuple[bytes, bytes]:
        """Return the exact bytes the produced image must hold in the reserved
        head and tail areas.  Both are small fixed-size buffers."""
        s = self.sector_size
        head = bytearray(self.first_usable_lba * s)
        head[0:512] = self.mbr
        head[s : s + len(self.primary_header)] = self.primary_header
        array_off = self.primary_entries_lba * s
        head[array_off : array_off + GPT_ENTRY_ARRAY_SIZE] = self.entries
        tail_len = self.device_size - self.backup_entries_lba * s
        tail = bytearray(tail_len)
        tail[0:GPT_ENTRY_ARRAY_SIZE] = self.entries
        header_off = self.last_lba * s - self.backup_entries_lba * s
        tail[header_off : header_off + len(self.backup_header)] = self.backup_header
        return bytes(head), bytes(tail)


def _build_header(
    *,
    my_lba: int,
    alternate_lba: int,
    first_usable: int,
    last_usable: int,
    disk_guid: bytes,
    entry_lba: int,
    entries_crc: int,
) -> bytes:
    base = _HEADER.pack(
        GPT_SIGNATURE,
        GPT_REVISION,
        GPT_HEADER_SIZE,
        0,
        0,
        my_lba,
        alternate_lba,
        first_usable,
        last_usable,
        disk_guid,
        entry_lba,
        GPT_ENTRY_COUNT,
        GPT_ENTRY_SIZE,
        entries_crc,
    )
    return _HEADER.pack(
        GPT_SIGNATURE,
        GPT_REVISION,
        GPT_HEADER_SIZE,
        crc32(base),
        0,
        my_lba,
        alternate_lba,
        first_usable,
        last_usable,
        disk_guid,
        entry_lba,
        GPT_ENTRY_COUNT,
        GPT_ENTRY_SIZE,
        entries_crc,
    )


def build_protective_mbr(device_size: int, sector_size: int) -> bytes:
    last_lba = device_size // sector_size - 1
    mbr = bytearray(512)
    off = 446
    mbr[off + 1 : off + 4] = b"\x00\x02\x00"  # CHS of LBA 1
    mbr[off + 4] = PROTECTIVE_MBR_TYPE
    mbr[off + 5 : off + 8] = b"\xff\xff\xff"
    struct.pack_into("<I", mbr, off + 8, 1)
    struct.pack_into("<I", mbr, off + 12, min(last_lba, 0xFFFFFFFF))
    mbr[510:512] = MBR_SIGNATURE
    return bytes(mbr)


def build_layout(
    device_size: int,
    sector_size: int,
    disk_guid: bytes,
    partition_specs: Iterable[tuple[int, bytes, int, int]],
) -> GPTLayout:
    """Build a canonical GPT layout.

    ``partition_specs`` yields ``(index, raw_128_byte_entry, start_lba,
    end_lba)``.  Only the LBA fields are patched; type GUID, unique GUID,
    attributes and name are preserved verbatim.
    """
    if sector_size not in (512, 4096):
        raise ValueError("only 512- and 4096-byte sectors are supported")
    last_lba, first_usable, last_usable, es = layout_for_sector_size(
        device_size, sector_size
    )
    if first_usable > last_usable:
        raise ValueError("disk is too small for a GPT")
    if len(disk_guid) != 16 or disk_guid == ZERO_GUID:
        raise ValueError("invalid disk GUID")

    array = bytearray(GPT_ENTRY_ARRAY_SIZE)
    parts: list[LayoutPartition] = []
    occupied: list[tuple[int, int, int]] = []
    for index, raw, start, end in partition_specs:
        if len(raw) != GPT_ENTRY_SIZE:
            raise ValueError("partition entry must be 128 bytes")
        if raw[0:16] == ZERO_GUID:
            raise ValueError("partition type GUID must not be zero")
        if not (1 <= index <= GPT_ENTRY_COUNT):
            raise ValueError("partition index out of range")
        if not (first_usable <= start <= end <= last_usable):
            raise ValueError(
                f"partition {index} range {start}..{end} is outside usable "
                f"range {first_usable}..{last_usable}"
            )
        off = (index - 1) * GPT_ENTRY_SIZE
        if any(array[off : off + GPT_ENTRY_SIZE]):
            raise ValueError("duplicate partition index")
        patched = patch_entry_lbas(raw, start, end)
        array[off : off + GPT_ENTRY_SIZE] = patched
        parts.append(
            LayoutPartition(
                index=index, raw=patched, start_lba=start, end_lba=end
            )
        )
        occupied.append((start, end, index))

    occupied.sort()
    for (s1, e1, i1), (s2, e2, i2) in zip(occupied, occupied[1:]):
        if s2 <= e1:
            raise ValueError(f"partitions {i1} and {i2} overlap")

    entries = bytes(array)
    entries_crc = crc32(entries)
    primary_entries_lba = 2
    backup_entries_lba = last_lba - es
    primary_header = _build_header(
        my_lba=1,
        alternate_lba=last_lba,
        first_usable=first_usable,
        last_usable=last_usable,
        disk_guid=disk_guid,
        entry_lba=primary_entries_lba,
        entries_crc=entries_crc,
    )
    backup_header = _build_header(
        my_lba=last_lba,
        alternate_lba=1,
        first_usable=first_usable,
        last_usable=last_usable,
        disk_guid=disk_guid,
        entry_lba=backup_entries_lba,
        entries_crc=entries_crc,
    )
    return GPTLayout(
        device_size=device_size,
        sector_size=sector_size,
        last_lba=last_lba,
        first_usable_lba=first_usable,
        last_usable_lba=last_usable,
        disk_guid=disk_guid,
        primary_entries_lba=primary_entries_lba,
        backup_entries_lba=backup_entries_lba,
        entries=entries,
        entries_crc=entries_crc,
        primary_header=primary_header,
        backup_header=backup_header,
        mbr=build_protective_mbr(device_size, sector_size),
        partitions=parts,
    )


def write_layout(fd: int, layout: GPTLayout) -> None:
    """Overwrite every reserved GPT area of an already-copied image."""
    s = layout.sector_size
    tail_start = layout.backup_entries_lba * s
    zero_range(fd, 0, layout.first_usable_lba * s)
    zero_range(fd, tail_start, layout.device_size - tail_start)
    pwrite_all(fd, layout.mbr, 0)
    pwrite_all(fd, layout.primary_header, s)
    pwrite_all(fd, layout.entries, layout.primary_entries_lba * s)
    pwrite_all(fd, layout.entries, layout.backup_entries_lba * s)
    pwrite_all(fd, layout.backup_header, layout.last_lba * s)


class _Hasher:
    __slots__ = ("index", "start", "end", "digest")

    def __init__(self, index: int, start: int, end: int) -> None:
        self.index = index
        self.start = start
        self.end = end
        self.digest = hashlib.sha256()


def copy_with_partition_hashes(
    src_fd: int,
    dst_fd: int,
    device_size: int,
    sector_size: int,
    partitions: Sequence[Partition],
    *,
    chunk_size: int = COPY_CHUNK,
    progress: Callable[[int], None] | None = None,
) -> dict[int, str]:
    """Copy the whole disk in bounded chunks while hashing each partition.

    Memory use is O(chunk_size + number of partitions), independent of the
    disk length.
    """
    hashers = [
        _Hasher(p.index, *p.byte_range(sector_size))
        for p in partitions
        if p.start_lba <= p.end_lba
    ]
    hashers.sort(key=lambda h: h.start)
    pending = list(hashers)
    active: list[_Hasher] = []
    offset = 0
    while offset < device_size:
        want = min(chunk_size, device_size - offset)
        data = pread_exact(src_fd, want, offset)
        pwrite_all(dst_fd, data, offset)
        chunk_end = offset + want
        while pending and pending[0].start < chunk_end:
            active.append(pending.pop(0))
        still: list[_Hasher] = []
        for h in active:
            lo = max(h.start, offset)
            hi = min(h.end, chunk_end)
            if lo < hi:
                h.digest.update(data[lo - offset : hi - offset])
            if h.end > chunk_end:
                still.append(h)
        active = still
        offset = chunk_end
        if progress is not None:
            progress(offset)
    if pending or active:
        raise RuntimeError("partition hashing did not cover all partitions")
    return {h.index: h.digest.hexdigest() for h in hashers}


@dataclass
class VerificationResult:
    checks: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons


def verify_layout_image(
    path: str,
    layout: GPTLayout,
    *,
    expected_hashes: dict[int, str] | None = None,
    hash_chunk: int = COPY_CHUNK,
) -> VerificationResult:
    """Re-open a produced image and verify it against ``layout``."""
    result = VerificationResult()
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        result.reasons.append(f"无法读取成品镜像：{exc}")
        return result

    if size != layout.device_size:
        result.reasons.append(
            f"成品长度 {size} 字节，与源盘 {layout.device_size} 字节不一致"
        )
    else:
        result.checks.append(f"成品长度与源盘一致（{size} 字节）")

    fd = os.open(path, os.O_RDONLY)
    try:
        mbr = inspect_mbr(fd, layout.device_size, layout.sector_size)
        if mbr.problems:
            result.reasons.extend(f"保护 MBR 复验失败：{p}" for p in mbr.problems)
        else:
            result.checks.append("保护 MBR 合法（单一 0xEE 项、覆盖整盘）")

        copies: dict[str, tuple[GPTHeader, bytes]] = {}
        for which, label in (("primary", "主"), ("backup", "备")):
            try:
                copies[which] = read_gpt_copy(
                    fd, layout.device_size, layout.sector_size, which
                )
                result.checks.append(f"{label} GPT 表头与分区项 CRC、位置复验通过")
            except GPTValidationError as exc:
                result.reasons.extend(
                    f"{label} GPT 复验失败：{r}" for r in exc.reasons
                )

        if len(copies) == 2:
            primary_header, primary_entries = copies["primary"]
            backup_header, backup_entries = copies["backup"]
            if primary_header.disk_guid != layout.disk_guid:
                result.reasons.append("成品主 GPT 磁盘 GUID 与源盘不一致")
            if backup_header.disk_guid != layout.disk_guid:
                result.reasons.append("成品备 GPT 磁盘 GUID 与源盘不一致")
            if primary_header.disk_guid != backup_header.disk_guid:
                result.reasons.append("成品主备 GPT 磁盘 GUID 不一致")
            else:
                result.checks.append(
                    f"磁盘 GUID 保留为 {guid_to_str(primary_header.disk_guid)}"
                )
            if primary_header.entries_crc != backup_header.entries_crc:
                result.reasons.append("成品主备 GPT 分区项 CRC 不一致")
            for which in ("primary", "backup"):
                header, entries = copies[which]
                if entries != layout.entries:
                    result.reasons.append(
                        f"成品{'主' if which == 'primary' else '备'} GPT "
                        "分区项与重建结果不一致"
                    )
                if header.first_usable_lba != layout.first_usable_lba:
                    result.reasons.append(
                        f"成品{'主' if which == 'primary' else '备'} GPT "
                        f"FirstUsableLBA={header.first_usable_lba}，"
                        f"应为 {layout.first_usable_lba}"
                    )
                if header.last_usable_lba != layout.last_usable_lba:
                    result.reasons.append(
                        f"成品{'主' if which == 'primary' else '备'} GPT "
                        f"LastUsableLBA={header.last_usable_lba}，"
                        f"应为 {layout.last_usable_lba}"
                    )
            if not any("分区项与重建结果" in r for r in result.reasons):
                result.checks.append("主备 GPT 分区项与重建结果逐字节一致")

            actual_parts = parse_entries(primary_entries)
            exp_map = {p.index: p for p in layout.partitions}
            act_map = {p.index: p for p in actual_parts}
            if set(exp_map) != set(act_map):
                result.reasons.append(
                    "分区项集合与重建结果不一致："
                    f"期望 {sorted(exp_map)}，实际 {sorted(act_map)}"
                )
            else:
                for index in sorted(exp_map):
                    e, a = exp_map[index], act_map[index]
                    if (e.start_lba, e.end_lba) != (a.start_lba, a.end_lba):
                        result.reasons.append(
                            f"分区 {index} 的 LBA 范围与重建结果不一致"
                        )
                    if e.raw != a.raw:
                        result.reasons.append(
                            f"分区 {index} 的分区项内容发生变化"
                        )
                if not any("分区项" in r for r in result.reasons):
                    result.checks.append(
                        "分区类型、唯一 GUID、属性、名称与范围全部保留"
                    )

            _, issues = check_partitions(
                primary_header,
                primary_entries,
                layout.device_size,
                layout.sector_size,
                layout.sector_size,
            )
            if issues.fatal:
                result.reasons.extend(
                    f"成品分区复验失败：{r}" for r in issues.fatal
                )
            else:
                result.checks.append("成品分区边界对齐且不与新 GPT 区域重叠")

            if expected_hashes is not None:
                hashes_ok = True
                for part in actual_parts:
                    expected = expected_hashes.get(part.index)
                    if expected is None:
                        result.reasons.append(
                            f"分区 {part.index} 缺少源内容哈希，无法复验"
                        )
                        hashes_ok = False
                        continue
                    start, end = part.byte_range(layout.sector_size)
                    digest = hashlib.sha256()
                    offset = start
                    while offset < end:
                        want = min(hash_chunk, end - offset)
                        try:
                            data = pread_exact(fd, want, offset)
                        except EOFError:
                            result.reasons.append(
                                f"成品分区 {part.index} 内容截断"
                                f"（字节 {offset}）"
                            )
                            hashes_ok = False
                            break
                        digest.update(data)
                        offset += want
                    if digest.hexdigest() != expected:
                        result.reasons.append(
                            f"分区 {part.index} 内容哈希与源盘不一致"
                        )
                        hashes_ok = False
                if hashes_ok:
                    result.checks.append(
                        "各分区内容 SHA-256 与源盘逐区一致"
                    )

        expected_head, expected_tail = layout.expected_reserved_bytes()
        try:
            actual_head = pread_exact(fd, len(expected_head), 0)
            tail_start = layout.device_size - len(expected_tail)
            actual_tail = pread_exact(fd, len(expected_tail), tail_start)
        except EOFError:
            result.reasons.append("成品 GPT 保留区读取失败")
        else:
            if actual_head != expected_head:
                result.reasons.append("成品主 GPT 保留区字节与重建结果不一致")
            elif actual_tail != expected_tail:
                result.reasons.append("成品备 GPT 保留区字节与重建结果不一致")
            else:
                result.checks.append(
                    "主备 GPT 与保护 MBR 保留区逐字节符合重建结果"
                )
    finally:
        os.close(fd)

    return result
