"""Bounded, draft-only spontaneous applications to public BTP company sites."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app import memory as mem
from app.config import ROOT_DIR, AgentConfig, CandidateProfile, RunnerSettings
from app.connectors.search_engine import resolve_search_url
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

OVERPASS_QUERY = (
    '[out:json][timeout:25][maxsize:5242880];'
    'area["ISO3166-1"="MA"][admin_level=2]->.morocco;('
    'nwr(area.morocco)["craft"~"construction|builder|civil_engineering|structural_engineering",i];'
    'nwr(area.morocco)["office"="construction"];'
    'nwr(area.morocco)["office"="company"]["industry"~"construction|building|civil engineering",i];'
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

    @property
    def location(self) -> str:
        return ", ".join(value for value in (self.city, self.region, "Morocco") if value)


@dataclass
class BtpOutreachReport:
    origin_city: str = "Casablanca"
    candidates: int = 0
    osm_candidates: int = 0
    web_candidates: int = 0
    processed: int = 0
    researched: int = 0
    drafts: int = 0
    existing: int = 0
    no_website: int = 0
    no_spontaneous_instructions: int = 0
    no_qualifying_email: int = 0
    portal_or_form: int = 0
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
                "asked": 0,
                "investigated": 0,
                "blocked": self.blocked,
                "errors": [],
                "drafts": self.drafts,
                "btp": {
                    "candidates": self.candidates,
                    "osm_candidates": self.osm_candidates,
                    "web_candidates": self.web_candidates,
                    "processed": self.processed,
                    "researched": self.researched,
                    "existing": self.existing,
                    "no_website": self.no_website,
                    "no_spontaneous_instructions": self.no_spontaneous_instructions,
                    "no_qualifying_email": self.no_qualifying_email,
                    "portal_or_form": self.portal_or_form,
                    "daily_limit_reached": self.daily_limit_reached,
                },
            },
            "followup": {"sent": 0, "blocked": 0, "errors": []},
            "companies": self.companies,
        }


class OverpassBtpDiscovery:
    """Fetch only a size-limited, capped public Overpass result."""

    def __init__(self, fetch_json=None):
        self.fetch_json = fetch_json or self._fetch_json

    def discover(self) -> list[BtpCompany]:
        payload = self.fetch_json(OVERPASS_URL, params={"data": OVERPASS_QUERY})
        return parse_overpass_companies(payload, source_url=OVERPASS_URL)

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


class PublicCompanyWebsiteFinder:
    """Find likely official sites for directory-listed companies, never contacts."""

    def __init__(self, search=None):
        self.search = search or self._search

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


class PublicBtpCompanySearch:
    """Bounded city-by-city web search that augments incomplete OSM listings."""

    def __init__(self, search=None):
        self.search = search or PublicCompanyWebsiteFinder._search
        self.errors: list[str] = []

    def discover(self) -> list[BtpCompany]:
        cities = sorted(
            MOROCCO_CITY_CENTERS.items(),
            key=lambda item: haversine_km(item[1][1], item[1][2]),
        )
        found: dict[str, BtpCompany] = {}
        for city, (region, latitude, longitude) in cities:
            query = f'entreprise BTP travaux publics génie civil Maroc {city}'
            try:
                results = self.search(query)[:MAX_WEBSITE_SEARCH_RESULTS]
            except Exception as exc:
                self.errors.append(f"public company search failed for {city}: {exc}")
                break
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
                ))
        return sorted(found.values(), key=lambda item: (item.distance_km, item.name.casefold()))


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
        ))
    companies.sort(key=lambda item: (item.distance_km, item.name.casefold()))
    return companies[: min(MAX_CANDIDATES, max(0, limit))]


def rank_nearest(companies: list[BtpCompany], max_companies: int = DEFAULT_MAX_COMPANIES
                 ) -> list[BtpCompany]:
    cap = max(1, min(HARD_MAX_COMPANIES, int(max_companies)))
    return sorted(companies, key=lambda item: (item.distance_km, item.name.casefold()))[:cap]


def _is_btp_business(tags: dict) -> bool:
    values = " ".join(str(tags.get(key, "")) for key in ("craft", "office", "industry")).lower()
    return any(term in values for term in ("construction", "builder", "civil_engineering",
                                           "civil engineering", "building"))


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
    company_search: PublicBtpCompanySearch | None = None,
    researcher: WebsiteResearcher | None = None,
    website_finder: PublicCompanyWebsiteFinder | None = None,
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
        return report
    cap = min(requested_cap, remaining_budget)
    directory = discovery or OverpassBtpDiscovery()
    listed: list[BtpCompany] = []
    searched: list[BtpCompany] = []
    try:
        listed = directory.discover()
    except Exception as exc:
        report.errors.append(f"public Overpass discovery failed: {exc}")
    search = company_search or PublicBtpCompanySearch()
    try:
        searched = search.discover()
        report.errors.extend(getattr(search, "errors", []))
    except Exception as exc:
        report.errors.append(f"public company web search failed: {exc}")
    if not listed and not searched:
        return report
    report.osm_candidates = len(listed)
    report.web_candidates = len(searched)
    candidates = rank_nearest(_merge_candidates(listed, searched), MAX_CANDIDATES)
    report.candidates = len(candidates)
    targets = []
    for candidate in candidates:
        discovery_source = (
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
            report.existing += 1
            continue
        targets.append((candidate, company, target, existing_outreach))

    targets = targets[:cap]
    report.processed = len(targets)

    state_path = ROOT_DIR / "data" / "website_research_state.json"
    crawler = researcher or WebsiteResearcher(
        max_pages=5,
        per_host_delay=0.25,
        state_path=state_path,
        session=session,
        allow_redirects=False,
    )
    finder = website_finder or PublicCompanyWebsiteFinder()
    for candidate, company, target, existing_outreach in targets:
        website = _eligible_website(candidate.website) or finder.find(
            candidate.name, candidate.location,
        )
        if not website:
            report.no_website += 1
            target.status = "no_verified_website"
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
                discover_sitemaps=False,
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
            report.companies.append(_company_result(candidate, "no_explicit_instructions"))
            continue
        contact = _qualifying_contact(evidence, domain)
        if not contact:
            if _application_portal(evidence, domain):
                report.portal_or_form += 1
                target.status = "portal_or_form"
                report.companies.append(_company_result(candidate, "portal_or_form"))
            else:
                report.no_qualifying_email += 1
                target.status = "no_official_employer_email"
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
        else:
            report.blocked += 1
            status = "blocked_by_safety_gate"
            target.status = "blocked_by_safety_gate"
        report.companies.append(_company_result(candidate, status))
    return report


def _company_result(candidate: BtpCompany, status: str) -> dict:
    return {
        "name": candidate.name,
        "city": candidate.city,
        "region": candidate.region,
        "distance_km": round(candidate.distance_km, 1),
        "status": status,
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
                )
    return sorted(by_name.values(), key=lambda item: (item.distance_km, item.name.casefold()))
