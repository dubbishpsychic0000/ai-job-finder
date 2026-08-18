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
from app.config import AgentConfig, CandidateProfile
from app.discovery.email_verification import EmailVerificationService
from app.discovery.opportunity_details import classify_opportunity, detect_application_method
from app.scoring.engine import compute_scores

logger = logging.getLogger(__name__)


@dataclass
class AnalysisReport:
    analyzed: list[dict] = field(default_factory=list)
    blocked_by_rules: int = 0
    errors: list[str] = field(default_factory=list)


async def analyze_job(session: Session, job, analyzer: JobAnalyzer, matcher: CandidateMatcher,
                      mobility: MobilityAgent, decision_agent: DecisionAgent,
                      profile: CandidateProfile, config: AgentConfig,
                      target_countries: list[str]) -> dict | None:
    """Analyze one job. Returns summary dict (or None on failure)."""
    existing = mem.store.get_analysis(session, job.id)
    if not existing:
        analysis = await analyzer.analyze(job)
        mem.store.add_analysis(session, job.id, analysis, model_used=analyzer.llm.name)
        session.flush()
    else:
        analysis = existing.raw_json or {}

    # Classification/application routing is deterministic and retained in the
    # job record, independent of whether an LLM is available.
    job.opportunity_type = classify_opportunity(job.title, job.description, job.source_type)
    verification = EmailVerificationService(session).verify_job(job, source_type=job.source_type)
    job.contact_email = verification.email if verification.verified else ""
    job.application_method, job.application_url = detect_application_method(
        text=f"{job.title}\n{job.description}", url=job.url, source_type=job.source_type,
        has_verified_email=verification.verified)

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
    session.flush()

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
                       llm: LLMProvider, target_countries: list[str]) -> AnalysisReport:
    report = AnalysisReport()
    analyzer = JobAnalyzer(llm)
    matcher = CandidateMatcher(llm, profile)
    mobility = MobilityAgent(llm)
    decision_agent = DecisionAgent(llm, config, profile=profile)

    jobs = mem.store.get_jobs_by_status(session, ["new"])
    for job in jobs:
        try:
            summary = await analyze_job(session, job, analyzer, matcher, mobility,
                                        decision_agent, profile, config, target_countries)
            if summary:
                report.analyzed.append(summary)
        except Exception as exc:
            logger.exception("Analysis failed for job #%s", job.id)
            report.errors.append(f"#{job.id}: {exc}")
            mem.store.record_event(session, "analysis", f"failed: {exc}", "error", {"job_id": job.id})
    return report
