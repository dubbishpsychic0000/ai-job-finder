"""Deterministic opportunity classification and application-route detection.

These helpers intentionally make conservative claims.  They are used before
an LLM's optional interpretation so a job-board link never becomes a made-up
email application route.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

OPPORTUNITY_TYPES = {
    "JOB", "INTERNSHIP", "APPRENTICESHIP", "TRAINING", "RECRUITMENT_EVENT",
    "RECRUITER_OPPORTUNITY", "IMMIGRATION_OPPORTUNITY", "SCHOLARSHIP", "GRADUATE_PROGRAM",
}
APPLICATION_METHODS = {
    "EMAIL", "ONLINE_FORM", "COMPANY_PORTAL", "JOB_BOARD", "RECRUITER",
    "PHONE", "WHATSAPP", "IN_PERSON", "UNKNOWN",
}
ENTITY_TYPES = {
    "INDIVIDUAL_VACANCY",
    "EMPLOYER_CAREERS_PAGE",
    "JOB_BOARD_CATEGORY",
    "OCCUPATIONAL_REFERENCE",
    "SALARY_INFORMATION",
    "FORUM_DISCUSSION",
    "RECRUITMENT_EVENT",
    "TRAINING_PROGRAM",
    "UNKNOWN",
}
ENTITY_STATES = {
    "VACANCY_VERIFIED",
    "VACANCY_UNVERIFIED",
    "VACANCY_CLOSED",
    "VACANCY_STATUS_UNKNOWN",
    "RESEARCH_REQUIRED",
    "REJECTED_NON_VACANCY",
}

_TYPE_PATTERNS = (
    ("IMMIGRATION_OPPORTUNITY", r"\b(immigration|visa|work permit|permis de travail)\b"),
    ("SCHOLARSHIP", r"\b(scholarship|bourse)\b"),
    ("GRADUATE_PROGRAM", r"\b(graduate program(?:me)?|programme jeune diplômé|jeune diplômé|recent graduates?)\b"),
    ("APPRENTICESHIP", r"\b(apprenticeship|apprenti(?:ssage)?)\b"),
    ("INTERNSHIP", r"\b(internship|intern\b|stage|stagiaire|pfe|afstudeerstage|praktikum|prácticas|estágio|تدريب)\b"),
    ("TRAINING", r"\b(training|formation|bootcamp|cours)\b"),
    ("RECRUITMENT_EVENT", r"\b(job fair|recruitment event|forum emploi|salon emploi)\b"),
    ("RECRUITER_OPPORTUNITY", r"\b(recruiter|recruitment agency|cabinet de recrutement|intérim)\b"),
)
_NON_VACANCY_PATTERNS = (
    (
        "SALARY_INFORMATION",
        r"\b(?:salary|salaries|wage|pay)\b"
        r"|\b(?:salaire|salaires|rémunération|lohn|salaris)\b",
        "salary_reference",
    ),
    (
        "OCCUPATIONAL_REFERENCE",
        r"\b(?:occupational outlook|career profile|occupation profile|"
        r"what does .{1,60} do|career information|job description)\b"
        r"|/(?:marketreport|occupation|occupational-outlook|salary|research|"
        r"career-options)/",
        "occupational_reference",
    ),
    (
        "UNKNOWN",
        r"\b(?:case look[- ]?up|civil cases?|civil law|"
        r"title vii of the civil rights act|civil protection and humanitarian aid|"
        r"civil infrastructure platform)\b",
        "non_employment_information",
    ),
    (
        "FORUM_DISCUSSION",
        r"\b(?:forum|discussion|reddit|quora|question and answer|q&a|thread)\b",
        "forum_or_discussion",
    ),
    (
        "RECRUITMENT_EVENT",
        r"\b(?:job fair|career fair|recruitment event|forum emploi|salon emploi)\b",
        "recruitment_event",
    ),
    (
        "TRAINING_PROGRAM",
        r"\b(?:training course|formation professionnelle|bootcamp|online course|certification course)\b",
        "training_program",
    ),
)
_DETAIL_PATH_PARTS = {
    "job", "jobs", "vacancy", "vacancies", "position", "positions",
    "offre", "offres", "emploi", "emplois", "opening", "openings",
}
_GENERIC_PATH_PARTS = {
    "job", "jobs", "vacancy", "vacancies", "position", "positions",
    "offre", "offres", "emploi", "emplois", "opening", "openings",
    "careers", "career", "search", "results", "all", "category",
}
_DIRECT_ATS_DOMAINS = {
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever": ("jobs.lever.co",),
    "workday": ("myworkdayjobs.com",),
    "icims": ("icims.com",),
    "smartrecruiters": ("smartrecruiters.com",),
}
_GENERIC_LISTING_TITLES = {
    "career", "careers", "job", "jobs", "job opening", "job opportunity",
    "open position", "open positions", "career opportunities",
    "current openings", "all jobs",
}
_NON_EMPLOYMENT_DOMAINS = {
    "youtube.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
}


def classify_entity(opportunity) -> tuple[str, str, str]:
    """Conservatively classify a result separately from its employment type.

    Official ATS records and the dedicated public-competition adapter may
    provide structured verification. Search-result pages and snippets remain
    unverified even when their URLs appear job-shaped.
    """
    title = str(getattr(opportunity, "title", "") or "")
    url = str(getattr(opportunity, "url", "") or "")
    raw = getattr(opportunity, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    competition = raw.get("public_competition")
    if isinstance(competition, dict):
        status = str(competition.get("status") or "UNKNOWN")
        if status == "OPEN_ANNOUNCEMENT":
            return (
                "INDIVIDUAL_VACANCY",
                "VACANCY_VERIFIED" if competition.get("verification_status") == "verified"
                else "VACANCY_UNVERIFIED",
                str(competition.get("reason_code") or "public_competition_unverified"),
            )
        if status == "CLOSED_ANNOUNCEMENT":
            return (
                "INDIVIDUAL_VACANCY",
                "VACANCY_CLOSED",
                str(competition.get("reason_code") or "public_competition_closed"),
            )
        if status in {
            "EXAM_CONVOCATION", "RESULTS_PUBLICATION", "CANCELLATION",
            "OTHER_PROCEDURAL_NOTICE",
        }:
            return (
                "UNKNOWN",
                "RESEARCH_REQUIRED",
                f"public_competition_{status.casefold()}",
            )
        return (
            "INDIVIDUAL_VACANCY",
            "VACANCY_STATUS_UNKNOWN",
            str(competition.get("reason_code") or "public_competition_status_unknown"),
        )
    try:
        parsed_url = urlsplit(url)
    except ValueError:
        return "UNKNOWN", "RESEARCH_REQUIRED", "invalid_listing_url"
    path_parts = [
        part.casefold() for part in parsed_url.path.split("/") if part
    ]
    text = f"{title}\n{' '.join(path_parts)}".casefold()

    for entity_type, pattern, reason in _NON_VACANCY_PATTERNS:
        if (
            entity_type == "OCCUPATIONAL_REFERENCE"
            and re.search(r"\bjob description\b", text, re.I)
            and re.search(r"\b(?:vacancy|opening|hiring|apply now)\b", title, re.I)
        ):
            continue
        if re.search(pattern, text, re.I):
            return entity_type, "REJECTED_NON_VACANCY", reason

    host = (parsed_url.hostname or "").casefold()
    if any(
        host == domain or host.endswith(f".{domain}")
        for domain in _NON_EMPLOYMENT_DOMAINS
    ):
        return "FORUM_DISCUSSION", "REJECTED_NON_VACANCY", "discussion_or_social_page"
    if any(name in host for name in ("reddit.", "quora.", "forum.")):
        return "FORUM_DISCUSSION", "REJECTED_NON_VACANCY", "forum_domain"

    if (
        re.search(
            r"\b(?:jobs?|careers?|open positions|current openings)\s+(?:in|at|with)\b",
            title,
            re.I,
        )
    ):
        return "JOB_BOARD_CATEGORY", "RESEARCH_REQUIRED", "job_category_page"

    if re.search(r"\b(?:\d[\d,]*\s+)?(?:job vacancies|job openings|jobs?)\b", title, re.I):
        host = (parsed_url.hostname or "").casefold()
        if (
            re.search(r"\b(?:\d[\d,]*\s+)?(?:job vacancies|job openings)\b", title, re.I)
            or (
                any(name in host for name in (
                    "bayt.com", "indeed.", "naukri.com", "jobsora.", "emploi.ma",
                ))
                and re.search(r"\bjobs?\b", title, re.I)
            )
        ):
            return "JOB_BOARD_CATEGORY", "RESEARCH_REQUIRED", "job_category_page"

    if re.search(r"\bfree job post\b|\bpost (?:a )?job\b", title, re.I):
        return "JOB_BOARD_CATEGORY", "RESEARCH_REQUIRED", "job_board_landing_page"

    if re.search(r"/(?:q-[^/]+|jobs/search|job-search|search/jobs)(?:\.html)?$", url, re.I):
        return "JOB_BOARD_CATEGORY", "RESEARCH_REQUIRED", "job_search_results_page"

    if path_parts and path_parts[-1] in {"careers", "career", "jobs", "openings"}:
        if getattr(opportunity, "effective_source_type", lambda: "")() in {
            "company_career", "employer",
        }:
            return "EMPLOYER_CAREERS_PAGE", "RESEARCH_REQUIRED", "employer_careers_page"
        return "JOB_BOARD_CATEGORY", "RESEARCH_REQUIRED", "job_category_page"

    if re.search(r"\bjob vacancy\s*:", title, re.I):
        return (
            "INDIVIDUAL_VACANCY",
            "VACANCY_UNVERIFIED",
            "specific_listing_url_unverified",
        )

    direct_ats_url = _supported_ats_posting_url(url)
    if direct_ats_url and title.strip() and title.casefold().strip() not in _GENERIC_LISTING_TITLES:
        return (
            "INDIVIDUAL_VACANCY",
            "VACANCY_UNVERIFIED",
            "ats_detail_requires_live_evidence",
        )

    ats_name = str(raw.get("ats") or "").casefold()
    external_id = str(getattr(opportunity, "external_id", "") or "")
    company = str(getattr(opportunity, "company", "") or "").strip()
    source = str(getattr(opportunity, "source", "") or "")
    configured_slug = source.split(":", 1)[-1].casefold()
    company_is_source_slug = company.casefold().replace(" ", "-") == configured_slug
    official_ats_host = any(
        host == domain or host.endswith(f".{domain}")
        for domain in _DIRECT_ATS_DOMAINS.get(ats_name, ())
    )
    direct_ats_record = (
        getattr(opportunity, "effective_source_type", lambda: "")() == "ats"
        and official_ats_host
        and getattr(opportunity, "verification_status", "verified") == "verified"
        and bool(external_id)
        and not external_id.casefold().startswith(("http://", "https://"))
        and bool(company)
        and not company_is_source_slug
        and bool(str(getattr(opportunity, "location", "") or "").strip())
        and bool(title.strip())
        and title.casefold().strip() not in _GENERIC_LISTING_TITLES
        and bool(url)
    )
    if direct_ats_record:
        return (
            "INDIVIDUAL_VACANCY",
            "VACANCY_UNVERIFIED",
            "ats_record_requires_current_evidence",
        )

    if _has_detail_path(path_parts) or re.search(
        r"(?:req(?:uisition)?[-_ ]?\d{3,}|job[-_ ]?\d{3,})",
        "/".join(path_parts),
        re.I,
    ):
        return "INDIVIDUAL_VACANCY", "VACANCY_UNVERIFIED", "specific_listing_url_unverified"

    return "UNKNOWN", "RESEARCH_REQUIRED", "insufficient_vacancy_evidence"


def _has_detail_path(path_parts: list[str]) -> bool:
    for index, part in enumerate(path_parts[:-1]):
        if part in _DETAIL_PATH_PARTS and path_parts[index + 1] not in _GENERIC_PATH_PARTS:
            return True
    return False


def _supported_ats_posting_url(url: str) -> bool:
    from app.discovery.vacancy_verification import supported_ats_detail

    return supported_ats_detail(url) is not None


def is_verified_individual_vacancy(job) -> bool:
    """Require source-specific provenance before any downstream analysis."""
    url = str(getattr(job, "entity_evidence_url", "") or "")
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    official_ats_host = any(
        host == domain or host.endswith(f".{domain}")
        for domains in _DIRECT_ATS_DOMAINS.values()
        for domain in domains
    )
    observed_at = getattr(job, "last_verified_at", None)
    now = None
    if observed_at is not None:
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_seconds = (now - observed_at).total_seconds()
        if age_seconds > 86400 or age_seconds < -300:
            return False
    closing_at = getattr(job, "closing_at", None)
    if closing_at is not None:
        if closing_at.tzinfo is None:
            closing_at = closing_at.replace(tzinfo=timezone.utc)
        if closing_at <= (now or datetime.now(timezone.utc)):
            return False
    competition = getattr(job, "public_competition", None)
    if (
        getattr(job, "source", "") == "emploi_public_ma"
        and isinstance(competition, dict)
    ):
        route = competition.get("submission_route")
        if not isinstance(route, dict):
            return False
        route_kind = str(route.get("kind") or "")
        route_url = str(route.get("destination_url") or "")
        try:
            route_parts = urlsplit(route_url)
            valid_route_url = bool(
                route_parts.scheme == "https"
                and route_parts.hostname
                and route_parts.username is None
                and route_parts.password is None
                and route_parts.port in {None, 443}
            )
        except ValueError:
            valid_route_url = False
        route_is_supported = (
            route_kind == "ONLINE_PORTAL"
            and valid_route_url
            and getattr(job, "application_method", "") == "COMPANY_PORTAL"
            and str(getattr(job, "application_url", "") or "")
            == route_url
        ) or (
            route_kind == "EMAIL"
            and bool(str(route.get("destination_email") or ""))
            and getattr(job, "application_method", "") == "EMAIL"
        ) or (
            route_kind == "POSTAL"
            and bool(str(route.get("destination_raw") or ""))
            and getattr(job, "application_method", "") == "UNKNOWN"
        )
        metadata_observed = competition.get("observed_at")
        try:
            metadata_observed_at = (
                datetime.fromisoformat(str(metadata_observed))
                if metadata_observed else None
            )
        except ValueError:
            metadata_observed_at = None
        if metadata_observed_at is not None and metadata_observed_at.tzinfo is None:
            metadata_observed_at = metadata_observed_at.replace(tzinfo=timezone.utc)
        return (
            getattr(job, "entity_type", "") == "INDIVIDUAL_VACANCY"
            and getattr(job, "entity_state", "") == "VACANCY_VERIFIED"
            and getattr(job, "entity_reason_code", "")
            == "official_public_competition_verified_open"
            and getattr(job, "source_type", "") == "government"
            and getattr(job, "verification_status", "") == "verified"
            and observed_at is not None
            and metadata_observed_at == observed_at
            and (now - metadata_observed_at).total_seconds() <= 86400
            and str(getattr(job, "url", "") or "") == url
            and parsed.scheme == "https"
            and host in {"emploi-public.ma", "www.emploi-public.ma"}
            and competition.get("source") == "emploi_public_ma"
            and competition.get("stage") == "OPEN_ANNOUNCEMENT"
            and competition.get("status") == "OPEN_ANNOUNCEMENT"
            and not bool(route.get("conflict"))
            and route_is_supported
            and bool(str(getattr(job, "title", "") or "").strip())
            and bool(str(getattr(job, "description", "") or "").strip())
            and bool(str(getattr(job, "location", "") or "").strip())
            and bool(getattr(getattr(job, "company", None), "name", ""))
            and closing_at is not None
            and closing_at > (now or datetime.now(timezone.utc))
            and str(competition.get("evidence_url") or "") == url
        )
    return (
        getattr(job, "entity_type", "") == "INDIVIDUAL_VACANCY"
        and getattr(job, "entity_state", "") == "VACANCY_VERIFIED"
        and getattr(job, "entity_reason_code", "") == "matching_public_ats_posting_record"
        and getattr(job, "source_type", "") == "ats"
        and getattr(job, "verification_status", "") == "verified"
        and observed_at is not None
        and str(getattr(job, "url", "") or "") == url
        and _supported_ats_posting_url(url)
        and bool(str(getattr(job, "title", "") or "").strip())
        and bool(str(getattr(job, "description", "") or "").strip())
        and bool(str(getattr(job, "location", "") or "").strip())
        and bool(getattr(getattr(job, "company", None), "name", ""))
        and getattr(job, "application_method", "") == "COMPANY_PORTAL"
        and str(getattr(job, "application_url", "") or "") == url
        and parsed.scheme in {"http", "https"}
        and official_ats_host
    )


def classify_opportunity(title: str = "", description: str = "", source_type: str = "") -> str:
    text = f"{title}\n{description}".lower()
    for kind, pattern in _TYPE_PATTERNS:
        if re.search(pattern, text, re.I):
            return kind
    return "RECRUITER_OPPORTUNITY" if source_type == "recruitment" else "JOB"


def detect_application_method(*, text: str = "", url: str = "", source_type: str = "",
                              has_verified_email: bool = False) -> tuple[str, str]:
    """Return ``(method, application_url)`` without inventing a contact method."""
    value = text.lower()
    if re.search(r"(?:whatsapp|\bwa\.me/)\b", value):
        return "WHATSAPP", url
    if re.search(r"(?:call|phone|téléphone|tel\.)\s*(?:us|nous)?", value):
        return "PHONE", ""
    if re.search(r"(?:in person|walk[ -]?in|sur place|présentez[- ]vous)", value):
        return "IN_PERSON", ""
    if source_type == "recruitment" or re.search(r"\b(recruiter|cabinet de recrutement|recruitment agency)\b", value):
        return "RECRUITER", url
    if re.search(r"(?:apply online|apply now|postuler(?: en ligne)?|candidater|submit application)", value):
        return ("COMPANY_PORTAL" if source_type in {"ats", "company_career"} else "ONLINE_FORM"), url
    if source_type in {"ats", "company_career"}:
        return "COMPANY_PORTAL", url
    if source_type == "job_board":
        return "JOB_BOARD", url
    if has_verified_email:
        return "EMAIL", ""
    return "UNKNOWN", ""
