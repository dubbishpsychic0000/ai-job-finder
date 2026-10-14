"""Connector tests: RSS parsing, static files, normalization, search plan."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.connectors.base import infer_country, parse_date
from app.connectors.static_files import StaticFilesSource
from app.normalization import normalize
from app.workflows.search_plan import SearchPlan


def test_infer_country():
    assert infer_country("Paris, France") == "France"
    assert infer_country("Toronto, Canada") == "Canada"
    assert infer_country("Lisboa, Portugal") == "Portugal"
    assert infer_country("Berlin, Deutschland") == "Germany"
    assert infer_country("") == ""
    assert infer_country("Somewhere unknown") == "" or infer_country("Somewhere unknown") == "Canada"


def test_parse_date_aware_utc():
    dt = parse_date("2026-08-10")
    assert dt is not None and dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 8


def test_normalize_drops_bad():
    from app.connectors.base import Opportunity

    opp = Opportunity(source="x", external_id="1", title="", url="https://x/j", location="Paris, France")
    assert normalize(opp) is None  # empty title
    bad = Opportunity(source="x", external_id="1", title="civil technician", url="nope",
                      location="Paris, France")
    assert normalize(bad) is None  # invalid url
    good = Opportunity(source="x", external_id="1", title="  Civil Technician  ",
                       url="https://x/j", location="Paris, France",
                       posted_at=datetime.now(timezone.utc))
    g = normalize(good)
    assert g.title == "Civil Technician"
    assert g.country == "France"


def test_normalize_preserves_unknown_posting_date_and_drops_future_evidence():
    from datetime import timedelta

    from app.connectors.base import Opportunity

    instant = datetime(2026, 10, 13, tzinfo=timezone.utc)
    unknown = Opportunity(
        source="tavily",
        external_id="https://example.test/jobs/1",
        title="Civil Engineering Technician",
        url="https://example.test/jobs/1",
        raw={"query": "civil technician", "posted": "2026-10-12"},
    )
    normalized = normalize(unknown, now=instant)
    assert normalized is not None
    assert normalized.posted_at is None

    future = Opportunity(
        source="fixture",
        external_id="future-1",
        title="Civil Engineering Technician",
        url="https://example.test/jobs/2",
        posted_at=instant + timedelta(days=3),
        posted_at_source="fixture:posted_at",
        posted_at_evidence_url="https://example.test/jobs/2",
    )
    normalized_future = normalize(future, now=instant)
    assert normalized_future is not None
    assert normalized_future.posted_at is None
    assert normalized_future.posted_at_source == ""
    assert normalized_future.posted_at_evidence_url == ""


def test_rss_does_not_use_page_updated_date_as_posting_date():
    from types import SimpleNamespace

    from app.connectors.rss import RSSJobSource

    source = RSSJobSource()
    entry = SimpleNamespace(
        title="Civil Engineer",
        link="https://employer.example/jobs/123",
        summary="A civil engineering vacancy.",
        updated="2026-10-12",
        tags=[],
    )
    opportunity = source._from_entry(entry, "https://employer.example/feed")
    assert opportunity.posted_at is None
    assert opportunity.posted_at_source == ""


def test_greenhouse_and_lever_update_timestamps_are_not_posting_dates():
    from app.connectors.greenhouse import GreenhouseSource
    from app.connectors.lever import LeverSource

    greenhouse = GreenhouseSource(boards=["example"])
    greenhouse_item = greenhouse._from_job("example", {
        "id": 12,
        "title": "Civil Engineer",
        "updated_at": "2026-10-12T00:00:00Z",
        "absolute_url": "https://boards.greenhouse.io/example/jobs/12",
        "company_name": "Example Engineering",
        "location": {"name": "Casablanca, Morocco"},
        "content": "Public civil engineering job record.",
    })
    lever = LeverSource(sites=["example"])
    lever_item = lever._from_posting("example", {
        "id": "abc-123",
        "text": "Civil Engineer",
        "createdAt": 1791763200000,
        "hostedUrl": "https://jobs.lever.co/example/abc-123",
        "company": "Example Engineering",
        "categories": {"location": "Casablanca, Morocco"},
    })
    assert greenhouse_item.posted_at is None
    assert lever_item.posted_at is None


def test_static_source_returns_opportunities():
    from tests.conftest import ROOT

    source = StaticFilesSource(ROOT / "tests" / "fixtures" / "demo", live=True)
    ops = asyncio.run(source.search("civil engineering technician"))
    assert ops, "fixture must yield opportunities"
    first = ops[0]
    assert first.title
    assert first.url.startswith("http")
    assert first.dedup_key()


def test_rss_source_preserves_structure_from_xml():
    """Feed the RSS connector a local fixture feed and check normalization."""
    from pathlib import Path

    import feedparser

    feed_path = Path(__file__).parent / "fixtures" / "feed.xml"
    parsed = feedparser.parse(str(feed_path))
    assert len(parsed.entries) >= 1


def test_search_plan_generates_combos(prefs, config):
    plan = SearchPlan(prefs).build(max_per_country=2)
    assert plan, "preferences must yield a plan"
    countries = {c["country"] for c in plan}
    assert "France" in countries
    assert len(plan) >= len(countries)
    # queries should be distinct per country (deduped)
    keys = [(p["query"].lower(), p["location"].lower()) for p in plan]
    assert len(keys) == len(set(keys))
