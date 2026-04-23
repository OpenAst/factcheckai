import os
import re
import requests
from google import genai
from groq import Groq
from typing import List, Dict
from dotenv import load_dotenv
from urllib.parse import urlparse
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None
try:
    from ddgs import DDGS
except Exception:
    try:
        from duckduckgo_search import DDGS
    except Exception:
        DDGS = None

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
GUIDANCE_PDF_PATHS = [p.strip() for p in os.getenv("GUIDANCE_PDF_PATHS", "").split(os.pathsep) if p.strip()]

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


def _is_pdf_link(link: str) -> bool:
    if not link:
        return False
    lowered = link.lower().split("?", 1)[0].split("#", 1)[0]
    return lowered.endswith(".pdf")


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
    # Exclude obvious social/user-generated links and PDFs
    filtered = [
        r for r in results
        if r.get('link') and not _is_social_link(r.get('link')) and not _is_pdf_link(r.get('link'))
    ]
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
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
]

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

_GUIDANCE_CACHE = None


def _guidance_paths() -> List[str]:
    if GUIDANCE_PDF_PATHS:
        return [p for p in GUIDANCE_PDF_PATHS if os.path.isfile(p)]

    backend_dir = os.path.dirname(__file__)
    default_candidates = []
    for filename in os.listdir(backend_dir):
        if not filename.lower().endswith(".pdf"):
            continue
        if "uolo" not in filename.lower():
            continue
        default_candidates.append(os.path.join(backend_dir, filename))
    return sorted(default_candidates)


def _normalize_guidance_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _chunk_guidance_text(source_name: str, text: str, chunk_size: int = 1200) -> List[Dict[str, str]]:
    chunks = []
    normalized = text.replace("\r", "\n")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", normalized) if p.strip()]
    current = ""
    chunk_index = 1
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append({"source": source_name, "text": _normalize_guidance_text(current), "chunk_index": str(chunk_index)})
            chunk_index += 1
        current = paragraph[:chunk_size]
    if current:
        chunks.append({"source": source_name, "text": _normalize_guidance_text(current), "chunk_index": str(chunk_index)})
    return chunks


def _load_guidance_chunks() -> List[Dict[str, str]]:
    global _GUIDANCE_CACHE
    if _GUIDANCE_CACHE is not None:
        return _GUIDANCE_CACHE

    chunks = []
    if PdfReader is None:
        print("[guidance] pypdf not available; project guidance PDFs will not be loaded")
        _GUIDANCE_CACHE = chunks
        return chunks

    for path in _guidance_paths():
        try:
            reader = PdfReader(path)
            page_text = []
            for page in reader.pages:
                page_text.append(page.extract_text() or "")
            combined = "\n".join(page_text).strip()
            if not combined:
                continue
            chunks.extend(_chunk_guidance_text(os.path.basename(path), combined))
        except Exception as exc:
            print(f"[guidance] Failed to load {path}: {exc}")

    print(f"[guidance] Loaded {len(chunks)} guidance chunks from {len(_guidance_paths())} PDF(s)")
    _GUIDANCE_CACHE = chunks
    return chunks


