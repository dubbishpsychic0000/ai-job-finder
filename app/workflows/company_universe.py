"""Persist vacancy-independent employer candidates and hand them to research."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import AgentConfig
from app.discovery.company_universe import (
    CompanyCandidate,
    OpenCompanyDiscovery,
    _find_existing_company,
    _trusted_website,
    canonical_company_key,
)
from app.discovery.website_researcher import host_domain
from app.models import Company, Evidence


@dataclass
class CompanyUniverseReport:
    candidates: int = 0
    stored: int = 0
    duplicates: int = 0
    researched: int = 0
    companies_discovered: int = 0
    newly_added: int = 0
    already_known: int = 0
    companies_with_official_websites: int = 0
    companies_without_websites: int = 0
    companies_requiring_research: int = 0
    companies_with_recruitment_evidence: int = 0
    companies_with_qualifying_contacts: int = 0
    outreach_ready_companies: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "candidates": self.candidates,
            "discovered": self.companies_discovered,
            "source_records_added": self.stored,
            "newly_added": self.newly_added,
            "already_known": self.already_known,
            "known_official_websites": self.companies_with_official_websites,
            "companies_without_websites": self.companies_without_websites,
            "requiring_research": self.companies_requiring_research,
            "researched": self.researched,
            "companies_with_recruitment_evidence": self.companies_with_recruitment_evidence,
            "companies_with_qualifying_contacts": self.companies_with_qualifying_contacts,
            "outreach_ready_companies": self.outreach_ready_companies,
            "errors": self.errors,
        }


async def run_company_universe_discovery(
    session: Session,
    config: AgentConfig,
    *,
    candidates: list[CompanyCandidate] | None = None,
    researcher=None,
    candidate_discovery: OpenCompanyDiscovery | None = None,
) -> CompanyUniverseReport:
    """Ingest bounded open-source candidates and report outreach readiness."""
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
        candidates = _interleave_candidates(candidates, max_candidates)
    report.candidates = len(candidates)

    unique_candidates = {}
    for candidate in candidates:
        website = _trusted_website(candidate.website)
        domain = host_domain(website) if website else ""
        key = canonical_company_key(candidate.name, domain)
        unique_candidates.setdefault(key, candidate)
    existing_company_ids = set()
    for candidate in unique_candidates.values():
        website = _trusted_website(candidate.website)
        domain = host_domain(website) if website else ""
        existing = _find_existing_company(session, candidate.name, domain)
        if existing:
            existing_company_ids.add(existing.id)

    try:
        report.stored, report.duplicates = discovery.persist(
            session, candidates, minimum_relevance=minimum_relevance,
        )
    except Exception as exc:
        report.errors.append(f"company candidate persistence failed: {exc}")

    companies_by_id = {}
    for candidate in unique_candidates.values():
        website = _trusted_website(candidate.website)
        domain = host_domain(website) if website else ""
        company = _find_existing_company(session, candidate.name, domain)
        if company:
            companies_by_id[company.id] = company
    report.newly_added = sum(
        company_id not in existing_company_ids for company_id in companies_by_id
    )
    report.companies_discovered = len(companies_by_id)
    report.already_known = len(companies_by_id) - report.newly_added
    try:
        research_limit = max(0, min(20, int(
            discovery_cfg.get("company_universe_research_limit", 5)
        )))
    except (TypeError, ValueError) as exc:
        report.errors.append(f"invalid company-universe research limit: {exc}")
        research_limit = 0
    research_queue = sorted(
        companies_by_id.values(),
        key=lambda company: (
            not bool(_trusted_website(company.website)),
            -company.relevance_score,
            company.name.casefold(),
        ),
    )
    research_ids = {
        company.id for company in research_queue[:research_limit]
    }
    evidence_by_company: dict[int, list[Evidence]] = {}
    if companies_by_id:
        for row in (
            session.query(Evidence)
            .filter(Evidence.company_id.in_(list(companies_by_id)))
            .order_by(Evidence.discovered_at.desc())
            .limit(10000)
        ):
            evidence_by_company.setdefault(row.company_id, []).append(row)

    now = datetime.now(timezone.utc)
    for company in companies_by_id.values():
        website = _trusted_website(company.website)
        evidence = evidence_by_company.get(company.id, [])
        if (
            website
            and researcher is not None
            and company.id in research_ids
            and not _research_is_fresh(company, evidence, now)
        ):
            try:
                result = researcher.research_and_apply(
                    website, company_id=company.id,
                    official_domain=company.official_domain,
                )
                if result.visited or result.evidence:
                    company.last_researched_at = datetime.now(timezone.utc)
                    report.researched += 1
                evidence = evidence_by_company[company.id] = (
                    session.query(Evidence)
                    .filter(Evidence.company_id == company.id)
                    .order_by(Evidence.discovered_at.desc())
                    .all()
                )
            except Exception as exc:
                report.errors.append(f"website research failed for {company.name}: {exc}")
        if website:
            report.companies_with_official_websites += 1
        else:
            report.companies_without_websites += 1
        if not website or not _research_is_fresh(company, evidence, now):
            report.companies_requiring_research += 1
        if any(row.field_name in {
            "relevant_page", "recruitment_email", "spontaneous_application"
        } for row in evidence):
            report.companies_with_recruitment_evidence += 1
        has_contact, is_ready = _contact_readiness(company, evidence)
        if has_contact:
            report.companies_with_qualifying_contacts += 1
        if is_ready and _research_is_fresh(company, evidence, now):
            report.outreach_ready_companies += 1
    return report


def _research_is_fresh(company: Company, rows: list[Evidence], now: datetime) -> bool:
    if not company.last_researched_at or not rows:
        return False
    researched_at = company.last_researched_at
    if researched_at.tzinfo is None:
        researched_at = researched_at.replace(tzinfo=timezone.utc)
    return now - researched_at <= timedelta(days=30)


def _contact_readiness(company: Company, rows: list[Evidence]) -> tuple[bool, bool]:
    if not company.official_domain:
        return False, False
    from app.workflows.btp_outreach import _evidence_facts, _qualifying_contact

    contact = _qualifying_contact(
        _evidence_facts(rows),
        company.official_domain.lower().removeprefix("www.").rstrip("."),
    )
    return bool(contact), bool(contact and _trusted_website(company.website))
def _interleave_candidates(
    candidates: list[CompanyCandidate], limit: int,
) -> list[CompanyCandidate]:
    by_source: dict[str, list[CompanyCandidate]] = {}
    for candidate in candidates:
        by_source.setdefault(candidate.source, []).append(candidate)
    sources = list(by_source.values())
    return [
        sources[source_index][item_index]
        for item_index in range(max(map(len, sources), default=0))
        for source_index in range(len(sources))
        if item_index < len(sources[source_index])
    ][:limit]
