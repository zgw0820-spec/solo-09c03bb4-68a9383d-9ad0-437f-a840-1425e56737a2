"""Block-checksum manifest: creation, strict validation and atomic storage.

A manifest describes the *finished* image as a sequence of fixed-size blocks
(default 4 MiB), each with its own SHA-256.  Because every byte of the image
belongs to exactly one block, the block list covers the protective MBR/GPT,
the partition data, all unallocated space and the short final block when the
image length is not a multiple of the block size.

The same validator is used by the server after writing the file (so a broken
manifest can never be published) and by the offline verifier page in the
browser.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .gpt import BlockDigest, GPTLayout

MANIFEST_FORMAT = "gpt-4kn-block-manifest"
MANIFEST_VERSION = 1
DEFAULT_BLOCK_BYTES = 4 * 1024 * 1024
HEX_DIGITS = frozenset("0123456789abcdef")
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_BLOCK_BYTES = 64 * 1024 * 1024
MAX_BLOCK_COUNT = 10_000_000
REGION_LABELS = frozenset(
    {"gpt-head", "gpt-tail", "partition", "unallocated"}
)


class ManifestError(Exception):
    """The manifest is malformed or does not fully describe the image."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass
class ManifestBlock:
    index: int
    offset: int
    length: int
    sha256: str


@dataclass
class ManifestRegion:
    kind: str
    start: int
    end: int
    label: str = ""


@dataclass
class Manifest:
    format: str
    version: int
    image_name: str
    image_size: int
    block_bytes: int
    block_count: int
    blocks: list[ManifestBlock]
    regions: list[ManifestRegion] = field(default_factory=list)
    source_name: str = ""
    disk_guid: str = ""
    algorithm: str = "sha256"

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "version": self.version,
            "algorithm": self.algorithm,
            "image_name": self.image_name,
            "image_size": self.image_size,
            "block_bytes": self.block_bytes,
            "block_count": self.block_count,
            "source_name": self.source_name,
            "disk_guid": self.disk_guid,
            "blocks": [
                {
                    "index": b.index,
                    "offset": b.offset,
                    "length": b.length,
                    "sha256": b.sha256,
                }
                for b in self.blocks
            ],
            "regions": [
                {"kind": r.kind, "start": r.start, "end": r.end, "label": r.label}
                for r in self.regions
            ],
        }


def build_regions(layout: GPTLayout) -> list[ManifestRegion]:
    """Describe how the image is partitioned for audit purposes.

    The regions tile ``[0, image_size)`` with no gaps and no overlaps:
    protective MBR + primary GPT, every partition, every unallocated gap and
    the backup GPT at the tail.
    """
    size = layout.device_size
    sector = layout.sector_size
    head_end = layout.first_usable_lba * sector
    tail_start = layout.backup_entries_lba * sector
    spans: list[tuple[int, int, str, str]] = [(0, min(head_end, size), "gpt-head", "保护 MBR + 主 GPT")]
    for part in sorted(layout.partitions, key=lambda p: p.start_lba):
        spans.append(
            (
                part.start_lba * sector,
                min((part.end_lba + 1) * sector, size),
                "partition",
                f"分区 {part.index}",
            )
        )
    spans.append(
        (tail_start, size, "gpt-tail", "备 GPT")
    )
    spans.sort(key=lambda item: (item[0], item[1]))

    regions: list[ManifestRegion] = []
    cursor = 0
    for start, end, kind, label in spans:
        if start < cursor:
            # Overlap cannot happen for a verified layout; keep the manifest
            # creation conservative and let validation catch it if it does.
            continue
        if start > cursor:
            regions.append(
                ManifestRegion("unallocated", cursor, start, "未分配区")
            )
        regions.append(ManifestRegion(kind, start, end, label))
        cursor = end
    if cursor < size:
        regions.append(ManifestRegion("unallocated", cursor, size, "未分配区"))
    return regions


