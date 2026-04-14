from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
import re
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
try:
    from .services import SerperService, GeminiService, VisionService, DuckDuckGoService, _is_social_link
    from .database import init_db, CacheService, CuratedEvidenceService
except ImportError:
    from services import SerperService, GeminiService, VisionService, DuckDuckGoService, _is_social_link
    from database import init_db, CacheService, CuratedEvidenceService

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


class CuratedEvidenceRequest(BaseModel):
    url: str
    title: str = ""
    source: str = ""
    claim_summary: str = ""
    verdict: str = ""
    notes: str = ""
    tags: List[str] = []

gemini_service = GeminiService()

# Admin token for simple auth on cache listing endpoint
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")


def _require_admin(x_admin_token: Optional[str]):
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="Admin access not configured. Set ADMIN_TOKEN in backend/.env and send it as the x-admin-token header.",
        )
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _extract_suspected_author(text: str) -> str:
    lines = [_normalize_space(line) for line in (text or "").splitlines() if _normalize_space(line)]
    if not lines:
        return ""

    for idx, line in enumerate(lines[:6]):
        handle_match = re.search(r"@([A-Za-z0-9_]{2,})", line)
        if handle_match:
            before_handle = _normalize_space(line[:handle_match.start()])
            before_handle = re.sub(r"^(post|tweet|thread)\s+", "", before_handle, flags=re.IGNORECASE).strip(" :-")
            if before_handle:
                return before_handle
            if idx > 0:
                previous = re.sub(r"^(post|tweet|thread)\s+", "", lines[idx - 1], flags=re.IGNORECASE).strip(" :-")
                if previous and "http" not in previous.lower():
                    return previous

    first_line = re.sub(r"^(post|tweet|thread)\s+", "", lines[0], flags=re.IGNORECASE).strip(" :-")
    if 1 <= len(first_line.split()) <= 4 and "http" not in first_line.lower():
        return first_line
    return ""


def _extract_quote_fragment(text: str, author: str = "") -> str:
    cleaned_lines = []
    for raw_line in (text or "").splitlines():
        line = _normalize_space(raw_line)
        if not line:
            continue
        if re.fullmatch(r"@?[A-Za-z0-9_]{2,}", line):
            continue
        if line.lower() in {"post", "tweet", "thread", "follow"}:
            continue
        if author and line.lower() == author.lower():
            continue
        cleaned_lines.append(line)

    body = " ".join(cleaned_lines)
    if author:
        body = re.sub(re.escape(author), "", body, flags=re.IGNORECASE)
    body = re.sub(r"@[A-Za-z0-9_]{2,}", "", body)
    body = _normalize_space(body)
    if not body:
        return ""

    first_sentence = re.split(r"(?<=[.!?])\s+", body, maxsplit=1)[0]
    words = first_sentence.split()
    return " ".join(words[:16]).strip()


