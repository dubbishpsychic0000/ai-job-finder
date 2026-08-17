"""Immigration & work-pathway discovery tests — Phase 4 (spec §11, §13, §34).

Hermetic: fetchers are injectable, sources point at fixture-shaped official
URLs, and the workflow is exercised with a fake engine. No live web access.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import feedparser
from sqlalchemy import func, select

from app import memory as mem
from app import models
from app.config import load_yaml
from app.connectors.immigration.official import is_official
from app.discovery.immigration import (
    ImmigrationDiscoveryEngine,
    facts_from_text,
)
from app.workflows.discovery import run_discovery

FIX = Path(__file__).parent / "fixtures"
IMM = FIX / "immigration"
NOW = datetime(2026, 2, 1, tzinfo=timezone.utc)


def _async_ret(value):
    async def _fn(*a, **k):
        return value
    return _fn


# ---- extraction / matching helpers -------------------------------------------


def test_facts_from_text_only_occupation_sentences():
    text = ("The shortage occupation list includes civil engineering technicians, "
            "who are in high demand. Seasonal hiring opens in spring.")
    facts = facts_from_text(text, "Canada", "TFW", "https://www.canada.ca/x",
                            ["civil engineering technician"], retrieved_at=NOW)
    assert len(facts) == 1
    assert facts[0].fact_type == "shortage_occupation"
    assert facts[0].source_domain == "www.canada.ca"
    assert "seasonal hiring" not in facts[0].claim


def test_facts_from_text_empty_when_short():
    assert facts_from_text("Too short. Not long enough.", "Canada", "P", "https://x.ca",
                           ["civil engineering technician"], retrieved_at=NOW) == []


def test_match_occupation_is_deterministic():
    eng = ImmigrationDiscoveryEngine(countries=["Canada"], occupations=[
        "civil engineering technician", "road technician"])
    from app.discovery.immigration import DiscoveredFact

    hit = DiscoveredFact(country="Canada", program="P", claim="Civil engineering technicians listed",
                         source_url="https://www.canada.ca/x", source_domain="www.canada.ca",
                         retrieved_at=NOW)
    assert eng.match_occupation(hit) == (True, "civil engineering technician")
    miss = DiscoveredFact(country="Canada", program="P", claim="Nurses are in high demand",
                          source_url="https://www.canada.ca/x", source_domain="www.canada.ca",
                          retrieved_at=NOW)
    assert eng.match_occupation(miss) == (False, "")


def test_whitelist_enforced_on_configured_urls():
    eng = ImmigrationDiscoveryEngine(
        countries=["Canada"],
        occupations=["civil engineering technician"],
        sources=[{"country": "Canada", "name": "blog", "kind": "html",
                  "program": "P", "urls": ["https://example.blog.blogspot.com/work.html"]}],
        fetch_html=_async_ret("Civil engineering technicians are in high demand on the shortage occupation list."),
        now=lambda: NOW,
    )
    facts = asyncio.run(eng.discover())
    assert facts == []
    assert eng.rejected_unofficial() == 1


def test_engine_extracts_html_facts_official():
    text = (IMM / "official_page.html").read_text(encoding="utf-8")
    eng = ImmigrationDiscoveryEngine(
        countries=["Canada"],
        occupations=["civil engineering technician", "road technician", "infrastructure technician"],
        sources=[{"country": "Canada", "name": "IRCC", "kind": "html",
                  "program": "Temporary Foreign Worker Program",
                  "urls": ["https://www.canada.ca/en/immigration-refugees-citizenship/work.html"]}],
        fetch_html=_async_ret(text),
        now=lambda: NOW,
    )
    facts = asyncio.run(eng.discover())
    assert len(facts) >= 1
    f = facts[0]
    assert f.country == "Canada"
    assert f.source_domain == "www.canada.ca"
    assert f.fact_type == "shortage_occupation"
    assert f.occupation
    assert is_official(f.source_url)


def test_engine_parses_rss():
    parsed = feedparser.parse((IMM / "shortage_feed.xml").read_text(encoding="utf-8"))
    entries = [{
        "link": e.link, "title": e.title, "summary": e.summary, "published": e.get("published", ""),
    } for e in parsed.entries]
    eng = ImmigrationDiscoveryEngine(
        countries=["Canada"],
        occupations=["civil engineering technician", "road technician"],
        sources=[{"country": "Canada", "name": "IRCC feed", "kind": "rss",
                  "program": "Canada official news",
                  "urls": ["https://www.canada.ca/en/immigration-refugees-citizenship/services/news.rss"]}],
        fetch_rss=_async_ret(entries),
        now=lambda: NOW,
    )
    facts = asyncio.run(eng.discover())
    assert len(facts) == 2
    assert "civil engineering technicians" in facts[0].claim.lower()
    assert facts[0].source_domain == "www.canada.ca"


def test_engine_parses_json_and_caps_per_country():
    payload = {"items": [
        {"summary": "Civil engineering technicians remain on the shortage occupation list.",
         "occupation": "Civil Engineering Technician", "category": "shortage_occupation"},
        {"summary": "Road technicians are in high demand.",
         "occupation": "Road Construction Technician", "category": "shortage_occupation"},
    ]}
    eng = ImmigrationDiscoveryEngine(
        countries=["Canada"],
        occupations=["civil engineering technician", "road technician"],
        sources=[{"country": "Canada", "name": "jobbank_api", "kind": "json",
                  "program": "Canada Job Bank facts", "list_path": "items",
                  "mapping": {"claim": "summary", "occupation": "occupation", "fact_type": "category"},
                  "urls": ["https://www.canada.ca/official-labour-api.json"]}],
        fetch_json=_async_ret(payload),
        now=lambda: NOW,
    )
    facts = asyncio.run(eng.discover(limit_per_country=1))
    assert len(facts) == 1  # capped per country
    assert facts[0].confidence == 100


def test_priority_countries_orders_by_matched():
    from app.discovery.immigration import DiscoveredFact

    def fact(country, claim):
        return DiscoveredFact(country=country, program="P", claim=claim,
                              source_url="https://www.canada.ca/x", source_domain="x.ca",
                              retrieved_at=NOW, matched=True)
    eng = ImmigrationDiscoveryEngine(countries=["Canada", "France"], occupations=[])
    ordered = eng.priority_countries([fact("France", "a"), fact("Canada", "b"),
                                      fact("Canada", "c")])
    assert ordered == [("Canada", 2), ("France", 1)]


# ---- workflow -----------------------------------------------------------------


def test_run_immigration_discovery_stores_idempotently(db, config, prefs, profile):
    from app.workflows.immigration_discovery import run_immigration_discovery

    class StubEngine:
        facts = fake_engine_ok()

        async def discover(self, limit_per_country=None):
            return self.facts

        def rejected_unofficial(self):
            return 0

        def match_occupation(self, fact):
            return (True, fact.occupation)

        def priority_countries(self, facts):
            return [("Canada", 2)]

    asyncio.run(run_immigration_discovery(db, config, prefs, profile=profile,
                                          sources_path=FIX / "sources_demo.yaml",
                                          engine=StubEngine()))
    n = db.execute(select(func.count()).select_from(models.ImmigrationFact)).scalar_one()
    assert n == 2
    asyncio.run(run_immigration_discovery(db, config, prefs, profile=profile,
                                          sources_path=FIX / "sources_demo.yaml",
                                          engine=StubEngine()))
    assert db.execute(select(func.count()).select_from(models.ImmigrationFact)).scalar_one() == n
    assert mem.store.stats(db)["immigration_facts"] == n
    events = db.execute(select(models.Event).where(models.Event.type == "immigration_discovery")).scalars().all()
    assert len(events) == 2  # one audit event per run


def fake_engine_ok():
    from app.discovery.immigration import DiscoveredFact

    return [
        DiscoveredFact(country="Canada", program="TFW",
                       claim="Civil engineering technicians are on the shortage occupation list.",
                       source_url="https://www.canada.ca/x", source_domain="www.canada.ca",
                       retrieved_at=NOW, occupation="Civil Engineering Technician",
                       fact_type="shortage_occupation", matched=False),
        DiscoveredFact(country="Canada", program="TFW",
                       claim="Express Entry now covers road construction technicians.",
                       source_url="https://www.canada.ca/y", source_domain="www.canada.ca",
                       retrieved_at=NOW, occupation="Road Construction Technician",
                       fact_type="program", matched=False),
    ]


def test_run_immigration_discovery_offline_no_sources(db, config, prefs, profile):
    from app.workflows.immigration_discovery import run_immigration_discovery

    eng = ImmigrationDiscoveryEngine(countries=prefs.countries, occupations=[], sources=[])
    report = asyncio.run(run_immigration_discovery(
        db, config, prefs, profile=profile,
        sources_path=FIX / "sources_demo.yaml", engine=eng))
    assert report.facts_discovered == 0
    assert report.stored == 0
    assert db.execute(select(func.count()).select_from(models.ImmigrationFact)).scalar_one() == 0


def test_run_discovery_immigration_flag_off_hermetic(db, config, prefs, profile):
    asyncio.run(run_discovery(db, config, prefs,
                              sources_path=FIX / "sources_demo.yaml",
                              profile=profile))
    assert db.execute(select(func.count()).select_from(models.ImmigrationFact)).scalar_one() == 0
    events = db.execute(select(models.Event).where(models.Event.type == "immigration_discovery")).scalars().all()
    assert events == []


def test_engine_uses_demo_sources_end_to_end(db, config, prefs, profile):
    from app.workflows.immigration_discovery import run_immigration_discovery

    demo = load_yaml(IMM / "sources_demo.yaml")["immigration_sources"]
    parsed = feedparser.parse((IMM / "shortage_feed.xml").read_text(encoding="utf-8"))
    entries = [{"link": e.link, "title": e.title, "summary": e.summary,
                "published": e.get("published", "")} for e in parsed.entries]
    json_payload = {"items": [
        {"summary": "Civil engineering technicians in high demand.", "occupation": "Civil Engineering Technician",
         "category": "shortage_occupation"},
        {"summary": "New pathways for skilled technicians opened.", "occupation": "Site Technician",
         "category": "program"},
    ]}
    eng = ImmigrationDiscoveryEngine(
        countries=prefs.countries,
        occupations=["civil engineering technician", "site technician", "road technician"],
        sources=demo,
        fetch_html=_async_ret((IMM / "official_page.html").read_text(encoding="utf-8")),
        fetch_rss=_async_ret(entries),
        fetch_json=_async_ret(json_payload),
        now=lambda: NOW,
    )
    report = asyncio.run(run_immigration_discovery(
        db, config, prefs, profile=profile,
        sources_path=FIX / "sources_demo.yaml", engine=eng))
    assert report.facts_discovered >= 3
    assert report.stored == report.facts_discovered
    assert report.rejected_unofficial == 0
    assert report.matched >= 1
    assert report.priority_countries and report.priority_countries[0][0] == "Canada"
    rows = db.execute(select(models.ImmigrationFact)).scalars().all()
    assert all(is_official(r.source_url) for r in rows)
    assert all(r.source_domain == "www.canada.ca" for r in rows)
