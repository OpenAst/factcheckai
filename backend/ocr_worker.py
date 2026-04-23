import base64
import io
import os
import time
from typing import Optional

import easyocr
import numpy as np
from PIL import Image

from .ocr_queue import get_ocr_job_payload, is_ocr_queue_available, pop_ocr_job, update_ocr_job


EASYOCR_LANGS = [lang.strip() for lang in os.getenv("EASYOCR_LANGS", "en").split(",") if lang.strip()]
EASYOCR_MODEL_DIR = os.getenv("EASYOCR_MODEL_DIR", "").strip() or None
OCR_MAX_IMAGE_DIMENSION = int(os.getenv("OCR_MAX_IMAGE_DIMENSION", "1600"))
OCR_DOWNLOAD_MODELS = os.getenv("OCR_DOWNLOAD_MODELS", "true").lower() == "true"

_reader: Optional[easyocr.Reader] = None


def _get_reader() -> easyocr.Reader:
    global _reader
    if _reader is None:
        print(f"[ocr-worker] Initializing EasyOCR reader langs={EASYOCR_LANGS} model_dir={EASYOCR_MODEL_DIR or 'default'}")
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

    return np.array(image)


def _extract_text(image_data: str) -> str:
    image = _decode_image(image_data)
    reader = _get_reader()
    results = reader.readtext(image, detail=0, paragraph=False)
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

    print("[ocr-worker] Worker started")
    while True:
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
            print(f"[ocr-worker] Processing job {job_id}")
            update_ocr_job(job_id, status="processing")
            result_text = _extract_text(image_data)
            update_ocr_job(job_id, status="completed", result_text=result_text)
            print(f"[ocr-worker] Completed job {job_id} chars={len(result_text)}")
        except Exception as exc:
            update_ocr_job(job_id, status="failed", error=str(exc))
            print(f"[ocr-worker] Job {job_id} failed: {exc}")
            time.sleep(1)


if __name__ == "__main__":
    run_worker()
