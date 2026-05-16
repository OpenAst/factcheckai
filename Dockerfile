FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/storage


FROM base AS web

COPY requirements-web.txt ./
RUN pip install -r requirements-web.txt
COPY backend ./backend

ENV PORT=8000 \
    CACHE_DB_PATH=/app/storage/cache.db

EXPOSE 8000

CMD ["sh", "-c", "gunicorn -k uvicorn.workers.UvicornWorker backend.main:app --bind 0.0.0.0:${PORT:-8000} --timeout 90"]


FROM base AS worker

COPY requirements-worker.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.7.0 torchvision==0.22.0 \
    && pip install --no-deps -r requirements-worker.txt
COPY backend ./backend

ENV EASYOCR_LANGS=en \
    OCR_MAX_IMAGE_DIMENSION=1600 \
    OCR_DOWNLOAD_MODELS=true \
    EASYOCR_MODEL_DIR=/app/storage/easyocr

CMD ["python", "-m", "backend.ocr_worker"]
