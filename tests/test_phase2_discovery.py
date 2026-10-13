from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app import memory as mem
from app.config import ROOT_DIR, AgentConfig, load_yaml
from app.connectors.linkedin import LinkedInJobsSource
from app.discovery.company_universe import (
    CompanyCandidate,
    OpenCompanyDiscovery,
    _trusted_website,
    canonical_company_key,
)
from app.discovery.website_researcher import ResearchResult
from app.models import Company, Discovery, Evidence
from app.workflows import btp_outreach
from app.workflows.btp_outreach import run_btp_outreach
from app.workflows.company_universe import (
    _interleave_candidates,
    _research_is_due,
    _research_is_fresh,
    run_company_universe_discovery,
)


def test_linkedin_index_captures_posts_without_direct_fetch():
    seen = []

    async def search_fn(query, location):
        seen.append(query)
        return [{
            "url": "https://www.linkedin.com/posts/recruiter_hiring-123",
            "title": "Nous recrutons technicien génie civil",
            "snippet": "Envoyez votre CV à recrutement@acme.ma",
        }]

    result = asyncio.run(LinkedInJobsSource(search_fn=search_fn).search("civil", "Morocco"))
    assert result[0].source_type == "recruitment_post"
    assert "linkedin.com/posts" in seen[0]
    assert "recrutement@acme.ma" in result[0].description


def test_open_company_adapters_and_persistence(db, config):
    def fetch(url, *, params):
        if "overpass" in url:
            return {"elements": [{"tags": {
                "name": "Atlas BTP", "industry": "construction",
                "website": "https://atlas-btp.ma",
            }}]}
        return {"results": {"bindings": [{
            "name": {"value": "Atlas BTP"},
            "industryLabel": {"value": "construction"},
            "website": {"value": "https://atlas-btp.ma"},
            "entity": {"value": "https://www.wikidata.org/entity/Q1"},
        }]}}

    discovery = OpenCompanyDiscovery(fetch_json=fetch)
    candidates = discovery.osm(
        overpass_url="https://overpass.test/api", query="[out:json];",
        country="Morocco", target_terms=["construction"],
    )
    candidates += discovery.wikidata(
        endpoint="https://wikidata.test/sparql", sparql="SELECT",
        country="Morocco", target_terms=["construction"],
    )
    stored, duplicates = discovery.persist(db, candidates)
    assert stored == 2
    assert duplicates == 1
    company = db.query(Company).one()
    assert company.official_domain == "atlas-btp.ma"
    assert {row.source for row in mem.store.get_discoveries(db, kind="COMPANY")} == {
        "osm", "wikidata",
    }
    assert canonical_company_key("Atlas BTP", "https://atlas-btp.ma") == "atlas btp ma"


def test_company_universe_workflow_accepts_no_vacancy_candidate(db, config):
    candidates = [CompanyCandidate(
        name="Rif Engineering", industry="engineering", website="https://rif.ma",
        source="osm", reason="local engineering office", relevance_score=75,
    )]
    report = asyncio.run(run_company_universe_discovery(db, config, candidates=candidates))
    assert report.stored == 1
    assert mem.store.get_discoveries(db, kind="COMPANY")[0].kind == "COMPANY"


def _seed_researched_company(
    db, candidate, *, days_ago: int, recruitment_email: str = "",
    has_application_instructions: bool = False,
):
    OpenCompanyDiscovery(fetch_json=lambda *_args, **_kwargs: {}).persist(
        db, [candidate],
    )
    company = db.query(Company).filter_by(
        official_domain=candidate.website.removeprefix("https://"),
    ).one()
    company.last_researched_at = datetime.now(timezone.utc) - timedelta(
        days=days_ago,
    )
    source_url = f"{candidate.website}/careers"
    db.add(Evidence(
        company_id=company.id,
        source_url=source_url,
        source_type="careers" if recruitment_email else "website",
        page_title="Careers",
        field_name="recruitment_email" if recruitment_email else "relevant_page",
        extracted_value=recruitment_email or source_url,
        snippet="Recruitment contact published on official company website.",
    ))
    if recruitment_email and has_application_instructions:
        db.add(Evidence(
            company_id=company.id,
            source_url=source_url,
            source_type="careers",
            page_title="Careers",
            field_name="application_instructions",
            extracted_value=f"Envoyez votre CV de candidature à {recruitment_email}.",
            snippet=f"Envoyez votre CV de candidature à {recruitment_email}.",
        ))
    db.flush()
    return company


