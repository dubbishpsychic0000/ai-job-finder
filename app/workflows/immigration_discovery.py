"""Immigration & work-pathway discovery workflow (spec §11, §13).

Runs the ImmigrationDiscoveryEngine over the configured official sources and
persists every claim into `immigration_facts` (with source evidence), alongside
a deterministic candidate-fit flag and a country priority list. Called from
discovery when `discovery.immigration_discovery` is enabled; all network access
is injectable so tests stay hermetic.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import memory as mem
from app.config import ROOT_DIR, AgentConfig, CandidateProfile, Preferences, load_yaml
from app.discovery.immigration import (
    ImmigrationDiscoveryEngine,
    ImmigrationDiscoveryReport,
    candidate_occupations,
)

SOURCE = "immigration_discovery"


async def run_immigration_discovery(session: Session, config: AgentConfig,
                                    prefs: Preferences, *,
                                    profile: CandidateProfile | None = None,
                                    sources_path=None, engine=None,
                                    limit_per_country: int | None = None) -> ImmigrationDiscoveryReport:
    dcfg = config.discovery or {}
    path = sources_path or ROOT_DIR / dcfg.get(
        "immigration_sources_path", "config/immigration_sources.yaml")
    cfg = load_yaml(path)
    srclist = cfg.get("immigration_sources", [])

    occupations = candidate_occupations(profile=profile, prefs=prefs)
    eng = engine or ImmigrationDiscoveryEngine(
        countries=prefs.countries, occupations=occupations, sources=srclist)
    facts = await eng.discover(limit_per_country=limit_per_country or int(
        dcfg.get("immigration_facts_per_country", 25)))

    report = ImmigrationDiscoveryReport(
        facts_discovered=len(facts),
        rejected_unofficial=eng.rejected_unofficial(),
    )
    for fact in facts:
        matched, occ = eng.match_occupation(fact)
        fact.matched = matched
        if matched and not fact.occupation:
            fact.occupation = occ
        _, created = mem.store.upsert_immigration_fact(
            session,
            country=fact.country, program=fact.program, fact_type=fact.fact_type,
            claim=fact.claim, source_url=fact.source_url,
            source_domain=fact.source_domain, source_name=fact.source_name,
            confidence=fact.confidence, occupation=fact.occupation,
            matched=fact.matched, retrieved_at=fact.retrieved_at,
        )
        report.stored += int(created)
        report.matched += int(matched)

    report.priority_countries = eng.priority_countries(facts)
    if report.facts_discovered:
        mem.store.record_event(
            session, SOURCE,
            f"stored {report.stored} facts ({report.matched} matched; "
            f"{report.rejected_unofficial} non-official rejected)",
            "info", {"priority_countries": [c for c, _ in report.priority_countries]})
    return report
