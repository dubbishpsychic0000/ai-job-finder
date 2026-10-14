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
from sqlalchemy.orm import Session

from app import memory as mem
from app.config import ROOT_DIR, AgentConfig, CandidateProfile, Preferences, load_yaml
from app.connectors import registry
from app.connectors.tavily_resilience import (
    GENERAL_TAVILY_DAILY_LIMIT,
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
    detect_application_method,
)
from app.discovery.verification import freshness_label
from app.discovery.vocabulary import CandidateVocabulary
from app.discovery.website_researcher import host_domain, normalize_url
from app.models import Company
from app.normalization import normalize

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
                limit=GENERAL_TAVILY_DAILY_LIMIT,
                durable_reserver=budget_reserver,
                durable_exhauster=budget_exhauster,
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
    available = sum(budget.remaining() for budget in _tavily_budgets(connectors))
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
    return scheduled, deferred_by_source, stale_by_source


def _remaining_tavily_requests(connector) -> int:
    budgets = {}
    for key in getattr(connector, "keys", []):
        budget = getattr(key, "budget", None)
        if budget is not None:
            budgets[getattr(budget, "identity", id(budget))] = budget
    remaining = sum(budget.remaining() for budget in budgets.values())
    attempt_budget = getattr(connector, "attempt_budget", None)
    if attempt_budget is not None:
        remaining = min(remaining, attempt_budget.remaining())
    return remaining


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
                        budget_reserver=None,
                        budget_exhauster=None) -> DiscoveryReport:
    report = DiscoveryReport()
    discovery_cfg = config.discovery or {}
    max_api_attempts = max(
        0, int(discovery_cfg.get("max_api_requests_per_run", 17)),
    )
    if max_api_attempts_override is not None:
        max_api_attempts = min(max_api_attempts, max(0, int(max_api_attempts_override)))
    api_attempt_budget = ApiAttemptBudget(max_api_attempts)
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
    plan = build_adaptive_plan(session, prefs, config, vocab=vocab, profile=profile,
                               max_per_country=max_per_country,
                               max_queries_per_run=max_queries,
                               learning_sources={c.get("name", connector.name)
                                                 for c, connector in connectors})

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
    scheduled_queries, deferred_queries, stale_queries = _allocate_source_queries(
        connectors, plan, max_requests, max_api_attempts=max_api_attempts,
    )
    run_budget_deferred = [0] * len(scheduled_queries)
    if max_search_combinations is not None:
        scheduled_queries, run_budget_deferred = _cap_scheduled_queries(
            scheduled_queries, max_search_combinations, plan,
        )
    attempted_combinations: set[tuple[str, str, str]] = set()
    for source_index, (source_cfg, connector) in enumerate(connectors):
        source_name = source_cfg.get("name", connector.name)
        inapplicable_countries: dict[str, int] = {}
        for combo in plan:
            if not _source_applies_to_country(source_cfg, combo):
                country = str(combo.get("country") or combo.get("location") or "UNKNOWN")
                inapplicable_countries[country] = (
                    inapplicable_countries.get(country, 0) + 1
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
            "duplicates": 0,
            "duplicate_reasons": {"exact": 0, "probable": 0, "same_run": 0},
            "new": 0,
            "queries_cached": 0,
            "queries_api": 0,
            "queries_empty": 0,
            "live_empty_responses": 0,
            "cached_empty_results": 0,
            "queries_filtered": 0,
            "queries_failed": 0,
            "queries_budget_deferred": deferred_queries[source_index],
            "queries_run_budget_deferred": run_budget_deferred[source_index],
            "queries_stale_cache": stale_queries[source_index],
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
            processed = [o for o in processed if o is not None]
            report.opportunities_fetched += len(processed)
            source_report["fetched"] += len(results)
            source_report["normalized"] += len(processed)
            source_report["normalization_rejected"] += len(results) - len(processed)
            dup = find_duplicates(session, processed)
            seen_canonical = set()
            combo_new = 0
            for idx, opp in enumerate(processed):
                if idx in dup:
                    report.duplicates += 1
                    source_report["duplicates"] += 1
                    source_report["duplicate_reasons"][dup[idx]] += 1
                    continue
                canonical = opp.canonical_job_id()
                if canonical in seen_canonical:  # same vacancy from another source this run
                    report.duplicates += 1
                    source_report["duplicates"] += 1
                    source_report["duplicate_reasons"]["same_run"] += 1
                    continue
                seen_canonical.add(canonical)
                src_type = opp.effective_source_type()
                qmap = discovery_cfg.get("source_quality") or {}
                quality = int(qmap.get(src_type, opp.effective_quality())) if qmap else opp.effective_quality()
                company, company_urls = _upsert_opportunity_company(
                    session, opp, src_type
                )
                entity_type, entity_state, entity_reason = classify_entity(opp)
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
                              "source_url": opp.url},
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
                    "status": "new",
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
                    "search_query": combo["query"],
                    "search_language": combo.get("lang", ""),
                    "search_country": combo.get("country", opp.country),
                    "canonical_job_id": canonical,
                    "freshness": freshness_label(opp.posted_at),
                    "opportunity_type": classify_opportunity(opp.title, opp.description, src_type),
                    "application_method": "UNKNOWN",
                    "application_url": "",
                    "entity_type": entity_type,
                    "entity_state": entity_state,
                    "entity_reason_code": entity_reason,
                    "entity_evidence_url": opp.url,
                }
                job, created = mem.store.upsert_job(session, job_data)
                if created:
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
                    verification = EmailVerificationService(session).verify_job(job, source_type=src_type)
                    # Never copy an unverified string into a job as a sendable recipient.
                    job.contact_email = verification.email if verification.verified else ""
                    method, application_url = detect_application_method(
                        text=f"{job.title}\n{job.description}", url=job.url, source_type=src_type,
                        has_verified_email=verification.verified)
                    job.application_method, job.application_url = method, application_url
                    mem.store.enqueue_notification(session, "JOB_FOUND", job_id=job.id,
                                                   payload={"opportunity_id": mem.store.opportunity_id(job)})
                    report.new_opportunity_origins[job.id] = origin or "unknown"
                    report.new_jobs += 1
                    combo_new += 1
                    source_report["new"] += 1
            mem.store.record_query(session, combo["query"], combo.get("country", ""),
                                   source_name, jobs_found=len(processed),
                                   relevant_jobs=combo_new)
            return len(processed)

        async def search(combo, _connector=connector):
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

        def record_query_outcome(
            results, combo, _connector=connector, source=source_report,
        ):
            origin = getattr(_connector, "last_origin", "")
            status = getattr(_connector, "last_status", "")
            error_code = getattr(_connector, "last_error_code", "")
            deferred_reason = getattr(_connector, "last_deferred_reason", "")
            country = str(combo.get("country") or combo.get("location") or "UNKNOWN")
            language = str(combo.get("lang") or "unknown")
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
            elif origin == "api":
                source["queries_api"] += 1
                provider_result_count = int(
                    getattr(_connector, "last_provider_result_count", len(results)),
                )
                accepted_result_count = int(
                    getattr(_connector, "last_accepted_result_count", len(results)),
                )
                source["provider_results_returned"] += provider_result_count
                source["provider_results_filtered"] += max(
                    0, provider_result_count - accepted_result_count,
                )
                if status == "empty":
                    source["live_empty_responses"] += 1
                if status == "filtered":
                    source["queries_filtered"] += 1
            provider_attempts = int(getattr(_connector, "last_call_attempts", 0))
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
                if error_code in {
                    "daily_request_budget_exhausted",
                    "run_api_attempt_budget_exhausted",
                } and not getattr(_connector, "last_call_attempts", 0):
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
                        and _remaining_tavily_requests(connector) <= 0
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
                        continue
                    source_report["queries"] += 1
                    record_query_coverage(combo)
                    results = await search(combo)
                    record_query_outcome(results, combo)
                    items_found += process(combo, results, source_name, source_report)
        except Exception as exc:  # connector-level isolation
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
