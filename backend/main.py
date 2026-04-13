from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import re
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
try:
    from .services import SerperService, GeminiService, VisionService, DuckDuckGoService, _is_social_link
    from .database import init_db, CacheService
except ImportError:
    from services import SerperService, GeminiService, VisionService, DuckDuckGoService, _is_social_link
    from database import init_db, CacheService

load_dotenv()

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(lifespan=lifespan, title="SRT Fact-Check AI API")

# Enable CORS for the Chrome Extension
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class FactCheckRequest(BaseModel):
    text: str
    category: Optional[str] = None
    subcategory: Optional[str] = None

class EvidenceLink(BaseModel):
    title: str
    url: str
    snippet: str

class FactCheckResponse(BaseModel):
    verdict_md: str
    extracted_claim: str = ""
    evidence_links: List[EvidenceLink] = []
    is_cached: bool = False

gemini_service = GeminiService()

# Admin token for simple auth on cache listing endpoint
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"message": "SRT Fact-Check AI API", "health": "/health", "admin_cache": "/admin/cache"}


@app.get('/admin/cache')
def admin_list_cache(x_admin_token: Optional[str] = Header(None)):
    """Return cached claim entries. Protected by `ADMIN_TOKEN` env var via header `x-admin-token`."""
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="Admin access not configured. Set ADMIN_TOKEN in backend/.env and send it as the x-admin-token header.",
        )
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    entries = CacheService.list_cache()
    return {"cache": entries}

