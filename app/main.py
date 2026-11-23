"""FastAPI application: upload, convert, verify, download."""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from .config import Settings, settings as default_settings
from .errors import ConversionError
from .jobs import Job, make_job_manager
from .samples import SAMPLES_BY_KEY, SampleCache

logger = logging.getLogger("migrate")
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_CHUNK = 4 * 1024 * 1024
EXTERNAL_NAME_HEADER = "x-filename"


def _clean_name(raw: str | None) -> str:
    if not raw:
        return "disk.img"
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"')
    return name[:128] or "disk.img"


def _upload_name_from_header(request: Request) -> str:
    raw = request.headers.get(EXTERNAL_NAME_HEADER)
    if raw is None:
        return "disk.img"
    # Clients percent-encode the name to survive the ISO-8859-1 header charset.
    try:
        from urllib.parse import unquote

        raw = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        pass
    return _clean_name(raw)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or default_settings
    manager = make_job_manager(cfg)
    # The committed 64 MiB reproducible sample doubles as the "healthy" demo.
    samples = SampleCache(
        cfg.sample_dir,
        cfg.sample_cache_dir,
        prebuilt={"healthy": cfg.sample_download_name},
    )
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 让 multipart 落盘临时文件进入可写卷，而不是只读根文件系统。
        Path(tempfile.gettempdir()).mkdir(parents=True, exist_ok=True)
        manager.start()
        try:
            yield
        finally:
            manager.stop()

    app = FastAPI(
        title="512e → 4Kn GPT 迁盘",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.manager = manager
    app.state.settings = cfg
    app.mount(
        "/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static"
    )

    def error_response(
        exc: ConversionError, status: int | None = None
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status or exc.status_code, content={"error": exc.as_dict()}
        )

    def sample_catalog() -> list[dict]:
        try:
            return samples.catalog()
        except Exception:  # a broken scenario must not take down the page
            logger.exception("sample catalog failed")
            return []

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "max_upload_bytes": cfg.max_upload_bytes,
                "samples": sample_catalog(),
                "job_ttl_seconds": cfg.job_ttl_seconds,
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {
            "max_upload_bytes": cfg.max_upload_bytes,
            "source_sector_size": 512,
            "target_sector_size": 4096,
            "samples": sample_catalog(),
            "job_ttl_seconds": cfg.job_ttl_seconds,
        }

    @app.get("/api/samples")
    async def list_samples() -> dict[str, Any]:
        return {"samples": sample_catalog()}

    @app.get("/api/samples/{key}")
    async def sample_file(key: str):
        spec = SAMPLES_BY_KEY.get(key)
        if spec is None:
            return error_response(
                ConversionError("SAMPLE_MISSING", "没有这个样盘场景。", status_code=404)
            )
        try:
            path = samples.path_for(key)
        except Exception:
            return error_response(
                ConversionError(
                    "SAMPLE_BUILD_FAILED", "样盘生成失败。", status_code=500
                )
            )
        assert path is not None
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=spec.filename,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    def check_declared_size(request: Request) -> ConversionError | None:
        raw = request.headers.get("content-length")
        if raw is None:
            return None
        try:
            declared = int(raw)
        except ValueError:
            return ConversionError(
                "BAD_REQUEST", "Content-Length 不是整数。", status_code=400
            )
        # Multipart framing adds a small, bounded overhead.
        if declared > cfg.max_upload_bytes + 64 * 1024:
            return ConversionError(
                "FILE_TOO_LARGE",
                "上传文件超过服务端大小上限。",
                details={"max_bytes": cfg.max_upload_bytes},
                status_code=413,
            )
        return None

    async def store_raw_body(request: Request, job: Job) -> int:
        written = 0
        with open(job.source_path, "xb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > cfg.max_upload_bytes:
                    raise ConversionError(
                        "FILE_TOO_LARGE",
                        "上传文件超过服务端大小上限，已中止接收。",
                        details={"max_bytes": cfg.max_upload_bytes},
                        status_code=413,
                    )
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        return written

    async def store_multipart(request: Request, job: Job) -> tuple[int, str | None]:
        form = await request.form()
        try:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise ConversionError(
                    "NO_FILE", "请选择要上传的 RAW 磁盘文件。", status_code=400
                )
            name = _clean_name(upload.filename)
            written = 0
            with open(job.source_path, "xb") as out:
                while True:
                    chunk = await upload.read(UPLOAD_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > cfg.max_upload_bytes:
                        raise ConversionError(
                            "FILE_TOO_LARGE",
                            "上传文件超过服务端大小上限，已中止接收。",
                            details={"max_bytes": cfg.max_upload_bytes},
                            status_code=413,
                        )
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            return written, name
        finally:
            await form.close()

    @app.post("/api/jobs")
    async def create_job(request: Request) -> JSONResponse:
        declared_error = check_declared_size(request)
        if declared_error is not None:
            return error_response(declared_error)

        content_type = request.headers.get("content-type", "").lower().split(";")[0].strip()
        is_multipart = content_type == "multipart/form-data"

        job = manager.create_job(None)
        try:
            if is_multipart:
                written, name = await store_multipart(request, job)
            else:
                written = await store_raw_body(request, job)
                name = _upload_name_from_header(request)
            job.source_name = name
        except ConversionError as exc:
            manager.discard(job.id)
            return error_response(exc)
        except Exception:
            manager.discard(job.id)
            return error_response(
                ConversionError(
                    "UPLOAD_FAILED",
                    "接收上传文件时出错，请重试。",
                    status_code=500,
                )
            )

        if written == 0:
            manager.discard(job.id)
            return error_response(
                ConversionError("EMPTY_FILE", "上传的文件是空的。", status_code=400)
            )

        try:
            manager.analyze(job)
        except ConversionError as exc:
            manager.discard(job.id)
            return error_response(exc)
        except Exception:
            manager.discard(job.id)
            return error_response(
                ConversionError(
                    "INSPECT_FAILED", "解析磁盘 GPT 时出错。", status_code=500
                )
            )

        manager.enqueue(job)
        return JSONResponse(status_code=202, content={"job": manager.snapshot(job.id)})

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str) -> JSONResponse:
        snapshot = manager.snapshot(job_id)
        if snapshot is None:
            return error_response(
                ConversionError(
                    "JOB_NOT_FOUND",
                    "找不到该任务，可能已过期或被删除。",
                    status_code=404,
                )
            )
        return JSONResponse(snapshot)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> JSONResponse:
        snapshot = manager.cancel(job_id)
        if snapshot is None:
            return error_response(
                ConversionError("JOB_NOT_FOUND", "找不到该任务。", status_code=404)
            )
        return JSONResponse(snapshot)

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str) -> JSONResponse:
        job = manager.get(job_id)
        if job is None:
            return error_response(
                ConversionError("JOB_NOT_FOUND", "找不到该任务。", status_code=404)
            )
        manager.discard(job_id)
        return JSONResponse({"deleted": job_id})

    @app.get("/api/jobs/{job_id}/download")
    async def download(job_id: str):
        job: Job | None = manager.get(job_id)
        if job is None:
            return error_response(
                ConversionError("JOB_NOT_FOUND", "找不到该任务。", status_code=404)
            )
        if job.status != "done" or job.error is not None:
            return error_response(
                ConversionError(
                    "NOT_READY",
                    "任务尚未完成或未通过复验，暂不可下载。",
                    status_code=409,
                )
            )
        if not job.output_path.is_file():
            return error_response(
                ConversionError(
                    "OUTPUT_MISSING", "成品文件已不存在（可能已过期）。", status_code=410
                )
            )
        stem = _clean_name(job.source_name or "disk.img")
        if stem.lower().endswith(".img"):
            stem = stem[:-4]
        filename = f"{stem}.4k.img"
        return FileResponse(
            job.output_path,
            media_type="application/octet-stream",
            filename=filename,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    return app


app = create_app()
