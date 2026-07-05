from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
import re
import os
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from dotenv import load_dotenv
try:
    from .logging_config import configure_logging
    from .services import SerperService, GeminiService, DuckDuckGoService, _is_social_link, _is_pdf_link
    from .database import init_db, CacheService, CuratedEvidenceService, ReviewService
    from .media_routes import router as media_router
except ImportError:
    from logging_config import configure_logging
    from services import SerperService, GeminiService, DuckDuckGoService, _is_social_link, _is_pdf_link
    from database import init_db, CacheService, CuratedEvidenceService, ReviewService
    from media_routes import router as media_router

load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app):
    logger.info("starting application")
    init_db()
    logger.info("database initialized")
    yield
    logger.info("stopping application")

app = FastAPI(lifespan=lifespan, title="SRT Fact-Check AI API")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started_at = time.perf_counter()
    method = request.method
    path = request.url.path
    client = request.client.host if request.client else "unknown"
    logger.info("request started method=%s path=%s client=%s", method, path, client)
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.exception(
            "request failed method=%s path=%s client=%s duration_ms=%.2f",
            method,
            path,
            client,
            duration_ms,
        )
        raise
    duration_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "request completed method=%s path=%s client=%s status_code=%s duration_ms=%.2f",
        method,
        path,
        client,
        response.status_code,
        duration_ms,
    )
    return response

# Enable CORS for the Chrome Extension
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(media_router)

class FactCheckRequest(BaseModel):
    text: str
    category: Optional[str] = None
    subcategory: Optional[str] = None
    selected_claim: Optional[str] = None
    evidence_strategy: Optional[str] = None

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
    evidence_strategy: str = "neutral"
    detected_language: str = "unknown"
    language_label: str = "Unknown"
    language_confidence: str = "low"


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
    rater_decision: str = ""
    verdict_md: str = ""
    selected_evidence_url: str = ""
    selected_evidence_title: str = ""
    selected_evidence_snippet: str = ""
    evidence_links: List[EvidenceLink] = []
    notes: str = ""


gemini_service = GeminiService()

CACHE_VERSION = "2026-07-05-single-claim-selection"

UKRAINIAN_NEWS_DOMAINS = [
    "kyivindependent.com",
    "pravda.com.ua",
    "eurointegration.com.ua",
    "ukrinform.net",
    "suspilne.media",
    "hromadske.ua",
    "detector.media",
    "forbes.ua",
    "tsn.ua",
    "unian.ua",
    "rbc.ua",
    "obozrevatel.com",
    "nv.ua",
    "liga.net",
    "censor.net",
    "armyinform.com.ua",
    "mil.in.ua",
]

UKRAINIAN_FACTCHECK_DOMAINS = [
    "stopfake.org",
    "voxukraine.org",
    "detector.media",
    "euvsdisinfo.eu",
]

UKRAINIAN_SOURCE_DOMAINS = UKRAINIAN_FACTCHECK_DOMAINS + UKRAINIAN_NEWS_DOMAINS

SPANISH_FACTCHECK_DOMAINS = [
    "maldita.es",
    "newtral.es",
    "verificat.cat",
    "chequeado.com",
    "colombiacheck.com",
    "animalpolitico.com",
    "verificado.com.mx",
    "efe.com",
    "afp.com",
]

SPANISH_NEWS_DOMAINS = [
    "elpais.com",
    "bbc.com",
    "cnnespanol.cnn.com",
    "univision.com",
    "telemundo.com",
    "efe.com",
]

SPANISH_SOURCE_DOMAINS = SPANISH_FACTCHECK_DOMAINS + SPANISH_NEWS_DOMAINS

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


def _has_cyrillic(value: str) -> bool:
    return bool(re.search(r"[\u0400-\u04ff]", value or ""))


def _spanish_signal_score(value: str) -> int:
    lowered = f" {(value or '').lower()} "
    accented_chars = len(re.findall(r"[áéíóúñü¿¡]", lowered))
    spanish_words = [
        " el ", " la ", " los ", " las ", " de ", " del ", " que ", " para ", " por ",
        " con ", " una ", " un ", " en ", " sobre ", " gobierno ", " presidente ",
        " vacuna", " elecciones", " falso", " engañoso", " verificado", "según",
        " años", " país", " policía", " salud", " dinero", " migrantes",
    ]
    word_hits = sum(1 for marker in spanish_words if marker in lowered)
    return accented_chars + word_hits


