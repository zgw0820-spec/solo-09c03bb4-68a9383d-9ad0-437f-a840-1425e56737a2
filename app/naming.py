"""User-visible file names for uploads, images and manifests."""

from __future__ import annotations

import re

_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\/]')


def clean_name(raw: str | None) -> str:
    """Return a safe, single-component display/download name."""
    if not raw:
        return "disk.img"
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"')
    name = _UNSAFE.sub("", name)
    return name[:128] or "disk.img"


def _stem(raw: str | None) -> str:
    name = clean_name(raw)
    if name.lower().endswith(".img"):
        name = name[:-4]
    name = name.strip()
    return name or "disk"


def output_filename(raw: str | None) -> str:
    return f"{_stem(raw)}.4k.img"


def manifest_filename(raw: str | None) -> str:
    return f"{_stem(raw)}.4k.manifest.json"
