from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.connectors.linkedin import LinkedInJobsSource
from app.connectors.search_engine import SearchEngineSource
from app.connectors.tavily_resilience import (
    BTP_TAVILY_DAILY_RESERVE,
    GENERAL_TAVILY_DAILY_LIMIT,
    TAVILY_DAILY_REQUEST_LIMIT,
    DailyBudget,
    ResilientTavily,
    TavilyCache,
    TavilyKey,
    key_fingerprint,
)
from app.discovery.email_verification import EmailVerificationService, is_safe_email
from app.workflows.discovery import _direct_company_career_url


def test_linkedin_index_results_are_kept_without_allowing_direct_fetch():
    assert not SearchEngineSource._allowed("https://www.linkedin.com/jobs/view/1")
    assert SearchEngineSource._allowed(
        "https://www.linkedin.com/jobs/view/1", allow_social_index=True
    )

    async def search_fn(query, location):
        return [{"url": "https://www.linkedin.com/jobs/view/1", "title": "Civil Engineer"}]

    results = asyncio.run(
        LinkedInJobsSource(search_fn=search_fn).search("civil", "Morocco")
    )
    assert len(results) == 1
    assert results[0].verification_status == "unverified"


def test_tavily_negative_results_are_cached_and_charged_once(tmp_path, monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    calls = {"count": 0}

    def post(*args, **kwargs):
        calls["count"] += 1
        return Response()

    monkeypatch.setattr("app.connectors.tavily_resilience.requests.post", post)
    key = "test-key"
    budget = DailyBudget(tmp_path / "budget.json", limit=20)
    source = ResilientTavily(
        keys=[TavilyKey(key, budget, key_fingerprint(key))],
        cache=TavilyCache(tmp_path / "cache.json"),
    )

    assert asyncio.run(source.search("civil", "Morocco")) == []
    assert asyncio.run(source.search("civil", "Morocco")) == []
    assert calls["count"] == 1
    assert budget.remaining() == 19


def test_tavily_daily_budget_reserves_three_requests_for_btp(tmp_path):
    assert TAVILY_DAILY_REQUEST_LIMIT == 20
    assert BTP_TAVILY_DAILY_RESERVE == 3
    assert GENERAL_TAVILY_DAILY_LIMIT == 17
    path = tmp_path / "shared-budget.json"
    general = DailyBudget(path, limit=GENERAL_TAVILY_DAILY_LIMIT)

    for _ in range(GENERAL_TAVILY_DAILY_LIMIT):
        general.record()

    btp = DailyBudget(path, limit=TAVILY_DAILY_REQUEST_LIMIT)
    assert general.remaining() == 0
    assert btp.remaining() == BTP_TAVILY_DAILY_RESERVE


def test_provider_quota_exhaustion_is_shared_across_reserved_budget_views(tmp_path):
    path = tmp_path / "shared-budget.json"
    general = DailyBudget(path, limit=GENERAL_TAVILY_DAILY_LIMIT)
    general.exhaust()

    btp = DailyBudget(path, limit=TAVILY_DAILY_REQUEST_LIMIT)

    assert general.remaining() == 0
    assert btp.remaining() == 0


def test_email_verification_requires_employer_domain():
    assert is_safe_email("recruitment@geotest.ma")
    service = EmailVerificationService()
    mismatch = service.verify(
        "support@greenhouse.io",
        source_url="https://acme.ma/careers",
        source_type="ats",
    )
    assert not mismatch.verified
    assert mismatch.verification_method == "domain_mismatch"

    verified = service.verify(
        "recruitment@acme.ma",
        source_url="https://acme.ma/careers",
        source_type="company_career",
    )
    assert verified.verified
    assert verified.confidence == 100

    parent_domain = service.verify(
        "jobs@acme.ma",
        source_url="https://careers.acme.ma/jobs",
        source_type="company_career",
    )
    assert not parent_domain.verified

    unrelated_domain = service.verify(
        "jobs@other.ma",
        source_url="https://jobs.lever.co/acme",
        source_type="ats",
    )
    assert not unrelated_domain.verified


def test_company_website_provenance_excludes_ats_and_requires_employer_https_page():
    direct = SimpleNamespace(raw={"page": "https://build.example.ma/careers"})
    ats_flagged = SimpleNamespace(raw={
        "page": "https://build.example.ma/careers",
        "ats": True,
    })
    vendor_page = SimpleNamespace(raw={"page": "https://jobs.myworkdayjobs.com/build"})
    http_page = SimpleNamespace(raw={"page": "http://build.example.ma/careers"})

    assert _direct_company_career_url(direct, "company_career") == (
        "https://build.example.ma/careers"
    )
    assert not _direct_company_career_url(direct, "ats")
    assert not _direct_company_career_url(ats_flagged, "company_career")
    assert not _direct_company_career_url(vendor_page, "company_career")
    assert not _direct_company_career_url(http_page, "company_career")