def _looks_ukraine_related(value: str) -> bool:
    lowered = (value or "").lower()
    markers = [
        "укра",
        "київ",
        "киев",
        "зеленськ",
        "зеленск",
        "zelensky",
        "zelenskyy",
        "ukraine",
        "ukrainian",
        "russia",
        "рос",
        "війна",
        "война",
        "kyiv",
        "kiev",
    ]
    return any(marker in lowered for marker in markers)


def _is_ukraine_context(original_text: str, claim_text: str = "") -> bool:
    combined = f"{original_text}\n{claim_text}"
    return _has_cyrillic(combined) or _looks_ukraine_related(combined)


def _detect_language_context(original_text: str, claim_text: str = "") -> Dict[str, str]:
    combined = _normalize_space(f"{original_text}\n{claim_text}")
    ukraine_context = _is_ukraine_context(original_text, claim_text)
    spanish_score = _spanish_signal_score(combined)

    if ukraine_context and spanish_score >= 3:
        return {
            "language": "spanish_ukraine_context",
            "label": "Spanish / Ukraine context",
            "region": "ua",
            "search_gl": "ua",
            "search_hl": "es",
            "ddg_region": "ua-uk",
            "confidence": "high" if _has_cyrillic(combined) or spanish_score >= 6 else "medium",
        }

    if ukraine_context:
        has_ukrainian_chars = bool(re.search(r"[іїєґІЇЄҐ]", combined))
        has_cyrillic = _has_cyrillic(combined)
        return {
            "language": "ukrainian" if has_ukrainian_chars or has_cyrillic else "ukraine_context",
            "label": "Ukrainian / Ukraine context" if has_cyrillic else "Ukraine context",
            "region": "ua",
            "search_gl": "ua",
            "search_hl": "uk",
            "ddg_region": "ua-uk",
            "confidence": "high" if has_cyrillic else "medium",
        }

    if spanish_score >= 3:
        return {
            "language": "spanish",
            "label": "Spanish",
            "region": "es",
            "search_gl": "es",
            "search_hl": "es",
            "ddg_region": "es-es",
            "confidence": "medium" if spanish_score < 6 else "high",
        }

    return {
        "language": "english_or_unknown",
        "label": "English / unknown",
        "region": "",
        "search_gl": "",
        "search_hl": "",
        "ddg_region": "",
        "confidence": "low",
    }


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


def _extract_labeled_section(text: str, marker: str, stop_markers: Optional[List[str]] = None, max_chars: int = 1200) -> str:
    if not text:
        return ""

    stop_markers = stop_markers or []
    normalized_text = text.replace("\r", "")
    if marker.lower() not in normalized_text.lower():
        return ""

    lines = normalized_text.splitlines()
    capture = False
    captured = []
    marker_lower = marker.lower()
    stop_pattern = "|".join(re.escape(item.lower().rstrip(":")) for item in stop_markers)
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if capture and captured:
                break
            continue

        if line.lower().startswith(marker_lower):
            capture = True
            remainder = line[len(marker):].strip()
            if remainder:
                captured.append(remainder)
            continue

        if capture:
            lowered = line.lower().rstrip(":")
            if stop_pattern and re.match(rf"^({stop_pattern})\b", lowered, flags=re.IGNORECASE):
                break
            captured.append(line)
            if len(" ".join(captured)) > max_chars:
                break

    return _normalize_space(" ".join(captured))


def _extract_factcheck_source_text(text: str) -> str:
    if not text:
        return ""

    content = _extract_labeled_section(
        text,
        "Content In Review:",
        stop_markers=["Transcript:", "Text in Media:", "All detected text:", "Creation time:", "Link information:"],
        max_chars=1600,
    )
    transcript = _extract_labeled_section(
        text,
        "Transcript:",
        stop_markers=["Text in Media:", "All detected text:", "Creation time:", "Link information:"],
        max_chars=900,
    )
    media = (
        _extract_labeled_section(text, "Text in Media:", stop_markers=["All detected text:", "Creation time:", "Link information:"], max_chars=500)
        or _extract_labeled_section(text, "All detected text:", stop_markers=["Creation time:", "Link information:"], max_chars=500)
    )

    sections = []
    if content:
        sections.append(content)
    if transcript and transcript not in sections:
        sections.append(transcript)
    if media and media not in sections:
        sections.append(media)
    if sections:
        return _normalize_space("\n\n".join(sections))

    return ""


