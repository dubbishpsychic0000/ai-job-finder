"""Persist vacancy-independent employer candidates and hand them to research."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import AgentConfig
from app.discovery.company_universe import CompanyCandidate, OpenCompanyDiscovery


@dataclass
class CompanyUniverseReport:
    candidates: int = 0
    stored: int = 0
    duplicates: int = 0
    researched: int = 0
    errors: list[str] = field(default_factory=list)


async def run_company_universe_discovery(
    session: Session,
    config: AgentConfig,
    *,
    candidates: list[CompanyCandidate] | None = None,
    researcher=None,
) -> CompanyUniverseReport:
    """Ingest externally collected candidates without claiming they are hiring.

    Network adapters are deliberately called by the orchestration layer with
    bounded queries; this function only owns persistence and Phase 1 handoff.
    """
    report = CompanyUniverseReport(candidates=len(candidates or []))
    discovery = OpenCompanyDiscovery()
    try:
        report.stored, report.duplicates = discovery.persist(session, candidates or [])
        if researcher is not None:
            from app import models
            for candidate in candidates or []:
                company = next(
                    (c for c in session.query(models.Company).all()
                     if c.normalized_name == candidate.name.lower().strip()),
                    None,
                )
                if company and candidate.website:
                    researcher.research_and_apply(
                        candidate.website, company_id=company.id,
                        official_domain=company.official_domain,
                    )
                    report.researched += 1
    except Exception as exc:
        report.errors.append(str(exc))
    return report
