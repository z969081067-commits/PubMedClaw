"""Search PubMed from natural-language requests and optionally download PDFs via PMC."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from html import unescape
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

try:
    import requests
except ModuleNotFoundError as error:
    requests = None  # type: ignore[assignment]
    REQUESTS_IMPORT_ERROR = error

    class RequestsRequestException(Exception):
        """Fallback request exception when requests is unavailable."""

else:
    REQUESTS_IMPORT_ERROR = None
    RequestsRequestException = requests.RequestException


ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PMC_IDCONV_URL = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
HTTP_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 60
HTTP_RETRY_ATTEMPTS = 3
HTTP_RETRY_BACKOFF_SECONDS = 1.0
SELF_CHECK_TIMEOUT_SECONDS = 10
DEFAULT_MAX_RESULTS = 10
DEFAULT_RECENT_YEARS = 5
DEFAULT_DOWNLOAD_DIR_NAME = "pubmed-downloads"
MAX_FILENAME_LENGTH = 120
USER_AGENT = "pubmed_paper_finder/3.1"
PDF_ACCEPT_HEADER = "application/pdf,application/octet-stream;q=0.9,text/html;q=0.8,*/*;q=0.5"
QUALITY_BOOST_ARTICLE_TYPES = {"systematic review", "meta-analysis", "review", "guideline"}
WINDOWS_RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

STOPWORDS = {
    "a", "an", "and", "article", "articles", "biomedical", "find", "for", "from", "get", "in",
    "literature", "look", "of", "on", "paper", "papers", "recent", "review", "reviews", "search",
    "since", "studies", "study", "the", "to", "with",
}

SYNONYM_MAP = {
    "alzheimer's disease": ["alzheimer disease", "alzheimers disease", "ad"],
    "alzheimers disease": ["alzheimer's disease", "alzheimer disease", "ad"],
    "gut microbiota": ["gut microbiome", "intestinal microbiota", "microbiome"],
    "microbiota": ["microbiome", "intestinal microbiota"],
    "microbiome": ["microbiota", "gut microbiome"],
    "crispr": ["crispr-cas", "gene editing"],
    "plant disease resistance": ["plant immunity", "crop disease resistance", "disease resistance"],
    "colorectal cancer": ["colon cancer", "colorectal neoplasms", "crc"],
    "breast cancer": ["mammary carcinoma", "breast neoplasms"],
    "inflammatory bowel disease": ["ibd", "crohn disease", "ulcerative colitis"],
    "parkinson's disease": ["parkinson disease", "pd"],
    "immune checkpoint": ["checkpoint blockade", "pd-1", "pd-l1", "ctla-4"],
}

SPECIES_RULES = {
    "human": {"aliases": ["human", "humans", "patient", "patients", "clinical"], "pubmed": '"Humans"[MeSH Terms]', "score_terms": ["human", "humans", "patient", "patients", "clinical"]},
    "mouse": {"aliases": ["mouse", "mice", "murine", "mouse model", "mouse models"], "pubmed": '"Mice"[MeSH Terms]', "score_terms": ["mouse", "mice", "murine", "mouse model", "mouse models"]},
    "rat": {"aliases": ["rat", "rats", "rodent", "rodents"], "pubmed": '"Rats"[MeSH Terms]', "score_terms": ["rat", "rats", "rodent", "rodents"]},
    "plant": {"aliases": ["plant", "plants", "crop", "crops", "arabidopsis"], "pubmed": '"Plants"[MeSH Terms]', "score_terms": ["plant", "plants", "crop", "crops", "arabidopsis"]},
    "zebrafish": {"aliases": ["zebrafish", "danio rerio"], "pubmed": '"Danio rerio"[MeSH Terms]', "score_terms": ["zebrafish", "danio rerio"]},
    "drosophila": {"aliases": ["drosophila", "fruit fly", "drosophila melanogaster"], "pubmed": '"Drosophila melanogaster"[MeSH Terms]', "score_terms": ["drosophila", "fruit fly", "drosophila melanogaster"]},
}

ARTICLE_TYPE_RULES = {
    "review": {"aliases": ["review", "reviews", "review paper", "review papers"], "pubmed": '"Review"[Publication Type]'},
    "systematic review": {"aliases": ["systematic review", "systematic reviews"], "pubmed": '"Systematic Review"[Publication Type]'},
    "meta-analysis": {"aliases": ["meta-analysis", "meta analyses", "meta analysis", "meta-analyses"], "pubmed": '"Meta-Analysis"[Publication Type]'},
    "clinical trial": {"aliases": ["clinical trial", "clinical trials"], "pubmed": '"Clinical Trial"[Publication Type]'},
    "randomized controlled trial": {"aliases": ["randomized controlled trial", "randomised controlled trial", "rct", "rcts"], "pubmed": '"Randomized Controlled Trial"[Publication Type]'},
    "case report": {"aliases": ["case report", "case reports"], "pubmed": '"Case Reports"[Publication Type]'},
    "guideline": {"aliases": ["guideline", "guidelines"], "pubmed": '"Guideline"[Publication Type]'},
}


class PubMedSearchError(RuntimeError):
    """Raised when PubMed search or parsing fails in a recoverable way."""


class FullTextDownloadError(RuntimeError):
    """Raised when a selected article cannot be downloaded from configured sources."""


@dataclass
class SearchParameters:
    original_request: str
    topic: str
    keywords: List[str]
    keyword_groups: List[List[str]]
    year_start: Optional[int]
    year_end: Optional[int]
    species: List[str]
    article_types: List[str]
    excluded_article_types: List[str]
    include_terms: List[str]
    exclude_terms: List[str]
    max_results: int
    sort_by: str
    article_type_filter_mode: str
    assumptions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data.pop("original_request", None)
        return data


@dataclass
class PaperResult:
    result_index: int
    pmid: str
    pmcid: str
    doi: str
    title: str
    authors: List[str]
    journal: str
    year: Optional[int]
    abstract: str
    publication_types: List[str]
    score: float
    score_breakdown: Dict[str, float]
    explanation: List[str]


@dataclass
class SearchOutcome:
    parameters: SearchParameters
    pubmed_query: str
    retrieved_count: int
    selected_count: int
    results: List[PaperResult]


@dataclass
class DownloadArticle:
    result_index: int
    title: str
    pmid: str
    pmcid: str
    doi: str


@dataclass
class DownloadSuccess:
    result_index: int
    title: str
    file_path: str
    source: str


@dataclass
class DownloadFailure:
    result_index: int
    title: str
    doi: str
    reason_code: str
    reason: str
    reason_detail: str


@dataclass
class DownloadOutcome:
    topic: str
    folder_name: str
    output_dir: str
    selected_indices: List[int]
    downloaded: List[DownloadSuccess]
    failed: List[DownloadFailure]


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def deduplicate_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for item in items:
        key = item.lower()
        if item and key not in seen:
            ordered.append(item)
            seen.add(key)
    return ordered


def split_list_argument(raw_value: Optional[str]) -> List[str]:
    if not raw_value:
        return []
    return [normalize_whitespace(v) for v in re.split(r"[;,]", raw_value) if normalize_whitespace(v)]


def positive_int(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def remove_output_control_phrases(request: str) -> str:
    patterns = [
        r'(?:\s*(?:,|;|\band\b)\s*)?\b(?:save|export|output|write|store)(?:\s+\w+){0,8}?\s+(?:to|into|under|in)\s+["\']?[A-Za-z]:\\[^\r\n"<>|?*]+["\']?',
        r'(?:\s*(?:,|;|\band\b)\s*)?\b(?:save|export|output|write|store)\b[^.;]*',
    ]
    cleaned = request
    for pattern in patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    return normalize_whitespace(cleaned)


def remove_exclusion_segments(request: str) -> str:
    return normalize_whitespace(re.sub(r"\b(?:exclude|excluding|without)\b[^.;]*", " ", request, flags=re.IGNORECASE))


def parse_max_results(request: str) -> Optional[int]:
    for pattern in [r"\btop\s+(\d+)\b", r"\bmax(?:imum)?(?:\s+of)?\s+(\d+)\b", r"\bup to\s+(\d+)\b", r"\b(\d+)\s+(?:papers|articles|results)\b"]:
        match = re.search(pattern, request, flags=re.IGNORECASE)
        if match:
            return max(1, min(int(match.group(1)), 200))
    return None


def parse_year_override(years_value: Optional[str]) -> Tuple[Optional[int], Optional[int], List[str]]:
    assumptions: List[str] = []
    if not years_value:
        return None, None, assumptions
    current_year = datetime.now().year
    normalized = normalize_whitespace(years_value.lower())
    match = re.fullmatch(r"(\d{4})\s*[-:]\s*(\d{4})", normalized)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if end < start:
            start, end = end, start
            assumptions.append("CLI year range was reversed and has been normalized.")
        return start, end, assumptions
    match = re.fullmatch(r"since\s+(\d{4})", normalized)
    if match:
        return int(match.group(1)), current_year, assumptions
    match = re.fullmatch(r"last\s+(\d+)\s+years?", normalized)
    if match:
        span = max(1, int(match.group(1)))
        return current_year - span + 1, current_year, assumptions
    match = re.fullmatch(r"(\d{4})", normalized)
    if match:
        year = int(match.group(1))
        return year, year, assumptions
    assumptions.append(f"Could not parse CLI years value '{years_value}'; no explicit year filter was applied.")
    return None, None, assumptions


def parse_year_range(request: str) -> Tuple[Optional[int], Optional[int], List[str]]:
    assumptions: List[str] = []
    current_year = datetime.now().year
    lowered = request.lower()
    for pattern in [r"\bsince\s+(\d{4})\b", r"\bfrom\s+(\d{4})\s+(?:to|through|until)\s+(\d{4})\b", r"\bbetween\s+(\d{4})\s+and\s+(\d{4})\b", r"\b(\d{4})\s*[-–—]\s*(\d{4})\b"]:
        match = re.search(pattern, lowered)
        if match:
            if len(match.groups()) == 1:
                return int(match.group(1)), current_year, assumptions
            start, end = int(match.group(1)), int(match.group(2))
            if end < start:
                start, end = end, start
                assumptions.append("The requested year range was reversed and has been normalized.")
            return start, end, assumptions
    match = re.search(r"\blast\s+(\d+)\s+years?\b", lowered)
    if match:
        span = max(1, int(match.group(1)))
        return current_year - span + 1, current_year, assumptions
    if re.search(r"\b(recent|latest|newest)\b", lowered):
        assumptions.append(f"Interpreted recency language as a ranking preference favoring the last {DEFAULT_RECENT_YEARS} years, not a hard date filter.")
    return None, None, assumptions


def detect_species(request: str) -> List[str]:
    lowered = request.lower()
    return [canonical for canonical, rule in SPECIES_RULES.items() if any(alias in lowered for alias in rule["aliases"])]


def normalize_article_type(type_name: str) -> Optional[str]:
    lowered = type_name.lower()
    for canonical, rule in ARTICLE_TYPE_RULES.items():
        if lowered == canonical or lowered in rule["aliases"]:
            return canonical
    return None


def detect_article_types(request: str) -> Tuple[List[str], str, List[str]]:
    lowered = request.lower()
    detected = [canonical for canonical, rule in ARTICLE_TYPE_RULES.items() if any(alias in lowered for alias in rule["aliases"])]
    only_language = bool(re.search(r"\b(only|strictly|exclusively|must be)\b", lowered))
    assumptions: List[str] = []
    mode = "none"
    if detected:
        mode = "strict" if only_language else "boost"
        if mode == "boost":
            assumptions.append("Article types mentioned in the request were treated as ranking boosts unless explicitly required.")
    return deduplicate_preserve_order(detected), mode, assumptions


def detect_sort_preference(request: str) -> str:
    if re.search(r"\b(most recent|newest|latest|sort by date|chronological)\b", request.lower()):
        return "date"
    return "relevance"


def extract_include_exclude_terms(request: str) -> Tuple[List[str], List[str], List[str]]:
    def parse_terms(patterns: Sequence[str]) -> List[str]:
        collected: List[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, request, flags=re.IGNORECASE):
                for part in re.split(r",|\band\b", match.group(1), flags=re.IGNORECASE):
                    normalized = normalize_whitespace(part.strip(" .;:"))
                    if normalized:
                        collected.append(normalized)
        return deduplicate_preserve_order(collected)

    include_terms = parse_terms([r"\binclude\s+([^.;]+)", r"\bincluding\s+([^.;]+)"])
    raw_excludes = parse_terms([r"\bexclude\s+([^.;]+)", r"\bexcluding\s+([^.;]+)", r"\bwithout\s+([^.;]+)"])
    exclude_terms: List[str] = []
    excluded_article_types: List[str] = []
    for item in raw_excludes:
        normalized = normalize_article_type(item)
        if normalized:
            excluded_article_types.append(normalized)
        else:
            exclude_terms.append(item)
    return include_terms, deduplicate_preserve_order(exclude_terms), deduplicate_preserve_order(excluded_article_types)


def strip_request_noise(request: str) -> str:
    cleaned = request
    for pattern in [
        r"^[\s,]*(find|search(?: for)?|look for|get|retrieve)\b",
        r"\b(?:top|max(?:imum)?(?: of)?|up to)\s+\d+\b",
        r"\b(?:since|from|between|last)\b[^,.;]*",
        r"\b(prioriti[sz]e|prefer|emphasi[sz]e|focus on)\b[^,.;]*",
        r"\b(?:include|including|exclude|excluding|without)\b[^,.;]*",
        r"\b(most recent|newest|latest|recent)\b",
    ]:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    for canonical, rule in ARTICLE_TYPE_RULES.items():
        for variant in [canonical] + list(rule["aliases"]):
            cleaned = re.sub(rf"\b{re.escape(variant)}\b", " ", cleaned, flags=re.IGNORECASE)
    for canonical, rule in SPECIES_RULES.items():
        if canonical == "plant":
            continue
        for variant in [canonical] + list(rule["aliases"]):
            cleaned = re.sub(rf"\b(in|for|using|within)\s+{re.escape(variant)}(?:\s+models?)?\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:paper|papers|article|articles|study|studies|literature)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = normalize_whitespace(re.sub(r"[,:;]+", " ", cleaned))
    cleaned = re.sub(r"\b(and|or)\b\s*$", "", cleaned, flags=re.IGNORECASE)
    if re.search(r"\bon\b", cleaned, flags=re.IGNORECASE):
        parts = re.split(r"\bon\b", cleaned, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2 and parts[1].strip():
            cleaned = normalize_whitespace(parts[1])
    return cleaned.strip(" -")


def split_topic_chunks(topic: str) -> List[str]:
    working = re.sub(r"\bapplications in\b", " and ", topic, flags=re.IGNORECASE)
    chunks = re.split(r"\s*(?:,|;|\band\b)\s*", working, flags=re.IGNORECASE)
    normalized = [normalize_whitespace(chunk.strip(" -")) for chunk in chunks if normalize_whitespace(chunk)]
    return normalized or [normalize_whitespace(topic)]


def tokenize(value: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9']+", value.lower())


def build_keyword_group_from_chunk(chunk: str) -> List[str]:
    normalized_chunk = chunk.lower()
    if normalized_chunk in SYNONYM_MAP:
        return deduplicate_preserve_order([chunk] + SYNONYM_MAP[normalized_chunk])
    for canonical, synonyms in sorted(SYNONYM_MAP.items(), key=lambda item: len(item[0]), reverse=True):
        if canonical in normalized_chunk:
            return deduplicate_preserve_order([canonical] + synonyms + [chunk])
    return [chunk]


def build_keyword_groups(topic: str, include_terms: Sequence[str]) -> Tuple[List[str], List[List[str]]]:
    groups: List[List[str]] = []
    for chunk in split_topic_chunks(topic):
        group = build_keyword_group_from_chunk(chunk)
        if group:
            groups.append(group)
    for include_term in include_terms:
        groups.append([include_term])
    if not groups:
        tokens = [token for token in tokenize(topic) if token not in STOPWORDS]
        groups = [[token] for token in tokens] if tokens else [[topic]]
    return deduplicate_preserve_order([term for group in groups for term in group]), groups


def parse_request(query: str, args: argparse.Namespace) -> SearchParameters:
    assumptions: List[str] = []
    normalized_query = normalize_whitespace(query)
    cleaned_query = remove_output_control_phrases(normalized_query)
    if cleaned_query != normalized_query:
        assumptions.append("Output-control phrases in the request were ignored; this skill never writes local files during search.")
    free_text_max_results = parse_max_results(cleaned_query)
    year_start, year_end, year_assumptions = parse_year_range(cleaned_query)
    assumptions.extend(year_assumptions)
    detected_species = detect_species(cleaned_query)
    positive_request = remove_exclusion_segments(cleaned_query)
    detected_article_types, filter_mode, article_type_assumptions = detect_article_types(positive_request)
    assumptions.extend(article_type_assumptions)
    include_terms, exclude_terms, excluded_article_types = extract_include_exclude_terms(cleaned_query)
    topic = strip_request_noise(cleaned_query) or cleaned_query
    if topic == cleaned_query:
        assumptions.append("The request topic was ambiguous, so the cleaned query text was used as the topic.")
    keywords, keyword_groups = build_keyword_groups(topic, include_terms)
    max_results = args.max_results or free_text_max_results or DEFAULT_MAX_RESULTS
    if max_results == DEFAULT_MAX_RESULTS and free_text_max_results is None and args.max_results is None:
        assumptions.append(f"No result limit was provided; defaulted to {DEFAULT_MAX_RESULTS} results.")
    if args.years:
        year_start, year_end, cli_year_assumptions = parse_year_override(args.years)
        assumptions.extend(cli_year_assumptions)
    if args.species:
        requested_species = deduplicate_preserve_order([value.lower() for value in split_list_argument(args.species)])
        unsupported_species = [value for value in requested_species if value not in SPECIES_RULES]
        if unsupported_species:
            supported_species = ", ".join(sorted(SPECIES_RULES))
            raise PubMedSearchError(
                "Unsupported species override(s): "
                f"{', '.join(unsupported_species)}. "
                f"Supported species are: {supported_species}."
            )
        detected_species = requested_species
    if args.article_types:
        detected_article_types = []
        for value in split_list_argument(args.article_types):
            normalized = normalize_article_type(value)
            if normalized:
                detected_article_types.append(normalized)
            else:
                assumptions.append(f"Unrecognized article type override '{value}' was ignored.")
        filter_mode = "strict" if detected_article_types else "none"
    sort_by = args.sort_by or detect_sort_preference(cleaned_query)
    if not args.sort_by and sort_by == "relevance":
        assumptions.append("No sort preference was provided; defaulted to relevance ordering.")
    return SearchParameters(
        original_request=query,
        topic=topic,
        keywords=keywords,
        keyword_groups=keyword_groups,
        year_start=year_start,
        year_end=year_end,
        species=detected_species,
        article_types=deduplicate_preserve_order(detected_article_types),
        excluded_article_types=excluded_article_types,
        include_terms=include_terms,
        exclude_terms=exclude_terms,
        max_results=max_results,
        sort_by=sort_by,
        article_type_filter_mode=filter_mode,
        assumptions=deduplicate_preserve_order(assumptions),
    )


def configure_standard_streams() -> None:
    for stream_name in ("stdin", "stdout"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (TypeError, ValueError):
                pass


def should_retry_request(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    if response is None:
        return True
    status_code = getattr(response, "status_code", None)
    return bool(status_code in {429, 500, 502, 503, 504})


def request_with_retries(
    url: str,
    params: Optional[Dict[str, object]] = None,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> "requests.Response":
    last_error: Optional[BaseException] = None
    effective_headers = {"User-Agent": USER_AGENT}
    if headers:
        effective_headers.update(headers)
    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
        try:
            response = requests.get(  # type: ignore[union-attr]
                url,
                params=params,
                headers=effective_headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response
        except RequestsRequestException as error:
            last_error = error
            if attempt >= HTTP_RETRY_ATTEMPTS or not should_retry_request(error):
                raise
            time.sleep(HTTP_RETRY_BACKOFF_SECONDS * attempt)
    raise PubMedSearchError(f"PubMed request failed without a retryable exception: {last_error}")


def explain_request_failure(error: BaseException) -> str:
    message = normalize_whitespace(str(error)) or error.__class__.__name__
    lowered = message.lower()
    if "winerror 10013" in lowered or "访问权限不允许" in message:
        hint = "Outbound HTTPS access to eutils.ncbi.nlm.nih.gov:443 appears blocked by firewall, proxy, VPN, or endpoint policy."
    elif "name resolution" in lowered or "getaddrinfo failed" in lowered:
        hint = "DNS resolution for eutils.ncbi.nlm.nih.gov failed in the current Python/OpenClaw environment."
    elif "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
        hint = "TLS or certificate validation failed while connecting to PubMed."
    elif "proxy" in lowered:
        hint = "The active proxy configuration blocked or rejected the PubMed request."
    else:
        hint = "Check outbound HTTPS access to the target HTTPS source from the same Python/OpenClaw environment."
    return f"{message} {hint}"


def quote_pubmed_term(term: str) -> str:
    return f'"{normalize_whitespace(term).replace(chr(34), "")}"[Title/Abstract]'


def build_pubmed_query(parameters: SearchParameters) -> str:
    parts: List[str] = []
    for group in parameters.keyword_groups:
        term_group = " OR ".join(quote_pubmed_term(term) for term in group if term)
        if term_group:
            parts.append(f"({term_group})")
    if parameters.exclude_terms:
        parts.append(f"NOT ({' OR '.join(quote_pubmed_term(term) for term in parameters.exclude_terms)})")
    if parameters.article_types and parameters.article_type_filter_mode == "strict":
        parts.append(f"({' OR '.join(ARTICLE_TYPE_RULES[t]['pubmed'] for t in parameters.article_types if t in ARTICLE_TYPE_RULES)})")
    if parameters.excluded_article_types:
        parts.append(f"NOT ({' OR '.join(ARTICLE_TYPE_RULES[t]['pubmed'] for t in parameters.excluded_article_types if t in ARTICLE_TYPE_RULES)})")
    if parameters.year_start and parameters.year_end:
        parts.append(f'("{parameters.year_start}/01/01"[Date - Publication] : "{parameters.year_end}/12/31"[Date - Publication])')
    if parameters.species:
        filters = [SPECIES_RULES[s]["pubmed"] for s in parameters.species if s in SPECIES_RULES]
        if filters:
            parts.append(f"({' OR '.join(filters)})")
    return " AND ".join(parts) if parts else quote_pubmed_term(parameters.topic)


def request_json(url: str, params: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    response = request_with_retries(url, params=params)
    return response.json()


def request_text(url: str, params: Optional[Dict[str, object]] = None) -> str:
    response = request_with_retries(url, params=params)
    return response.text


def search_pubmed_ids(query: str, max_results: int, sort_by: str) -> Tuple[int, List[str]]:
    payload = request_json(ESEARCH_URL, {"db": "pubmed", "retmode": "json", "retmax": max(10, min(max_results * 3, 200)), "sort": "pub date" if sort_by == "date" else "relevance", "term": query, "tool": "pubmed_paper_finder"})
    esearchresult = payload.get("esearchresult", {})
    count = int(esearchresult.get("count", "0"))
    id_list = esearchresult.get("idlist", [])
    if not isinstance(id_list, list):
        raise PubMedSearchError("Unexpected PubMed ESearch response format.")
    return count, [str(item) for item in id_list]


def batched(sequence: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(sequence), size):
        yield sequence[index:index + size]


def join_xml_text(element: Optional[ET.Element]) -> str:
    return "" if element is None else normalize_whitespace("".join(element.itertext()))


def parse_year_from_article(pubmed_article: ET.Element) -> Optional[int]:
    for path in [".//Article/ArticleDate/Year", ".//JournalIssue/PubDate/Year", ".//PubDate/Year"]:
        year_text = pubmed_article.findtext(path)
        if year_text and year_text.isdigit():
            return int(year_text)
    medline_date = pubmed_article.findtext(".//PubDate/MedlineDate")
    if medline_date:
        match = re.search(r"(19|20)\d{2}", medline_date)
        if match:
            return int(match.group(0))
    return None


def parse_pubmed_xml(xml_text: str) -> List[PaperResult]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise PubMedSearchError(f"Could not parse PubMed XML: {error}") from error
    papers: List[PaperResult] = []
    for article in root.findall(".//PubmedArticle"):
        abstract_parts: List[str] = []
        for abstract_node in article.findall(".//Article/Abstract/AbstractText"):
            label = abstract_node.attrib.get("Label")
            value = join_xml_text(abstract_node)
            if label and value:
                abstract_parts.append(f"{label}: {value}")
            elif value:
                abstract_parts.append(value)
        article_ids = {identifier.attrib.get("IdType", "").lower(): normalize_whitespace(identifier.text or "") for identifier in article.findall(".//PubmedData/ArticleIdList/ArticleId")}
        papers.append(PaperResult(
            result_index=0,
            pmid=normalize_whitespace(article.findtext(".//MedlineCitation/PMID", default="")),
            pmcid=normalize_pmcid(article_ids.get("pmc", "")),
            doi=article_ids.get("doi", ""),
            title=join_xml_text(article.find(".//Article/ArticleTitle")),
            authors=[normalize_whitespace(f"{author.findtext('ForeName', default='').strip()} {author.findtext('LastName', default='').strip()}".strip()) for author in article.findall(".//AuthorList/Author") if normalize_whitespace(f"{author.findtext('ForeName', default='').strip()} {author.findtext('LastName', default='').strip()}".strip())],
            journal=normalize_whitespace(article.findtext(".//Article/Journal/ISOAbbreviation") or article.findtext(".//Article/Journal/Title", default="")),
            year=parse_year_from_article(article),
            abstract="\n".join(abstract_parts),
            publication_types=[normalize_whitespace(node.text or "") for node in article.findall(".//PublicationTypeList/PublicationType") if normalize_whitespace(node.text or "")],
            score=0.0,
            score_breakdown={},
            explanation=[],
        ))
    return papers


def fetch_pubmed_details(pubmed_ids: Sequence[str]) -> List[PaperResult]:
    papers: List[PaperResult] = []
    for batch in batched(list(pubmed_ids), 100):
        xml_text = request_text(EFETCH_URL, {"db": "pubmed", "retmode": "xml", "id": ",".join(batch), "tool": "pubmed_paper_finder"})
        papers.extend(parse_pubmed_xml(xml_text))
    return papers


def score_paper(paper: PaperResult, parameters: SearchParameters) -> PaperResult:
    text_blob = f"{paper.title} {paper.abstract}".lower()
    title_blob = paper.title.lower()
    publication_types = [item.lower() for item in paper.publication_types]
    explanations: List[str] = []
    matched_groups = 0
    title_hits = 0
    for group in parameters.keyword_groups:
        group_terms = [term.lower() for term in group]
        if any(term in text_blob for term in group_terms):
            matched_groups += 1
            explanations.append(f"Matched topic group: {group[0]}")
        if any(term in title_blob for term in group_terms):
            title_hits += 1
    relevance_score = min(50.0, (matched_groups / max(1, len(parameters.keyword_groups))) * 35.0 + title_hits * 5.0)
    article_type_score = 0.0
    quality_evidence_score = 0.0
    if parameters.article_types:
        requested = [item.lower() for item in parameters.article_types]
        if any(requested_type in publication_type for requested_type in requested for publication_type in publication_types):
            article_type_score = 20.0
            explanations.append("Matched requested publication type.")
    elif any(quality_type in publication_type for quality_type in QUALITY_BOOST_ARTICLE_TYPES for publication_type in publication_types):
        quality_evidence_score = 12.0
        explanations.append("Matched a higher-evidence publication type.")
    species_score = 0.0
    for species in parameters.species:
        species_rule = SPECIES_RULES.get(species)
        if species_rule and any(term in text_blob for term in species_rule["score_terms"]):
            species_score = 15.0
            explanations.append(f"Matched requested species: {species}.")
            break
    recency_score = 0.0
    if paper.year:
        recency_score = max(0.0, 15.0 - min(float(max(0, datetime.now().year - paper.year)), 15.0))
        explanations.append(f"Publication year {paper.year} contributes recency score.")
    paper.score_breakdown = {
        "topic_relevance": round(relevance_score, 2),
        "article_type_match": round(article_type_score, 2),
        "quality_evidence": round(quality_evidence_score, 2),
        "recency": round(recency_score, 2),
        "species_match": round(species_score, 2),
    }
    paper.score = round(sum(paper.score_breakdown.values()), 2)
    paper.explanation = deduplicate_preserve_order(explanations)
    return paper


def rank_results(papers: Sequence[PaperResult], parameters: SearchParameters) -> List[PaperResult]:
    scored = [score_paper(paper, parameters) for paper in papers]
    if parameters.sort_by == "date":
        scored.sort(key=lambda item: (item.year or 0, item.score, int(item.pmid) if item.pmid.isdigit() else -1), reverse=True)
    else:
        scored.sort(key=lambda item: (item.score, item.year or 0, int(item.pmid) if item.pmid.isdigit() else -1), reverse=True)
    ranked = scored[: parameters.max_results]
    for index, paper in enumerate(ranked, start=1):
        paper.result_index = index
    return ranked


def run_search(parameters: SearchParameters) -> SearchOutcome:
    pubmed_query = build_pubmed_query(parameters)
    retrieved_count, pubmed_ids = search_pubmed_ids(pubmed_query, parameters.max_results, parameters.sort_by)
    if not pubmed_ids:
        return SearchOutcome(parameters=parameters, pubmed_query=pubmed_query, retrieved_count=0, selected_count=0, results=[])
    ranked_results = rank_results(fetch_pubmed_details(pubmed_ids), parameters)
    return SearchOutcome(parameters=parameters, pubmed_query=pubmed_query, retrieved_count=retrieved_count, selected_count=len(ranked_results), results=ranked_results)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search PubMed from a natural-language biomedical request.")
    query_group = parser.add_mutually_exclusive_group(required=False)
    query_group.add_argument("--query", help="Natural-language literature request.")
    query_group.add_argument("--query-stdin", action="store_true", help="Read the natural-language literature request from standard input.")
    parser.add_argument("--self-check", action="store_true", help="Validate local runtime dependencies and PubMed HTTPS reachability.")
    parser.add_argument("--download-request-stdin", action="store_true", help="Read a JSON download request from standard input and download selected PDFs via PMC.")
    parser.add_argument("--max-results", type=positive_int, help="Maximum number of ranked results to return.")
    parser.add_argument("--years", help='Year filter, for example "2021-2025" or "since 2021".')
    parser.add_argument("--article-types", help='Comma-separated publication types, for example "review,meta-analysis".')
    parser.add_argument("--species", help='Comma-separated species values, for example "mouse,human".')
    parser.add_argument("--sort-by", choices=["relevance", "date"], help="Sort results by relevance or publication date.")
    parser.add_argument("--download-root", help="Optional root folder for PDF downloads. Defaults to the current user's Desktop.")
    parser.add_argument("--unpaywall-email", help=argparse.SUPPRESS)  # Deprecated; no longer used.
    return parser


def ensure_runtime_dependencies() -> None:
    if REQUESTS_IMPORT_ERROR is not None:
        raise PubMedSearchError(
            "Missing Python dependency 'requests'. Install it with "
            "'python -m pip install -r <skill>\\scripts\\requirements.txt' "
            "or 'py -m pip install -r <skill>\\scripts\\requirements.txt'."
        )


def probe_pubmed_connectivity() -> Tuple[bool, str]:
    ensure_runtime_dependencies()
    try:
        response = requests.get(  # type: ignore[union-attr]
            ESEARCH_URL,
            params={
                "db": "pubmed",
                "retmode": "json",
                "retmax": 0,
                "term": "gut microbiota",
                "tool": "pubmed_paper_finder-self-check",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=SELF_CHECK_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except RequestsRequestException as error:
        return False, explain_request_failure(error)
    return True, "Reached PubMed ESearch over HTTPS."


def build_self_check_payload() -> Tuple[int, Dict[str, object]]:
    payload: Dict[str, object] = {
        "status": "ok",
        "runtime": {
            "python_executable": sys.executable,
            "python_version": sys.version.split()[0],
            "requests_version": getattr(requests, "__version__", None) if requests is not None else None,
            "stdin_encoding": getattr(sys.stdin, "encoding", None),
            "stdout_encoding": getattr(sys.stdout, "encoding", None),
            "proxy_env_present": {name: bool(os.environ.get(name)) for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")},
        },
    }
    try:
        ensure_runtime_dependencies()
    except PubMedSearchError as error:
        payload["status"] = "error"
        payload["dependency_check"] = {"ok": False, "detail": str(error)}
        payload["pubmed_connectivity"] = {"ok": False, "detail": "Skipped because dependencies are missing."}
        return 1, payload

    payload["dependency_check"] = {"ok": True, "detail": "Python runtime and requests dependency are available."}
    connectivity_ok, detail = probe_pubmed_connectivity()
    payload["pubmed_connectivity"] = {"ok": connectivity_ok, "detail": detail}
    if not connectivity_ok:
        payload["status"] = "error"
        return 1, payload
    return 0, payload


def build_download_context(query: str, parameters: SearchParameters) -> Dict[str, str]:
    topic = parameters.topic or query
    return {
        "topic": topic,
        "folder_name": sanitize_filename(topic, DEFAULT_DOWNLOAD_DIR_NAME),
    }


def build_result_payload(result: PaperResult) -> Dict[str, object]:
    preview = result.abstract[:400].strip()
    if len(result.abstract) > 400:
        preview += "..."
    return {
        "result_index": result.result_index,
        "title": result.title,
        "pmid": result.pmid,
        "pmcid": result.pmcid,
        "doi": result.doi,
        "journal": result.journal,
        "year": result.year,
        "publication_types": result.publication_types,
        "score": result.score,
        "score_breakdown": result.score_breakdown,
        "why_selected": " ".join(result.explanation) if result.explanation else "Matched the constructed PubMed query.",
        "abstract_preview": preview,
        "download_hints": {
            "pmc_available": bool(result.pmcid),
            "doi_available": bool(result.doi),
            "preferred_source": "pmc" if result.pmcid else "unavailable",
        },
    }


def build_stdout_payload(query: str, outcome: SearchOutcome) -> Dict[str, object]:
    return {
        "query": query,
        "parsed_search_parameters": outcome.parameters.to_dict(),
        "pubmed_query": outcome.pubmed_query,
        "retrieved_count": outcome.retrieved_count,
        "selected_count": outcome.selected_count,
        "download_context": build_download_context(query, outcome.parameters),
        "results": [build_result_payload(result) for result in outcome.results],
    }


def normalize_pmcid(value: str) -> str:
    normalized = normalize_whitespace(value)
    if not normalized:
        return ""
    if normalized.upper().startswith("PMC"):
        return f"PMC{normalized[3:]}"
    digits = re.sub(r"\D", "", normalized)
    return f"PMC{digits}" if digits else ""


def normalize_doi(value: str) -> str:
    normalized = normalize_whitespace(value)
    normalized = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", normalized, flags=re.IGNORECASE)
    return normalized.strip()


def sanitize_filename(value: str, fallback: str) -> str:
    cleaned = normalize_whitespace(re.sub(r'[<>:"/\\|?*\x00-\x1F]+', " ", value))
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        cleaned = fallback
    cleaned = cleaned[:MAX_FILENAME_LENGTH].rstrip(" .")
    if cleaned.upper() in WINDOWS_RESERVED_FILENAMES:
        cleaned = f"_{cleaned}"
    return cleaned or fallback


def ensure_unique_pdf_path(directory: pathlib.Path, base_name: str) -> pathlib.Path:
    candidate = directory / f"{base_name}.pdf"
    if not candidate.exists():
        return candidate
    for suffix in range(2, 1000):
        candidate = directory / f"{base_name} ({suffix}).pdf"
        if not candidate.exists():
            return candidate
    raise FullTextDownloadError(f"Could not create a unique filename for '{base_name}'.")


def default_download_root() -> pathlib.Path:
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        return pathlib.Path(user_profile) / "Desktop"
    return pathlib.Path.home() / "Desktop"


def parse_selected_indices(raw_value: object) -> List[int]:
    def parse_token(token: str) -> List[int]:
        token = normalize_whitespace(token)
        if not token:
            return []
        if re.fullmatch(r"\d+\s*-\s*\d+", token):
            start_str, end_str = re.split(r"\s*-\s*", token)
            start, end = int(start_str), int(end_str)
            if end < start:
                start, end = end, start
            return list(range(start, end + 1))
        return [int(token)]

    values: List[int] = []
    if isinstance(raw_value, int):
        values = [raw_value]
    elif isinstance(raw_value, str):
        for token in re.split(r"[,\s;]+", raw_value):
            values.extend(parse_token(token))
    elif isinstance(raw_value, list):
        for item in raw_value:
            if isinstance(item, int):
                values.append(item)
            elif isinstance(item, str):
                values.extend(parse_token(item))
            else:
                raise PubMedSearchError("Selected indices must be integers or strings.")
    else:
        raise PubMedSearchError("Selected indices must be provided as an integer, string, or list.")
    cleaned = deduplicate_preserve_order([str(item) for item in values if item > 0])
    if not cleaned:
        raise PubMedSearchError("No valid selected indices were provided for download.")
    return [int(item) for item in cleaned]


def coerce_download_articles(results_value: object) -> List[DownloadArticle]:
    if not isinstance(results_value, list):
        raise PubMedSearchError("Download request must include a JSON 'results' list.")
    articles: List[DownloadArticle] = []
    for position, item in enumerate(results_value, start=1):
        if not isinstance(item, dict):
            raise PubMedSearchError("Each download request result entry must be a JSON object.")
        raw_index = item.get("result_index", position)
        if isinstance(raw_index, str) and raw_index.isdigit():
            result_index = int(raw_index)
        elif isinstance(raw_index, int):
            result_index = raw_index
        else:
            raise PubMedSearchError("Each result must include a numeric result_index.")
        articles.append(DownloadArticle(
            result_index=result_index,
            title=normalize_whitespace(str(item.get("title", ""))),
            pmid=normalize_whitespace(str(item.get("pmid", ""))),
            pmcid=normalize_pmcid(str(item.get("pmcid", ""))),
            doi=normalize_doi(str(item.get("doi", ""))),
        ))
    return articles


def parse_download_request(raw_payload: object) -> Tuple[str, str, List[int], List[DownloadArticle]]:
    if not isinstance(raw_payload, dict):
        raise PubMedSearchError("Download request must be a JSON object.")
    search_payload = raw_payload.get("search_payload")
    if search_payload is not None and not isinstance(search_payload, dict):
        raise PubMedSearchError("search_payload must be a JSON object when provided.")
    payload_source = search_payload if isinstance(search_payload, dict) else raw_payload
    selected_indices_value = raw_payload.get("selected_indices", raw_payload.get("selection", raw_payload.get("download_selection")))
    if selected_indices_value is None:
        raise PubMedSearchError("Download request must include selected_indices, selection, or download_selection.")
    selected_indices = parse_selected_indices(selected_indices_value)
    results_value = raw_payload.get("results", payload_source.get("results"))
    articles = coerce_download_articles(results_value)

    topic = normalize_whitespace(str(raw_payload.get("topic", "")))
    if not topic:
        download_context = raw_payload.get("download_context", payload_source.get("download_context"))
        if isinstance(download_context, dict):
            topic = normalize_whitespace(str(download_context.get("topic", "")))
            folder_name = normalize_whitespace(str(download_context.get("folder_name", "")))
        else:
            folder_name = ""
    else:
        folder_name = normalize_whitespace(str(raw_payload.get("folder_name", "")))
    if not topic:
        parsed_parameters = payload_source.get("parsed_search_parameters")
        if isinstance(parsed_parameters, dict):
            topic = normalize_whitespace(str(parsed_parameters.get("topic", "")))
    if not topic:
        topic = normalize_whitespace(str(payload_source.get("query", "")))
    if not topic:
        topic = DEFAULT_DOWNLOAD_DIR_NAME
    if not folder_name:
        folder_name = sanitize_filename(topic, DEFAULT_DOWNLOAD_DIR_NAME)
    else:
        folder_name = sanitize_filename(folder_name, DEFAULT_DOWNLOAD_DIR_NAME)
    return topic, folder_name, selected_indices, articles


def response_is_pdf(response: "requests.Response") -> bool:
    content_type = (response.headers.get("Content-Type") or "").lower()
    return "pdf" in content_type or response.content[:4] == b"%PDF"


def response_is_html(response: "requests.Response") -> bool:
    content_type = (response.headers.get("Content-Type") or "").lower()
    return "html" in content_type or response.text.lstrip().startswith("<")


def extract_pdf_url_from_html(html_text: str, base_url: str) -> str:
    patterns = [
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
        r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return urljoin(base_url, unescape(match.group(1)))
    return ""


def is_pmc_download_challenge_page(html_text: str, url: str) -> bool:
    lowered_html = html_text.lower()
    lowered_url = url.lower()
    return (
        "pmc.ncbi.nlm.nih.gov" in lowered_url
        and "preparing to download" in lowered_html
        and ("pow_challenge" in lowered_html or "cloudpmc-viewer-pow" in lowered_html)
    )


def parse_pmc_pow_challenge(html_text: str) -> Tuple[str, int]:
    challenge_match = re.search(r'const\s+POW_CHALLENGE\s*=\s*"([^"]+)"', html_text)
    difficulty_match = re.search(r'const\s+POW_DIFFICULTY\s*=\s*"(\d+)"', html_text)
    if not challenge_match or not difficulty_match:
        raise FullTextDownloadError("PMC browser challenge page was detected, but the challenge parameters could not be parsed.")
    return challenge_match.group(1), int(difficulty_match.group(1))


def solve_pmc_pow(challenge: str, difficulty: int) -> int:
    prefix = "0" * max(1, difficulty)
    nonce = 0
    while True:
        digest = hashlib.sha256(f"{challenge}{nonce}".encode("utf-8")).hexdigest()
        if digest.startswith(prefix):
            return nonce
        nonce += 1


def fetch_pmc_pdf_with_pow(pdf_url: str, challenge_html: str, referer: Optional[str] = None) -> bytes:
    challenge, difficulty = parse_pmc_pow_challenge(challenge_html)
    nonce = solve_pmc_pow(challenge, difficulty)
    response = requests.get(  # type: ignore[union-attr]
        pdf_url,
        headers={"User-Agent": USER_AGENT, "Accept": PDF_ACCEPT_HEADER, "Referer": referer or pdf_url},
        cookies={"cloudpmc-viewer-pow": f"{challenge},{nonce}"},
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    if response_is_pdf(response):
        return response.content
    if response_is_html(response) and is_pmc_download_challenge_page(response.text, getattr(response, "url", pdf_url) or pdf_url):
        raise FullTextDownloadError("PMC browser challenge was solved, but the server still returned a challenge page instead of the PDF.")
    raise FullTextDownloadError("PMC browser challenge was solved, but the final response was not a PDF.")


def resolve_pdf_bytes(url: str, referer: Optional[str] = None, depth: int = 0) -> Tuple[bytes, str]:
    if depth > 2:
        raise FullTextDownloadError("Exceeded nested PDF resolution depth.")
    headers = {"Accept": PDF_ACCEPT_HEADER}
    if referer:
        headers["Referer"] = referer
    response = request_with_retries(url, headers=headers, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    final_url = getattr(response, "url", url) or url
    if response_is_pdf(response):
        return response.content, final_url
    if not response_is_html(response):
        raise FullTextDownloadError("The selected source did not return a downloadable PDF.")
    if is_pmc_download_challenge_page(response.text, final_url):
        pdf_bytes = fetch_pmc_pdf_with_pow(final_url, response.text, referer=referer or final_url)
        return pdf_bytes, final_url
    nested_pdf_url = extract_pdf_url_from_html(response.text, final_url)
    if not nested_pdf_url or nested_pdf_url == final_url:
        raise FullTextDownloadError("No downloadable PDF link was found on the selected source page.")
    return resolve_pdf_bytes(nested_pdf_url, referer=final_url, depth=depth + 1)


def write_pdf_file(destination: pathlib.Path, pdf_bytes: bytes) -> None:
    if not pdf_bytes.startswith(b"%PDF"):
        raise FullTextDownloadError("Resolved content did not start with a PDF header.")
    with destination.open("wb") as handle:
        handle.write(pdf_bytes)


def agent_browser_available() -> bool:
    return shutil.which("agent-browser") is not None or shutil.which("agent-browser.cmd") is not None


def run_agent_browser_command(args: Sequence[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("agent-browser") or shutil.which("agent-browser.cmd")
    if not executable:
        raise FullTextDownloadError("agent-browser is not available for PMC browser-assisted download.")
    command = subprocess.list2cmdline([executable, *args])
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
        shell=True,
    )


def fetch_pmc_pdf_via_browser_cookie(pdf_url: str) -> bytes:
    if not agent_browser_available():
        raise FullTextDownloadError("agent-browser is not available for PMC browser-assisted download.")

    session_name = f"pubmed-pmc-{uuid.uuid4().hex[:8]}"
    last_error = ""
    try:
        open_result = run_agent_browser_command(["--session", session_name, "open", pdf_url], timeout=180)
        if open_result.returncode != 0:
            last_error = normalize_whitespace(open_result.stderr or open_result.stdout)
            raise FullTextDownloadError(
                f"Browser-assisted PMC access failed before cookie acquisition: {last_error or 'unknown browser error.'}"
            )

        wait_result = run_agent_browser_command(["--session", session_name, "wait", "2000"], timeout=30)
        if wait_result.returncode != 0:
            last_error = normalize_whitespace(wait_result.stderr or wait_result.stdout)

        cookies_result = run_agent_browser_command(["--session", session_name, "cookies", "get", "--json"], timeout=60)
        if cookies_result.returncode != 0:
            last_error = normalize_whitespace(cookies_result.stderr or cookies_result.stdout)
            raise FullTextDownloadError(
                f"Browser-assisted PMC access could not read browser cookies: {last_error or 'unknown browser error.'}"
            )
        try:
            cookie_payload = json.loads(cookies_result.stdout)
        except json.JSONDecodeError as error:
            raise FullTextDownloadError("Browser-assisted PMC access returned invalid cookie JSON.") from error
        cookies_data = cookie_payload.get("data", {}).get("cookies", [])
        if not isinstance(cookies_data, list):
            raise FullTextDownloadError("Browser-assisted PMC access returned cookies in an unexpected format.")

        cookie_jar: Dict[str, str] = {}
        for item in cookies_data:
            if not isinstance(item, dict):
                continue
            name = normalize_whitespace(str(item.get("name", "")))
            value = str(item.get("value", ""))
            domain = normalize_whitespace(str(item.get("domain", ""))).lower()
            if name and value and ("pmc.ncbi.nlm.nih.gov" in domain or domain.endswith(".nih.gov") or domain.endswith(".ncbi.nlm.nih.gov")):
                cookie_jar[name] = value

        pow_cookie = cookie_jar.get("cloudpmc-viewer-pow")
        if not pow_cookie:
            raise FullTextDownloadError(
                "PMC article page exposes a PDF link, but browser-assisted access did not obtain the required download cookie."
            )

        response = requests.get(  # type: ignore[union-attr]
            pdf_url,
            headers={"User-Agent": USER_AGENT, "Accept": PDF_ACCEPT_HEADER, "Referer": pdf_url},
            cookies=cookie_jar,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        if response_is_pdf(response):
            return response.content
        if response_is_html(response) and is_pmc_download_challenge_page(response.text, getattr(response, "url", pdf_url) or pdf_url):
            raise FullTextDownloadError(
                "PMC article page exposes a PDF link, and browser-assisted access obtained cookies, but the download challenge still blocked automated retrieval."
            )
        raise FullTextDownloadError("Browser-assisted PMC access completed, but the final response was not a PDF.")
    except subprocess.TimeoutExpired as error:
        raise FullTextDownloadError("Browser-assisted PMC access timed out while preparing the PDF download.") from error
    finally:
        try:
            run_agent_browser_command(["--session", session_name, "close"], timeout=30)
        except Exception:
            pass


def lookup_pmcid(article: DownloadArticle) -> str:
    if article.pmcid:
        return normalize_pmcid(article.pmcid)
    identifiers = [article.pmid, article.doi]
    for identifier in identifiers:
        normalized_identifier = normalize_whitespace(identifier)
        if not normalized_identifier:
            continue
        payload = request_json(PMC_IDCONV_URL, {"ids": normalized_identifier, "format": "json", "tool": "pubmed_paper_finder"})
        records = payload.get("records", [])
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            pmcid = normalize_pmcid(str(record.get("pmcid", "")))
            if pmcid:
                return pmcid
    return ""


def attempt_pmc_download(article: DownloadArticle, destination: pathlib.Path) -> str:
    pmcid = lookup_pmcid(article)
    if not pmcid:
        raise FullTextDownloadError("No PMC full-text record is available for this article.")

    article_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    article_response = request_with_retries(article_url, headers={"Accept": PDF_ACCEPT_HEADER}, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    article_html = article_response.text if response_is_html(article_response) else ""
    article_pdf_url = extract_pdf_url_from_html(article_html, article_url) if article_html else ""

    candidates = deduplicate_preserve_order([
        article_pdf_url,
        f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
        f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/?pdf=render",
        article_url,
    ])
    last_error: Optional[str] = None
    saw_pdf_evidence = bool(article_pdf_url)
    saw_browser_challenge = False
    for candidate in candidates:
        if not candidate:
            continue
        try:
            pdf_bytes, _ = resolve_pdf_bytes(candidate, referer=article_url if candidate != article_url else None)
            write_pdf_file(destination, pdf_bytes)
            return "pmc"
        except FullTextDownloadError as error:
            message = normalize_whitespace(str(error))
            last_error = message
            if "PMC exposes a PDF link for this article" in message:
                saw_browser_challenge = True

    if saw_browser_challenge and article_pdf_url:
        try:
            pdf_bytes = fetch_pmc_pdf_via_browser_cookie(article_pdf_url)
            write_pdf_file(destination, pdf_bytes)
            return "pmc_browser"
        except FullTextDownloadError as error:
            last_error = normalize_whitespace(str(error))
        raise FullTextDownloadError(
            last_error or "PMC article page exposes a PDF link, but automated download was blocked by a browser challenge page."
        )
    if saw_pdf_evidence:
        raise FullTextDownloadError(
            last_error or "PMC article page exposed a PDF link, but the PDF could not be downloaded programmatically."
        )
    raise FullTextDownloadError(last_error or "PMC has a full-text record for this article, but no downloadable PDF link was found on the article page.")


def summarize_download_failure(
    article: Optional[DownloadArticle],
    selected_index: int,
    doi_for_output: str,
    failure_reasons: Sequence[str],
) -> DownloadFailure:
    stable_reasons = list(failure_reasons)
    has_pmc_browser_challenge = any("PMC article page exposes a PDF link, but automated download was blocked by a browser challenge page." in reason for reason in stable_reasons)
    has_no_pmc_pdf = any(
        "No PMC full-text record is available for this article." in reason
        or "PMC has a full-text record for this article, but no downloadable PDF link was found on the article page." in reason
        for reason in stable_reasons
    )
    if has_pmc_browser_challenge:
        summary = "PMC exposes a PDF link, but automated download was blocked by a browser challenge page."
        reason_code = "pmc_browser_challenge"
    elif has_no_pmc_pdf and stable_reasons:
        summary = "PMC did not provide a downloadable PDF for this article."
        reason_code = "no_pmc_pdf"
    elif stable_reasons:
        summary = "PMC download failed."
        reason_code = "pmc_download_failed"
    else:
        summary = "No PMC download source was available."
        reason_code = "download_unavailable"
    return DownloadFailure(
        result_index=selected_index,
        title=article.title if article and article.title else f"Result #{selected_index}",
        doi=doi_for_output,
        reason_code=reason_code,
        reason=summary,
        reason_detail=" ; ".join(stable_reasons),
    )


def download_selected_articles(
    topic: str,
    folder_name: str,
    selected_indices: Sequence[int],
    articles: Sequence[DownloadArticle],
    download_root_override: Optional[str],
    unpaywall_email: str = "",  # Deprecated parameter kept for backward compatibility; no longer used.
) -> DownloadOutcome:
    root = pathlib.Path(download_root_override).expanduser() if download_root_override else default_download_root()
    output_dir = root / folder_name
    output_dir.mkdir(parents=True, exist_ok=True)
    article_map = {article.result_index: article for article in articles}
    downloaded: List[DownloadSuccess] = []
    failed: List[DownloadFailure] = []

    for selected_index in selected_indices:
        article = article_map.get(selected_index)
        if article is None:
            failed.append(DownloadFailure(
                result_index=selected_index,
                title=f"Result #{selected_index}",
                doi="",
                reason_code="missing_result",
                reason="No search result with that number was present in the supplied results payload.",
                reason_detail="",
            ))
            continue

        title_for_name = article.title or article.doi or article.pmid or f"result-{selected_index}"
        destination = ensure_unique_pdf_path(output_dir, sanitize_filename(title_for_name, f"result-{selected_index}"))
        failure_reasons: List[str] = []

        try:
            source = attempt_pmc_download(article, destination)
            downloaded.append(DownloadSuccess(
                result_index=selected_index,
                title=article.title,
                file_path=str(destination.resolve()),
                source=source,
            ))
            continue
        except (FullTextDownloadError, RequestsRequestException, PubMedSearchError) as error:
            failure_reasons.append(f"PMC: {normalize_whitespace(str(error))}")

        failed.append(summarize_download_failure(
            article=article,
            selected_index=selected_index,
            doi_for_output=normalize_doi(article.doi),
            failure_reasons=failure_reasons,
        ))

    return DownloadOutcome(
        topic=topic,
        folder_name=folder_name,
        output_dir=str(output_dir.resolve()),
        selected_indices=list(selected_indices),
        downloaded=downloaded,
        failed=failed,
    )


def build_download_payload(outcome: DownloadOutcome) -> Dict[str, object]:
    return {
        "topic": outcome.topic,
        "folder_name": outcome.folder_name,
        "output_dir": outcome.output_dir,
        "selected_indices": outcome.selected_indices,
        "downloaded_count": len(outcome.downloaded),
        "failed_count": len(outcome.failed),
        "downloaded": [asdict(item) for item in outcome.downloaded],
        "failed": [asdict(item) for item in outcome.failed],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_standard_streams()
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.self_check and (args.query or args.query_stdin or args.download_request_stdin):
        parser.error("--self-check cannot be combined with --query, --query-stdin, or --download-request-stdin")
    if args.download_request_stdin and (args.query or args.query_stdin):
        parser.error("--download-request-stdin cannot be combined with --query or --query-stdin")
    if not args.self_check and not args.query and not args.query_stdin and not args.download_request_stdin:
        parser.error("one of --query, --query-stdin, --download-request-stdin, or --self-check is required")

    try:
        if args.self_check:
            exit_code, payload = build_self_check_payload()
            json.dump(payload, sys.stdout, indent=2, ensure_ascii=True)
            sys.stdout.write("\n")
            return exit_code

        ensure_runtime_dependencies()

        if args.download_request_stdin:
            raw_download_request = sys.stdin.read()
            if not raw_download_request.strip():
                raise PubMedSearchError("The download request JSON is empty.")
            try:
                payload = json.loads(raw_download_request)
            except json.JSONDecodeError as error:
                raise PubMedSearchError(f"Could not parse download request JSON: {error.msg}.") from error
            topic, folder_name, selected_indices, articles = parse_download_request(payload)
            outcome = download_selected_articles(
                topic=topic,
                folder_name=folder_name,
                selected_indices=selected_indices,
                articles=articles,
                download_root_override=args.download_root,
            )
            json.dump(build_download_payload(outcome), sys.stdout, indent=2, ensure_ascii=True)
            sys.stdout.write("\n")
            return 0

        query = normalize_whitespace(sys.stdin.read()) if args.query_stdin else normalize_whitespace(args.query or "")
        if not query:
            raise PubMedSearchError("The literature request is empty.")
        parameters = parse_request(query, args)
        outcome = run_search(parameters)
    except RequestsRequestException as error:
        print(f"PubMed request failed: {explain_request_failure(error)}", file=sys.stderr)
        return 1
    except PubMedSearchError as error:
        print(f"Search failed: {error}", file=sys.stderr)
        return 1

    json.dump(build_stdout_payload(query, outcome), sys.stdout, indent=2, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
