"""Discovery analytics API tests — Phase 7 (spec §30).

Calls the FastAPI handler functions directly with a real test DB session
(no HTTP client needed) and verifies the report is deterministic/hermetic.
"""
from __future__ import annotations

from pathlib import Path

import app.api.main as api
from app import memory as mem
from app import models

FIX = Path(__file__).parent / "fixtures"


def test_analytics_report(db, monkeypatch):
    from app.config import get_preferences

    monkeypatch.setattr("app.config.get_preferences",
                        lambda: get_preferences(FIX / "preferences.yaml"))
    # deterministic profile (conftest already pins get_profile to the fixture)

    company = mem.store.get_or_create_company(db, "Colas")
    job = models.Job(source="static_files", external_id="an1", dedup_key="hash:an1",
                     title="Technicien VRD", company_id=company.id, country="France",
                     description="d", url="https://example.com/j", status="analyzed")
    db.add(job)
    db.flush()
    mem.store.record_query(db, "civil engineering technician", "France", source="s",
                           jobs_found=3, relevant_jobs=2)
    source = mem.store.upsert_source(db, "demo_rss", "rss", "https://example.com/feed")
    mem.store.mark_source_fetched(db, source, 5)

    report = api.api_analytics(db=db)
    assert set(report) == {"stats", "countries", "top_queries", "source_health"}

    # country ranking sees the fixture prefs + the seeded job
    fr = next(c for c in report["countries"] if c["country"] == "France")
    assert fr["jobs"] == 1
    assert fr["score"] == 1.5  # french language affinity
    assert next(c for c in report["countries"] if c["country"] == "Belgium")["score"] == 1.5

    # query learning surfaced in analytics
    assert report["top_queries"][0]["query"] == "civil engineering technician"
    assert report["top_queries"][0]["value"] > 0.5

    # connector health
    assert report["source_health"][0]["items_found"] == 5


def test_analytics_empty_db_is_safe(db, monkeypatch):
    def _boom_prefs():
        raise FileNotFoundError("no prefs")

    monkeypatch.setattr("app.config.get_preferences", _boom_prefs)
    report = api.api_analytics(db=db)
    assert report["countries"] == []
    assert report["top_queries"] == []
    assert report["source_health"] == []
    assert "total_jobs" in report["stats"]  # stats block still present
