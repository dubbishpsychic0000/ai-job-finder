"""Analysis workflow — runs the AI Brain over every new job.

Pipeline per job (spec §23): extract requirements -> match profile -> research
international fit -> score -> decide. New 'new' jobs get processed; each step is
persisted so a crash mid-run can resume without redoing work.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app import memory as mem
from app.agents.candidate_matcher import CandidateMatcher
from app.agents.decision_agent import DecisionAgent
from app.agents.job_analyzer import JobAnalyzer
from app.agents.llm import LLMProvider
from app.agents.mobility_agent import MobilityAgent
from app.config import AgentConfig, CandidateProfile, Preferences
from app.discovery.email_verification import EmailVerificationService
from app.discovery.opportunity_details import (
    classify_opportunity,
    detect_application_method,
    is_verified_individual_vacancy,
)
from app.scoring.engine import compute_scores

logger = logging.getLogger(__name__)


@dataclass
class AnalysisReport:
    analyzed: list[dict] = field(default_factory=list)
    blocked_by_rules: int = 0
    blocked_unverified: int = 0
    errors: list[str] = field(default_factory=list)


async def analyze_job(session: Session, job, analyzer: JobAnalyzer, matcher: CandidateMatcher,
                      mobility: MobilityAgent, decision_agent: DecisionAgent,
                      profile: CandidateProfile, config: AgentConfig,
                      target_countries: list[str]) -> dict | None:
    """Analyze one job. Returns summary dict (or None on failure)."""
    if job.opportunity_type == "SPONTANEOUS_APPLICATION":
        job.status = "outreach_only"
        return None
    if not is_verified_individual_vacancy(job):
        job.status = "verification_required"
        mem.store.record_event(
            session,
            "analysis",
            f"#{job.id} held before eligibility analysis: vacancy evidence is not verified",
            "warn",
            {"job_id": job.id, "reason_code": "vacancy_not_verified"},
        )
        return None
    competition = getattr(job, "public_competition", None)
    if (
        job.source == "emploi_public_ma"
        and isinstance(competition, dict)
        and competition.get("source") == "emploi_public_ma"
    ):
        requirements = competition.get("requirements_original") or {}
        checklist = {
            name: {
                "requirement_from_accessible_notice": str(requirements.get(name) or ""),
                "candidate_evidence_status": "UNKNOWN",
                "review_status": (
                    "OWNER_REVIEW_REQUIRED"
                    if requirements.get(name) else "NOT_STATED_ON_ACCESSIBLE_DETAIL_PAGE"
                ),
            }
            for name in ("diploma", "specialty", "experience", "nationality", "age")
        }
        competition["eligibility_checklist"] = {
            "status": "OWNER_REVIEW_REQUIRED",
            "candidate_eligibility": "NOT_ASSESSED",
            "items": checklist,
            "notice_documents_not_fetched": bool(
                competition.get("notice_documents_not_fetched"),
            ),
            "submission_owner_approval": "REQUIRED_NOT_GRANTED",
        }
        job.public_competition = competition
        job.application_method = "UNKNOWN"
        job.application_url = ""
        summary = {
            "public_competition": competition["eligibility_checklist"],
            "source_evidence_url": competition.get("evidence_url", ""),
            "candidate_eligibility": "NOT_ASSESSED",
        }
        mem.store.add_analysis(
            session, job.id, summary, model_used="deterministic_public_competition",
        )
        mem.store.add_decision(
            session,
            job.id,
            "HOLD",
            0,
            {},
            "Official competition requires owner review of eligibility requirements "
            "and explicit competition-specific submission approval.",
            ["public_competition_owner_review_required"],
        )
        job.status = "analyzed"
        mem.store.record_event(
            session,
            "analysis",
            f"#{job.id} held for public competition owner review",
            "info",
            {"job_id": job.id, "reason_code": "public_competition_owner_review_required"},
        )
        return {
            "job_id": job.id,
            "title": job.title,
            "decision": "HOLD",
            "overall": 0,
            "scores": {},
            "country": job.country,
        }
    existing = mem.store.get_analysis(session, job.id)
    if not existing:
        analysis = await analyzer.analyze(job)
        mem.store.add_analysis(session, job.id, analysis, model_used=analyzer.llm.name)
    else:
        analysis = existing.raw_json or {}

    # Classification/application routing is deterministic and retained in the
    # job record, independent of whether an LLM is available.
    job.opportunity_type = classify_opportunity(job.title, job.description, job.source_type)
    verification = EmailVerificationService(session).verify_job(job, source_type=job.source_type)
    job.contact_email = verification.email if verification.verified else ""
    job.application_method, job.application_url = detect_application_method(
        text=f"{job.title}\n{job.description}", url=job.url, source_type=job.source_type,
        has_verified_email=verification.verified,
        contact_email=job.contact_email)

    match = await matcher.match(job, analysis)
    mobility_res = await mobility.analyze(job, analysis)

    dimensions, overall = compute_scores(
        matcher=match, mobility=mobility_res, source=job.source, config=config)

    band = config.threshold_for(overall)
    context = {
        "sponsorship_ok": mobility_res.get("foreign_applicants_ok", True),
        "job": job.id,
    }
    result = await decision_agent.decide(session, job, overall, dimensions, band, context)

    mem.store.add_decision(session, job.id, result.decision, result.overall,
                           result.dimensions, result.reason, result.rules_fired,
                           result.ai_reason)
    job.status = "analyzed"

    mem.store.record_event(session, "analysis",
                           f"#{job.id} {job.title[:40]} -> {result.decision} ({overall:.0f}%)",
                           "info", {"job_id": job.id, "decision": result.decision})
    event = "INTERNSHIP_FOUND" if job.opportunity_type == "INTERNSHIP" else "JOB_RANKED"
    priority = "high" if overall >= 90 else "normal"
    mem.store.enqueue_notification(session, event, job_id=job.id, priority=priority,
                                   payload={"score": overall, "opportunity_id": mem.store.opportunity_id(job)})
    return {
        "job_id": job.id,
        "title": job.title,
        "decision": result.decision,
        "overall": overall,
        "scores": dimensions,
        "contact_email": job.contact_email,
        "country": job.country,
    }


async def run_analysis(session: Session, config: AgentConfig, profile: CandidateProfile,
                       llm: LLMProvider, target_countries: list[str],
                       preferences: Preferences | None = None) -> AnalysisReport:
    report = AnalysisReport()
    analyzer = JobAnalyzer(llm)
    matcher = CandidateMatcher(llm, profile)
    mobility = MobilityAgent(llm)
    decision_agent = DecisionAgent(
        llm, config, profile=profile, preferences=preferences,
    )

    jobs = [
        job for job in mem.store.get_jobs_by_status(session, ["new"])
        if job.opportunity_type != "SPONTANEOUS_APPLICATION"
    ]
    for job in jobs:
        try:
            summary = await analyze_job(
                session, job, analyzer, matcher, mobility, decision_agent,
                profile, config, target_countries,
            )
            if summary:
                report.analyzed.append(summary)
            elif job.status == "verification_required":
                report.blocked_unverified += 1
        except Exception as exc:
            logger.exception("Analysis failed for job #%s", job.id)
            report.errors.append(f"#{job.id}: {exc}")
            mem.store.record_event(session, "analysis", f"failed: {exc}", "error", {"job_id": job.id})
    return report
