"""Social signal discovery tests — Phase 6 (spec §15-18, §34).

Covers: LinkedIn connector (index + user URLs, both authorized channels), Meta
connector (user-provided ONLY, zero network access), access-mode enforcement,
and the opt-in workflow (idempotent persistence, flag-off hermeticity).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import models
from app.connectors.linkedin import LinkedInJobsSource
from app.connectors.meta import MetaJobsSource
from app.workflows.discovery import run_discovery
from app.workflows.social_signals import run_social_signal_discovery

FIX = Path(__file__).parent / "fixtures"


# ---- LinkedIn: authorized channels (§15, §34) ----------------------------------


def test_linkedin_index_results_marked_unverified():
    async def fake(query, location=""):
        return [{"url": "https://www.linkedin.com/jobs/view/42", "title": "Site Engineer",
                 "snippet": "civil engineering construction"}]

    src = LinkedInJobsSource(search_fn=fake, urls=[
        "https://www.linkedin.com/jobs/view/1",
        "https://evil.example/job"])  # non-linkedin user URL must be ignored
    ops = asyncio.run(src.search("civil engineer job", "Canada"))
    assert len(ops) == 2  # 1 index hit + 1 user URL; evil.example dropped
    assert all(o.source_type == "social_signal" for o in ops)
    assert all(o.verification_status == "unverified" for o in ops)
    assert all("linkedin.com" in o.url for o in ops)
    channels = {o.raw["channel"] for o in ops}
    assert channels == {"search_engine_index", "user_provided"}


def test_linkedin_rejects_authorized_only_mode():
    with pytest.raises(ValueError):
        LinkedInJobsSource(config={"access_mode": "authorized_only"})


def test_linkedin_user_provided_mode_skips_index():
    async def boom(query, location=""):
        raise AssertionError("index search must not run in user_provided mode")

    src = LinkedInJobsSource(config={"access_mode": "user_provided"},
                             urls=["https://www.linkedin.com/jobs/view/9"], search_fn=boom)
    ops = asyncio.run(src.search("x", ""))
    assert [o.url for o in ops] == ["https://www.linkedin.com/jobs/view/9"]


# ---- Meta: user-provided ONLY (§17, §34) ----------------------------------------


def test_meta_rejects_non_user_provided_mode():
    for mode in ("public", "authorized_only"):
        with pytest.raises(ValueError):
            MetaJobsSource(config={"access_mode": mode})


def test_meta_emits_only_user_provided_signals(monkeypatch):
    src = MetaJobsSource(
        config={"access_mode": "user_provided", "country": "France"},
        urls=["https://www.facebook.com/groups/travaux/announce/1"],
        leads=[{"title": "Chef de chantier", "location": "Île-de-France, France",
                "note": "pasted by the user"}])
    ops = asyncio.run(src.search("", ""))
    assert len(ops) == 2
    assert all(o.source_type == "social_signal" for o in ops)
    assert all(o.verification_status == "unverified" for o in ops)
    assert all("facebook.com" in o.url or o.url.startswith("meta://") for o in ops)
    # zero network access by construction: no requests import path to abuse


# ---- workflow (opt-in, idempotent, hermetic) ------------------------------------


def test_workflow_stores_social_signals(db, config, prefs, profile, monkeypatch):
    async def fake_index(query, location=""):
        return [{"url": "https://www.linkedin.com/jobs/view/77", "title": "Site Engineer",
                 "snippet": "road construction"}]

    monkeypatch.setattr("app.connectors.linkedin._index_search", fake_index)
    report = asyncio.run(run_social_signal_discovery(
        db, config, prefs, sources_path=FIX / "social_sources_demo.yaml", profile=profile))
    assert report.stored >= 3  # 1 linkedin index + 1 linkedin URL + 1 meta URL + 1 meta lead
    n = db.execute(select(func.count()).select_from(models.OpportunitySource)).scalar_one()
    assert n == report.stored
    kinds = set(db.execute(select(models.OpportunitySource.kind)).scalars().all())
    assert kinds == {"social_signal"}

    # second run is idempotent (keyed on url)
    report2 = asyncio.run(run_social_signal_discovery(
        db, config, prefs, sources_path=FIX / "social_sources_demo.yaml", profile=profile))
    assert report2.stored == 0
    assert db.execute(select(func.count()).select_from(models.OpportunitySource)).scalar_one() == n


def test_discovery_flag_off_hermetic(db, config, prefs, profile):
    asyncio.run(run_discovery(db, config, prefs, sources_path=FIX / "sources_demo.yaml",
                              profile=profile))
    assert db.execute(select(func.count()).select_from(models.OpportunitySource)).scalar_one() == 0
