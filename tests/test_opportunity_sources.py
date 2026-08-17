"""Search-engine expansion → opportunity sources tests — Phase 5 (spec §6).

Hermetic: `search_fn` is injected; no live web access.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import func, select

from app import models
from app.discovery.opportunity_sources import (
    OpportunitySourceDiscoveryEngine,
    classify_opportunity_source,
)
from app.workflows.discovery import run_discovery
from app.workflows.opportunity_sources import run_opportunity_source_discovery

FIX = Path(__file__).parent / "fixtures"


# ---- classification ------------------------------------------------------------


def test_classify_official_government():
    cand = classify_opportunity_source(
        "https://www.canada.ca/x", "Immigration and Citizenship", "work permit program")
    assert cand.kind == "immigration"
    assert cand.notes == "official source"


def test_classify_shortage_page():
    cand = classify_opportunity_source(
        "https://www.gov.uk/example", "Skilled worker", "shortage occupation list")
    assert cand.kind == "shortage"


def test_classify_recruitment_agency():
    cand = classify_opportunity_source(
        "https://agency.example.com", "Civil engineering recruitment agency", "we place foreign workers")
    assert cand.kind == "recruitment_agency"


def test_classify_employer_careers():
    cand = classify_opportunity_source("https://example.com/careers", "Careers at Example Construction")
    assert cand.kind == "employer_career"


def test_classify_social_signal():
    # no career keywords in the snippet, so host detection decides §18
    assert classify_opportunity_source("https://www.linkedin.com/jobs/view/1").kind == "social_signal"
    assert classify_opportunity_source("https://www.facebook.com/groups/jobs-europe").kind == "social_signal"


def test_classify_sponsorship_and_international():
    # "visa" would classify as immigration; plain sponsorship stays on brand (§22/§23)
    cand = classify_opportunity_source("https://x.com/jobs", "Employer sponsorship available")
    assert cand.kind == "sponsorship"
    assert cand.sponsorship_signal == "high"
    cand = classify_opportunity_source("https://x.com/hiring", "Open to international applicants")
    assert cand.kind == "international"
    assert cand.international_recruitment_signal == "high"


def test_classify_unknown_stays_general():
    cand = classify_opportunity_source("https://unknown.example/page", "Some random page")
    assert cand.kind == "general"
    assert cand.sponsorship_signal == "unknown"  # never a positive claim (§10)


# ---- engine ---------------------------------------------------------------------


async def _fake_search(query, location=""):
    return [
        {"url": f"https://careers.example/{location.lower()}", "title": query,
         "snippet": "careers page for civil engineering"},
        {"url": "https://www.canada.ca/shortage", "title": "Shortage occupations",
         "snippet": "shortage occupation list civil engineering technicians"},
        {"url": "https://agency.example.com", "title": "Engineering recruitment agency",
         "snippet": "international recruitment"},
    ]


def test_engine_discovers_and_classifies(prefs):
    eng = OpportunitySourceDiscoveryEngine(prefs=prefs, search_fn=_fake_search)
    found = asyncio.run(eng.discover([("civil engineering technician careers", "Canada")], max_per_country=5))
    kinds = {c.kind for c in found}
    assert "employer_career" in kinds
    assert "shortage" in kinds
    assert "recruitment_agency" in kinds
    assert all(c.country == "Canada" for c in found)


def test_workflow_stores_idempotently(db, config, prefs, profile):
    report = asyncio.run(run_opportunity_source_discovery(
        db, config, prefs, profile=profile, search_fn=_fake_search))
    assert report.stored >= 1
    n = db.execute(select(func.count()).select_from(models.OpportunitySource)).scalar_one()
    assert n == report.stored
    # second run is idempotent (keyed on url)
    report2 = asyncio.run(run_opportunity_source_discovery(
        db, config, prefs, profile=profile, search_fn=_fake_search))
    assert report2.stored == 0
    assert db.execute(select(func.count()).select_from(models.OpportunitySource)).scalar_one() == n
    assert mem_stats(db)["opportunity_sources"] == n


def mem_stats(db):
    from app import memory as mem

    return mem.store.stats(db)


def test_discovery_flag_off_hermetic(db, config, prefs, profile):
    asyncio.run(run_discovery(db, config, prefs, sources_path=FIX / "sources_demo.yaml",
                              profile=profile))
    assert db.execute(select(func.count()).select_from(models.OpportunitySource)).scalar_one() == 0


def test_discovery_stores_freshness(db, config, prefs, profile):
    asyncio.run(run_discovery(db, config, prefs, sources_path=FIX / "sources_demo.yaml",
                              profile=profile))
    jobs = db.execute(select(models.Job)).scalars().all()
    assert jobs
    assert all(j.freshness in ("very_high", "high", "medium", "low", "stale") for j in jobs)
