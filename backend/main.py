from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
import re
import os
import traceback
from contextlib import asynccontextmanager
from dotenv import load_dotenv
try:
    from .services import SerperService, GeminiService, DuckDuckGoService, _is_social_link, _is_pdf_link
    from .database import init_db, CacheService, CuratedEvidenceService, ReviewService
    from .ocr_queue import get_ocr_job, is_ocr_queue_available, submit_ocr_job
except ImportError:
    from services import SerperService, GeminiService, DuckDuckGoService, _is_social_link, _is_pdf_link
    from database import init_db, CacheService, CuratedEvidenceService, ReviewService
    from ocr_queue import get_ocr_job, is_ocr_queue_available, submit_ocr_job

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
    selected_claim: Optional[str] = None

class EvidenceLink(BaseModel):
    title: str
    url: str
    snippet: str

class ClaimOption(BaseModel):
    claim: str
    evidence_links: List[EvidenceLink] = []

class FactCheckResponse(BaseModel):
    verdict_md: str
    extracted_claim: str = ""
    extracted_claims: List[str] = []
    claim_options: List[ClaimOption] = []
    evidence_links: List[EvidenceLink] = []
    is_cached: bool = False
    claim_status: str = "factual_claim"
    claim_reason: str = ""


class CuratedEvidenceRequest(BaseModel):
    url: str
    title: str = ""
    source: str = ""
    claim_summary: str = ""
    verdict: str = ""
    notes: str = ""
    tags: List[str] = []


class ReviewSelectionRequest(BaseModel):
    post_text: str
    extracted_claim: str = ""
    claim_status: str = ""
    verdict_md: str = ""
    selected_evidence_url: str
    selected_evidence_title: str = ""
    selected_evidence_snippet: str = ""
    evidence_links: List[EvidenceLink] = []
    notes: str = ""


class OcrJobRequest(BaseModel):
    image_data: str
    source_hint: str = ""


class OcrJobResponse(BaseModel):
    job_id: str
    status: str
    result_text: str = ""
    error: str = ""

gemini_service = GeminiService()

CACHE_VERSION = "2026-04-25-always-extract-claims"

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


def _extract_attribution_claim(text: str, author: str = "") -> str:
    normalized = _normalize_space(text)
    if not normalized or not author:
        return ""

    quote_match = re.search(r'["“]([^"”]{4,180})["”]', normalized)
    if quote_match:
        quote_text = _normalize_space(quote_match.group(1))
        return f'{author} said "{quote_text}"'

    calling_match = re.search(
        r"\b(calling|called|calls)\s+([A-Z][A-Za-z.\s]{1,60}?)\s+(?:a|an)\s+['\"“]?([^'\"”]{3,120})['\"”]?",
        normalized,
        flags=re.IGNORECASE,
    )
    if calling_match:
        target = _normalize_space(calling_match.group(2))
        descriptor = _normalize_space(calling_match.group(3))
        return f'{author} called {target} "{descriptor}"'

    return ""


