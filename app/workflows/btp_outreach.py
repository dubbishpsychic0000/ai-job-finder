"""Bounded, draft-only spontaneous applications to public BTP company sites."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app import memory as mem
from app.config import ROOT_DIR, AgentConfig, CandidateProfile, RunnerSettings
from app.connectors.search_engine import resolve_search_url
from app.connectors.tavily_resilience import (
    BTP_TAVILY_DAILY_RESERVE,
    TAVILY_DAILY_REQUEST_LIMIT,
    DailyBudget,
    ResilientTavily,
    TavilyCache,
    TavilyKey,
    key_fingerprint,
)
from app.discovery.email_verification import EmailVerificationService
from app.discovery.employers import classify_result
from app.discovery.website_researcher import (
    EvidenceFact,
    WebsiteResearcher,
    extract_spontaneous_instruction,
    host_domain,
    normalize_url,
)
from app.email.service import ApplicationEngine
from app.models import Job

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MAX_OVERPASS_BYTES = 5 * 1024 * 1024
MAX_CANDIDATES = 1000
DEFAULT_MAX_COMPANIES = 20
HARD_MAX_COMPANIES = 50
MAX_WEBSITE_SEARCH_RESULTS = 8
MAX_TAVILY_API_CALLS_PER_RUN = BTP_TAVILY_DAILY_RESERVE
MAX_DUCKDUCKGO_API_CALLS_PER_RUN = 3
DUCKDUCKGO_MIN_DELAY_SECONDS = 1.0
MAX_CITY_SEARCHES_PER_RUN = 2
CITY_SEARCH_STATE_PATH = ROOT_DIR / "data" / "btp_search_state.json"
OVERPASS_CACHE_PATH = ROOT_DIR / "data" / "btp_overpass_cache.json"
OVERPASS_CACHE_TTL = timedelta(days=7)
BTPOUTREACH_STATE_PATH = ROOT_DIR / "data" / "btp_outreach_state.json"
BTPOUTREACH_STATE_MAX_BYTES = 5 * 1024 * 1024
WIKIDATA_CACHE_PATH = ROOT_DIR / "data" / "btp_wikidata_cache.json"
WIKIDATA_CACHE_TTL = timedelta(days=30)
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
MAX_SEED_FILE_BYTES = 1024 * 1024
MAX_SEED_COMPANIES = 2000
SEED_FILE_PATH = ROOT_DIR / "candidate" / "btp_company_seeds.csv"
CASABLANCA_CENTER = (33.5731, -7.5898)
SPONTANEOUS_APPLICATION = "SPONTANEOUS_APPLICATION"
MOROCCO_CITY_CENTERS = {
    "Casablanca": ("Casablanca-Settat", 33.5731, -7.5898),
    "Mohammedia": ("Casablanca-Settat", 33.6866, -7.3830),
    "Settat": ("Casablanca-Settat", 33.0010, -7.6166),
    "El Jadida": ("Casablanca-Settat", 33.2316, -8.5007),
    "Rabat": ("Rabat-Salé-Kénitra", 34.0209, -6.8416),
    "Kenitra": ("Rabat-Salé-Kénitra", 34.2610, -6.5802),
    "Beni Mellal": ("Béni Mellal-Khénifra", 32.3373, -6.3498),
    "Meknes": ("Fès-Meknès", 33.8935, -5.5473),
    "Fes": ("Fès-Meknès", 34.0331, -5.0003),
    "Safi": ("Marrakech-Safi", 32.2994, -9.2372),
    "Marrakech": ("Marrakech-Safi", 31.6295, -7.9811),
    "Tangier": ("Tanger-Tétouan-Al Hoceïma", 35.7595, -5.8340),
    "Tetouan": ("Tanger-Tétouan-Al Hoceïma", 35.5889, -5.3626),
    "Oujda": ("Oriental", 34.6814, -1.9086),
    "Agadir": ("Souss-Massa", 30.4278, -9.5981),
    "Laayoune": ("Laâyoune-Sakia El Hamra", 27.1536, -13.2033),
    "Dakhla": ("Dakhla-Oued Ed-Dahab", 23.6848, -15.9582),
}
TERMINAL_TARGET_STATUSES = {
    "no_verified_website",
    "no_explicit_instructions",
    "no_official_employer_email",
    "portal_or_form",
    "drafted_for_review",
}


class PublicSearchBudgetError(RuntimeError):
    """Raised when the BTP workflow reaches its bounded public-search budget."""


class _BoundedPublicSearchRouter:
    """Use Tavily when available, then bounded DuckDuckGo fallback."""

    def __init__(
        self,
        primary=None,
        fallback=None,
        *,
        max_fallback_calls: int = MAX_DUCKDUCKGO_API_CALLS_PER_RUN,
    ):
        self.primary = primary
        self.fallback = fallback or PublicCompanyWebsiteFinder._search
        self.max_fallback_calls = max(0, max_fallback_calls)
        self.fallback_calls = 0
        self.primary_unavailable = primary is None
        self._last_fallback_call = 0.0

    def __call__(self, query: str) -> list[dict[str, str]]:
        primary_error = None
        if not self.primary_unavailable:
            try:
                return self.primary(query)
            except Exception as exc:
                primary_error = exc
                self.primary_unavailable = True
        if self.fallback_calls >= self.max_fallback_calls:
            raise PublicSearchBudgetError(
                "bounded public-search providers are unavailable or their run budgets are exhausted"
            ) from primary_error
        delay = DUCKDUCKGO_MIN_DELAY_SECONDS - (time.monotonic() - self._last_fallback_call)
        if delay > 0:
            time.sleep(delay)
        self.fallback_calls += 1
        self._last_fallback_call = time.monotonic()
        try:
            return self.fallback(query)
        except Exception as exc:
            raise PublicSearchBudgetError(
                f"bounded public-search providers are unavailable: {exc}"
            ) from (primary_error or exc)


class BtpOutreachState:
    """Vault-persisted screening ledger; protects progress when Actions resets SQLite."""

    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, dict[str, str]] = {}
        if not path.exists():
            return
        try:
            if path.stat().st_size > BTPOUTREACH_STATE_MAX_BYTES:
                raise BtpOutreachSafetyError(
                    "BTP screening state exceeds the 5 MiB safety limit"
                )
            raw = json.loads(path.read_text(encoding="utf-8"))
            records = raw.get("companies", {})
            if raw.get("schema_version") != 1 or not isinstance(records, dict):
                raise ValueError("unsupported BTP screening-state format")
            for key, record in records.items():
                if not isinstance(key, str) or not isinstance(record, dict):
                    raise ValueError("invalid BTP screening-state record")
                datetime.fromisoformat(record["updated_at"])
            self.records = records
        except BtpOutreachSafetyError:
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise BtpOutreachSafetyError(
                "BTP screening state is unreadable; refusing to risk duplicate drafts"
            ) from exc

    @staticmethod
    def key(candidate: BtpCompany) -> str:
        name = _normalize_city_name(candidate.name)
        city = _canonical_city(candidate.city) or _nearest_city(candidate.latitude, candidate.longitude)
        identity = f"{name}\x00{city}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def is_suppressed(self, candidate: BtpCompany, *, now: datetime | None = None) -> bool:
        record = self.records.get(self.key(candidate))
        if not record:
            return False
        status = record.get("status", "")
        if status == "drafted_for_review":
            return True
        ttl = {
            "no_verified_website": timedelta(days=30),
            "no_explicit_instructions": timedelta(days=30),
            "no_official_employer_email": timedelta(days=30),
            "portal_or_form": timedelta(days=30),
            "in_progress": timedelta(days=1),
            "blocked_by_safety_gate": timedelta(days=1),
            "research_error": timedelta(hours=6),
        }.get(status)
        if ttl is None:
            return False
        updated = datetime.fromisoformat(record["updated_at"])
        current = now or datetime.now(timezone.utc)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current - updated < ttl

    def is_expired_negative(self, candidate: BtpCompany, *, now: datetime | None = None) -> bool:
        record = self.records.get(self.key(candidate))
        if not record or record.get("status") not in TERMINAL_TARGET_STATUSES - {"drafted_for_review"}:
            return False
        if self.is_suppressed(candidate, now=now):
            return False
        return record.get("status") not in {"drafted_for_review"}

    def mark(self, candidate: BtpCompany, status: str) -> None:
        self.records[self.key(candidate)] = {
            "name": candidate.name[:255],
            "city": _canonical_city(candidate.city)
                    or _nearest_city(candidate.latitude, candidate.longitude),
            "source": candidate.source[:64],
            "status": status[:64],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def clear(self, candidate: BtpCompany) -> None:
        self.records.pop(self.key(candidate), None)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        serialized = json.dumps({
            "schema_version": 1,
            "companies": self.records,
        }, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > BTPOUTREACH_STATE_MAX_BYTES:
            raise BtpOutreachSafetyError("BTP screening state exceeds the 5 MiB safety limit")
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(self.path)


OVERPASS_QUERY = (
    '[out:json][timeout:25][maxsize:5242880];'
    'area["ISO3166-1"="MA"][admin_level=2]->.morocco;('
    'nwr(area.morocco)["craft"~"construction|builder|civil_engineering|structural_engineering",i];'
    'nwr(area.morocco)["office"="construction"];'
    'nwr(area.morocco)["office"="company"]["industry"~"construction|building|civil engineering|public works|infrastructure",i];'
    ');out center;'
)


class BtpOutreachSafetyError(ValueError):
    """Raised when this workflow's mandatory draft-only gates are not met."""


