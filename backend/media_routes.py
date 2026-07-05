import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

try:
    from .media_ai import MediaAIService
except ImportError:
    from media_ai import MediaAIService

logger = logging.getLogger(__name__)
router = APIRouter()
media_ai_service = MediaAIService()


class MediaAnalyzeRequest(BaseModel):
    image_data: str
    post_text: str = ""


class MediaAnalyzeResponse(BaseModel):
    media_text: str = ""
    media_claim: str = ""
    search_terms: str = ""
    summary: str = ""


class AudioTranscribeRequest(BaseModel):
    audio_data: str
    post_text: str = ""


class AudioTranscribeResponse(BaseModel):
    transcript: str = ""
    summary: str = ""


@router.post("/media/analyze", response_model=MediaAnalyzeResponse)
def analyze_media(payload: MediaAnalyzeRequest):
    if not payload.image_data.strip():
        raise HTTPException(status_code=400, detail="image_data is required")
    logger.info(
        "media analysis requested image_chars=%s post_chars=%s",
        len(payload.image_data or ""),
        len(payload.post_text or ""),
    )
    result = media_ai_service.analyze_media(
        payload.image_data.strip(),
        post_text=payload.post_text.strip(),
    )
    return MediaAnalyzeResponse(**result)


@router.post("/audio/transcribe", response_model=AudioTranscribeResponse)
def transcribe_audio(payload: AudioTranscribeRequest):
    if not payload.audio_data.strip():
        raise HTTPException(status_code=400, detail="audio_data is required")
    logger.info(
        "audio transcription requested audio_chars=%s post_chars=%s",
        len(payload.audio_data or ""),
        len(payload.post_text or ""),
    )
    result = media_ai_service.transcribe_audio(
        payload.audio_data.strip(),
        post_text=payload.post_text.strip(),
    )
    return AudioTranscribeResponse(**result)