def _looks_like_attributed_post(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return bool(
        re.search(r"@[A-Za-z0-9_]{2,}", text)
        or lowered.startswith("post")
        or " follow " in f" {lowered} "
    )


def _should_prioritize_authorship(text: str, extracted_claim: str, suspected_author: str) -> bool:
    if not text or not suspected_author:
        return False
    if not _looks_like_attributed_post(text):
        return False

    text_low = text.lower()
    claim_low = (extracted_claim or "").lower()
    attribution_signals = [
        "posted",
        "tweeted",
        "wrote",
        "shared",
        "said",
        "statement",
        "quote",
    ]
    if any(signal in text_low for signal in attribution_signals):
        return True

    # If the extracted claim is itself just a shallow attribution paraphrase,
    # avoid doubling down on authorship-first routing.
    if any(signal in claim_low for signal in attribution_signals):
        return False

    return False


def _detect_scam_like_claim(text: str) -> Optional[Dict[str, str]]:
    normalized = _normalize_space(text).lower()
    if not normalized:
        return None

    benefit_terms = [
        "benefit", "benefits", "qualify", "eligible", "eligibility",
        "widow", "widows", "veteran", "veterans", "compensation",
        "grant", "grants", "payout", "claim your", "unclaimed",
    ]
    urgency_terms = [
        "learn more", "enrollment closes", "before enrollment closes",
        "act now", "tap", "limited time", "deadline", "apply now",
        "before it closes", "don't miss", "unlocking",
    ]
    deception_terms = [
        "you didn't know existed", "hidden", "most people don't know",
        "new 2026 benefits", "ages 40-75", "ages 40–75",
        "if you qualify", "options most widows don't know about",
    ]

    has_benefit = any(term in normalized for term in benefit_terms)
    has_urgency = any(term in normalized for term in urgency_terms)
    has_deception = any(term in normalized for term in deception_terms)

    if not has_benefit:
        return None
    if not (has_urgency or has_deception):
        return None

    if "widow" in normalized and "veteran" in normalized:
        claim = "Widows of veterans may qualify for legitimate new or little-known benefits through the linked offer."
    elif "veteran" in normalized:
        claim = "Veterans are being offered legitimate new or little-known benefits through the linked offer."
    else:
        claim = "The post claims people may qualify for legitimate hidden or newly available benefits through the linked offer."

    if "2026" in normalized:
        claim = claim.replace("benefits", "2026 benefits", 1)
    if "40-75" in normalized or "40–75" in normalized:
        claim = claim.replace("Veterans", "Veterans ages 40-75", 1)

    return {
        "status": "factual_claim",
        "claim": claim,
        "reason": "This promotional post makes implied eligibility or benefit claims with scam-style urgency, so it should be checked as a factual claim.",
    }


def _extract_media_focus_text(text: str) -> str:
    if not text:
        return ""

    normalized_text = text.replace("\r", "")
    markers = ["All detected text:", "Text in Media:"]
    for marker in markers:
        if marker.lower() not in normalized_text.lower():
            continue

        lines = normalized_text.splitlines()
        capture = False
        captured = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                if capture and captured:
                    break
                continue

            if line.lower().startswith(marker.lower()):
                capture = True
                remainder = line[len(marker):].strip()
                if remainder:
                    captured.append(remainder)
                continue

            if capture:
                if re.match(r"^(content in review|transcript|creation time|link information|retry detection|fact check claim)\b", line, flags=re.IGNORECASE):
                    break
                captured.append(line)
                if len(" ".join(captured)) > 350:
                    break

        focused = _normalize_space(" ".join(captured))
        if focused:
            return focused

    return ""


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

    queries.append(f"{extracted_claim} fact check")
    queries.append(f"{extracted_claim} false misleading evidence")

    scam_like = _detect_scam_like_claim(f"{original_text}\n{extracted_claim}")
    if scam_like:
        queries.append(f"{extracted_claim} scam false warning")

    attribution_claim = _extract_attribution_claim(original_text, suspected_author)
    if _looks_like_attributed_post(original_text) and suspected_author:
        if quote_fragment:
            queries.append(f'"{suspected_author}" "{quote_fragment}" news')
            queries.append(f'"{suspected_author}" "{quote_fragment}" fact check')
        if attribution_claim and extracted_claim.strip().lower() != attribution_claim.strip().lower():
            queries.append(f'"{suspected_author}" said news Reuters AP')
        queries.append(f'"{suspected_author}" statement Reuters AP BBC')
        queries.append(f'"{suspected_author}" post verified news')
    return _dedupe(queries)[:3]


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


def _filter_credible(results, category: Optional[str] = None):
    import urllib.parse
    blacklist = [
        'facebook.com', 'instagram.com', 'twitter.com', 'x.com', 'tiktok.com',
        'reddit.com', 'youtube.com', 'medium.com', 'quora.com', 'blogspot.com', 'wordpress.com',
        'pinterest.com', 'linkedin.com'
    ]

    global_allowlist = [
        'reuters.com', 'apnews.com', 'bbc.co.uk', 'bbc.com', 'nytimes.com', 'washingtonpost.com',
        'theguardian.com', 'cnn.com', 'bloomberg.com', 'economist.com', 'factcheck.org', 'snopes.com',
        'politifact.com', 'fullfact.org', 'afp.com', 'africacheck.org', 'leadstories.com'
    ]

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
        link = (r.get('link') or r.get('url') or r.get('href') or r.get('source') or '')
        r['link'] = link
        try:
            host = urllib.parse.urlparse(link).hostname or ''
            host = host.lower()
            if host.startswith('www.'):
                host = host[4:]
        except Exception:
            host = ''

        if host:
            if link.lower().split("?", 1)[0].split("#", 1)[0].endswith(".pdf"):
                continue
            skip = False
            for b in blacklist:
                if host == b or host.endswith('.' + b):
                    skip = True
                    break
            if skip:
                continue

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
            t = (r.get('title') or '').lower()
            s = (r.get('snippet') or '').lower()
            if 'news' in host or 'news' in t or 'news' in s or 'report' in t or 'report' in s or 'says' in t:
                filtered.append(r)

    if prefer:
        return prefer
    return filtered or results


def _search_claim_results(claim_text: str, original_text: str, category: Optional[str], suspected_author: str = "") -> List[Dict]:
    search_queries = _build_search_queries(original_text, claim_text)
    try:
        collected_results = []
        for query in search_queries:
            ddg_results = DuckDuckGoService.search(query, max_results=5)
            if ddg_results:
                collected_results.append(ddg_results)
                continue
            serper_results = SerperService.search(query)
            if serper_results:
                collected_results.append(serper_results)
        search_results = _merge_search_results(collected_results, max_results=10)
    except Exception as e:
        print('Primary search failed, falling back to DuckDuckGo then Serper:', e)
        fallback_results = []
        for query in search_queries:
            ddg_results = DuckDuckGoService.search(query, max_results=5)
            if ddg_results:
                fallback_results.append(ddg_results)
                continue
            fallback_results.append(SerperService.search(query))
        search_results = _merge_search_results(fallback_results, max_results=10)

    if suspected_author and _should_prioritize_authorship(original_text, claim_text, suspected_author):
        print(f"Authorship-sensitive search triggered for claim: {claim_text}")

    return _filter_credible(search_results, category=category)


def _extract_verdict_label(verdict_md: str) -> str:
    match = re.search(r"\*\*Verdict\*\*:\s*([A-Za-z ]+)", verdict_md or "", flags=re.IGNORECASE)
    if match:
        return _normalize_space(match.group(1))
    return ""

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
        "admin_reviews": "/admin/reviews",
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


@app.post("/ocr/jobs", response_model=OcrJobResponse)
def create_ocr_job(payload: OcrJobRequest):
    print(f"[ocr-api] create job request source_hint={payload.source_hint!r} image_chars={len(payload.image_data or '')}")
    if not payload.image_data.strip():
        raise HTTPException(status_code=400, detail="image_data is required")
    if not is_ocr_queue_available():
        raise HTTPException(status_code=503, detail="OCR queue is not configured")

    job_id = submit_ocr_job(
        payload.image_data.strip(),
        metadata={"source_hint": payload.source_hint.strip()},
    )
    print(f"[ocr-api] queued job_id={job_id}")
    return OcrJobResponse(job_id=job_id, status="queued")


@app.get("/ocr/jobs/{job_id}", response_model=OcrJobResponse)
def read_ocr_job(job_id: str):
    print(f"[ocr-api] read job_id={job_id}")
    if not is_ocr_queue_available():
        raise HTTPException(status_code=503, detail="OCR queue is not configured")

    job = get_ocr_job(job_id)
    if not job:
        print(f"[ocr-api] job not found job_id={job_id}")
        raise HTTPException(status_code=404, detail="OCR job not found")

    print(f"[ocr-api] job status job_id={job_id} status={job['status']}")
    return OcrJobResponse(
        job_id=job["job_id"],
        status=job["status"],
        result_text=job.get("result_text", ""),
        error=job.get("error", ""),
    )


@app.get('/admin/reviews')
def admin_list_reviews(
    q: str = Query(default="", description="Search saved reviews"),
    x_admin_token: Optional[str] = Header(None),
):
    _require_admin(x_admin_token)
    entries = ReviewService.list_reviews(q)
    return {"reviews": entries}


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


@app.post('/reviews')
def save_review(payload: ReviewSelectionRequest):
    if not payload.post_text.strip():
        raise HTTPException(status_code=400, detail="post_text is required")
    if not payload.selected_evidence_url.strip():
        raise HTTPException(status_code=400, detail="selected_evidence_url is required")

    evidence_links = [
        {"title": item.title, "url": item.url, "snippet": item.snippet}
        for item in payload.evidence_links
    ]
    system_verdict = _extract_verdict_label(payload.verdict_md)

    ReviewService.save_review(
        post_text=payload.post_text.strip(),
        extracted_claim=payload.extracted_claim.strip(),
        claim_status=payload.claim_status.strip(),
        system_verdict=system_verdict,
        verdict_markdown=payload.verdict_md.strip(),
        selected_evidence_url=payload.selected_evidence_url.strip(),
        selected_evidence_title=payload.selected_evidence_title.strip(),
        selected_evidence_snippet=payload.selected_evidence_snippet.strip(),
        all_evidence=evidence_links,
        notes=payload.notes.strip(),
    )

    CuratedEvidenceService.add_entry(
        url=payload.selected_evidence_url.strip(),
        title=payload.selected_evidence_title.strip(),
        source="Rater selected evidence",
        claim_summary=payload.extracted_claim.strip(),
        verdict=system_verdict,
        notes=payload.notes.strip() or payload.post_text.strip()[:500],
        tags=[tag for tag in [payload.claim_status.strip(), "rater-selected"] if tag],
    )
    return {"status": "ok", "message": "Review saved"}


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
          <p>Add curated evidence links from WhatsApp and inspect curated evidence, saved review decisions, and cached fact-checks in one place.</p>
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
          <h2>Saved Reviews</h2>
          <p>These are the posts where a rater selected a claim, a supporting evidence link, and a verdict context to save.</p>
          <label for="reviewSearch">Search Saved Reviews</label>
          <input id="reviewSearch" type="text" placeholder="Search by claim, verdict, evidence URL, notes, or post text" />
          <button id="searchReviewsBtn" class="secondary" type="button">Search Reviews</button>
          <div id="reviewList" class="list"></div>
        </section>

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
        const reviewList = document.getElementById("reviewList");
        const cacheList = document.getElementById("cacheList");
        const reviewSearch = document.getElementById("reviewSearch");

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

        async function loadReviews() {
          const query = reviewSearch.value.trim();
          const url = query ? `/admin/reviews?q=${encodeURIComponent(query)}` : "/admin/reviews";
          const resp = await fetch(url, { headers: getHeaders() });
          if (!resp.ok) throw new Error("Could not load saved reviews");
          const data = await resp.json();
          const items = data.reviews || [];
          reviewList.innerHTML = items.length ? items.map(item => `
            <div class="item">
              <div><strong>${escapeHtml(item.extracted_claim || "No extracted claim stored")}</strong></div>
              <div class="meta">${escapeHtml(item.system_verdict || "No verdict")} • ${escapeHtml(item.claim_status || "unknown")} • Updated ${escapeHtml(item.updated_at || item.created_at || "")}</div>
              <div style="margin-top:8px;"><strong>Chosen evidence:</strong> <a href="${escapeHtml(item.selected_evidence_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.selected_evidence_title || item.selected_evidence_url || "Open link")}</a></div>
              <div class="meta">${escapeHtml(item.selected_evidence_snippet || "")}</div>
              <div style="margin-top:8px;"><strong>Post text:</strong> ${escapeHtml(item.post_text || "")}</div>
              <div style="margin-top:8px;"><strong>Verdict markdown:</strong></div>
              <div style="margin-top:4px; white-space:pre-wrap;">${escapeHtml(item.verdict_markdown || "")}</div>
              <div style="margin-top:8px;"><strong>Notes:</strong> ${escapeHtml(item.notes || "")}</div>
              <div>${(item.all_evidence || []).map(link => `<span class="pill">${escapeHtml(link.title || link.url || "Evidence link")}</span>`).join("")}</div>
            </div>
          `).join("") : '<div class="item">No saved reviews yet.</div>';
        }

        async function refreshAll() {
          try {
            await Promise.all([loadEvidence(), loadReviews(), loadCache()]);
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
        document.getElementById("searchReviewsBtn").addEventListener("click", loadReviews);
        reviewSearch.addEventListener("keydown", (event) => {
          if (event.key === "Enter") loadReviews();
        });

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
    try:
        if not request.text:
            raise HTTPException(status_code=400, detail="Empty text provided")

        print(
            f"[factcheck] start text_chars={len(request.text)} "
            f"selected_claim={'yes' if request.selected_claim else 'no'}"
        )

        # 0. Check cache
        cached_result = CacheService.get_cached_verdict(request.text)
        if cached_result:
            verdict_md = cached_result.get("verdict_markdown")
            evidence_links_cached = cached_result.get("evidence_links", [])
            metadata = cached_result.get("metadata", {})
            cache_version = metadata.get("cache_version")
            if cache_version != CACHE_VERSION:
                print(
                    f"Skipping stale cache for: {request.text[:50]}... "
                    f"(cached version={cache_version}, expected={CACHE_VERSION})"
                )
                cached_result = None
            elif verdict_md and ("AI error" in verdict_md or "Fact-checking error" in verdict_md):
                print(f"Skipping cached error result for: {request.text[:50]}...")
                cached_result = None

        if cached_result:
            print(f"[factcheck] cache hit for: {request.text[:50]}...")
            verdict_md = cached_result.get("verdict_markdown")
            evidence_links_cached = cached_result.get("evidence_links", [])
            metadata = cached_result.get("metadata", {})
            filtered_cached = [
                e for e in (evidence_links_cached or [])
                if e.get('url') and not _is_social_link(e.get('url')) and not _is_pdf_link(e.get('url'))
            ]
            evidence_links_resp = [
                EvidenceLink(title=e.get("title", "Source"), url=e.get("url", ""), snippet=e.get("snippet", ""))
                for e in filtered_cached
            ]
            claim_options = []
            for option in metadata.get("claim_options", []) or []:
                option_links = [
                    EvidenceLink(title=e.get("title", "Source"), url=e.get("url", ""), snippet=e.get("snippet", ""))
                    for e in option.get("evidence_links", [])
                    if e.get("url")
                ]
                claim_options.append(ClaimOption(claim=option.get("claim", ""), evidence_links=option_links))
            return FactCheckResponse(
                verdict_md=verdict_md,
                extracted_claim=metadata.get("extracted_claim", ""),
                extracted_claims=metadata.get("extracted_claims", []),
                claim_options=claim_options,
                evidence_links=evidence_links_resp,
                is_cached=True,
                claim_status=metadata.get("claim_status", "factual_claim"),
                claim_reason=metadata.get("claim_reason", ""),
            )

        print("[factcheck] preparing claim extraction")
        claim_source_text = _extract_media_focus_text(request.text) or request.text
        if claim_source_text != request.text:
            print(f"[factcheck] media-focused extraction text selected: {claim_source_text[:120]}")

        claim_status = "factual_claim"
        claim_reason = ""
        scam_override = _detect_scam_like_claim(claim_source_text)
        if scam_override:
            print("[factcheck] scam-like claim override triggered")
            fallback_claim = scam_override.get("claim", "").strip()
            claim_reason = scam_override.get("reason", "")
        else:
            fallback_claim = gemini_service.extract_claim(claim_source_text)

        print("[factcheck] extracting candidate claims")
        extracted_claims = gemini_service.extract_claims(claim_source_text, max_claims=3)
        if fallback_claim and fallback_claim not in extracted_claims:
            extracted_claims.insert(0, fallback_claim)
        attribution_claim = _extract_attribution_claim(request.text, _extract_suspected_author(request.text))
        if attribution_claim and attribution_claim not in extracted_claims:
            extracted_claims.append(attribution_claim)
        extracted_claims = [claim for claim in extracted_claims if claim][:3]

        selected_claim = (request.selected_claim or "").strip()
        if selected_claim and selected_claim in extracted_claims:
            extracted_claim = selected_claim
        else:
            extracted_claim = extracted_claims[0] if extracted_claims else fallback_claim
        print(f"[factcheck] active claim: {extracted_claim}")

        suspected_author = _extract_suspected_author(request.text)
        prioritize_authorship = _should_prioritize_authorship(request.text, extracted_claim, suspected_author)

        print("[factcheck] gathering search results")
        claim_options = []
        claim_results_map = {}
        preview_claims = extracted_claims[:2]
        for claim in preview_claims:
            claim_results = _search_claim_results(claim, request.text, request.category, suspected_author=suspected_author)
            claim_results_map[claim] = claim_results
            option_links = [
                EvidenceLink(
                    title=r.get("title", "Source"),
                    url=r.get("link", ""),
                    snippet=r.get("snippet", "")
                )
                for r in claim_results[:3]
                if r.get("link") and not _is_social_link(r.get("link")) and not _is_pdf_link(r.get("link"))
            ]
            claim_options.append(ClaimOption(claim=claim, evidence_links=option_links))

        if extracted_claim not in claim_results_map:
            claim_results_map[extracted_claim] = _search_claim_results(
                extracted_claim,
                request.text,
                request.category,
                suspected_author=suspected_author,
            )
            claim_options.append(
                ClaimOption(
                    claim=extracted_claim,
                    evidence_links=[
                        EvidenceLink(
                            title=r.get("title", "Source"),
                            url=r.get("link", ""),
                            snippet=r.get("snippet", ""),
                        )
                        for r in claim_results_map[extracted_claim][:3]
                        if r.get("link") and not _is_social_link(r.get("link")) and not _is_pdf_link(r.get("link"))
                    ],
                )
            )

        deduped_claim_options = []
        seen_claims = set()
        for option in claim_options:
            key = option.claim.strip().lower()
            if not key or key in seen_claims:
                continue
            seen_claims.add(key)
            deduped_claim_options.append(option)
        claim_options = deduped_claim_options[:3]

        search_results = claim_results_map.get(extracted_claim, [])

        print(f"[factcheck] generating verdict using {len(search_results)} search results")
        result_md = gemini_service.fact_check(
            extracted_claim,
            search_results,
            original_text=request.text,
            suspected_author=suspected_author,
            prioritize_authorship=prioritize_authorship,
        )

        safe_results = [
            r for r in search_results
            if r.get('link') and not _is_social_link(r.get('link')) and not _is_pdf_link(r.get('link'))
        ]
        evidence_links = [
            EvidenceLink(
                title=r.get("title", "Source"),
                url=r.get("link", ""),
                snippet=r.get("snippet", "")
            )
            for r in safe_results
        ]

        evidence_links_for_cache = [
            {"title": e.title, "url": e.url, "snippet": e.snippet} for e in evidence_links
        ]

        if "Fact-checking error" not in result_md and "AI error" not in result_md:
            print("[factcheck] saving successful result to cache")
            CacheService.save_to_cache(
                request.text,
                result_md,
                evidence_links_for_cache,
                metadata={
                    "cache_version": CACHE_VERSION,
                    "extracted_claim": extracted_claim,
                    "extracted_claims": extracted_claims,
                    "claim_options": [
                        {
                            "claim": option.claim,
                            "evidence_links": [
                                {"title": e.title, "url": e.url, "snippet": e.snippet}
                                for e in option.evidence_links
                            ],
                        }
                        for option in claim_options
                    ],
                    "claim_status": claim_status,
                    "claim_reason": claim_reason,
                },
            )

        print("[factcheck] completed successfully")
        return FactCheckResponse(
            verdict_md=result_md,
            extracted_claim=extracted_claim,
            extracted_claims=extracted_claims,
            claim_options=claim_options,
            evidence_links=evidence_links,
            is_cached=False,
            claim_status=claim_status,
            claim_reason=claim_reason,
        )
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[factcheck] unhandled error: {exc}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Fact-check pipeline failed: {exc}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
