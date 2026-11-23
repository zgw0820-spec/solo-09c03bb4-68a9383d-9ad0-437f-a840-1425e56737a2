"""Block manifest, single-pass verification and the download gate."""

from __future__ import annotations

import hashlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import gpt
from app import samplegen as fixtures
from app.config import Settings
from app.converter import (
    analyze_source,
    build_image_manifest,
    convert,
    verify_output,
)
from app.errors import VerificationError
from app.main import create_app
from app.manifest import (
    ManifestError,
    read_manifest_file,
    validate_manifest,
)
from app.samplegen import MIB

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _analyze(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return analyze_source(fd, os.path.getsize(path))
    finally:
        os.close(fd)


def _convert(src, dst, chunk=1 * MIB):
    analysis = _analyze(src)
    src_fd = os.open(src, os.O_RDONLY)
    try:
        dst_fd = os.open(dst, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            return convert(src_fd, dst_fd, analysis, chunk_size=chunk)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


@pytest.fixture()
def converted(tmp_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=16)
    out = tmp_path / "out.img"
    result = _convert(src, out)
    return out, result


# -- manifest content ------------------------------------------------------


def test_blocks_cover_whole_image_including_short_tail(tmp_path):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=16)
    out = tmp_path / "out.img"
    result = _convert(src, out)
    block = 3 * MIB  # deliberately not a divisor of 16 MiB
    report = verify_output(str(out), result, block_bytes=block)
    blocks = report.block_hashes
    assert [b.offset for b in blocks] == [i * block for i in range(len(blocks))]
    assert [b.length for b in blocks] == [block] * 5 + [1 * MIB]
    assert blocks[-1].offset + blocks[-1].length == 16 * MIB
    # Every block digest must match an independent re-read of that slice.
    with open(out, "rb") as handle:
        for block_info in blocks:
            handle.seek(block_info.offset)
            assert hashlib.sha256(handle.read(block_info.length)).hexdigest() == block_info.sha256


def test_manifest_covers_gpt_unallocated_and_partition(converted):
    out, result = converted
    report = verify_output(str(out), result, block_bytes=4 * MIB)
    manifest = build_image_manifest(
        str(out),
        result,
        image_name="disk.4k.img",
        source_name="disk.img",
        block_bytes=4 * MIB,
    )
    kinds = [region.kind for region in manifest.regions]
    assert kinds[0] == "gpt-head"
    assert kinds[-1] == "gpt-tail"
    assert "partition" in kinds
    assert "unallocated" in kinds
    # Regions tile the image exactly.
    assert manifest.regions[0].start == 0
    assert manifest.regions[-1].end == manifest.image_size
    for prev, cur in zip(manifest.regions, manifest.regions[1:]):
        assert prev.end == cur.start
    assert [b.sha256 for b in manifest.blocks] == [
        b.sha256 for b in report.block_hashes
    ]
    assert manifest.block_count == 4
    validate_manifest(manifest.to_dict())


def test_manifest_rejects_incomplete_block_list(converted):
    out, result = converted
    verify_output(str(out), result, block_bytes=4 * MIB)
    manifest = build_image_manifest(
        str(out), result, image_name="x.img", source_name="x.img", block_bytes=4 * MIB
    )
    doc = manifest.to_dict()
    doc["blocks"] = doc["blocks"][:-1]
    with pytest.raises(ManifestError) as info:
        validate_manifest(doc)
    assert any("不符" in p or "覆盖" in p for p in info.value.problems)


def test_manifest_rejects_gap_between_blocks(converted):
    out, result = converted
    verify_output(str(out), result, block_bytes=4 * MIB)
    manifest = build_image_manifest(
        str(out), result, image_name="x.img", source_name="x.img", block_bytes=4 * MIB
    )
    doc = manifest.to_dict()
    doc["blocks"][2]["offset"] += 4096
    with pytest.raises(ManifestError):
        validate_manifest(doc)


def test_manifest_rejects_truncated_last_block(converted):
    out, result = converted
    verify_output(str(out), result, block_bytes=3 * MIB)
    manifest = build_image_manifest(
        str(out), result, image_name="x.img", source_name="x.img", block_bytes=3 * MIB
    )
    doc = manifest.to_dict()
    doc["blocks"][-1]["length"] -= 512
    with pytest.raises(ManifestError) as info:
        validate_manifest(doc)
    assert any("应为" in p for p in info.value.problems)


def test_manifest_rejects_wrong_declared_length(converted):
    out, result = converted
    verify_output(str(out), result, block_bytes=4 * MIB)
    manifest = build_image_manifest(
        str(out), result, image_name="x.img", source_name="x.img", block_bytes=4 * MIB
    )
    doc = manifest.to_dict()
    doc["image_size"] += 4096
    with pytest.raises(ManifestError):
        validate_manifest(doc)


def test_manifest_file_round_trip(converted, tmp_path):
    out, result = converted
    verify_output(str(out), result, block_bytes=4 * MIB)
    manifest = build_image_manifest(
        str(out), result, image_name="x.img", source_name="x.img", block_bytes=4 * MIB
    )
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    assert read_manifest_file(path).to_dict() == manifest.to_dict()
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestError):
        read_manifest_file(path)


