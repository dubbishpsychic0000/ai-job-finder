from __future__ import annotations

import asyncio

from app import memory as mem
from app.connectors.linkedin import LinkedInJobsSource
from app.discovery.company_universe import (
    CompanyCandidate,
    OpenCompanyDiscovery,
    canonical_company_key,
)
from app.workflows.company_universe import run_company_universe_discovery


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
    assert stored == 1
    assert duplicates == 1
    company = db.query(__import__("app.models", fromlist=["Company"]).Company).one()
    assert company.official_domain == "atlas-btp.ma"
    assert mem.store.get_discoveries(db, kind="COMPANY")[0].source == "osm"
    assert canonical_company_key("Atlas BTP", "https://atlas-btp.ma") == "atlas btp ma"


def test_company_universe_workflow_accepts_no_vacancy_candidate(db, config):
    candidates = [CompanyCandidate(
        name="Rif Engineering", industry="engineering", website="https://rif.ma",
        source="osm", reason="local engineering office", relevance_score=75,
    )]
    report = asyncio.run(run_company_universe_discovery(db, config, candidates=candidates))
    assert report.stored == 1
    assert mem.store.get_discoveries(db, kind="COMPANY")[0].kind == "COMPANY"


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
    report = asyncio.run(run_company_universe_discovery(db, config))
    assert report.stored == 0
    assert report.errors == [
        "company-universe discovery is enabled but company_universe_sources is empty"
    ]