def _extract_srt_post_claim_text(text: str) -> str:
    lines = [_normalize_space(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    content_lines = []
    skip_patterns = [
        r"^content in review$",
        r"^creation time$",
        r"^link information$",
        r"^retry detection$",
        r"^fact check claim$",
        r"^pin to page$",
        r"^unpin from page$",
        r"^possible claims$",
        r"^trust signals$",
        r"^hide all$",
    ]
    for line in lines:
        lowered = line.lower()
        if any(re.search(pattern, lowered) for pattern in skip_patterns):
            continue
        if lowered.startswith("hide translation"):
            continue
        if " • hide all" in lowered:
            before_marker = re.split(r"hide translation\s*\([^)]*\)\s*•\s*hide all", line, flags=re.IGNORECASE)[0]
            after_marker = re.split(r"hide translation\s*\([^)]*\)\s*•\s*hide all", line, flags=re.IGNORECASE)[-1]
            for part in (before_marker, after_marker):
                part = _normalize_space(part)
                if len(part) >= 12:
                    content_lines.append(part)
            continue
        if len(line) >= 12:
            content_lines.append(line)

    if not content_lines:
        return ""

    focused = []
    for line in content_lines[:4]:
        if line not in focused:
            focused.append(line)
        if len(" ".join(focused)) > 320:
            break
    return _normalize_space(" ".join(focused))


def _claim_terms(value: str) -> set:
    return {
        term.lower()
        for term in re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ0-9]{4,}", value or "")
        if term.lower() not in {"hide", "translation", "ukrainian", "content", "review", "claim"}
    }


def _is_claim_grounded_in_text(claim: str, source_text: str) -> bool:
    claim_terms = _claim_terms(claim)
    if not claim_terms:
        return False
    source_terms = _claim_terms(source_text)
    if not source_terms:
        return True
    overlap = claim_terms & source_terms
    if len(claim_terms) <= 3:
        return len(overlap) >= max(1, len(claim_terms) - 1)
    return len(overlap) >= 2 and (len(overlap) / len(claim_terms)) >= 0.35


def _fallback_claim_from_post_text(source_text: str) -> str:
    normalized = _normalize_space(source_text)
    if not normalized:
        return ""
    segments = [
        _normalize_space(segment)
        for segment in re.split(r"(?<=[.!?。！？])\s+|[•|]", normalized)
        if _normalize_space(segment)
    ]
    for segment in segments:
        if len(segment) >= 12 and not segment.lower().startswith("hide translation"):
            return segment[:240]
    return normalized[:240]


def _build_search_queries(original_text: str, extracted_claim: str, search_terms: str = "") -> List[str]:
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

    def _source_language_fragment(text: str) -> str:
        candidates = []
        for line in (text or "").splitlines():
            normalized = _normalize_space(line)
            if len(normalized) < 12:
                continue
            if re.search(r"^(content in review|hide translation|hide all|all detected text|transcript)\b", normalized, flags=re.IGNORECASE):
                continue
            if _has_cyrillic(normalized):
                candidates.append(normalized)
        if not candidates:
            return ""
        return max(candidates, key=len)[:180]

    def _source_keyword_query(text: str) -> str:
        stopwords = {
            "hide", "translation", "ukrainian", "українців", "україна", "україни",
            "також", "зокрема", "який", "яка", "які", "вони", "адже", "через",
            "with", "that", "have", "this", "from", "about", "their", "there",
        }
        terms = []
        for term in re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ0-9]{4,}", text or ""):
            lowered = term.lower()
            if lowered in stopwords:
                continue
            if lowered not in terms:
                terms.append(lowered)
            if len(terms) >= 10:
                break
        return " ".join(terms)

    queries: List[str] = []
    suspected_author = _extract_suspected_author(original_text)
    quote_fragment = _extract_quote_fragment(original_text, suspected_author)
    source_fragment = _source_language_fragment(original_text)
    source_keyword_query = _source_keyword_query(source_fragment)

    language_context = _detect_language_context(original_text, extracted_claim)
    is_ukraine_context = language_context["language"] in {"ukrainian", "ukraine_context", "spanish_ukraine_context"}
    is_spanish_context = language_context["language"] in {"spanish", "spanish_ukraine_context"}

    if is_ukraine_context:
        if search_terms:
            queries.append(search_terms)
        if source_fragment:
            queries.append(source_fragment)
            first_words = " ".join(source_fragment.split()[:10])
            if first_words:
                queries.append(f'"{first_words}"')
        if source_keyword_query:
            queries.append(f"{source_keyword_query} українські новини")
            for domain in ["forbes.ua", "tsn.ua", "unian.ua", "rbc.ua"]:
                queries.append(f"{source_keyword_query} site:{domain}")
        for domain in UKRAINIAN_FACTCHECK_DOMAINS[:1]:
            queries.append(f"{extracted_claim} site:{domain}")
        queries.append(f"{extracted_claim} українські новини")
        queries.append(f"{extracted_claim} Reuters AP")
    if is_spanish_context:
        for domain in SPANISH_FACTCHECK_DOMAINS[:2]:
            queries.append(f"{extracted_claim} site:{domain}")
        queries.append(f"{extracted_claim} verificación de datos")
        queries.append(f"{extracted_claim} noticias evidencia")
    if search_terms:
        queries.append(f"{search_terms} evidence")
    queries.append(f"{extracted_claim} evidence")
    queries.append(f"{extracted_claim} latest reporting")

    scam_like = _detect_scam_like_claim(f"{original_text}\n{extracted_claim}")
    if scam_like:
        queries.append(f"{extracted_claim} official warning")

    attribution_claim = _extract_attribution_claim(original_text, suspected_author)
    if _looks_like_attributed_post(original_text) and suspected_author:
        if quote_fragment:
            queries.append(f'"{suspected_author}" "{quote_fragment}" news')
            queries.append(f'"{suspected_author}" "{quote_fragment}" fact check')
        if attribution_claim and extracted_claim.strip().lower() != attribution_claim.strip().lower():
            queries.append(f'"{suspected_author}" said news Reuters AP')
        queries.append(f'"{suspected_author}" statement Reuters AP BBC')
        queries.append(f'"{suspected_author}" post verified news')
    if is_ukraine_context or is_spanish_context:
        return _dedupe(queries)[:7]
    return _dedupe(queries)[:2]


