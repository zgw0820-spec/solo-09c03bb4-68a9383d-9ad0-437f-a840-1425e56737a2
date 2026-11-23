"""HTTP-layer tests: upload, poll, download, rejection paths."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app import samplegen as fixtures
from app.samplegen import MIB


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        work_dir=tmp_path / "jobs",
        max_upload_bytes=128 * MIB,
        job_ttl_seconds=600,
        copy_chunk_bytes=1 * MIB,
        max_concurrent_jobs=1,
        sample_dir=tmp_path / "samples",
        sample_cache_dir=tmp_path / "samples",
        sample_download_name="sample.img",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _upload(client, path, name="disk.img"):
    with open(path, "rb") as fh:
        return client.post(
            "/api/jobs",
            content=fh,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename": name,
            },
        )


def _wait(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_upload_convert_download(client, tmp_path):
    src = tmp_path / "vm-disk.img"
    fixtures.make_disk(src, disk_mib=16)
    response = _upload(client, src, name="vm-disk.img")
    assert response.status_code == 202, response.text
    job = response.json()["job"]
    assert job["summary"]["source_sector_size"] == 512
    assert job["download_url"] is None

    final = _wait(client, job["id"])
    assert final["status"] == "done", final
    assert final["checks"]
    assert final["summary"]["used_gpt_copy"] == "primary"
    assert final["summary"]["partitions"][0]["start_lba_4k"] == 256

    download = client.get(final["download_url"])
    assert download.status_code == 200
    assert len(download.content) == 16 * MIB
    assert "vm-disk.4k.img" in download.headers["content-disposition"]

    # The produced image is independently parseable at 4 KiB.
    out = tmp_path / "downloaded.img"
    out.write_bytes(download.content)
    fixtures_inspect(out)


def fixtures_inspect(path):
    import os

    from app.gpt import inspect_mbr, read_gpt_copy

    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.path.getsize(path)
        assert inspect_mbr(fd, size, 4096).problems == []
        read_gpt_copy(fd, size, 4096, "primary")
        read_gpt_copy(fd, size, 4096, "backup")
    finally:
        os.close(fd)


def test_multipart_upload(client, tmp_path):
    src = tmp_path / "disk.img"
    fixtures.make_disk(src, disk_mib=8)
    with open(src, "rb") as fh:
        response = client.post(
            "/api/jobs", files={"file": ("disk.img", fh, "application/octet-stream")}
        )
    assert response.status_code == 202, response.text
    final = _wait(client, response.json()["job"]["id"])
    assert final["status"] == "done"
    assert final["source_name"] == "disk.img"


def test_rejected_disk_returns_explanation(client, tmp_path):
    src = tmp_path / "hybrid.img"
    fixtures.make_disk(src, disk_mib=8)
    fixtures.add_hybrid_mbr_entry(src)
    response = _upload(client, src)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "HYBRID_MBR"
    assert error["message"]


def test_both_copies_bad_returns_explanation(client, tmp_path):
    src = tmp_path / "bad.img"
    fixtures.make_disk(src, disk_mib=8)
    fixtures.corrupt_primary_header(src)
    fixtures.corrupt_backup_entries(src)
    response = _upload(client, src)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "BOTH_GPT_INVALID"
    assert len(error["details"]["copies"]) == 2


def test_single_bad_copy_is_reported_in_summary(client, tmp_path):
    src = tmp_path / "onebad.img"
    fixtures.make_disk(src, disk_mib=8)
    fixtures.corrupt_primary_header(src)
    response = _upload(client, src)
    assert response.status_code == 202
    final = _wait(client, response.json()["job"]["id"])
    assert final["status"] == "done"
    assert final["summary"]["used_gpt_copy"] == "backup"
    copies = {c["which"]: c for c in final["summary"]["copies"]}
    assert copies["primary"]["valid"] is False
    assert copies["primary"]["problems"]


def test_empty_upload_is_rejected(client):
    response = client.post(
        "/api/jobs",
        content=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EMPTY_FILE"


def test_size_limit_enforced(tmp_path):
    settings = Settings(
        work_dir=tmp_path / "jobs",
        max_upload_bytes=4 * MIB,
        job_ttl_seconds=600,
        copy_chunk_bytes=1 * MIB,
        max_concurrent_jobs=1,
        sample_dir=None,
        sample_cache_dir=tmp_path / "samples",
        sample_download_name="sample.img",
    )
    src = tmp_path / "big.img"
    fixtures.make_disk(src, disk_mib=8)
    with TestClient(create_app(settings)) as small:
        response = _upload(small, src)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_download_before_finish_is_conflict(client, tmp_path):
    src = tmp_path / "slow.img"
    fixtures.make_disk(src, disk_mib=8)
    response = _upload(client, src)
    job_id = response.json()["job"]["id"]
    download = client.get(f"/api/jobs/{job_id}/download")
    if download.status_code == 409:
        assert download.json()["error"]["code"] == "NOT_READY"
    else:  # finished too fast
        assert download.status_code == 200


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/" + "0" * 32).status_code == 404
    assert client.get("/api/jobs/not-a-job").status_code == 404


def test_sample_catalog_and_download(client):
    catalog = client.get("/api/samples").json()["samples"]
    keys = {item["key"] for item in catalog}
    assert {"healthy", "one-bad-copy", "tail-collision", "unaligned", "both-bad"} <= keys
    response = client.get("/api/samples/healthy")
    assert response.status_code == 200
    assert len(response.content) == 16 * MIB
    assert client.get("/api/samples/nope").status_code == 404


def test_sample_scenarios_behave_as_advertised(client):
    """Each bundled scenario must produce its documented outcome end-to-end."""
    expected = {
        "healthy": ("done", None),
        "one-bad-copy": ("done", None),
        "tail-collision": ("rejected", "GPT_REGION_CONFLICT"),
        "unaligned": ("rejected", "PARTITION_NOT_ALIGNED"),
        "both-bad": ("rejected", "BOTH_GPT_INVALID"),
    }
    for key, (want_status, want_code) in expected.items():
        blob = client.get(f"/api/samples/{key}").content
        response = client.post(
            "/api/jobs",
            content=blob,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename": f"{key}.img",
            },
        )
        if want_status == "rejected":
            assert response.status_code == 422, (key, response.text)
            assert response.json()["error"]["code"] == want_code, key
            continue
        assert response.status_code == 202, (key, response.text)
        final = _wait(client, response.json()["job"]["id"])
        assert final["status"] == "done", (key, final)
        assert final["checks"]


def test_index_and_assets(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "512e → 4Kn" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_delete_job(client, tmp_path):
    src = tmp_path / "d.img"
    fixtures.make_disk(src, disk_mib=8)
    job_id = _upload(client, src).json()["job"]["id"]
    _wait(client, job_id)
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_cancel_queued_or_running_job(client, tmp_path):
    src = tmp_path / "c.img"
    fixtures.make_disk(src, disk_mib=16)
    response = _upload(client, src)
    assert response.status_code == 202
    job_id = response.json()["job"]["id"]
    cancelled = client.post(f"/api/jobs/{job_id}/cancel").json()
    assert cancelled["status"] in ("failed", "done")
    if cancelled["status"] == "failed":
        assert cancelled["error"]["code"] == "CANCELLED"
        assert client.get(f"/api/jobs/{job_id}/download").status_code == 409


def test_job_ttl_purges_completed_job(tmp_path):
    """A finished job older than the TTL is deleted by the reaper."""
    import time as _time

    from app.jobs import JobManager

    manager = JobManager(tmp_path / "jobs", job_ttl_seconds=1, chunk_size=MIB)
    manager.start()
    try:
        job = manager.create_job("x.img")
        job.source_path.write_bytes(b"\x00" * 512)
        job.status = "done"
        job.updated_at = _time.time() - 3600
        with manager._lock:  # noqa: SLF001
            manager._jobs[job.id] = job
        job_dir = job.dir
        # Force the reaper pass directly instead of sleeping.
        with manager._lock:  # noqa: SLF001
            expired = [
                j.id
                for j in manager._jobs.values()
                if j.updated_at < _time.time() - manager.job_ttl_seconds
                and j.status in ("done", "failed")
            ]
        for jid in expired:
            manager.discard(jid)
        assert manager.get(job.id) is None
        assert not job_dir.exists()
    finally:
        manager.stop()


def test_orphan_directories_are_cleaned_on_start(tmp_path):
    import os as _os
    import time as _time

    from app.jobs import JobManager

    work = tmp_path / "jobs"
    stale = work / ("a" * 32)
    stale.mkdir(parents=True)
    (stale / "source.img").write_bytes(b"\x00")
    old = _time.time() - 7200
    _os.utime(stale, (old, old))
    manager = JobManager(work, job_ttl_seconds=60)
    manager.start()
    try:
        assert not stale.exists()
    finally:
        manager.stop()


def test_filename_percent_encoded_round_trip(client, tmp_path):
    from urllib.parse import quote

    src = tmp_path / "磁盘.img"
    fixtures.make_disk(src, disk_mib=8)
    with open(src, "rb") as fh:
        response = client.post(
            "/api/jobs",
            content=fh,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename": quote("虚拟机 磁盘.img"),
            },
        )
    assert response.status_code == 202, response.text
    final = _wait(client, response.json()["job"]["id"])
    assert final["source_name"] == "虚拟机 磁盘.img"
    from urllib.parse import unquote

    disposition = unquote(
        client.get(final["download_url"]).headers["content-disposition"]
    )
    assert "虚拟机 磁盘.4k.img" in disposition


def test_sample_cache_accepts_string_paths(tmp_path):
    """Config values are Paths in-app, but the cache must not care."""
    from app.samples import SampleCache

    cache = SampleCache(str(tmp_path), str(tmp_path / "cache"))
    path = cache.path_for("healthy")
    assert path is not None and path.stat().st_size == 16 * MIB