def _looks_like_attributed_post(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return bool(
        re.search(r"@[A-Za-z0-9_]{2,}", text)
        or lowered.startswith("post")
        or " follow " in f" {lowered} "
    )


def _build_search_queries(original_text: str, extracted_claim: str) -> List[str]:
    def _dedupe(items: List[str]) -> List[str]:
        seen = set()
        ordered = []
        for item in items:
            normalized = _normalize_space(item)
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(normalized)
        return ordered

    queries: List[str] = []
    suspected_author = _extract_suspected_author(original_text)
    quote_fragment = _extract_quote_fragment(original_text, suspected_author)

    if _looks_like_attributed_post(original_text) and suspected_author:
        if quote_fragment:
            queries.append(f'"{suspected_author}" "{quote_fragment}" news')
            queries.append(f'"{suspected_author}" "{quote_fragment}" fact check')
        queries.append(f'"{suspected_author}" statement Reuters AP BBC')
        queries.append(f'"{suspected_author}" post verified news')

    queries.append(f"{extracted_claim} fact check")
    return _dedupe(queries)[:4]


def _merge_search_results(query_results: List[List[Dict]], max_results: int = 8) -> List[Dict]:
    merged: List[Dict] = []
    seen_links = set()
    for results in query_results:
        for item in results or []:
            link = (item.get("link") or item.get("url") or item.get("href") or "").strip()
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            normalized = dict(item)
            normalized["link"] = link
            merged.append(normalized)
            if len(merged) >= max_results:
                return merged
    return merged

@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/")
def root():
    return {
        "message": "SRT Fact-Check AI API",
        "health": "/health",
        "admin_cache": "/admin/cache",
        "admin_evidence": "/admin/evidence",
        "admin_ui": "/admin/ui",
    }


@app.get('/admin/cache')
def admin_list_cache(x_admin_token: Optional[str] = Header(None)):
    """Return cached claim entries. Protected by `ADMIN_TOKEN` env var via header `x-admin-token`."""
    _require_admin(x_admin_token)
    entries = CacheService.list_cache()
    return {"cache": entries}


@app.get('/admin/evidence')
def admin_list_evidence(x_admin_token: Optional[str] = Header(None)):
    _require_admin(x_admin_token)
    entries = CuratedEvidenceService.list_entries()
    return {"evidence": entries}


@app.post('/admin/evidence')
def admin_add_evidence(payload: CuratedEvidenceRequest, x_admin_token: Optional[str] = Header(None)):
    _require_admin(x_admin_token)
    if not payload.url.strip():
        raise HTTPException(status_code=400, detail="URL is required")
    CuratedEvidenceService.add_entry(
        url=payload.url.strip(),
        title=payload.title.strip(),
        source=payload.source.strip(),
        claim_summary=payload.claim_summary.strip(),
        verdict=payload.verdict.strip(),
        notes=payload.notes.strip(),
        tags=[tag.strip() for tag in payload.tags if tag.strip()],
    )
    return {"status": "ok", "message": "Evidence saved"}


@app.get("/admin/ui", response_class=HTMLResponse)
def admin_ui():
    return """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>SRT Admin</title>
      <style>
        :root {
          --bg: #f5efe4;
          --panel: #fffaf2;
          --ink: #1f2937;
          --muted: #6b7280;
          --line: #d6c6aa;
          --accent: #0f766e;
          --accent-2: #8b5e34;
          --danger: #b91c1c;
        }
        * { box-sizing: border-box; }
        body {
          margin: 0;
          font-family: Georgia, "Times New Roman", serif;
          color: var(--ink);
          background:
            radial-gradient(circle at top left, #fff8eb, transparent 35%),
            linear-gradient(135deg, #efe3cf 0%, #f8f2e8 48%, #eadcc6 100%);
        }
        .wrap {
          max-width: 1100px;
          margin: 0 auto;
          padding: 24px;
        }
        .hero {
          padding: 24px;
          border: 1px solid var(--line);
          background: rgba(255, 250, 242, 0.9);
          border-radius: 18px;
          box-shadow: 0 16px 40px rgba(64, 40, 16, 0.08);
          margin-bottom: 20px;
        }
        h1, h2 { margin: 0 0 12px; }
        p { color: var(--muted); }
        .grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
          gap: 20px;
        }
        .card {
          background: var(--panel);
          border: 1px solid var(--line);
          border-radius: 18px;
          padding: 18px;
          box-shadow: 0 12px 30px rgba(64, 40, 16, 0.06);
        }
        label {
          display: block;
          font-size: 14px;
          margin: 12px 0 6px;
          color: var(--accent-2);
          font-weight: 700;
        }
        input, textarea, select {
          width: 100%;
          border: 1px solid var(--line);
          border-radius: 12px;
          padding: 10px 12px;
          font: inherit;
          background: white;
          color: var(--ink);
        }
        textarea { min-height: 96px; resize: vertical; }
        button {
          border: 0;
          border-radius: 999px;
          padding: 10px 16px;
          font: inherit;
          font-weight: 700;
          cursor: pointer;
          background: var(--accent);
          color: white;
          margin-top: 14px;
          margin-right: 10px;
        }
        button.secondary {
          background: #ede3d1;
          color: var(--ink);
        }
        .status {
          margin-top: 12px;
          font-size: 14px;
          color: var(--muted);
        }
        .status.error { color: var(--danger); }
        .list {
          display: grid;
          gap: 12px;
          margin-top: 14px;
          max-height: 480px;
          overflow: auto;
        }
        .item {
          border: 1px solid var(--line);
          border-radius: 14px;
          padding: 12px;
          background: #fff;
        }
        .item a {
          color: var(--accent);
          text-decoration: none;
          font-weight: 700;
        }
        .meta {
          font-size: 13px;
          color: var(--muted);
          margin-top: 6px;
        }
        .pill {
          display: inline-block;
          padding: 3px 8px;
          border-radius: 999px;
          background: #e7f5f3;
          color: var(--accent);
          font-size: 12px;
          margin: 4px 6px 0 0;
        }
        code {
          background: #efe7da;
          padding: 2px 6px;
          border-radius: 6px;
        }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="hero">
          <h1>SRT Admin</h1>
          <p>Add curated evidence links from WhatsApp and inspect both curated evidence and cached fact-checks in one place.</p>
          <label for="token">Admin Token</label>
          <input id="token" type="password" placeholder="Enter x-admin-token" />
          <div>
            <button id="saveTokenBtn" class="secondary" type="button">Save Token</button>
            <button id="refreshBtn" type="button">Refresh Data</button>
          </div>
          <div class="status" id="topStatus">Use the token from <code>backend/.env</code>.</div>
        </div>

        <div class="grid">
          <section class="card">
            <h2>Add Evidence</h2>
            <label for="url">URL</label>
            <input id="url" type="url" placeholder="https://example.com/fact-check" />

            <label for="title">Title</label>
            <input id="title" type="text" placeholder="Article title" />

            <label for="source">Source</label>
            <input id="source" type="text" placeholder="PolitiFact, AP News, Africa Check..." />

            <label for="claimSummary">Claim Summary</label>
            <textarea id="claimSummary" placeholder="Short summary of the claim being checked"></textarea>

            <label for="verdict">Verdict</label>
            <select id="verdict">
              <option value="">Select verdict</option>
              <option>True</option>
              <option>False</option>
              <option>Misleading</option>
              <option>Out of Context</option>
              <option>Mixed</option>
              <option>Unverified</option>
            </select>

            <label for="notes">Notes</label>
            <textarea id="notes" placeholder="Extra context, language, where you found it, etc."></textarea>

            <label for="tags">Tags</label>
            <input id="tags" type="text" placeholder="politics, election, france" />

            <button id="saveEvidenceBtn" type="button">Save Evidence</button>
            <div class="status" id="formStatus"></div>
          </section>

          <section class="card">
            <h2>Curated Evidence</h2>
            <p>These are your manually saved links.</p>
            <div id="evidenceList" class="list"></div>
          </section>
        </div>

        <section class="card" style="margin-top:20px;">
          <h2>Claim Cache</h2>
          <p>These are automatic fact-check results already cached by the system.</p>
          <div id="cacheList" class="list"></div>
        </section>
      </div>

      <script>
        const tokenInput = document.getElementById("token");
        const topStatus = document.getElementById("topStatus");
        const formStatus = document.getElementById("formStatus");
        const evidenceList = document.getElementById("evidenceList");
        const cacheList = document.getElementById("cacheList");

        const savedToken = localStorage.getItem("srt_admin_token") || "";
        tokenInput.value = savedToken;

        function getHeaders() {
          const token = tokenInput.value.trim();
          return {
            "Content-Type": "application/json",
            "x-admin-token": token,
          };
        }

        function setStatus(el, message, isError = false) {
          el.textContent = message;
          el.className = isError ? "status error" : "status";
        }

        function escapeHtml(value) {
          return (value || "").replace(/[&<>"']/g, (ch) => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;"
          }[ch]));
        }

        async function loadEvidence() {
          const resp = await fetch("/admin/evidence", { headers: getHeaders() });
          if (!resp.ok) throw new Error("Could not load evidence");
          const data = await resp.json();
          const items = data.evidence || [];
          evidenceList.innerHTML = items.length ? items.map(item => `
            <div class="item">
              <a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.title || item.url)}</a>
              <div class="meta">${escapeHtml(item.source || "Unknown source")} • ${escapeHtml(item.verdict || "No verdict")} • ${escapeHtml(item.created_at || "")}</div>
              <div style="margin-top:8px;">${escapeHtml(item.claim_summary || item.notes || "")}</div>
              <div>${(item.tags || []).map(tag => `<span class="pill">${escapeHtml(tag)}</span>`).join("")}</div>
            </div>
          `).join("") : '<div class="item">No curated evidence saved yet.</div>';
        }

        async function loadCache() {
          const resp = await fetch("/admin/cache", { headers: getHeaders() });
          if (!resp.ok) throw new Error("Could not load cache");
          const data = await resp.json();
          const items = data.cache || [];
          cacheList.innerHTML = items.length ? items.map(item => `
            <div class="item">
              <div><strong>${escapeHtml(item.claim_text || "Untitled claim")}</strong></div>
              <div class="meta">${escapeHtml(item.timestamp || "")}</div>
              <div style="margin-top:8px; white-space:pre-wrap;">${escapeHtml(item.verdict_markdown || "")}</div>
            </div>
          `).join("") : '<div class="item">No cached fact-check results yet.</div>';
        }

        async function refreshAll() {
          try {
            await Promise.all([loadEvidence(), loadCache()]);
            setStatus(topStatus, "Admin data loaded.");
          } catch (err) {
            setStatus(topStatus, err.message + ". Check your admin token and make sure the backend is running.", true);
          }
        }

        document.getElementById("saveTokenBtn").addEventListener("click", () => {
          localStorage.setItem("srt_admin_token", tokenInput.value.trim());
          setStatus(topStatus, "Token saved in this browser.");
        });

        document.getElementById("refreshBtn").addEventListener("click", refreshAll);

        document.getElementById("saveEvidenceBtn").addEventListener("click", async () => {
          const payload = {
            url: document.getElementById("url").value.trim(),
            title: document.getElementById("title").value.trim(),
            source: document.getElementById("source").value.trim(),
            claim_summary: document.getElementById("claimSummary").value.trim(),
            verdict: document.getElementById("verdict").value.trim(),
            notes: document.getElementById("notes").value.trim(),
            tags: document.getElementById("tags").value.split(",").map(v => v.trim()).filter(Boolean)
          };

          if (!payload.url) {
            setStatus(formStatus, "URL is required.", true);
            return;
          }

          try {
            const resp = await fetch("/admin/evidence", {
              method: "POST",
              headers: getHeaders(),
              body: JSON.stringify(payload)
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || "Save failed");
            setStatus(formStatus, "Evidence saved.");
            await refreshAll();
          } catch (err) {
            setStatus(formStatus, err.message, true);
          }
        });

        refreshAll();
      </script>
    </body>
    </html>
    """

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

    suspected_author = _extract_suspected_author(request.text)

    # 2. Search for information using attribution-first queries when the text looks
    # like a quoted social post. Bias toward direct claim checks and wire coverage.
    search_queries = _build_search_queries(request.text, extracted_claim)

    # For scam-like claims, add extra keywords
    if _is_scam_like(extracted_claim) or _is_scam_like(request.text):
        search_queries.insert(0, f"{extracted_claim} scam misleading fact check")
        print(f"Enhanced search queries for scam-like claim: {search_queries}")

    # Prefer DuckDuckGo results when available
    search_results = []
    try:
        collected_results = []
        for query in search_queries:
            ddg_results = DuckDuckGoService.search(query, max_results=8)
            if ddg_results:
                collected_results.append(ddg_results)
                continue
            collected_results.append(SerperService.search(query))
        search_results = _merge_search_results(collected_results, max_results=10)
    except Exception as e:
        print('DuckDuckGo search failed, falling back to Serper:', e)
        fallback_results = [SerperService.search(query) for query in search_queries]
        search_results = _merge_search_results(fallback_results, max_results=10)

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
            'politifact.com', 'fullfact.org', 'afp.com', 'africacheck.org', 'leadstories.com'
        ]

        # Category-specific allowlists to prioritize domain authority per topic
        category_allowlists = {
            'health': ['cdc.gov', 'who.int', 'nejm.org', 'hmh.com'],
            'politics': ['politifact.com', 'factcheck.org', 'apnews.com', 'reuters.com', 'africacheck.org', 'leadstories.com'],
            'economy': ['ft.com', 'economist.com', 'bloomberg.com', 'wsj.com'],
            'science': ['nature.com', 'sciencemag.org', 'who.int'],
            'international': ['reuters.com', 'apnews.com', 'bbc.com', 'aljazeera.com', 'afp.com'],
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
    result_md = gemini_service.fact_check(
        extracted_claim,
        search_results,
        original_text=request.text,
        suspected_author=suspected_author,
    )

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