def test_company_universe_improves_over_previous_research_selection(db, config):
    config.discovery["company_universe_research_limit"] = 1
    previously_researched = CompanyCandidate(
        name="High Relevance", industry="construction", website="https://high.ma",
        source="fixture", reason="construction employer", relevance_score=100,
    )
    never_researched = CompanyCandidate(
        name="New Candidate", industry="construction", website="https://new.ma",
        source="fixture", reason="construction employer", relevance_score=50,
    )
    incumbent = _seed_researched_company(
        db, previously_researched, days_ago=1,
    )
    candidates = [previously_researched, never_researched]

    # The previous relevance-first queue spent its only slot on the fresh
    # incumbent, then skipped it without considering the new candidate.
    legacy_ranked = sorted(
        candidates,
        key=lambda candidate: (
            not bool(candidate.website),
            -candidate.relevance_score,
            candidate.name.casefold(),
        ),
    )
    incumbent_evidence = db.query(Evidence).filter_by(
        company_id=incumbent.id,
    ).all()
    legacy_calls = [] if _research_is_fresh(
        incumbent, incumbent_evidence, datetime.now(timezone.utc),
    ) else legacy_ranked[:1]
    assert legacy_ranked[0] is previously_researched
    assert legacy_calls == []

    researched = []

    class FakeResearcher:
        def research_and_apply(self, _website, *, company_id, official_domain):
            researched.append(official_domain)
            db.add(Evidence(
                company_id=company_id,
                source_url=f"https://{official_domain}/careers",
                source_type="careers",
                field_name="recruitment_email",
                extracted_value=f"careers@{official_domain}",
                snippet="Recruitment contact published on official careers page.",
            ))
            address = f"careers@{official_domain}"
            db.add(Evidence(
                company_id=company_id,
                source_url=f"https://{official_domain}/careers",
                source_type="careers",
                field_name="application_instructions",
                extracted_value=f"Envoyez votre CV de candidature à {address}.",
                snippet=f"Envoyez votre CV de candidature à {address}.",
            ))
            db.flush()
            return ResearchResult(
                official_domain=official_domain,
                visited=[f"https://{official_domain}/"],
            )

    researcher = FakeResearcher()
    report = asyncio.run(run_company_universe_discovery(
        db, config, candidates=candidates, researcher=researcher,
    ))

    assert researched == ["new.ma"]
    assert report.researched == 1
    assert report.companies_with_qualifying_contacts == 1
    assert report.outreach_ready_companies == 1
    assert len(set(researched)) == 1


def test_company_universe_rotates_within_budget_without_starving_candidates(
    db, config,
):
    config.discovery["company_universe_research_limit"] = 1
    candidates = [
        CompanyCandidate(
            name=name,
            industry="construction",
            website=f"https://{domain}.ma",
            source="fixture",
            reason="construction employer",
            relevance_score=score,
        )
        for name, domain, score in (
            ("Alpha", "alpha", 100),
            ("Bravo", "bravo", 90),
            ("Charlie", "charlie", 80),
        )
    ]
    researched = []

    class FakeResearcher:
        def research_and_apply(self, _website, *, company_id, official_domain):
            researched.append(official_domain)
            source_url = f"https://{official_domain}/about"
            db.add(Evidence(
                company_id=company_id,
                source_url=source_url,
                source_type="website",
                field_name="relevant_page",
                extracted_value=source_url,
            ))
            db.flush()
            return ResearchResult(
                official_domain=official_domain, visited=[source_url],
            )

    researcher = FakeResearcher()
    per_run_counts = []
    for _ in range(4):
        before = len(researched)
        report = asyncio.run(run_company_universe_discovery(
            db, config, candidates=candidates, researcher=researcher,
        ))
        per_run_counts.append(len(researched) - before)
        assert report.researched <= 1

    assert researched == ["alpha.ma", "bravo.ma", "charlie.ma", "alpha.ma"]
    assert per_run_counts == [1, 1, 1, 1]
    assert len(set(researched[:3])) == 3
    assert researched[3] in researched[:3]


