# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/index.html frontend/tsconfig*.json frontend/vite.config.ts ./
COPY frontend/public ./public
COPY frontend/src ./src
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

FROM python:3.12-slim AS runtime

ARG INSTALL_LOCAL_ASR=false
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    FRAMEFLOW_DATA_DIR=/data \
    FRAMEFLOW_DATABASE_URL=sqlite:////data/frameflow.db \
    FRAMEFLOW_FRONTEND_DIR=/app/frontend \
    HF_HOME=/data/models/huggingface

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements-asr-local.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt \
    && if [ "${INSTALL_LOCAL_ASR}" = "true" ]; then pip install -r requirements-asr-local.txt; fi

COPY backend/app ./app
COPY backend/seed_media ./seed_media
COPY --from=frontend-builder /build/frontend/dist ./frontend

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin frameflow \
    && mkdir -p /data \
    && chown -R frameflow:frameflow /app /data

USER frameflow
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=20s --timeout=4s --start-period=30s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.serve"]
