import base64
import io
import logging
import os
import re
from importlib import import_module
from typing import Any, Dict

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

try:
    genai = import_module("google.genai")
except Exception:
    genai = None

try:
    Groq = import_module("groq").Groq
except Exception:
    Groq = None

load_dotenv()
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash-001",
]


class MediaAIService:
    def __init__(self):
        self.groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY and Groq else None
        self.gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY and genai else None

    def analyze_media(self, image_data: str, post_text: str = "") -> Dict[str, str]:
        """Use Gemini Vision to describe media and extract checkable visual claims."""
        if not self.gemini_client or not genai:
            return {
                "media_text": "",
                "media_claim": "",
                "search_terms": "",
                "summary": "Media analysis unavailable because GEMINI_API_KEY/google-genai is not configured.",
            }

        mime_type = "image/png"
        payload = image_data or ""
        if payload.startswith("data:"):
            header, payload = payload.split(",", 1)
            match = re.match(r"data:([^;]+);base64", header)
            if match:
                mime_type = match.group(1)

        try:
            image_bytes = base64.b64decode(payload)
        except Exception as exc:
            return {
                "media_text": "",
                "media_claim": "",
                "search_terms": "",
                "summary": f"Could not decode captured image: {exc}",
            }

        prompt = f"""You are assisting a fact-checker.
Analyze the attached screenshot/image from a social media review queue.

Use the image plus this extracted post text as context:
{post_text}

Return exactly:
VISIBLE_TEXT: <important visible text in the image, preserving source language when useful>
IMAGE_DESCRIPTION: <one sentence describing what the image appears to show>
MEDIA_CLAIM: <the factual claim the image is being used to support, or blank if none>
SEARCH_TERMS: <5-12 words/names/phrases useful for web/image verification>
VERIFICATION_NOTES: <what a rater should verify about this image, e.g. origin, old image, location, event, manipulation>
"""
        try:
            types = getattr(genai, "types", None) or import_module("google.genai.types")
            part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            response = self.gemini_client.models.generate_content(
                model=GEMINI_MODELS[0],
                contents=[part, prompt],
            )
            text = getattr(response, "text", "") or ""
        except Exception as exc:
            logger.warning("Gemini media analysis failed error=%s", exc)
            return {
                "media_text": "",
                "media_claim": "",
                "search_terms": "",
                "summary": f"Media analysis failed: {exc}",
            }

        parsed = {
            "media_text": "",
            "media_claim": "",
            "search_terms": "",
            "summary": text.strip(),
        }
        labels = {
            "VISIBLE_TEXT:": "media_text",
            "MEDIA_CLAIM:": "media_claim",
            "SEARCH_TERMS:": "search_terms",
        }
        for line in text.splitlines():
            for prefix, key in labels.items():
                if line.startswith(prefix):
                    parsed[key] = line.split(":", 1)[1].strip()
        return parsed

    def transcribe_audio(self, audio_data: str, post_text: str = "") -> Dict[str, str]:
        """Transcribe a captured tab-audio recording with Groq Whisper."""
        if not self.groq_client:
            return {
                "transcript": "",
                "summary": "Audio transcription unavailable because GROQ_API_KEY/groq is not configured.",
            }

        mime_type = "audio/webm"
        payload = audio_data or ""
        if payload.startswith("data:"):
            header, payload = payload.split(",", 1)
            match = re.match(r"data:([^;]+);base64", header)
            if match:
                mime_type = match.group(1)

        try:
            audio_bytes = base64.b64decode(payload)
        except Exception as exc:
            return {
                "transcript": "",
                "summary": f"Could not decode captured audio: {exc}",
            }

        extension = "webm"
        if "mp4" in mime_type:
            extension = "mp4"
        elif "mpeg" in mime_type or "mp3" in mime_type:
            extension = "mp3"
        elif "wav" in mime_type:
            extension = "wav"
        elif "ogg" in mime_type:
            extension = "ogg"

        try:
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = f"tab-audio.{extension}"
            response = self.groq_client.audio.transcriptions.create(
                file=audio_file,
                model="whisper-large-v3",
                response_format="json",
                temperature=0,
            )
            transcript = response.get("text", "") if isinstance(response, dict) else getattr(response, "text", "")
            transcript = (transcript or "").strip()
            return {
                "transcript": transcript,
                "summary": "Audio transcription completed." if transcript else "No speech was detected in the captured audio.",
            }
        except Exception as exc:
            logger.warning("Groq audio transcription failed error=%s", exc)
            return {
                "transcript": "",
                "summary": f"Audio transcription failed: {exc}",
            }