@app.post("/factcheck", response_model=FactCheckResponse)
async def perform_fact_check(request: FactCheckRequest):
    if not request.text:
        raise HTTPException(status_code=400, detail="Empty text provided")

    # 0. Check cache
    cached_result = CacheService.get_cached_verdict(request.text)
    if cached_result:
        print(f"Cache hit for: {request.text[:50]}...")
        # Return cached verdict and evidence links when available
        verdict_md = cached_result.get("verdict_markdown")
        evidence_links_cached = cached_result.get("evidence_links", [])
        # Filter cached evidence to exclude social/user-generated links, then map
        filtered_cached = [e for e in (evidence_links_cached or []) if e.get('url') and not _is_social_link(e.get('url'))]
        evidence_links_resp = [
            EvidenceLink(title=e.get("title", "Source"), url=e.get("url", ""), snippet=e.get("snippet", ""))
            for e in filtered_cached
        ]
        return FactCheckResponse(verdict_md=verdict_md, extracted_claim="", evidence_links=evidence_links_resp, is_cached=True)

    # 1. Extract the main claim from the text
    extracted_claim = gemini_service.extract_claim(request.text)
    print(f"Extracted claim: {extracted_claim}")

    # If the claim appears to describe a money giveaway / too-good-to-be-true ad,
    # append keywords to improve search results for scam/misleading evidence.
    def _is_scam_like(s: str) -> bool:
        if not s:
            return False
        s_low = s.lower()
        patterns = [
            r"free\s+money",
            r"free\s+dollars",
            r"free\s+cash",
            r"win\s+\$?\d+",
            r"too\s+good\s+to\s+be\s+true",
            r"get\s+rich\s+quick",
            r"make\s+money\s+fast",
            r"no\s+risk",
            r"guaranteed\s+\w+",
            r"earn\s+\$",
        ]
        for p in patterns:
            if re.search(p, s_low):
                return True
        return False

    # 2. Search for information using the extracted claim (possibly enhanced)
    # Always append ':fact check' to bias searches towards fact-check pages
    search_query = f"{extracted_claim} :fact check"

    # For scam-like claims, add extra keywords
    if _is_scam_like(extracted_claim) or _is_scam_like(request.text):
        search_query = f"{extracted_claim} scam misleading fact check"
        # ensure ':fact check' is present
        if ':fact check' not in search_query:
            search_query = f"{search_query} :fact check"
        print(f"Enhanced search query for scam-like claim: {search_query}")

    # Prefer DuckDuckGo results when available
    search_results = []
    try:
        ddg_results = DuckDuckGoService.search(search_query, max_results=8)
        if ddg_results:
            search_results = ddg_results
        else:
            search_results = SerperService.search(search_query)
    except Exception as e:
        print('DuckDuckGo search failed, falling back to Serper:', e)
        search_results = SerperService.search(search_query)

    # 2b. Filter search results to prefer credible sources and exclude social media/unofficial sites
    def _filter_credible(results, category: Optional[str] = None):
        import urllib.parse
        # Blacklist known social and unofficial domains
        blacklist = [
            'facebook.com', 'instagram.com', 'twitter.com', 'x.com', 'tiktok.com',
            'reddit.com', 'youtube.com', 'medium.com', 'quora.com', 'blogspot.com', 'wordpress.com',
            'pinterest.com', 'linkedin.com'
        ]

        # Allowlist authoritative news and fact-check domains (expand as needed)
        global_allowlist = [
            'reuters.com', 'apnews.com', 'bbc.co.uk', 'bbc.com', 'nytimes.com', 'washingtonpost.com',
            'theguardian.com', 'cnn.com', 'bloomberg.com', 'economist.com', 'factcheck.org', 'snopes.com',
            'politifact.com', 'fullfact.org', 'afp.com'
        ]

        # Category-specific allowlists to prioritize domain authority per topic
        category_allowlists = {
            'health': ['cdc.gov', 'who.int', 'nejm.org', 'hmh.com'],
            'politics': ['nytimes.com', 'washingtonpost.com', 'politifact.com', 'factcheck.org'],
            'economy': ['ft.com', 'economist.com', 'bloomberg.com', 'wsj.com'],
            'science': ['nature.com', 'sciencemag.org', 'who.int'],
            'international': ['reuters.com', 'apnews.com', 'bbc.com', 'aljazeera.com'],
            'default': global_allowlist
        }

        filtered = []
        prefer = []
        for r in results:
            # Normalize potential link keys
            link = (r.get('link') or r.get('url') or r.get('href') or r.get('source') or '')
            r['link'] = link
            try:
                host = urllib.parse.urlparse(link).hostname or ''
                host = host.lower()
                if host.startswith('www.'):
                    host = host[4:]
            except Exception:
                host = ''

            # Exclude blacklisted domains (exact or subdomain match)
            if host:
                skip = False
                for b in blacklist:
                    if host == b or host.endswith('.' + b):
                        skip = True
                        break
                if skip:
                    continue

            # Prefer category-specific allowlist first, then global
            preferred = False
            if category:
                cat = category.lower()
                cat_list = category_allowlists.get(cat, [])
                for a in cat_list:
                    if host == a or host.endswith('.' + a):
                        preferred = True
                        break
            if not preferred:
                for a in global_allowlist:
                    if host == a or host.endswith('.' + a):
                        preferred = True
                        break

            if preferred or host.endswith('.gov') or host.endswith('.edu'):
                prefer.append(r)
            else:
                # Heuristic: include if domain contains 'news' or title/snippet mentions 'report' or 'says'
                t = (r.get('title') or '').lower()
                s = (r.get('snippet') or '').lower()
                if 'news' in host or 'news' in t or 'news' in s or 'report' in t or 'report' in s or 'says' in t:
                    filtered.append(r)

        # If we have preferred authoritative sources, return them first
        if prefer:
            return prefer
        # Otherwise return filtered heuristics (may be empty)
        return filtered or results

    filtered_results = _filter_credible(search_results, category=request.category)
    # Use filtered results for evidence links and fact-checking
    search_results = filtered_results

    # 3. Analyze with Gemini
    result_md = gemini_service.fact_check(extracted_claim, search_results)

    # 4. Format evidence links (defensive: exclude any social/user-generated links)
    safe_results = [r for r in search_results if r.get('link') and not _is_social_link(r.get('link'))]
    evidence_links = [
        EvidenceLink(
            title=r.get("title", "Source"),
            url=r.get("link", ""),
            snippet=r.get("snippet", "")
        )
        for r in safe_results
    ]

    # Convert evidence links to simple dicts for caching
    evidence_links_for_cache = [
        {"title": e.title, "url": e.url, "snippet": e.snippet} for e in evidence_links
    ]

    # 5. Save to cache if successful (include evidence links)
    if "Fact-checking error" not in result_md and "AI error" not in result_md:
        CacheService.save_to_cache(request.text, result_md, evidence_links_for_cache)

    return FactCheckResponse(
        verdict_md=result_md,
        extracted_claim=extracted_claim,
        evidence_links=evidence_links,
        is_cached=False
    )


class OcrRequest(BaseModel):
    images: List[str]


@app.post('/ocr')
async def ocr_images(req: OcrRequest):
    if not req.images:
        raise HTTPException(status_code=400, detail="No images provided")
    aggregated = []
    try:
        for data_url in req.images:
            res = VisionService.ocr_data_url(data_url)
            text = res.get('text', '')
            aggregated.append(text or '')
        combined = '\n\n'.join([t for t in aggregated if t])
        return {"texts": aggregated, "combined": combined}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR error: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