def test_company_universe_prioritizes_missing_contacts_and_preserves_qualification(
    db, config,
):
    config.discovery.update({
        "company_universe_research_limit": 1,
        "company_universe_min_relevance": 50,
    })
    no_contact = CompanyCandidate(
        name="No Contact", industry="construction", website="https://no-contact.ma",
        source="fixture", reason="construction employer", relevance_score=60,
    )
    stale_contact = CompanyCandidate(
        name="Stale Contact", industry="construction",
        website="https://stale-contact.ma", source="fixture",
        reason="construction employer", relevance_score=100,
    )
    fresh_contact = CompanyCandidate(
        name="Fresh Contact", industry="construction",
        website="https://fresh-contact.ma", source="fixture",
        reason="construction employer", relevance_score=90,
    )
    below_relevance = CompanyCandidate(
        name="Below Relevance", industry="construction",
        website="https://below-relevance.ma", source="fixture",
        reason="construction employer", relevance_score=10,
    )
    _seed_researched_company(db, no_contact, days_ago=5)
    _seed_researched_company(
        db, stale_contact, days_ago=35,
        recruitment_email="careers@stale-contact.ma",
    )
    _seed_researched_company(
        db, fresh_contact, days_ago=5,
        recruitment_email="careers@fresh-contact.ma",
        has_application_instructions=True,
    )
    researched = []

    class FakeResearcher:
        def research_and_apply(self, _website, *, company_id, official_domain):
            researched.append(official_domain)
            source_url = f"https://{official_domain}/about"
            db.add(Evidence(
                company_id=company_id,
                source_url=source_url,
                source_type="website",
                field_name="relevant_page",
                extracted_value=source_url,
            ))
            db.flush()
            return ResearchResult(
                official_domain=official_domain, visited=[source_url],
            )

    report = asyncio.run(run_company_universe_discovery(
        db, config,
        candidates=[
            no_contact, stale_contact, fresh_contact, below_relevance,
        ],
        researcher=FakeResearcher(),
    ))

    assert researched == ["no-contact.ma"]
    assert report.researched == 1
    assert report.companies_discovered == 3
    assert report.companies_with_qualifying_contacts == 2
    assert report.outreach_ready_companies == 1


def test_company_with_contact_but_no_application_instructions_remains_due_for_research(
    db,
):
    candidate = CompanyCandidate(
        name="Contact Without Instructions",
        industry="construction",
        website="https://contact-without-instructions.ma",
        source="fixture",
        reason="construction employer",
        relevance_score=70,
    )
    company = _seed_researched_company(
        db,
        candidate,
        days_ago=1,
        recruitment_email="careers@contact-without-instructions.ma",
    )
    rows = db.query(Evidence).filter_by(company_id=company.id).all()

    assert _research_is_fresh(company, rows, datetime.now(timezone.utc))
    assert _research_is_due(company, rows, datetime.now(timezone.utc))


def test_company_universe_does_not_promote_directory_or_document_urls():
    assert not _trusted_website("https://www.kerix.ma/annuaire/atlas")
    assert not _trusted_website("https://registre.gov.ma/company/atlas")
    assert not _trusted_website(
        "https://www.maroc1000.net/Secteur-d'activite_BTP"
    )
    assert not _trusted_website("https://docs.google.com/document/d/atlas")
    assert _trusted_website("https://atlas.ma/careers") == "https://atlas.ma/careers"


def test_company_universe_deduplicates_by_domain_not_same_name(db):
    discovery = OpenCompanyDiscovery(fetch_json=lambda *_args, **_kwargs: {})
    candidates = [
        CompanyCandidate(
            name="Atlas Construction", industry="construction", country="Morocco",
            website="https://atlas-a.ma", source="osm", source_url="https://overpass.test/a",
        ),
        CompanyCandidate(
            name="Atlas Construction", industry="construction", country="Morocco",
            website="https://atlas-b.ma", source="wikidata",
            source_url="https://www.wikidata.org/entity/Q2",
        ),
    ]

    stored, duplicates = discovery.persist(db, candidates)

    assert stored == 2
    assert duplicates == 0
    assert {
        company.official_domain for company in db.query(Company).all()
    } == {"atlas-a.ma", "atlas-b.ma"}


def test_company_universe_upgrades_domainless_name_match_with_official_site(db):
    discovery = OpenCompanyDiscovery(fetch_json=lambda *_args, **_kwargs: {})
    candidates = [
        CompanyCandidate(
            name="Rif Works", industry="construction", country="Morocco",
            source="osm", source_url="https://overpass.test/query",
        ),
        CompanyCandidate(
            name="Rif Works", industry="civil engineering", country="Morocco",
            website="https://rif-works.ma", source="wikidata",
            source_url="https://www.wikidata.org/entity/Q3",
        ),
    ]

    discovery.persist(db, candidates)

    company = db.query(Company).one()
    assert company.official_domain == "rif-works.ma"
    assert company.website == "https://rif-works.ma/"


def test_company_universe_candidate_cap_preserves_both_sources():
    candidates = [
        CompanyCandidate(name=f"OSM {index}", source="osm")
        for index in range(4)
    ] + [
        CompanyCandidate(name=f"Wikidata {index}", source="wikidata")
        for index in range(4)
    ]

    capped = _interleave_candidates(candidates, 4)

    assert [candidate.source for candidate in capped] == [
        "osm", "wikidata", "osm", "wikidata",
    ]


