"""Employer & recruitment-agency discovery (spec §7, §9).

Builds a candidate-relevant "*company universe*": employers likely to hire the
candidate's profile even when they have few or no jobs indexed by aggregators,
plus recruitment agencies that place foreign technical workers.

Flow (spec §7):

    candidate profile → industry search → relevant employers
        → company verification → careers-page discovery → monitoring

This module implements industry search, employer/agency classification and
careers-page verification. Every network dependency (`search_fn`, `fetch_html`)
is injectable so tests stay hermetic and offline.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.config import CandidateProfile, Preferences
from app.discovery.vocabulary import CandidateVocabulary

logger = logging.getLogger(__name__)
_USER_AGENT = "WorldwideCareerAgent/0.1 (respects robots.txt and site terms)"

AGENCY_KEYWORDS = ("recruit", "staffing", "interim", "agenc", "talent",
                   "placement", "headhunt", "mobility international")

JOB_BOARD_HOSTS = ("indeed.com", "linkedin.com/jobs", "glassdoor.com", "monster.com",
                   "stepstone", "jooble", "careerbuilder.com", "hotjobs")

INDUSTRY_KEYWORDS = {
    "civil_engineering": ("civil engineering", "genie civil", "génie civil",
                          "civil works", "infrastructure", "ouvrages",
                          "road", "voirie", "routes", "highway", "vrd", "tecn"),
    "construction": ("construction", "bâtiment", "batiment", "build"),
}

SPONSOR_HIGH = ("visa sponsorship", "work permit sponsorship", "sponsor visas", "titre de séjour")
SPONSOR_MED = ("sponsorship", "visa", "work permit", "permis de travail")
INTERNATIONAL_HIGH = ("international candidates", "international applicants",
                      "welcome international", "recrute à l'international",
                      "recruit international", "internationals welcome")
INTERNATIONAL_MED = ("international", "worldwide", "monde entier")

_CAREER_TITLE_NOISE = re.compile(r"careers?|jobs?|recruit[a-z]*|hiring|work (at|for)|join|about|–|-|\||:")


@dataclass
class EmployerCandidate:
    name: str
    careers_url: str
    source_url: str
    country: str = ""
    kind: str = "company"                     # company | recruitment_agency
    industry: str = ""                        # civil_engineering | construction
    sponsorship_signal: str = "unknown"
    international_recruitment_signal: str = "unknown"
    recruitment_url: str = ""


@dataclass
class VerificationVerdict:
    verified: bool
    ats: str = ""
    page_title: str = ""


@dataclass
class EmployerReport:
    discovered: int = 0
    verified: int = 0
    unverified: int = 0
    stored: int = 0
    source_errors: list[str] = field(default_factory=list)


def candidate_employer_queries(vocab: CandidateVocabulary, countries: list[str],
                               role_limit: int = 3) -> list[tuple[str, str]]:
    """(query, location) pairs for employer + agency discovery."""
    terms = vocab.roles()[:role_limit]
    if not terms:
        terms = ["civil engineering"]
    pairs: list[tuple[str, str]] = []
    for country in countries:
        for term in terms:
            pairs.append((f"{term} careers", country))
            pairs.append((f"{term} recruiting", country))
            pairs.append((f"{term} recruitment agency", country))
    return pairs


def _signal(text: str, high: tuple[str, ...], medium: tuple[str, ...]) -> str:
    low = text.lower()
    if any(k in low for k in high):
        return "high"
    if any(k in low for k in medium):
        return "medium"
    return "unknown"


def _industry(text: str) -> str:
    low = text.lower()
    for industry, kws in INDUSTRY_KEYWORDS.items():
        if any(k in low for k in kws):
            return industry
    return ""


def _domain_company_name(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    for prefix in ("www.", "careers.", "career.", "jobs.", "recruiting.", "hr."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    labels = host.split(".")
    if len(labels) < 2:
        return ""
    name, rest = labels[0], labels[1:]
    if name in ("wd", "wd5", "wd3", "myworkdaysite", "boards", "api", "search"):
        name = rest[0] if rest else name
    name = re.sub(r"[^a-z0-9]+", " ", name).strip()
    return name.title()


def _title_company_hint(title: str) -> str:
    cleaned = _CAREER_TITLE_NOISE.sub(" ", (title or "").lower())
    words = [w for w in cleaned.split() if w]
    if not words:
        return ""
    joined = " ".join(words[:3])
    return joined[:40].title()


def classify_result(url: str, title: str = "", snippet: str = "",
                    country: str = "") -> EmployerCandidate | None:
    """Classify one search result into an employer/agency candidate (or None)."""
    if not url:
        return None
    host = (urlparse(url).netloc or "").lower()
    if not host or "." not in host:
        return None
    if any(b in host for b in JOB_BOARD_HOSTS):
        return None
    text = " ".join(filter(None, (title, snippet))).lower()
    kind = "recruitment_agency" if any(k in text for k in AGENCY_KEYWORDS) else "company"
    name = _domain_company_name(url) or _title_company_hint(title)
    if not name and kind == "company":
        name = _title_company_hint(title)
    return EmployerCandidate(
        name=name or "Unknown",
        careers_url=url,
        source_url=url,
        country=country or "",
        kind=kind,
        industry=_industry(text),
        sponsorship_signal=_signal(text, SPONSOR_HIGH, SPONSOR_MED),
        international_recruitment_signal=_signal(text, INTERNATIONAL_HIGH, INTERNATIONAL_MED),
        recruitment_url=url if kind == "recruitment_agency" else "",
    )


class EmployerDiscoveryEngine:
    """Runs industry search, classifies results and verifies careers pages."""

    def __init__(self, profile: CandidateProfile | None = None,
                 prefs: Preferences | None = None,
                 search_fn=None, fetch_html=None):
        self.profile = profile
        self.prefs = prefs
        self.search_fn = search_fn or _default_search
        self.fetch_html = fetch_html or _http_fetch

    async def discover_prefs(self, max_per_country: int = 5) -> list[EmployerCandidate]:
        vocab = CandidateVocabulary(profile=self.profile, prefs=self.prefs)
        queries = candidate_employer_queries(vocab, (self.prefs and self.prefs.countries) or [])
        return await self.discover(queries, max_per_country=max_per_country)

    async def discover(self, queries: list[tuple[str, str]],
                       max_per_country: int = 5) -> list[EmployerCandidate]:
        found: dict[str, EmployerCandidate] = {}
        for query, location in queries:
            try:
                raw = await self.search_fn(query, location) or []
            except Exception as exc:
                logger.warning("employer search %r failed: %s", query, exc)
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                cand = classify_result(str(item.get("url", "")),
                                       str(item.get("title", "")),
                                       str(item.get("snippet", "")),
                                       country=location)
                if cand is None:
                    continue
                key = (cand.careers_url or cand.name).lower()
                if key in found:
                    continue
                found[key] = cand
        # cap per country so one country can't monopolize the universe
        out: list[EmployerCandidate] = []
        per_country: dict[str, int] = {}
        for cand in found.values():
            c = cand.country or ""
            if per_country.get(c, 0) >= max_per_country:
                continue
            per_country[c] = per_country.get(c, 0) + 1
            out.append(cand)
        return out

    async def verify(self, url: str) -> VerificationVerdict:
        """Fetch the careers page and confirm it is live + detect ATS."""
        try:
            html = self.fetch_html(url)
        except Exception as exc:
            logger.warning("employer verify %s failed: %s", url, exc)
            html = None
        if not html:
            return VerificationVerdict(verified=False)
        from app.connectors.ats_detect import detect_ats

        return VerificationVerdict(verified=True, ats=detect_ats(url, html),
                                   page_title=_page_title(html))


def _page_title(html: str) -> str:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        t = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
        return t[:80]
    except Exception:
        return ""


async def _default_search(query: str, location: str = "") -> list[dict]:
    """Live search through the search-engine connector (unwraps redirects)."""
    from app.connectors.search_engine import SearchEngineSource, resolve_search_url

    ops = await SearchEngineSource(results_per_query=8).search(query, location)
    return [{"url": resolve_search_url(o.url), "title": o.title, "snippet": o.description}
            for o in ops if o.url]


def _http_fetch(url: str) -> str:
    import requests

    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=20)
    if resp.status_code in (401, 403, 429):
        return ""
    resp.raise_for_status()
    return resp.text
