"""Runtime configuration, all overridable through environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    work_dir: Path
    max_upload_bytes: int
    job_ttl_seconds: int
    copy_chunk_bytes: int
    max_concurrent_jobs: int
    sample_dir: Path | None
    sample_cache_dir: Path
    sample_download_name: str

    @classmethod
    def from_env(cls) -> "Settings":
        work_dir = Path(os.environ.get("MIGRATE_WORK_DIR", "/data/jobs"))
        sample_raw = os.environ.get("MIGRATE_SAMPLE_DIR", "/srv/app/sample")
        sample_dir = Path(sample_raw) if sample_raw else None
        cache_raw = os.environ.get("MIGRATE_SAMPLE_CACHE_DIR")
        sample_cache_dir = (
            Path(cache_raw) if cache_raw else work_dir / "_samples"
        )
        return cls(
            work_dir=work_dir,
            max_upload_bytes=_int_env(
                "MIGRATE_MAX_UPLOAD_BYTES", 8 * 1024 * 1024 * 1024
            ),
            job_ttl_seconds=_int_env("MIGRATE_JOB_TTL_SECONDS", 3600),
            copy_chunk_bytes=_int_env(
                "MIGRATE_COPY_CHUNK_BYTES", 4 * 1024 * 1024
            ),
            max_concurrent_jobs=_int_env("MIGRATE_MAX_CONCURRENT_JOBS", 1),
            sample_dir=sample_dir,
            sample_cache_dir=sample_cache_dir,
            sample_download_name=os.environ.get(
                "MIGRATE_SAMPLE_NAME", "sample-512b-gpt.img"
            ),
        )


settings = Settings.from_env()
