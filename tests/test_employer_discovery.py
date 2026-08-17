"""Employer & recruitment-agency discovery tests (spec §7, §9) — fully hermetic.

Network access is injected (fake `search_fn` + fake `fetch_html`), so nothing
hits the live web.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select

from app import models
from app.connectors.search_engine import resolve_search_url
from app.discovery.employers import (
    EmployerDiscoveryEngine,
    candidate_employer_queries,
    classify_result,
)
from app.discovery.vocabulary import CandidateVocabulary
from app.workflows.discovery import run_discovery
from app.workflows.employer_discovery import run_employer_discovery

FIX = Path(__file__).parent / "fixtures"

_HTML = "<html><head><title>Careers</title></head><body><a>Test</a></body></html>"


# ---- result classification (§7/§9) --------------------------------------------


def test_classify_company_result():
    cand = classify_result(
        "https://careers.bouygues.com/jobs", "Careers at Bouygues Construction",
        "Road and infrastructure engineering jobs. Visa sponsorship for international candidates.",
        country="France")
    assert cand is not None
    assert "Bouygues" in cand.name
    assert cand.kind == "company"
    assert cand.careers_url == "https://careers.bouygues.com/jobs"
    assert cand.country == "France"
    assert cand.industry == "civil_engineering"
    assert cand.sponsorship_signal == "high"        # explicit visa sponsorship
    assert cand.international_recruitment_signal == "high"


def test_classify_agency_result():
    cand = classify_result(
        "https://www.manpower.fr", "Manpower Interim International",
        "Recruitment agency for international candidates.", country="France")
    assert cand is not None
    assert cand.kind == "recruitment_agency"
    assert cand.recruitment_url == "https://www.manpower.fr"
    assert cand.international_recruitment_signal == "high"


def test_classify_skips_job_boards_and_weak_results():
    assert classify_result("https://www.indeed.com/jobs?q=civil") is None
    assert classify_result("", "no url here") is None
    assert classify_result("/relative/path") is None


def test_classify_absence_stays_unknown():
    cand = classify_result("https://careers.example.com", "Example Careers", "Join us!")
    assert cand.sponsorship_signal == "unknown"      # never 'no' — spec §10
    assert cand.international_recruitment_signal == "unknown"


# ---- query generation ---------------------------------------------------------


def test_employer_queries_cover_roles_and_countries(profile, prefs):
    vocab = CandidateVocabulary(profile=profile, prefs=prefs)
    pairs = candidate_employer_queries(vocab, ["France", "Germany"])
    queries = [q for q, _ in pairs]
    assert any("civil engineering technician careers" in q for q in queries)
    assert any("recruitment agency" in q for q in queries)
    locations = {loc for _, loc in pairs}
    assert "France" in locations and "Germany" in locations


# ---- search-engine URL unwrapping ---------------------------------------------


def test_resolve_search_url_unwraps_duckduckgo():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fcareers.bouygues.com%2Fjobs&rut=abc"
    assert resolve_search_url(href) == "https://careers.bouygues.com/jobs"
    plain = "https://careers.bouygues.com/jobs"
    assert resolve_search_url(plain) == plain


# ---- discovery engine (dedup + caps) ------------------------------------------


async def _fake_search(query: str, location: str) -> list[dict]:
    if location == "France":
        return [
            {"url": "https://careers.bouygues.com/jobs", "title": "Careers at Bouygues Construction",
             "snippet": "Road and infrastructure engineering jobs. Visa sponsorship for international candidates."},
            {"url": "https://careers.bouygues.com/jobs", "title": "Dupe", "snippet": "same careers url"},
            {"url": "https://careers.eiffage.com/jobs", "title": "Eiffage Careers",
             "snippet": "Civil engineering technician opportunities."},
            {"url": "https://colas.com/careers", "title": "Colas Careers",
             "snippet": "Construction technician roles."},
            {"url": "https://www.manpower.fr", "title": "Manpower Interim International",
             "snippet": "Recruitment agency for international candidates."},
            {"url": "https://random.org/x", "title": "Random", "snippet": "nothing here"},
        ]
    if location == "Germany":
        return [{"url": "https://careers.hochtief.de/jobs", "title": "Hochtief Careers",
                 "snippet": "Bau careers."}]
    return []


def test_engine_discovers_dedups_and_caps(profile, prefs):
    engine = EmployerDiscoveryEngine(profile=profile, prefs=prefs, search_fn=_fake_search)
    cands = asyncio.run(engine.discover_prefs(max_per_country=2))
    assert cands
    urls = [c.careers_url for c in cands]
    assert len(urls) == len(set(urls)), "careers URLs must be deduplicated"
    france = [c for c in cands if c.country == "France"]
    assert len(france) <= 2, "per-country cap must hold"
    assert all("bouygues" not in (c.name.lower()) or c.sponsorship_signal == "high" for c in cands)


def test_engine_survives_search_failure(profile, prefs):
    async def boom(query, location):
        raise ConnectionError("offline")

    engine = EmployerDiscoveryEngine(profile=profile, prefs=prefs, search_fn=boom)
    assert asyncio.run(engine.discover_prefs(max_per_country=5)) == []


def test_engine_verifier_offline_degrade(profile, prefs):
    engine = EmployerDiscoveryEngine(profile=profile, prefs=prefs,
                                     search_fn=_fake_search, fetch_html=lambda url: "")
    cands = asyncio.run(engine.discover_prefs(max_per_country=5))
    assert cands
    assert all(not asyncio.run(engine.verify(c.careers_url)).verified for c in cands)


# ---- persistence workflow (§7/§9) ----------------------------------------------


def test_run_employer_discovery_stores_employers_and_agencies(db, config, prefs, profile):
    report = asyncio.run(run_employer_discovery(
        db, config, prefs, profile=profile, search_fn=_fake_search,
        fetch_html=lambda url: _HTML))
    assert report.discovered > 0
    assert report.verified == report.discovered
    assert report.stored > 0

    companies = db.execute(select(models.Company)).scalars().all()
    assert companies
    by_name = {c.name: c for c in companies}
    bouygues = by_name.get("Bouygues")
    assert bouygues is not None
    assert bouygues.careers_url == "https://careers.bouygues.com/jobs"
    assert bouygues.industry == "civil_engineering"
    assert bouygues.source == "employer_discovery"
    assert bouygues.sponsorship_signal == "high"
    manpower = by_name.get("Manpower")
    assert manpower is not None
    assert manpower.recruitment_url == "https://www.manpower.fr"
    assert manpower.industry == ""


def test_employer_discovery_idempotent(db, config, prefs, profile):
    def run():
        return asyncio.run(run_employer_discovery(
            db, config, prefs, profile=profile, search_fn=_fake_search,
            fetch_html=lambda url: _HTML))

    run()
    first_count = len(db.execute(select(models.Company)).scalars().all())
    run()
    second_count = len(db.execute(select(models.Company)).scalars().all())
    assert first_count == second_count, "re-running must not duplicate companies"


def test_run_discovery_employer_flag_off_is_hermetic(db, config, prefs, profile):
    report = asyncio.run(run_discovery(db, config, prefs, FIX / "sources_demo.yaml",
                                       profile=profile))
    assert report.employers_discovered == 0
    employer_sources = db.execute(
        select(models.Company).where(models.Company.source == "employer_discovery")
    ).scalars().all()
    assert not employer_sources, "employer discovery must be opt-in (off by default)"
