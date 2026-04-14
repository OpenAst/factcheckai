import os
import requests
from google import genai
from google.genai import types
from groq import Groq
from typing import List, Dict
from dotenv import load_dotenv
import base64
import io
from urllib.parse import urlparse
try:
    from duckduckgo_search import DDGS
except Exception:
    DDGS = None

try:
    from google.cloud import vision
except Exception:
    vision = None

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Optional comma-separated list of reliable news domains (e.g. cnn.com,bbc.co.uk)
RELIABLE_NEWS_DOMAINS = [d.strip().lower() for d in os.getenv("RELIABLE_NEWS_DOMAINS", "").split(",") if d.strip()]

PRIORITY_FACTCHECK_DOMAINS = [
    "politifact.com",
    "factcheck.org",
    "apnews.com",
    "africacheck.org",
    "leadstories.com",
    "snopes.com",
    "fullfact.org",
    "reuters.com",
    "afp.com",
]

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


def _domain_matches(netloc: str, domain: str) -> bool:
    return netloc == domain or netloc.endswith("." + domain)


def _source_priority_score(link: str) -> int:
    netloc = _normalize_netloc(link)
    if not netloc:
        return 99

    for idx, domain in enumerate(PRIORITY_FACTCHECK_DOMAINS):
        if _domain_matches(netloc, domain):
            return idx

    if RELIABLE_NEWS_DOMAINS:
        for idx, domain in enumerate(RELIABLE_NEWS_DOMAINS, start=20):
            if _domain_matches(netloc, domain):
                return idx

    if netloc.endswith(".gov") or netloc.endswith(".edu"):
        return 40

    return 80


def filter_search_results(results: List[Dict], max_results: int = 5) -> List[Dict]:
    # Exclude obvious social/user-generated links
    filtered = [r for r in results if r.get('link') and not _is_social_link(r.get('link'))]
    ordered = sorted(
        filtered,
        key=lambda r: (
            _source_priority_score(r.get("link", "")),
            len(r.get("snippet", "")) == 0,
            len(r.get("title", "")) == 0,
        ),
    )
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
        """Use duckduckgo_search if installed to get organic results.
        Returns list of dicts with keys: title, link, snippet
        """
        if DDGS is None:
            print('duckduckgo_search not available')
            return []
        try:
            results = DDGS().text(query, max_results=max_results)
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
        self.gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

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
        if not self.gemini_client:
            raise Exception("No GEMINI_API_KEY set")
        last_error = None
        for model_name in GEMINI_MODELS:
            try:
                print(f"Trying Gemini model: {model_name}")
                response = self.gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                return getattr(response, "text", "") or ""
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

    def fact_check(
        self,
        claim: str,
        search_results: List[Dict],
        original_text: str = "",
        suspected_author: str = "",
    ) -> str:
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

ORIGINAL POST TEXT:
{original_text or claim}

SUSPECTED AUTHOR:
{suspected_author or "Unknown / not clearly stated"}

SEARCH RESULTS:
{context}

YOUR TASK:
1. First determine whether the post or statement is authentically attributable to the suspected author.
2. Use reliable news reports, fact-checkers, official records, or direct primary-source reporting for attribution. Do not treat random reposts, social embeds, or unsourced blogs as proof.
3. If the search results do NOT reliably confirm the author actually made the post, make that the central finding and mark the content as Unverified, False, or Misleading as appropriate.
4. If the attribution appears supported, then evaluate the truthfulness of the factual claims inside the post.
5. If the post contains strong false claims, prioritize the most direct evidence that refutes those claims.
6. CRITICAL: Identify the DATE and CURRENCY of the news. Is this a current event or old news being reshared?
7. Evaluate if the claim uses a "True" event in a "Misleading" or "Out of Context" way.
8. If the sources are only background explainers and do not directly verify the claim, say so and lower confidence.
9. Provide a structured report in Markdown.

STRUCTURE:
- **Verdict**: (Choose one: True, False, Misleading, Out of Context, Mixed, or Unverified)
- **Summary**: (2-3 sentences explaining the core finding, starting with whether the attribution is verified)
- **Attribution Check**: (State whether reliable reporting confirms the named author really made the post/statement)
- **Key Points**: (Bullet points with supporting facts, prioritizing direct refuting evidence when the claims are false)
- **Date Check**: (Explicitly state if the event is current or from the past)

If search results are empty or irrelevant, state "Unverified" and explain why."""
        return self._call_model(prompt)


class VisionService:
    """Simple wrapper around Google Cloud Vision for OCR and basic web detection.
    Requires the `google-cloud-vision` package and credentials set via
    `GOOGLE_APPLICATION_CREDENTIALS` or default application credentials.
    """
    @staticmethod
    def _vision_credentials_available() -> bool:
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not credentials_path:
            return False
        return os.path.isfile(credentials_path)

    @staticmethod
    def _ocr_with_gemini(image_bytes: bytes, mime_type: str = "image/png") -> Dict:
        if not GEMINI_API_KEY:
            return {'text': '', 'web_entities': []}

        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[
                    "Extract all readable text from this image, especially overlaid post text. "
                    "Return only the detected text, preserving line breaks as much as possible. "
                    "If no readable text is visible, return an empty response.",
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                ],
            )
            text = (getattr(response, "text", "") or "").strip()
            return {'text': text, 'web_entities': []}
        except Exception as e:
            print('Gemini OCR fallback failed:', e)
            return {'text': '', 'web_entities': []}

    @staticmethod
    def ocr_image_bytes(image_bytes: bytes, mime_type: str = "image/png") -> Dict:
        # Prefer Google Cloud Vision if available and configured
        if vision is not None and VisionService._vision_credentials_available():
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
        elif vision is not None and os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            print('Skipping Google Vision: GOOGLE_APPLICATION_CREDENTIALS does not point to a valid file')

        gemini_result = VisionService._ocr_with_gemini(image_bytes, mime_type=mime_type)
        if gemini_result.get('text'):
            return gemini_result

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
            mime_type = "image/png"
            if header.startswith("data:") and ";" in header:
                mime_type = header[5:].split(";", 1)[0] or mime_type
            image_bytes = base64.b64decode(b64)
            return VisionService.ocr_image_bytes(image_bytes, mime_type=mime_type)
        except Exception as e:
            print('VisionService ocr_data_url error:', e)
            return {'text': '', 'web_entities': []}