@dataclass(frozen=True)
class BtpCompany:
    name: str
    website: str
    city: str
    region: str
    latitude: float
    longitude: float
    distance_km: float
    source_url: str = OVERPASS_URL
    source: str = ""

    @property
    def location(self) -> str:
        return ", ".join(value for value in (self.city, self.region, "Morocco") if value)


@dataclass
class BtpOutreachReport:
    origin_city: str = "Casablanca"
    candidates: int = 0
    osm_candidates: int = 0
    wikidata_candidates: int = 0
    seed_candidates: int = 0
    web_candidates: int = 0
    known_website_candidates: int = 0
    website_search_required: int = 0
    search_budget_deferred: int = 0
    processed: int = 0
    researched: int = 0
    drafts: int = 0
    existing: int = 0
    no_website: int = 0
    no_spontaneous_instructions: int = 0
    no_qualifying_email: int = 0
    portal_or_form: int = 0
    search_budget_limited: bool = False
    blocked: int = 0
    daily_limit_reached: bool = False
    errors: list[str] = field(default_factory=list)
    companies: list[dict] = field(default_factory=list)
    limitation: str = (
        "OpenStreetMap and indexed public search results are incomplete; this is not a "
        "canonical or exhaustive list of Moroccan BTP employers."
    )

    def as_run_report(self) -> dict:
        return {
            "workflow": "btp_outreach",
            "origin_city": self.origin_city,
            "limitation": self.limitation,
            "discovery": {
                "new_jobs": 0,
                "fetched": self.candidates,
                "errors": self.errors,
            },
            "analysis": {"analyzed": 0, "decisions": {}, "errors": []},
            "action": {
                "applied": 0,
                "sent": 0,
                "asked": 0,
                "investigated": 0,
                "blocked": self.blocked,
                "errors": [],
                "drafts": self.drafts,
                "btp": {
                    "candidates": self.candidates,
                    "known_website_candidates": self.known_website_candidates,
                    "website_search_required": self.website_search_required,
                    "search_budget_deferred": self.search_budget_deferred,
                    "osm_candidates": self.osm_candidates,
                    "wikidata_candidates": self.wikidata_candidates,
                    "seed_candidates": self.seed_candidates,
                    "web_candidates": self.web_candidates,
                    "processed": self.processed,
                    "researched": self.researched,
                    "drafts": self.drafts,
                    "sent": 0,
                    "gmail_drafts_confirmed": sum(
                        company.get("gmail_draft_confirmed", False)
                        for company in self.companies
                    ),
                    "existing": self.existing,
                    "no_website": self.no_website,
                    "no_official_website": self.no_website,
                    "no_spontaneous_instructions": self.no_spontaneous_instructions,
                    "no_qualifying_email": self.no_qualifying_email,
                    "portal_or_form": self.portal_or_form,
                    "blocked": self.blocked,
                    "search_budget_limited": self.search_budget_limited,
                    "daily_limit_reached": self.daily_limit_reached,
                },
            },
            "followup": {"sent": 0, "blocked": 0, "errors": []},
            "companies": self.companies,
        }


