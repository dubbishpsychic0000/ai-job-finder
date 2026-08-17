"""Search-engine expansion — opportunity source discovery (spec §6).

The search engine is *not* a job database. This module repurposes it as a
discovery layer that finds **opportunity sources** on top of raw jobs:

    employer career pages, recruitment agencies, government programmes,
    immigration/work-permit pages, workforce-shortage pages, sponsorship
    pages, international-hiring announcements, social signals.

Each surfaced URL is classified deterministically and becomes an
`OpportunitySource` the agent can later monitor or expand into connectors.
The classifier never turns absence of evidence into a positive claim.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import CandidateProfile, Preferences
from app.discovery.vocabulary import CandidateVocabulary

logger = logging.getLogger(__name__)

GOV_HOST_SUFFIXES = (
    ".gouv.fr", ".gov.", ".gov.uk", ".gov.au", ".govt.nz", ".gov.nl", ".gov.pt",
    ".gov.es", ".gov.ie", ".gov.in", ".canada.ca", ".gc.ca", ".europa.eu",
    ".eures.europa.eu", ".usa.gov", ".usajobs.gov", ".jobapi.gv.at", ".admin.ch",
)

GOV_KEYWORDS = ("eures", "emploi", "travail", "arbeitsagentur", "work and pension",
                "employment service", "public employment", "job centre", "pole emploi")

AGENCY_KEYWORDS = ("recruit", "staffing", "interim", "agenc", "placement", "headhunt")

IMMIGRATION_KEYWORDS = ("immigration", "visa", "work permit", "permis de travail",
                        "skilled worker", "foreign worker", "travailleur étranger",
                        "arbeitserlaubnis")

SHORTAGE_KEYWORDS = ("shortage", "métiers en tension", "metiers en tension", "fachkräfte",
                     "fachkraefte", "engpass", "labour market", "labor market", "in demand")

SPONSOR_KEYWORDS = ("sponsorship", "sponsor visas", "visa sponsorship")

INTERNATIONAL_KEYWORDS = ("international applicants", "international candidates",
                          "recrute à l'international", "recruiting internationally",
                          "worldwide", "welcome international")

# (query_suffix, kind) pairs applied to every candidate occupation term.
SOURCE_QUERY_KINDS = (
    ("careers", "employer_career"),
    ("recruitment agency", "recruitment_agency"),
    ("international hiring", "international"),
    ("visa sponsorship", "sponsorship"),
    ("shortage occupation", "shortage"),
    ("work permit", "immigration"),
)


@dataclass
class SourceCandidate:
    url: str
    title: str
    kind: str = "general"
    country: str = ""
    sponsorship_signal: str = "unknown"
    international_recruitment_signal: str = "unknown"
    notes: str = ""


def classify_opportunity_source(url: str, title: str = "", snippet: str = "") -> SourceCandidate:
    """Deterministic classification of a search result into a source type."""
    if not url:
        return SourceCandidate(url="", title=title)
    host = urlparse(url).netloc.lower()
    text = " ".join(filter(None, (title, snippet))).lower()

    kind: str
    notes: list[str] = []
    if any(h.endswith(s) for h in [host] for s in GOV_HOST_SUFFIXES) or any(
            k in text for k in GOV_KEYWORDS):
        kind = "government"
        if any(k in text for k in SHORTAGE_KEYWORDS):
            kind = "shortage"
        elif any(k in text for k in IMMIGRATION_KEYWORDS):
            kind = "immigration"
        notes.append("official source")
    elif any(k in text for k in AGENCY_KEYWORDS):
        kind = "recruitment_agency"
    elif any(k in text for k in IMMIGRATION_KEYWORDS):
        kind = "immigration"
    elif any(k in text for k in SHORTAGE_KEYWORDS):
        kind = "shortage"
    elif any(k in text for k in SPONSOR_KEYWORDS):
        kind = "sponsorship"
    elif any(k in text for k in INTERNATIONAL_KEYWORDS):
        kind = "international"
    elif any(k in text for k in ("career", "careers", "jobs", "hiring", "recrutement")):
        kind = "employer_career"
    elif host in ("linkedin.com", "www.linkedin.com") or "linkedin.com" in host or "facebook.com" in host:
        kind = "social_signal"
    else:
        kind = "general"

    return SourceCandidate(
        url=url,
        title=(title or "")[:500],
        kind=kind,
        sponsorship_signal=_signal(text, SPONSOR_KEYWORDS),
        international_recruitment_signal=_signal(text, INTERNATIONAL_KEYWORDS),
        notes=", ".join(notes) if notes else "",
    )


def _signal(text: str, markers: tuple[str, ...]) -> str:
    return "high" if any(k in text for k in markers) else "unknown"


def candidate_source_queries(vocab: CandidateVocabulary, countries: list[str],
                             role_limit: int = 3) -> list[tuple[str, str]]:
    """(query, location) pairs: occupation term × source-category suffix."""
    pairs: list[tuple[str, str]] = []
    role_terms = vocab.roles()[:role_limit]
    for country in countries:
        for term in role_terms[:1]:
            for suffix, _kind in SOURCE_QUERY_KINDS:
                pairs.append((f"{term} {suffix}", country))
        for loc_term in vocab.country_terms(country)[:1]:
            pairs.append((f"{loc_term} recrutement recruteurs", country))
    return pairs


class OpportunitySourceDiscoveryEngine:
    """Surfaces opportunity sources from search-engine results.

    `search_fn(query, location)` is injectable (defaults to the live
    search-engine connector) so tests stay hermetic.
    """

    def __init__(self, profile: CandidateProfile | None = None,
                 prefs: Preferences | None = None, search_fn=None,
                 max_per_country: int = 5):
        self.profile = profile
        self.prefs = prefs
        self.search_fn = search_fn or _default_search
        self.max_per_country = max_per_country

    async def discover_prefs(self) -> list[SourceCandidate]:
        vocab = CandidateVocabulary(profile=self.profile, prefs=self.prefs)
        queries = candidate_source_queries(vocab, (self.prefs and self.prefs.countries) or [])
        return await self.discover(queries, max_per_country=self.max_per_country)

    async def discover(self, queries: list[tuple[str, str]],
                       max_per_country: int = 5) -> list[SourceCandidate]:
        found: dict[str, SourceCandidate] = {}
        for query, location in queries:
            try:
                raw = await self.search_fn(query, location) or []
            except Exception as exc:
                logger.warning("opportunity-source search %r failed: %s", query, exc)
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                cand = classify_opportunity_source(
                    str(item.get("url", "")),
                    str(item.get("title", "")),
                    str(item.get("snippet", "")))
                if not cand.url:
                    continue
                cand.country = location or ""
                key = cand.url.lower()
                if key in found:
                    continue
                found[key] = cand
        out, per_country = [], {}
        for cand in found.values():
            c = cand.country or ""
            if per_country.get(c, 0) >= max_per_country:
                continue
            per_country[c] = per_country.get(c, 0) + 1
            out.append(cand)
        return out


async def _default_search(query: str, location: str = "") -> list[dict]:
    from app.connectors.search_engine import SearchEngineSource, resolve_search_url

    ops = await SearchEngineSource(results_per_query=8).search(query, location)
    return [{"url": resolve_search_url(o.url), "title": o.title, "snippet": o.description}
            for o in ops if o.url]
