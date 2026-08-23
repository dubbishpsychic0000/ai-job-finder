"""Memory layer — the only place that talks to the database.

Everything the pipeline needs to remember and check (jobs, decisions,
applications, contacts, sources, events) lives here. Keeping repositories
in one module makes the state machine and tests easy to follow.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.models import (
    Application,
    Company,
    Contact,
    Decision,
    Email,
    EmailVerification,
    Event,
    ImmigrationFact,
    ImmigrationProgram,
    Job,
    JobAnalysis,
    Notification,
    OpportunitySource,
    QueryStat,
    Source,
    utcnow,
)


def record_event(session: Session, type: str, message: str, level: str = "info", data: dict | None = None) -> None:
    session.add(Event(type=type, level=level, message=message, data=data or {}))


# ------------------------- companies -------------------------
def get_or_create_company(session: Session, name: str, url: str = "", country: str = "",
                          *, careers_url: str = "", recruitment_url: str = "", industry: str = "",
                          source: str = "", sponsorship_signal: str = "",
                          international_recruitment_signal: str = "") -> Company:
    norm = _normalize_name(name)
    if not norm:
        norm = "unknown"
    existing = session.execute(select(Company).where(Company.normalized_name == norm)).scalar_one_or_none()
    if existing:
        if careers_url and not existing.careers_url:
            existing.careers_url = careers_url
        if recruitment_url and not existing.recruitment_url:
            existing.recruitment_url = recruitment_url
        if industry and not existing.industry:
            existing.industry = industry
        if source and not existing.source:
            existing.source = source
        if sponsorship_signal and existing.sponsorship_signal == "unknown":
            existing.sponsorship_signal = sponsorship_signal
        if international_recruitment_signal and existing.international_recruitment_signal == "unknown":
            existing.international_recruitment_signal = international_recruitment_signal
        existing.last_checked_at = utcnow()
        return existing
    c = Company(name=name, normalized_name=norm, website=url, country=country,
                careers_url=careers_url, recruitment_url=recruitment_url, industry=industry,
                source=source,
                sponsorship_signal=sponsorship_signal or "unknown",
                international_recruitment_signal=international_recruitment_signal or "unknown",
                last_checked_at=utcnow())
    session.add(c)
    return c


def _normalize_name(name: str) -> str:
    import unicodedata

    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c))
    return " ".join(n.lower().split())[:255]


# ------------------------- jobs -------------------------
def find_job_by_key(session: Session, dedup_key: str) -> Job | None:
    return session.execute(select(Job).where(Job.dedup_key == dedup_key)).scalar_one_or_none()


def find_similar_job(session: Session, title: str, company_norm: str, limit: int = 5) -> list[Job]:
    """Best-effort fuzzy match when exact dedup_key misses."""
    stmt = select(Job).where(Job.company_id.is_not(None)).limit(0)
    # Cheap n-gram prefix containment on title; good enough for MVP.
    from sqlalchemy import or_

    like = f"%{title.lower()[:24]}%"
    stmt = select(Job).where(or_(Job.title.ilike(like), Job.dedup_key.ilike(f"%{company_norm[:12]}%"))).limit(limit)
    return list(session.execute(stmt).scalars().all())


def upsert_job(session: Session, data: dict[str, Any]) -> tuple[Job, bool]:
    key = data["dedup_key"]
    job = find_job_by_key(session, key)
    if job:
        return job, False
    job = Job(**{k: v for k, v in data.items() if hasattr(Job, k)})
    session.add(job)
    session.flush()  # needed to get job.id for opportunity_id
    return job, True


def get_jobs_by_status(session: Session, statuses: Iterable[str]) -> list[Job]:
    return list(session.execute(select(Job).where(Job.status.in_(list(statuses)))).scalars().all())


def get_job(session: Session, job_id: int) -> Job | None:
    return session.get(Job, job_id)


def opportunity_id(job: Job) -> str:
    """Stable, user-facing ID suitable for `wca job JOB-YYYY-MMDD-NNNN`."""
    discovered = job.discovered_at or utcnow()
    return f"JOB-{discovered:%Y-%m%d}-{job.id:04d}"


def get_job_by_opportunity_id(session: Session, value: str) -> Job | None:
    import re
    match = re.fullmatch(r"JOB-\d{4}-\d{4}-(\d+)", (value or "").strip(), re.I)
    return get_job(session, int(match.group(1))) if match else None


# ------------------------- analysis / decisions -------------------------
def add_analysis(session: Session, job_id: int, analysis: dict[str, Any], model_used: str = "") -> JobAnalysis:
    a = JobAnalysis(job_id=job_id, model_used=model_used, raw_json=analysis, **{
        k: v for k, v in analysis.items() if hasattr(JobAnalysis, k)
    })
    session.add(a)
    return a


def get_analysis(session: Session, job_id: int) -> JobAnalysis | None:
    return session.execute(select(JobAnalysis).where(JobAnalysis.job_id == job_id)).scalar_one_or_none()


def add_decision(session: Session, job_id: int, decision: str, overall_score: float,
                 scores: dict[str, float], reason: str, rules_fired: list[str], ai_reason: str = "") -> Decision:
    d = Decision(job_id=job_id, decision=decision, overall_score=overall_score, scores=scores,
                 reason=reason, rules_fired=rules_fired, ai_reason=ai_reason)
    session.add(d)
    return d


def get_last_decision(session: Session, job_id: int) -> Decision | None:
    return session.execute(
        select(Decision).where(Decision.job_id == job_id).order_by(Decision.id.desc())
    ).scalar_one_or_none()


# ------------------------- applications -------------------------
def add_application(session: Session, job_id: int, decision_id: int, action: str,
                    score: float, contact_email: str = "") -> Application:
    app = Application(job_id=job_id, decision_id=decision_id, action=action, score=score,
                      contact_email=contact_email, status="draft")
    session.add(app)
    session.flush()
    return app


def get_application(session: Session, app_id: int) -> Application | None:
    return session.get(Application, app_id)


def find_applications(session: Session, job_id: int,
                      statuses: tuple[str, ...] | None = None) -> list[Application]:
    stmt = select(Application).where(Application.job_id == job_id)
    if statuses:
        stmt = stmt.where(Application.status.in_(statuses))
    return list(session.execute(stmt).scalars().all())


def applications_due_for_followup(session: Session, now: datetime | None = None) -> list[Application]:
    now = now or utcnow()
    return list(session.execute(
        select(Application).where(
            Application.status.in_(["sent", "replied"]),
            Application.follow_up_at.is_not(None),
            Application.follow_up_at <= now,
            Application.follow_ups_sent < _max_followups(),
        ).order_by(Application.follow_up_at)
    ).scalars().all())


def _max_followups() -> int:
    from app.config import get_config

    return int(get_config().rules.get("max_follow_ups_per_application", 2))


def count_sent_today(session: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    start = now - timedelta(days=1)
    return len(list(session.execute(
        select(Email).where(Email.status == "sent", Email.sent_at >= start)
    ).scalars().all()))


def count_dispatched_today(session: Session, now: datetime | None = None,
                           action: str | None = None,
                           statuses: tuple[str, ...] = ("sent", "drafted")) -> int:
    """Outbound communications actually created today (sent or drafted).

    `dry_run` is deliberately excluded: nothing left the safety boundary, so it
    must not consume the daily outbound budget.
    """
    from sqlalchemy import func

    now = now or utcnow()
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.count()).select_from(Application).where(
        Application.status.in_(statuses),
        Application.sent_at.is_not(None),
        Application.sent_at >= start,
    )
    if action:
        stmt = stmt.where(Application.action == action)
    return int(session.execute(stmt).scalar_one() or 0)


def count_applications_today(session: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    start = now - timedelta(days=1)
    stmt = select(Application).where(
        Application.sent_at.is_not(None), Application.sent_at >= start)
    return len(list(session.execute(stmt).scalars().all()))


# ------------------------- emails -------------------------
def add_email(session: Session, application_id: int | None, to_addr: str, subject: str, body: str,
              attachments: list[str] | None = None, status: str = "draft", mode: str = "") -> Email:
    e = Email(application_id=application_id, to_addr=to_addr, subject=subject, body=body,
              attachments=attachments or [], status=status, mode=mode)
    session.add(e)
    session.flush()
    return e


# ------------------------- email verification -------------------------
def add_email_verification(session: Session, *, job_id: int | None, email: str,
                           source_url: str = "", source_domain: str = "",
                           verified: bool = False, verification_method: str = "",
                           confidence: int = 0) -> EmailVerification:
    existing = session.execute(select(EmailVerification).where(
        EmailVerification.job_id == job_id, EmailVerification.email == email.lower()
    )).scalar_one_or_none()
    now = utcnow()
    if existing:
        existing.source_url = source_url or existing.source_url
        existing.source_domain = source_domain or existing.source_domain
        existing.verified = verified
        existing.verification_method = verification_method or existing.verification_method
        existing.confidence = max(existing.confidence, confidence)
        existing.verified_at = now if verified else existing.verified_at
        return existing
    row = EmailVerification(job_id=job_id, email=email.lower(), source_url=source_url,
                            source_domain=source_domain, verified=verified,
                            verification_method=verification_method, confidence=confidence,
                            verified_at=now if verified else None)
    session.add(row)
    session.flush()
    return row


def get_verified_email(session: Session, job_id: int) -> EmailVerification | None:
    return session.execute(select(EmailVerification).where(
        EmailVerification.job_id == job_id, EmailVerification.verified.is_(True)
    ).order_by(EmailVerification.confidence.desc(), EmailVerification.id.desc())).scalars().first()


# ------------------------- notification queue --------------------------
def enqueue_notification(session: Session, event_type: str, *, job_id: int | None = None,
                         priority: str = "normal", payload: dict | None = None) -> Notification:
    row = Notification(event_type=event_type, job_id=job_id, priority=priority,
                       payload=payload or {})
    session.add(row)
    return row


def queued_notifications(session: Session, *, priorities: tuple[str, ...] | None = None) -> list[Notification]:
    stmt = select(Notification).where(Notification.status == "queued").order_by(Notification.created_at, Notification.id)
    if priorities:
        stmt = stmt.where(Notification.priority.in_(priorities))
    return list(session.execute(stmt).scalars().all())


# ------------------------- contacts -------------------------
def recent_contact(session: Session, email: str, days: int = 14) -> Contact | None:
    cutoff = utcnow() - timedelta(days=days)
    return session.execute(
        select(Contact).where(Contact.email == email, Contact.last_contacted_at >= cutoff)
    ).scalar_one_or_none()


def recent_company_contact(session: Session, company_id: int | None, days: int = 7) -> Contact | None:
    if not company_id:
        return None
    cutoff = utcnow() - timedelta(days=days)
    return session.execute(
        select(Contact).where(Contact.company_id == company_id, Contact.last_contacted_at >= cutoff)
    ).scalar_one_or_none()


def upsert_contact(session: Session, email: str, person_name: str = "", role: str = "",
                   source: str = "", company_id: int | None = None) -> Contact:
    c = session.execute(select(Contact).where(Contact.email == email)).scalar_one_or_none()
    if not c:
        c = Contact(email=email, person_name=person_name, role=role, source=source, company_id=company_id)
        session.add(c)
        session.flush()
    c.last_contacted_at = utcnow()
    if not c.first_contacted_at:
        c.first_contacted_at = c.last_contacted_at
    return c


# ------------------------- immigration facts -------------------------
def upsert_immigration_fact(session: Session, *, country: str, program: str,
                            fact_type: str, claim: str, source_url: str,
                            source_domain: str = "", source_name: str = "",
                            confidence: int = 100, occupation: str = "",
                            matched: bool = False,
                            retrieved_at: datetime | None = None) -> tuple[ImmigrationFact, bool]:
    """Store one §11 fact; idempotent on the (country, program, claim) triple."""
    existing = session.execute(
        select(ImmigrationFact).where(
            ImmigrationFact.country == country,
            ImmigrationFact.program == program,
            ImmigrationFact.claim == claim,
        )
    ).scalars().first()
    if existing:
        return existing, False
    fact = ImmigrationFact(
        country=country, program=program, fact_type=fact_type, claim=claim,
        source_url=source_url, source_domain=source_domain or _domain(source_url),
        source_name=source_name, confidence=confidence, occupation=occupation,
        matched=matched, retrieved_at=retrieved_at or utcnow(),
    )
    session.add(fact)
    session.flush()
    return fact, True


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc.lower()


# ------------------------- opportunity sources (spec §6) --------------------
def upsert_opportunity_source(session: Session, *, kind: str, url: str, title: str = "",
                              country: str = "", source: str = "",
                              sponsorship_signal: str = "unknown",
                              international_recruitment_signal: str = "unknown",
                              notes: str = "") -> tuple[OpportunitySource, bool]:
    existing = session.execute(
        select(OpportunitySource).where(OpportunitySource.url == url)
    ).scalars().first()
    if existing:
        existing.last_checked_at = utcnow()
        existing.notes = (existing.notes + f" | {notes}").strip(" |") if notes else existing.notes
        return existing, False
    row = OpportunitySource(
        kind=kind, url=url, title=title[:512], country=country, source=source,
        sponsorship_signal=sponsorship_signal,
        international_recruitment_signal=international_recruitment_signal,
        notes=notes, last_checked_at=utcnow())
    session.add(row)
    session.flush()
    return row, True


def mark_job_freshness(session: Session, job: Job, freshness: str = "",
                       last_verified_at: datetime | None = None) -> None:
    if freshness:
        job.freshness = freshness
    if last_verified_at is not None:
        job.last_verified_at = last_verified_at
        job.verification_status = "verified"
    elif last_verified_at is None and freshness == "stale":
        job.verification_status = "stale"


def count_stale_jobs(session: Session, now: datetime | None = None) -> int:
    from app.discovery.verification import freshness_label
    from app.models import Job as _Job

    now = now or utcnow()
    stale = 0
    for job in session.execute(select(_Job)).scalars().all():
        if freshness_label(job.posted_at, now) == "stale":
            stale += 1
    return stale


# ------------------------- query learning (spec §24/§31) --------------------
def get_query_stat(session: Session, query: str, country: str = "", source: str = "") -> QueryStat | None:
    return session.execute(
        select(QueryStat).where(QueryStat.query == query, QueryStat.country == country,
                                QueryStat.source == source)
    ).scalar_one_or_none()


def aggregate_query_stat(session: Session, query: str, country: str = "",
                         sources: set[str] | None = None) -> QueryStat | None:
    """Source-agnostic learning view of a (query, country): sums the ledger rows.

    Discovery records one row per (query, country, connector source); the
    adaptive scheduler (§24/§31) should learn from the *aggregate* outcome, not
    a single source's view.
    """
    stmt = select(QueryStat).where(QueryStat.query == query, QueryStat.country == country)
    if sources is not None:
        stmt = stmt.where(QueryStat.source.in_(sources))
    rows = list(session.execute(stmt).scalars().all())
    if not rows:
        return None
    merged = QueryStat(query=query, country=country)
    merged.jobs_found = sum(r.jobs_found for r in rows)
    merged.relevant_jobs = sum(r.relevant_jobs for r in rows)
    merged.applications = sum(r.applications for r in rows)
    merged.responses = sum(r.responses for r in rows)
    merged.interviews = sum(r.interviews for r in rows)
    merged.runs = sum(r.runs for r in rows)
    merged.last_run_at = max((r.last_run_at for r in rows if r.last_run_at), default=None)
    return merged


def record_query_outcome(session: Session, query: str, country: str = "", source: str = "",
                         applications: int = 0, responses: int = 0, interviews: int = 0) -> None:
    """Accumulate post-discovery outcomes (§24) on a (query, country, source) row.

    Used when applications get recorded for a job that discovery found via that
    query; employers' replies/interviews land on the same counter when present.
    No-op when discovery has no ledger row for the combo (nothing to track).
    """
    stat = get_query_stat(session, query, country, source)
    if not stat:
        return
    stat.applications += max(0, applications)
    stat.responses += max(0, responses)
    stat.interviews += max(0, interviews)


def record_query(session: Session, query: str, country: str = "", source: str = "",
                 jobs_found: int = 0, relevant_jobs: int = 0) -> QueryStat:
    stat = get_query_stat(session, query, country, source)
    if not stat:
        stat = QueryStat(query=query, country=country, source=source,
                         jobs_found=0, relevant_jobs=0, applications=0,
                         responses=0, interviews=0, runs=0)
        session.add(stat)
    stat.jobs_found += jobs_found
    stat.relevant_jobs += relevant_jobs
    stat.runs += 1
    stat.last_run_at = utcnow()
    return stat


def best_queries(session: Session, min_runs: int = 1, limit: int = 50) -> list[QueryStat]:
    return list(session.execute(
        select(QueryStat).where(QueryStat.runs >= min_runs)
        .order_by(QueryStat.relevant_jobs.desc()).limit(limit)
    ).scalars().all())


# ------------------------- sources -------------------------
def get_source(session: Session, name: str) -> Source | None:
    return session.execute(select(Source).where(Source.name == name)).scalar_one_or_none()


def upsert_source(session: Session, name: str, kind: str, base_url: str = "") -> Source:
    s = get_source(session, name)
    if not s:
        s = Source(name=name, kind=kind, base_url=base_url)
        session.add(s)
    return s


def mark_source_fetched(session: Session, source: Source, items: int, error: str = "") -> None:
    now = utcnow()
    source.last_fetch_at = now
    source.items_found = items
    source.last_error = error
    if error:  # §27 — triage failures for connector health
        source.last_failure_at = now
        lowered = error.lower()
        source.rate_limit_status = (
            "limited" if any(k in lowered for k in ("rate limit", "429", "too many")) else "error")
    else:
        source.last_success_at = now
        source.rate_limit_status = "ok"


# ------------------------- stats (dashboard) -------------------------
def stats(session: Session) -> dict[str, Any]:
    now = utcnow()
    start = now - timedelta(days=1)

    def _count(stmt) -> int:
        return len(list(session.execute(stmt).scalars().all()))

    return {
        "new_opportunities": _count(select(models.Job).where(models.Job.discovered_at >= start)),
        "applications_today": count_applications_today(session, now),
        "emails_sent_today": count_sent_today(session, now),
        "employers_contacted": _count(select(Contact)),
        "immigration_programs": _count(select(ImmigrationProgram)),
        "immigration_facts": _count(select(ImmigrationFact)),
        "opportunity_sources": _count(select(OpportunitySource)),
        "stale_jobs": count_stale_jobs(session),
        "total_jobs": _count(select(models.Job)),
        "total_emails": _count(select(Email)),
        "last_events": [
            {"type": e.type, "level": e.level, "message": e.message, "at": e.occurred_at.isoformat()}
            for e in session.execute(select(Event).order_by(Event.id.desc()).limit(10)).scalars().all()
        ],
    }
