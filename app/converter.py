"""Disk analysis and the streaming 512 -> 4096 byte conversion pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from .errors import ConversionError, VerificationError
from .gpt import (
    COPY_CHUNK,
    GPTHeader,
    GPTLayout,
    GPTValidationError,
    Partition,
    PartitionIssues,
    build_layout,
    check_partitions,
    copy_with_partition_hashes,
    guid_to_str,
    layout_for_sector_size,
    inspect_mbr,
    parse_entries,
    read_gpt_copy,
    reserved_head_bytes,
    reserved_tail_bytes,
    verify_layout_image,
    write_layout,
)

SOURCE_SECTOR_SIZE = 512
TARGET_SECTOR_SIZE = 4096


@dataclass
class GPTCopyReport:
    which: str
    valid: bool
    used: bool
    problems: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


@dataclass
class SourceAnalysis:
    disk_size: int
    sector_size: int
    disk_guid: str
    header: GPTHeader
    entries: bytes
    partitions: list[Partition]
    issues: PartitionIssues
    warnings: list[str]
    copies: list[GPTCopyReport]
    mbr_warnings: list[str]

    @property
    def used_copy(self) -> str:
        for copy in self.copies:
            if copy.used:
                return copy.which
        raise AssertionError("no GPT copy selected")


def analyze_source(fd: int, disk_size: int) -> SourceAnalysis:
    """Validate both GPT copies and select exactly one source of truth.

    Raises :class:`ConversionError` when the input cannot be converted.
    """
    if disk_size <= 0:
        raise ConversionError("EMPTY_FILE", "上传的文件是空的。")
    if disk_size % SOURCE_SECTOR_SIZE:
        raise ConversionError(
            "SOURCE_NOT_512",
            f"盘长 {disk_size} 字节不是 512 字节逻辑扇区的整数倍"
            f"（余 {disk_size % SOURCE_SECTOR_SIZE}），不是合法的 512e/512n "
            "GPT 磁盘。",
        )

    warnings: list[str] = []
    try:
        mbr = inspect_mbr(fd, disk_size, SOURCE_SECTOR_SIZE)
    except EOFError:
        raise ConversionError("TOO_SHORT", "文件太小，容纳不下保护 MBR。") from None

    if mbr.hybrid:
        raise ConversionError(
            "HYBRID_MBR",
            "保护 MBR 含非 0xEE 项（混合 MBR）或含多个 0xEE 项，不接受混合 GPT。",
            details={
                "non_ee_types": [f"0x{t:02x}" for t in mbr.non_ee_types],
                "ee_count": mbr.ee_count,
            },
        )
    warnings.extend(mbr.warnings)

    copies: list[GPTCopyReport] = []
    parsed: dict[str, tuple[GPTHeader, bytes]] = {}
    for which, label in (("primary", "主 GPT"), ("backup", "备 GPT")):
        try:
            header, entries = read_gpt_copy(
                fd, disk_size, SOURCE_SECTOR_SIZE, which
            )
        except GPTValidationError as exc:
            copies.append(
                GPTCopyReport(
                    which=which,
                    valid=False,
                    used=False,
                    problems=exc.reasons,
                )
            )
            continue
        parsed[which] = (header, entries)
        copies.append(
            GPTCopyReport(
                which=which,
                valid=True,
                used=False,
                detail={
                    "label": label,
                    "my_lba": header.my_lba,
                    "alternate_lba": header.alternate_lba,
                    "first_usable_lba": header.first_usable_lba,
                    "last_usable_lba": header.last_usable_lba,
                    "entries_lba": header.partition_entry_lba,
                    "disk_guid": guid_to_str(header.disk_guid),
                    "header_crc": f"{header.header_crc:08x}",
                    "entries_crc": f"{header.entries_crc:08x}",
                    "partition_count": len(parse_entries(entries)),
                },
            )
        )

    valid = [c for c in copies if c.valid]
    if not valid:
        raise ConversionError(
            "BOTH_GPT_INVALID",
            "主备 GPT 均校验失败，无法安全推断原始分区布局，拒绝转换。",
            details={"copies": [c.problems for c in copies]},
        )

    if len(valid) == 2:
        p_head, p_entries = parsed["primary"]
        b_head, b_entries = parsed["backup"]
        conflicts: list[str] = []
        if p_head.disk_guid != b_head.disk_guid:
            conflicts.append(
                "主备 GPT 磁盘 GUID 不同："
                f"{guid_to_str(p_head.disk_guid)} vs {guid_to_str(b_head.disk_guid)}"
            )
        if p_entries != b_entries:
            conflicts.append("主备 GPT 的 128 项分区表内容不一致")
        if p_head.first_usable_lba != b_head.first_usable_lba:
            conflicts.append(
                "主备 GPT FirstUsableLBA 不同："
                f"{p_head.first_usable_lba} vs {b_head.first_usable_lba}"
            )
        if p_head.last_usable_lba != b_head.last_usable_lba:
            conflicts.append(
                "主备 GPT LastUsableLBA 不同："
                f"{p_head.last_usable_lba} vs {b_head.last_usable_lba}"
            )
        if conflicts:
            raise ConversionError(
                "GPT_COPIES_CONFLICT",
                "主备 GPT 均有效但相互矛盾（磁盘标识或分区项不一致），"
                "拒绝转换，以免丢失或错置分区。",
                details={"conflicts": conflicts},
            )
        which = "primary"
        warnings.append("主备 GPT 均有效且内容一致，采用主 GPT。")
    else:
        which = valid[0].which
        broken = "backup" if which == "primary" else "primary"
        warnings.append(
            f"{'备' if broken == 'backup' else '主'} GPT 校验失败，"
            f"仅使用{'主' if which == 'primary' else '备'} GPT 重建。"
        )

    for copy in copies:
        copy.used = copy.which == which
    header, entries = parsed[which]

    partitions, issues = check_partitions(
        header,
        entries,
        disk_size,
        SOURCE_SECTOR_SIZE,
        TARGET_SECTOR_SIZE,
    )

    try:
        target_last_lba, target_first_usable, target_last_usable, _ = (
            layout_for_sector_size(disk_size, TARGET_SECTOR_SIZE)
        )
    except ValueError:
        target_first_usable = target_last_usable = -1
    if target_first_usable > target_last_usable:
        raise ConversionError(
            "DISK_TOO_SMALL",
            f"盘长 {disk_size} 字节不足以容纳 4 KiB 逻辑扇区的 GPT"
            f"（至少需要 {12 * TARGET_SECTOR_SIZE} 字节），无法生成目标镜像。",
            details={
                "disk_size": disk_size,
                "minimum_bytes": 12 * TARGET_SECTOR_SIZE,
            },
        )
    if disk_size % TARGET_SECTOR_SIZE:
        raise ConversionError(
            "DISK_NOT_ALIGNED",
            f"盘长 {disk_size} 字节不是 4096 字节的整数倍"
            f"（余 {disk_size % TARGET_SECTOR_SIZE}），无法生成等长的 4 KiB "
            "逻辑扇区镜像。",
            details={
                "disk_size": disk_size,
                "target_sector_size": TARGET_SECTOR_SIZE,
                "remainder": disk_size % TARGET_SECTOR_SIZE,
            },
        )
    if issues.invalid:
        raise ConversionError(
            "PARTITION_INVALID",
            "所选 GPT 的分区项内部不一致（超出 GPT 可用区、分区互相重叠或唯一 "
            "GUID 重复），拒绝转换。",
            details={"problems": issues.invalid},
        )
    if issues.overlap or issues.alignment:
        # Both classes are reported together: on a 512-byte source a partition
        # reaching into the new tail area is almost always also unaligned, and
        # the user needs to see every affected boundary, not just the first.
        details = {
            "problems": issues.overlap + issues.alignment,
            "overlap": issues.overlap,
            "alignment": issues.alignment,
            "target_sector_size": TARGET_SECTOR_SIZE,
            "new_reserved_head_bytes": reserved_head_bytes(
                disk_size, TARGET_SECTOR_SIZE
            ),
            "new_reserved_tail_bytes": reserved_tail_bytes(
                disk_size, TARGET_SECTOR_SIZE
            ),
        }
        if issues.overlap:
            raise ConversionError(
                "GPT_REGION_CONFLICT",
                "按 4 KiB 扇区重建后，新的保护 MBR / 主备 GPT 保留区会与现有分区"
                "重叠，拒绝转换。",
                details=details,
            )
        raise ConversionError(
            "PARTITION_NOT_ALIGNED",
            "有分区边界未对齐到 4096 字节，转换后分区将无法按 4 KiB 扇区寻址。",
            details=details,
        )
    return SourceAnalysis(
        disk_size=disk_size,
        sector_size=SOURCE_SECTOR_SIZE,
        disk_guid=guid_to_str(header.disk_guid),
        header=header,
        entries=entries,
        partitions=partitions,
        issues=issues,
        warnings=warnings + issues.warnings,
        copies=copies,
        mbr_warnings=mbr.warnings,
    )


@dataclass
class ConversionResult:
    analysis: SourceAnalysis
    layout: GPTLayout
    checks: list[str]
    partition_hashes: dict[int, str]


def plan_layout(analysis: SourceAnalysis) -> GPTLayout:
    """Translate byte ranges to 4 KiB LBAs and rebuild the table."""
    specs = []
    for p in analysis.partitions:
        start_byte, end_byte_exclusive = p.byte_range(SOURCE_SECTOR_SIZE)
        specs.append(
            (
                p.index,
                p.raw,
                start_byte // TARGET_SECTOR_SIZE,
                end_byte_exclusive // TARGET_SECTOR_SIZE - 1,
            )
        )
    return build_layout(
        analysis.disk_size,
        TARGET_SECTOR_SIZE,
        analysis.header.disk_guid,
        specs,
    )


def convert(
    src_fd: int,
    dst_fd: int,
    analysis: SourceAnalysis,
    *,
    chunk_size: int = COPY_CHUNK,
    progress: Callable[[int], None] | None = None,
) -> ConversionResult:
    """Copy the disk, rebuild GPT for 4 KiB sectors, then verify the result.

    The source hashes are computed during the copy, so the final verification
    only needs one sequential pass over the produced image.  The caller owns
    the file descriptors and is responsible for cleanup on failure.
    """
    layout = plan_layout(analysis)
    os.ftruncate(dst_fd, analysis.disk_size)
    hashes = copy_with_partition_hashes(
        src_fd,
        dst_fd,
        analysis.disk_size,
        SOURCE_SECTOR_SIZE,
        analysis.partitions,
        chunk_size=chunk_size,
        progress=progress,
    )
    write_layout(dst_fd, layout)
    os.fsync(dst_fd)
    return ConversionResult(
        analysis=analysis,
        layout=layout,
        checks=[],
        partition_hashes=hashes,
    )


def verify_output(path: str, result: ConversionResult) -> list[str]:
    """Mandatory re-open/verify pass; raises if any check fails."""
    report = verify_layout_image(
        path, result.layout, expected_hashes=result.partition_hashes
    )
    result.checks = report.checks
    if not report.ok:
        raise VerificationError(report.reasons)
    return report.checks