class OverpassBtpDiscovery:
    """Fetch only a size-limited, capped public Overpass result."""

    def __init__(self, fetch_json=None, *, cache_path=None):
        self.fetch_json = fetch_json or self._fetch_json
        self.cache_path = cache_path or (OVERPASS_CACHE_PATH if fetch_json is None else None)
        self.errors: list[str] = []

    def discover(self) -> list[BtpCompany]:
        try:
            cached = self._load_cache()
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.errors.append(f"Overpass cache is unreadable and will be refreshed: {exc}")
            cached = None
        if cached and cached[0] >= datetime.now(timezone.utc) - OVERPASS_CACHE_TTL:
            return cached[1]
        try:
            payload = self.fetch_json(OVERPASS_URL, params={"data": OVERPASS_QUERY})
            remark = str(payload.get("remark", "")).strip()
            if remark:
                raise RuntimeError(f"Overpass returned an incomplete result: {remark[:300]}")
            companies = parse_overpass_companies(payload, source_url=OVERPASS_URL)
            if self.cache_path:
                try:
                    self._save_cache(companies)
                except OSError as exc:
                    self.errors.append(f"Overpass results could not be cached: {exc}")
            return companies
        except Exception as exc:
            if cached:
                self.errors.append(
                    f"Overpass refresh failed; using cached public listings: {exc}"
                )
                return cached[1]
            raise

    def _load_cache(self) -> tuple[datetime, list[BtpCompany]] | None:
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("companies"), list):
                raise ValueError("invalid Overpass cache format")
            expected_hash = hashlib.sha256(OVERPASS_QUERY.encode("utf-8")).hexdigest()
            if raw.get("query_hash") != expected_hash:
                return None
            companies = [BtpCompany(**item) for item in raw.get("companies", [])]
            return datetime.fromisoformat(raw["fetched_at"]), companies
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.errors.append(f"Overpass cache is unreadable and will be refreshed: {exc}")
            return None

    def _save_cache(self, companies: list[BtpCompany]) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "query_hash": hashlib.sha256(OVERPASS_QUERY.encode("utf-8")).hexdigest(),
            "companies": [company.__dict__ for company in companies],
        }, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.cache_path)

    @staticmethod
    def _fetch_json(url: str, *, params: dict[str, str]):
        chunks = []
        size = 0
        with requests.get(
            url,
            params=params,
            headers={"User-Agent": "WorldwideCareerAgent/0.1 (bounded public BTP directory lookup)"},
            timeout=(10, 35),
            stream=True,
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_OVERPASS_BYTES:
                    raise ValueError("Overpass response exceeded the 5 MiB safety limit")
                chunks.append(chunk)
        return json.loads(b"".join(chunks))


class WikidataBtpDiscovery:
    """Monthly cached, quota-free supplemental seeds from Wikidata."""

    QUERY = """
    PREFIX wd: <http://www.wikidata.org/entity/>
    PREFIX wdt: <http://www.wikidata.org/prop/direct/>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX bd: <http://www.bigdata.com/rdf#>
    PREFIX wikibase: <http://wikiba.se/ontology#>
    SELECT ?entity ?entityLabel ?website ?cityLabel ?coordinates ?industryLabel WHERE {
      ?entity wdt:P17 wd:Q1028;
              wdt:P452 ?industry.
      ?industry rdfs:label ?industryLabel.
      FILTER(LANG(?industryLabel) = "en" &&
        REGEX(LCASE(STR(?industryLabel)), "construction|building|civil engineering|public works"))
      OPTIONAL { ?entity wdt:P856 ?website. }
      OPTIONAL {
        ?entity wdt:P131 ?city.
        ?city rdfs:label ?cityLabel.
        FILTER(LANG(?cityLabel) = "en" || LANG(?cityLabel) = "fr")
      }
      OPTIONAL {
        { ?entity wdt:P159 ?place. } UNION { ?entity wdt:P131 ?place. }
        ?place wdt:P625 ?coordinates.
      }
      SERVICE wikibase:label { bd:serviceParam wikibase:language "fr,en". }
    }
    LIMIT 100
    """

    def __init__(self, fetch_json=None, *, cache_path=None):
        self.fetch_json = fetch_json or self._fetch_json
        self.cache_path = cache_path or WIKIDATA_CACHE_PATH
        self.errors: list[str] = []

    def discover(self) -> list[BtpCompany]:
        try:
            cached = self._load_cache()
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.errors.append(f"Wikidata cache is unreadable and will be refreshed: {exc}")
            cached = None
        if cached and cached[0] >= datetime.now(timezone.utc) - WIKIDATA_CACHE_TTL:
            return cached[1]
        try:
            payload = self.fetch_json(WIKIDATA_SPARQL_URL, params={
                "query": self.QUERY,
                "format": "json",
            })
            companies = self._parse(payload)
            try:
                self._save_cache(companies)
            except OSError as exc:
                self.errors.append(f"Wikidata results could not be cached: {exc}")
            return companies
        except Exception as exc:
            self.errors.append(f"Wikidata BTP discovery failed: {exc}")
            return cached[1] if cached else []

    def _load_cache(self) -> tuple[datetime, list[BtpCompany]] | None:
        if not self.cache_path.exists():
            return None
        raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("companies"), list):
            raise ValueError("invalid Wikidata cache format")
        query_hash = hashlib.sha256(self.QUERY.encode("utf-8")).hexdigest()
        if raw.get("query_hash") != query_hash:
            return None
        timestamp = datetime.fromisoformat(raw["fetched_at"])
        companies = [
            BtpCompany(**item) for item in raw.get("companies", [])
        ]
        return timestamp, companies

    def _save_cache(self, companies: list[BtpCompany]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "query_hash": hashlib.sha256(self.QUERY.encode("utf-8")).hexdigest(),
            "companies": [company.__dict__ for company in companies],
        }, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.cache_path)

    @staticmethod
    def _parse(payload: dict) -> list[BtpCompany]:
        companies: dict[str, BtpCompany] = {}
        rows = (payload.get("results") or {}).get("bindings", [])
        for row in rows:
            name = _binding(row, "entityLabel")
            industry = _binding(row, "industryLabel")
            if not name or not _mentions_construction(industry):
                continue
            if any(term in industry.casefold() for term in ("vehicle", "shipbuilding", "naval")):
                continue
            source_url = _binding(row, "entity")
            point = _wikidata_point(_binding(row, "coordinates"))
            city = _canonical_city(_binding(row, "cityLabel"))
            if point is None and city:
                _, latitude, longitude = MOROCCO_CITY_CENTERS[city]
                point = (latitude, longitude)
            if point is None:
                continue
            latitude, longitude = point
            if not (20 <= latitude <= 37 and -18 <= longitude <= -0.5):
                continue
            if not city:
                city = min(
                    MOROCCO_CITY_CENTERS,
                    key=lambda value: haversine_km(
                        *MOROCCO_CITY_CENTERS[value][1:],
                        origin=(latitude, longitude),
                    ),
                )
            region, _, _ = _city_location(city)
            website = _eligible_website(_binding(row, "website"))
            if website and not _company_name_matches(name, website, ""):
                website = ""
            companies[name.casefold()] = BtpCompany(
                name=name,
                website=website,
                city=city,
                region=region,
                latitude=latitude,
                longitude=longitude,
                distance_km=haversine_km(latitude, longitude),
                source_url=source_url,
                source="wikidata",
            )
        return sorted(companies.values(), key=lambda item: (item.distance_km, item.name.casefold()))

    @staticmethod
    def _fetch_json(url: str, *, params: dict[str, str]):
        response = requests.get(
            url,
            params=params,
            headers={
                "User-Agent": "ai-job-finder/0.1 (monthly public BTP seed refresh)",
                "Accept": "application/sparql-results+json",
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        return response.json()


def load_btp_seed_csv(path: Path | None = None) -> list[BtpCompany]:
    seed_path = path or SEED_FILE_PATH
    if not seed_path.exists():
        return []
    if seed_path.stat().st_size > MAX_SEED_FILE_BYTES:
        raise ValueError("BTP company seed CSV exceeds the 1 MiB limit")
    content = seed_path.read_text(encoding="utf-8-sig")
    rows = csv.DictReader(io.StringIO(content))
    allowed = {"name", "city", "region", "latitude", "longitude", "website", "source_url"}
    required = {"name", "source_url"}
    headers = set(rows.fieldnames or ())
    if not required.issubset(headers) or not headers.issubset(allowed):
        raise ValueError(
            "BTP seed CSV headers must include name, source_url and only supported location/website fields"
        )
    companies = []
    for line_number, row in enumerate(rows, start=2):
        if line_number > MAX_SEED_COMPANIES + 1:
            raise ValueError(f"BTP company seed CSV exceeds {MAX_SEED_COMPANIES} rows")
        name = (row.get("name") or "").strip()[:255]
        source_url = _eligible_website((row.get("source_url") or "").strip())
        if not name or not source_url:
            raise ValueError(f"BTP seed CSV row {line_number} requires a name and HTTPS source_url")
        city_value = (row.get("city") or "").strip()
        city = _canonical_city(city_value)
        has_latitude = bool((row.get("latitude") or "").strip())
        has_longitude = bool((row.get("longitude") or "").strip())
        if has_latitude != has_longitude:
            raise ValueError(f"BTP seed CSV row {line_number} must provide both coordinates")
        if has_latitude:
            try:
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
            except ValueError as exc:
                raise ValueError(f"BTP seed CSV row {line_number} has invalid coordinates") from exc
            if not (20 <= latitude <= 37 and -18 <= longitude <= -0.5):
                raise ValueError(f"BTP seed CSV row {line_number} coordinates are outside Morocco")
            if not city:
                city = min(
                    MOROCCO_CITY_CENTERS,
                    key=lambda value: haversine_km(
                        *MOROCCO_CITY_CENTERS[value][1:],
                        origin=(latitude, longitude),
                    ),
                )
        elif city:
            _, latitude, longitude = MOROCCO_CITY_CENTERS[city]
        else:
            raise ValueError(f"BTP seed CSV row {line_number} needs a known city or coordinates")
        default_region, _, _ = _city_location(city)
        region = (row.get("region") or "").strip()[:120] or default_region
        companies.append(BtpCompany(
            name=name,
            website=_eligible_website((row.get("website") or "").strip()),
            city=city,
            region=region,
            latitude=latitude,
            longitude=longitude,
            distance_km=haversine_km(latitude, longitude),
            source_url=source_url,
            source="curated_seed_csv",
        ))
    return companies


class PublicCompanyWebsiteFinder:
    """Find likely official sites for directory-listed companies, never contacts."""

    def __init__(self, search=None):
        self.search = search or _configured_public_search()

    def find(self, company_name: str, location: str = "") -> str:
        query = f'"{company_name}" BTP entreprise Maroc site officiel {location}'.strip()
        for item in self.search(query)[:MAX_WEBSITE_SEARCH_RESULTS]:
            url = _eligible_website(str(item.get("url", "")))
            if not url or _is_directory_or_job_site(url):
                continue
            if _company_name_matches(company_name, url, str(item.get("title", ""))):
                return url
        return ""

    @staticmethod
    def _search(query: str) -> list[dict[str, str]]:
        response = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "ma-fr", "ia": "web"},
            headers={"User-Agent": "WorldwideCareerAgent/0.1 (public company website lookup)"},
            timeout=20,
        )
        lowered = response.text.casefold()
        if "unfortunately, bots use duckduckgo too" in lowered:
            raise RuntimeError("DuckDuckGo returned a human-verification challenge")
        if response.status_code != 200:
            raise RuntimeError(
                f"DuckDuckGo returned HTTP {response.status_code}; public search is unavailable"
            )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        results = []
        for result in soup.select(".result")[:MAX_WEBSITE_SEARCH_RESULTS]:
            anchor = result.select_one(".result__a")
            if not anchor:
                continue
            snippet = result.select_one(".result__snippet")
            results.append({
                "url": resolve_search_url(anchor.get("href", "")),
                "title": anchor.get_text(" ", strip=True),
                "snippet": snippet.get_text(" ", strip=True) if snippet else "",
            })
        return results


