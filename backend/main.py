from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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
    allow_origins=["*"],  # In production, specify the extension ID
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class FactCheckRequest(BaseModel):
    text: str

class FactCheckResponse(BaseModel):
    verdict_md: str
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

    # 1. Search for information
    search_results = SerperService.search(request.text)
    
    # 2. Analyze with Gemini
    result_md = gemini_service.fact_check(request.text, search_results)
    
    # 3. Save to cache if successful
    if "Fact-checking error" not in result_md:
        CacheService.save_to_cache(request.text, result_md)
    
    return FactCheckResponse(verdict_md=result_md, is_cached=False)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
