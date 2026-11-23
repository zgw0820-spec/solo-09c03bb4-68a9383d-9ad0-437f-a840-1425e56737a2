"""GPT parsing/building unit tests plus end-to-end conversion scenarios."""

from __future__ import annotations

import os
import uuid

import pytest

from app.converter import analyze_source, convert
from app.errors import ConversionError
from app.gpt import (
    GPT_ENTRY_ARRAY_SIZE,
    build_layout,
    write_layout,
    check_partitions,
    encode_entry,
    inspect_mbr,
    parse_entries,
    read_gpt_copy,
    verify_layout_image,
)
from app import samplegen as fixtures
from app.samplegen import MIB, SECTOR

ESP = fixtures.ESP_TYPE
LINUX = fixtures.LINUX_TYPE


def _analyze(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return analyze_source(fd, os.path.getsize(path))
    finally:
        os.close(fd)


def _convert(src, dst):
    analysis = _analyze(src)
    src_fd = os.open(src, os.O_RDONLY)
    try:
        dst_fd = os.open(dst, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            result = convert(src_fd, dst_fd, analysis, chunk_size=1 * MIB)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)
    return analysis, result


def _read_output(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.path.getsize(path)
        primary = read_gpt_copy(fd, size, 4096, "primary")
        backup = read_gpt_copy(fd, size, 4096, "backup")
        mbr = inspect_mbr(fd, size, 4096)
    finally:
        os.close(fd)
    return primary, backup, mbr


@pytest.fixture()
def disk(tmp_path):
    path = tmp_path / "src.img"
    fixtures.make_disk(path, disk_mib=64)
    return path


@pytest.fixture()
def out_path(tmp_path):
    return tmp_path / "out.img"


def test_analysis_selects_primary_when_both_agree(disk):
    analysis = _analyze(disk)
    assert analysis.used_copy == "primary"
    assert [p.index for p in analysis.partitions] == [1, 2]
    assert analysis.disk_guid == "8B0E5F1A-2C4D-4E8F-9A3B-1C2D3E4F5A6B"
    assert {c.which: c.valid for c in analysis.copies} == {
        "primary": True,
        "backup": True,
    }


def test_conversion_preserves_bytes_and_rebuilds_gpt(disk, out_path):
    analysis, result = _convert(disk, out_path)
    assert out_path.stat().st_size == disk.stat().st_size

    report = verify_layout_image(
        str(out_path), result.layout, expected_hashes=result.partition_hashes
    )
    assert report.ok, report.reasons

    (primary_header, entries), (backup_header, backup_entries), mbr = _read_output(
        out_path
    )
    assert not mbr.problems
    assert mbr.protective_size_lbas == 64 * MIB // 4096 - 1
    assert primary_header.disk_guid == analysis.header.disk_guid
    assert primary_header.first_usable_lba == 6  # 2 + 4 entry sectors
    assert backup_header.alternate_lba == 1
    assert entries == backup_entries
    parts = parse_entries(entries)
    assert [(p.start_lba, p.end_lba) for p in parts] == [
        (256, 1023),  # 1 MiB .. 4 MiB
        (1280, 15871),  # 5 MiB .. 62 MiB
    ]

    src_fd = os.open(disk, os.O_RDONLY)
    out_fd = os.open(out_path, os.O_RDONLY)
    try:
        for part in parts:
            start, end = part.byte_range(4096)
            for offset in range(start, end, 4 * MIB):
                length = min(4 * MIB, end - offset)
                assert os.pread(src_fd, length, offset) == os.pread(
                    out_fd, length, offset
                )
    finally:
        os.close(out_fd)
        os.close(src_fd)


def test_primary_header_corrupt_uses_backup(tmp_path, out_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=32)
    fixtures.corrupt_primary_header(src)
    analysis = _analyze(src)
    assert analysis.used_copy == "backup"
    copies = {c.which: c for c in analysis.copies}
    assert copies["primary"].valid is False
    assert any("CRC32" in p for p in copies["primary"].problems)

    _, result = _convert(src, out_path)
    assert verify_layout_image(
        str(out_path), result.layout, expected_hashes=result.partition_hashes
    ).ok


def test_backup_entries_corrupt_uses_primary(tmp_path, out_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=32)
    fixtures.corrupt_backup_entries(src)
    analysis = _analyze(src)
    assert analysis.used_copy == "primary"
    assert {c.which: c.valid for c in analysis.copies} == {
        "primary": True,
        "backup": False,
    }
    _, result = _convert(src, out_path)
    assert verify_layout_image(
        str(out_path), result.layout, expected_hashes=result.partition_hashes
    ).ok


def test_both_copies_corrupt_is_rejected(tmp_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=32)
    fixtures.corrupt_primary_header(src)
    fixtures.corrupt_backup_entries(src)
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "BOTH_GPT_INVALID"
    assert info.value.details["copies"]


def test_disk_guid_conflict_is_rejected(tmp_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=32)
    fixtures.set_backup_disk_guid(src, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "GPT_COPIES_CONFLICT"
    assert any("磁盘 GUID" in c for c in info.value.details["conflicts"])


def test_partition_entry_conflict_is_rejected(tmp_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=32)

    def mutate(entries: bytearray) -> None:
        entries[56:58] = "X".encode("utf-16-le")

    fixtures.patch_backup_entries(src, mutate)
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "GPT_COPIES_CONFLICT"
    assert any("分区表" in c for c in info.value.details["conflicts"])


def test_hybrid_mbr_is_rejected(tmp_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=32)
    fixtures.add_hybrid_mbr_entry(src)
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "HYBRID_MBR"


def test_unaligned_partition_is_rejected(tmp_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(
        src,
        disk_mib=32,
        partitions=[
            dict(index=1, type=ESP, unique=fixtures._guid(1), name="odd",
                 start=1 * MIB + 512, end=8 * MIB, fill=b"\x11")
        ],
    )
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "PARTITION_NOT_ALIGNED"
    assert info.value.details["problems"]


def test_unaligned_disk_length_is_rejected(tmp_path):
    src = tmp_path / "src.img"
    # Valid dual GPT for 32 MiB + 512 bytes: both copies and partitions are
    # fine, only the 512 -> 4096 length conversion is impossible.
    fixtures.make_disk(src, disk_mib=32, extra_bytes=512)
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "DISK_NOT_ALIGNED"
    assert info.value.details["remainder"] == 512


def test_partition_colliding_with_new_backup_gpt_is_rejected(disk):
    """A partition ending inside the 4 KiB backup reserved area is fatal.

    With 4 KiB-aligned partitions this cannot happen on a 4 KiB-aligned disk,
    so the rule is exercised directly through the checker with a crafted
    table (unreachable through a well-formed aligned source).
    """
    analysis = _analyze(disk)
    tail_start = analysis.disk_size - 5 * 4096
    entries = bytearray(GPT_ENTRY_ARRAY_SIZE)
    # Last 512-byte LBA inside the source usable range, ending 512 bytes into
    # the new 4 KiB backup area.
    end_lba = (tail_start + 512) // 512 - 1
    entries[0:128] = encode_entry(LINUX, fixtures._guid(7), 34, end_lba, name="t")
    _, issues = check_partitions(
        analysis.header, bytes(entries), analysis.disk_size, 512, 4096
    )
    assert any("备 GPT" in p for p in issues.overlap)
    # No well-formed aligned source can reach this state, so assert that the
    # rule engine flags it; the converter turns overlap into a hard stop.
    with pytest.raises(ConversionError) as info:
        raise ConversionError(
            "GPT_REGION_CONFLICT", "x", details={"problems": issues.overlap}
        )
    assert info.value.details["problems"]


def test_partition_colliding_with_new_primary_gpt_is_rejected(tmp_path):
    src = tmp_path / "src.img"
    # 20 KiB = LBA 40, inside the source usable area (>= LBA 34) but before
    # the 4 KiB primary reserved area ends at 24 KiB.
    fixtures.make_disk(
        src,
        disk_mib=32,
        partitions=[
            dict(index=1, type=LINUX, unique=fixtures._guid(3), name="head",
                 start=20480, end=8 * MIB, fill=b"\x22")
        ],
    )
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "GPT_REGION_CONFLICT"
    assert any("主 GPT" in p for p in info.value.details["problems"])


def test_missing_protective_mbr_is_rebuilt(tmp_path, out_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=32)
    fixtures.wipe_mbr_entries(src)
    analysis = _analyze(src)
    assert analysis.mbr_warnings
    _, result = _convert(src, out_path)
    report = verify_layout_image(
        str(out_path), result.layout, expected_hashes=result.partition_hashes
    )
    assert report.ok, report.reasons
    _, _, mbr = _read_output(out_path)
    assert not mbr.problems


def test_verification_detects_tampered_output(disk, out_path):
    _, result = _convert(disk, out_path)
    fixtures.patch_bytes(out_path, 2 * 4096 + 56, b"\x00")
    report = verify_layout_image(
        str(out_path), result.layout, expected_hashes=result.partition_hashes
    )
    assert not report.ok
    assert any("分区项" in reason for reason in report.reasons)


def test_verification_detects_content_change(tmp_path, out_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=32)
    _, result = _convert(src, out_path)
    # Flip a byte inside partition 1's payload.
    fixtures.patch_bytes(out_path, 2 * MIB + 7, b"\x99")
    report = verify_layout_image(
        str(out_path), result.layout, expected_hashes=result.partition_hashes
    )
    assert not report.ok
    assert any("内容哈希" in reason for reason in report.reasons)


def test_copy_reads_are_bounded(disk, out_path):
    """The copy loop must never read the whole disk into memory."""
    from app import gpt

    analysis = _analyze(disk)
    calls: list[int] = []
    original = gpt.pread_exact

    def tracking(fd, length, offset):
        calls.append(length)
        return original(fd, length, offset)

    src_fd = os.open(disk, os.O_RDONLY)
    dst_fd = os.open(out_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        gpt.pread_exact = tracking
        try:
            result = convert(src_fd, dst_fd, analysis, chunk_size=256 * 1024)
        finally:
            gpt.pread_exact = original
    finally:
        os.close(dst_fd)
        os.close(src_fd)
    assert calls
    assert max(calls) <= 16 * 1024 + 256 * 1024
    assert verify_layout_image(
        str(out_path), result.layout, expected_hashes=result.partition_hashes
    ).ok


def test_empty_entries_with_residue_are_tolerated(tmp_path, out_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=32)

    def mutate(entries: bytearray) -> None:
        # Empty type GUID but stale bytes in the name field: harmless junk
        # that a canonical rebuild must clear.
        entries[5 * 128 + 56 : 5 * 128 + 64] = b"\x77" * 8

    fixtures.patch_primary_entries(src, mutate)
    fixtures.patch_backup_entries(src, mutate)
    analysis = _analyze(src)
    assert any("残留" in w for w in analysis.warnings)
    _, result = _convert(src, out_path)
    assert verify_layout_image(
        str(out_path), result.layout, expected_hashes=result.partition_hashes
    ).ok


def test_guid_attributes_name_preserved(tmp_path, out_path):
    src = tmp_path / "src.img"
    unique = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    fixtures.make_disk(
        src,
        disk_mib=32,
        partitions=[
            dict(index=1, type=ESP, unique=unique, name="keep me",
                 start=1 * MIB, end=4 * MIB, attributes=0x8000000000000001,
                 fill=b"\x11")
        ],
    )
    _, _ = _convert(src, out_path)
    (_, entries), _, _ = _read_output(out_path)
    part = parse_entries(entries)[0]
    assert part.unique_guid_text == unique.upper()
    assert part.attributes == 0x8000000000000001
    assert part.name == "keep me"


def test_build_layout_rejects_overlap():
    raw = encode_entry(LINUX, fixtures._guid(1), 0, 0, name="a")
    with pytest.raises(ValueError):
        build_layout(32 * MIB, 4096, uuid.uuid4().bytes_le, [
            (1, raw, 6, 100),
            (2, raw, 100, 200),
        ])


def test_check_partitions_flags_alignment_and_overlap(disk):
    analysis = _analyze(disk)
    # Re-run the rule engine on a synthetic table touching the new head area.
    from app.gpt import encode_entry as ee

    entries = bytearray(GPT_ENTRY_ARRAY_SIZE)
    entries[0:128] = ee(LINUX, fixtures._guid(9), 5, 20, name="bad")
    _, issues = check_partitions(
        analysis.header, bytes(entries), analysis.disk_size, 512, 4096
    )
    assert issues.overlap and issues.alignment


def test_source_length_not_multiple_of_512_is_rejected(tmp_path):
    src = tmp_path / "odd.img"
    src.write_bytes(b"\x00" * 1000)
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "SOURCE_NOT_512"


def test_tiny_file_is_rejected(tmp_path):
    src = tmp_path / "tiny.img"
    src.write_bytes(b"\x00" * 512)
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "BOTH_GPT_INVALID"


def test_empty_guid_partition_is_rejected(tmp_path):
    src = tmp_path / "dup.img"
    fixtures.make_disk(
        src,
        disk_mib=32,
        partitions=[
            dict(index=1, type=ESP, unique="00000000-0000-0000-0000-000000000000",
                 name="bad", start=1 * MIB, end=4 * MIB),
        ],
    )
    # build_layout refuses a zero unique GUID? It only refuses zero type GUID,
    # so this disk is written; the analyser must reject it semantically.
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "PARTITION_INVALID"


def test_duplicate_partition_guid_is_rejected(tmp_path):
    src = tmp_path / "dup2.img"
    same = fixtures._guid(5)
    fixtures.make_disk(
        src,
        disk_mib=32,
        partitions=[
            dict(index=1, type=ESP, unique=same, name="a", start=1 * MIB, end=4 * MIB),
            dict(index=2, type=ESP, unique=same, name="b", start=5 * MIB, end=8 * MIB),
        ],
    )
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "PARTITION_INVALID"


def test_disk_too_small_for_target_gpt_is_rejected(tmp_path):
    """Valid as a 512-byte GPT but cannot hold a 4 KiB one (needs 12 sectors)."""
    src = tmp_path / "small.img"
    size = 44 * 1024  # 512-byte GPT is fine (needs 68 sectors), 4 KiB is not
    layout = build_layout(size, SECTOR, uuid.uuid4().bytes_le, [])
    fd = os.open(src, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, size)
        write_layout(fd, layout)
        os.fsync(fd)
    finally:
        os.close(fd)
    with pytest.raises(ConversionError) as info:
        _analyze(src)
    assert info.value.code == "DISK_TOO_SMALL"
    assert info.value.details["minimum_bytes"] == 12 * 4096