class _TavilyCompanySearch:
    def __init__(self, connector, max_api_calls: int = MAX_TAVILY_API_CALLS_PER_RUN):
        self.connector = connector
        self.max_api_calls = max_api_calls
        self.api_calls = 0

    def __call__(self, query: str) -> list[dict[str, str]]:
        cached = self.connector.has_cached_result(query)
        if not cached and self.api_calls >= self.max_api_calls:
            raise PublicSearchBudgetError("BTP Tavily per-run search cap reached")
        if not cached and not any(key.budget.remaining() > 0 for key in self.connector.keys):
            raise PublicSearchBudgetError("Tavily daily key budget exhausted")
        results = asyncio.run(self.connector.search(query))
        if not cached:
            self.api_calls += 1
        if self.connector.last_status == "degraded":
            raise RuntimeError("Tavily company search failed or its daily budget is exhausted")
        return [
            {"url": item.url, "title": item.title, "snippet": item.description}
            for item in results
        ]


def _configured_public_search():
    from dotenv import load_dotenv

    load_dotenv()
    configured_keys = os.getenv("TAVILY_API_KEYS") or os.getenv("TAVILY_API_KEY", "")
    api_keys = [key.strip() for key in configured_keys.split(",") if key.strip()]
    if not api_keys:
        return _BoundedPublicSearchRouter()

    data_dir = ROOT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    keys = [
        TavilyKey(
            api_key=key,
            budget=DailyBudget(
                data_dir / f"tavily_budget_{key_fingerprint(key)}.json",
                limit=TAVILY_DAILY_REQUEST_LIMIT,
            ),
            fp=key_fingerprint(key),
        )
        for key in api_keys
    ]
    connector = ResilientTavily(
        keys=keys,
        cache=TavilyCache(data_dir / "tavily_cache.json"),
        results_per_query=MAX_WEBSITE_SEARCH_RESULTS,
        source_name="btp_company_search",
        result_source_type="search_engine",
        query_suffix="",
        max_key_attempts=1,
    )
    return _BoundedPublicSearchRouter(
        primary=_TavilyCompanySearch(
            connector,
            max_api_calls=BTP_TAVILY_DAILY_RESERVE,
        ),
    )


