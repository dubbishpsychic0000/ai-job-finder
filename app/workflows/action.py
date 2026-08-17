"""Action workflow — executes APPLY / ASK_EMPLOYER decisions through the
ApplicationEngine (email + safety gate), and INVESTIGATE decisions through
immigration research. IGNORE/HOLD need no action.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app import memory as mem
from app.agents.communication_agent import CommunicationAgent
from app.agents.llm import LLMProvider
from app.config import AgentConfig, CandidateProfile, RunnerSettings
from app.email.service import ApplicationEngine

logger = logging.getLogger(__name__)


@dataclass
class ActionReport:
    applied: list[dict] = field(default_factory=list)
    asked: list[dict] = field(default_factory=list)
    investigated: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def pending_actions(session: Session) -> list:
    """Jobs decided as APPLY/ASK_EMPLOYER/INVESTIGATE but not yet acted on."""
    from sqlalchemy import select

    from app import models

    decided = select(models.Decision.job_id).where(
        models.Decision.decision.in_(["APPLY", "ASK_EMPLOYER", "INVESTIGATE"]))
    acted = select(models.Job.id).where(models.Job.status.in_(["acted", "closed"]))
    return list(session.execute(
        select(models.Job).where(
            models.Job.status == "analyzed",
            models.Job.id.in_(decided),
            models.Job.id.not_in(acted),
        )
    ).scalars().all())


def lang_for_country(country: str) -> str:
    return {
        "France": "fr", "Belgium": "fr", "Canada": "fr", "Quebec": "fr",
        "Germany": "de", "Netherlands": "nl", "Spain": "es", "Portugal": "pt",
    }.get(country, "en")


async def run_actions(session: Session, config: AgentConfig, settings: RunnerSettings,
                      profile: CandidateProfile, llm: LLMProvider,
                      immigration_agent=None) -> ActionReport:
    report = ActionReport()
    communicator = CommunicationAgent(llm, profile)
    # §20 — re-verify a posting is still live before emailing (opt-in via config;
    # kept OFF by default so tests and dry-runs stay offline).
    verify_fn = _verify_before_send(config) if (config.email or {}).get("verify_url_before_send", False) else None
    engine = ApplicationEngine(session, config, settings, profile, communicator,
                               verify_url_fn=verify_fn)
    jobs = pending_actions(session)

    for job in jobs:
        decision = mem.store.get_last_decision(session, job.id)
        if not decision:
            continue
        try:
            lang = lang_for_country(job.country)
            if decision.decision in ("APPLY", "ASK_EMPLOYER"):
                to_addr = job.contact_email
                if not to_addr:
                    report.blocked.append({"job_id": job.id, "reason": "no contact email"})
                    mem.store.record_event(session, "action", "no contact email — cannot email",
                                           "warn", {"job_id": job.id})
                    continue
                result = await engine.run(job, decision, decision.decision, to_addr, lang)
                target = report.applied if decision.decision == "APPLY" else report.asked
                target.append({"job_id": job.id, **result})
                if result["status"] == "blocked":
                    report.blocked.append({"job_id": job.id, "reason": result.get("report", result.get("reason"))})
            elif decision.decision == "INVESTIGATE":
                research = {}
                if immigration_agent:
                    research = await immigration_agent.research(
                        job.country or (job.location or "").split(",")[-1].strip(), job.title)
                    _record_programs(session, research, job)
                report.investigated.append({"job_id": job.id, "research": research})
                job.status = "acted"
                mem.store.record_event(session, "action", f"investigated #{job.id}",
                                       "info", {"job_id": job.id})
        except Exception as exc:
            logger.exception("Action failed for job #%s", job.id)
            report.errors.append(f"#{job.id}: {exc}")
            mem.store.record_event(session, "action", f"failed: {exc}", "error", {"job_id": job.id})
    return report


def _verify_before_send(config: AgentConfig):
    """Live-page checker for §20 before generating an employer email."""

    def _check(url: str) -> bool:
        import requests

        try:
            resp = requests.get(url, headers={"User-Agent": "WorldwideCareerAgent/0.1 (verifier)"},
                                timeout=20, allow_redirects=True)
            return resp.ok and bool(resp.text.strip())
        except Exception:
            return False

    return _check


def _record_programs(session: Session, research: dict, job) -> None:
    if research.get("status") != "verified":
        mem.store.record_event(session, "immigration", research.get("reason", "unverified"),
                               "warn", {"job_id": job.id})
        return
    from app.models import ImmigrationProgram, utcnow

    for prog in research.get("programs", []):
        already = session.query(ImmigrationProgram).filter_by(
            country=research.get("country"), program=prog.get("program", "")).first()
        if already:
            continue
        session.add(ImmigrationProgram(
            country=research.get("country", ""),
            program=prog.get("program", ""),
            occupation=research.get("occupation", ""),
            eligibility=prog.get("eligibility", ""),
            language_requirements=prog.get("language_level", ""),
            work_experience=prog.get("experience_years", ""),
            occupation_restrictions=prog.get("restrictions", ""),
            claim=research.get("claims", [{}])[0].get("claim", "") if research.get("claims") else "",
            official_source_url=research.get("source_url", ""),
            verified_at=utcnow(),
        ))
    mem.store.record_event(session, "immigration",
                           f"verified {len(research.get('programs', []))} program(s) for {research.get('country')}",
                           "info", {"job_id": job.id})
