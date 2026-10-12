# DaoCloud mirror of Docker Hub's python:3.13-slim (Docker Hub is slow/blocked in mainland China).
# Override with BASE_IMAGE in .env, e.g. python:3.13-slim outside China.
ARG BASE_IMAGE=m.daocloud.io/docker.io/library/python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# Dependencies first so code edits don't invalidate this layer.
COPY requirements.txt .
RUN pip install --index-url "${PIP_INDEX_URL}" -r requirements.txt

# Non-root user; /app/data holds state.json and is mounted as a volume.
RUN useradd --system --uid 10001 --home-dir /app zcare \
    && mkdir -p /app/data && chown zcare:zcare /app/data

COPY --chown=zcare:zcare . .

USER zcare

EXPOSE 8000

# slim has no curl; use the stdlib.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# One process only: each extra worker would start its own scheduler (duplicate messages).
CMD ["python", "main.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
