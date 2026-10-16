"""Bounded, sanitized production discovery without analysis or outbound actions."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from app.config import AgentConfig, CandidateProfile, Preferences, load_yaml
from app.discovery.opportunity_details import is_verified_individual_vacancy
from app.discovery.vacancy_verification import MAX_DETAIL_VERIFICATIONS_PER_RUN
from app.workflows.discovery import DEFAULT_SOURCES_PATH, DiscoveryReport, run_discovery

REPORT_SCHEMA_VERSION = 5
MAX_SEARCH_COMBINATIONS = 12
MAX_TAVILY_ATTEMPTS = 8

_QUALIFICATION_PATTERNS = (
    r".{0,45}\b(?:minimum|min\.?|at least|required|must have|exigé|exigée|requis|requise)\b.{0,90}",
    r".{0,35}\b\d{1,2}\+?\s*(?:years?|ans?|jahre|años|anos)\b.{0,55}",
    r".{0,45}\b(?:bachelor|master|diploma|degree|diplôme|diploma|diplom|licence)\b.{0,70}",
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ().-]{7,}\d)(?!\w)")
_URL = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|authorization|bearer|token)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def bounded_source_allowed(source: dict) -> bool:
    kind = source.get("kind")
    return kind == "tavily" or (
        kind == "emploi_public" and source.get("name") == "emploi_public_ma"
    )


def validate_discovery_input_status(status_path) -> str:
    """Return a safe block reason unless every restored input is attested."""
    import json
    from pathlib import Path

    path = Path(status_path)
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "discovery_input_status_missing"
    except (OSError, ValueError):
        return "discovery_input_status_invalid"
    if not isinstance(status, dict):
        return "discovery_input_status_invalid"
    if (
        not isinstance(status.get("schema_version"), int)
        or isinstance(status.get("schema_version"), bool)
        or status["schema_version"] != 1
    ):
        return "discovery_input_status_invalid"
    if status.get("ready") is not True:
        reason = status.get("reason_code")
        if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
            return reason
        return "production_inputs_not_restored"
    required_true = (
        "vault_repository_validated",
        "vault_ref_validated",
        "database_snapshot_restored",
        "candidate_search_profile_restored",
        "provider_keys_restored",
        "provider_budget_ledgers_restored",
    )
    if any(status.get(key) is not True for key in required_true):
        return "discovery_input_status_invalid"
    key_count = status.get("provider_key_count")
    ledger_count = status.get("provider_budget_ledger_count")
    if (
        not isinstance(key_count, int)
        or isinstance(key_count, bool)
        or key_count < 1
        or not isinstance(ledger_count, int)
        or isinstance(ledger_count, bool)
    ):
        return "discovery_input_status_invalid"
    if ledger_count != key_count:
        return "restored_budget_ledger_count_mismatch"
    expected_ledgers = status.get("expected_budget_ledger_count")
    restored_ledgers = status.get("restored_budget_ledger_count")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (expected_ledgers, restored_ledgers)
    ):
        return "discovery_input_status_invalid"
    if (
        expected_ledgers is not None
        and restored_ledgers is not None
        and expected_ledgers != restored_ledgers
    ):
        return "restored_budget_ledger_count_mismatch"
    expected_inputs = status.get("expected_input_count")
    restored_inputs = status.get("restored_input_count")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (expected_inputs, restored_inputs)
    ):
        return "discovery_input_status_invalid"
    if (
        expected_inputs is not None
        and restored_inputs is not None
        and expected_inputs != restored_inputs
    ):
        return "restored_input_count_mismatch"
    commit = status.get("vault_commit")
    checkout = status.get("vault_checkout_path")
    if (
        not isinstance(commit, str)
        or not re.fullmatch(r"[a-f0-9]{40}", commit)
        or not isinstance(checkout, str)
        or not checkout
        or (
            status.get("provider_cache_restored") is not True
            and status.get("provider_cache_restored") is not False
        )
    ):
        return "discovery_input_status_invalid"
    return ""


async def run_bounded_discovery(
    session,
    config: AgentConfig,
    preferences: Preferences,
    profile: CandidateProfile,
    *,
    budget_store=None,
    phase_callback=None,
) -> DiscoveryReport:
    """Run configured listing connectors with strict task-specific bounds."""
    if phase_callback is not None:
        phase_callback("source_initialization")
    source_configuration = load_yaml(DEFAULT_SOURCES_PATH)
    if any(
        source.get("enabled", True) and not bounded_source_allowed(source)
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
    discovery["verify_vacancy_details"] = True
    bounded_config = config.model_copy(update={"discovery": discovery})
    if phase_callback is not None:
        phase_callback("discovery_execution")
    return await run_discovery(
        session,
        bounded_config,
        preferences,
        profile=profile,
        llm=None,
        max_queries_override=MAX_SEARCH_COMBINATIONS,
        max_api_attempts_override=MAX_TAVILY_ATTEMPTS,
        max_search_combinations=MAX_SEARCH_COMBINATIONS,
        budget_reserver=(
            budget_store.reserve_general if budget_store is not None else None
        ),
        budget_exhauster=budget_store.exhaust if budget_store is not None else None,
    )


def validate_budget_ledgers(data_dir, keys: list[str]) -> tuple[bool, int]:
    """Require the restored per-key ledger before allowing any API attempt."""
    import json
    from pathlib import Path

    from app.connectors.tavily_resilience import (
        TAVILY_DAILY_REQUEST_LIMIT,
        key_fingerprint,
    )

    root = Path(data_dir)
    today = datetime.now(timezone.utc).date()
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
            or not _valid_ledger_date(payload["date"])
            or not isinstance(payload.get("count"), int)
            or isinstance(payload.get("count"), bool)
            or not 0 <= payload["count"] <= TAVILY_DAILY_REQUEST_LIMIT
            or not isinstance(payload.get("exhausted"), bool)
        ):
            return False, 0
        try:
            ledger_date = datetime.strptime(
                payload["date"], "%Y-%m-%d",
            ).date()
        except ValueError:
            return False, 0
        if ledger_date > today:
            return False, 0
    return True, len(seen)


def _valid_ledger_date(value: str) -> bool:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except (TypeError, ValueError):
        return False


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


def blocked_report(
    reason_code: str,
    *,
    phase: str = "",
    exception_class: str = "",
    diagnostic_message: str = "",
) -> dict:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "BLOCKED",
        "reason_code": reason_code,
        "phase": phase,
        "diagnostic": {
            "exception_class": exception_class,
            "message": diagnostic_message,
        },
        "limits": {
            "search_combinations": MAX_SEARCH_COMBINATIONS,
            "tavily_api_attempts": MAX_TAVILY_ATTEMPTS,
            "vacancy_detail_verifications": MAX_DETAIL_VERIFICATIONS_PER_RUN,
        },
        "counts": {
            "external_results_fetched": 0,
            "normalized_results": 0,
            "duplicates": 0,
            "unique_new_opportunities": 0,
            "unique_new_candidate_vacancies": 0,
            "unique_new_discovery_records": 0,
            "individual_vacancies_verified": 0,
            "individual_vacancies_unverified": 0,
            "individual_vacancies_closed_or_expired": 0,
            "individual_vacancies_status_unknown": 0,
            "research_required": 0,
            "rejected_non_vacancy": 0,
            "requiring_eligibility_assessment": 0,
            "new_from_live_api": 0,
            "new_from_cache": 0,
        },
        "sources": [],
        "coverage": {
            "new_by_country": {},
            "new_by_search_language": {},
            "new_by_source": {},
            "new_by_opportunity_type": {},
            "new_by_entity_type": {},
            "new_by_entity_state": {},
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
            "durably_reserved_attempts": 0,
            "rate_limit_errors": 0,
            "budget_deferred_queries": 0,
            "daily_budget": None,
            "restore_diagnostic_reason": "",
            "ledger_diagnostics": [],
            "expected_budget_ledger_count": None,
            "restored_budget_ledger_count": None,
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
        "private_vault_budget_ledger_writes": 0,
        "production_database_writes": 0,
        "ephemeral_database_writes": True,
    }


def _clean_text(value, limit: int = 250) -> str:
    text = " ".join(str(value or "").split())
    text = _EMAIL.sub("[redacted]", text)
    text = _PHONE.sub("[redacted]", text)
    return text[:limit]


def _profile_sensitive_terms(profile: CandidateProfile | None) -> list[str]:
    if profile is None:
        return []

    values: list[str] = []

    def collect(value) -> None:
        if isinstance(value, str):
            if len(value.strip()) >= 4:
                values.append(value.strip())
        elif isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                collect(child)

    collect(profile.model_dump())
    return sorted(set(values), key=len, reverse=True)


def _safe_search_query(value, sensitive_terms=()) -> str:
    query = " ".join(str(value or "").split())
    for term in sensitive_terms:
        if not term:
            continue
        query = re.sub(
            re.escape(term),
            "[profile_term]",
            query,
            flags=re.IGNORECASE,
        )
    query = _SECRET_ASSIGNMENT.sub("[credential]", query)
    query = _EMAIL.sub("[email]", query)
    query = _PHONE.sub("[phone]", query)
    query = _URL.sub("[url]", query)
    return query[:300]


def _trace_ids(query: str, intent: str, country: str, language: str):
    intent_payload = "\0".join((intent, query))
    combination_payload = "\0".join((query, intent, country, language))
    intent_id = hashlib.sha256(intent_payload.encode("utf-8")).hexdigest()[:12]
    combination_id = hashlib.sha256(
        combination_payload.encode("utf-8"),
    ).hexdigest()[:12]
    return f"intent_{intent_id}", f"plan_{combination_id}"


def _stable_record_id(url: str) -> str:
    safe_url = _public_url(url)
    return "record_" + hashlib.sha256(
        safe_url.encode("utf-8"),
    ).hexdigest()[:16]


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
    if (
        posted is None
        or not getattr(job, "posted_at_source", "")
        or not getattr(job, "posted_at_evidence_url", "")
    ):
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


def _safe_public_competition(value: object) -> dict | None:
    if not isinstance(value, dict) or value.get("source") != "emploi_public_ma":
        return None
    route = value.get("submission_route") or {}
    if not isinstance(route, dict):
        route = {}
    requirements = value.get("requirements_original") or {}
    if not isinstance(requirements, dict):
        requirements = {}
    checklist = value.get("eligibility_checklist")
    safe_checklist = None
    if isinstance(checklist, dict):
        items = checklist.get("items") or {}
        if not isinstance(items, dict):
            items = {}
        safe_checklist = {
            "status": _clean_text(checklist.get("status"), 40),
            "candidate_eligibility": _clean_text(
                checklist.get("candidate_eligibility"), 40,
            ),
            "submission_owner_approval": _clean_text(
                checklist.get("submission_owner_approval"), 40,
            ),
            "notice_documents_not_fetched": bool(
                checklist.get("notice_documents_not_fetched"),
            ),
            "items": {
                key: {
                    "requirement_from_accessible_notice": _clean_text(
                        item.get("requirement_from_accessible_notice"), 500,
                    ),
                    "candidate_evidence_status": _clean_text(
                        item.get("candidate_evidence_status"), 40,
                    ),
                    "review_status": _clean_text(item.get("review_status"), 40),
                }
                for key, item in items.items()
                if key in {"diploma", "specialty", "experience", "nationality", "age"}
                and isinstance(item, dict)
            },
        }
    notice_documents = value.get("notice_documents")
    if not isinstance(notice_documents, list):
        notice_documents = []
    return {
        "portal_id": _clean_text(value.get("portal_id"), 36),
        "stage": _clean_text(value.get("stage"), 40),
        "status": _clean_text(value.get("status"), 40),
        "verification_status": _clean_text(
            value.get("verification_status"), 24,
        ),
        "reason_code": _clean_text(value.get("reason_code"), 80),
        "evidence_url": _public_url(value.get("evidence_url")),
        "observed_at": _clean_text(value.get("observed_at"), 64),
        "language": _clean_text(value.get("language"), 8),
        "title": _clean_text(value.get("title_original"), 180),
        "administration": _clean_text(
            value.get("administration_original"), 180,
        ),
        "grade": _clean_text(value.get("grade_original"), 64),
        "location": _clean_text(value.get("location_original"), 120),
        "post_count": (
            value.get("post_count")
            if isinstance(value.get("post_count"), int)
            and not isinstance(value.get("post_count"), bool)
            else None
        ),
        "requirements": {
            key: _clean_text(val, 1000)
            for key, val in requirements.items()
            if key in {"diploma", "specialty", "experience", "nationality", "age"}
        },
        "deadline": {
            "source_text": _clean_text(value.get("deadline_original"), 100),
            "at": _clean_text(value.get("deadline_at"), 64),
        },
        "posted_at": {
            "source_text": _clean_text(value.get("posted_at_original"), 100),
            "at": _clean_text(value.get("posted_at"), 64),
            "evidence_url": _public_url(
                value.get("evidence_url") if value.get("posted_at") else "",
            ),
        },
        "submission_route": {
            "kind": _clean_text(route.get("kind"), 32),
            "conflict": bool(route.get("conflict")),
            "instructions": _clean_text(route.get("instructions_raw"), 200),
            "destination": _clean_text(route.get("destination_raw"), 200),
            "url": _public_url(route.get("destination_url")),
        },
        "notice_documents": [
            {
                "label": _clean_text(document.get("label"), 100),
                "url": _public_url(document.get("url")),
                "fetched": False,
            }
            for document in notice_documents
            if isinstance(document, dict)
        ][:8],
        "notice_documents_not_fetched": bool(
            value.get("notice_documents_not_fetched"),
        ),
        "eligibility_checklist": safe_checklist,
        "submission_owner_approval": "REQUIRED_NOT_GRANTED",
    }


def build_sanitized_report(
    report: DiscoveryReport,
    jobs: list,
    config: AgentConfig,
    daily_budget: dict,
    *,
    sources_config: dict | None = None,
    now: datetime | None = None,
    profile: CandidateProfile | None = None,
) -> dict:
    instant = now or datetime.now(timezone.utc)
    sensitive_terms = _profile_sensitive_terms(profile)
    safe_plan = []
    for item in report.plan_combinations:
        query = _safe_search_query(item.get("query"), sensitive_terms)
        intent = _clean_text(item.get("intent"), 48)
        country = _clean_text(item.get("country"), 80)
        language = _clean_text(item.get("language"), 16)
        plan_index = int(item.get("plan_index", 0))
        intent_id, combination_id = _trace_ids(
            query, intent, country, language,
        )
        safe_plan.append({
            "plan_index": plan_index,
            "query_intent_id": intent_id,
            "plan_combination_id": combination_id,
            "query": query,
            "query_redacted": query != " ".join(
                str(item.get("query") or "").split()
            ),
            "intent": intent,
            "country": country,
            "language": language,
        })
    source_reports = []
    queried_country_counts: Counter[str] = Counter()
    queried_language_counts: Counter[str] = Counter()
    query_errors: Counter[str] = Counter()
    deferred_by_reason: Counter[str] = Counter()
    for item in report.source_reports:
        errors = {
            re.sub(r"[^a-zA-Z0-9_-]", "", str(code)): int(count)
            for code, count in (item.get("query_error_codes") or {}).items()
        }
        query_errors.update(errors)
        deferred_by_reason.update(
            item.get("queries_deferred_by_reason") or {},
        )
        queried_country_counts.update(item.get("queries_by_country") or {})
        queried_language_counts.update(item.get("queries_by_language") or {})
        query_trace = []
        for trace in item.get("query_trace") or []:
            query = _safe_search_query(trace.get("query"), sensitive_terms)
            intent = _clean_text(trace.get("intent"), 48)
            country = _clean_text(trace.get("country"), 80)
            language = _clean_text(trace.get("language"), 16)
            plan_index = int(trace.get("plan_index", 0))
            intent_id, combination_id = _trace_ids(
                query, intent, country, language,
            )
            request_status = str(trace.get("request_status") or "unknown")
            if request_status not in {
                "pending", "attempted", "cached", "deferred", "failed",
            }:
                request_status = "unknown"
            reason_code = str(trace.get("reason_code") or "")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason_code):
                reason_code = ""
            query_trace.append({
                "plan_index": plan_index,
                "query_intent_id": intent_id,
                "plan_combination_id": combination_id,
                "query": query,
                "query_redacted": query != " ".join(
                    str(trace.get("query") or "").split()
                ),
                "intent": intent,
                "country": country,
                "language": language,
                "source": _clean_text(item.get("name"), 80),
                "request_status": request_status,
                "reason_code": reason_code,
                "result_state": _clean_text(trace.get("result_state"), 48),
                "provider_attempts": max(
                    0, int(trace.get("provider_attempts", 0)),
                ),
            })
        verification_evidence = []
        for evidence in item.get("verification_evidence") or []:
            url = _public_url(evidence.get("url"))
            checks = evidence.get("evidence_checks")
            checks = checks if isinstance(checks, dict) else {}
            verification_evidence.append({
                "record_id": _stable_record_id(url),
                "source_category": _clean_text(
                    evidence.get("ats_vendor"), 32,
                ),
                "outcome": _clean_text(evidence.get("outcome"), 24),
                "reason_code": _clean_text(evidence.get("reason_code"), 80),
                "observed_at": _clean_text(evidence.get("observed_at"), 64),
                "evidence_url": url,
                "evidence_checks": {
                    str(name): value
                    for name, value in checks.items()
                    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(name))
                    and isinstance(value, bool)
                },
            })
        source_reports.append({
            "name": _clean_text(item.get("name"), 80),
            "kind": _clean_text(item.get("kind"), 40),
            "status": _clean_text(item.get("health"), 32),
            "result_state": _clean_text(item.get("result_state"), 40),
            "request_scope": {
                "applies_to_countries": [
                    _clean_text(country, 80)
                    for country in (item.get("request_scope") or {}).get(
                        "applies_to_countries", [],
                    )
                ],
                "domains": [
                    _clean_text(domain, 120)
                    for domain in (item.get("request_scope") or {}).get("domains", [])
                ],
                "official_only": bool(
                    (item.get("request_scope") or {}).get("official_only", False)
                ),
                "request_scope_id": _clean_text(
                    (item.get("request_scope") or {}).get("request_scope_id"), 16,
                ),
            },
            "inapplicable_country_queries": {
                _clean_text(country, 80): int(count)
                for country, count in (
                    item.get("inapplicable_country_queries") or {}
                ).items()
            },
            "queries_attempted": int(item.get("queries", 0)),
            "queries_live_api": int(item.get("queries_api", 0)),
            "queries_direct": int(item.get("queries_direct", 0)),
            "queries_cached": int(item.get("queries_cached", 0)),
            "queries_empty": int(item.get("queries_empty", 0)),
            "live_empty_responses": int(item.get("live_empty_responses", 0)),
            "cached_empty_results": int(item.get("cached_empty_results", 0)),
            "queries_filtered": int(item.get("queries_filtered", 0)),
            "queries_failed": int(item.get("queries_failed", 0)),
            "queries_budget_deferred": int(item.get("queries_budget_deferred", 0)),
            "queries_run_limit_deferred": int(item.get("queries_run_budget_deferred", 0)),
            "query_trace": query_trace[:256],
            "verification_evidence": verification_evidence[:MAX_DETAIL_VERIFICATIONS_PER_RUN],
            "stale_cache_entries": int(item.get("queries_stale_cache", 0)),
            "external_results": int(item.get("fetched", 0)),
            "public_page_requests": int(item.get("public_page_requests", 0)),
            "public_page_requests_by_language": {
                _clean_text(language, 20): int(count)
                for language, count in (
                    item.get("public_page_requests_by_language") or {}
                ).items()
            },
            "public_page_telemetry_by_request_language": {
                _clean_text(language, 20): {
                    _clean_text(metric, 40): int(count)
                    for metric, count in metrics.items()
                    if isinstance(count, int)
                    and not isinstance(count, bool)
                    and count >= 0
                }
                for language, metrics in (
                    item.get("public_page_telemetry_by_request_language") or {}
                ).items()
                if isinstance(metrics, dict)
            },
            "public_competition_status_counts": {
                str(status): int(count)
                for status, count in (
                    item.get("public_competition_status_counts") or {}
                ).items()
                if str(status).isalnum() or "_" in str(status)
            },
            "procedural_notices": int(item.get("procedural_notices", 0)),
            "procedural_notices_matching_existing": int(
                item.get("procedural_notices_matching_existing", 0),
            ),
            "procedural_notices_without_matching_vacancy": int(
                item.get("procedural_notices_without_matching_vacancy", 0),
            ),
            "public_competition_results_by_country": {
                _clean_text(country, 80): int(count)
                for country, count in (
                    item.get("public_competition_results_by_country") or {}
                ).items()
            },
            "public_competition_results_by_language": {
                _clean_text(language, 20): int(count)
                for language, count in (
                    item.get("public_competition_results_by_language") or {}
                ).items()
            },
            "verified_individual_vacancies_by_query": {
                _clean_text(query, 180): int(count)
                for query, count in (
                    item.get("verified_individual_vacancies_by_query") or {}
                ).items()
            },
            "verified_individual_vacancies_by_country": {
                _clean_text(country, 80): int(count)
                for country, count in (
                    item.get("verified_individual_vacancies_by_country") or {}
                ).items()
            },
            "verified_individual_vacancies_by_language": {
                _clean_text(language, 20): int(count)
                for language, count in (
                    item.get("verified_individual_vacancies_by_language") or {}
                ).items()
            },
            "results_from_live_api": int(item.get("fetched_api_results", 0)),
            "results_from_cache": int(item.get("fetched_cache_results", 0)),
            "provider_results_returned": int(
                item.get("provider_results_returned", 0),
            ),
            "provider_results_filtered": int(
                item.get("provider_results_filtered", 0),
            ),
            "cached_provider_results_filtered": int(
                item.get("cached_provider_results_filtered", 0),
            ),
            "filter_reason_counts": {
                str(code): int(count)
                for code, count in (item.get("filter_reason_counts") or {}).items()
                if str(code).isalnum() or "_" in str(code)
            },
            "filtered_url_examples": [
                {
                    "reason_code": _clean_text(example.get("reason_code"), 64),
                    "host": _clean_text(example.get("host"), 120),
                    "path_pattern": _clean_text(example.get("path_pattern"), 180),
                }
                for example in (item.get("filtered_url_examples") or [])
                if isinstance(example, dict)
            ][:12],
            "normalization_rejection_reasons": {
                str(code): int(count)
                for code, count in (
                    item.get("normalization_rejection_reasons") or {}
                ).items()
                if str(code).isalnum() or "_" in str(code)
            },
            "normalized": int(item.get("normalized", 0)),
            "duplicates": int(item.get("duplicates", 0)),
            "duplicate_reasons": {
                str(key): int(value)
                for key, value in (item.get("duplicate_reasons") or {}).items()
            },
            "new": int(item.get("new", 0)),
            "new_by_entity_type": {
                str(code): int(count)
                for code, count in (item.get("new_by_entity_type") or {}).items()
                if str(code).isalnum() or "_" in str(code)
            },
            "new_by_entity_state": {
                str(code): int(count)
                for code, count in (item.get("new_by_entity_state") or {}).items()
                if str(code).isalnum() or "_" in str(code)
            },
            "detail_verification_attempts": int(
                item.get("detail_verification_attempts", 0),
            ),
            "detail_verifications_reused": int(
                item.get("detail_verifications_reused", 0),
            ),
            "verification_candidates": int(item.get("verification_candidates", 0)),
            "verification_source_unsupported": int(
                item.get("verification_source_unsupported", 0),
            ),
            "verified_individual_vacancies": int(
                item.get("verified_individual_vacancies", 0),
            ),
            "unverified_vacancy_candidates": int(
                item.get("unverified_vacancy_candidates", 0),
            ),
            "closed_vacancies": int(item.get("closed_vacancies", 0)),
            "unknown_vacancy_status": int(
                item.get("unknown_vacancy_status", 0),
            ),
            "research_leads": int(item.get("research_leads", 0)),
            "rejected_non_vacancy": int(
                item.get("rejected_non_vacancy", 0),
            ),
            "existing_candidates_reverified": int(
                item.get("existing_candidates_reverified", 0),
            ),
            "existing_candidates_verified": int(
                item.get("existing_candidates_verified", 0),
            ),
            "existing_candidates_closed": int(
                item.get("existing_candidates_closed", 0),
            ),
            "existing_candidates_unknown": int(
                item.get("existing_candidates_unknown", 0),
            ),
            "verification_reason_codes": {
                str(code): int(count)
                for code, count in (item.get("verification_reason_codes") or {}).items()
                if str(code).isalnum() or "_" in str(code)
            },
            "error_codes": errors,
            "provider_results_by_country": {
                _clean_text(key, 80): int(value)
                for key, value in (
                    item.get("provider_results_by_country") or {}
                ).items()
            },
            "provider_results_by_language": {
                _clean_text(key, 20): int(value)
                for key, value in (
                    item.get("provider_results_by_language") or {}
                ).items()
            },
            "accepted_results_by_country": {
                _clean_text(key, 80): int(value)
                for key, value in (
                    item.get("accepted_results_by_country") or {}
                ).items()
            },
            "accepted_results_by_language": {
                _clean_text(key, 20): int(value)
                for key, value in (
                    item.get("accepted_results_by_language") or {}
                ).items()
            },
            "filtered_results_by_country": {
                _clean_text(key, 80): int(value)
                for key, value in (
                    item.get("filtered_results_by_country") or {}
                ).items()
            },
            "filtered_results_by_language": {
                _clean_text(key, 20): int(value)
                for key, value in (
                    item.get("filtered_results_by_language") or {}
                ).items()
            },
            "key_budget_events": {
                str(fingerprint): {
                    str(reason): int(count)
                    for reason, count in reasons.items()
                    if str(reason).isalnum() or "_" in str(reason)
                }
                for fingerprint, reasons in (
                    item.get("key_budget_events") or {}
                ).items()
                if re.fullmatch(r"[a-f0-9]{8}", str(fingerprint))
            },
            "queries_deferred_by_reason": {
                str(reason): int(count)
                for reason, count in (
                    item.get("queries_deferred_by_reason") or {}
                ).items()
                if str(reason).isalnum() or "_" in str(reason)
            },
            "queried_by_country": dict(item.get("queries_by_country") or {}),
            "queried_by_language": dict(item.get("queries_by_language") or {}),
            "provider_attempts_by_country": {
                _clean_text(country, 80): int(count)
                for country, count in (
                    item.get("provider_attempts_by_country") or {}
                ).items()
            },
            "provider_attempts_by_language": {
                _clean_text(language, 20): int(count)
                for language, count in (
                    item.get("provider_attempts_by_language") or {}
                ).items()
            },
            "cache_hits_by_country": {
                _clean_text(country, 80): int(count)
                for country, count in (item.get("cache_hits_by_country") or {}).items()
            },
            "cache_hits_by_language": {
                _clean_text(language, 20): int(count)
                for language, count in (item.get("cache_hits_by_language") or {}).items()
            },
            "successful_empty_responses": int(item.get("queries_empty", 0)),
            "requests_not_attempted_due_to_budget": int(
                item.get("queries_budget_deferred", 0)
                + item.get("queries_run_budget_deferred", 0)
            ),
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
                if not bounded_source_allowed(source)
                else "not_initialized"
            ),
            "result_state": "not_attempted",
            "request_scope": {
                "applies_to_countries": source.get(
                    "applies_to_countries",
                    ["*"] if not source.get("domains") else [],
                ),
                "domains": sorted(source.get("domains") or []),
                "official_only": bool(source.get("official_only", False)),
                "request_scope_id": "",
            },
            "inapplicable_country_queries": {},
            "queries_attempted": 0,
            "queries_live_api": 0,
            "queries_cached": 0,
            "queries_empty": 0,
            "live_empty_responses": 0,
            "cached_empty_results": 0,
            "queries_filtered": 0,
            "queries_failed": 0,
            "queries_budget_deferred": 0,
            "queries_run_limit_deferred": 0,
            "query_trace": [],
            "verification_evidence": [],
            "stale_cache_entries": 0,
            "external_results": 0,
            "public_page_requests": 0,
            "public_page_requests_by_language": {},
            "public_page_telemetry_by_request_language": {},
            "results_from_live_api": 0,
            "results_from_cache": 0,
            "provider_results_returned": 0,
            "provider_results_filtered": 0,
            "cached_provider_results_filtered": 0,
            "filter_reason_counts": {},
            "filtered_url_examples": [],
            "normalization_rejection_reasons": {},
            "normalized": 0,
            "duplicates": 0,
            "duplicate_reasons": {},
            "new": 0,
            "new_by_entity_type": {},
            "new_by_entity_state": {},
            "detail_verification_attempts": 0,
            "detail_verifications_reused": 0,
            "verification_candidates": 0,
            "verification_source_unsupported": 0,
            "verified_individual_vacancies": 0,
            "unverified_vacancy_candidates": 0,
            "closed_vacancies": 0,
            "unknown_vacancy_status": 0,
            "research_leads": 0,
            "rejected_non_vacancy": 0,
            "verification_reason_codes": {},
            "provider_results_by_country": {},
            "provider_results_by_language": {},
            "accepted_results_by_country": {},
            "accepted_results_by_language": {},
            "filtered_results_by_country": {},
            "filtered_results_by_language": {},
            "key_budget_events": {},
            "queries_deferred_by_reason": {},
            "existing_candidates_reverified": 0,
            "existing_candidates_verified": 0,
            "existing_candidates_closed": 0,
            "existing_candidates_unknown": 0,
            "error_codes": {},
            "queried_by_country": {},
            "queried_by_language": {},
            "provider_attempts_by_country": {},
            "provider_attempts_by_language": {},
            "cache_hits_by_country": {},
            "cache_hits_by_language": {},
        })

    type_counts: Counter[str] = Counter()
    country_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    freshness_counts: Counter[str] = Counter()
    entity_type_counts: Counter[str] = Counter()
    entity_state_counts: Counter[str] = Counter()
    opportunities = []
    for job in jobs:
        origin = report.new_opportunity_origins.get(job.id, "unknown")
        entity_state = _clean_text(
            getattr(job, "entity_state", "RESEARCH_REQUIRED"), 40,
        ).upper()
        verified_vacancy = is_verified_individual_vacancy(job)
        category = _opportunity_type(job) if verified_vacancy else "UNKNOWN"
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
        entity_type = _clean_text(getattr(job, "entity_type", "UNKNOWN"), 40).upper()
        entity_type_counts[entity_type] += 1
        entity_state_counts[entity_state] += 1
        method = _clean_text(job.application_method or "UNKNOWN", 40).upper()
        listing_url = _public_url(job.url)
        route_url = _public_url(job.application_url)
        observed_at = getattr(job, "last_verified_at", None)
        verification_outcome = {
            "VACANCY_VERIFIED": "OPEN",
            "VACANCY_CLOSED": "CLOSED_OR_EXPIRED",
            "VACANCY_STATUS_UNKNOWN": "UNKNOWN_OR_UNAVAILABLE",
            "VACANCY_UNVERIFIED": "UNVERIFIED_CANDIDATE",
            "RESEARCH_REQUIRED": "RESEARCH_REQUIRED",
            "REJECTED_NON_VACANCY": "REJECTED_NON_VACANCY",
        }.get(entity_state, "UNKNOWN_OR_UNAVAILABLE")
        opportunities.append({
            "record_id": _stable_record_id(listing_url),
            "title": _clean_text(job.title, 180) or "Untitled listing",
            "employer": _clean_text(job.company.name if job.company else "", 120) or None,
            "listing_url": listing_url,
            "country": country,
            "country_basis": (
                "verified_official_competition_detail"
                if verified_vacancy
                and isinstance(getattr(job, "public_competition", None), dict)
                else "verified_ats_detail_location" if verified_vacancy
                else "connector_or_search_location_not_independently_verified"
            ),
            "location": _clean_text(job.location, 120),
            "posted_at": (
                job.posted_at.isoformat()
                if getattr(job, "posted_at", None) else None
            ),
            "posting_date_evidence": {
                "status": (
                    "SOURCE_REPORTED"
                    if getattr(job, "posted_at_source", "")
                    and getattr(job, "posted_at_evidence_url", "")
                    and getattr(job, "posted_at", None) is not None
                    else "UNVERIFIABLE"
                ),
                "source_field": re.sub(
                    r"[^A-Za-z0-9_.:-]",
                    "",
                    str(getattr(job, "posted_at_source", "") or ""),
                )[:128],
                "evidence_url": _public_url(
                    getattr(job, "posted_at_evidence_url", ""),
                ),
            },
            "discovered_at": (
                job.discovered_at.isoformat()
                if getattr(job, "discovered_at", None) else None
            ),
            "date_status": freshness,
            "opportunity_type": category,
            "entity_type": entity_type,
            "entity_state": entity_state,
            "entity_reason_code": _clean_text(
                getattr(job, "entity_reason_code", "legacy_record_unverified"), 64,
            ),
            "entity_evidence_url": _public_url(
                getattr(job, "entity_evidence_url", ""),
            ),
            "vacancy_verification": {
                "outcome": verification_outcome,
                "observed_at": observed_at.isoformat() if observed_at else None,
                "reason_code": _clean_text(
                    getattr(job, "entity_reason_code", "legacy_record_unverified"),
                    64,
                ),
                "evidence_url": _public_url(
                    getattr(job, "entity_evidence_url", ""),
                ),
            },
            "source": source,
            "source_confidence": job.source_confidence,
            "result_origin": "LIVE_API_RESPONSE" if origin == "api" else (
                "CACHE" if origin == "cache" else "UNKNOWN"
            ),
            "search_language": language,
            "language_basis": "search_query_language_not_verified_listing_language",
            "application_route": {
                "status": "VERIFIED" if verified_vacancy else (
                    "DETECTED_UNVERIFIED" if method != "UNKNOWN" else "NOT_DETECTED"
                ),
                "type": method,
                "verified": verified_vacancy,
                "url": route_url or (listing_url if method != "UNKNOWN" else ""),
                "evidence_url": listing_url if verified_vacancy or method != "UNKNOWN" else "",
            },
            "qualification_evidence": (
                _qualification_evidence(job.description or "")
                if verified_vacancy else []
            ),
            "discovery_relevance": (
                "Returned by a configured civil-engineering/BTP search; "
                "candidate fit was not evaluated."
            ),
            "eligibility": "NOT_EVALUATED",
            "public_competition": _safe_public_competition(
                getattr(job, "public_competition", None),
            ),
        })

    budget = report.api_request_budget or {}
    sources_with_errors = sum(1 for item in source_reports if item["queries_failed"])
    external_results = sum(item["external_results"] for item in source_reports)
    provider_results_returned = sum(
        item["provider_results_returned"] for item in source_reports
    )
    provider_results_filtered = sum(
        item["provider_results_filtered"] for item in source_reports
    )
    cached_provider_results_filtered = sum(
        item["cached_provider_results_filtered"] for item in source_reports
    )
    source_queries = sum(item["queries_attempted"] for item in source_reports)
    live_api_queries = sum(item["queries_live_api"] for item in source_reports)
    successful_empty_queries = sum(
        item["live_empty_responses"] for item in source_reports
    )
    cached_empty_queries = sum(
        item["cached_empty_results"] for item in source_reports
    )
    failed_queries = sum(item["queries_failed"] for item in source_reports)
    not_attempted_due_budget = sum(
        item["queries_budget_deferred"] + item["queries_run_limit_deferred"]
        for item in source_reports
    )
    source_country_exclusions = sum(
        sum(item["inapplicable_country_queries"].values())
        for item in source_reports
    )
    filter_reasons = Counter()
    normalization_reasons = Counter()
    result_by_country = Counter()
    result_by_language = Counter()
    for item in source_reports:
        filter_reasons.update(item.get("filter_reason_counts") or {})
        normalization_reasons.update(
            item.get("normalization_rejection_reasons") or {},
        )
        result_by_country.update(item.get("provider_results_by_country") or {})
        result_by_language.update(item.get("provider_results_by_language") or {})
    existing_verification_updates = {
        outcome: sum(
            item[f"existing_candidates_{outcome}"]
            for item in source_reports
        )
        for outcome in ("reverified", "verified", "closed", "unknown")
    }
    if external_results:
        result_state = "provider_results_received"
    elif provider_results_returned:
        result_state = "provider_results_filtered_by_source"
    elif cached_provider_results_filtered:
        result_state = "cached_results_filtered_by_source"
    elif live_api_queries and successful_empty_queries == live_api_queries:
        result_state = "all_live_provider_responses_successfully_empty"
    elif not live_api_queries and cached_empty_queries:
        result_state = "cached_empty_results_no_live_requests"
    elif source_queries and successful_empty_queries + cached_empty_queries == source_queries and not failed_queries:
        result_state = "all_attempted_requests_successfully_empty"
    elif not source_queries and not_attempted_due_budget:
        result_state = "all_requests_deferred_by_budget"
    elif not source_queries:
        result_state = "no_source_requests_attempted"
    elif failed_queries:
        result_state = "requests_failed_or_unresolved"
    else:
        result_state = "mixed_empty_and_unresolved_requests"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "COMPLETED",
        "reason_code": "",
        "search_plan": {
            "planned_combinations": len({
                item["plan_combination_id"] for item in safe_plan
            }),
            "plan_entries": len(safe_plan),
            "combinations": safe_plan[:MAX_SEARCH_COMBINATIONS],
        },
        "limits": {
            "search_combinations": MAX_SEARCH_COMBINATIONS,
            "tavily_api_attempts": MAX_TAVILY_ATTEMPTS,
            "vacancy_detail_verifications": MAX_DETAIL_VERIFICATIONS_PER_RUN,
            "daily_query_limit_setting_unchanged": True,
        },
        "counts": {
            "search_combinations_attempted": int(report.combinations_attempted),
            "source_requests_attempted": int(report.source_requests_attempted),
            "external_results_fetched": external_results,
            "normalized_results": int(report.opportunities_fetched),
            "duplicates": int(report.duplicates),
            "unique_new_opportunities": entity_state_counts["VACANCY_VERIFIED"],
            "unique_new_candidate_vacancies": entity_type_counts["INDIVIDUAL_VACANCY"],
            "unique_new_discovery_records": int(report.new_jobs),
            "individual_vacancies_verified": entity_state_counts["VACANCY_VERIFIED"],
            "individual_vacancies_unverified": entity_state_counts["VACANCY_UNVERIFIED"],
            "individual_vacancies_closed_or_expired": entity_state_counts["VACANCY_CLOSED"],
            "individual_vacancies_status_unknown": entity_state_counts[
                "VACANCY_STATUS_UNKNOWN"
            ],
            "existing_candidates_reverified": existing_verification_updates[
                "reverified"
            ],
            "existing_candidates_verified": existing_verification_updates["verified"],
            "existing_candidates_closed": existing_verification_updates["closed"],
            "existing_candidates_unknown": existing_verification_updates["unknown"],
            "research_required": entity_state_counts["RESEARCH_REQUIRED"],
            "rejected_non_vacancy": entity_state_counts["REJECTED_NON_VACANCY"],
            "requiring_eligibility_assessment": entity_state_counts["VACANCY_VERIFIED"],
            "new_from_live_api": sum(1 for value in report.new_opportunity_origins.values() if value == "api"),
            "new_from_cache": sum(1 for value in report.new_opportunity_origins.values() if value == "cache"),
            "stale_or_expired_new": sum(
                count for status, count in freshness_counts.items()
                if status in {"STALE_BY_POSTING_DATE", "EXPIRED_BY_CLOSING_DATE"}
            ),
            "stale_by_posting_date_new": freshness_counts[
                "STALE_BY_POSTING_DATE"
            ],
            "expired_by_closing_date_new": freshness_counts[
                "EXPIRED_BY_CLOSING_DATE"
            ],
            "unverifiable_posting_date_new": freshness_counts["UNVERIFIABLE_DATE"],
        },
        "query_outcome": {
            "result_state": result_state,
            "source_requests_attempted": source_queries,
            "successful_empty_responses": successful_empty_queries,
            "cached_empty_results": cached_empty_queries,
            "failed_requests": failed_queries,
            "requests_not_attempted_due_to_budget": not_attempted_due_budget,
            "source_country_combinations_inapplicable": source_country_exclusions,
            "provider_results_returned": provider_results_returned,
            "provider_results_filtered": provider_results_filtered,
            "cached_provider_results_filtered": cached_provider_results_filtered,
            "provider_results_by_country": dict(result_by_country),
            "provider_results_by_language": dict(result_by_language),
            "filter_reason_counts": dict(filter_reasons),
            "normalization_rejection_reasons": dict(normalization_reasons),
            "deferred_requests_by_reason": dict(deferred_by_reason),
        },
        "coverage": {
            "new_by_country": dict(country_counts),
            "new_by_search_language": dict(language_counts),
            "new_by_source": dict(source_counts),
            "new_by_opportunity_type": dict(type_counts),
            "new_by_entity_type": dict(entity_type_counts),
            "new_by_entity_state": dict(entity_state_counts),
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