def build_manifest(
    *,
    image_name: str,
    layout: GPTLayout,
    blocks: Iterable[BlockDigest],
    block_bytes: int = DEFAULT_BLOCK_BYTES,
    source_name: str = "",
) -> Manifest:
    block_list = [
        ManifestBlock(
            index=b.index, offset=b.offset, length=b.length, sha256=b.sha256
        )
        for b in blocks
    ]
    expected_count = (
        (layout.device_size + block_bytes - 1) // block_bytes
        if layout.device_size
        else 0
    )
    manifest = Manifest(
        format=MANIFEST_FORMAT,
        version=MANIFEST_VERSION,
        image_name=image_name,
        image_size=layout.device_size,
        block_bytes=block_bytes,
        block_count=expected_count,
        blocks=block_list,
        regions=build_regions(layout),
        source_name=source_name,
        disk_guid=_guid_text(layout.disk_guid),
    )
    # Refuse to hand out a manifest that our own validator rejects.
    validate_manifest(manifest.to_dict())
    return manifest


def _guid_text(raw: bytes) -> str:
    try:
        import uuid

        return str(uuid.UUID(bytes_le=raw)).upper()
    except (ValueError, TypeError):
        return ""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in HEX_DIGITS for ch in value)
    )


def _require_int(
    doc: dict[str, Any], key: str, problems: list[str], *, minimum: int = 0
) -> int | None:
    value = doc.get(key)
    if not _is_int(value):
        problems.append(f"清单字段 {key} 缺失或不是整数")
        return None
    if value < minimum:
        problems.append(f"清单字段 {key} 小于 {minimum}")
        return None
    return value


