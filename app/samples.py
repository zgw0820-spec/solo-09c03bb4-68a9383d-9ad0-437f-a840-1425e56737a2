"""Bundled example disks shown in the web UI.

Each entry is a small, self-describing scenario built on demand (or read
from ``MIGRATE_SAMPLE_DIR`` if a prebuilt file exists).  Keeping them small
keeps the container image and the repository light.
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MIB = 1024 * 1024
SECTOR = 512

ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"


@dataclass(frozen=True)
class SampleSpec:
    key: str
    filename: str
    title: str
    summary: str
    outcome: str  # "ok" | "rejected"
    build: Callable[[Path], None]
    disk_mib: int = 16


def _guid(seed: int) -> str:
    return f"{seed:08x}-2222-4333-8444-{seed:012x}"


def _build_healthy(path: Path) -> None:
    from .samplegen import make_disk  # noqa: PLC0415

    make_disk(path, disk_mib=16)


def _build_one_bad_copy(path: Path) -> None:
    from .samplegen import corrupt_primary_header, make_disk  # noqa: PLC0415

    make_disk(path, disk_mib=16)
    corrupt_primary_header(path)


def _build_tail_collision(path: Path) -> None:
    """A partition that the 4 KiB backup GPT area would overlap.

    Source layout: a partition whose final sector abuts the *512-byte* backup
    GPT.  On a 4 KiB sector disk the backup reserved area is 5 sectors
    (20 KiB), which reaches into this partition.
    """
    from .gpt import (  # noqa: PLC0415
        build_layout,
        encode_entry,
        layout_for_sector_size,
        pwrite_all,
        write_layout,
    )

    size = 16 * MIB
    last_lba, _, last_usable, es = layout_for_sector_size(size, SECTOR)
    # A 4 KiB-aligned partition that sits exactly in the region the new 4 KiB
    # backup GPT must occupy: [size - 20480, size - 17408).  Both ends are
    # 4 KiB aligned, so this is a pure space conflict, not an alignment one.
    start_byte = size - 5 * 4096
    end_byte = (last_usable + 1) * SECTOR
    raw = encode_entry(LINUX, _guid(3), 0, 0, name="tail-overlap")
    layout = build_layout(
        size,
        SECTOR,
        uuid.uuid4().bytes_le,
        [(1, raw, start_byte // SECTOR, end_byte // SECTOR - 1)],
    )
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, size)
        pwrite_all(fd, b"\x33" * 4096, start_byte)
        write_layout(fd, layout)
        os.fsync(fd)
    finally:
        os.close(fd)


def _build_unaligned(path: Path) -> None:
    from .samplegen import make_disk  # noqa: PLC0415

    make_disk(
        path,
        disk_mib=16,
        partitions=[
            dict(index=1, type=ESP, unique=_guid(1), name="shifted",
                 start=1 * MIB + 512, end=6 * MIB, fill=b"\x11")
        ],
    )


def _build_both_bad(path: Path) -> None:
    from .samplegen import (  # noqa: PLC0415
        corrupt_backup_entries,
        corrupt_primary_header,
        make_disk,
    )

    make_disk(path, disk_mib=16)
    corrupt_primary_header(path)
    corrupt_backup_entries(path)


SAMPLES: tuple[SampleSpec, ...] = (
    SampleSpec(
        key="healthy",
        filename="sample-512b-gpt.img",
        title="正常样盘（双 GPT 一致）",
        summary="16 MiB，512 字节逻辑扇区，两个 4 KiB 对齐的分区，双 GPT 一致。",
        outcome="ok",
        build=_build_healthy,
    ),
    SampleSpec(
        key="one-bad-copy",
        filename="sample-one-bad-copy.img",
        title="单表损坏",
        summary="主 GPT 表头 CRC 被破坏，备 GPT 完好，应仅用备份重建。",
        outcome="ok",
        build=_build_one_bad_copy,
    ),
    SampleSpec(
        key="tail-collision",
        filename="sample-tail-collision.img",
        title="空间冲突（备 GPT 区）",
        summary="分区尾部伸入 4 KiB 备 GPT 保留区，应拒绝转换并说明原因。",
        outcome="rejected",
        build=_build_tail_collision,
    ),
    SampleSpec(
        key="unaligned",
        filename="sample-unaligned.img",
        title="分区未对齐",
        summary="分区起点偏移 512 字节，无法按 4 KiB 扇区寻址，应拒绝。",
        outcome="rejected",
        build=_build_unaligned,
    ),
    SampleSpec(
        key="both-bad",
        filename="sample-both-bad.img",
        title="主备 GPT 均损坏",
        summary="两份 GPT 校验都失败，无法安全推断布局，应拒绝。",
        outcome="rejected",
        build=_build_both_bad,
    ),
)

SAMPLES_BY_KEY = {s.key: s for s in SAMPLES}


class SampleCache:
    """Locate prebuilt scenarios, or build missing ones on first download.

    ``base_dir`` is searched first (read-only image directory in Docker);
    ``cache_dir`` must be writable (a Docker volume).  The catalog itself
    never builds anything, so opening the page stays cheap.
    """

    def __init__(
        self,
        base_dir: Path | None,
        cache_dir: Path | None = None,
        *,
        prebuilt: dict[str, str] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else None
        self.prebuilt_names = dict(prebuilt or {})
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir is not None
            else Path(tempfile.gettempdir()) / "gpt-samples"
        )
        self._lock = threading.Lock()
        self._paths: dict[str, Path] = {}

    def locate(self, key: str) -> Path | None:
        """Return an existing file for ``key`` without building one.

        Callers must hold ``_lock`` (``catalog`` does so via ``path_for``).
        """
        spec = SAMPLES_BY_KEY.get(key)
        if spec is None:
            return None
        if self.base_dir is not None:
            name = self.prebuilt_names.get(key, spec.filename)
            prebuilt = self.base_dir / name
            if prebuilt.is_file():
                return prebuilt
        if key in self._paths and self._paths[key].is_file():
            return self._paths[key]
        cached = self.cache_dir / spec.filename
        return cached if cached.is_file() else None

    def path_for(self, key: str, *, build: bool = True) -> Path | None:
        spec = SAMPLES_BY_KEY.get(key)
        if spec is None:
            return None
        with self._lock:
            existing = self.locate(key)
            if existing is not None:
                self._paths[key] = existing
                return existing
            if not build:
                return None
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            target = self.cache_dir / spec.filename
            tmp = target.with_name(target.name + ".tmp")
            try:
                spec.build(tmp)
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
            self._paths[key] = target
            return target

    def catalog(self) -> list[dict]:
        items = []
        for spec in SAMPLES:
            with self._lock:
                path = self.locate(spec.key)
            items.append(
                {
                    "key": spec.key,
                    "title": spec.title,
                    "summary": spec.summary,
                    "outcome": spec.outcome,
                    "filename": spec.filename,
                    "size": path.stat().st_size if path else None,
                }
            )
        return items
