import os
import requests
from google import genai
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
    import easyocr
except Exception:
    easyocr = None

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")

# Optional comma-separated list of reliable news domains (e.g. cnn.com,bbc.co.uk)
RELIABLE_NEWS_DOMAINS = [d.strip().lower() for d in os.getenv("RELIABLE_NEWS_DOMAINS", "").split(",") if d.strip()]

PRIORITY_FACTCHECK_DOMAINS = [
    "politifact.com",
    "reuters.com",
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
    'youtube.com', 'youtu.be', 'linkedin.com', 'tiktok.com', 'snapchat.com', 
    'threads.com', 'x.com'
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

# Gemini models
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
        self.gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

    def _call_gemini(self, prompt: str) -> str:
        """Try Gemini models."""
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
        """Use Gemini only."""
        try:
            return self._call_gemini(prompt)
        except Exception as gemini_err:
            return f"AI error (Gemini failed): {gemini_err}"

    def extract_claim(self, text: str) -> str:
        """Use AI to isolate the single main factual claim from the text."""
        prompt = f"""You are a senior fact-checking assistant. From the text below, identify and extract the single most important VERIFIABLE FACTUAL CLAIM.

Rules:
- Prefer the most consequential and specific factual assertion, not a vague topic summary.
- Do NOT just restate who posted the content unless authorship itself is the main checkable claim.
- If the text contains several factual statements, choose the one that would matter most to verify for misinformation review.
- Keep concrete names, places, dates, numbers, actions, and outcomes when present.

Output ONLY the claim as a short sentence (max 2 sentences). Do NOT add any commentary or explanation.

TEXT:
{text}

MAIN CLAIM:"""
        result = self._call_model(prompt)
        claim = result.strip().replace("MAIN CLAIM:", "").strip()
        return claim if claim else text

    def extract_claims(self, text: str, max_claims: int = 3) -> List[str]:
        """Extract up to three fact-checkable claims, ordered by importance."""
        prompt = f"""You are a senior fact-checking assistant.
From the text below, extract up to {max_claims} distinct VERIFIABLE FACTUAL CLAIMS.

Rules:
- Return 2 claims when there are clearly 2 meaningful factual claims.
- Return 3 claims only when there are 3 genuinely distinct and important checkable claims.
- Prefer consequential, specific claims over vague summaries.
- Do NOT include opinion, rhetoric, or pure attribution unless authorship itself is a factual claim.
- Keep each claim short, concrete, and standalone.
- If there is only 1 real factual claim, return just 1.
- If there is no factual claim, return NO_CLAIM.

Return exactly in this format:
CLAIM: <claim 1>
CLAIM: <claim 2>
CLAIM: <claim 3>

TEXT:
{text}
"""
        result = self._call_model(prompt)
        claims = []
        for line in (result or "").splitlines():
            if line.strip().upper() == "NO_CLAIM":
                return []
            if line.startswith("CLAIM:"):
                claim = line.split(":", 1)[1].strip()
                if claim and claim not in claims:
                    claims.append(claim)
        return claims[:max_claims]

    def classify_claimability(self, text: str) -> Dict[str, str]:
        """Classify whether text contains a fact-checkable claim."""
        prompt = f"""You are helping a fact-checking workflow.
Decide whether the text contains a clear verifiable factual claim.

Rules:
- Use NO_CLAIM when the text is mainly opinion, insult, praise, emotion, advice, satire, vague rhetoric, or personal preference.
- Use FACTUAL_CLAIM when the text contains a specific claim that can be checked against evidence.
- Use MIXED when the text mixes opinion with at least one checkable factual claim.
- If MIXED, extract only the strongest and most consequential checkable factual claim.
- Prefer the deepest factual assertion, not a surface-level paraphrase.
- Do NOT select mere authorship or attribution as the claim unless the post is fundamentally about whether a named person made a statement.
- If NO_CLAIM, leave the claim blank.

Return exactly in this format:
STATUS: <NO_CLAIM or FACTUAL_CLAIM or MIXED>
CLAIM: <short extracted claim or blank>
REASON: <one short sentence>

TEXT:
{text}
"""
        result = self._call_model(prompt)
        status = "FACTUAL_CLAIM"
        claim = ""
        reason = ""
        for line in (result or "").splitlines():
            if line.startswith("STATUS:"):
                status = line.split(":", 1)[1].strip().upper() or status
            elif line.startswith("CLAIM:"):
                claim = line.split(":", 1)[1].strip()
            elif line.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()

        if status not in {"NO_CLAIM", "FACTUAL_CLAIM", "MIXED"}:
            status = "FACTUAL_CLAIM"
        if status == "NO_CLAIM":
            claim = ""
        return {
            "status": status.lower(),
            "claim": claim,
            "reason": reason or "The model did not provide a reason.",
        }

    def fact_check(
        self,
        claim: str,
        search_results: List[Dict],
        original_text: str = "",
        suspected_author: str = "",
        prioritize_authorship: bool = False,
    ) -> str:
        """Analyze the claim against search results and produce a verdict."""
        context = ""
        for i, res in enumerate(search_results):
            context += f"Source {i+1}: {res.get('title')}\n"
            context += f"Snippet: {res.get('snippet')}\n"
            context += f"Link: {res.get('link')}\n\n"

        task_steps = """1. Determine the truthfulness of the main factual claim.
2. If this appears to be an attributed post, separately assess whether reliable reporting confirms the named author actually made the post or statement.
3. Do not let attribution distract from the main factual claim unless attribution itself is the main thing being checked.
4. If the post contains strong false claims, prioritize the most direct evidence that refutes those claims.
5. CRITICAL: Identify the DATE and CURRENCY of the news. Is this a current event or old news being reshared?
6. Evaluate if the claim uses a "True" event in a "Misleading" or "Out of Context" way.
7. Use direct fact-checks, wire reports, official records, or primary-source reporting over generic commentary.
8. If the sources are only background explainers and do not directly verify the claim, say so and lower confidence.
9. Provide a structured report in Markdown."""

        if prioritize_authorship:
            task_steps = """1. Determine the truthfulness of the main factual claim.
2. Because this appears to be an attributed social post, also check whether reliable reporting confirms the named author actually made the post or statement.
3. If attribution is unsupported, clearly say that, but still evaluate the substance of the factual claim when the sources allow it.
4. If the post contains strong false claims, prioritize the most direct evidence that refutes those claims.
5. CRITICAL: Identify the DATE and CURRENCY of the news. Is this a current event or old news being reshared?
6. Evaluate if the claim uses a "True" event in a "Misleading" or "Out of Context" way.
7. Use direct fact-checks, wire reports, official records, or primary-source reporting over generic commentary.
8. If the sources are only background explainers and do not directly verify the claim, say so and lower confidence.
9. Provide a structured report in Markdown."""

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
{task_steps}

STRUCTURE:
- **Verdict**: (Choose one: True, False, Misleading, Out of Context, Mixed, or Unverified)
- **Summary**: (2-3 sentences explaining the core finding, starting with the main factual finding)
- **Attribution Check**: (Only mention this if attribution is actually relevant to the case)
- **Key Points**: (Bullet points with supporting facts, prioritizing direct refuting evidence when the claims are false)
- **Date Check**: (Explicitly state if the event is current or from the past)

If search results are empty or irrelevant, state "Unverified" and explain why."""
        return self._call_model(prompt)


class VisionService:
    """OCR helper built around EasyOCR."""

    @staticmethod
    def _log(message: str):
        print(f"[VisionService] {message}")

    _easyocr_reader = None

    @staticmethod
    def _preprocess_image_bytes(image_bytes: bytes) -> List[bytes]:
        """Generate a few OCR-friendly variants for screenshot text."""
        try:
            from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        except Exception:
            return [image_bytes]

        variants: List[bytes] = [image_bytes]
        try:
            original = Image.open(io.BytesIO(image_bytes)).convert("RGB")

            boosted = original.resize(
                (max(1, original.width * 2), max(1, original.height * 2)),
                Image.Resampling.LANCZOS,
            )
            gray = ImageOps.grayscale(boosted)
            contrast = ImageEnhance.Contrast(gray).enhance(1.8)
            sharpened = contrast.filter(ImageFilter.SHARPEN)
            thresholded = sharpened.point(lambda px: 255 if px > 165 else 0)

            for img in (boosted, sharpened, thresholded):
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                variants.append(buf.getvalue())
        except Exception as e:
            VisionService._log(f"Image preprocessing failed: {e}")

        return variants

    @staticmethod
    def _get_easyocr_reader():
        if easyocr is None:
            message = "EasyOCR package is unavailable. Install easyocr, torch, and torchvision."
            VisionService._log(message)
            raise RuntimeError(message)
        if VisionService._easyocr_reader is not None:
            return VisionService._easyocr_reader

        try:
            languages_raw = os.getenv("EASYOCR_LANGS", "en")
            languages = [lang.strip() for lang in languages_raw.split(",") if lang.strip()]
            VisionService._log(f"Initializing EasyOCR with langs={languages}")
            VisionService._easyocr_reader = easyocr.Reader(languages, gpu=False)
            return VisionService._easyocr_reader
        except Exception as e:
            message = f"Failed to initialize EasyOCR: {e}"
            VisionService._log(message)
            raise RuntimeError(message)

    @staticmethod
    def _ocr_with_easyocr(image_bytes: bytes) -> Dict:
        reader = VisionService._get_easyocr_reader()

        try:
            from PIL import Image
            import numpy as np

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            result = reader.readtext(np.array(image), detail=0, paragraph=False)
            lines = [(item or "").strip() for item in (result or []) if (item or "").strip()]

            combined = "\n".join(lines).strip()
            VisionService._log(f"EasyOCR returned {len(combined)} characters")
            return {'text': combined, 'web_entities': []}
        except Exception as e:
            VisionService._log(f"EasyOCR failed: {e}")
            return {'text': '', 'web_entities': []}

    @staticmethod
    def ocr_image_bytes(image_bytes: bytes, mime_type: str = "image/png") -> Dict:
        prepared_images = VisionService._preprocess_image_bytes(image_bytes)
        VisionService._log(
            f"OCR request received: original_bytes={len(image_bytes)} mime_type={mime_type} prepared_variants={len(prepared_images)}"
        )

        best_easyocr = {'text': '', 'web_entities': []}
        for idx, candidate in enumerate(prepared_images, start=1):
            VisionService._log(f"Trying EasyOCR variant {idx}/{len(prepared_images)}")
            easyocr_result = VisionService._ocr_with_easyocr(candidate)
            if len((easyocr_result.get('text') or '').strip()) > len(best_easyocr.get('text', '').strip()):
                best_easyocr = easyocr_result
        if best_easyocr.get('text'):
            VisionService._log(
                f"EasyOCR selected best result with text_chars={len(best_easyocr.get('text', '').strip())}"
            )
            return best_easyocr
        VisionService._log("EasyOCR returned no readable text")
        VisionService._log("All OCR providers returned no readable text")
        return {'text': '', 'web_entities': []}

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
            VisionService._log(f"Decoded data URL to {len(image_bytes)} bytes ({mime_type})")
            return VisionService.ocr_image_bytes(image_bytes, mime_type=mime_type)
        except RuntimeError:
            raise
        except Exception as e:
            VisionService._log(f"ocr_data_url error: {e}")
            return {'text': '', 'web_entities': []}
