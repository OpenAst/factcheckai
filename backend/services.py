import os
import requests
import google.generativeai as genai
from groq import Groq
from typing import List, Dict
from dotenv import load_dotenv
import base64

try:
    from google.cloud import vision
except Exception:
    vision = None

load_dotenv()

# Configure APIs
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Groq models (primary - free tier)
GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # Best quality free model
    "llama-3.1-8b-instant",      # Fast fallback
    "mixtral-8x7b-32768",        # Alternative fallback
]

# Gemini models (secondary fallback if Groq fails)
GEMINI_MODELS = [
    "gemini-2.0-flash-001",
    "gemini-2.0-flash",
    "gemini-1.5-flash-latest",
]

class SerperService:
    @staticmethod
    def search(query: str) -> List[Dict]:
        url = "https://google.serper.dev/search"
        payload = {"q": query}
        headers = {
            'X-API-KEY': SERPER_API_KEY,
            'Content-Type': 'application/json'
        }
        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            results = response.json()
            organic = results.get("organic", [])[:5]
            return [
                {
                    "title": r.get("title", ""),
                    "link": r.get("link", ""),
                    "snippet": r.get("snippet", "")
                }
                for r in organic
            ]
        except Exception as e:
            print(f"Serper search error: {e}")
            return []

class GeminiService:
    def __init__(self):
        self.groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

    def _call_groq(self, prompt: str) -> str:
        """Try Groq models (free, fast)."""
        if not self.groq_client:
            raise Exception("No GROQ_API_KEY set")
        last_error = None
        for model in GROQ_MODELS:
            try:
                print(f"Trying Groq model: {model}")
                response = self.groq_client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1024,
                )
                return response.choices[0].message.content
            except Exception as e:
                last_error = str(e)
                print(f"Groq model {model} failed: {last_error}")
                continue
        raise Exception(f"All Groq models failed: {last_error}")

    def _call_gemini(self, prompt: str) -> str:
        """Try Gemini models as fallback."""
        last_error = None
        for model_name in GEMINI_MODELS:
            try:
                print(f"Trying Gemini model: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                return response.text
            except Exception as e:
                last_error = str(e)
                print(f"Gemini model {model_name} failed: {last_error}")
                continue
        raise Exception(f"All Gemini models failed: {last_error}")

    def _call_model(self, prompt: str) -> str:
        """Try Groq first, then Gemini as fallback."""
        try:
            return self._call_groq(prompt)
        except Exception as groq_err:
            print(f"Groq failed, trying Gemini: {groq_err}")
            try:
                return self._call_gemini(prompt)
            except Exception as gemini_err:
                return f"AI error (all providers failed): Groq: {groq_err} | Gemini: {gemini_err}"

    def extract_claim(self, text: str) -> str:
        """Use AI to isolate the single main factual claim from the text."""
        prompt = f"""You are a fact-checking assistant. From the text below, identify and extract the single most important VERIFIABLE FACTUAL CLAIM.
Output ONLY the claim as a short sentence (max 2 sentences). Do NOT add any commentary or explanation.

TEXT:
{text}

MAIN CLAIM:"""
        result = self._call_model(prompt)
        claim = result.strip().replace("MAIN CLAIM:", "").strip()
        return claim if claim else text

    def fact_check(self, claim: str, search_results: List[Dict]) -> str:
        """Analyze the claim against search results and produce a verdict."""
        context = ""
        for i, res in enumerate(search_results):
            context += f"Source {i+1}: {res.get('title')}\n"
            context += f"Snippet: {res.get('snippet')}\n"
            context += f"Link: {res.get('link')}\n\n"

        prompt = f"""You are an expert fact-checker for the SRT (Social Responsibility Tools) platform.
Analyze the following claim using the provided search results.

CLAIM:
{claim}

SEARCH RESULTS:
{context}

YOUR TASK:
1. Determine the truthfulness of the claim.
2. CRITICAL: Identify the DATE and CURRENCY of the news. Is this a current event or old news being reshared?
3. Evaluate if the claim uses a "True" event in a "Misleading" or "Out of Context" way.
4. Provide a structured report in Markdown.

STRUCTURE:
- **Verdict**: (Choose one: True, False, Misleading, Out of Context, Mixed, or Unverified)
- **Summary**: (2-3 sentences explaining the core finding)
- **Key Points**: (Bullet points with supporting facts)
- **Date Check**: (Explicitly state if the event is current or from the past)

If search results are empty or irrelevant, state "Unverified" and explain why."""
        return self._call_model(prompt)


class VisionService:
    """Simple wrapper around Google Cloud Vision for OCR and basic web detection.
    Requires the `google-cloud-vision` package and credentials set via
    `GOOGLE_APPLICATION_CREDENTIALS` or default application credentials.
    """
    @staticmethod
    def ocr_image_bytes(image_bytes: bytes) -> Dict:
        if vision is None:
            raise Exception("google-cloud-vision not installed or could not be imported")
        client = vision.ImageAnnotatorClient()
        image = vision.Image(content=image_bytes)
        # Use DOCUMENT_TEXT_DETECTION for denser text (good for overlaid text)
        resp = client.document_text_detection(image=image)
        text = ''
        try:
            text = resp.full_text_annotation.text if resp.full_text_annotation and resp.full_text_annotation.text else ''
        except Exception:
            text = ''

        # web detection (optional) to find similar pages and context
        web_entities = []
        try:
            web = client.web_detection(image=image).web_detection
            if web and web.web_entities:
                for e in web.web_entities[:5]:
                    web_entities.append({
                        'description': e.description,
                        'score': getattr(e, 'score', None)
                    })
        except Exception:
            web_entities = []

        return {'text': text or '', 'web_entities': web_entities}

    @staticmethod
    def ocr_data_url(data_url: str) -> Dict:
        # data_url like: data:image/png;base64,....
        if not data_url:
            return {'text': '', 'web_entities': []}
        try:
            header, b64 = data_url.split(',', 1)
            image_bytes = base64.b64decode(b64)
            return VisionService.ocr_image_bytes(image_bytes)
        except Exception as e:
            print('VisionService ocr_data_url error:', e)
            return {'text': '', 'web_entities': []}
