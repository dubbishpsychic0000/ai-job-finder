"""Opportunity-source discovery workflow (spec §6) — search-engine expansion.

Runs the OpportunitySourceDiscoveryEngine and persists every surfaced source
(career pages, agencies, government/immigration/shortage/sponsorship pages,
international-hiring announcements) into `opportunity_sources`. Opt-in via
`discovery.opportunity_source_discovery`; `search_fn` is injectable so tests
stay hermetic.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app import memory as mem
from app.config import AgentConfig, CandidateProfile, Preferences
from app.discovery.opportunity_sources import OpportunitySourceDiscoveryEngine

SOURCE = "opportunity_source_discovery"


@dataclass
class OpportunitySourceReport:
    discovered: int = 0
    stored: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    source_errors: list[str] = field(default_factory=list)


async def run_opportunity_source_discovery(session: Session, config: AgentConfig,
                                           prefs: Preferences, *,
                                           profile: CandidateProfile | None = None,
                                           search_fn=None,
                                           limit_per_country: int | None = None) -> OpportunitySourceReport:
    dcfg = config.discovery or {}
    limit = limit_per_country or int(dcfg.get("opportunity_sources_per_country", 5))
    engine = OpportunitySourceDiscoveryEngine(profile=profile, prefs=prefs,
                                              search_fn=search_fn,
                                              max_per_country=limit)
    report = OpportunitySourceReport()
    candidates = await engine.discover_prefs()
    report.discovered = len(candidates)

    for cand in candidates:
        _, created = mem.store.upsert_opportunity_source(
            session,
            kind=cand.kind, url=cand.url, title=cand.title, country=cand.country,
            source=SOURCE, sponsorship_signal=cand.sponsorship_signal,
            international_recruitment_signal=cand.international_recruitment_signal,
            notes=cand.notes)
        if created:
            report.stored += 1
        report.by_kind[cand.kind] = report.by_kind.get(cand.kind, 0) + 1

    if report.stored:
        mem.store.record_event(
            session, SOURCE,
            f"stored {report.stored} opportunity sources "
            f"({', '.join(f'{k}:{v}' for k, v in report.by_kind.items())})",
            "info", {"discovered": report.discovered})
    return report