class PublicBtpCompanySearch:
    """Bounded city-by-city web search that augments incomplete OSM listings."""

    def __init__(self, search=None, *, state_path=None,
                 max_city_searches: int = MAX_CITY_SEARCHES_PER_RUN):
        self.search = search or _configured_public_search()
        self.state_path = state_path or (CITY_SEARCH_STATE_PATH if search is None else None)
        self.max_city_searches = max(1, int(max_city_searches))
        self.errors: list[str] = []
        self.search_budget_limited = False

    def discover(self) -> list[BtpCompany]:
        all_cities = sorted(
            MOROCCO_CITY_CENTERS.items(),
            key=lambda item: haversine_km(item[1][1], item[1][2]),
        )
        cursor = _load_city_search_cursor(self.state_path, len(all_cities))
        cities = all_cities[cursor:] + all_cities[:cursor]
        found: dict[str, BtpCompany] = {}
        searched_cities = 0
        for offset, (city, (region, latitude, longitude)) in enumerate(cities):
            if searched_cities >= self.max_city_searches:
                break
            query = f'entreprise BTP travaux publics génie civil Maroc {city}'
            try:
                results = self.search(query)[:MAX_WEBSITE_SEARCH_RESULTS]
            except Exception as exc:
                self.errors.append(f"public company search failed for {city}: {exc}")
                self.search_budget_limited = isinstance(exc, PublicSearchBudgetError)
                break
            searched_cities += 1
            _save_city_search_cursor(
                self.state_path,
                (cursor + offset + 1) % len(all_cities),
            )
            for item in results:
                url = _eligible_website(str(item.get("url", "")))
                if not url:
                    continue
                text = f"{item.get('title', '')} {item.get('snippet', '')} {host_domain(url)}"
                if not _mentions_construction(text):
                    continue
                if _is_directory_or_job_site(url):
                    name = _directory_company_name(str(item.get("title", "")), city)
                    website = ""
                else:
                    candidate = classify_result(
                        url, str(item.get("title", "")), str(item.get("snippet", "")),
                        country="Morocco",
                    )
                    if not candidate or candidate.name == "Unknown":
                        continue
                    name = candidate.name
                    website = url
                if not name:
                    continue
                key = host_domain(url) if website else name.casefold()
                found.setdefault(key, BtpCompany(
                    name=name,
                    website=website,
                    city=city,
                    region=region,
                    latitude=latitude,
                    longitude=longitude,
                    distance_km=haversine_km(latitude, longitude),
                    source_url=url,
                    source="public_company_search",
                ))
        return sorted(found.values(), key=lambda item: (item.distance_km, item.name.casefold()))


def _load_city_search_cursor(path: Path | None, city_count: int) -> int:
    if path is None or not path.exists():
        return 0
    state = json.loads(path.read_text(encoding="utf-8"))
    cursor = int(state.get("next_city_index", 0))
    return cursor % city_count


def _save_city_search_cursor(path: Path | None, cursor: int) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"next_city_index": cursor}),
        encoding="utf-8",
    )
    temporary.replace(path)


def _binding(row: dict, name: str) -> str:
    return str((row.get(name) or {}).get("value", "")).strip()


def _wikidata_point(value: str) -> tuple[float, float] | None:
    match = re.fullmatch(r"Point\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", value)
    if not match:
        return None
    try:
        longitude, latitude = map(float, match.groups())
    except ValueError:
        return None
    return (latitude, longitude) if -90 <= latitude <= 90 and -180 <= longitude <= 180 else None


def _normalize_city_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(
        re.sub(r"[^a-z0-9 ]+", " ", normalized.encode("ascii", "ignore").decode()).split()
    )


def _canonical_city(value: str) -> str:
    normalized = _normalize_city_name(value)
    aliases = {
        "fes": "Fes",
        "fez": "Fes",
        "tanger": "Tangier",
        "tangier": "Tangier",
        "marrakesh": "Marrakech",
        "beni mellal": "Beni Mellal",
        "el jadida": "El Jadida",
    }
    if normalized in aliases:
        return aliases[normalized]
    return next(
        (city for city in MOROCCO_CITY_CENTERS if _normalize_city_name(city) == normalized),
        "",
    )


def _nearest_city(latitude: float, longitude: float) -> str:
    return min(
        MOROCCO_CITY_CENTERS,
        key=lambda city: haversine_km(
            *MOROCCO_CITY_CENTERS[city][1:],
            origin=(latitude, longitude),
        ),
    )


def _city_location(city: str) -> tuple[str, float, float]:
    return MOROCCO_CITY_CENTERS[city]


def _mentions_construction(text: str) -> bool:
    value = text.casefold()
    return any(term in value for term in (
        "btp", "construction", "travaux publics", "génie civil", "genie civil",
        "bâtiment", "batiment", "infrastructure",
    ))


def _directory_company_name(title: str, city: str) -> str:
    value = re.split(r"\s+(?:\||—|–|-|:)\s+", title, maxsplit=1)[0]
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    city = unicodedata.normalize("NFKD", city).encode("ascii", "ignore").decode()
    stop_words = {
        "entreprise", "entreprises", "societe", "compagnie", "company", "groupe",
        "group", "sarl", "sasu", "s.a", "casa",
        "btp", "construction", "constructions", "travaux", "publics", "génie",
        "genie", "civil", "bâtiment", "batiment", "maroc", "morocco",
        "annuaire", "directory", "liste", "des", "les", "de", "du", "la", "le",
        *re.sub(r"[^a-z0-9]+", " ", city.casefold()).split(),
    }
    tokens = [
        token for token in re.sub(r"[^a-z0-9&]+", " ", value.casefold()).split()
        if len(token) > 2 and token not in stop_words
    ]
    return " ".join(token.title() for token in tokens)[:255]


def _is_directory_or_job_site(url: str) -> bool:
    domain = host_domain(url)
    excluded = (
        "annuaire", "telecontact", "kerix", "charika", "kompass",
        "marocannuaire", "goafricaonline", "linkedin", "indeed",
        "facebook", "instagram", "youtube",
    )
    return any(term in domain for term in excluded)


