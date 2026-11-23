#!/usr/bin/env python3
"""Create the example 512-byte-sector GPT disk committed to the repo.

16 MiB, two 1 MiB-aligned partitions, valid protective MBR, identical
primary/backup GPTs.  Only the GPT areas and a short banner per partition are
actually written, so the file is a sparse 16 MiB image occupying ~40 KiB on
disk (and well under 100 KiB in git).

Usage:
    python3 tools/make_sample.py            # (re)generate and validate
    python3 tools/make_sample.py --check    # validate the existing file
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.converter import analyze_source  # noqa: E402
from app.gpt import build_layout, encode_entry, guid_from_str, pwrite_all, write_layout  # noqa: E402

MIB = 1024 * 1024
SECTOR = 512
DISK_SIZE = 16 * MIB
DISK_GUID = "8b0e5f1a-2c4d-4e8f-9a3b-1c2d3e4f5a6b"
DEFAULT_OUTPUT = REPO_ROOT / "sample" / "sample-512b-gpt.img"

ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
PARTS = [
    dict(index=1, type=ESP, unique="1d2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9",
         name="EFI System", start=1 * MIB, end=4 * MIB),
    dict(index=2, type=LINUX, unique="9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a",
         name="data", start=5 * MIB, end=DISK_SIZE - 2 * MIB),
]


def build(path: Path) -> None:
    specs = []
    for p in PARTS:
        raw = encode_entry(p["type"], p["unique"], 0, 0, name=p["name"])
        specs.append((p["index"], raw, p["start"] // SECTOR, p["end"] // SECTOR - 1))
    layout = build_layout(DISK_SIZE, SECTOR, guid_from_str(DISK_GUID), specs)

    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, DISK_SIZE)  # holes: unallocated space reads as zeros
        for p in PARTS:
            banner = (
                f"512e sample partition {p['index']} ({p['name']}), byte range "
                f"[{p['start']}, {p['end']}), {p['end'] - p['start']} bytes\n"
            ).encode("ascii")
            pwrite_all(fd, banner, p["start"] + 1024)
        write_layout(fd, layout)
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check", action="store_true", help="only validate an existing file"
    )
    args = parser.parse_args()

    out: Path = args.output
    if not args.check:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        build(tmp)
        os.replace(tmp, out)

    if not out.is_file():
        print(f"{out} does not exist", file=sys.stderr)
        return 1

    fd = os.open(out, os.O_RDONLY)
    try:
        analysis = analyze_source(fd, os.path.getsize(out))
    finally:
        os.close(fd)
    print(
        f"{out}: {out.stat().st_size} bytes, used={analysis.used_copy}, "
        f"guid={analysis.disk_guid}, "
        f"partitions={[(p.index, p.start_lba, p.end_lba) for p in analysis.partitions]}"
    )
    for warning in analysis.warnings:
        print(f"  note: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