# -- single sequential pass ------------------------------------------------


def test_verification_reads_the_image_once(tmp_path, monkeypatch):
    src = tmp_path / "src.img"
    fixtures.make_disk(src, disk_mib=16)
    out = tmp_path / "out.img"
    result = _convert(src, out)

    original = gpt.pread_exact
    reads: list[tuple[int, int]] = []
    seen_sizes: set[int] = set()

    def tracking(fd, length, offset):
        reads.append((offset, length))
        seen_sizes.add(length)
        return original(fd, length, offset)

    monkeypatch.setattr(gpt, "pread_exact", tracking)
    report = verify_output(str(out), result, block_bytes=4 * MIB, chunk_size=1 * MIB)
    assert report.ok
    image_size = 16 * MIB
    assert report.scanned_bytes == image_size
    sequential = [r for r in reads if r[1] == 1 * MIB]
    assert len(sequential) == image_size // (1 * MIB)
    assert [offset for offset, _ in sequential] == [
        i * MIB for i in range(image_size // MIB)
    ]
    # GPT header/table/reserved reads are tiny; the whole image is not read
    # twice (a full extra pass would add another image_size worth of bytes).
    total = sum(length for _, length in reads)
    assert total < image_size + 128 * 1024


# -- HTTP gate -------------------------------------------------------------


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
        manifest_block_bytes=4 * MIB,
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


def test_downloads_require_both_artifacts(client, tmp_path):
    src = tmp_path / "vm.img"
    fixtures.make_disk(src, disk_mib=16)
    job_id = _upload(client, src, name="vm.img").json()["job"]["id"]
    final = _wait(client, job_id)
    assert final["status"] == "done"
    assert final["download_url"]
    assert final["manifest"]["download_url"]
    assert final["manifest"]["block_count"] == 4
    assert final["manifest"]["block_bytes"] == 4 * MIB

    image = client.get(final["download_url"])
    manifest = client.get(final["manifest"]["download_url"])
    assert image.status_code == 200 and len(image.content) == 16 * MIB
    assert manifest.status_code == 200
    assert "vm.4k.manifest.json" in manifest.headers["content-disposition"]
    doc = json.loads(manifest.content)
    assert doc["image_size"] == 16 * MIB
    assert len(doc["blocks"]) == 4
    assert doc["blocks"][-1]["offset"] + doc["blocks"][-1]["length"] == 16 * MIB
    # The served manifest's blocks describe the served image.
    assert hashlib.sha256(image.content[: 4 * MIB]).hexdigest() == doc["blocks"][0]["sha256"]
    assert hashlib.sha256(image.content[-4 * MIB:]).hexdigest() == doc["blocks"][-1]["sha256"]


def test_manifest_withheld_when_verification_fails(client, tmp_path, monkeypatch):
    src = tmp_path / "vm.img"
    fixtures.make_disk(src, disk_mib=16)

    def boom(*args, **kwargs):
        raise VerificationError(["simulated failure"])

    monkeypatch.setattr("app.jobs.verify_output", boom)
    job_id = _upload(client, src).json()["job"]["id"]
    final = _wait(client, job_id)
    assert final["status"] == "failed"
    assert final["error"]["code"] == "OUTPUT_VERIFICATION_FAILED"
    assert final["download_url"] is None
    assert final["manifest"]["download_url"] is None
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 409
    assert client.get(f"/api/jobs/{job_id}/manifest").status_code == 409
    job_dir = client.app.state.settings.work_dir / job_id
    assert not (job_dir / "output-4k.img").exists()
    assert not (job_dir / "manifest.json").exists()


def test_manifest_withheld_when_manifest_write_fails(client, tmp_path, monkeypatch):
    src = tmp_path / "vm.img"
    fixtures.make_disk(src, disk_mib=16)

    def boom(*args, **kwargs):
        raise VerificationError(["manifest write failed"])

    monkeypatch.setattr("app.jobs.write_manifest_atomic", boom)
    job_id = _upload(client, src).json()["job"]["id"]
    final = _wait(client, job_id)
    assert final["status"] == "failed"
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 409
    assert client.get(f"/api/jobs/{job_id}/manifest").status_code == 409
    job_dir = client.app.state.settings.work_dir / job_id
    assert not (job_dir / "output-4k.img").exists()
    assert not (job_dir / "manifest.json").exists()


def test_cancel_before_publish_withholds_both(client, tmp_path):
    src = tmp_path / "vm.img"
    fixtures.make_disk(src, disk_mib=16)
    job_id = _upload(client, src).json()["job"]["id"]
    # Cancel immediately; the worker must not publish either file afterwards.
    cancelled = client.post(f"/api/jobs/{job_id}/cancel").json()
    if cancelled["status"] == "failed":
        assert cancelled["error"]["code"] == "CANCELLED"
        assert cancelled["download_url"] is None
        assert cancelled["manifest"]["download_url"] is None
        final = _wait(client, job_id)
        assert final["status"] == "failed"
        assert client.get(f"/api/jobs/{job_id}/download").status_code == 409
        assert client.get(f"/api/jobs/{job_id}/manifest").status_code == 409
    else:  # too fast to cancel; whichever state, the pair stays consistent
        assert cancelled["status"] == "done"
        assert client.get(f"/api/jobs/{job_id}/download").status_code == 200
        assert client.get(f"/api/jobs/{job_id}/manifest").status_code == 200


def test_offline_verifier_page_is_self_contained(client):
    online = client.get("/verify")
    assert online.status_code == 200
    assert "/static/verify.js" in online.text

    offline = client.get("/api/verifier/offline.html")
    assert offline.status_code == 200
    assert "attachment" in offline.headers["content-disposition"]
    body = offline.text
    assert "<style>" in body and "<script>" in body
    assert "/static/verify.js" not in body
    assert "/static/verify.css" not in body
    # The page is local-only: it must not call back into the server.
    assert "fetch(" not in body
    assert "XMLHttpRequest" not in body
    assert "gpt-4kn-block-manifest" in body
    # The offline copy does not offer to download itself, nor link anywhere.
    assert "另存离线校验页" not in body
    assert "<link " not in body
    for needle in ('src="http', "src='http", 'href="http', "href='http", "import(", "navigator.sendBeacon"):
        assert needle not in body


def test_status_before_manifest_is_not_downloadable(client, tmp_path, monkeypatch):
    """A 'done' job whose manifest vanished must not serve the image alone."""
    src = tmp_path / "vm.img"
    fixtures.make_disk(src, disk_mib=16)
    job_id = _upload(client, src).json()["job"]["id"]
    final = _wait(client, job_id)
    assert final["status"] == "done"
    job = client.app.state.manager.get(job_id)
    job.manifest_path.unlink()
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 409
    assert client.get(f"/api/jobs/{job_id}/manifest").status_code == 409