def _company_name_matches(company_name: str, url: str, text: str) -> bool:
    host = host_domain(url).replace("-", " ").replace(".", " ")
    haystack = f"{host} {text}".casefold()
    ignored = {"btp", "sarl", "sa", "societe", "société", "entreprise", "company", "maroc"}
    tokens = [
        token for token in re.sub(r"[^a-z0-9]+", " ", company_name.casefold()).split()
        if len(token) >= 3 and token not in ignored
    ]
    matches = sum(token in haystack for token in tokens)
    return bool(tokens and matches == len(tokens))


def haversine_km(latitude: float, longitude: float,
                 origin: tuple[float, float] = CASABLANCA_CENTER) -> float:
    """Great-circle distance from the fixed coarse Casablanca city center."""
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = math.radians(latitude), math.radians(longitude)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    value = min(1.0, max(0.0, value))
    return 6371.0088 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def parse_overpass_companies(payload: dict, *, source_url: str = OVERPASS_URL,
                             limit: int = MAX_CANDIDATES) -> list[BtpCompany]:
    """Parse public OSM business fields; never read or persist street-address tags."""
    companies: list[BtpCompany] = []
    seen: set[tuple[str, str]] = set()
    elements = (payload or {}).get("elements", [])
    for element in elements:
        tags = element.get("tags") or {}
        if not _is_btp_business(tags):
            continue
        name = str(tags.get("name") or "").strip()[:255]
        point = _element_point(element)
        if not name or point is None:
            continue
        website = normalize_url(str(tags.get("contact:website") or tags.get("website") or ""))
        if len(website) > 512:
            website = ""
        city = _public_location_value(tags, ("addr:city", "is_in:city", "city"))
        region = _public_location_value(
            tags, ("addr:region", "addr:province", "addr:state", "is_in:region", "province", "state")
        )
        key = (name.casefold(), host_domain(website))
        if key in seen:
            continue
        seen.add(key)
        latitude, longitude = point
        companies.append(BtpCompany(
            name=name,
            website=website,
            city=city,
            region=region,
            latitude=latitude,
            longitude=longitude,
            distance_km=haversine_km(latitude, longitude),
            source_url=source_url,
            source="openstreetmap",
        ))
    companies.sort(key=lambda item: (item.distance_km, item.name.casefold()))
    return companies[: min(MAX_CANDIDATES, max(0, limit))]


def rank_nearest(companies: list[BtpCompany], max_companies: int = DEFAULT_MAX_COMPANIES
                 ) -> list[BtpCompany]:
    cap = max(1, min(HARD_MAX_COMPANIES, int(max_companies)))
    return sorted(companies, key=lambda item: (item.distance_km, item.name.casefold()))[:cap]


def _is_btp_business(tags: dict) -> bool:
    values = " ".join(
        str(tags.get(key, "")) for key in ("craft", "office", "industry", "construction")
    ).casefold()
    name = str(tags.get("name", "")).casefold()
    terms = (
        "btp", "construction", "builder", "civil_engineering", "civil engineering",
        "building", "travaux publics", "public works", "batiment", "bâtiment",
        "infrastructure",
    )
    return any(term in values or term in name for term in terms)


