from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
try:
    from .services import SerperService, GeminiService
except ImportError:
    from services import SerperService, GeminiService

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

gemini_service = GeminiService()

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.post("/factcheck", response_model=FactCheckResponse)
async def perform_fact_check(request: FactCheckRequest):
    if not request.text:
        raise HTTPException(status_code=400, detail="Empty text provided")
    
    # 1. Search for information
    search_results = SerperService.search(request.text)
    
    # 2. Analyze with Gemini
    result_md = gemini_service.fact_check(request.text, search_results)
    
    return FactCheckResponse(verdict_md=result_md)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