def get_project_guidance(query: str, max_chunks: int = 3) -> str:
    chunks = _load_guidance_chunks()
    if not chunks:
        return ""

    query_terms = {term for term in re.findall(r"[a-z0-9]{3,}", (query or "").lower()) if len(term) >= 3}
    if not query_terms:
        return ""

    scored = []
    for chunk in chunks:
        chunk_terms = set(re.findall(r"[a-z0-9]{3,}", chunk["text"].lower()))
        overlap = len(query_terms & chunk_terms)
        if overlap <= 0:
            continue
        scored.append((overlap, len(chunk["text"]), chunk))

    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = [item[2] for item in scored[:max_chunks]]
    if not selected:
        return ""

    return "\n\n".join(
        f"[{item['source']} chunk {item['chunk_index']}]\n{item['text']}"
        for item in selected
    )

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
            response = requests.post(url, headers=headers, json=payload, timeout=12)
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
            print('DDGS search package not available')
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
        """Try Groq models first."""
        if not self.groq_client:
            raise Exception("No GROQ_API_KEY set")
        attempted_models = []
        last_error = None
        for model_name in GROQ_MODELS:
            try:
                print(f"Trying Groq model: {model_name}")
                attempted_models.append(model_name)
                response = self.groq_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                )
                content = response.choices[0].message.content if response.choices else ""
                return content or ""
            except Exception as e:
                last_error = str(e)
                print(f"Groq model {model_name} failed: {last_error}")
                continue
        attempted = ", ".join(attempted_models) if attempted_models else "none"
        raise Exception(f"All Groq models failed after trying [{attempted}]. Last error: {last_error}")

    def _call_gemini(self, prompt: str) -> str:
        """Try Gemini models."""
        if not self.gemini_client:
            raise Exception("No GEMINI_API_KEY set")
        attempted_models = []
        last_error = None
        for model_name in GEMINI_MODELS:
            try:
                print(f"Trying Gemini model: {model_name}")
                attempted_models.append(model_name)
                response = self.gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                return getattr(response, "text", "") or ""
            except Exception as e:
                last_error = str(e)
                print(f"Gemini model {model_name} failed: {last_error}")
                continue
        attempted = ", ".join(attempted_models) if attempted_models else "none"
        raise Exception(f"All Gemini models failed after trying [{attempted}]. Last error: {last_error}")

    def _call_model(self, prompt: str) -> str:
        """Use Groq first, then fall back to Gemini."""
        try:
            return self._call_groq(prompt)
        except Exception as groq_err:
            print(f"Groq failed, falling back to Gemini: {groq_err}")
            try:
                return self._call_gemini(prompt)
            except Exception as gemini_err:
                return f"AI error (Groq failed: {groq_err}; Gemini failed: {gemini_err})"

    def _guidance_block(self, query: str) -> str:
        guidance = get_project_guidance(query)
        if not guidance:
            return ""
        return f"""
PROJECT GUIDANCE:
Use the following internal project guidance as higher-priority workflow instruction when it is relevant.
If a guidance snippet conflicts with your default tendency, follow the guidance snippet.

{guidance}
"""

    def extract_claim(self, text: str) -> str:
        """Use AI to isolate the single main factual claim from the text."""
        guidance_block = self._guidance_block(text)
        prompt = f"""You are a senior fact-checking assistant. From the text below, identify and extract the single most important VERIFIABLE FACTUAL CLAIM.

{guidance_block}

Rules:
- Prefer the most consequential and specific factual assertion, not a vague topic summary.
- If the input includes quoted article text plus separate media-overlay text or "All detected text", prioritize the overlaid/media claim first.
- Do NOT just restate who posted the content unless authorship itself is the main checkable claim.
- If the text contains several factual statements, choose the one that would matter most to verify for misinformation review.
- If the text is promotional or ad-like but implies eligibility, hidden benefits, grants, payouts, compensation, deadlines, or urgent action, extract the IMPLIED factual claim behind the promotion.
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
        guidance_block = self._guidance_block(text)
        prompt = f"""You are a senior fact-checking assistant.
From the text below, extract up to {max_claims} distinct VERIFIABLE FACTUAL CLAIMS.

{guidance_block}

Rules:
- Return 2 claims when there are clearly 2 meaningful factual claims.
- Return 3 claims only when there are 3 genuinely distinct and important checkable claims.
- Prefer consequential, specific claims over vague summaries.
- If the input mixes image-overlay text with surrounding commentary or opinion, prioritize the image-overlay or media-detected text first.
- Do NOT include opinion, rhetoric, or pure attribution unless authorship itself is a factual claim.
- If the text explicitly attributes a quoted statement to a named person, you may include a secondary claim in the form: `<Person> said "<statement>"`, but only after the main substantive claim.
- Treat scam ads, benefit-eligibility bait, urgent enrollment offers, grant/payout offers, and hidden-benefit promotions as fact-checkable claims even if phrased like marketing.
- For promotional bait, extract the implied factual claim, not just the slogan.
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
        guidance_block = self._guidance_block(text)
        prompt = f"""You are helping a fact-checking workflow.
Decide whether the text contains a clear verifiable factual claim.

{guidance_block}

Rules:
- Use NO_CLAIM when the text is mainly opinion, insult, praise, emotion, advice, satire, vague rhetoric, or personal preference.
- Use FACTUAL_CLAIM when the text contains a specific claim that can be checked against evidence.
- Use MIXED when the text mixes opinion with at least one checkable factual claim.
- If MIXED, extract only the strongest and most consequential checkable factual claim.
- If the input includes a separate media headline, overlaid text, or an "All detected text" section, prefer that factual claim over surrounding commentary.
- Prefer the deepest factual assertion, not a surface-level paraphrase.
- Do NOT select mere authorship or attribution as the claim unless the post is fundamentally about whether a named person made a statement.
- If the text explicitly says a named person said or wrote a quoted statement, that attribution can be a checkable claim, but it should not override a stronger substantive claim unless the quote attribution is the real dispute.
- Treat scam-like ads, benefit bait, grant/payout offers, miracle offers, and urgent qualification/enrollment messages as FACTUAL_CLAIM even when they look like advertisement copy.
- If the text implies someone can qualify for hidden, new, limited-time, or little-known benefits, grants, compensation, or payouts, extract that implied claim.
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
        guidance_query = f"{claim}\n{original_text}\n{suspected_author}"
        guidance_block = self._guidance_block(guidance_query)
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

{guidance_block}

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