def validate_manifest(doc: Any) -> Manifest:
    """Strictly validate a parsed manifest document.

    Raises :class:`ManifestError` with every problem found.  A manifest is
    only accepted when its block list covers ``[0, image_size)`` contiguously,
    i.e. there is no room for an unverified part of the image.
    """
    problems: list[str] = []
    if not isinstance(doc, dict):
        raise ManifestError(["清单根节点必须是 JSON 对象"])

    if doc.get("format") != MANIFEST_FORMAT:
        problems.append(f"清单格式标识必须是 {MANIFEST_FORMAT}")
    version = _require_int(doc, "version", problems, minimum=1)
    if version is not None and version != MANIFEST_VERSION:
        problems.append(f"清单版本 {version} 不受支持")
    algorithm = doc.get("algorithm", "sha256")
    if algorithm != "sha256":
        problems.append("清单算法必须是 sha256")

    image_size = _require_int(doc, "image_size", problems, minimum=1)
    block_bytes = _require_int(doc, "block_bytes", problems, minimum=1)
    block_count = _require_int(doc, "block_count", problems, minimum=0)
    if block_bytes is not None and block_bytes > MAX_BLOCK_BYTES:
        problems.append(f"块大小 {block_bytes} 超过 {MAX_BLOCK_BYTES} 字节上限")
    if block_count is not None and block_count > MAX_BLOCK_COUNT:
        problems.append(f"块数量 {block_count} 超过 {MAX_BLOCK_COUNT} 上限")
    image_name = doc.get("image_name")
    if not isinstance(image_name, str) or not image_name.strip():
        problems.append("清单缺少镜像文件名 image_name")

    blocks_doc = doc.get("blocks")
    if not isinstance(blocks_doc, list):
        problems.append("清单缺少 blocks 数组")
        blocks_doc = []

    blocks: list[ManifestBlock] = []
    if problems:
        # Structural fields are unusable; report them all at once.
        raise ManifestError(problems)

    assert image_size is not None and block_bytes is not None
    assert block_count is not None
    expected_count = (image_size + block_bytes - 1) // block_bytes
    if block_count != expected_count:
        problems.append(
            f"清单声明 {block_count} 块，按镜像长度 {image_size} 与块大小 "
            f"{block_bytes} 应为 {expected_count} 块"
        )
    if len(blocks_doc) != block_count:
        problems.append(
            f"清单 blocks 实际 {len(blocks_doc)} 项，与声明的 {block_count} 块不符"
        )

    seen_indexes: set[int] = set()
    cursor = 0
    for position, entry in enumerate(blocks_doc):
        if not isinstance(entry, dict):
            problems.append(f"第 {position + 1} 个块不是对象")
            continue
        index = entry.get("index")
        offset = entry.get("offset")
        length = entry.get("length")
        digest = entry.get("sha256")
        if not _is_int(index) or index < 0:
            problems.append(f"第 {position + 1} 个块 index 非法")
            continue
        if index in seen_indexes:
            problems.append(f"块索引 {index} 重复")
        seen_indexes.add(index)
        if not _is_int(offset) or not _is_int(length) or length <= 0:
            problems.append(f"块 {index} 的 offset/length 非法")
            continue
        if offset != cursor:
            problems.append(
                f"块 {index} 起始字节 {offset} 与上一块结束字节 {cursor} 不连续"
            )
        expected_len = min(block_bytes, image_size - offset)
        if length != expected_len:
            problems.append(
                f"块 {index} 长度 {length} 字节，按位置应为 {expected_len} 字节"
            )
        if not _is_sha256(digest):
            problems.append(f"块 {index} 的 sha256 不是 64 位小写十六进制")
        else:
            blocks.append(
                ManifestBlock(
                    index=index, offset=offset, length=length, sha256=digest
                )
            )
        cursor = offset + length

    if not problems and cursor != image_size:
        problems.append(
            f"清单块只覆盖到字节 {cursor}，镜像共 {image_size} 字节，清单不完整"
        )

    regions: list[ManifestRegion] = []
    regions_doc = doc.get("regions", [])
    if regions_doc and not isinstance(regions_doc, list):
        problems.append("regions 必须是数组")
    elif isinstance(regions_doc, list):
        region_cursor = 0
        for position, entry in enumerate(regions_doc):
            if not isinstance(entry, dict):
                problems.append(f"第 {position + 1} 个区域不是对象")
                continue
            kind = entry.get("kind")
            start = entry.get("start")
            end = entry.get("end")
            label = entry.get("label", "")
            if kind not in REGION_LABELS:
                problems.append(f"第 {position + 1} 个区域类型 {kind!r} 未知")
            if not _is_int(start) or not _is_int(end) or end <= start:
                problems.append(f"第 {position + 1} 个区域范围非法")
                continue
            if start != region_cursor:
                problems.append(
                    f"区域 [{start}, {end}) 与上一区域结束字节 {region_cursor} 不连续"
                )
            if not isinstance(label, str):
                problems.append(f"第 {position + 1} 个区域 label 非法")
            regions.append(ManifestRegion(kind, start, end, str(label)))
            region_cursor = end
        if not problems and region_cursor != image_size:
            problems.append(
                f"区域只覆盖到字节 {region_cursor}，镜像共 {image_size} 字节"
            )

    if problems:
        raise ManifestError(problems)

    return Manifest(
        format=MANIFEST_FORMAT,
        version=MANIFEST_VERSION,
        image_name=image_name,
        image_size=image_size,
        block_bytes=block_bytes,
        block_count=block_count,
        blocks=blocks,
        regions=regions,
        source_name=str(doc.get("source_name", "")),
        disk_guid=str(doc.get("disk_guid", "")),
    )


def read_manifest_file(path: str | os.PathLike[str]) -> Manifest:
    """Read and validate a manifest from disk (bounded read)."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ManifestError([f"无法读取清单文件：{exc}"]) from exc
    if size > MAX_MANIFEST_BYTES:
        raise ManifestError(
            [f"清单文件 {size} 字节，超过 {MAX_MANIFEST_BYTES} 字节上限"]
        )
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManifestError([f"清单不是合法的 JSON：{exc}"]) from exc
    return validate_manifest(doc)


def manifest_bytes(manifest: Manifest) -> bytes:
    return (
        json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        + "\n"
    ).encode("utf-8")


def manifest_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
