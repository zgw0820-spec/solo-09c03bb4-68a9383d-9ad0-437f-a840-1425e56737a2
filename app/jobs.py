"""Disk-backed jobs with a small pool of conversion workers.

Uploads and converted images live on disk.  Only fixed-size buffers are held
in memory (upload chunks, copy chunks, GPT areas), so peak memory does not
depend on the disk length.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .converter import (
    SourceAnalysis,
    analyze_source,
    build_image_manifest,
    convert,
    plan_layout,
    verify_output,
    write_manifest_atomic,
)
from .errors import ConversionError, VerificationError
from .naming import manifest_filename, output_filename

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
META_NAME = "job.json"
SOURCE_NAME = "source.img"
OUTPUT_NAME = "output-4k.img"
MANIFEST_NAME = "manifest.json"
BLOCK_BYTES = 4 * 1024 * 1024
BLOCK_PREVIEW_LIMIT = 64


@dataclass
class Job:
    id: str
    dir: Path
    created_at: float
    updated_at: float
    status: str = "queued"  # queued|converting|verifying|done|failed
    copied_bytes: int = 0
    total_bytes: int = 0
    verify_bytes: int = 0
    analysis: SourceAnalysis | None = None
    result_checks: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    source_name: str | None = None
    manifest_sha256: str | None = None
    block_count: int = 0
    block_bytes: int = BLOCK_BYTES
    block_preview: list[dict[str, Any]] = field(default_factory=list)
    regions: list[dict[str, Any]] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def source_path(self) -> Path:
        return self.dir / SOURCE_NAME

    @property
    def output_path(self) -> Path:
        return self.dir / OUTPUT_NAME

    @property
    def manifest_path(self) -> Path:
        return self.dir / MANIFEST_NAME

    def summary(self) -> dict[str, Any] | None:
        a = self.analysis
        if a is None:
            return None
        return {
            "disk_size": a.disk_size,
            "source_sector_size": a.sector_size,
            "target_sector_size": 4096,
            "disk_guid": a.disk_guid,
            "used_gpt_copy": a.used_copy,
            "copies": [
                {
                    "which": c.which,
                    "valid": c.valid,
                    "used": c.used,
                    "problems": c.problems,
                    "detail": c.detail,
                }
                for c in a.copies
            ],
            "partitions": [
                {
                    "index": p.index,
                    "type_guid": p.type_guid_text,
                    "unique_guid": p.unique_guid_text,
                    "attributes": f"0x{p.attributes:016x}",
                    "name": p.name,
                    "start_lba_4k": p.start_lba // 8,
                    "end_lba_4k": (p.end_lba + 1) // 8 - 1,
                    "byte_start": p.start_lba * a.sector_size,
                    "byte_end_exclusive": (p.end_lba + 1) * a.sector_size,
                }
                for p in a.partitions
            ],
            "warnings": a.warnings,
        }

    def manifest_summary(self) -> dict[str, Any] | None:
        published = self.published
        return {
            "block_bytes": self.block_bytes,
            "block_count": self.block_count,
            "sha256": self.manifest_sha256,
            "filename": manifest_filename(self.source_name),
            "download_url": (
                f"/api/jobs/{self.id}/manifest" if published else None
            ),
            "blocks": self.block_preview,
            "block_preview_limit": BLOCK_PREVIEW_LIMIT,
            "regions": self.regions,
            "published": published,
        }

    def public(self) -> dict[str, Any]:
        published = self.published
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "copied_bytes": self.copied_bytes,
            "total_bytes": self.total_bytes,
            "verify_bytes": self.verify_bytes,
            "source_name": self.source_name,
            "summary": self.summary(),
            "checks": self.result_checks,
            "error": self.error,
            "manifest": self.manifest_summary(),
            "download_url": (
                f"/api/jobs/{self.id}/download" if published else None
            ),
            "output_size": self.total_bytes if published else None,
        }

    @property
    def published(self) -> bool:
        """Both the image and its manifest exist and passed verification."""
        return (
            self.status == "done"
            and bool(self.manifest_sha256)
            and self.output_path.is_file()
            and self.manifest_path.is_file()
        )

    def drop_published(self) -> None:
        """Withdraw the image and the manifest together."""
        self.manifest_sha256 = None
        self.output_path.unlink(missing_ok=True)
        self.manifest_path.unlink(missing_ok=True)


class JobManager:
    def __init__(
        self,
        work_dir: Path,
        *,
        max_concurrent_jobs: int = 1,
        chunk_size: int = 4 * 1024 * 1024,
        job_ttl_seconds: int = 3600,
        max_upload_bytes: int = 8 * 1024**3,
        block_bytes: int = BLOCK_BYTES,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.chunk_size = chunk_size
        self.job_ttl_seconds = job_ttl_seconds
        self.max_upload_bytes = max_upload_bytes
        self.block_bytes = block_bytes
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._workers = [
            threading.Thread(
                target=self._worker, name=f"migrate-worker-{i}", daemon=True
            )
            for i in range(max(1, max_concurrent_jobs))
        ]
        self._reaper: threading.Thread | None = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self.work_dir.mkdir(parents=True, exist_ok=True)
        # Job directories and the sample cache share this volume; the cache is
        # not named like a job, so orphan purging ignores it automatically.
        self._purge_orphans()
        for worker in self._workers:
            worker.start()
        self._reaper = threading.Thread(
            target=self._reap_loop, name="migrate-reaper", daemon=True
        )
        self._reaper.start()
        self._started = True

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel.set()
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=5)
        if self._reaper is not None:
            self._reaper.join(timeout=5)
        self._started = False

    def _purge_orphans(self) -> None:
        """Drop directories left behind by a previous process."""
        if not self.work_dir.is_dir():
            return
        with self._lock:
            active = set(self._jobs)
        cutoff = time.time() - self.job_ttl_seconds
        for child in self.work_dir.iterdir():
            if not child.is_dir() or not JOB_ID_RE.match(child.name):
                continue
            if child.name in active:
                continue
            try:
                old = child.stat().st_mtime < cutoff
            except OSError:
                continue
            if old:
                shutil.rmtree(child, ignore_errors=True)

    def _reap_loop(self) -> None:
        interval = min(60.0, max(5.0, self.job_ttl_seconds / 4))
        while not self._stop.wait(interval):
            cutoff = time.time() - self.job_ttl_seconds
            with self._lock:
                expired = [
                    job.id
                    for job in self._jobs.values()
                    if job.updated_at < cutoff and job.status in ("done", "failed")
                ]
            for job_id in expired:
                self.discard(job_id)
            if self._stop.is_set():
                return
            self._purge_orphans()

    # -- job creation ------------------------------------------------------

    def create_job(self, source_name: str | None) -> Job:
        job_id = uuid.uuid4().hex
        job_dir = self.work_dir / job_id
        job_dir.mkdir()
        now = time.time()
        job = Job(
            id=job_id,
            dir=job_dir,
            created_at=now,
            updated_at=now,
            source_name=source_name,
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def analyze(self, job: Job) -> SourceAnalysis:
        try:
            size = job.source_path.stat().st_size
        except OSError as exc:
            raise ConversionError(
                "SOURCE_MISSING", "源文件已不存在，任务无法继续。", status_code=410
            ) from exc
        job.total_bytes = size
        fd = os.open(job.source_path, os.O_RDONLY)
        try:
            analysis = analyze_source(fd, size)
        finally:
            os.close(fd)
        job.analysis = analysis
        return analysis

    def enqueue(self, job: Job) -> None:
        self._queue.put(job.id)

    def discard(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is not None:
            job.cancel.set()
            shutil.rmtree(job.dir, ignore_errors=True)

    # -- reads -------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        if not JOB_ID_RE.match(job_id):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        if job is None:
            return None
        with self._lock:
            return job.public()

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.cancel.set()
        with self._lock:
            if job.status in ("queued", "converting", "verifying"):
                job.status = "failed"
                job.error = {
                    "code": "CANCELLED",
                    "message": "任务已取消。",
                    "details": {},
                }
                job.drop_published()
                self._write_meta(job)
            return job.public()

    # -- worker ------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            job = self.get(job_id)
            if job is None:
                continue
            if job.cancel.is_set():
                # Cancelled while queued: the API already set the failure state.
                self._write_meta(job)
                continue
            try:
                self._run_job(job)
            except ConversionError as exc:
                if job.status != "done":
                    self._fail(job, exc.code, exc.message, exc.details)
            except VerificationError as exc:
                self._fail(
                    job,
                    "OUTPUT_VERIFICATION_FAILED",
                    "成品复验未通过，已删除镜像和清单，拒绝提供下载。",
                    {"reasons": exc.reasons},
                )
            except Exception as exc:  # pragma: no cover - defensive
                self._fail(
                    job,
                    "INTERNAL_ERROR",
                    "转换过程中发生内部错误，输出已丢弃。",
                    {"type": type(exc).__name__},
                )
            finally:
                if job.status != "done":
                    job.drop_published()
                self._write_meta(job)

    def _check_cancel(self, job: Job) -> None:
        if job.cancel.is_set() or job.status == "failed":
            raise ConversionError("CANCELLED", "任务已取消。", status_code=409)

    def _run_job(self, job: Job) -> None:
        analysis = job.analysis
        if analysis is None:
            analysis = self.analyze(job)
        plan_layout(analysis)  # fail before creating any output
        self._set_status(job, "converting")

        last_write = time.monotonic()

        def progress(copied: int) -> None:
            nonlocal last_write
            if job.cancel.is_set():
                raise ConversionError(
                    "CANCELLED", "任务已取消。", status_code=409
                )
            job.copied_bytes = copied
            now = time.monotonic()
            if now - last_write >= 1.0:
                last_write = now
                job.updated_at = time.time()
                self._write_meta(job)

        src_fd = os.open(job.source_path, os.O_RDONLY)
        try:
            dst_fd = os.open(
                job.output_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                result = convert(
                    src_fd,
                    dst_fd,
                    analysis,
                    chunk_size=self.chunk_size,
                    progress=progress,
                )
            finally:
                os.close(dst_fd)
        finally:
            os.close(src_fd)

        self._check_cancel(job)

        self._set_status(job, "verifying")
        job.copied_bytes = analysis.disk_size
        self._write_meta(job)

        # Verification and manifest creation share a single sequential read
        # of the finished image; the source disk is not copied a second time.
        def verify_progress(scanned: int) -> None:
            if job.cancel.is_set():
                raise ConversionError("CANCELLED", "任务已取消。", status_code=409)
            job.verify_bytes = scanned

        report = verify_output(
            str(job.output_path),
            result,
            block_bytes=self.block_bytes,
            chunk_size=self.chunk_size,
            progress=verify_progress,
        )
        job.result_checks = report.checks
        self._check_cancel(job)

        image_name = output_filename(job.source_name)
        manifest = build_image_manifest(
            str(job.output_path),
            result,
            image_name=image_name,
            source_name=job.source_name or "",
            block_bytes=self.block_bytes,
        )
        job.block_count = manifest.block_count
        job.block_bytes = manifest.block_bytes
        job.block_preview = [
            {
                "index": b.index,
                "offset": b.offset,
                "length": b.length,
                "sha256": b.sha256,
            }
            for b in manifest.blocks[:BLOCK_PREVIEW_LIMIT]
        ]
        job.regions = [
            {"kind": r.kind, "start": r.start, "end": r.end, "label": r.label}
            for r in manifest.regions
        ]
        digest = write_manifest_atomic(str(job.manifest_path), manifest)

        # Publication is atomic under the lock: a cancel that arrives after
        # the manifest is written still wins and withdraws both files.
        with self._lock:
            if job.cancel.is_set() or job.status == "failed":
                raise ConversionError("CANCELLED", "任务已取消。", status_code=409)
            job.source_path.unlink(missing_ok=True)
            job.manifest_sha256 = digest
            job.status = "done"
            job.updated_at = time.time()
            self._write_meta(job)

    def _set_status(self, job: Job, status: str) -> None:
        job.status = status
        job.updated_at = time.time()
        self._write_meta(job)

    def _fail(
        self,
        job: Job,
        code: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        job.error = {
            "code": code,
            "message": message,
            "details": details or {},
        }
        job.status = "failed"
        job.updated_at = time.time()
        job.drop_published()
        self._write_meta(job)

    def _write_meta(self, job: Job) -> None:
        meta = job.public()
        meta["version"] = 1
        path = job.dir / META_NAME
        tmp = job.dir / (META_NAME + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(meta, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except FileNotFoundError:
            pass  # job was deleted concurrently


def make_job_manager(settings: Any) -> JobManager:
    return JobManager(
        settings.work_dir,
        max_concurrent_jobs=settings.max_concurrent_jobs,
        chunk_size=settings.copy_chunk_bytes,
        job_ttl_seconds=settings.job_ttl_seconds,
        max_upload_bytes=settings.max_upload_bytes,
        block_bytes=settings.manifest_block_bytes,
    )
