import os
import requests
import google.generativeai as genai
from typing import List, Dict
from dotenv import load_dotenv

load_dotenv()

# Configure APIs
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
SERPER_API_KEY = os.getenv("SERPER_API_KEY")

# List of candidate models to try (free-tier first, then paid fallbacks)
CANDIDATE_MODELS = [
    "gemini-2.0-flash",          # Free tier: 15 RPM, 1M TPM
    "gemini-2.0-flash-lite",     # Free tier: 30 RPM, very fast
    "gemini-1.5-flash"          # Free tier: 15 RPM, reliable
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
            organic = results.get("organic", [])[:5]  # Top 5 results
            # Normalize to consistent structure
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
    def __init__(self, model_name: str = "gemini-1.5-flash"):
        self.default_model_name = model_name

    def _call_model(self, prompt: str) -> str:
        """Try each model in CANDIDATE_MODELS until one succeeds."""
        last_error = None
        for model_name in CANDIDATE_MODELS:
            try:
                print(f"Attempting with model: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                return response.text
            except Exception as e:
                last_error = str(e)
                print(f"Model {model_name} failed: {last_error}")
                continue
        return f"AI error (all models failed): {last_error}"

    def extract_claim(self, text: str) -> str:
        """Use Gemini to isolate the single main factual claim from the text."""
        prompt = f"""
You are a fact-checking assistant. From the text below, identify and extract the single most important VERIFIABLE FACTUAL CLAIM.
Output ONLY the claim as a short sentence (max 2 sentences). Do NOT add any commentary or explanation.

TEXT:
{text}

MAIN CLAIM:"""
        result = self._call_model(prompt)
        # Clean up any leading labels Gemini might add
        claim = result.strip().replace("MAIN CLAIM:", "").strip()
        return claim if claim else text

    def fact_check(self, claim: str, search_results: List[Dict]) -> str:
        """Analyze the claim against search results and produce a verdict."""
        context = ""
        for i, res in enumerate(search_results):
            context += f"Source {i+1}: {res.get('title')}\n"
            context += f"Snippet: {res.get('snippet')}\n"
            context += f"Link: {res.get('link')}\n\n"

        prompt = f"""
You are an expert fact-checker for the SRT (Social Responsibility Tools) platform.
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

If search results are empty or irrelevant, state "Unverified" and explain why.
"""
        return self._call_model(prompt)
