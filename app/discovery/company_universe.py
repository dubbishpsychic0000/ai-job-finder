"""Vacancy-independent company discovery from open geographic/entity sources."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from app import memory as mem


@dataclass(frozen=True)
class CompanyCandidate:
    name: str
    industry: str = ""
    country: str = ""
    location: str = ""
    website: str = ""
    source: str = ""
    source_url: str = ""
    reason: str = ""
    relevance_score: float = 0


def canonical_company_key(name: str, domain: str = "") -> str:
    value = domain or name
    value = value.lower().strip()
    value = re.sub(r"^https?://", "", value).split("/", 1)[0]
    value = re.sub(r"\b(ltd|limited|sarl|sa|llc|inc)\b", "", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def candidate_relevance(name: str, industry: str, target_terms: list[str]) -> float:
    text = f"{name} {industry}".lower()
    hits = sum(1 for term in target_terms if term.lower() in text)
    return min(100.0, hits * 25.0)


class OpenCompanyDiscovery:
    """Small bounded adapters; results are candidates, not hiring claims."""

    def __init__(self, *, fetch_json=None):
        self.fetch_json = fetch_json or self._fetch_json

    def osm(self, *, overpass_url: str, query: str, country: str = "",
            target_terms: list[str] | None = None) -> list[CompanyCandidate]:
        payload = self.fetch_json(overpass_url, params={"data": query})
        out = []
        for element in (payload or {}).get("elements", []):
            tags = element.get("tags") or {}
            name = (tags.get("name") or "").strip()
            if not name:
                continue
            website = tags.get("website") or tags.get("contact:website") or ""
            industry = tags.get("industry") or tags.get("craft") or tags.get("office") or ""
            out.append(CompanyCandidate(
                name=name, industry=industry, country=country, website=website,
                source="osm", source_url=overpass_url,
                reason="open geographic business/entity seed",
                relevance_score=candidate_relevance(name, industry, target_terms or []),
            ))
        return out

    def wikidata(self, *, endpoint: str, sparql: str, country: str = "",
                 target_terms: list[str] | None = None) -> list[CompanyCandidate]:
        payload = self.fetch_json(endpoint, params={"query": sparql, "format": "json"})
        out = []
        for row in (payload or {}).get("results", {}).get("bindings", []):
            name = row.get("name", {}).get("value", "").strip()
            if not name:
                continue
            website = row.get("website", {}).get("value", "")
            industry = row.get("industryLabel", {}).get("value", "")
            source_url = row.get("entity", {}).get("value", endpoint)
            out.append(CompanyCandidate(
                name=name, industry=industry, country=country, website=website,
                source="wikidata", source_url=source_url,
                reason="open entity/industry seed",
                relevance_score=candidate_relevance(name, industry, target_terms or []),
            ))
        return out

    def persist(self, session, candidates: list[CompanyCandidate],
                *, minimum_relevance: float = 0) -> tuple[int, int]:
        stored = duplicates = 0
        for candidate in candidates:
            if candidate.relevance_score < minimum_relevance:
                continue
            domain = _domain(candidate.website)
            key = canonical_company_key(candidate.name, domain)
            company = mem.store.get_or_create_company(
                session, candidate.name, candidate.website, candidate.country,
                industry=candidate.industry, source=candidate.source,
                official_domain=domain,
            )
            if company.discovery_reason and key in company.discovery_reason:
                duplicates += 1
            else:
                company.discovery_reason = (
                    f"{company.discovery_reason}; {key}:{candidate.reason}"
                ).strip("; ")
                company.relevance_score = max(company.relevance_score, candidate.relevance_score)
                mem.store.record_discovery(
                    session, kind="COMPANY", url=candidate.website,
                    title=candidate.name, company_name=candidate.name,
                    company_id=company.id, source=candidate.source,
                    source_type=candidate.source,
                    discovery_channel="independent_company_seed",
                    evidence={"source_url": candidate.source_url,
                              "industry": candidate.industry,
                              "location": candidate.location},
                    reason=candidate.reason,
                    relevance_score=candidate.relevance_score,
                )
                stored += 1
        return stored, duplicates

    @staticmethod
    def _fetch_json(url: str, *, params: dict[str, str]):
        response = requests.get(url, params=params, timeout=20,
                                headers={"User-Agent": "WorldwideCareerAgent/0.1"})
        response.raise_for_status()
        return response.json()


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host
