"""Discovery workflow — runs the configured connectors across the search plan,
normalizes, deduplicates against memory and stores only genuinely new jobs.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import memory as mem
from app.config import ROOT_DIR, AgentConfig, CandidateProfile, Preferences, load_yaml
from app.connectors import registry
from app.connectors.tavily_resilience import (
    TAVILY_DAILY_REQUEST_LIMIT,
    ApiAttemptBudget,
    DailyBudget,
    TavilyKey,
    key_fingerprint,
)
from app.deduplication import find_duplicates
from app.discovery.email_verification import EmailVerificationService
from app.discovery.opportunity_details import (
    classify_entity,
    classify_opportunity,
    is_verified_individual_vacancy,
)
from app.discovery.vacancy_verification import (
    MAX_DETAIL_VERIFICATIONS_PER_RUN,
    VacancyDetailVerifier,
    supported_ats_detail,
)
from app.discovery.verification import freshness_label
from app.discovery.vocabulary import CandidateVocabulary
from app.discovery.website_researcher import host_domain, normalize_url
from app.models import Company, Job
from app.normalization import normalization_rejection_reason, normalize

logger = logging.getLogger(__name__)

DEFAULT_SOURCES_PATH = ROOT_DIR / "config" / "sources.yaml"
DEMO_SOURCES_PATH = ROOT_DIR / "config" / "sources_demo.yaml"
ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "smartrecruiters.com",
    "ashbyhq.com", "personio.com", "workable.com",
)
JOB_BOARD_DOMAINS = (
    "linkedin.com", "indeed.com", "glassdoor.com", "monster.com",
    "ziprecruiter.com", "naukri.com", "bayt.com", "rekrute.com",
    "emploi.ma", "anapec.org", "welcome-to-the-jungle.com",
)
DIRECTORY_DOMAINS = (
    "annuaire.com", "telecontact.ma", "kerix.net", "kerix.ma", "kompass.com",
    "marocannuaire.org", "marocannuaire.com", "maroc1000.net",
    "marocproguide.com", "goafricaonline.com",
)


class CompanyUrlKind(StrEnum):
    EMPLOYER_OFFICIAL_WEBSITE = "EMPLOYER_OFFICIAL_WEBSITE"
    EMPLOYER_WEBSITE = "EMPLOYER_WEBSITE"
    EMPLOYER_CAREERS_PAGE = "EMPLOYER_CAREERS_PAGE"
    EMPLOYER_RECRUITMENT_PAGE = "EMPLOYER_RECRUITMENT_PAGE"
    COMPANY_DIRECTORY = "COMPANY_DIRECTORY"
    GOVERNMENT_COMPANY_REGISTRY = "GOVERNMENT_COMPANY_REGISTRY"
    THIRD_PARTY_COMPANY_PROFILE = "THIRD_PARTY_COMPANY_PROFILE"
    ATS = "ATS"
    JOB_BOARD = "JOB_BOARD"
    DOCUMENT_HOST = "DOCUMENT_HOST"
    DIRECTORY = "DIRECTORY"
    UNKNOWN = "UNKNOWN"


def classify_url_role(url: str, title: str = "", snippet: str = "", *, source_type: str = "") -> str:
    """Classify a discovered URL without promoting third-party discovery pages as employer-owned sites."""
    candidate = normalize_url(str(url or ""))
    if not candidate:
        return CompanyUrlKind.UNKNOWN.value
    domain = (host_domain(candidate) or "").lower()
    path = urlparse(candidate).path.lower()
    text = f"{title or ''} {snippet or ''}".lower()
    if not domain:
        return CompanyUrlKind.UNKNOWN.value
    if any(domain == item or domain.endswith(f".{item}") for item in ATS_DOMAINS):
        return CompanyUrlKind.ATS.value
    if any(domain == item or domain.endswith(f".{item}") for item in JOB_BOARD_DOMAINS):
        return CompanyUrlKind.JOB_BOARD.value
    if any(domain == item or domain.endswith(f".{item}") for item in DIRECTORY_DOMAINS):
        return CompanyUrlKind.COMPANY_DIRECTORY.value
    if any(domain == item or domain.endswith(f".{item}") for item in (
        "scribd.com", "rocketreach.co", "zoominfo.com", "apollo.io",
        "wikipedia.org", "crunchbase.com",
    )):
        return CompanyUrlKind.THIRD_PARTY_COMPANY_PROFILE.value
    if any(domain == item or domain.endswith(f".{item}") for item in (
        "docs.google.com", "drive.google.com",
    )) or path.endswith(".pdf") or "/document/" in path or "/documents/" in path:
        return CompanyUrlKind.DOCUMENT_HOST.value
    if domain.endswith((".gov.ma", ".gov", ".gouv.fr")) and any(
        token in text or token in path
        for token in ("registre", "societe", "company", "annuaire", "directory")
    ):
        return CompanyUrlKind.GOVERNMENT_COMPANY_REGISTRY.value
    if any(token in text for token in (
        "annuaire", "directory", "liste des entreprises", "company directory",
        "companies list", "répertoire des entreprises", "repertoire des entreprises",
        "secteur d'activité", "secteur d activite",
    )):
        return CompanyUrlKind.COMPANY_DIRECTORY.value
    if any(token in path for token in (
        "directory", "annuaire", "liste-entreprises", "company-profile",
        "profile/", "listing", "secteur-d",
    )):
        return CompanyUrlKind.THIRD_PARTY_COMPANY_PROFILE.value
    if (
        "careers" in path or "career" in path or "recruit" in path
        or domain.startswith(("careers.", "career.", "jobs.", "recruiting."))
    ):
        return CompanyUrlKind.EMPLOYER_CAREERS_PAGE.value
    if source_type in {"company_career", "company_careers", "company_website", "official_website"}:
        return CompanyUrlKind.EMPLOYER_OFFICIAL_WEBSITE.value
    return CompanyUrlKind.UNKNOWN.value


def _classify_company_urls(opp, source_type: str) -> dict[str, str]:
    """Classify employer-related links without promoting job/ATS or directory URLs to official sites."""
    raw = opp.raw or {}
    result = {
        "kind": CompanyUrlKind.UNKNOWN.value,
        "website": "",
        "official_domain": "",
        "careers_url": "",
        "recruitment_url": "",
        "profile_url": "",
        "discovery_source_url": str(getattr(opp, "url", "") or ""),
    }
    raw_title = str(raw.get("title") or getattr(opp, "title", "") or "")
    raw_snippet = str(raw.get("snippet") or getattr(opp, "description", "") or "")
    direct_values = (
        ("company_website", CompanyUrlKind.EMPLOYER_OFFICIAL_WEBSITE),
        ("official_website", CompanyUrlKind.EMPLOYER_OFFICIAL_WEBSITE),
        ("careers_url", CompanyUrlKind.EMPLOYER_CAREERS_PAGE),
        ("recruitment_url", CompanyUrlKind.EMPLOYER_RECRUITMENT_PAGE),
    )
    for field_name, kind in direct_values:
        direct = _trusted_employer_url(
            raw.get(field_name), title=raw_title, snippet=raw_snippet,
        )
        if not direct:
            continue
        result["kind"] = kind.value
        if kind == CompanyUrlKind.EMPLOYER_OFFICIAL_WEBSITE:
            result["website"] = _website_origin(direct)
            result["official_domain"] = host_domain(direct)
            if field_name == "company_profile_url":
                result["profile_url"] = direct
        elif kind == CompanyUrlKind.EMPLOYER_CAREERS_PAGE:
            result["careers_url"] = direct
            result["website"] = _website_origin(direct)
            result["official_domain"] = host_domain(direct)
        else:
            result["recruitment_url"] = direct
            result["website"] = _website_origin(direct)
            result["official_domain"] = host_domain(direct)
        return result

    profile_url = normalize_url(str(raw.get("company_profile_url") or ""))
    if profile_url and classify_url_role(
        profile_url, raw_title, raw_snippet, source_type=source_type,
    ) in {
        CompanyUrlKind.COMPANY_DIRECTORY.value,
        CompanyUrlKind.GOVERNMENT_COMPANY_REGISTRY.value,
        CompanyUrlKind.THIRD_PARTY_COMPANY_PROFILE.value,
        CompanyUrlKind.DOCUMENT_HOST.value,
    }:
        result["kind"] = classify_url_role(
            profile_url, raw_title, raw_snippet, source_type=source_type,
        )
        result["profile_url"] = profile_url
        return result

    if source_type == "ats" or raw.get("ats"):
        result["kind"] = CompanyUrlKind.ATS.value
        return result

    page = _trusted_employer_url(
        raw.get("page"), title=raw_title, snippet=raw_snippet,
    )
    if page and source_type == "company_career":
        result.update(
            kind=CompanyUrlKind.EMPLOYER_CAREERS_PAGE.value,
            website=_website_origin(page),
            official_domain=host_domain(page),
            careers_url=page,
        )
        return result

    result["kind"] = classify_url_role(
        result["discovery_source_url"], raw_title, raw_snippet,
        source_type=source_type,
    )
    if result["kind"] == CompanyUrlKind.COMPANY_DIRECTORY.value:
        result["kind"] = CompanyUrlKind.DIRECTORY.value
    return result


def _trusted_employer_url(value, *, title: str = "", snippet: str = "") -> str:
    normalized = normalize_url(str(value or ""))
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    try:
        if parsed.port not in (None, 443):
            return ""
    except ValueError:
        return ""
    role = classify_url_role(normalized, title, snippet)
    if role in {
        CompanyUrlKind.ATS.value,
        CompanyUrlKind.JOB_BOARD.value,
        CompanyUrlKind.COMPANY_DIRECTORY.value,
        CompanyUrlKind.GOVERNMENT_COMPANY_REGISTRY.value,
        CompanyUrlKind.THIRD_PARTY_COMPANY_PROFILE.value,
        CompanyUrlKind.DOCUMENT_HOST.value,
    }:
        return ""
    return normalized


def _website_origin(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))


def _direct_company_career_url(opp, source_type: str) -> str:
    """Compatibility helper returning only a provenance-qualified career page."""
    classified = _classify_company_urls(opp, source_type)
    return classified["careers_url"]


def _upsert_opportunity_company(
    session: Session, opp, source_type: str,
) -> tuple[Company, dict[str, str]]:
    company_urls = _classify_company_urls(opp, source_type)
    company = mem.store.get_or_create_company(
        session,
        opp.company or "Unknown",
        company_urls["website"],
        opp.country,
        careers_url=company_urls["careers_url"],
        recruitment_url=company_urls["recruitment_url"],
        source=opp.source,
        official_domain=company_urls["official_domain"],
        sponsorship_signal=opp.sponsorship_signal,
        international_recruitment_signal=opp.international_candidate_signal,
    )
    incoming_domain = company_urls["official_domain"]
    if incoming_domain and (
        not company.official_domain or company.official_domain == incoming_domain
    ):
        company.official_domain = incoming_domain
        if not company.website or host_domain(company.website) != incoming_domain:
            company.website = company_urls["website"]
    return company, company_urls


@dataclass
class DiscoveryReport:
    combinations_attempted: int = 0
    source_requests_attempted: int = 0
    opportunities_fetched: int = 0
    new_jobs: int = 0
    duplicates: int = 0
    source_errors: list[str] = field(default_factory=list)
    employers_discovered: int = 0
    companies_discovered: int = 0
    company_universe: dict = field(default_factory=dict)
    immigration_facts: int = 0
    opportunity_sources: int = 0
    social_signals: int = 0
    # One entry per configured connector.  This makes a zero-result run
    # explainable without treating it as an error.
    source_reports: list[dict] = field(default_factory=list)
    plan_combinations: list[dict] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    api_request_budget: dict = field(default_factory=dict)
    new_opportunity_origins: dict[int, str] = field(default_factory=dict)


def _load_source_connectors(
    path: Path | None = None,
    *,
    budget_reserver=None,
    budget_exhauster=None,
) -> list[tuple[dict, object]]:
    path = path or DEFAULT_SOURCES_PATH
    cfg = load_yaml(path)
    # Ensure .env is loaded into os.environ so downstream modules see TAVILY_API_KEYS
    load_dotenv()
    proxy = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or None

    # Build Tavily resilient keys from TAVILY_API_KEYS (comma-separated)
    tavily_keys_str = os.getenv("TAVILY_API_KEYS") or os.getenv("TAVILY_API_KEY", "")
    tavily_keys = [k.strip() for k in tavily_keys_str.split(",") if k.strip()]
    tavily_resilient_keys = []
    seen_key_fingerprints = set()
    for k in tavily_keys:
        fingerprint = key_fingerprint(k, 64)
        if fingerprint in seen_key_fingerprints:
            continue
        seen_key_fingerprints.add(fingerprint)
        budget_path = Path(f"data/tavily_budget_{key_fingerprint(k)}.json")
        budget_path.parent.mkdir(parents=True, exist_ok=True)
        tavily_resilient_keys.append(TavilyKey(
            api_key=k,
            budget=DailyBudget(
                budget_path,
                limit=TAVILY_DAILY_REQUEST_LIMIT,
                durable_reserver=budget_reserver,
                durable_exhauster=budget_exhauster,
                reservation_type="general",
            ),
            fp=key_fingerprint(k),
        ))

    connectors = []
    for c in cfg.get("connectors", []):
        if not c.get("enabled", True):
            continue
        kind = c["kind"]
        if kind not in registry:
            logger.warning("Unknown connector kind %s — skipping", kind)
            continue
        opts = _resolve_paths(c)
        opts.pop("kind", None)
        opts.pop("name", None)
        opts.pop("enabled", None)
        opts.pop("mode", None)  # metadata for diagnostics, not connector options
        opts.pop("applies_to_countries", None)
        opts.pop("allocation_priority", None)
        opts.pop("allocation_group", None)
        if kind in ("search_engine", "tavily"):
            opts["source_name"] = c.get("name", kind)
            if proxy:
                opts["proxy"] = proxy
        elif kind == "emploi_public":
            opts["source_name"] = c.get("name", kind)
        if kind == "tavily" and tavily_resilient_keys:
            # Use resilient wrapper with all keys
            from app.connectors.tavily_resilience import ResilientTavily, TavilyCache
            cache = TavilyCache(Path("data/tavily_cache.json"))
            connector = ResilientTavily(
                keys=tavily_resilient_keys,
                cache=cache,
                results_per_query=c.get("results_per_query", 10),
                official_only=c.get("official_only", False),
                domains=c.get("domains"),
                source_name=c.get("name", "tavily"),
                result_source_type=c.get("result_source_type", "search_engine"),
            )
            connectors.append((c, connector))
            continue
        try:
            connectors.append((c, registry[kind](**opts)))
        except Exception as exc:
            logger.warning("Failed to init connector %s: %s", c.get("name"), exc)
    return connectors


def _tavily_budgets(connectors: list[tuple[dict, object]]) -> list[object]:
    budgets = {}
    for _source_cfg, connector in connectors:
        if not callable(getattr(connector, "has_cached_result", None)):
            continue
        for key in getattr(connector, "keys", []):
            budget = getattr(key, "budget", None)
            if budget is not None:
                budgets[getattr(budget, "identity", id(budget))] = budget
    return list(budgets.values())


def _source_applies_to_country(source_cfg: dict, combo: dict) -> bool:
    countries = source_cfg.get("applies_to_countries")
    if not countries:
        return not bool(source_cfg.get("domains"))
    if isinstance(countries, str):
        countries = [countries]
    country = str(combo.get("country") or combo.get("location") or "").casefold()
    return "*" in {str(value).casefold() for value in countries} or country in {
        str(value).casefold() for value in countries
    }


def _allocate_source_queries(
    connectors: list[tuple[dict, object]],
    plan: list[dict],
    max_requests_per_source: int,
    *,
    rotation_offset: int | None = None,
    max_api_attempts: int | None = None,
    deferral_reasons: list[dict[str, int]] | None = None,
    country_attempt_limits: dict[str, int] | None = None,
) -> tuple[list[list[dict]], list[int], list[int]]:
    """Allocate applicable source requests against the shared Tavily budget.

    Plan combinations are shared across sources; requests are not. A request's
    source scope determines applicability, filtering, and cache identity.
    """
    scheduled: list[list[dict]] = []
    deferred_by_source: list[int] = []
    stale_by_source: list[int] = []
    budgeted_sources: list[int] = []
    uncached_by_source: dict[int, list[int]] = {}
    daily_available = sum(budget.remaining() for budget in _tavily_budgets(connectors))
    available = daily_available
    if max_api_attempts is not None:
        available = min(available, max(0, int(max_api_attempts)))

    for source_index, (_source_cfg, connector) in enumerate(connectors):
        combos = plan[:max_requests_per_source] if max_requests_per_source else plan
        scheduled.append([])
        deferred_by_source.append(0)
        stale_by_source.append(0)
        applicable_indices = []
        for combo_index, combo in enumerate(combos):
            if _source_applies_to_country(_source_cfg, combo):
                applicable_indices.append(combo_index)
        if not callable(getattr(connector, "has_cached_result", None)) or not getattr(
            connector, "keys", None
        ):
            scheduled[source_index] = [
                combo for index, combo in enumerate(combos)
                if index in applicable_indices
            ]
            continue
        budgeted_sources.append(source_index)
        uncached_indices = []
        for combo_index in applicable_indices:
            combo = combos[combo_index]
            if connector.has_cached_result(combo["query"], combo["location"]):
                scheduled[source_index].append(combo)
            else:
                uncached_indices.append(combo_index)
                stale_check = getattr(connector, "has_stale_cached_result", None)
                if callable(stale_check) and stale_check(
                    combo["query"], combo["location"],
                ):
                    stale_by_source[source_index] += 1
        uncached_by_source[source_index] = uncached_indices

    if country_attempt_limits is not None:
        allowed_countries = {
            str(country).casefold() for country in country_attempt_limits
        }
        for source_index, (source_cfg, _connector) in enumerate(connectors):
            combos = plan[:max_requests_per_source] if max_requests_per_source else plan
            target_combos = [
                combo for combo in combos
                if _source_applies_to_country(source_cfg, combo)
                and str(
                    combo.get("country") or combo.get("location") or "",
                ).casefold() in allowed_countries
            ]
            if source_index in uncached_by_source:
                cached_indices = {
                    index for index, combo in enumerate(combos)
                    if _source_applies_to_country(source_cfg, combo)
                    and str(
                        combo.get("country") or combo.get("location") or "",
                    ).casefold() in allowed_countries
                    and index not in uncached_by_source[source_index]
                }
                uncached_indices = set(uncached_by_source[source_index])
                scheduled[source_index] = [
                    combo for index, combo in enumerate(combos)
                    if index in cached_indices or (
                        index in uncached_indices
                        and str(
                            combo.get("country") or combo.get("location") or "",
                        ).casefold() in allowed_countries
                    )
                ]
            else:
                scheduled[source_index] = target_combos
        return scheduled, deferred_by_source, stale_by_source

    if not budgeted_sources:
        return scheduled, deferred_by_source, stale_by_source
    offset = (
        datetime.now(timezone.utc).hour // 2
        if rotation_offset is None else rotation_offset
    ) % len(budgeted_sources)
    order = budgeted_sources[offset:] + budgeted_sources[:offset]
    source_combos = {
        index: (
            plan[:max_requests_per_source] if max_requests_per_source else plan
        )
        for index in budgeted_sources
    }
    selected_indices = {index: set() for index in budgeted_sources}
    source_counts = {index: 0 for index in budgeted_sources}
    combo_count = max(
        (len(source_combos[index]) for index in budgeted_sources),
        default=0,
    )
    def source_group(source_cfg: dict) -> str:
        if source_cfg.get("allocation_group"):
            return str(source_cfg["allocation_group"])
        if source_cfg.get("official_only"):
            return "official_ats"
        if source_cfg.get("domains"):
            return "market_specific"
        return "broad"

    # Spend the first slots across distinct request classes (broad web, official
    # ATS, and market-specific domains) so one class cannot consume every slot.
    group_candidates: dict[str, list[tuple[int, int]]] = {}
    for source_index in budgeted_sources:
        group = source_group(connectors[source_index][0])
        group_candidates.setdefault(group, []).extend(
            (source_index, combo_index)
            for combo_index in uncached_by_source[source_index]
        )
    groups_by_priority = sorted(
        group_candidates,
        key=lambda group: max(
            int(connectors[source_index][0].get("allocation_priority", 50))
            for source_index, _combo_index in group_candidates[group]
        ),
        reverse=True,
    )
    grouped_combo_counts: dict[int, int] = {}
    for group in groups_by_priority:
        candidates = group_candidates[group]
        chosen = max(
            candidates,
            key=lambda pair: (
                -grouped_combo_counts.get(pair[1], 0),
                int(connectors[pair[0]][0].get("allocation_priority", 50)),
                -pair[1],
                -order.index(pair[0]),
            ),
        )
        source_index, combo_index = chosen
        if available <= 0:
            break
        selected_indices[source_index].add(combo_index)
        source_counts[source_index] += 1
        grouped_combo_counts[combo_index] = (
            grouped_combo_counts.get(combo_index, 0) + 1
        )
        available -= 1

    # Diversify the remaining allocation across distinct market-specific
    # scopes while reserving one slot for an unserved search combination.
    # These requests still use the same durable key ledgers and run cap.
    market_sources = [
        index for index in budgeted_sources
        if source_group(connectors[index][0]) == "market_specific"
        and uncached_by_source[index]
        and not selected_indices[index]
    ]
    extra_market_slots = min(4, max(0, available - 1))
    for _ in range(extra_market_slots):
        candidates = [
            index for index in market_sources
            if not selected_indices[index]
        ]
        if not candidates:
            break
        source_index = max(
            candidates,
            key=lambda index: (
                int(connectors[index][0].get("allocation_priority", 50)),
                -order.index(index),
            ),
        )
        combo_index = min(
            uncached_by_source[source_index],
            key=lambda index: (
                grouped_combo_counts.get(index, 0),
                index,
            ),
        )
        selected_indices[source_index].add(combo_index)
        source_counts[source_index] += 1
        grouped_combo_counts[combo_index] = (
            grouped_combo_counts.get(combo_index, 0) + 1
        )
        available -= 1

    # Then cover unserved plan combinations in their adaptive priority order.
    # Source-specific requests may repeat a combination because their scopes
    # are distinct; this pass avoids doing so until other combinations had a
    # first request.
    served_combinations = {
        combo_index
        for selected in selected_indices.values()
        for combo_index in selected
    }
    for combo_index in range(combo_count):
        if available <= 0:
            break
        if combo_index in served_combinations:
            continue
        candidates = [
            index for index in budgeted_sources
            if combo_index in uncached_by_source[index]
            and combo_index not in selected_indices[index]
        ]
        if not candidates:
            continue
        selected_source = max(
            candidates,
            key=lambda index: (
                int(connectors[index][0].get("allocation_priority", 50))
                - source_counts[index] * 25,
                -order.index(index),
            ),
        )
        selected_indices[selected_source].add(combo_index)
        source_counts[selected_source] += 1
        available -= 1
        served_combinations.add(combo_index)

    # Only use remaining allowance on additional source scopes after each
    # distinct combination has had a chance to run.
    if available > 0:
        for combo_index in range(combo_count):
            candidates = [
                index for index in budgeted_sources
                if combo_index in uncached_by_source[index]
                and combo_index not in selected_indices[index]
            ]
            while candidates and available > 0:
                selected_source = max(
                    candidates,
                    key=lambda index: (
                        int(connectors[index][0].get("allocation_priority", 50))
                        - source_counts[index] * 25,
                        -order.index(index),
                    ),
                )
                selected_indices[selected_source].add(combo_index)
                source_counts[selected_source] += 1
                available -= 1
                candidates.remove(selected_source)

    for source_index in budgeted_sources:
        selected = selected_indices[source_index]
        combos = source_combos[source_index]
        scheduled[source_index] = [
            combo for index, combo in enumerate(combos)
            if _source_applies_to_country(connectors[source_index][0], combo)
            and (
                index not in uncached_by_source[source_index]
                or index in selected
            )
        ]
        deferred_by_source[source_index] = (
            len(uncached_by_source[source_index]) - len(selected)
        )
        if deferral_reasons is not None and deferred_by_source[source_index]:
            if daily_available <= 0:
                reason = "aggregate_daily_budget"
            elif (
                max_api_attempts is not None
                and max(0, int(max_api_attempts)) <= daily_available
            ):
                reason = "run_attempt_cap"
            else:
                reason = "aggregate_daily_budget"
            deferral_reasons[source_index][reason] = (
                deferral_reasons[source_index].get(reason, 0)
                + deferred_by_source[source_index]
            )
    return scheduled, deferred_by_source, stale_by_source


def _remaining_tavily_requests(connector, country: str | None = None) -> int:
    budgets = {}
    for key in getattr(connector, "keys", []):
        budget = getattr(key, "budget", None)
        if budget is not None:
            budgets[getattr(budget, "identity", id(budget))] = budget
    remaining = sum(budget.remaining() for budget in budgets.values())
    attempt_budget = getattr(connector, "attempt_budget", None)
    if attempt_budget is not None:
        remaining = min(remaining, attempt_budget.remaining(country))
    return remaining


def _apply_vacancy_evidence(session: Session, job: Job, evidence) -> None:
    job.entity_type = "INDIVIDUAL_VACANCY"
    job.entity_evidence_url = evidence.evidence_url
    job.entity_reason_code = evidence.reason_code
    job.last_verified_at = evidence.observed_at
    if evidence.title:
        job.title = evidence.title
    if evidence.employer:
        company = mem.store.get_or_create_company(
            session, evidence.employer, country=evidence.country,
        )
        job.company_id = company.id
    if evidence.location:
        job.location = evidence.location
    if evidence.country:
        job.country = evidence.country
    if evidence.description:
        job.description = evidence.description
    job.posted_at = evidence.posted_at
    job.posted_at_source = evidence.posted_at_source
    job.posted_at_evidence_url = (
        evidence.evidence_url if evidence.posted_at is not None else ""
    )
    job.closing_at = evidence.closing_at
    if evidence.outcome == "OPEN":
        job.country = evidence.country or job.country or "UNKNOWN"
        job.freshness = freshness_label(evidence.posted_at)
        job.opportunity_type = classify_opportunity(
            evidence.title, evidence.description, job.source_type,
        )
        job.application_method = "COMPANY_PORTAL"
        job.application_url = evidence.application_url
        job.verification_status = "verified"
        job.entity_state = "VACANCY_VERIFIED"
        if job.status == "verification_required":
            job.status = "new"
    else:
        job.opportunity_type = "UNKNOWN"
        job.entity_state = (
            "VACANCY_CLOSED" if evidence.outcome == "CLOSED"
            else "VACANCY_STATUS_UNKNOWN"
        )
        job.verification_status = (
            "closed" if evidence.outcome == "CLOSED" else "unverified"
        )
        job.application_method = "UNKNOWN"
        job.application_url = ""


def _public_competition(opp) -> dict:
    raw = getattr(opp, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    metadata = raw.get("public_competition")
    return metadata if isinstance(metadata, dict) else {}


def _competition_observed_at(metadata: dict) -> datetime | None:
    try:
        observed_at = datetime.fromisoformat(str(metadata.get("observed_at") or ""))
    except ValueError:
        return None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return observed_at


def _apply_public_competition_evidence(
    session: Session, job: Job, opp,
) -> str:
    metadata = _public_competition(opp)
    status = str(metadata.get("status") or "UNKNOWN")
    observed_at = _competition_observed_at(metadata)
    detail_available = metadata.get("reason_code") not in {
        "detail_page_unavailable", "detail_page_limit_reached",
    }
    if detail_available:
        job.title = opp.title or job.title
        job.description = opp.description or job.description
        job.location = opp.location if opp.location != "Morocco" else job.location
    job.url = opp.url or job.url
    job.country = opp.country or job.country or "Morocco"
    job.last_verified_at = observed_at
    job.entity_type = "INDIVIDUAL_VACANCY"
    job.entity_evidence_url = opp.url
    job.public_competition = metadata
    if opp.closing_at is not None:
        job.closing_at = opp.closing_at
    if opp.posted_at is not None:
        job.posted_at = opp.posted_at
        job.posted_at_source = opp.posted_at_source
        job.posted_at_evidence_url = opp.posted_at_evidence_url
        job.freshness = freshness_label(opp.posted_at)
    if opp.company:
        company = mem.store.get_or_create_company(
            session, opp.company, country=job.country,
        )
        job.company_id = company.id
    job.source_type = "government"
    route = metadata.get("submission_route") or {}
    if status == "OPEN_ANNOUNCEMENT":
        job.entity_state = "VACANCY_VERIFIED"
        job.entity_reason_code = "official_public_competition_verified_open"
        job.verification_status = "verified"
        if route.get("kind") == "ONLINE_PORTAL":
            job.application_method = "COMPANY_PORTAL"
            job.application_url = str(route.get("destination_url") or "")
        elif route.get("kind") == "EMAIL":
            job.application_method = "EMAIL"
            job.application_url = ""
        else:
            job.application_method = "UNKNOWN"
            job.application_url = ""
        if job.status in {"closed", "verification_required"}:
            job.status = "new"
    elif status == "CLOSED_ANNOUNCEMENT":
        job.entity_state = "VACANCY_CLOSED"
        job.entity_reason_code = str(
            metadata.get("reason_code") or "official_public_competition_closed",
        )
        job.verification_status = "closed"
        job.status = "closed"
        job.application_method = "UNKNOWN"
        job.application_url = ""
    else:
        job.entity_state = "VACANCY_STATUS_UNKNOWN"
        job.entity_reason_code = str(
            metadata.get("reason_code") or "public_competition_status_unknown",
        )
        job.verification_status = "unverified"
        job.status = "verification_required"
        job.application_method = "UNKNOWN"
        job.application_url = ""
    return status


def _persist_public_competition_notice(
    session: Session, opp, metadata: dict, source_name: str,
) -> bool:
    """Store procedural evidence and update, but never create, a candidate job."""
    portal_id = str(metadata.get("portal_id") or "")
    stage = str(metadata.get("stage") or "")
    observed_at = _competition_observed_at(metadata)
    existing = None
    if portal_id:
        existing = session.execute(
            select(Job).where(
                Job.source == source_name,
                Job.external_id == portal_id,
            ).limit(1),
        ).scalar_one_or_none()
    notice = {
        "stage": stage,
        "status": str(metadata.get("status") or "UNKNOWN"),
        "observed_at": observed_at.isoformat() if observed_at else "",
        "evidence_url": str(metadata.get("evidence_url") or opp.url),
        "reason_code": str(metadata.get("reason_code") or "official_procedural_notice"),
    }
    notice["notice_id"] = (
        f"{source_name}:{portal_id}:{stage}:{notice['observed_at']}"
    )
    if existing is not None:
        updated = dict(existing.public_competition or {})
        previous = updated.get("latest_procedural_notice")
        previous = previous if isinstance(previous, dict) else None
        history = updated.get("procedural_notice_history")
        history = list(history) if isinstance(history, list) else []
        if previous is not None and not any(
            isinstance(item, dict)
            and item.get("notice_id") == previous.get("notice_id")
            for item in history
        ):
            legacy_previous = dict(previous)
            legacy_previous.setdefault(
                "notice_id",
                f"{source_name}:{portal_id}:"
                f"{legacy_previous.get('stage', '')}:"
                f"{legacy_previous.get('observed_at', '')}",
            )
            history.append(legacy_previous)

        previous_id = (
            str(previous.get("notice_id"))
            if previous and previous.get("notice_id")
            else (
                f"{source_name}:{portal_id}:"
                f"{previous.get('stage', '')}:"
                f"{previous.get('observed_at', '')}"
                if previous else ""
            )
        )
        previous_observed = (
            _competition_observed_at(previous)
            if previous else None
        )
        is_latest = previous is None or (
            observed_at is not None
            and (previous_observed is None or observed_at >= previous_observed)
        )
        if previous_id and previous_id != notice["notice_id"]:
            notice[
                "supersedes_notice_id" if is_latest else "superseded_by_notice_id"
            ] = previous_id
        if not any(
            isinstance(item, dict)
            and item.get("notice_id") == notice["notice_id"]
            for item in history
        ):
            history.append(notice)
        updated["procedural_notice_history"] = history
        if is_latest:
            updated["latest_procedural_notice"] = notice
            updated["stage"] = stage
            updated["observed_at"] = notice["observed_at"]
            updated["evidence_url"] = notice["evidence_url"]
            if stage in {"CANCELLATION", "EXAM_CONVOCATION", "RESULTS_PUBLICATION"}:
                updated["status"] = "CLOSED_ANNOUNCEMENT"
                updated["verification_status"] = "closed"
                existing.entity_type = "INDIVIDUAL_VACANCY"
                existing.entity_state = "VACANCY_CLOSED"
                existing.entity_reason_code = (
                    "official_cancellation_notice"
                    if stage == "CANCELLATION" else "official_application_stage_ended"
                )
                existing.verification_status = "closed"
                existing.status = "closed"
                existing.application_method = "UNKNOWN"
                existing.application_url = ""
            else:
                updated["status"] = "UNKNOWN"
                updated["verification_status"] = "unverified"
                existing.entity_state = "VACANCY_STATUS_UNKNOWN"
                existing.entity_reason_code = (
                    "official_procedural_notice_requires_reverification"
                )
                existing.verification_status = "unverified"
                existing.status = "verification_required"
                existing.application_method = "UNKNOWN"
                existing.application_url = ""
            existing.last_verified_at = observed_at
            existing.entity_evidence_url = notice["evidence_url"]
        existing.public_competition = updated
    mem.store.record_discovery(
        session,
        kind="PUBLIC_COMPETITION_NOTICE",
        url=notice["evidence_url"],
        title=str(metadata.get("title_original") or opp.title),
        company_name=str(metadata.get("administration_original") or ""),
        source=source_name,
        source_type="government",
        discovery_channel="direct",
        evidence={"notice": notice, "portal_id": portal_id},
        reason=notice["reason_code"],
        canonical_key=f"{source_name}:{portal_id}:{stage}",
    )
    return existing is not None


def _cap_scheduled_queries(
    scheduled: list[list[dict]],
    limit: int,
    plan: list[dict] | None = None,
) -> tuple[list[list[dict]], list[int]]:
    """Cap distinct search-plan combinations and retain their source requests."""
    cap = max(0, int(limit))
    selected = [[] for _ in scheduled]
    combination_order: list[tuple[str, str, str]] = []
    combo_by_key: dict[tuple[str, str, str], dict] = {}
    def combo_key(combo: dict) -> tuple[str, str, str]:
        return (
            str(combo.get("query", "")).casefold(),
            str(combo.get("country") or combo.get("location", "")).casefold(),
            str(combo.get("lang", "")).casefold(),
        )

    for combo in plan or []:
        key = combo_key(combo)
        if key not in combo_by_key:
            combo_by_key[key] = combo
            combination_order.append(key)
    for combos in scheduled:
        for combo in combos:
            key = combo_key(combo)
            if key not in combo_by_key:
                combo_by_key[key] = combo
                combination_order.append(key)
    selected_keys = set(combination_order[:cap])
    for source_index, combos in enumerate(scheduled):
        selected[source_index] = [
            combo for combo in combos if combo_key(combo) in selected_keys
        ]
    deferred = [
        len(combos) - len(selected[index])
        for index, combos in enumerate(scheduled)
    ]
    return selected, deferred


def _source_query_trace(
    source_cfg: dict,
    plan: list[dict],
    allocated: list[dict],
    scheduled: list[dict],
    *,
    max_requests_per_source: int,
    allocation_reason: str,
    country_attempt_limits: dict[str, int] | None = None,
    connector=None,
) -> list[dict]:
    """Describe each applicable source/plan request and its allocation state."""
    plan_limit = max_requests_per_source or len(plan)
    allocated_indices = {
        int(combo["_plan_index"]) for combo in allocated
    }
    scheduled_indices = {
        int(combo["_plan_index"]) for combo in scheduled
    }
    trace = []
    for combo in plan[:plan_limit]:
        plan_index = int(combo["_plan_index"])
        if not _source_applies_to_country(source_cfg, combo):
            continue
        country = str(
            combo.get("country") or combo.get("location") or "",
        )
        country_in_allocation = (
            country_attempt_limits is None
            or country.casefold() in {
                str(value).casefold() for value in country_attempt_limits
            }
        )
        if not country_in_allocation:
            request_status = "deferred"
            reason_code = "country_not_in_allocation"
        elif plan_index in scheduled_indices:
            request_status = "pending"
            reason_code = ""
        elif plan_index in allocated_indices:
            request_status = "deferred"
            reason_code = "search_combination_cap"
        else:
            request_status = "deferred"
            reason_code = allocation_reason or "allocation_deferred"
        cache_check = getattr(connector, "has_cached_result", None)
        cache_available = bool(
            callable(cache_check)
            and cache_check(combo["query"], combo["location"])
        )
        source_available = (
            bool(getattr(connector, "keys", []))
            if source_cfg.get("kind") == "tavily"
            else connector is not None
        )
        trace.append({
            "plan_index": plan_index,
            "query": str(combo.get("query") or ""),
            "intent": str(combo.get("intent") or "unknown"),
            "country": country,
            "language": str(combo.get("lang") or ""),
            "request_status": request_status,
            "reason_code": reason_code,
            "result_state": "",
            "provider_attempts": 0,
            "allocation_eligible": (
                country_in_allocation
                and source_available
                and not cache_available
                and (
                    source_cfg.get("kind") != "tavily"
                    or _remaining_tavily_requests(
                        connector, country,
                    ) > 0
                )
            ),
            "source_available": source_available,
            "cache_available": cache_available,
        })
    for combo in plan[plan_limit:]:
        if _source_applies_to_country(source_cfg, combo):
            trace.append({
                "plan_index": int(combo["_plan_index"]),
                "query": str(combo.get("query") or ""),
                "intent": str(combo.get("intent") or "unknown"),
                "country": str(combo.get("country") or combo.get("location") or ""),
                "language": str(combo.get("lang") or ""),
                "request_status": "deferred",
                "reason_code": "source_request_cap",
                "result_state": "",
                "provider_attempts": 0,
                "allocation_eligible": False,
                "source_available": bool(
                    getattr(connector, "keys", [])
                    if source_cfg.get("kind") == "tavily"
                    else connector is not None
                ),
                "cache_available": False,
            })
    return trace


def _query_outcome_state(
    *,
    origin: str,
    status: str,
    error_code: str,
    deferred_reason: str,
    provider_attempts: int,
) -> tuple[str, str]:
    """Classify a source request without confusing skipped keys with skipped queries."""
    if origin == "cache":
        return "cached", ""
    if provider_attempts > 0 or origin in {"api", "direct"}:
        if status in {"degraded", "unconfigured", "blocked"} or error_code:
            return "failed", error_code
        return "attempted", ""
    reason = error_code or deferred_reason
    if any(
        token in reason
        for token in (
            "budget",
            "ledger",
            "country_allocation_cap_exhausted",
            "country_not_in_attempt_allocation",
        )
    ):
        return "deferred", reason
    if status in {"degraded", "unconfigured", "blocked"} or error_code:
        return "failed", error_code
    return "failed", error_code or "request_outcome_unavailable"


def _resolve_paths(cfg: dict) -> dict:
    opts = dict(cfg)
    p = opts.get("path")
    if p and not Path(p).is_absolute():
        opts["path"] = str(ROOT_DIR / p)
    return opts


async def run_discovery(session: Session, config: AgentConfig, prefs: Preferences,
                        sources_path: Path | None = None,
                        profile: CandidateProfile | None = None,
                        llm=None,
                        *,
                        max_queries_override: int | None = None,
                        max_api_attempts_override: int | None = None,
                        max_search_combinations: int | None = None,
                        country_attempt_limits: dict[str, int] | None = None,
                        budget_reserver=None,
                        budget_exhauster=None) -> DiscoveryReport:
    report = DiscoveryReport()
    discovery_cfg = config.discovery or {}
    max_api_attempts = max(
        0, int(discovery_cfg.get("max_api_requests_per_run", 17)),
    )
    if max_api_attempts_override is not None:
        max_api_attempts = min(max_api_attempts, max(0, int(max_api_attempts_override)))
    api_attempt_budget = ApiAttemptBudget(
        max_api_attempts,
        country_limits=country_attempt_limits,
    )
    max_per_country = int(discovery_cfg.get(
        "max_per_country", config.search_plan.get("max_combinations_per_country", 3)))
    max_queries = int(discovery_cfg.get("max_queries_per_run", 40))
    if max_queries_override is not None:
        requested_cap = max(0, int(max_queries_override))
        max_queries = (
            min(max_queries, requested_cap) if max_queries > 0 else requested_cap
        )
    vocab = CandidateVocabulary(profile=profile, prefs=prefs)
    # Optional LLM vocabulary expansion (discovery.vocab_llm) — cached on disk.
    if llm is not None and discovery_cfg.get("vocab_llm", False):
        await vocab.llm_expanded_roles(llm)
        logger.info("vocab_llm enabled; vocabulary now %d terms", len(vocab.roles()))
    from app.workflows.adaptive_plan import build_adaptive_plan

    if budget_reserver is None and budget_exhauster is None:
        connectors = _load_source_connectors(sources_path)
    else:
        connectors = _load_source_connectors(
            sources_path,
            budget_reserver=budget_reserver,
            budget_exhauster=budget_exhauster,
        )
    for _source_cfg, connector in connectors:
        if callable(getattr(connector, "has_cached_result", None)):
            connector.attempt_budget = api_attempt_budget
    researcher = None
    if discovery_cfg.get("website_research", False):
        from app.discovery.website_researcher import WebsiteResearcher
        researcher = WebsiteResearcher(
            max_pages=int(discovery_cfg.get("website_research_max_pages", 12)),
            per_host_delay=float(discovery_cfg.get("website_research_delay_seconds", 0.25)),
            state_path=ROOT_DIR / "data" / "website_research_state.json",
            session=session,
            refresh_after_days=int(discovery_cfg.get("website_research_refresh_days", 7)),
        )
    plan = [
        {**combo, "_plan_index": index}
        for index, combo in enumerate(build_adaptive_plan(
            session,
            prefs,
            config,
            vocab=vocab,
            profile=profile,
            max_per_country=max_per_country,
            max_queries_per_run=max_queries,
            learning_sources={
                c.get("name", connector.name)
                for c, connector in connectors
            },
        ))
    ]
    report.plan_combinations = [
        {
            "plan_index": int(combo["_plan_index"]),
            "query": str(combo.get("query") or ""),
            "intent": str(combo.get("intent") or "unknown"),
            "country": str(combo.get("country") or combo.get("location") or ""),
            "language": str(combo.get("lang") or ""),
        }
        for combo in plan
    ]

    # Employer + agency universe (§7, §9) — opt-in so the job pipeline stays hermetic.
    if discovery_cfg.get("employer_discovery", False):
        from app.workflows.employer_discovery import run_employer_discovery

        ereport = await run_employer_discovery(session, config, prefs, profile=profile)
        report.employers_discovered = ereport.stored

    if discovery_cfg.get("company_universe_discovery", False):
        from app.workflows.company_universe import run_company_universe_discovery

        company_report = await run_company_universe_discovery(
            session, config, researcher=researcher,
        )
        report.companies_discovered = company_report.stored
        report.company_universe = company_report.as_dict()
        report.source_errors.extend(company_report.errors)

    # Immigration & work-pathway discovery (§11, §13) — opt-in, official web sources.
    if discovery_cfg.get("immigration_discovery", False):
        from app.workflows.immigration_discovery import run_immigration_discovery

        ireport = await run_immigration_discovery(session, config, prefs, profile=profile)
        report.immigration_facts = ireport.stored

    # Search-engine expansion: surface opportunity sources (§6) — opt-in, live web.
    if discovery_cfg.get("opportunity_source_discovery", False):
        from app.workflows.opportunity_sources import run_opportunity_source_discovery

        os_report = await run_opportunity_source_discovery(session, config, prefs, profile=profile)
        report.opportunity_sources = os_report.stored

    # Authorized social signals: LinkedIn (index) + Meta/user-provided (§15-18).
    if discovery_cfg.get("social_signal_discovery", False):
        from app.workflows.social_signals import (
            DEFAULT_SOCIAL_SOURCES_PATH,
            run_social_signal_discovery,
        )

        spath = Path(discovery_cfg.get("social_sources_path")) if discovery_cfg.get(
            "social_sources_path") else DEFAULT_SOCIAL_SOURCES_PATH
        sreport = await run_social_signal_discovery(session, config, prefs,
                                                    sources_path=spath, profile=profile)
        report.social_signals = sreport.stored

    max_requests = int(discovery_cfg.get("max_requests_per_source", 0))
    allocation_deferred_reasons = [{} for _ in connectors]
    allocated_queries, deferred_queries, stale_queries = _allocate_source_queries(
        connectors,
        plan,
        max_requests,
        max_api_attempts=max_api_attempts,
        deferral_reasons=allocation_deferred_reasons,
        country_attempt_limits=country_attempt_limits,
    )
    run_budget_deferred = [0] * len(allocated_queries)
    scheduled_queries = allocated_queries
    if max_search_combinations is not None:
        scheduled_queries, run_budget_deferred = _cap_scheduled_queries(
            scheduled_queries, max_search_combinations, plan,
        )
    attempted_combinations: set[tuple[str, str, str]] = set()
    verify_vacancy_details = bool(
        discovery_cfg.get("verify_vacancy_details", False),
    )
    detail_verifier = VacancyDetailVerifier() if verify_vacancy_details else None
    detail_verifications = 0
    detail_evidence_cache = {}
    reverified_job_ids: set[int] = set()
    seen_competition_notices: set[tuple[str, str, str]] = set()

    def verify_detail(url: str, source_report: dict):
        nonlocal detail_verifications
        if url in detail_evidence_cache:
            source_report["detail_verifications_reused"] += 1
            return detail_evidence_cache[url]
        if detail_verifier is None or detail_verifications >= MAX_DETAIL_VERIFICATIONS_PER_RUN:
            return None
        detail_verifications += 1
        source_report["detail_verification_attempts"] += 1
        evidence = detail_verifier.verify(url)
        detail_evidence_cache[url] = evidence
        coordinates = supported_ats_detail(url)
        source_report.setdefault("verification_evidence", []).append({
            "url": url,
            "ats_vendor": coordinates[0] if coordinates else "unsupported",
            "outcome": evidence.outcome,
            "reason_code": evidence.reason_code,
            "observed_at": evidence.observed_at.isoformat(),
            "evidence_checks": dict(evidence.evidence_checks),
        })
        return evidence

    def record_verification(source_report: dict, evidence, *, existing=False) -> None:
        reason = evidence.reason_code
        reasons = source_report["verification_reason_codes"]
        reasons[reason] = reasons.get(reason, 0) + 1
        if existing:
            source_report["existing_candidates_reverified"] += 1
        else:
            source_report["verification_candidates"] += 1
        outcome_field = {
            "OPEN": "verified",
            "CLOSED": "closed",
            "UNKNOWN": "unknown",
        }.get(evidence.outcome, "unknown")
        field = (
            f"existing_candidates_{outcome_field}" if existing
            else {
                "verified": "verified_individual_vacancies",
                "closed": "closed_vacancies",
                "unknown": "unknown_vacancy_status",
            }[outcome_field]
        )
        source_report[field] += 1

    for source_index, (source_cfg, connector) in enumerate(connectors):
        source_name = source_cfg.get("name", connector.name)
        inapplicable_countries: dict[str, int] = {}
        for combo in plan:
            if not _source_applies_to_country(source_cfg, combo):
                country = str(combo.get("country") or combo.get("location") or "UNKNOWN")
                inapplicable_countries[country] = (
                    inapplicable_countries.get(country, 0) + 1
                )
        allocation_reason = ""
        if allocation_deferred_reasons[source_index]:
            reason = max(
                allocation_deferred_reasons[source_index],
                key=allocation_deferred_reasons[source_index].get,
            )
            allocation_reason = (
                "daily_budget_exhausted"
                if reason == "aggregate_daily_budget"
                else reason
            )
        source_report = {
            "name": source_name,
            "kind": source_cfg.get("kind", getattr(connector, "kind", "?")),
            "mode": source_cfg.get("mode", "real"),
            "request_scope": {
                "applies_to_countries": source_cfg.get(
                    "applies_to_countries",
                    ["*"] if not source_cfg.get("domains") else [],
                ),
                "domains": sorted(source_cfg.get("domains") or []),
                "official_only": bool(source_cfg.get("official_only", False)),
                "request_scope_id": getattr(connector, "request_scope_id", ""),
            },
            "inapplicable_country_queries": inapplicable_countries,
            "queries": 0,
            "fetched": 0,
            "normalized": 0,
            "normalization_rejected": 0,
            "normalization_rejection_reasons": {},
            "duplicates": 0,
            "duplicate_reasons": {"exact": 0, "probable": 0, "same_run": 0},
            "new": 0,
            "verification_candidates": 0,
            "verification_source_unsupported": 0,
            "verified_individual_vacancies": 0,
            "unverified_vacancy_candidates": 0,
            "closed_vacancies": 0,
            "unknown_vacancy_status": 0,
            "research_leads": 0,
            "rejected_non_vacancy": 0,
            "existing_candidates_reverified": 0,
            "existing_candidates_verified": 0,
            "existing_candidates_closed": 0,
            "existing_candidates_unknown": 0,
            "detail_verifications_reused": 0,
            "detail_verification_attempts": 0,
            "verification_reason_codes": {},
            "public_page_requests": 0,
            "public_page_telemetry_by_request_language": {},
            "public_competition_status_counts": {},
            "public_competition_results_by_country": {},
            "public_competition_results_by_language": {},
            "verified_individual_vacancies_by_query": {},
            "verified_individual_vacancies_by_country": {},
            "verified_individual_vacancies_by_language": {},
            "procedural_notices": 0,
            "procedural_notices_matching_existing": 0,
            "procedural_notices_without_matching_vacancy": 0,
            "new_by_entity_type": {},
            "new_by_entity_state": {},
            "provider_results_by_country": {},
            "provider_results_by_language": {},
            "accepted_results_by_country": {},
            "accepted_results_by_language": {},
            "filtered_results_by_country": {},
            "filtered_results_by_language": {},
            "filter_reason_counts": {},
            "filtered_url_examples": [],
            "key_budget_events": {},
            "queries_cached": 0,
            "queries_api": 0,
            "queries_direct": 0,
            "queries_empty": 0,
            "live_empty_responses": 0,
            "cached_empty_results": 0,
            "queries_filtered": 0,
            "queries_failed": 0,
            "queries_budget_deferred": deferred_queries[source_index],
            "queries_deferred_by_reason": allocation_deferred_reasons[source_index],
            "queries_run_budget_deferred": run_budget_deferred[source_index],
            "queries_stale_cache": stale_queries[source_index],
            "query_trace": _source_query_trace(
                source_cfg,
                plan,
                allocated_queries[source_index],
                scheduled_queries[source_index],
                max_requests_per_source=max_requests,
                allocation_reason=allocation_reason,
                country_attempt_limits=country_attempt_limits,
                connector=connector,
            ),
            "verification_evidence": [],
            "api_attempts": 0,
            "api_successes": 0,
            "api_failed_attempts": 0,
            "api_retry_attempts": 0,
            "api_attempts_budget_deferred": 0,
            "query_error_codes": {},
            "queries_by_country": {},
            "queries_by_language": {},
            "provider_attempts_by_country": {},
            "provider_attempts_by_language": {},
            "cache_hits_by_country": {},
            "cache_hits_by_language": {},
            "fetched_api_results": 0,
            "fetched_cache_results": 0,
            "provider_results_returned": 0,
            "provider_results_filtered": 0,
            "cached_provider_results_filtered": 0,
            "error": "",
            "rate_status": "ok",
            "health": "ready",
        }
        source = mem.store.upsert_source(session, source_name,
                                         getattr(connector, "kind", "?"), str(source_cfg.get("path", "")))
        items_found = 0
        attempt_metrics = getattr(connector, "attempt_metrics", None)
        metrics_before = attempt_metrics() if callable(attempt_metrics) else {}
        combos = scheduled_queries[source_index]
        budgeted_tavily = (
            callable(getattr(connector, "has_cached_result", None))
            and bool(getattr(connector, "keys", None))
        )
        parallel = bool(discovery_cfg.get("parallel_fetch", False)) and not budgeted_tavily

        def process(
            combo, results, source_name, source_report, _connector=connector,
        ) -> int:
            """Normalize + dedup + store one combo's results; returns jobs found."""
            nonlocal detail_verifications
            origin = getattr(_connector, "last_origin", "")
            if origin == "api":
                source_report["fetched_api_results"] += len(results)
            elif origin == "cache":
                source_report["fetched_cache_results"] += len(results)
            for o in results:
                raw = dict(o.raw or {})
                raw.update({
                    "search_query": combo["query"],
                    "search_location": combo["location"],
                    "search_country": combo.get("country", ""),
                    "search_language": combo.get("lang", ""),
                    "search_intent": combo.get("intent", ""),
                })
                o.raw = raw
            processed = [normalize(o) for o in results]
            for item in results:
                reason = normalization_rejection_reason(item)
                if reason:
                    reasons = source_report["normalization_rejection_reasons"]
                    reasons[reason] = reasons.get(reason, 0) + 1
            processed = [o for o in processed if o is not None]
            report.opportunities_fetched += len(processed)
            source_report["fetched"] += len(results)
            source_report["normalized"] += len(processed)
            source_report["normalization_rejected"] += len(results) - len(processed)
            dup = find_duplicates(session, processed)
            seen_canonical = set()
            combo_new = 0
            for idx, opp in enumerate(processed):
                competition = _public_competition(opp)
                if competition.get("stage") in {
                    "EXAM_CONVOCATION", "RESULTS_PUBLICATION", "CANCELLATION",
                    "OTHER_PROCEDURAL_NOTICE",
                }:
                    status = str(competition.get("status") or "UNKNOWN")
                    source_report["public_competition_status_counts"][status] = (
                        source_report["public_competition_status_counts"].get(status, 0) + 1
                    )
                    for field, key in (
                        (
                            "public_competition_results_by_country",
                            str(opp.country or "Morocco"),
                        ),
                        (
                            "public_competition_results_by_language",
                            str(opp.language or combo.get("lang") or "unknown"),
                        ),
                    ):
                        values = source_report[field]
                        values[key] = values.get(key, 0) + 1
                    notice_key = (
                        str(competition.get("portal_id") or ""),
                        str(competition.get("stage") or ""),
                        str(competition.get("observed_at") or ""),
                    )
                    if notice_key not in seen_competition_notices:
                        seen_competition_notices.add(notice_key)
                        matched = _persist_public_competition_notice(
                            session, opp, competition, source_name,
                        )
                        source_report["procedural_notices"] += 1
                        field = (
                            "procedural_notices_matching_existing"
                            if matched else "procedural_notices_without_matching_vacancy"
                        )
                        source_report[field] += 1
                    continue
                if idx in dup:
                    report.duplicates += 1
                    source_report["duplicates"] += 1
                    source_report["duplicate_reasons"][dup[idx]] += 1
                    entity_type, _entity_state, _entity_reason = classify_entity(opp)
                    competition = _public_competition(opp)
                    if competition and competition.get("portal_id"):
                        existing_job = session.execute(
                            select(Job).where(
                                Job.source == source_name,
                                Job.external_id == str(competition["portal_id"]),
                            ).limit(1),
                        ).scalar_one_or_none()
                        if (
                            existing_job is not None
                            and existing_job.id not in reverified_job_ids
                        ):
                            reverified_job_ids.add(existing_job.id)
                            status = _apply_public_competition_evidence(
                                session, existing_job, opp,
                            )
                            source_report["existing_candidates_reverified"] += 1
                            outcome = {
                                "OPEN_ANNOUNCEMENT": "existing_candidates_verified",
                                "CLOSED_ANNOUNCEMENT": "existing_candidates_closed",
                            }.get(status, "existing_candidates_unknown")
                            source_report[outcome] += 1
                            reasons = source_report["verification_reason_codes"]
                            reason = existing_job.entity_reason_code
                            reasons[reason] = reasons.get(reason, 0) + 1
                    if entity_type == "INDIVIDUAL_VACANCY":
                        coordinates = supported_ats_detail(opp.url)
                        if coordinates is not None:
                            clauses = [Job.dedup_key == opp.dedup_key()]
                            canonical_id = opp.canonical_job_id()
                            if canonical_id:
                                clauses.append(Job.canonical_job_id == canonical_id)
                            if opp.url:
                                clauses.append(Job.url == opp.url)
                            existing_job = session.execute(
                                select(Job).where(or_(*clauses)).limit(1),
                            ).scalar_one_or_none()
                            if (
                                existing_job is not None
                                and existing_job.id not in reverified_job_ids
                                and not is_verified_individual_vacancy(existing_job)
                            ):
                                evidence = verify_detail(opp.url, source_report)
                                if evidence is not None:
                                    reverified_job_ids.add(existing_job.id)
                                    _apply_vacancy_evidence(
                                        session, existing_job, evidence,
                                    )
                                    record_verification(
                                        source_report, evidence, existing=True,
                                    )
                    continue
                canonical = opp.canonical_job_id()
                if canonical in seen_canonical:  # same vacancy from another source this run
                    report.duplicates += 1
                    source_report["duplicates"] += 1
                    source_report["duplicate_reasons"]["same_run"] += 1
                    continue
                seen_canonical.add(canonical)
                src_type = opp.effective_source_type()
                entity_type, entity_state, entity_reason = classify_entity(opp)
                observed_at = None
                verified_route = ""
                evidence = None
                if competition:
                    observed_at = _competition_observed_at(competition)
                    status = str(competition.get("status") or "UNKNOWN")
                    entity_reason = str(
                        competition.get("reason_code") or "public_competition_unverified",
                    )
                    if status == "OPEN_ANNOUNCEMENT":
                        entity_type = "INDIVIDUAL_VACANCY"
                        entity_state = "VACANCY_VERIFIED"
                        competition["reason_code"] = (
                            "official_public_competition_verified_open"
                        )
                        route = competition.get("submission_route") or {}
                        route_kind = route.get("kind")
                        if route_kind == "ONLINE_PORTAL":
                            verified_route = str(route.get("destination_url") or "")
                            opp.application_method = "COMPANY_PORTAL"
                        elif route_kind == "EMAIL":
                            opp.application_method = "EMAIL"
                        else:
                            opp.application_method = "UNKNOWN"
                        opp.verification_status = "verified"
                        source_report["verified_individual_vacancies"] += 1
                        source_report["verification_candidates"] += 1
                        query = str(combo.get("query") or "unknown")
                        query_counts = source_report[
                            "verified_individual_vacancies_by_query"
                        ]
                        query_counts[query] = query_counts.get(query, 0) + 1
                        for field, key in (
                            (
                                "verified_individual_vacancies_by_country",
                                str(opp.country or "Morocco"),
                            ),
                            (
                                "verified_individual_vacancies_by_language",
                                str(opp.language or combo.get("lang") or "unknown"),
                            ),
                        ):
                            values = source_report[field]
                            values[key] = values.get(key, 0) + 1
                    elif status == "CLOSED_ANNOUNCEMENT":
                        entity_type = "INDIVIDUAL_VACANCY"
                        entity_state = "VACANCY_CLOSED"
                        opp.verification_status = "closed"
                        source_report["closed_vacancies"] += 1
                        source_report["verification_candidates"] += 1
                    else:
                        entity_type = "INDIVIDUAL_VACANCY"
                        entity_state = "VACANCY_STATUS_UNKNOWN"
                        opp.verification_status = "unverified"
                        source_report["unknown_vacancy_status"] += 1
                        source_report["verification_candidates"] += 1
                    competition["observed_at"] = (
                        observed_at.isoformat() if observed_at else ""
                    )
                    reasons = source_report["verification_reason_codes"]
                    reasons[entity_reason] = reasons.get(entity_reason, 0) + 1
                    source_report["public_competition_status_counts"][status] = (
                        source_report["public_competition_status_counts"].get(status, 0) + 1
                    )
                    for field, key in (
                        (
                            "public_competition_results_by_country",
                            str(opp.country or "Morocco"),
                        ),
                        (
                            "public_competition_results_by_language",
                            str(opp.language or combo.get("lang") or "unknown"),
                        ),
                    ):
                        values = source_report[field]
                        values[key] = values.get(key, 0) + 1
                    if competition.get("deadline_at"):
                        opp.closing_at = datetime.fromisoformat(
                            str(competition["deadline_at"]),
                        )
                    if competition.get("posted_at"):
                        opp.posted_at = datetime.fromisoformat(
                            str(competition["posted_at"]),
                        )
                        opp.posted_at_source = "emploi_public:date_publication"
                        opp.posted_at_evidence_url = opp.url
                elif entity_type == "INDIVIDUAL_VACANCY":
                    ats_detail = supported_ats_detail(opp.url)
                    if verify_vacancy_details and ats_detail is not None:
                        evidence = verify_detail(opp.url, source_report)
                    else:
                        evidence = None
                    if evidence is not None:
                        observed_at = evidence.observed_at
                        entity_reason = evidence.reason_code
                        if evidence.title:
                            opp.title = evidence.title
                        if evidence.employer:
                            opp.company = evidence.employer
                        if evidence.location:
                            opp.location = evidence.location
                        if evidence.country:
                            opp.country = evidence.country
                        if evidence.description:
                            opp.description = evidence.description
                        opp.posted_at = evidence.posted_at
                        opp.posted_at_source = evidence.posted_at_source
                        opp.posted_at_evidence_url = (
                            evidence.evidence_url
                            if evidence.posted_at is not None else ""
                        )
                        opp.closing_at = evidence.closing_at
                        if evidence.outcome == "OPEN":
                            entity_state = "VACANCY_VERIFIED"
                            opp.country = evidence.country or opp.country or "UNKNOWN"
                            opp.verification_status = "verified"
                            verified_route = evidence.application_url
                        elif evidence.outcome == "CLOSED":
                            entity_state = "VACANCY_CLOSED"
                            opp.verification_status = "closed"
                            opp.closing_at = evidence.closing_at
                        else:
                            entity_state = "VACANCY_STATUS_UNKNOWN"
                            opp.verification_status = "unverified"
                    else:
                        entity_state = "VACANCY_UNVERIFIED"
                        if verify_vacancy_details and ats_detail is None:
                            source_report["verification_source_unsupported"] += 1
                        entity_reason = (
                            "detail_verification_disabled"
                            if not verify_vacancy_details
                            else "detail_verification_limit_reached"
                            if ats_detail is not None
                            else "unsupported_detail_verification_source"
                        )
                        opp.verification_status = "unverified"
                    if evidence is not None:
                        record_verification(source_report, evidence)
                    elif entity_state == "VACANCY_UNVERIFIED":
                        source_report["unverified_vacancy_candidates"] += 1
                        reasons = source_report["verification_reason_codes"]
                        reasons[entity_reason] = reasons.get(entity_reason, 0) + 1
                    elif entity_state == "VACANCY_STATUS_UNKNOWN":
                        source_report["unknown_vacancy_status"] += 1
                elif entity_state == "RESEARCH_REQUIRED":
                    source_report["research_leads"] += 1
                elif entity_state == "REJECTED_NON_VACANCY":
                    source_report["rejected_non_vacancy"] += 1
                source_report["new_by_entity_type"][entity_type] = (
                    source_report["new_by_entity_type"].get(entity_type, 0) + 1
                )
                source_report["new_by_entity_state"][entity_state] = (
                    source_report["new_by_entity_state"].get(entity_state, 0) + 1
                )
                if (
                    src_type == "ats"
                    and opp.external_id == opp.url
                    and not verified_route
                    and not (evidence and evidence.employer)
                ):
                    # A slug parsed from a search-result URL is not employer evidence.
                    opp.company = ""
                qmap = discovery_cfg.get("source_quality") or {}
                quality = int(qmap.get(src_type, opp.effective_quality())) if qmap else opp.effective_quality()
                company, company_urls = _upsert_opportunity_company(
                    session, opp, src_type
                )
                raw_channel = (opp.raw or {}).get("channel", "")
                kind = "RECRUITMENT_POST" if src_type == "recruitment_post" else "JOB"
                if src_type in {"company_career", "ats"}:
                    kind = "CAREER_PAGE" if src_type == "company_career" else "JOB"
                mem.store.record_discovery(
                    session, kind=kind, url=opp.url, title=opp.title,
                    company_name=opp.company or "", company_id=company.id,
                    source=opp.source, source_type=src_type,
                    discovery_channel=raw_channel or ("indexed" if "search" in source_name else "direct"),
                    evidence={"description": opp.description[:4000], "query": combo["query"],
                              "location": opp.location,
                              "search_location": combo["location"],
                              "company_url_kind": company_urls["kind"],
                              "company_website": company_urls["website"],
                              "careers_url": company_urls["careers_url"],
                              "recruitment_url": company_urls["recruitment_url"],
                              "company_profile_url": company_urls["profile_url"],
                              "source_url": opp.url,
                              "public_competition": competition},
                    reason="discovered opportunity", relevance_score=quality,
                )
                job_data = {
                    "source": opp.source,
                    "external_id": opp.external_id,
                    "dedup_key": opp.dedup_key(),
                    "title": opp.title,
                    "company_id": company.id,
                    "location": opp.location,
                    "country": opp.country,
                    "description": opp.description,
                    "url": opp.url,
                    "posted_at": opp.posted_at,
                    "posted_at_source": opp.posted_at_source,
                    "posted_at_evidence_url": opp.posted_at_evidence_url,
                    "employment_type": opp.employment_type or "full_time",
                    "salary": opp.salary or "",
                    "contact_email": opp.contact_email or "",
                    "status": (
                        "closed" if entity_state == "VACANCY_CLOSED" else "new"
                    ),
                    "source_type": src_type,
                    "source_quality": quality,
                    "source_confidence": quality,
                    "closing_at": opp.closing_at,
                    "language": opp.language or combo.get("lang", ""),
                    "sponsorship_signal": opp.sponsorship_signal,
                    "international_candidate_signal": opp.international_candidate_signal,
                    "relocation_signal": opp.relocation_signal,
                    "work_permit_signal": opp.work_permit_signal,
                    "verification_status": opp.verification_status,
                    "last_verified_at": observed_at,
                    "search_query": combo["query"],
                    "search_language": combo.get("lang", ""),
                    "search_country": combo.get("country", opp.country),
                    "canonical_job_id": canonical,
                    "freshness": freshness_label(opp.posted_at),
                    "opportunity_type": (
                        classify_opportunity(opp.title, opp.description, src_type)
                        if entity_state == "VACANCY_VERIFIED" else "UNKNOWN"
                    ),
                    "application_method": (
                        str(getattr(opp, "application_method", "UNKNOWN"))
                        if competition else (
                            "COMPANY_PORTAL" if verified_route else "UNKNOWN"
                        )
                    ),
                    "application_url": verified_route,
                    "entity_type": entity_type,
                    "entity_state": entity_state,
                    "entity_reason_code": entity_reason,
                    "entity_evidence_url": opp.url,
                    "public_competition": competition,
                }
                job, created = mem.store.upsert_job(session, job_data)
                if created:
                    job.last_verified_at = observed_at
                    if (
                        researcher is not None
                        and src_type == "company_career"
                        and entity_state == "VACANCY_VERIFIED"
                        and opp.url
                    ):
                        researcher.research_and_apply(
                            opp.url, company_id=company.id, job_id=job.id,
                            official_domain=company.official_domain,
                        )
                        company.last_researched_at = datetime.now(timezone.utc)
                    if competition:
                        job.contact_email = ""
                    else:
                        verification = EmailVerificationService(session).verify_job(job, source_type=src_type)
                        # Never copy an unverified string into a job as a sendable recipient.
                        job.contact_email = verification.email if verification.verified else ""
                    if entity_state == "VACANCY_VERIFIED" and verified_route:
                        job.application_method = "COMPANY_PORTAL"
                        job.application_url = verified_route
                    else:
                        job.application_method = "UNKNOWN"
                        job.application_url = ""
                    mem.store.enqueue_notification(session, "JOB_FOUND", job_id=job.id,
                                                   payload={"opportunity_id": mem.store.opportunity_id(job)})
                    report.new_opportunity_origins[job.id] = origin or "unknown"
                    report.new_jobs += 1
                    combo_new += 1
                    source_report["new"] += 1
                elif competition:
                    status = _apply_public_competition_evidence(
                        session, job, opp,
                    )
                    source_report["existing_candidates_reverified"] += 1
                    outcome = {
                        "OPEN_ANNOUNCEMENT": "existing_candidates_verified",
                        "CLOSED_ANNOUNCEMENT": "existing_candidates_closed",
                    }.get(status, "existing_candidates_unknown")
                    source_report[outcome] += 1
                    reasons = source_report["verification_reason_codes"]
                    reason = job.entity_reason_code
                    reasons[reason] = reasons.get(reason, 0) + 1
            mem.store.record_query(session, combo["query"], combo.get("country", ""),
                                   source_name, jobs_found=len(processed),
                                   relevant_jobs=combo_new)
            return len(processed)

        async def search(combo, _connector=connector, _source_cfg=source_cfg):
            if (
                country_attempt_limits is not None
                and _source_cfg.get("kind") == "tavily"
            ):
                return await _connector.search(
                    combo["query"],
                    combo["location"],
                    country=str(
                        combo.get("country") or combo.get("location") or "",
                    ),
                )
            return await _connector.search(combo["query"], combo["location"])

        def record_query_coverage(combo, _source_report=source_report) -> None:
            attempted_combinations.add((
                str(combo.get("query", "")).casefold(),
                str(combo.get("country") or combo.get("location", "")).casefold(),
                str(combo.get("lang", "")).casefold(),
            ))
            country_counts = _source_report["queries_by_country"]
            country = str(combo.get("country", ""))
            country_counts[country] = country_counts.get(country, 0) + 1
            language_counts = _source_report["queries_by_language"]
            language = str(combo.get("lang", "unknown"))
            language_counts[language] = language_counts.get(language, 0) + 1

        def update_query_trace(
            combo,
            *,
            request_status: str,
            reason_code: str = "",
            result_state: str = "",
            provider_attempts: int = 0,
            _source_report=source_report,
        ) -> None:
            plan_index = int(combo.get("_plan_index", -1))
            for trace in _source_report["query_trace"]:
                if trace["plan_index"] == plan_index:
                    trace["request_status"] = request_status
                    trace["reason_code"] = reason_code
                    trace["result_state"] = result_state
                    trace["provider_attempts"] = max(0, int(provider_attempts))
                    return

        def record_query_outcome(
            results, combo, _connector=connector, source=source_report,
        ):
            origin = getattr(_connector, "last_origin", "")
            status = getattr(_connector, "last_status", "")
            error_code = getattr(_connector, "last_error_code", "")
            deferred_reason = getattr(_connector, "last_deferred_reason", "")
            source["public_page_requests"] += int(
                getattr(_connector, "last_page_requests", 0),
            )
            country = str(combo.get("country") or combo.get("location") or "UNKNOWN")
            language = str(combo.get("lang") or "unknown")
            portal_metrics = getattr(_connector, "last_request_metrics", None)
            if isinstance(portal_metrics, dict):
                by_language = source.setdefault(
                    "public_page_telemetry_by_request_language", {},
                )
                language_metrics = by_language.setdefault(language, {})
                for metric, count in portal_metrics.items():
                    if (
                        isinstance(metric, str)
                        and metric.replace("_", "").isalnum()
                        and isinstance(count, int)
                        and not isinstance(count, bool)
                        and count >= 0
                    ):
                        language_metrics[metric] = (
                            language_metrics.get(metric, 0) + count
                        )
            page_requests = int(getattr(_connector, "last_page_requests", 0))
            if page_requests:
                requests_by_language = source.setdefault(
                    "public_page_requests_by_language", {},
                )
                requests_by_language[language] = (
                    requests_by_language.get(language, 0) + page_requests
                )

            def add_count(field: str, key: str, count: int) -> None:
                if count <= 0:
                    return
                values = source[field]
                values[key] = values.get(key, 0) + count

            if origin == "cache":
                source["queries_cached"] += 1
                if status == "filtered":
                    source["queries_filtered"] += 1
                    source["cached_provider_results_filtered"] += int(
                        getattr(_connector, "last_provider_result_count", 0),
                    )
                for value, key in (
                    (country, "cache_hits_by_country"),
                    (language, "cache_hits_by_language"),
                ):
                    counts = source[key]
                    counts[value] = counts.get(value, 0) + 1
                add_count(
                    "provider_results_by_country", country,
                    int(getattr(_connector, "last_provider_result_count", 0)),
                )
                add_count(
                    "provider_results_by_language", language,
                    int(getattr(_connector, "last_provider_result_count", 0)),
                )
                accepted = int(getattr(
                    _connector, "last_accepted_result_count", len(results),
                ))
                filtered = max(
                    0,
                    int(getattr(_connector, "last_provider_result_count", len(results)))
                    - accepted,
                )
                add_count("accepted_results_by_country", country, accepted)
                add_count("accepted_results_by_language", language, accepted)
                add_count("filtered_results_by_country", country, filtered)
                add_count("filtered_results_by_language", language, filtered)
            elif origin == "api":
                source["queries_api"] += 1
                provider_result_count = int(
                    getattr(_connector, "last_provider_result_count", len(results)),
                )
                accepted_result_count = int(
                    getattr(_connector, "last_accepted_result_count", len(results)),
                )
                source["provider_results_returned"] += provider_result_count
                filtered_count = max(0, provider_result_count - accepted_result_count)
                source["provider_results_filtered"] += filtered_count
                add_count("provider_results_by_country", country, provider_result_count)
                add_count("provider_results_by_language", language, provider_result_count)
                add_count("accepted_results_by_country", country, accepted_result_count)
                add_count("accepted_results_by_language", language, accepted_result_count)
                add_count("filtered_results_by_country", country, filtered_count)
                add_count("filtered_results_by_language", language, filtered_count)
            elif origin == "direct":
                source["queries_direct"] += 1
            filter_reasons = getattr(
                _connector, "last_filter_reason_counts", {},
            )
            filtered_total = max(
                0,
                int(getattr(_connector, "last_provider_result_count", len(results)))
                - int(getattr(
                    _connector, "last_accepted_result_count", len(results),
                )),
            )
            known_filtered = sum(
                int(count) for count in filter_reasons.values()
            )
            if known_filtered < filtered_total:
                filter_reasons = dict(filter_reasons)
                filter_reasons["filter_reason_unavailable_from_legacy_cache"] = (
                    filtered_total - known_filtered
                )
            for reason, count in filter_reasons.items():
                source["filter_reason_counts"][reason] = (
                    source["filter_reason_counts"].get(reason, 0) + int(count)
                )
            for example in getattr(_connector, "last_filter_examples", []):
                safe_example = {
                    key: str(example.get(key, ""))[:180]
                    for key in ("reason_code", "host", "path_pattern")
                }
                if (
                    safe_example not in source["filtered_url_examples"]
                    and len(source["filtered_url_examples"]) < 12
                ):
                    source["filtered_url_examples"].append(safe_example)
            for event in getattr(_connector, "last_key_budget_events", []):
                fingerprint = str(event.get("fingerprint", ""))[:8]
                reason = str(event.get("reason_code", "unknown"))[:64]
                events = source["key_budget_events"]
                key_counts = events.setdefault(fingerprint, {})
                key_counts[reason] = key_counts.get(reason, 0) + 1
            provider_attempts = int(
                getattr(_connector, "last_call_attempts", 0),
            )
            trace_status, trace_reason = _query_outcome_state(
                origin=origin,
                status=status,
                error_code=error_code,
                deferred_reason=deferred_reason,
                provider_attempts=provider_attempts,
            )
            runtime_defer_reason = (
                trace_reason if trace_status == "deferred" else ""
            )
            if runtime_defer_reason and (
                "budget" in runtime_defer_reason
                or "ledger" in runtime_defer_reason
                or "allocation" in runtime_defer_reason
            ):
                deferred_reasons = source["queries_deferred_by_reason"]
                deferred_reasons[runtime_defer_reason] = (
                    deferred_reasons.get(runtime_defer_reason, 0) + 1
                )
                if status == "empty":
                    source["live_empty_responses"] += 1
                if status == "filtered":
                    source["queries_filtered"] += 1
            if provider_attempts:
                for value, key in (
                    (country, "provider_attempts_by_country"),
                    (language, "provider_attempts_by_language"),
                ):
                    counts = source[key]
                    counts[value] = counts.get(value, 0) + provider_attempts
            if not results and status == "empty":
                source["queries_empty"] += 1
                if origin == "cache":
                    source["cached_empty_results"] += 1
            if status in {"degraded", "unconfigured", "blocked"}:
                if trace_status == "deferred" and provider_attempts == 0:
                    source["queries_budget_deferred"] += 1
                else:
                    source["queries_failed"] += 1
            if (
                deferred_reason
                and deferred_reason != "budget_ledger_unavailable"
            ):
                source["api_attempts_budget_deferred"] += 1
            if error_code:
                codes = source["query_error_codes"]
                codes[error_code] = codes.get(error_code, 0) + 1
            update_query_trace(
                combo,
                request_status=trace_status,
                reason_code=trace_reason,
                result_state=status or "unknown",
                provider_attempts=int(
                    getattr(_connector, "last_call_attempts", 0),
                ),
            )

        failure = ""
        try:
            if parallel:  # §26 — all searches dispatched at once, writes stay ordered
                source_report["queries"] += len(combos)
                batch = await asyncio.gather(*(search(c) for c in combos), return_exceptions=True)
                for combo, results in zip(combos, batch, strict=True):
                    if isinstance(results, BaseException):
                        raise results  # connector-level isolation below
                    record_query_coverage(combo)
                    record_query_outcome(results, combo)
                    items_found += process(combo, results, source_name, source_report)
            else:
                for combo in combos:
                    if (
                        budgeted_tavily
                        and not connector.has_cached_result(
                            combo["query"], combo["location"],
                        )
                        and _remaining_tavily_requests(
                            connector,
                            str(combo.get("country") or combo.get("location") or ""),
                        ) <= 0
                    ):
                        ledger_unavailable = any(
                            getattr(getattr(key, "budget", None), "_ledger_error", False)
                            for key in getattr(connector, "keys", [])
                        )
                        if ledger_unavailable:
                            source_report["queries_failed"] += 1
                            codes = source_report["query_error_codes"]
                            codes["budget_ledger_unavailable"] = (
                                codes.get("budget_ledger_unavailable", 0) + 1
                            )
                            source_report["health"] = "degraded"
                        else:
                            source_report["queries_budget_deferred"] += 1
                            source_report["rate_status"] = "limited"
                            attempt_budget = getattr(
                                connector, "attempt_budget", None,
                            )
                            country = str(
                                combo.get("country") or combo.get("location") or "",
                            )
                            if (
                                country_attempt_limits is not None
                                and attempt_budget is not None
                                and attempt_budget.remaining(country) <= 0
                                and attempt_budget.remaining() > 0
                            ):
                                reason = "country_allocation_cap_exhausted"
                            elif attempt_budget is not None and attempt_budget.remaining() <= 0:
                                reason = "run_attempt_cap"
                            else:
                                reason = "daily_budget_exhausted"
                            deferred_reasons = source_report[
                                "queries_deferred_by_reason"
                            ]
                            deferred_reasons[reason] = (
                                deferred_reasons.get(reason, 0) + 1
                            )
                            update_query_trace(
                                combo,
                                request_status="deferred",
                                reason_code=reason,
                            )
                        continue
                    source_report["queries"] += 1
                    record_query_coverage(combo)
                    results = await search(combo)
                    record_query_outcome(results, combo)
                    items_found += process(combo, results, source_name, source_report)
        except Exception as exc:  # connector-level isolation
            for trace in source_report["query_trace"]:
                if trace["request_status"] == "pending":
                    trace["request_status"] = "failed"
                    trace["reason_code"] = "connector_exception"
            if not source_report["error"]:
                source_report["queries_failed"] += 1
            logger.exception("Discovery connector %s failed", source_name)
            report.source_errors.append(f"{source_name}: {exc}")
            mem.store.record_event(session, "discovery", f"connector failed: {exc}",
                                   "error", {"source": source_name})
            failure = f"{source_name}: {exc}"
            source_report["error"] = str(exc)
            if "429" in str(exc) or "rate limit" in str(exc).lower():
                source_report["rate_status"] = "limited"
        finally:
            if callable(attempt_metrics):
                metrics_after = attempt_metrics()
                source_report["api_attempts"] = (
                    metrics_after.get("attempts", 0)
                    - metrics_before.get("attempts", 0)
                )
                source_report["api_successes"] = (
                    metrics_after.get("successes", 0)
                    - metrics_before.get("successes", 0)
                )
                source_report["api_failed_attempts"] = (
                    metrics_after.get("failed_attempts", 0)
                    - metrics_before.get("failed_attempts", 0)
                )
                source_report["api_retry_attempts"] = (
                    metrics_after.get("retry_attempts", 0)
                    - metrics_before.get("retry_attempts", 0)
                )
            if failure:
                health = "failed"
            elif source_report["queries_failed"]:
                health = "degraded"
            elif source_report["queries_budget_deferred"]:
                health = (
                    "budget_deferred"
                    if source_report["queries"] == 0 and source_report["fetched"] == 0
                    else "partial_budget"
                )
            elif not source_report["fetched"]:
                health = (
                    "empty"
                    if source_report["queries_empty"] or source_report["queries"]
                    else "not_run"
                )
            else:
                health = "ok"
            source_report["health"] = health or "ok"
            source_report["result_state"] = (
                "results_received"
                if source_report["fetched"]
                else "provider_results_filtered"
                if source_report["provider_results_returned"]
                else "cached_results_filtered"
                if source_report["cached_provider_results_filtered"]
                else "successful_empty_response"
                if source_report["live_empty_responses"]
                else "cached_empty_result"
                if source_report["cached_empty_results"]
                else "successful_empty_response"
                if source_report["queries_empty"] and not source_report["queries_failed"]
                else "budget_deferred"
                if source_report["queries"] == 0
                and (
                    source_report["queries_budget_deferred"]
                    or source_report["queries_run_budget_deferred"]
                )
                else "not_attempted"
                if source_report["queries"] == 0
                else "failed_or_unresolved"
            )
            mem.store.mark_source_fetched(session, source, items_found, error=failure)
            report.source_reports.append(source_report)

    raw_results = sum(item["fetched"] for item in report.source_reports)
    report.combinations_attempted = len(attempted_combinations)
    report.source_requests_attempted = sum(
        item["queries"] for item in report.source_reports
    )
    normalization_rejected = sum(
        item["normalization_rejected"] for item in report.source_reports
    )
    failed_queries = sum(item["queries_failed"] for item in report.source_reports)
    empty_queries = sum(item["queries_empty"] for item in report.source_reports)
    total_queries = sum(item["queries"] for item in report.source_reports)
    budget_deferred_total = sum(
        item["queries_budget_deferred"] for item in report.source_reports
    )
    if connectors and raw_results == 0 and total_queries == 0 and budget_deferred_total:
        report.alerts.append(
            f"job search did not run: {budget_deferred_total} uncached searches "
            "were deferred by request budgets"
        )
        mem.store.record_event(session, "discovery", report.alerts[-1], "warning", {})
    elif connectors and raw_results == 0:
        report.alerts.append(
            f"job search returned no results after {total_queries} source queries: "
            f"{failed_queries} failed, {empty_queries} successful empty; "
            f"{budget_deferred_total} budget-deferred"
        )
        mem.store.record_event(session, "discovery", report.alerts[-1], "warning", {})
    elif raw_results and report.opportunities_fetched == 0:
        report.alerts.append(
            f"normalization rejected all {normalization_rejected} provider results"
        )
        mem.store.record_event(session, "discovery", report.alerts[-1], "warning", {})
    if report.new_jobs == 0 and report.duplicates:
        report.alerts.append(
            f"no new jobs: {report.duplicates} fetched opportunities were deduplicated; "
            "inspect per-source duplicate reasons and freshness"
        )
    report.api_request_budget = api_attempt_budget.as_dict()
    if report.api_request_budget["attempts"] >= report.api_request_budget["limit"]:
        logger.info(
            "Tavily per-run request cap reached after %d attempts",
            report.api_request_budget["attempts"],
        )
    if budget_deferred_total:
        report.alerts.append(
            f"Tavily request budget deferred {budget_deferred_total} uncached searches; "
            "see per-source budget and health fields"
        )
    mem.store.record_event(session, "discovery",
                           f"found {report.new_jobs} new jobs ({report.opportunities_fetched} fetched, "
                           f"{report.duplicates} dupes)", "info", {"new": report.new_jobs})
    return report