def test_company_universe_workflow_uses_configured_open_source(db, config):
    config.discovery.update({
        "company_universe_sources": [{
            "kind": "osm",
            "overpass_url": "https://overpass.test/api",
            "query": "[out:json];",
            "country": "Morocco",
            "target_terms": ["engineering"],
        }],
    })

    class FakeDiscovery:
        def osm(self, **kwargs):
            assert kwargs["country"] == "Morocco"
            return [CompanyCandidate(
                name="Rif Engineering", website="https://rif.ma",
                source="osm", relevance_score=50,
            )]

        def persist(self, session, candidates, *, minimum_relevance=0):
            assert len(candidates) == 1
            return 1, 0

    report = asyncio.run(run_company_universe_discovery(
        db, config, candidate_discovery=FakeDiscovery(),
    ))
    assert report.candidates == 1
    assert report.stored == 1
    assert not report.errors


def test_enabled_company_universe_reports_missing_sources(db, config):
    config.discovery["company_universe_sources"] = []
    report = asyncio.run(run_company_universe_discovery(db, config))
    assert report.stored == 0
    assert report.errors == [
        "company-universe discovery is enabled but company_universe_sources is empty"
    ]


def test_active_morocco_sources_populate_btp_pool_without_web_search(
    db, settings, profile,
):
    config = AgentConfig.model_validate(load_yaml(ROOT_DIR / "config" / "settings.yaml"))
    discovery_config = config.discovery
    assert discovery_config["company_universe_discovery"] is True
    sources = discovery_config["company_universe_sources"]
    assert {source["kind"] for source in sources} == {"osm", "wikidata"}
    osm_source = next(source for source in sources if source["kind"] == "osm")
    assert "construction" in osm_source["query"]
    assert "public works" in osm_source["query"]
    assert "infrastructure" in osm_source["query"]
    wikidata_source = next(source for source in sources if source["kind"] == "wikidata")
    assert "wd:Q1028" in wikidata_source["sparql"]
    assert "LIMIT 100" in wikidata_source["sparql"]
    assert "GROUP BY ?entity" in wikidata_source["sparql"]
    assert "automobil" in wikidata_source["sparql"]

    def fetch_json(url, *, params):
        if "overpass-api.de" in url:
            return {"elements": [
                {"tags": {
                    "name": "Company A",
                    "craft": "civil_engineering",
                    "contact:website": "https://company-a.ma",
                    "addr:city": "Casablanca",
                }},
                {"tags": {
                    "name": "Company B",
                    "industry": "construction",
                    "addr:city": "Rabat",
                }},
            ]}
        return {"results": {"bindings": [{
            "name": {"value": "Company A"},
            "industryLabel": {"value": "civil engineering"},
            "website": {"value": "https://company-a.ma"},
            "cityLabel": {"value": "Casablanca"},
            "entity": {"value": "https://www.wikidata.org/entity/Q123"},
        }]}}

    adapter = OpenCompanyDiscovery(fetch_json=fetch_json)
    universe = asyncio.run(run_company_universe_discovery(
        db, config, candidate_discovery=adapter,
    ))
    assert universe.candidates == 3
    assert universe.companies_discovered == 2
    assert universe.newly_added == 2
    assert universe.already_known == 0
    assert universe.companies_with_official_websites == 1
    assert universe.companies_without_websites == 1
    assert universe.researched == 0

    company_a = db.query(Company).filter_by(name="Company A").one()
    assert company_a.website == "https://company-a.ma/"
    assert company_a.official_domain == "company-a.ma"
    assert company_a.country == "Morocco"
    assert company_a.industry == "civil_engineering"
    assert company_a.source == "osm"
    company_a_records = db.query(Discovery).filter_by(company_id=company_a.id).all()
    assert any(
        row.source == "osm"
        and "overpass-api.de" in row.evidence["source_url"]
        and row.evidence["location"] == "Casablanca"
        for row in company_a_records
    )
    company_b = db.query(Company).filter_by(name="Company B").one()
    assert not company_b.website
    assert not company_b.official_domain
    assert not btp_outreach._mentions_construction(
        "Société marocaine de construction automobile construction automobile"
    )

    class EmptySource:
        def discover(self):
            return []

    class SearchMustWait:
        called = False

        def discover(self):
            self.called = True
            raise AssertionError("External search must wait while known websites remain")

    class WebsiteResearcher:
        def __init__(self):
            self.visited = []

        def research(self, url, *, company_id=None, official_domain="", **_kwargs):
            self.visited.append(official_domain)
            return ResearchResult(official_domain=official_domain)

    search = SearchMustWait()
    researcher = WebsiteResearcher()
    draft_settings = settings.model_copy(update={
        "email_mode": "draft",
        "enable_email": True,
        "email_provider": "gmail",
    })
    report = run_btp_outreach(
        db, config, draft_settings, profile,
        discovery=EmptySource(),
        wikidata_discovery=EmptySource(),
        company_search=search,
        researcher=researcher,
        screening_state_path=None,
        max_companies=1,
    )
    assert report.company_pool_candidates == 2
    assert report.known_website_candidates == 1
    assert report.external_company_search_skipped
    assert not search.called
    assert researcher.visited == ["company-a.ma"]
