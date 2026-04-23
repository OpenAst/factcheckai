import json
import os
import time
import uuid
from typing import Dict, Optional

from redis import Redis


REDIS_URL = os.getenv("REDIS_URL", "").strip()
OCR_QUEUE_NAME = os.getenv("OCR_QUEUE_NAME", "ocr_jobs")
OCR_JOB_TTL_SECONDS = int(os.getenv("OCR_JOB_TTL_SECONDS", "1800"))

_redis_client: Optional[Redis] = None


def _get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        if not REDIS_URL:
            raise RuntimeError("REDIS_URL is not configured")
        _redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def is_ocr_queue_available() -> bool:
    return bool(REDIS_URL)


def _job_key(job_id: str) -> str:
    return f"ocr_job:{job_id}"


def submit_ocr_job(image_data: str, metadata: Optional[Dict] = None) -> str:
    redis_client = _get_redis()
    job_id = uuid.uuid4().hex
    now = str(int(time.time()))
    redis_client.hset(
        _job_key(job_id),
        mapping={
            "status": "queued",
            "image_data": image_data,
            "result_text": "",
            "error": "",
            "metadata_json": json.dumps(metadata or {}),
            "created_at": now,
            "updated_at": now,
        },
    )
    redis_client.expire(_job_key(job_id), OCR_JOB_TTL_SECONDS)
    redis_client.rpush(OCR_QUEUE_NAME, job_id)
    return job_id


def get_ocr_job(job_id: str) -> Optional[Dict]:
    redis_client = _get_redis()
    payload = redis_client.hgetall(_job_key(job_id))
    if not payload:
        return None
    try:
        metadata = json.loads(payload.get("metadata_json", "") or "{}")
    except Exception:
        metadata = {}
    return {
        "job_id": job_id,
        "status": payload.get("status", "unknown"),
        "result_text": payload.get("result_text", ""),
        "error": payload.get("error", ""),
        "created_at": payload.get("created_at", ""),
        "updated_at": payload.get("updated_at", ""),
        "metadata": metadata,
    }


def pop_ocr_job(timeout: int = 5) -> Optional[str]:
    redis_client = _get_redis()
    item = redis_client.blpop(OCR_QUEUE_NAME, timeout=timeout)
    if not item:
        return None
    _, job_id = item
    return job_id


def get_ocr_job_payload(job_id: str) -> Optional[Dict]:
    redis_client = _get_redis()
    payload = redis_client.hgetall(_job_key(job_id))
    if not payload:
        return None
    return payload


def update_ocr_job(job_id: str, status: str, result_text: str = "", error: str = "") -> None:
    redis_client = _get_redis()
    redis_client.hset(
        _job_key(job_id),
        mapping={
            "status": status,
            "result_text": result_text,
            "error": error,
            "updated_at": str(int(time.time())),
        },
    )
    redis_client.expire(_job_key(job_id), OCR_JOB_TTL_SECONDS)
