from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict
import re
try:
    from .services import SerperService, GeminiService
    from .database import init_db, CacheService
except ImportError:
    from services import SerperService, GeminiService
    from database import init_db, CacheService

app = FastAPI(title="SRT Fact-Check AI API")

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

@app.on_event("startup")
def startup_event():
    init_db()

@app.get("/health")
def health_check():
    return {"status": "ok"}

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
        # Map cached evidence items into EvidenceLink models
        evidence_links_resp = [
            EvidenceLink(title=e.get("title", "Source"), url=e.get("url", ""), snippet=e.get("snippet", ""))
            for e in evidence_links_cached
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
    search_query = extracted_claim
    if _is_scam_like(extracted_claim) or _is_scam_like(request.text):
        search_query = f"{extracted_claim} scam misleading fact check"
        print(f"Enhanced search query for scam-like claim: {search_query}")

    search_results = SerperService.search(search_query)

    # 2b. Filter search results to prefer credible sources and exclude social media/unofficial sites
    def _filter_credible(results):
        import urllib.parse
        # Blacklist known social and unofficial domains
        blacklist = [
            'facebook.com', 'm.facebook.com', 'instagram.com', 'twitter.com', 'x.com', 'tiktok.com',
            'reddit.com', 'youtube.com', 'medium.com', 'quora.com', 'blogspot.com', 'wordpress.com',
            'pinterest.com', 'linkedin.com'
        ]

        # Allowlist authoritative news and fact-check domains (expand as needed)
        allowlist = [
            'reuters.com', 'apnews.com', 'bbc.co.uk', 'bbc.com', 'nytimes.com', 'washingtonpost.com',
            'theguardian.com', 'cnn.com', 'bloomberg.com', 'economist.com', 'factcheck.org', 'snopes.com',
            'politifact.com', 'fullfact.org', 'afp.com'
        ]

        filtered = []
        prefer = []
        for r in results:
            link = r.get('link', '') or ''
            try:
                host = urllib.parse.urlparse(link).hostname or ''
                host = host.lower()
            except Exception:
                host = ''

            # Exclude blacklisted domains
            if any(b in host for b in blacklist):
                continue

            # Prefer allowlist
            if any(a in host for a in allowlist) or host.endswith('.gov') or host.endswith('.edu'):
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

    filtered_results = _filter_credible(search_results)
    # Use filtered results for evidence links and fact-checking
    search_results = filtered_results

    # 3. Analyze with Gemini
    result_md = gemini_service.fact_check(extracted_claim, search_results)

    # 4. Save to cache if successful
    if "Fact-checking error" not in result_md and "AI error" not in result_md:
        CacheService.save_to_cache(request.text, result_md)

    # 5. Format evidence links
    evidence_links = [
        EvidenceLink(
            title=r.get("title", "Source"),
            url=r.get("link", ""),
            snippet=r.get("snippet", "")
        )
        for r in search_results if r.get("link")
    ]

    return FactCheckResponse(
        verdict_md=result_md,
        extracted_claim=extracted_claim,
        evidence_links=evidence_links,
        is_cached=False
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
