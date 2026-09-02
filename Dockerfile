# syntax=docker/dockerfile:1

# Media Monitor — FastAPI frontend plus the sitemap/extract/rerank/report
# pipeline. CPU only: the cross-encoder runs on a 2 vCPU instance, so the image
# deliberately avoids every CUDA wheel.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# lxml and pandas ship manylinux wheels, so no compiler is needed. curl is here
# for the healthcheck only; tini reaps the worker threads' children cleanly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch first, from the CPU index. Kept in its own layer: it is by far the
# largest dependency and it changes far less often than the app requirements.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.10.0

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY *.py ./
COPY static/ ./static/
# The curated profile set. The entrypoint copies this into the data volume the
# first time only, so edits made in the UI survive redeploys.
COPY profiles.example.json ./profiles.example.json
COPY profiles.json ./profiles.json.seed
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Every mutable path lives under a directory owned by the runtime user, so the
# named volumes mounted over them inherit that ownership instead of root's.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data /app/runs /app/page-cache /app/hf-cache \
    && chown -R app:app /app

ENV HOME=/home/app \
    HF_HOME=/app/hf-cache \
    RUNS_DIR=/app/runs \
    PROFILES_PATH=/app/data/profiles.json \
    PAGE_CACHE_DIR=/app/page-cache \
    HOST=0.0.0.0 \
    PORT=8000 \
    OMP_NUM_THREADS=2

USER app
EXPOSE 8000

# A run takes minutes and the model load alone can take 60s on a cold cache, so
# the start period is generous; the check itself only asks whether HTTP is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/api/status || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "server.py"]
