"""Classify persisted jobs for a reviewed, single-job email application."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app import memory as mem
from app import models
from app.agents.decision_agent import DecisionAgent
from app.config import AgentConfig, CandidateProfile, Preferences, RunnerSettings
from app.discovery.opportunity_details import is_verified_individual_vacancy
from app.email import gmail_oauth
from app.email import service as email_service
from app.scheduler.control import is_paused

READY_TO_SEND = "READY_TO_SEND"
NEEDS_REVIEW = "NEEDS_REVIEW"
PORTAL_APPLICATION = "PORTAL_APPLICATION"
BLOCKED = "BLOCKED"
_DUPLICATE_STATUSES = (
    "sent", "inquiry_sent", "drafted", "deferred", "replied", "interview",
    "offer", "rejected",
)
_ACTIVE_SEND_STATES = ("prepared", "dispatching", "uncertain", "confirmed_sent")
_OFFICIAL_RECIPIENT_METHODS = {
    "official_employer_posting",
    "official_page_published_alternate_recruitment_domain",
}
_RECONCILIATION_SCOPE = gmail_oauth.GMAIL_READONLY_SCOPE


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(
        timezone.utc,
    )


def verification_matches_job(job, evidence, *, now: datetime | None = None) -> bool:
    now = _as_utc(now or datetime.now(timezone.utc))
    observed_at = _as_utc(getattr(evidence, "observed_at", None))
    if (
        getattr(evidence, "outcome", "") != "OPEN"
        or getattr(evidence, "reason_code", "")
        != "matching_public_ats_posting_record"
        or getattr(evidence, "evidence_url", "") != job.url
        or observed_at is None
        or not timedelta(0) <= now - observed_at <= timedelta(minutes=5)
    ):
        return False
    def normalize(value):
        return " ".join(str(value or "").casefold().split())
    company = getattr(job, "company", None)
    if not all((
        normalize(job.title) == normalize(getattr(evidence, "title", "")),
        normalize(getattr(company, "name", "")) == normalize(
            getattr(evidence, "employer", ""),
        ),
        normalize(job.location) == normalize(getattr(evidence, "location", "")),
        normalize(job.description) == normalize(getattr(evidence, "description", "")),
        str(job.application_url or "") == str(
            getattr(evidence, "application_url", ""),
        ),
    )):
        return False
    closing_at = _as_utc(getattr(evidence, "closing_at", None))
    return not (
        closing_at is not None and closing_at <= now
    )


def _verified_recipient(session: Session, job) -> dict | None:
    email = str(getattr(job, "contact_email", "") or "").strip().casefold()
    if not email:
        return None
    verification = mem.store.get_verified_email(session, job.id)
    if (
        verification is None
        or not verification.verified
        or verification.email.strip().casefold() != email
        or verification.source_url != job.url
        or verification.verification_method not in _OFFICIAL_RECIPIENT_METHODS
        or verification.confidence < 95
        or verification.verified_at is None
    ):
        return None
    return {
        "email": email,
        "source_url": verification.source_url,
        "source_domain": verification.source_domain,
        "method": verification.verification_method,
        "confidence": verification.confidence,
        "verified_at": _as_utc(verification.verified_at).isoformat(),
    }


def _row(category: str, reason: str, job, *, recipient: dict | None = None) -> dict:
    company = getattr(job, "company", None)
    return {
        "category": category,
        "reason": reason,
        "job_id": mem.store.opportunity_id(job),
        "employer": getattr(company, "name", "") or "Unknown",
        "title": str(getattr(job, "title", "") or ""),
        "location": str(getattr(job, "location", "") or ""),
        "official_source": str(getattr(job, "url", "") or ""),
        "deadline": (
            _as_utc(getattr(job, "closing_at", None)).isoformat()
            if getattr(job, "closing_at", None) else None
        ),
        "recipient": recipient["email"] if recipient else "",
        "recipient_evidence": recipient,
        "application_route": {
            "type": str(getattr(job, "application_method", "") or "UNKNOWN"),
            "url": str(
                getattr(job, "application_url", "")
                or getattr(job, "url", "")
                or ""
            ),
            "verified": False,
        },
    }


def _config_limit(config: AgentConfig, name: str, default: int) -> int:
    email = config.email if isinstance(config.email, dict) else {}
    rules = config.rules if isinstance(config.rules, dict) else {}
    return int(email.get(name, rules.get(name, default)))


def _profile_eligibility_issue(
    job, analysis, profile: CandidateProfile, preferences: Preferences,
) -> tuple[str, str] | None:
    """Fail closed when required candidate facts are missing or do not match."""
    import unicodedata

    competition = getattr(job, "public_competition", None)
    competition = competition if isinstance(competition, dict) else {}
    requirements = " ".join((
        str(getattr(job, "description", "") or ""),
        str(getattr(analysis, "education_required", "") or ""),
        str(competition.get("qualification_requirements", "")),
        str(competition.get("eligibility_requirements", "")),
    )).casefold()
    if any(term in requirements for term in (
        "moroccan national", "moroccan nationality", "nationalité marocaine",
        "nationalité marocaine obligatoire", "marocain obligatoire",
        "nationalite marocaine", "marocain obligatoire",
        "citizenship of morocco", "nationality: moroccan",
    )):
        nationality = str(getattr(profile, "nationality", "") or "").strip().casefold()
        if not nationality:
            return NEEDS_REVIEW, "required_nationality_not_in_profile"
        if nationality not in {"moroccan", "marocain", "marocaine", "morocco", "maroc"}:
            return BLOCKED, "required_nationality_mismatch"

    education_requirement = " ".join((
        str(getattr(analysis, "education_required", "") or ""),
        str(competition.get("qualification_requirements", "")),
    )).strip().casefold()
    if education_requirement:
        education = " ".join(
            str(value)
            for entry in getattr(profile, "education", [])
            for value in entry.values()
        ).casefold()
        if not education:
            return NEEDS_REVIEW, "required_qualification_not_in_profile"
        def normalized(value: str) -> str:
            folded = unicodedata.normalize("NFKD", value.casefold())
            return " ".join(
                "".join(char for char in folded if not unicodedata.combining(char))
                .replace("/", " ").split()
            )

        required = normalized(education_requirement)
        candidate = normalized(education)
        degree_families = (
            ("technicien specialise", "technician specialized"),
            ("conducteur des travaux", "construction supervisor"),
            ("gros oeuvre", "structural work"),
            ("bachelor", "licence"),
            ("master",),
            ("engineer", "ingenieur"),
            ("architect", "architecte"),
        )
        accepted_degrees = [
            family for family in degree_families
            if any(term in required for term in family)
        ]
        has_matching_degree = (
            not accepted_degrees
            or any(
                any(term in candidate for term in family)
                for family in accepted_degrees
            )
        )
        field_terms = ("genie civil", "civil engineering")
        required_fields = [term for term in field_terms if term in required]
        has_matching_field = (
            not required_fields
            or any(term in candidate for term in required_fields)
        )
        if not (
            has_matching_degree
            and has_matching_field
            and (
                accepted_degrees or required_fields
                or candidate in required or required in candidate
            )
        ):
            return NEEDS_REVIEW, "required_qualification_match_unconfirmed"

    minimum_experience = getattr(analysis, "experience_min", None)
    if minimum_experience is not None:
        years = getattr(profile, "experience_years", None)
        if years is None:
            return NEEDS_REVIEW, "required_experience_not_in_profile"
        if years < minimum_experience:
            return BLOCKED, "required_experience_not_met"

    country_targets = {
        str(country).casefold() for country in getattr(preferences, "target_countries", [])
    }
    job_country = str(getattr(job, "country", "") or "").casefold()
    if country_targets and job_country and job_country not in country_targets:
        relocation_willing = getattr(profile, "relocation", {}).get("willing")
        if relocation_willing is None:
            return NEEDS_REVIEW, "location_eligibility_unconfirmed"
        if relocation_willing is not True:
            return BLOCKED, "location_preferences_not_met"

    permit_requirement = any(term in requirements for term in (
        "work permit required", "work authorization required",
        "visa sponsorship required", "autorisation de travail obligatoire",
        "must have the right to work", "right to work in",
        "authorized to work", "authorised to work",
        "work visa required", "visa required",
        "permis de travail obligatoire", "autorisation de travail",
    ))
    if permit_requirement and not getattr(profile, "work_authorization", {}):
        return NEEDS_REVIEW, "work_authorization_requirement_unresolved"
    return None


def classify_application(
    session: Session,
    job,
    *,
    config: AgentConfig,
    settings: RunnerSettings,
    profile: CandidateProfile,
    preferences: Preferences,
    now: datetime | None = None,
    live_evidence=None,
) -> dict:
    """Return one sanitized queue item; never sends, drafts, or writes."""
    now = _as_utc(now or datetime.now(timezone.utc))
    company = getattr(job, "company", None)
    item = _row(BLOCKED, "", job)
    item["application_route"]["verified"] = is_verified_individual_vacancy(job)

    competition = getattr(job, "public_competition", None)
    is_competition = (
        getattr(job, "source", "") == "emploi_public_ma"
        or (
            isinstance(competition, dict)
            and competition.get("source") == "emploi_public_ma"
        )
    )
    if is_competition:
        route = competition.get("submission_route") or {}
        if (
            competition.get("status") != "OPEN_ANNOUNCEMENT"
            or not is_verified_individual_vacancy(job)
            or route.get("kind") != "ONLINE_PORTAL"
            or not str(route.get("destination_url") or getattr(job, "application_url", ""))
            or getattr(job, "application_method", "") != "COMPANY_PORTAL"
        ):
            item.update(category=BLOCKED, reason="verified_portal_route_missing")
            return item
        item["application_route"]["verified"] = True
    if getattr(job, "opportunity_type", "") == "SPONTANEOUS_APPLICATION":
        item.update(category=BLOCKED, reason="not_a_job_vacancy_application")
        return item
    if getattr(job, "closing_at", None) and _as_utc(job.closing_at) <= now:
        item.update(category=BLOCKED, reason="application_deadline_elapsed")
        return item
    if not is_verified_individual_vacancy(job):
        item.update(category=BLOCKED, reason="official_individual_vacancy_not_verified")
        return item
    item["application_route"]["verified"] = True

    if getattr(job, "status", "") in {"acted", "closed", "ignored"}:
        item.update(category=BLOCKED, reason="job_already_processed")
        return item
    if session.scalars(
        select(models.LiveSendAttempt.id).where(
            models.LiveSendAttempt.job_id == job.id,
            models.LiveSendAttempt.state.in_(_ACTIVE_SEND_STATES),
        ).limit(1),
    ).first():
        item.update(category=BLOCKED, reason="live_send_recovery_required")
        return item
    if mem.store.find_applications(session, job.id, statuses=_DUPLICATE_STATUSES):
        item.update(category=BLOCKED, reason="duplicate_application_or_existing_outbound")
        return item

    decision = mem.store.get_last_decision(session, job.id)
    analysis = mem.store.get_analysis(session, job.id)
    if is_competition:
        eligibility_issue = _profile_eligibility_issue(
            job, analysis, profile, preferences,
        )
        if eligibility_issue:
            item.update(category=eligibility_issue[0], reason=eligibility_issue[1])
            return item
        item.update(
            category=PORTAL_APPLICATION,
            reason="official_application_requires_portal",
        )
        item["eligibility_assessment"] = (
            dict(competition.get("eligibility_assessment") or {})
        )
        return item
    if decision is None or analysis is None:
        item.update(category=NEEDS_REVIEW, reason="candidate_job_eligibility_not_assessed")
        return item
    if decision.decision != "APPLY":
        item.update(
            category=BLOCKED if decision.decision in {"IGNORE", "HOLD"} else NEEDS_REVIEW,
            reason=f"latest_candidate_decision_{decision.decision.casefold()}",
        )
        return item
    minimum_score = float(config.rules.get("min_application_score", 80))
    minimum_confidence = float(config.rules.get("min_application_confidence", 0.80))
    confidence = round(min(0.95, 0.5 + (decision.overall_score / 100.0) * 0.5), 2)
    if decision.overall_score < minimum_score or confidence < minimum_confidence:
        item.update(category=BLOCKED, reason="application_score_or_confidence_below_policy")
        return item
    hard_block, hard_rules = DecisionAgent(
        llm=None, config=config, profile=profile, preferences=preferences,
    ).hard_rules(session, job, decision.overall_score, config.threshold_for(decision.overall_score))
    if hard_block:
        item.update(
            category=NEEDS_REVIEW if hard_block == "HOLD" else BLOCKED,
            reason="candidate_eligibility:" + ",".join(hard_rules),
        )
        return item
    eligibility_issue = _profile_eligibility_issue(job, analysis, profile, preferences)
    if eligibility_issue:
        item.update(category=eligibility_issue[0], reason=eligibility_issue[1])
        return item

    if is_competition or getattr(job, "application_method", "") == "COMPANY_PORTAL":
        item.update(
            category=PORTAL_APPLICATION,
            reason="official_application_requires_portal",
        )
        item["application_route"]["verified"] = True
        return item
    if getattr(job, "application_method", "") != "EMAIL":
        item.update(category=NEEDS_REVIEW, reason="explicit_email_application_route_missing")
        return item

    recipient = _verified_recipient(session, job)
    if recipient is None:
        item.update(category=NEEDS_REVIEW, reason="official_job_specific_recipient_evidence_missing")
        return item
    item.update(recipient=recipient["email"], recipient_evidence=recipient)
    if live_evidence is not None:
        item["application_route"]["verified"] = bool(
            verification_matches_job(job, live_evidence, now=now)
            and getattr(live_evidence, "application_method", "") == "EMAIL"
            and str(getattr(live_evidence, "application_email", "")).strip().casefold()
            == recipient["email"]
        )
    else:
        last_verified = _as_utc(getattr(job, "last_verified_at", None))
        item["application_route"]["verified"] = bool(
            last_verified is not None
            and timedelta(0) <= now - last_verified <= timedelta(minutes=5)
            and getattr(job, "verification_status", "") == "verified"
        )
    if not item["application_route"]["verified"]:
        item.update(category=NEEDS_REVIEW, reason="fresh_official_posting_recheck_required")
        return item

    if not str(getattr(job, "description", "") or "").strip() or not company:
        item.update(category=NEEDS_REVIEW, reason="employer_or_posting_details_incomplete")
        return item
    if is_paused():
        item.update(category=BLOCKED, reason="global_emergency_pause_enabled")
        return item

    if settings.email_mode != "live":
        item.update(category=NEEDS_REVIEW, reason="EMAIL_MODE_must_be_live_for_direct_send")
        return item
    if not settings.enable_email:
        item.update(category=NEEDS_REVIEW, reason="ENABLE_EMAIL_must_be_true_for_direct_send")
        return item
    if settings.email_provider != "gmail":
        item.update(category=NEEDS_REVIEW, reason="EMAIL_PROVIDER_must_be_gmail")
        return item
    token_path = gmail_oauth.token_path(settings)
    if not token_path.is_file():
        item.update(category=NEEDS_REVIEW, reason="gmail_oauth_token_missing")
        return item
    granted = gmail_oauth.granted_scopes(settings)
    required = {
        gmail_oauth.GMAIL_COMPOSE_SCOPE,
        gmail_oauth.GMAIL_SEND_SCOPE,
        _RECONCILIATION_SCOPE,
    }
    missing = sorted(required - granted)
    if missing:
        item.update(
            category=NEEDS_REVIEW,
            reason="gmail_scopes_missing:" + ",".join(
                scope.rsplit("/", 1)[-1] for scope in missing
            ),
        )
        return item

    from app.workflows.action import lang_for_country

    cv = email_service.resolve_attachment(
        Path("candidate") / "cv",
        lang_for_country(str(getattr(job, "country", "") or "")),
    )
    if not cv or not Path(cv).is_file():
        item.update(category=BLOCKED, reason="verified_cv_missing")
        return item
    try:
        with Path(cv).open("rb") as stream:
            valid_pdf = stream.read(5) == b"%PDF-"
    except OSError:
        valid_pdf = False
    if not valid_pdf:
        item.update(category=BLOCKED, reason="cv_is_not_a_readable_pdf")
        return item

    daily_total = mem.store.count_dispatched_today(session, now=now)
    daily_applications = mem.store.count_dispatched_today(
        session, now=now, action="APPLY",
    )
    if daily_total >= _config_limit(config, "max_daily_outbound", settings.daily_max_outbound):
        item.update(category=BLOCKED, reason="daily_combined_outbound_cap_reached")
        return item
    if daily_applications >= _config_limit(
        config, "max_daily_applications", settings.daily_max_applications,
    ):
        item.update(category=BLOCKED, reason="daily_application_cap_reached")
        return item
    if mem.store.recent_contact(
        session, recipient["email"], days=int(config.rules.get("follow_up_days", [7])[0]),
    ):
        item.update(category=BLOCKED, reason="recipient_cooldown_active")
        return item
    cooldown_days = int(settings.employer_cooldown_days)
    if company.id and cooldown_days > 0 and mem.store.recent_company_contact(
        session, company.id, days=cooldown_days,
    ):
        item.update(category=BLOCKED, reason="employer_cooldown_active")
        return item

    item.update(
        category=READY_TO_SEND,
        reason=(
            "standing_authorization_preconditions_verified"
            if getattr(settings, "auto_send_verified_applications", False)
            else "preconditions_verified_exact_owner_confirmation_required"
        ),
    )
    return item


def application_queue(
    session: Session,
    *,
    config: AgentConfig,
    settings: RunnerSettings,
    profile: CandidateProfile,
    preferences: Preferences,
) -> list[dict]:
    jobs = session.scalars(
        select(models.Job).options(joinedload(models.Job.company))
        .where(models.Job.opportunity_type != "SPONTANEOUS_APPLICATION")
        .order_by(models.Job.discovered_at.desc(), models.Job.id.desc()),
    ).all()
    return [
        classify_application(
            session, job, config=config, settings=settings,
            profile=profile, preferences=preferences,
        )
        for job in jobs
    ]
