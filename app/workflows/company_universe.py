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
    candidate_discovery: OpenCompanyDiscovery | None = None,
) -> CompanyUniverseReport:
    """Ingest externally collected candidates without claiming they are hiring.

    Network adapters are deliberately called by the orchestration layer with
    bounded queries; this function only owns persistence and Phase 1 handoff.
    """
    report = CompanyUniverseReport()
    discovery = candidate_discovery or OpenCompanyDiscovery()
    discovery_cfg = config.discovery or {}
    if candidates is None:
        candidates = []
        sources = discovery_cfg.get("company_universe_sources", [])
        if not isinstance(sources, list):
            report.errors.append("company_universe_sources must be a list")
            sources = []
        elif not sources:
            report.errors.append(
                "company-universe discovery is enabled but company_universe_sources is empty"
            )
        if len(sources) > 10:
            report.errors.append("company-universe source list truncated to the 10-source limit")
        for source in sources[:10]:
            if not isinstance(source, dict):
                report.errors.append("ignored malformed company-universe source configuration")
                continue
            kind = source.get("kind")
            try:
                if kind == "osm":
                    collected = discovery.osm(
                        overpass_url=source["overpass_url"],
                        query=source["query"],
                        country=source.get("country", ""),
                        target_terms=source.get("target_terms", []),
                    )
                elif kind == "wikidata":
                    collected = discovery.wikidata(
                        endpoint=source["endpoint"],
                        sparql=source["sparql"],
                        country=source.get("country", ""),
                        target_terms=source.get("target_terms", []),
                    )
                else:
                    report.errors.append(f"unsupported company-universe source kind: {kind}")
                    continue
                candidates.extend(collected)
            except Exception as exc:
                report.errors.append(f"{kind or 'unknown'} source failed: {exc}")
    try:
        max_candidates = max(1, min(500, int(discovery_cfg.get(
            "company_universe_max_candidates", 100))))
        minimum_relevance = float(discovery_cfg.get("company_universe_min_relevance", 0))
    except (TypeError, ValueError) as exc:
        report.errors.append(f"invalid company-universe limit configuration: {exc}")
        return report
    if len(candidates) > max_candidates:
        report.errors.append(
            f"company candidate list truncated from {len(candidates)} to {max_candidates}"
        )
    candidates = candidates[:max_candidates]
    report.candidates = len(candidates)
    try:
        report.stored, report.duplicates = discovery.persist(
            session, candidates, minimum_relevance=minimum_relevance,
        )
    except Exception as exc:
        report.errors.append(f"company candidate persistence failed: {exc}")

    if researcher is not None:
        from app import models
        companies = session.query(models.Company).all()
        for candidate in candidates:
            company = next(
                (c for c in companies
                 if c.normalized_name == candidate.name.lower().strip()),
                None,
            )
            if company and candidate.website:
                try:
                    researcher.research_and_apply(
                        candidate.website, company_id=company.id,
                        official_domain=company.official_domain,
                    )
                    report.researched += 1
                except Exception as exc:
                    report.errors.append(f"website research failed for {candidate.name}: {exc}")
    return report
