from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict
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
        return FactCheckResponse(verdict_md=cached_result, is_cached=True)

    # 1. Extract the main claim from the text
    extracted_claim = gemini_service.extract_claim(request.text)
    print(f"Extracted claim: {extracted_claim}")

    # 2. Search for information using the extracted claim
    search_results = SerperService.search(extracted_claim)

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
