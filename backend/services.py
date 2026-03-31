import os
import requests
import google.generativeai as genai
from typing import List, Dict
from dotenv import load_dotenv

load_dotenv()

# Configure APIs
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
SERPER_API_KEY = os.getenv("SERPER_API_KEY")

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
            return results.get("organic", [])[:5]  # Top 5 results
        except Exception as e:
            print(f"Serper search error: {e}")
            return []

class GeminiService:
    def __init__(self, model_name: str = "gemini-1.5-flash-latest"):
        self.model = genai.GenerativeModel(model_name)

    def fact_check(self, text: str, search_results: List[Dict]) -> str:
        # Prepare context from search results
        context = ""
        for i, res in enumerate(search_results):
            context += f"Source {i+1}: {res.get('title')}\n"
            context += f"Snippet: {res.get('snippet')}\n"
            context += f"Link: {res.get('link')}\n\n"

        prompt = f"""
        You are an expert fact-checker for the SRT platform. 
        Analyze the following claim based on the provided search results.
        
        Claim: "{text}"
        
        Search Results (Context):
        {context}
        
        Please provide:
        1. **Verdict**: (e.g., True, False, Misleading, No Evidence)
        2. **Summary**: A concise explanation.
        3. **Key Points**: Bullet points of evidence found.
        4. **Source Links**: Links that support your verdict.
        
        Format the response in Markdown for display in a Chrome Extension popup.
        """
        
        try:
            response = self.model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"Fact-checking error: {str(e)}"
