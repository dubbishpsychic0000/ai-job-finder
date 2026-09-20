from __future__ import annotations

import asyncio

from app.connectors.linkedin import LinkedInJobsSource
from app.connectors.search_engine import SearchEngineSource
from app.connectors.tavily_resilience import (
    DailyBudget,
    ResilientTavily,
    TavilyCache,
    TavilyKey,
    key_fingerprint,
)
from app.discovery.email_verification import EmailVerificationService, is_safe_email


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
