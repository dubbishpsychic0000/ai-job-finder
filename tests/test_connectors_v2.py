"""Connector tests — Discovery V2 (spec §2, §8, §20, §21, §22, §34).

Hermetic: all network-backed connectors are tested against local fixtures via
their `data_path`, or offline degradation paths. No live web access.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app import memory as mem
from app.connectors.ats_detect import detect_ats
from app.connectors.base import Opportunity, source_type_for
from app.connectors.company_careers import CompanyCareersSource
from app.connectors.eures import EuresSource
from app.connectors.generic_api import GenericAPISource
from app.connectors.greenhouse import GreenhouseSource
from app.connectors.smartrecruiters import SmartRecruitersSource

FIX = Path(__file__).parent / "fixtures"


# ---- source metadata / §34 policy ---------------------------------------------


def test_source_type_inference():
    assert source_type_for("eures_france") == "government"
    assert source_type_for("greenhouse") == "ats"
    assert source_type_for("company_careers") == "company_career"
    assert source_type_for("duckduckgo") == "search_engine"
    assert source_type_for("linkedin") == "social_signal"
    assert source_type_for("my_recruitment_agency") == "recruitment"
    assert source_type_for("random_thing") == "unknown"


def test_connectors_declare_policy():
    for cls in (GreenhouseSource, SmartRecruitersSource, EuresSource, CompanyCareersSource):
        assert cls.access_mode in ("public", "authorized_only", "user_provided")
        assert cls.policy_notice, f"{cls.__name__} must state its access policy (§34)"
        assert cls.source_type


def test_opportunity_metadata_and_quality():
    opp = Opportunity(source="greenhouse", source_type="ats", source_quality=95,
                      external_id="1", title="Technician", url="https://x/j")
    assert opp.effective_source_type() == "ats"
    assert opp.effective_quality() == 95
    assert opp.sponsorship_signal == "unknown"  # absence must stay 'unknown' (spec §10)
    derived = Opportunity(source="government_portal", external_id="2", title="Tech",
                          url="https://x/j")
    assert derived.effective_source_type() == "government"
    assert derived.effective_quality() == 100


# ---- canonical identity / §21 ---------------------------------------------------


def test_canonical_job_id_cross_source():
    a = Opportunity(source="greenhouse:x", external_id="greenhouse:x:1",
                    title="  Civil Engineering Technician  ", company="Bouygues ",
                    country="France", url="https://boards.greenhouse.io/x/1")
    b = Opportunity(source="company_careers", external_id="https://b/careers/j1",
                    title="Civil Engineering Technician", company="bouygues",
                    country="france", url="https://b/careers/j1")
    assert a.canonical_job_id() == b.canonical_job_id()
    c = Opportunity(source="other", external_id="3", title="Site Technician",
                    company="Bouygues", country="France", url="https://x/3")
    assert a.canonical_job_id() != c.canonical_job_id()


def test_dedup_v2_canonical_cross_source(db):
    from app.deduplication import find_duplicates

    def job_data(opp: Opportunity, company_id: int) -> dict:
        return {
            "source": opp.source, "external_id": opp.external_id, "dedup_key": opp.dedup_key(),
            "title": opp.title, "company_id": company_id, "location": "Paris, France",
            "country": opp.country, "description": "", "url": opp.url, "posted_at": None,
            "employment_type": "full_time", "salary": "", "contact_email": "", "status": "new",
            "source_type": opp.effective_source_type(), "source_quality": opp.effective_quality(),
            "closing_at": None, "language": "", "sponsorship_signal": "unknown",
            "international_candidate_signal": "unknown", "relocation_signal": "unknown",
            "work_permit_signal": "unknown", "verification_status": "verified",
            "search_query": "", "search_language": "", "search_country": "",
            "canonical_job_id": opp.canonical_job_id(),
        }

    first = Opportunity(source="greenhouse:x", source_type="ats", external_id="greenhouse:x:1",
                        title="Civil Engineering Technician", company="Bouygues",
                        country="France", url="https://boards.greenhouse.io/x/1")
    again = Opportunity(source="company_careers", source_type="company_career",
                        external_id="https://b/careers/j1",
                        title="Civil Engineering Technician", company="Bouygues",
                        country="France", url="https://b/careers/j1")
    company = mem.store.get_or_create_company(db, first.company, first.url, first.country)
    mem.store.upsert_job(db, job_data(first, company.id))
    db.flush()
    dup = find_duplicates(db, [again])
    assert dup == {0: "exact"}, "same vacancy from a second source must be a canonical duplicate"


# ---- greenhouse ----------------------------------------------------------------


def test_greenhouse_happy_path():
    src = GreenhouseSource(boards=["bouygues"], data_path=FIX / "greenhouse.json")
    ops = asyncio.run(src.search("civil engineering"))
    assert ops and all(o.source_type == "ats" for o in ops)
    assert all(o.url.startswith("https://") for o in ops)
    assert all(o.title for o in ops)
    assert any("paris, france" in o.location.lower() for o in ops)


def test_greenhouse_query_filter_and_empty():
    src = GreenhouseSource(boards=["bouygues"], data_path=FIX / "greenhouse.json")
    ops = asyncio.run(src.search(""))
    assert len(ops) == 2
    roads = asyncio.run(src.search("road"))
    assert len(roads) == 1 and "Road" in roads[0].title
    none = GreenhouseSource(boards=[], data_path=FIX / "greenhouse.json")
    assert asyncio.run(none.search("civil")) == []


def test_greenhouse_offline_empty_when_unreadable(tmp_path):
    bad = tmp_path / "nope.json"
    bad.write_text("{not json", encoding="utf-8")
    src = GreenhouseSource(boards=["bouygues"], data_path=bad)
    assert asyncio.run(src.search("civil")) == []  # degrade, never crash


# ---- smartrecruiters -----------------------------------------------------------


def test_smartrecruiters_happy_path():
    src = SmartRecruitersSource(companies=["colas"], data_path=FIX / "smartrecruiters.json")
    ops = asyncio.run(src.search("technicien"))
    assert ops
    assert all(o.source_type == "ats" for o in ops)
    assert any(o.company == "Colas" for o in ops)
    assert any(o.country == "France" for o in ops)


def test_smartrecruiters_query_filter():
    src = SmartRecruitersSource(companies=["colas"], data_path=FIX / "smartrecruiters.json")
    ops = asyncio.run(src.search("site"))
    assert len(ops) == 1 and ops[0].title == "Site Technician"


# ---- generic json api ----------------------------------------------------------


def test_generic_api_mapping():
    cfg = {
        "name": "test_api",
        "list_path": "results.items",
        "mapping": {
            "title": "title", "company": "employer.name", "location": "city",
            "url": "permalink", "external_id": "uuid", "posted_at": "createdAt",
        },
    }
    src = GenericAPISource(config=cfg, data_path=FIX / "generic_api.json")
    ops = asyncio.run(src.search("civil engineering"))
    assert len(ops) == 1
    first = ops[0]
    assert first.company == "Vinci"
    assert first.country == "Netherlands"
    assert first.external_id == "x1"
    assert first.posted_at is not None


# ---- company careers / ATS detection -------------------------------------------


def test_company_careers_detects_ats():
    html = "<html><body><a href='/jobs/1' class='job-listing'>Civil Technician</a> Workday</body></html>"
    src = CompanyCareersSource(pages=[{"url": "https://x/careers", "company": "X", "ops": {}}])  # type: ignore[attr-defined]
    src._fetch = lambda url: html  # type: ignore[method-assign]
    ops = asyncio.run(src.search("civil"))
    assert ops and ops[0].source_type == "ats"
    assert ops[0].raw.get("ats") == "workday"


def test_detect_ats_known_systems():
    assert detect_ats("https://boards.greenhouse.io/acme") == "greenhouse"
    assert detect_ats("https://wd5.myworkdaysite.com/acme/careers") == "workday"
    assert detect_ats(html="<html>Powered by SAP SuccessFactors</html>") == "successfactors"
    assert detect_ats("https://example.com/careers") == ""


# ---- eures / government --------------------------------------------------------


def test_eures_offline_empty_and_metadata():
    src = EuresSource(feeds=[])
    assert asyncio.run(src.search("civil")) == []
    assert src.source_type == "government"
    assert src.access_mode == "public"
    assert src.policy_notice
    # a misconfigured feed degrades to an empty result instead of crashing
    src2 = EuresSource(feeds=["http://127.0.0.1:1/nope?q={query}"])
    assert asyncio.run(src2.search("civil")) == []


# ---- schema / discovery V2 columns ---------------------------------------------


def test_job_schema_v2_columns(db):
    from sqlalchemy import inspect

    cols = {c["name"] for c in inspect(db.get_bind()).get_columns("jobs")}
    for col in ("source_type", "source_quality", "closing_at", "language",
                "sponsorship_signal", "international_candidate_signal",
                "relocation_signal", "work_permit_signal", "verification_status",
                "search_query", "search_language", "search_country", "canonical_job_id"):
        assert col in cols, f"jobs.{col} missing from schema (§29)"
