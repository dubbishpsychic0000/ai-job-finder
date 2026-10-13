"""Bounded, sanitized production discovery without analysis or outbound actions."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from app.config import AgentConfig, CandidateProfile, Preferences, load_yaml
from app.workflows.discovery import DEFAULT_SOURCES_PATH, DiscoveryReport, run_discovery

REPORT_SCHEMA_VERSION = 1
MAX_SEARCH_COMBINATIONS = 12
MAX_TAVILY_ATTEMPTS = 8

_QUALIFICATION_PATTERNS = (
    r".{0,45}\b(?:minimum|min\.?|at least|required|must have|exigé|exigée|requis|requise)\b.{0,90}",
    r".{0,35}\b\d{1,2}\+?\s*(?:years?|ans?|jahre|años|anos)\b.{0,55}",
    r".{0,45}\b(?:bachelor|master|diploma|degree|diplôme|diploma|diplom|licence)\b.{0,70}",
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ().-]{7,}\d)(?!\w)")
async def run_bounded_discovery(
    session,
    config: AgentConfig,
    preferences: Preferences,
    profile: CandidateProfile,
) -> DiscoveryReport:
    """Run configured listing connectors with strict task-specific bounds."""
    source_configuration = load_yaml(DEFAULT_SOURCES_PATH)
    if any(
        source.get("enabled", True) and source.get("kind") != "tavily"
        for source in source_configuration.get("connectors", [])
    ):
        raise RuntimeError("unsupported_connector_enabled_for_bounded_discovery")
    discovery = dict(config.discovery or {})
    for key in (
        "employer_discovery",
        "company_universe_discovery",
        "immigration_discovery",
        "opportunity_source_discovery",
        "social_signal_discovery",
        "website_research",
        "parallel_fetch",
        "vocab_llm",
    ):
        discovery[key] = False
    bounded_config = config.model_copy(update={"discovery": discovery})
    return await run_discovery(
        session,
        bounded_config,
        preferences,
        profile=profile,
        llm=None,
        max_queries_override=MAX_SEARCH_COMBINATIONS,
        max_api_attempts_override=MAX_TAVILY_ATTEMPTS,
        max_total_source_queries=MAX_SEARCH_COMBINATIONS,
    )


def validate_budget_ledgers(data_dir, keys: list[str]) -> tuple[bool, int]:
    """Require the restored per-key ledger before allowing any API attempt."""
    import json
    from pathlib import Path

    from app.connectors.tavily_resilience import key_fingerprint

    root = Path(data_dir)
    seen: set[str] = set()
    for key in keys:
        fingerprint = key_fingerprint(key)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        ledger = root / f"tavily_budget_{fingerprint}.json"
        if not ledger.is_file():
            return False, 0
        try:
            payload = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, 0
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("date"), str)
            or not isinstance(payload.get("count"), int)
            or isinstance(payload.get("count"), bool)
            or payload["count"] < 0
            or not isinstance(payload.get("exhausted"), bool)
        ):
            return False, 0
    return True, len(keys)


def daily_budget_summary(data_dir, keys: list[str]) -> dict:
    from pathlib import Path

    from app.connectors.tavily_resilience import (
        GENERAL_TAVILY_DAILY_LIMIT,
        DailyBudget,
        key_fingerprint,
    )

    unique_keys = list(dict.fromkeys(key_fingerprint(key) for key in keys))
    remaining = [
        DailyBudget(
            Path(data_dir) / f"tavily_budget_{fingerprint}.json",
            limit=GENERAL_TAVILY_DAILY_LIMIT,
        ).remaining()
        for fingerprint in unique_keys
    ]
    return {
        "limit_per_key": GENERAL_TAVILY_DAILY_LIMIT,
        "keys_with_restored_ledgers": len(remaining),
        "remaining_by_key": remaining,
        "aggregate_remaining": sum(remaining),
    }


def blocked_report(reason_code: str) -> dict:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "BLOCKED",
        "reason_code": reason_code,
        "limits": {
            "search_combinations": MAX_SEARCH_COMBINATIONS,
            "tavily_api_attempts": MAX_TAVILY_ATTEMPTS,
        },
        "counts": {
            "external_results_fetched": 0,
            "normalized_results": 0,
            "duplicates": 0,
            "unique_new_opportunities": 0,
            "new_from_live_api": 0,
            "new_from_cache": 0,
        },
        "sources": [],
        "coverage": {
            "new_by_country": {},
            "new_by_search_language": {},
            "new_by_source": {},
            "new_by_opportunity_type": {},
            "date_status_new": {},
            "searched_country_queries": {},
            "searched_language_queries": {},
        },
        "opportunities": [],
        "provider": {
            "api_attempts": 0,
            "successes": 0,
            "failed_attempts": 0,
            "retry_attempts": 0,
            "remaining_run_attempts": MAX_TAVILY_ATTEMPTS,
            "rate_limit_errors": 0,
            "budget_deferred_queries": 0,
            "daily_budget": None,
        },
        "eligibility": "NOT_EVALUATED",
        "side_effects": _no_outbound_side_effects(),
    }


def _no_outbound_side_effects() -> dict:
    return {
        "gmail_accessed": False,
        "applications_attempted": 0,
        "drafts_created": 0,
        "emails_sent": 0,
        "whatsapp_messages_sent": 0,
        "private_vault_writes": 0,
        "production_database_writes": 0,
        "ephemeral_database_writes": True,
    }


def _clean_text(value, limit: int = 250) -> str:
    text = " ".join(str(value or "").split())
    text = _EMAIL.sub("[redacted]", text)
    text = _PHONE.sub("[redacted]", text)
    return text[:limit]


def _public_url(value) -> str:
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            return ""
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except (UnicodeError, ValueError):
        return ""


def _opportunity_type(job) -> str:
    text = f"{job.title or ''} {job.description or ''}".casefold()
    raw = str(getattr(job, "opportunity_type", "") or "").upper()
    if re.search(r"\bpfe\b|projet.{0,20}fin.{0,15}études|projet.{0,20}fin.{0,15}etudes", text):
        return "PFE"
    if raw in {"INTERNSHIP", "APPRENTICESHIP"}:
        return "INTERNSHIP_STAGE"
    if raw == "GRADUATE_PROGRAM" or re.search(
        r"\b(?:graduate|trainee|apprentice|apprentissage)\b", text,
    ):
        return "GRADUATE_TRAINEE"
    if raw == "JOB":
        return "JOB"
    return "UNKNOWN"


def _freshness(job, max_age_days: int, now: datetime) -> str:
    closing = getattr(job, "closing_at", None)
    if closing is not None:
        if closing.tzinfo is None:
            closing = closing.replace(tzinfo=timezone.utc)
        if closing <= now:
            return "EXPIRED_BY_CLOSING_DATE"
    posted = getattr(job, "posted_at", None)
    if posted is None:
        return "UNVERIFIABLE_DATE"
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    if (now - posted).days > max_age_days:
        return "STALE_BY_POSTING_DATE"
    return "DATED_WITHIN_WINDOW"


def _qualification_evidence(description: str) -> list[str]:
    results: list[str] = []
    for pattern in _QUALIFICATION_PATTERNS:
        for match in re.finditer(pattern, description, flags=re.IGNORECASE):
            text = _clean_text(match.group(0), 160)
            if text and text not in results:
                results.append(text)
            if len(results) >= 5:
                return results
    return results


def build_sanitized_report(
    report: DiscoveryReport,
    jobs: list,
    config: AgentConfig,
    daily_budget: dict,
    *,
    sources_config: dict | None = None,
    now: datetime | None = None,
) -> dict:
    instant = now or datetime.now(timezone.utc)
    source_reports = []
    queried_country_counts: Counter[str] = Counter()
    queried_language_counts: Counter[str] = Counter()
    query_errors: Counter[str] = Counter()
    for item in report.source_reports:
        errors = {
            re.sub(r"[^a-zA-Z0-9_-]", "", str(code)): int(count)
            for code, count in (item.get("query_error_codes") or {}).items()
        }
        query_errors.update(errors)
        queried_country_counts.update(item.get("queries_by_country") or {})
        queried_language_counts.update(item.get("queries_by_language") or {})
        source_reports.append({
            "name": _clean_text(item.get("name"), 80),
            "kind": _clean_text(item.get("kind"), 40),
            "status": _clean_text(item.get("health"), 32),
            "queries_attempted": int(item.get("queries", 0)),
            "queries_live_api": int(item.get("queries_api", 0)),
            "queries_cached": int(item.get("queries_cached", 0)),
            "queries_failed": int(item.get("queries_failed", 0)),
            "queries_budget_deferred": int(item.get("queries_budget_deferred", 0)),
            "queries_run_limit_deferred": int(item.get("queries_run_budget_deferred", 0)),
            "stale_cache_entries": int(item.get("queries_stale_cache", 0)),
            "external_results": int(item.get("fetched", 0)),
            "results_from_live_api": int(item.get("fetched_api_results", 0)),
            "results_from_cache": int(item.get("fetched_cache_results", 0)),
            "normalized": int(item.get("normalized", 0)),
            "duplicates": int(item.get("duplicates", 0)),
            "duplicate_reasons": {
                str(key): int(value)
                for key, value in (item.get("duplicate_reasons") or {}).items()
            },
            "new": int(item.get("new", 0)),
            "error_codes": errors,
            "queried_by_country": dict(item.get("queries_by_country") or {}),
            "queried_by_language": dict(item.get("queries_by_language") or {}),
        })

    configured = (sources_config or {}).get("connectors", [])
    observed = {item["name"] for item in source_reports}
    for source in configured:
        name = _clean_text(source.get("name", source.get("kind", "unknown")), 80)
        if name in observed:
            continue
        source_reports.append({
            "name": name,
            "kind": _clean_text(source.get("kind"), 40),
            "status": (
                "disabled_in_source_config"
                if not source.get("enabled", True)
                else "blocked_by_discovery_only_allowlist"
                if source.get("kind") != "tavily"
                else "not_initialized"
            ),
            "queries_attempted": 0,
            "queries_live_api": 0,
            "queries_cached": 0,
            "queries_failed": 0,
            "queries_budget_deferred": 0,
            "queries_run_limit_deferred": 0,
            "stale_cache_entries": 0,
            "external_results": 0,
            "results_from_live_api": 0,
            "results_from_cache": 0,
            "normalized": 0,
            "duplicates": 0,
            "duplicate_reasons": {},
            "new": 0,
            "error_codes": {},
            "queried_by_country": {},
            "queried_by_language": {},
        })

    type_counts: Counter[str] = Counter()
    country_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    freshness_counts: Counter[str] = Counter()
    opportunities = []
    for job in jobs:
        origin = report.new_opportunity_origins.get(job.id, "unknown")
        category = _opportunity_type(job)
        freshness = _freshness(
            job,
            int((config.rules or {}).get("max_days_since_posted", 45)),
            instant,
        )
        type_counts[category] += 1
        country = _clean_text(job.country or job.search_country or "UNKNOWN", 80)
        language = _clean_text(job.search_language or job.language or "UNKNOWN", 20)
        source = _clean_text(job.source or "UNKNOWN", 80)
        country_counts[country] += 1
        language_counts[language] += 1
        source_counts[source] += 1
        freshness_counts[freshness] += 1
        method = _clean_text(job.application_method or "UNKNOWN", 40).upper()
        listing_url = _public_url(job.url)
        route_url = _public_url(job.application_url)
        opportunities.append({
            "title": _clean_text(job.title, 180) or "Untitled listing",
            "employer": _clean_text(job.company.name if job.company else "", 120) or None,
            "listing_url": listing_url,
            "country": country,
            "country_basis": "connector_inferred_from_search_location",
            "location": _clean_text(job.location, 120),
            "posted_at": (
                job.posted_at.isoformat()
                if getattr(job, "posted_at", None) else None
            ),
            "date_status": freshness,
            "opportunity_type": category,
            "source": source,
            "source_confidence": job.source_confidence,
            "result_origin": "LIVE_API_RESPONSE" if origin == "api" else (
                "CACHE" if origin == "cache" else "UNKNOWN"
            ),
            "search_language": language,
            "language_basis": "search_query_language_not_verified_listing_language",
            "application_route": {
                "status": "DETECTED_UNVERIFIED" if method != "UNKNOWN" else "NOT_DETECTED",
                "type": method,
                "url": route_url or (listing_url if method != "UNKNOWN" else ""),
                "evidence_url": listing_url if method != "UNKNOWN" else "",
            },
            "qualification_evidence": _qualification_evidence(job.description or ""),
            "discovery_relevance": (
                "Returned by a configured civil-engineering/BTP search; "
                "candidate fit was not evaluated."
            ),
            "eligibility": "NOT_EVALUATED",
        })

    budget = report.api_request_budget or {}
    sources_with_errors = sum(1 for item in source_reports if item["queries_failed"])
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "COMPLETED",
        "reason_code": "",
        "limits": {
            "search_combinations": MAX_SEARCH_COMBINATIONS,
            "tavily_api_attempts": MAX_TAVILY_ATTEMPTS,
            "daily_query_limit_setting_unchanged": True,
        },
        "counts": {
            "external_results_fetched": sum(item["external_results"] for item in source_reports),
            "normalized_results": int(report.opportunities_fetched),
            "duplicates": int(report.duplicates),
            "unique_new_opportunities": int(report.new_jobs),
            "new_from_live_api": sum(1 for value in report.new_opportunity_origins.values() if value == "api"),
            "new_from_cache": sum(1 for value in report.new_opportunity_origins.values() if value == "cache"),
            "stale_or_expired_new": sum(
                count for status, count in freshness_counts.items()
                if status in {"STALE_BY_POSTING_DATE", "EXPIRED_BY_CLOSING_DATE"}
            ),
            "unverifiable_posting_date_new": freshness_counts["UNVERIFIABLE_DATE"],
        },
        "coverage": {
            "new_by_country": dict(country_counts),
            "new_by_search_language": dict(language_counts),
            "new_by_source": dict(source_counts),
            "new_by_opportunity_type": dict(type_counts),
            "date_status_new": dict(freshness_counts),
            "searched_country_queries": dict(queried_country_counts),
            "searched_language_queries": dict(queried_language_counts),
        },
        "sources": source_reports,
        "opportunities": opportunities,
        "provider": {
            "api_attempts": int(budget.get("attempts", 0)),
            "successes": int(budget.get("successes", 0)),
            "failed_attempts": int(budget.get("failed_attempts", 0)),
            "retry_attempts": int(budget.get("retry_attempts", 0)),
            "remaining_run_attempts": int(budget.get("remaining", MAX_TAVILY_ATTEMPTS)),
            "rate_limit_errors": sum(
                count for code, count in query_errors.items()
                if "quota" in code or "rate" in code or code in {"429", "432"}
            ),
            "error_codes": dict(query_errors),
            "budget_deferred_queries": sum(
                item["queries_budget_deferred"] + item["queries_run_limit_deferred"]
                for item in source_reports
            ),
            "failed_sources": sources_with_errors,
            "daily_budget": daily_budget,
        },
        "eligibility": "NOT_EVALUATED",
        "side_effects": _no_outbound_side_effects(),
    }
