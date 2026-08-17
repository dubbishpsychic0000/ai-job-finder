"""Government & public employment portal connector tests (spec §2, §14, §34).

Hermetic: the connector reads local fixtures via `data_path`. No live web access.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.connectors import registry
from app.connectors.base import Opportunity
from app.connectors.government import GovernmentPortalSource

FIX = Path(__file__).parent / "fixtures"
IMM = FIX / "immigration"


def test_government_declares_policy():
    assert GovernmentPortalSource.source_type == "government"
    assert GovernmentPortalSource.access_mode == "public"
    assert GovernmentPortalSource.policy_notice
    assert "government" in registry


def test_government_rejects_non_public_access():
    with pytest.raises(ValueError, match="access_mode 'public'"):
        GovernmentPortalSource(config={"access_mode": "authorized_only", "type": "api"})


def test_government_rss_data_path():
    src = GovernmentPortalSource(
        config={"type": "rss", "name": "canada_gov_jobs", "country": "Canada"},
        data_path=IMM / "gov_jobs.xml",
    )
    opps = asyncio.run(src.search("technician", "Canada"))
    assert opps
    assert all(isinstance(o, Opportunity) for o in opps)
    assert all(o.source_type == "government" for o in opps)
    assert all(o.country == "Canada" for o in opps)
    assert any("technician" in o.title.lower() for o in opps)


def test_government_api_data_path():
    src = GovernmentPortalSource(
        config={"type": "api", "name": "canada_gov_api", "country": "Canada"},
        data_path=IMM / "gov_jobs.json",
    )
    opps = asyncio.run(src.search("technician", "Canada"))
    assert len(opps) == 2
    assert opps[0].title == "Civil Engineering Technician"
    assert opps[0].source_type == "government"
    assert opps[0].url.startswith("https://www.canada.ca/")
    assert opps[0].effective_quality() == 100


def test_government_query_filter_mismatch_returns_empty():
    src = GovernmentPortalSource(
        config={"type": "api", "name": "canada_gov_api", "country": "Canada"},
        data_path=IMM / "gov_jobs.json",
    )
    assert asyncio.run(src.search("surgeon", "Canada")) == []


def test_government_offline_no_config():
    src = GovernmentPortalSource(config={"type": "api"})
    assert asyncio.run(src.search("technician", "Canada")) == []


def test_government_mapping_custom_fields():
    src = GovernmentPortalSource(
        config={"type": "api", "name": "austria_ams", "country": "Austria", "language": "de",
                "list_path": "response.docs",
                "mapping": {"title": "beruf", "company": "arbeitgeber", "url": "url",
                            "external_id": "id"}},
        data_path=IMM / "ams_jobs.json",
    )
    opps = asyncio.run(src.search("techniker", "Austria"))
    assert opps
    assert opps[0].title == "Straßentechniker (m/w/d)"
    assert opps[0].company == "ÖBB Infrastruktur"
    assert opps[0].language == "de"
    assert all(o.source_type == "government" for o in opps)
