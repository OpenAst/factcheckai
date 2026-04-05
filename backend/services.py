import os
import requests
import google.generativeai as genai
from groq import Groq
from typing import List, Dict
from dotenv import load_dotenv
import base64
import io
from urllib.parse import urlparse
try:
    from duckduckgo_search import ddg
except Exception:
    ddg = None

try:
    from google.cloud import vision
except Exception:
    vision = None

load_dotenv()

# Configure APIs
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Optional comma-separated list of reliable news domains (e.g. cnn.com,bbc.co.uk)
RELIABLE_NEWS_DOMAINS = [d.strip().lower() for d in os.getenv("RELIABLE_NEWS_DOMAINS", "").split(",") if d.strip()]

# Domains we treat as social media / user-generated content and want to exclude
SOCIAL_DOMAINS = [
    'twitter.com', 't.co', 'facebook.com', 'instagram.com', 'reddit.com',
    'youtube.com', 'youtu.be', 'linkedin.com', 'tiktok.com', 'snapchat.com'
]


def _normalize_netloc(link: str) -> str:
    try:
        netloc = urlparse(link).netloc.lower()
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ''


def _is_social_link(link: str) -> bool:
    netloc = _normalize_netloc(link)
    return any(s == netloc or netloc.endswith('.' + s) or s in netloc for s in SOCIAL_DOMAINS)


def _is_preferred_news(link: str) -> bool:
    if not RELIABLE_NEWS_DOMAINS:
        return False
    netloc = _normalize_netloc(link)
    return any(netloc == d or netloc.endswith('.' + d) for d in RELIABLE_NEWS_DOMAINS)


def filter_search_results(results: List[Dict], max_results: int = 5) -> List[Dict]:
    # Exclude obvious social/user-generated links
    filtered = [r for r in results if r.get('link') and not _is_social_link(r.get('link'))]
    # Prefer reliable news domains if provided
    if RELIABLE_NEWS_DOMAINS:
        preferred = [r for r in filtered if _is_preferred_news(r.get('link'))]
        others = [r for r in filtered if not _is_preferred_news(r.get('link'))]
        ordered = preferred + others
    else:
        ordered = filtered
    return ordered[:max_results]

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
            organic = results.get("organic", [])
            parsed = [
                {
                    "title": r.get("title", ""),
                    "link": r.get("link", ""),
                    "snippet": r.get("snippet", "")
                }
                for r in organic
            ]
            return filter_search_results(parsed)
        except Exception as e:
            print(f"Serper search error: {e}")
            return []


class DuckDuckGoService:
    @staticmethod
    def search(query: str, max_results: int = 5) -> List[Dict]:
        """Use duckduckgo_search.ddg if installed to get organic results.
        Returns list of dicts with keys: title, link, snippet
        """
        if ddg is None:
            print('duckduckgo_search not available')
            return []
        try:
            results = ddg(query, max_results=max_results)
            out = []
            for r in results:
                out.append({
                    'title': r.get('title') or r.get('text') or '',
                    'link': r.get('href') or r.get('link') or r.get('url') or '',
                    'snippet': r.get('body') or r.get('snippet') or ''
                })
            return filter_search_results(out)
        except Exception as e:
            print('DuckDuckGo search error:', e)
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
        # Prefer Google Cloud Vision if available and configured
        if vision is not None:
            try:
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
            except Exception as e:
                print('Google Vision failed:', e)

        # Fallback: try local OCR with pytesseract if google vision not available
        try:
            from PIL import Image
            import pytesseract
            img = Image.open(io.BytesIO(image_bytes))
            text = pytesseract.image_to_string(img)
            return {'text': text or '', 'web_entities': []}
        except Exception as e:
            print('Pytesseract OCR fallback failed:', e)
            raise Exception('No OCR available: install google-cloud-vision with credentials or pytesseract + pillow')

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
