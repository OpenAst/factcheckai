import base64
import io
import logging
import os
import time
from typing import Optional

import easyocr
import numpy as np
from PIL import Image
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .logging_config import configure_logging
from .ocr_queue import get_ocr_job_payload, is_ocr_queue_available, pop_ocr_job, update_ocr_job


configure_logging()
logger = logging.getLogger(__name__)


EASYOCR_LANGS = [lang.strip() for lang in os.getenv("EASYOCR_LANGS", "en,uk").split(",") if lang.strip()]
EASYOCR_MODEL_DIR = os.getenv("EASYOCR_MODEL_DIR", "").strip() or None
OCR_MAX_IMAGE_DIMENSION = int(os.getenv("OCR_MAX_IMAGE_DIMENSION", "1200"))
OCR_DOWNLOAD_MODELS = os.getenv("OCR_DOWNLOAD_MODELS", "true").lower() == "true"

_reader: Optional[easyocr.Reader] = None


def _get_reader() -> easyocr.Reader:
    global _reader
    if _reader is None:
        logger.info(
            "initializing EasyOCR reader langs=%s model_dir=%s",
            EASYOCR_LANGS,
            EASYOCR_MODEL_DIR or "default",
        )
        _reader = easyocr.Reader(
            EASYOCR_LANGS,
            gpu=False,
            model_storage_directory=EASYOCR_MODEL_DIR,
            download_enabled=OCR_DOWNLOAD_MODELS,
        )
    return _reader


def _decode_image(image_data: str) -> np.ndarray:
    if "," in image_data:
        image_data = image_data.split(",", 1)[1]
    raw = base64.b64decode(image_data)
    image = Image.open(io.BytesIO(raw)).convert("RGB")

    width, height = image.size
    longest = max(width, height)
    if longest > OCR_MAX_IMAGE_DIMENSION:
        scale = OCR_MAX_IMAGE_DIMENSION / float(longest)
        image = image.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
        logger.info(
            "ocr image resized original_width=%s original_height=%s resized_width=%s resized_height=%s",
            width,
            height,
            image.size[0],
            image.size[1],
        )
    else:
        logger.info("ocr image decoded width=%s height=%s", width, height)

    return np.array(image)


def _extract_text(image_data: str) -> str:
    started_at = time.perf_counter()
    image = _decode_image(image_data)
    logger.info("ocr image ready shape=%s decode_duration_ms=%.2f", image.shape, (time.perf_counter() - started_at) * 1000)
    reader_started_at = time.perf_counter()
    reader = _get_reader()
    logger.info("ocr reader ready duration_ms=%.2f", (time.perf_counter() - reader_started_at) * 1000)
    read_started_at = time.perf_counter()
    results = reader.readtext(image, detail=0, paragraph=False)
    logger.info("ocr readtext completed result_count=%s duration_ms=%.2f", len(results), (time.perf_counter() - read_started_at) * 1000)
    cleaned = []
    seen = set()
    for entry in results:
        text = " ".join(str(entry).split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return "\n".join(cleaned).strip()


def run_worker() -> None:
    if not is_ocr_queue_available():
        raise RuntimeError("REDIS_URL is required for the OCR worker")

    logger.info("ocr worker started")
    while True:
        try:
            job_id = pop_ocr_job(timeout=5)
            if not job_id:
                continue

            payload = get_ocr_job_payload(job_id)
            if not payload:
                continue

            image_data = payload.get("image_data", "")
            if not image_data:
                update_ocr_job(job_id, status="failed", error="Missing image data")
                continue

            try:
                logger.info("ocr job processing job_id=%s", job_id)
                update_ocr_job(job_id, status="processing")
                result_text = _extract_text(image_data)
                update_ocr_job(job_id, status="completed", result_text=result_text)
                logger.info("ocr job completed job_id=%s result_chars=%s", job_id, len(result_text))
            except Exception as exc:
                try:
                    update_ocr_job(job_id, status="failed", error=str(exc))
                except (RedisConnectionError, RedisTimeoutError):
                    logger.warning("could not update failed OCR job status due to Redis connectivity", exc_info=True)
                logger.exception("ocr job failed job_id=%s error=%s", job_id, exc)
                time.sleep(1)
        except (RedisConnectionError, RedisTimeoutError) as exc:
            logger.warning("redis temporarily unavailable for OCR worker: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    run_worker()
