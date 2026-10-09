from __future__ import annotations

import asyncio

from app import memory as mem
from app.config import ROOT_DIR, AgentConfig, load_yaml
from app.connectors.linkedin import LinkedInJobsSource
from app.discovery.company_universe import (
    CompanyCandidate,
    OpenCompanyDiscovery,
    canonical_company_key,
)
from app.discovery.website_researcher import ResearchResult
from app.models import Company, Discovery
from app.workflows.btp_outreach import run_btp_outreach
from app.workflows.company_universe import (
    _interleave_candidates,
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