def _normalize_evidence_strategy(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower().replace("_", "-")
    if normalized in {"refute", "refutation", "refutation-focused", "debunk", "debunking"}:
        return "refutation"
    return "neutral"


def _infer_evidence_strategy(requested_strategy: Optional[str], claim_source_text: str, extracted_claim: str) -> str:
    strategy = _normalize_evidence_strategy(requested_strategy)
    if strategy == "refutation":
        return strategy
    if _detect_scam_like_claim(f"{claim_source_text}\n{extracted_claim}"):
        return "refutation"
    lowered = f" {claim_source_text} {extracted_claim} ".lower()
    refutation_markers = [
        "scam",
        "hoax",
        "fake",
        "debunk",
        "false claim",
        "manipulated",
        "misleading",
        "disinformation",
        "дезінформац",
        "фейк",
        "шахрай",
        "маніпуляц",
    ]
    return "refutation" if any(marker in lowered for marker in refutation_markers) else "neutral"


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


def _source_domain(link: str) -> str:
    import urllib.parse
    try:
        host = urllib.parse.urlparse(link).hostname or ""
    except Exception:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _is_ukrainian_source(link: str) -> bool:
    host = _source_domain(link)
    return any(host == domain or host.endswith("." + domain) for domain in UKRAINIAN_SOURCE_DOMAINS)


def _is_spanish_source(link: str) -> bool:
    host = _source_domain(link)
    return any(host == domain or host.endswith("." + domain) for domain in SPANISH_SOURCE_DOMAINS)


def _rank_evidence_results(results: List[Dict], language_context: Optional[Dict[str, str]] = None) -> List[Dict]:
    language = (language_context or {}).get("language", "")
    if language not in {"ukrainian", "ukraine_context", "spanish", "spanish_ukraine_context"}:
        return results
    def source_matcher(link: str) -> bool:
        if language == "spanish_ukraine_context":
            return _is_spanish_source(link) or _is_ukrainian_source(link)
        if language == "spanish":
            return _is_spanish_source(link)
        return _is_ukrainian_source(link)
    return sorted(
        results,
        key=lambda item: (
            0 if source_matcher(item.get("link", "")) else 1,
            len(item.get("snippet", "")) == 0,
        ),
    )


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
        'politifact.com', 'fullfact.org', 'afp.com', 'africacheck.org', 'leadstories.com',
        *UKRAINIAN_FACTCHECK_DOMAINS,
        *UKRAINIAN_NEWS_DOMAINS,
        *SPANISH_FACTCHECK_DOMAINS,
        *SPANISH_NEWS_DOMAINS,
    ]

    category_allowlists = {
        'health': ['cdc.gov', 'who.int', 'nejm.org', 'hmh.com'],
        'politics': ['politifact.com', 'factcheck.org', 'apnews.com', 'reuters.com', 'africacheck.org', 'leadstories.com', *UKRAINIAN_FACTCHECK_DOMAINS, *UKRAINIAN_NEWS_DOMAINS, *SPANISH_FACTCHECK_DOMAINS, *SPANISH_NEWS_DOMAINS],
        'economy': ['ft.com', 'economist.com', 'bloomberg.com', 'wsj.com'],
        'science': ['nature.com', 'sciencemag.org', 'who.int'],
        'international': ['reuters.com', 'apnews.com', 'bbc.com', 'aljazeera.com', 'afp.com', *UKRAINIAN_FACTCHECK_DOMAINS, *UKRAINIAN_NEWS_DOMAINS, *SPANISH_FACTCHECK_DOMAINS, *SPANISH_NEWS_DOMAINS],
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


def _search_claim_results(claim_text: str, original_text: str, category: Optional[str], suspected_author: str = "", search_terms: str = "") -> List[Dict]:
    search_queries = _build_search_queries(original_text, claim_text, search_terms=search_terms)
    language_context = _detect_language_context(original_text, claim_text)
    is_regional_context = language_context["language"] in {"ukrainian", "ukraine_context", "spanish", "spanish_ukraine_context"}
    collected_results = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(search_queries)))) as executor:
        futures = [
            executor.submit(
                SerperService.search,
                query,
                gl=language_context.get("search_gl", ""),
                hl=language_context.get("search_hl", ""),
            )
            for query in search_queries
        ]
        for future in as_completed(futures):
            serper_results = future.result()
            if serper_results:
                collected_results.append(serper_results)

    if not collected_results:
        logger.info("Serper returned no results; falling back to DuckDuckGo")
        fallback_queries = search_queries[:2]
        with ThreadPoolExecutor(max_workers=min(2, max(1, len(fallback_queries)))) as executor:
            futures = [
                executor.submit(
                    DuckDuckGoService.search,
                    query,
                    max_results=5,
                    region=language_context.get("ddg_region", ""),
                )
                for query in fallback_queries
            ]
            for future in as_completed(futures):
                ddg_results = future.result()
                if ddg_results:
                    collected_results.append(ddg_results)

    search_results = _merge_search_results(collected_results, max_results=10 if is_regional_context else 8)

    if suspected_author and _should_prioritize_authorship(original_text, claim_text, suspected_author):
        logger.info("authorship-sensitive search triggered claim=%r", claim_text)

    return _rank_evidence_results(_filter_credible(search_results, category=category), language_context=language_context)


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
    if not payload.selected_evidence_url.strip() and not payload.rater_decision.strip():
        raise HTTPException(status_code=400, detail="selected_evidence_url or rater_decision is required")

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
        rater_decision=payload.rater_decision.strip(),
        verdict_markdown=payload.verdict_md.strip(),
        selected_evidence_url=payload.selected_evidence_url.strip(),
        selected_evidence_title=payload.selected_evidence_title.strip(),
        selected_evidence_snippet=payload.selected_evidence_snippet.strip(),
        all_evidence=evidence_links,
        notes=payload.notes.strip(),
    )

    if payload.selected_evidence_url.strip():
        CuratedEvidenceService.add_entry(
            url=payload.selected_evidence_url.strip(),
            title=payload.selected_evidence_title.strip(),
            source="Rater selected evidence",
            claim_summary=payload.extracted_claim.strip(),
            verdict=payload.rater_decision.strip() or system_verdict,
            notes=payload.notes.strip() or payload.post_text.strip()[:500],
            tags=[tag for tag in [payload.claim_status.strip(), payload.rater_decision.strip(), "rater-selected"] if tag],
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
              <div class="meta">AI: ${escapeHtml(item.system_verdict || "No verdict")} • Rater: ${escapeHtml(item.rater_decision || "Not selected")} • ${escapeHtml(item.claim_status || "unknown")} • Updated ${escapeHtml(item.updated_at || item.created_at || "")}</div>
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

        logger.info(
            "factcheck start text_chars=%s selected_claim=%s",
            len(request.text),
            "yes" if request.selected_claim else "no",
        )

        # 0. Check cache
        cached_result = CacheService.get_cached_verdict(request.text)
        if cached_result:
            verdict_md = cached_result.get("verdict_markdown")
            evidence_links_cached = cached_result.get("evidence_links", [])
            metadata = cached_result.get("metadata", {})
            cache_version = metadata.get("cache_version")
            if cache_version != CACHE_VERSION:
                logger.info(
                    "skipping stale cache text_preview=%r cached_version=%s expected_version=%s",
                    request.text[:50],
                    cache_version,
                    CACHE_VERSION,
                )
                cached_result = None
            elif verdict_md and ("AI error" in verdict_md or "Fact-checking error" in verdict_md):
                logger.info("skipping cached error result text_preview=%r", request.text[:50])
                cached_result = None

        if cached_result:
            logger.info("factcheck cache hit text_preview=%r", request.text[:50])
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
                evidence_strategy=metadata.get("evidence_strategy", "neutral"),
                detected_language=metadata.get("detected_language", "unknown"),
                language_label=metadata.get("language_label", "Unknown"),
                language_confidence=metadata.get("language_confidence", "low"),
            )

        logger.info("factcheck preparing claim extraction")
        claim_source_text = _extract_factcheck_source_text(request.text) or _extract_srt_post_claim_text(request.text) or request.text
        if claim_source_text != request.text:
            logger.info("factcheck media-focused extraction selected text_preview=%r", claim_source_text[:120])
        initial_language_context = _detect_language_context(request.text, claim_source_text)

        claim_status = "factual_claim"
        claim_reason = ""
        scam_override = _detect_scam_like_claim(claim_source_text)
        if scam_override:
            logger.info("factcheck scam-like claim override triggered")
            fallback_claim = scam_override.get("claim", "").strip()
            claim_reason = scam_override.get("reason", "")
        else:
            fallback_claim = ""

        logger.info("factcheck selecting claim to verify")
        selected = gemini_service.select_claim_to_verify(claim_source_text)
        selected_claim = selected.get("claim", "").strip()
        if selected_claim and not _is_claim_grounded_in_text(selected_claim, claim_source_text):
            logger.warning("dropped ungrounded selected claim claim=%r source_preview=%r", selected_claim, claim_source_text[:160])
            selected_claim = ""

        if fallback_claim:
            extracted_claims = [fallback_claim]
            claim_reason = claim_reason or selected.get("reason", "")
        elif selected.get("claim_check") == "NO":
            logger.info("no grounded claim extracted; returning no-claim response")
            return FactCheckResponse(
                verdict_md=(
                    "**Claim Check**: No\n\n"
                    "**Claim**: No clear verifiable factual claim was found.\n\n"
                    "**Recommended Rating**: No Claim\n\n"
                    f"**Why**: {selected.get('reason') or 'The extracted text appears to be advice, opinion, a question, or too vague to verify against evidence.'}\n\n"
                    "**Evidence**: No evidence search was run because there is no specific factual claim to check.\n\n"
                    "**Links**: None"
                ),
                extracted_claim="",
                extracted_claims=[],
                claim_options=[],
                evidence_links=[],
                is_cached=False,
                claim_status="no_claim",
                claim_reason="No grounded verifiable claim was extracted from the post text.",
                evidence_strategy="neutral",
                detected_language=initial_language_context.get("language", "unknown"),
                language_label=initial_language_context.get("label", "Unknown"),
                language_confidence=initial_language_context.get("confidence", "low"),
            )
        elif selected_claim:
            extracted_claims = [selected_claim]
            claim_reason = selected.get("reason", "")
        else:
            model_claims = gemini_service.extract_claims(claim_source_text, max_claims=1)
            extracted_claims = [
                claim for claim in model_claims
                if _is_claim_grounded_in_text(claim, claim_source_text)
            ]
            if not extracted_claims:
                logger.info("no grounded fallback claim extracted; returning unclear response")
                return FactCheckResponse(
                    verdict_md=(
                        "**Claim Check**: Unclear\n\n"
                        "**Claim**: The tool could not identify one clear factual claim to verify.\n\n"
                        "**Recommended Rating**: Needs Review\n\n"
                        "**Why**: The post may contain context or implied meaning that needs human judgment before searching evidence.\n\n"
                        "**Evidence**: No evidence search was run because the claim was unclear.\n\n"
                        "**Links**: None"
                    ),
                    extracted_claim="",
                    extracted_claims=[],
                    claim_options=[],
                    evidence_links=[],
                    is_cached=False,
                    claim_status="unclear_claim",
                    claim_reason=selected.get("reason", "The claim-selection step did not return a grounded claim."),
                    evidence_strategy="neutral",
                    detected_language=initial_language_context.get("language", "unknown"),
                    language_label=initial_language_context.get("label", "Unknown"),
                    language_confidence=initial_language_context.get("confidence", "low"),
                )
        attribution_claim = _extract_attribution_claim(request.text, _extract_suspected_author(request.text))
        if attribution_claim and attribution_claim not in extracted_claims:
            extracted_claims.append(attribution_claim)
        extracted_claims = [claim for claim in extracted_claims if claim][:3]

        selected_claim = (request.selected_claim or "").strip()
        if selected_claim and selected_claim in extracted_claims:
            extracted_claim = selected_claim
        else:
            extracted_claim = extracted_claims[0] if extracted_claims else fallback_claim
        language_context = _detect_language_context(request.text, extracted_claim)
        logger.info("factcheck active claim=%r", extracted_claim)

        suspected_author = _extract_suspected_author(request.text)
        prioritize_authorship = _should_prioritize_authorship(request.text, extracted_claim, suspected_author)
        evidence_strategy = _infer_evidence_strategy(request.evidence_strategy, claim_source_text, extracted_claim)
        logger.info("factcheck evidence_strategy=%s", evidence_strategy)

        logger.info("factcheck gathering search results")
        claim_options = []
        claim_results_map = {}
        claim_results_map[extracted_claim] = _search_claim_results(
            extracted_claim,
            request.text,
            request.category,
            suspected_author=suspected_author,
            search_terms=selected.get("search_terms", ""),
        )
        for claim in extracted_claims:
            links = []
            if claim == extracted_claim:
                links = [
                    EvidenceLink(
                        title=r.get("title", "Source"),
                        url=r.get("link", ""),
                        snippet=r.get("snippet", ""),
                    )
                    for r in claim_results_map[extracted_claim][:3]
                    if r.get("link") and not _is_social_link(r.get("link")) and not _is_pdf_link(r.get("link"))
                ]
            claim_options.append(
                ClaimOption(
                    claim=claim,
                    evidence_links=links,
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

        logger.info("factcheck generating verdict search_result_count=%s", len(search_results))
        result_md = gemini_service.fact_check(
            extracted_claim,
            search_results,
            original_text=request.text,
            suspected_author=suspected_author,
            prioritize_authorship=prioritize_authorship,
            evidence_strategy=evidence_strategy,
            language_context=language_context,
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
            logger.info("factcheck saving successful result to cache")
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
                    "evidence_strategy": evidence_strategy,
                    "detected_language": language_context.get("language", initial_language_context.get("language", "unknown")),
                    "language_label": language_context.get("label", initial_language_context.get("label", "Unknown")),
                    "language_confidence": language_context.get("confidence", initial_language_context.get("confidence", "low")),
                },
            )

        logger.info("factcheck completed successfully")
        return FactCheckResponse(
            verdict_md=result_md,
            extracted_claim=extracted_claim,
            extracted_claims=extracted_claims,
            claim_options=claim_options,
            evidence_links=evidence_links,
            is_cached=False,
            claim_status=claim_status,
            claim_reason=claim_reason,
            evidence_strategy=evidence_strategy,
            detected_language=language_context.get("language", "unknown"),
            language_label=language_context.get("label", "Unknown"),
            language_confidence=language_context.get("confidence", "low"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("factcheck unhandled error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Fact-check pipeline failed: {exc}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
