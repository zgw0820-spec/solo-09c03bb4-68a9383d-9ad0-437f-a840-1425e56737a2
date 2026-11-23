FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY --chown=10001:10001 sample ./sample

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin migrate \
 && mkdir -p /data/jobs \
 && chown -R migrate:migrate /data /srv/app

USER 10001:10001

ENV MIGRATE_WORK_DIR=/data/jobs \
    MIGRATE_SAMPLE_DIR=/srv/app/sample \
    MIGRATE_SAMPLE_CACHE_DIR=/data/jobs/_samples \
    MIGRATE_SAMPLE_NAME=sample-512b-gpt.img \
    MIGRATE_COPY_CHUNK_BYTES=4194304 \
    MIGRATE_MAX_UPLOAD_BYTES=8589934592 \
    MIGRATE_JOB_TTL_SECONDS=3600 \
    MIGRATE_MAX_CONCURRENT_JOBS=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
