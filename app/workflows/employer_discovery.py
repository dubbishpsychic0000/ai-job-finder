"""Employer discovery workflow (spec §7, §9).

Runs the EmployerDiscoveryEngine, verifies each careers page, and persists a
candidate-relevant company universe (employers + recruitment agencies) into the
`companies` table. Called from discovery when `discovery.employer_discovery` is
enabled; all network access is injectable so tests stay hermetic.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import memory as mem
from app.config import AgentConfig, CandidateProfile, Preferences
from app.discovery.employers import EmployerDiscoveryEngine, EmployerReport

SOURCE = "employer_discovery"


async def run_employer_discovery(session: Session, config: AgentConfig,
                                 prefs: Preferences, *, profile: CandidateProfile | None = None,
                                 search_fn=None, fetch_html=None,
                                 limit_per_country: int | None = None) -> EmployerReport:
    dcfg = config.discovery or {}
    dimit = limit_per_country or int(dcfg.get("employer_targets_per_country", 5))
    engine = EmployerDiscoveryEngine(profile=profile, prefs=prefs,
                                     search_fn=search_fn, fetch_html=fetch_html)
    report = EmployerReport()
    candidates = await engine.discover_prefs(max_per_country=dimit)
    report.discovered = len(candidates)

    for cand in candidates:
        verdict = await engine.verify(cand.careers_url)
        if not verdict.verified:
            report.unverified += 1
            continue
        report.verified += 1
        company = mem.store.get_or_create_company(
            session, cand.name or "Unknown", cand.careers_url, cand.country,
            careers_url=cand.careers_url, recruitment_url=cand.recruitment_url,
            industry=cand.industry, source=SOURCE,
            sponsorship_signal=cand.sponsorship_signal,
            international_recruitment_signal=cand.international_recruitment_signal)
        if verdict.ats:
            company.notes = (company.notes + f"\nATS: {verdict.ats}").strip()
        report.stored += 1

    if report.stored:
        mem.store.record_event(
            session, SOURCE,
            f"stored {report.stored} employers/agencies "
            f"({report.verified} verified, {report.unverified} unverified)",
            "info", {"discovered": report.discovered})
    return report
