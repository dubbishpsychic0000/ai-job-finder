"""Vacancy-independent company discovery from open geographic/entity sources."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

import requests

from app import memory as mem
from app.config import ROOT_DIR
from app.discovery.website_researcher import host_domain, normalize_url
from app.models import Company

logger = logging.getLogger(__name__)
MAX_COMPANY_UNIVERSE_RESPONSE_BYTES = 5 * 1024 * 1024
COMPANY_UNIVERSE_CACHE_TTL = timedelta(days=30)
COMPANY_UNIVERSE_CACHE_PATH = ROOT_DIR / "data" / "company_universe_cache.json"
COMPANY_UNIVERSE_CACHE_MAX_BYTES = 10 * 1024 * 1024
COMPANY_SOURCE_PRIORITY = {
    "company_careers_direct": 0,
    "company_career": 0,
    "company_careers": 0,
    "employer_discovery": 1,
    "osm": 2,
    "wikidata": 2,
    "curated_seed_csv": 3,
}


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
    text = re.sub(r"[_-]+", " ", f"{name} {industry}".lower())
    hits = sum(
        1 for term in target_terms
        if re.sub(r"[_-]+", " ", term.lower()) in text
    )
    return min(100.0, hits * 25.0)


class OpenCompanyDiscovery:
    """Small bounded adapters; results are candidates, not hiring claims."""

    def __init__(self, *, fetch_json=None):
        self.fetch_json = fetch_json or self._fetch_json
        self.cache_path = COMPANY_UNIVERSE_CACHE_PATH if fetch_json is None else None

    def osm(self, *, overpass_url: str, query: str, country: str = "",
            target_terms: list[str] | None = None) -> list[CompanyCandidate]:
        payload = self._request_json(
            "osm", overpass_url, {"data": query},
        )
        out = []
        for element in (payload or {}).get("elements", []):
            tags = element.get("tags") or {}
            name = (tags.get("name") or "").strip()
            if not name:
                continue
            website = tags.get("website") or tags.get("contact:website") or ""
            industry = next((
                str(tags.get(key, "")).strip()
                for key in ("industry", "craft", "office", "construction")
                if str(tags.get(key, "")).strip()
            ), "")
            location = next((
                str(tags.get(key, "")).strip()
                for key in (
                    "addr:city", "addr:town", "addr:village", "addr:municipality",
                    "addr:province", "addr:state", "is_in:city", "is_in",
                )
                if str(tags.get(key, "")).strip()
            ), "")[:120]
            out.append(CompanyCandidate(
                name=name, industry=industry[:128], country=country, website=website,
                location=location, source="osm",
                source_url=f"{overpass_url}?{urlencode({'data': query})}",
                reason="open geographic business/entity seed",
                relevance_score=candidate_relevance(name, industry, target_terms or []),
            ))
        return out

    def wikidata(self, *, endpoint: str, sparql: str, country: str = "",
                 target_terms: list[str] | None = None) -> list[CompanyCandidate]:
        payload = self._request_json(
            "wikidata", endpoint, {"query": sparql, "format": "json"},
        )
        out = []
        for row in (payload or {}).get("results", {}).get("bindings", []):
            name = row.get("name", {}).get("value", "").strip()
            if not name:
                continue
            website = row.get("website", {}).get("value", "")
            industry = str(row.get("industryLabel", {}).get("value", ""))[:128]
            source_url = row.get("entity", {}).get("value", endpoint)
            out.append(CompanyCandidate(
                name=name, industry=industry, country=country, website=website,
                location=row.get("cityLabel", {}).get("value", "").strip(),
                source="wikidata", source_url=source_url,
                reason="open entity/industry seed",
                relevance_score=candidate_relevance(name, industry, target_terms or []),
            ))
        return out

    def _request_json(self, kind: str, url: str, params: dict[str, str]):
        cache_key = hashlib.sha256(json.dumps(
            [kind, url, params], sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        cache = self._read_cache()
        entry = cache.get(cache_key)
        if entry:
            try:
                fetched_at = datetime.fromisoformat(entry["fetched_at"])
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - fetched_at <= COMPANY_UNIVERSE_CACHE_TTL:
                    return entry["payload"]
            except (KeyError, TypeError, ValueError):
                pass

        payload = self.fetch_json(url, params=params)
        if self.cache_path is not None:
            cache[cache_key] = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }
            try:
                self._write_cache(cache)
            except OSError as exc:
                logger.warning("Company-universe response cache could not be saved: %s", exc)
        return payload

    def _read_cache(self) -> dict:
        if self.cache_path is None or not self.cache_path.exists():
            return {}
        try:
            if self.cache_path.stat().st_size > COMPANY_UNIVERSE_CACHE_MAX_BYTES:
                return {}
            cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return cache if isinstance(cache, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_cache(self, cache: dict) -> None:
        if self.cache_path is None:
            return
        serialized = json.dumps(cache, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > COMPANY_UNIVERSE_CACHE_MAX_BYTES:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(self.cache_path)

    def persist(self, session, candidates: list[CompanyCandidate],
                *, minimum_relevance: float = 0) -> tuple[int, int]:
        stored = duplicates = 0
        persisted_by_key: dict[str, Company] = {}
        for candidate in candidates:
            if candidate.relevance_score < minimum_relevance:
                continue
            website = _trusted_website(candidate.website)
            domain = host_domain(website) if website else ""
            key = canonical_company_key(candidate.name, domain)
            if key in persisted_by_key:
                duplicates += 1
                company = persisted_by_key[key]
            else:
                company = _find_existing_company(session, candidate.name, domain)
                if company is None:
                    company = Company(
                        name=candidate.name[:255],
                        normalized_name=mem.store._normalize_name(candidate.name),
                        website=website,
                        official_domain=domain,
                        country=candidate.country[:64],
                        industry=candidate.industry[:128],
                        source=candidate.source[:128],
                    )
                    session.add(company)
                    session.flush()
                persisted_by_key[key] = company
            existing_domain = _normalized_domain(company.official_domain)
            domain_conflict = bool(existing_domain and domain and existing_domain != domain)
            if not domain_conflict:
                if domain and not company.official_domain:
                    company.official_domain = domain
                if website and (
                    not company.website or not existing_domain
                    or _normalized_domain(company.website) == domain
                ):
                    company.website = website
                if candidate.country and not company.country:
                    company.country = candidate.country
                if candidate.industry and not company.industry:
                    company.industry = candidate.industry[:128]
            if not company.source or _source_priority(candidate.source) < _source_priority(company.source):
                company.source = candidate.source[:128]
            company.relevance_score = max(company.relevance_score, candidate.relevance_score)
            reason_tag = f"[{candidate.source}] {candidate.reason}".strip()
            if reason_tag and reason_tag not in (company.discovery_reason or ""):
                company.discovery_reason = "; ".join(filter(
                    None, (company.discovery_reason, reason_tag)
                ))
            discovery_key = (
                f"company-universe:{company.id}:{candidate.source}:"
                f"{candidate.source_url or key}"
            )
            _, created = mem.store.record_discovery(
                session, kind="COMPANY", url=website,
                title=candidate.name, company_name=candidate.name,
                company_id=company.id, source=candidate.source,
                source_type=candidate.source,
                discovery_channel="independent_company_seed",
                evidence={
                    "source_url": candidate.source_url,
                    "official_domain": domain,
                    "website": website,
                    "industry": candidate.industry,
                    "country": candidate.country,
                    "location": candidate.location,
                    "reason": candidate.reason,
                },
                reason=candidate.reason,
                relevance_score=candidate.relevance_score,
                canonical_key=discovery_key,
            )
            if created:
                stored += 1
            else:
                duplicates += 1
        return stored, duplicates

    @staticmethod
    def _fetch_json(url: str, *, params: dict[str, str]):
        response = requests.get(url, params=params, timeout=(5, 30),
                                headers={"User-Agent": "WorldwideCareerAgent/0.1"})
        response.raise_for_status()
        if len(response.content) > MAX_COMPANY_UNIVERSE_RESPONSE_BYTES:
            raise ValueError("company-universe source response exceeds the 5 MiB limit")
        return response.json()


def _domain(url: str) -> str:
    return host_domain(_trusted_website(url)) if _trusted_website(url) else ""


def _trusted_website(url: str) -> str:
    normalized = normalize_url(str(url or ""))
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    try:
        if parsed.port not in (None, 443):
            return ""
    except ValueError:
        return ""
    domain = host_domain(normalized)
    try:
        ipaddress.ip_address(domain)
        return ""
    except ValueError:
        pass
    if (
        not domain
        or domain.endswith((".localhost", ".local", ".internal"))
        or any(domain == blocked or domain.endswith(f".{blocked}") for blocked in (
            "linkedin.com", "indeed.com", "glassdoor.com", "monster.com",
            "ziprecruiter.com", "naukri.com", "bayt.com", "rekrute.com", "emploi.ma",
            "anapec.org", "welcome-to-the-jungle.com", "greenhouse.io", "lever.co",
            "myworkdayjobs.com", "smartrecruiters.com", "ashbyhq.com", "personio.com",
            "workable.com", "facebook.com", "instagram.com", "tiktok.com", "youtube.com",
            "google.com", "wikipedia.org", "annuaire.com", "telecontact.ma", "kerix.net",
            "kompass.com", "marocannuaire.org", "goafricaonline.com",
        ))
    ):
        return ""
    return normalized


def _normalized_domain(value: str) -> str:
    url = value if "://" in (value or "") else f"https://{value}"
    return host_domain(url)


def _find_existing_company(session, name: str, domain: str) -> Company | None:
    if domain:
        company = session.query(Company).filter(
            (Company.official_domain.ilike(domain))
            | (Company.official_domain.ilike(f"www.{domain}"))
        ).order_by(Company.id.asc()).first()
        if company:
            return company
        normalized_name = mem.store._normalize_name(name)
        return session.query(Company).filter(
            Company.normalized_name == normalized_name,
            (Company.official_domain == "") | (Company.official_domain.is_(None)),
        ).order_by(Company.id.asc()).first()
    normalized_name = mem.store._normalize_name(name)
    return session.query(Company).filter(
        Company.normalized_name == normalized_name
    ).order_by(Company.id.asc()).first()


def _source_priority(source: str) -> int:
    return COMPANY_SOURCE_PRIORITY.get(source, 10)