def _element_point(element: dict) -> tuple[float, float] | None:
    point = element if "lat" in element and "lon" in element else element.get("center") or {}
    try:
        latitude, longitude = float(point["lat"]), float(point["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def _public_location_value(tags: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(tags.get(key) or "").strip()
        if value:
            return value[:120]
    return ""


def _eligible_website(url: str) -> str:
    normalized = normalize_url(url)
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    try:
        if parsed.port not in (None, 443):
            return ""
    except ValueError:
        return ""
    domain = host_domain(normalized)
    free_mail_domains = {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
        "msn.com", "yahoo.com", "yahoo.fr", "proton.me", "protonmail.com", "icloud.com",
        "aol.com",
    }
    try:
        ipaddress.ip_address(domain)
        return ""
    except ValueError:
        pass
    excluded = {
        "facebook.com", "instagram.com", "linkedin.com", "tiktok.com",
        "youtube.com", "google.com", "maps.google.com", "wikipedia.org",
        *free_mail_domains,
    }
    if (not domain or domain.endswith((".localhost", ".local", ".internal"))
            or any(domain == blocked or domain.endswith("." + blocked) for blocked in excluded)):
        return ""
    return normalized


def _qualifying_contact(evidence: list[EvidenceFact], official_domain: str
                        ) -> tuple[str, EvidenceFact, EvidenceFact] | None:
    instructions = [
        fact for fact in evidence
        if fact.field_name == "spontaneous_application"
        and extract_spontaneous_instruction(fact.extracted_value or fact.snippet)
    ]
    for instruction in instructions:
        for fact in evidence:
            if fact.source_url != instruction.source_url:
                continue
            if fact.field_name not in {"general_email", "recruitment_email"}:
                continue
            address = fact.extracted_value.strip().lower()
            verified = EmailVerificationService().verify(
                address,
                source_url=fact.source_url,
                source_type="company_career",
                official=True,
                employer_domain=official_domain,
            )
            if verified.verified:
                return address, instruction, fact
    return None


def _application_portal(evidence: list[EvidenceFact], official_domain: str) -> bool:
    for fact in evidence:
        if fact.field_name != "application_url":
            continue
        url = normalize_url(fact.extracted_value)
        if url and host_domain(url) != official_domain:
            return True
    return False


class _FrenchSpontaneousCommunicator:
    """Static, French-only text with no vacancy, qualification, or availability claims."""

    async def generate(self, job, action, target_language="fr", recipient_name=""):
        return {
            "subject": "Candidature spontanée — secteur BTP",
            "body": (
                "Madame, Monsieur,\n\n"
                "Je vous adresse ma candidature spontanée afin que vous puissiez "
                "l’examiner pour d’éventuelles opportunités au sein de votre entreprise. "
                "Mon CV est joint à ce message pour votre examen.\n\n"
                "Je vous remercie de votre attention et reste à votre disposition pour "
                "tout renseignement complémentaire.\n\n"
                "Cordialement,"
            ),
        }


def _outreach_job(session: Session, company, contact: str, location: str) -> Job:
    digest = hashlib.sha256(f"btp-spontaneous:{company.id}".encode()).hexdigest()
    job, _ = mem.store.upsert_job(session, {
        "source": "btp_spontaneous",
        "external_id": digest,
        "dedup_key": f"btp-spontaneous:{company.id}",
        "title": f"Candidature spontanée — {company.name}"[:255],
        "company_id": company.id,
        "location": location[:255],
        "country": "Morocco",
        "description": "SPONTANEOUS_APPLICATION; not a vacancy or advertised job.",
        "url": company.website,
        "contact_email": contact,
        "status": "outreach_only",
        "source_type": SPONTANEOUS_APPLICATION,
        "source_quality": 0,
        "source_confidence": 100,
        "opportunity_type": SPONTANEOUS_APPLICATION,
        "application_method": "EMAIL",
        "application_url": "",
    })
    job.opportunity_type = SPONTANEOUS_APPLICATION
    job.source_type = SPONTANEOUS_APPLICATION
    job.status = "outreach_only"
    return job


def run_btp_outreach(
    session: Session,
    config: AgentConfig,
    settings: RunnerSettings,
    profile: CandidateProfile,
    *,
    origin_city: str = "Casablanca",
    max_companies: int = DEFAULT_MAX_COMPANIES,
    discovery: OverpassBtpDiscovery | None = None,
    wikidata_discovery: WikidataBtpDiscovery | None = None,
    seed_csv_path: Path | None = None,
    company_search: PublicBtpCompanySearch | None = None,
    researcher: WebsiteResearcher | None = None,
    website_finder: PublicCompanyWebsiteFinder | None = None,
    screening_state_path: Path | None = BTPOUTREACH_STATE_PATH,
) -> BtpOutreachReport:
    """Research nearest public BTP companies and prepare only eligible Gmail drafts."""
    if origin_city != "Casablanca":
        raise BtpOutreachSafetyError("Only the coarse Casablanca city-center origin is supported")
    if settings.email_mode != "draft" or settings.enable_email is not True:
        raise BtpOutreachSafetyError(
            "BTP outreach requires EMAIL_MODE=draft and ENABLE_EMAIL=true; no Gmail interaction occurred"
        )
    if settings.email_provider != "gmail":
        raise BtpOutreachSafetyError(
            "BTP outreach requires EMAIL_PROVIDER=gmail; no email-provider interaction occurred"
        )
    requested_cap = max(1, min(HARD_MAX_COMPANIES, int(max_companies)))
    report = BtpOutreachReport(origin_city=origin_city)
    sent_today = mem.store.count_dispatched_today(session)
    applications_today = mem.store.count_dispatched_today(session, action="APPLY")
    total_limit = int(config.email.get(
        "max_daily_outbound", config.rules.get("max_daily_outbound", 10)))
    application_limit = int(config.rules.get("max_daily_applications", 5))
    remaining_budget = max(0, min(
        total_limit - sent_today,
        application_limit - applications_today,
    ))
    if remaining_budget == 0:
        report.daily_limit_reached = True
    cap = min(requested_cap, remaining_budget)
    screening_state = (
        BtpOutreachState(screening_state_path) if screening_state_path else None
    )
    directory = discovery or OverpassBtpDiscovery()
    listed: list[BtpCompany] = []
    searched: list[BtpCompany] = []
    try:
        listed = directory.discover()
        report.errors.extend(getattr(directory, "errors", []))
    except Exception as exc:
        report.errors.extend(getattr(directory, "errors", []))
        report.errors.append(f"public Overpass discovery failed: {exc}")
    try:
        seeded = load_btp_seed_csv(seed_csv_path)
    except Exception as exc:
        seeded = []
        report.errors.append(f"curated BTP seed CSV could not be read: {exc}")
    wikidata = wikidata_discovery or WikidataBtpDiscovery()
    try:
        wikidata_companies = wikidata.discover()
        report.errors.extend(getattr(wikidata, "errors", []))
    except Exception as exc:
        wikidata_companies = []
        report.errors.append(f"Wikidata BTP discovery failed: {exc}")
    search = company_search or PublicBtpCompanySearch()
    try:
        searched = search.discover()
        report.errors.extend(getattr(search, "errors", []))
        report.search_budget_limited = getattr(search, "search_budget_limited", False)
    except Exception as exc:
        report.errors.append(f"public company web search failed: {exc}")
    if not listed and not seeded and not wikidata_companies and not searched:
        return report
    report.osm_candidates = len(listed)
    report.seed_candidates = len(seeded)
    report.wikidata_candidates = len(wikidata_companies)
    report.web_candidates = len(searched)
    candidates = rank_nearest(
        _merge_candidates(listed, seeded, wikidata_companies, searched),
        MAX_CANDIDATES,
    )
    report.candidates = len(candidates)
    report.known_website_candidates = sum(
        bool(_eligible_website(candidate.website)) for candidate in candidates
    )
    report.website_search_required = (
        report.candidates - report.known_website_candidates
    )
    targets = []
    for candidate in candidates:
        if screening_state and screening_state.is_suppressed(candidate):
            report.existing += 1
            continue
        discovery_source = candidate.source or (
            "openstreetmap" if candidate.source_url == OVERPASS_URL else "public_company_search"
        )
        company = mem.store.get_or_create_company(
            session,
            candidate.name,
            candidate.website,
            "Morocco",
            industry="BTP / construction",
            source=discovery_source,
            official_domain=host_domain(candidate.website),
        )
        company.website = company.website or candidate.website
        target, _ = mem.store.record_discovery(
            session,
            kind="BTP_COMPANY",
            url=candidate.website,
            title=candidate.name,
            company_name=candidate.name,
            company_id=company.id,
            source=discovery_source,
            source_type=discovery_source,
            discovery_channel="btp_outreach",
            evidence={
                "city": candidate.city,
                "region": candidate.region,
                "country": "Morocco",
                "latitude": candidate.latitude,
                "longitude": candidate.longitude,
                "distance_km": round(candidate.distance_km, 1),
                "discovery_source_url": candidate.source_url,
            },
            reason=f"Public {discovery_source.replace('_', ' ')} BTP company listing",
            canonical_key=f"btp-outreach:{company.id}",
        )
        if target.status in TERMINAL_TARGET_STATUSES:
            if screening_state and screening_state.is_expired_negative(candidate):
                target.status = "discovered"
            else:
                if screening_state:
                    screening_state.mark(candidate, target.status)
                report.existing += 1
                continue
        existing_outreach = next(
            (job for job in session.query(Job).filter_by(
                source="btp_spontaneous", company_id=company.id
            ).all() if job.opportunity_type == SPONTANEOUS_APPLICATION),
            None,
        )
        if existing_outreach and mem.store.find_applications(
            session, existing_outreach.id, statuses=("sent", "drafted", "deferred", "dry_run")
        ):
            target.status = "drafted_for_review"
            if screening_state:
                screening_state.mark(candidate, "drafted_for_review")
            report.existing += 1
            continue
        targets.append((candidate, company, target, existing_outreach))

    targets.sort(key=lambda item: (
        not bool(_eligible_website(item[0].website)),
        item[0].distance_km,
        item[0].name.casefold(),
    ))
    targets = targets[:cap]
    report.processed = len(targets)

    state_path = ROOT_DIR / "data" / "website_research_state.json"
    crawler = researcher or WebsiteResearcher(
        max_pages=12,
        per_host_delay=0.25,
        state_path=state_path,
        session=session,
        allow_redirects=False,
    )
    finder = website_finder
    if finder is None:
        finder = PublicCompanyWebsiteFinder(
            search=search.search if isinstance(search, PublicBtpCompanySearch) else None,
        )
    for candidate, company, target, existing_outreach in targets:
        if screening_state:
            screening_state.mark(candidate, "in_progress")
        try:
            website = _eligible_website(candidate.website) or finder.find(
                candidate.name, candidate.location,
            )
        except Exception as exc:
            if isinstance(exc, PublicSearchBudgetError):
                report.search_budget_limited = True
                target.status = "search_budget_deferred"
                if screening_state:
                    screening_state.clear(candidate)
                report.search_budget_deferred += 1
                report.companies.append(
                    _company_result(candidate, "search_budget_deferred")
                )
                continue
            report.errors.append(f"official website search failed for a listed company: {exc}")
            target.status = "research_error"
            if screening_state:
                screening_state.mark(candidate, "research_error")
            report.companies.append(_company_result(candidate, "research_error"))
            continue
        if not website:
            report.no_website += 1
            target.status = "no_verified_website"
            if screening_state:
                screening_state.mark(candidate, "no_verified_website")
            report.companies.append(_company_result(candidate, "no_official_website"))
            continue
        company.website = website
        company.official_domain = host_domain(website)
        report.researched += 1
        domain = host_domain(website)
        try:
            result = crawler.research(
                website,
                company_id=company.id,
                official_domain=domain,
            )
            evidence = list(result.evidence)
            evidence.extend(
                EvidenceFact(
                    source_url=row.source_url,
                    source_type=row.source_type,
                    page_title=row.page_title,
                    field_name=row.field_name,
                    extracted_value=row.extracted_value,
                    snippet=row.snippet,
                    domain_relationship=row.domain_relationship,
                    confidence=row.confidence,
                    reason_code=row.reason_code,
                )
                for row in mem.store.get_evidence(session, company_id=company.id)
            )
        except Exception as exc:
            report.errors.append(f"website research failed for a listed company: {exc}")
            target.status = "research_error"
            if screening_state:
                screening_state.mark(candidate, "research_error")
            report.companies.append(_company_result(candidate, "research_error"))
            continue

        instructions = [
            fact for fact in evidence
            if fact.field_name == "spontaneous_application"
            and extract_spontaneous_instruction(fact.extracted_value or fact.snippet)
        ]
        if not instructions:
            report.no_spontaneous_instructions += 1
            target.status = "no_explicit_instructions"
            if screening_state:
                screening_state.mark(candidate, "no_explicit_instructions")
            report.companies.append(_company_result(candidate, "no_explicit_instructions"))
            continue
        contact = _qualifying_contact(evidence, domain)
        if not contact:
            if _application_portal(evidence, domain):
                report.portal_or_form += 1
                target.status = "portal_or_form"
                if screening_state:
                    screening_state.mark(candidate, "portal_or_form")
                report.companies.append(_company_result(candidate, "portal_or_form"))
            else:
                report.no_qualifying_email += 1
                target.status = "no_official_employer_email"
                if screening_state:
                    screening_state.mark(candidate, "no_official_employer_email")
                report.companies.append(_company_result(candidate, "no_official_employer_email"))
            continue
        address, instruction_fact, email_fact = contact
        job = existing_outreach or _outreach_job(session, company, address, candidate.location)
        job.contact_email = address
        verification = EmailVerificationService(session).verify(
            address,
            source_url=email_fact.source_url,
            source_type="company_career",
            official=True,
            employer_domain=domain,
            job_id=job.id,
        )
        if not verification.verified:
            report.no_qualifying_email += 1
            target.status = "no_official_employer_email"
            if screening_state:
                screening_state.mark(candidate, "no_official_employer_email")
            report.companies.append(_company_result(candidate, "no_official_employer_email"))
            continue
        decision = mem.store.get_last_decision(session, job.id)
        if not decision:
            decision = mem.store.add_decision(
                session, job.id, SPONTANEOUS_APPLICATION, 0, {},
                f"Official spontaneous-application instruction: {instruction_fact.source_url}",
                ["SPONTANEOUS_APPLICATION"],
            )
        engine = ApplicationEngine(
            session, config, settings, profile, _FrenchSpontaneousCommunicator()
        )
        result = asyncio.run(engine.run(job, decision, "APPLY", address, "fr"))
        job.status = "outreach_only"
        job.opportunity_type = SPONTANEOUS_APPLICATION
        job.source_type = SPONTANEOUS_APPLICATION
        if result.get("status") == "drafted":
            report.drafts += 1
            status = "drafted_for_review"
            target.status = status
            if screening_state:
                screening_state.mark(candidate, status)
        else:
            report.blocked += 1
            status = "blocked_by_safety_gate"
            target.status = "blocked_by_safety_gate"
            if screening_state:
                screening_state.mark(candidate, status)
        report.companies.append(_company_result(
            candidate,
            status,
            gmail_draft_confirmed=bool(result.get("draft_id"))
            if result.get("status") == "drafted" else False,
        ))
    return report


def _company_result(
    candidate: BtpCompany,
    status: str,
    *,
    gmail_draft_confirmed: bool = False,
) -> dict:
    return {
        "name": candidate.name,
        "city": candidate.city,
        "region": candidate.region,
        "distance_km": round(candidate.distance_km, 1),
        "source": candidate.source,
        "status": status,
        "gmail_draft_confirmed": gmail_draft_confirmed,
    }


def _merge_candidates(*groups: list[BtpCompany]) -> list[BtpCompany]:
    by_name: dict[str, BtpCompany] = {}
    for group in groups:
        for candidate in group:
            key = " ".join(candidate.name.casefold().split())
            existing = by_name.get(key)
            if existing is None:
                by_name[key] = candidate
            elif not existing.website and candidate.website:
                by_name[key] = BtpCompany(
                    name=existing.name,
                    website=candidate.website,
                    city=existing.city or candidate.city,
                    region=existing.region or candidate.region,
                    latitude=existing.latitude,
                    longitude=existing.longitude,
                    distance_km=existing.distance_km,
                    source_url=candidate.source_url,
                    source=candidate.source or existing.source,
                )
    return sorted(by_name.values(), key=lambda item: (item.distance_km, item.name.casefold()))
