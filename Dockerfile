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

ENV LOG_LEVEL=INFO \
    PORT=8000 \
    CACHE_DB_PATH=/app/storage/cache.db

EXPOSE 8000

CMD ["sh", "-c", "gunicorn -k uvicorn.workers.UvicornWorker backend.main:app --bind 0.0.0.0:${PORT:-8000} --timeout 90"]
