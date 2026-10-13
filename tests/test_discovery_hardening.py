from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

from app.config import AgentConfig
from app.connectors.base import Opportunity
from app.connectors.linkedin import LinkedInJobsSource
from app.connectors.search_engine import SearchEngineSource
from app.connectors.tavily_resilience import (
    BTP_TAVILY_DAILY_RESERVE,
    GENERAL_TAVILY_DAILY_LIMIT,
    TAVILY_DAILY_REQUEST_LIMIT,
    ApiAttemptBudget,
    DailyBudget,
    ResilientTavily,
    TavilyCache,
    TavilyKey,
    _cache_key,
    key_fingerprint,
)
from app.discovery.email_verification import EmailVerificationService, is_safe_email
from app.workflows import discovery as discovery_workflow
from app.workflows.discovery import (
    CompanyUrlKind,
    _allocate_source_queries,
    _cap_scheduled_queries,
    _classify_company_urls,
    _direct_company_career_url,
    _trusted_employer_url,
    _upsert_opportunity_company,
    classify_url_role,
)


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
    assert source.last_origin == "api"
    assert asyncio.run(source.search("civil", "Morocco")) == []
    assert source.last_origin == "cache"
    assert source.last_status == "empty"
    assert calls["count"] == 1
    assert budget.remaining() == 19


def test_tavily_cache_entries_expire_and_report_stale_entries(tmp_path):
    cache = TavilyCache(tmp_path / "cache.json", ttl_seconds=0)
    cache.put("query", [{"result": "cached"}])

    assert cache.get("query") is None
    assert cache.has_stale_entry("query")


def test_legacy_tavily_cache_entry_without_timestamp_is_stale(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"legacy": {"results": [{"title": "old"}]}}))
    cache = TavilyCache(path)

    assert cache.get("legacy") is None
    assert cache.has_stale_entry("legacy")


def test_tavily_cache_expires_at_exact_ttl_boundary(tmp_path):
    cache = TavilyCache(tmp_path / "cache.json")
    cache.put("query", [{"title": "cached"}])
    cache._data["query"]["cached_at"] = (
        datetime.now(timezone.utc).timestamp() - 30 * 24 * 60 * 60
    )

    assert cache.get("query") is None
    assert cache.has_stale_entry("query")


def test_tavily_daily_budget_resets_on_utc_date_boundary(tmp_path):
    budget = DailyBudget(tmp_path / "budget.json", limit=2)
    before_midnight = datetime(2025, 4, 5, 23, 59, 59, tzinfo=timezone.utc)
    after_midnight = datetime(2025, 4, 6, 0, 0, 0, tzinfo=timezone.utc)

    assert budget.reserve(now=before_midnight)
    assert budget.remaining(now=before_midnight) == 1
    assert budget.remaining(now=after_midnight) == 2
    assert budget.reserve(now=after_midnight)
    assert budget.remaining(now=after_midnight) == 1


def test_daily_budget_reservations_are_atomic_across_instances_and_threads(tmp_path):
    path = tmp_path / "shared-budget.json"
    budgets = [DailyBudget(path, limit=7) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=12) as executor:
        reservations = list(executor.map(lambda budget: budget.reserve(), budgets))

    assert sum(reservations) == 7
    assert DailyBudget(path, limit=7).remaining() == 0
    assert json.loads(path.read_text())["count"] == 7


def test_tavily_failed_retries_are_charged_and_share_aggregate_cap(
    tmp_path, monkeypatch
):
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(1)
        raise TimeoutError("simulated provider timeout")

    monkeypatch.setattr("app.connectors.tavily_resilience.requests.post", fail)
    run_budget = ApiAttemptBudget(2)
    keys = [
        TavilyKey(
            f"test-key-{index}",
            DailyBudget(tmp_path / f"budget-{index}.json", limit=20),
            key_fingerprint(f"test-key-{index}"),
        )
        for index in range(3)
    ]
    source = ResilientTavily(
        keys=keys,
        cache=TavilyCache(tmp_path / "cache.json"),
        attempt_budget=run_budget,
    )
    second_source = ResilientTavily(
        keys=[keys[0]],
        cache=TavilyCache(tmp_path / "second-cache.json"),
        attempt_budget=run_budget,
    )

    assert asyncio.run(source.search("civil", "Morocco")) == []
    assert source.last_status == "degraded"
    assert source.last_error_code == "provider_request_failed"
    assert source.last_deferred_reason == "run_api_attempt_budget_exhausted"
    assert asyncio.run(second_source.search("construction", "Morocco")) == []
    assert second_source.last_error_code == "run_api_attempt_budget_exhausted"
    assert len(calls) == 2
    assert [key.budget.remaining() for key in keys] == [19, 19, 20]
    assert source.attempt_metrics() == {
        "attempts": 2,
        "successes": 0,
        "failed_attempts": 2,
        "retry_attempts": 1,
    }
    assert run_budget.as_dict() == {
        "limit": 2,
        "attempts": 2,
        "successes": 0,
        "failed_attempts": 2,
        "retry_attempts": 1,
        "remaining": 0,
    }


def test_total_source_query_cap_selects_unique_uncached_combinations_first():
    class Connector:
        def __init__(self, cached=()):
            self.cached = set(cached)

        def has_cached_result(self, query, location):
            return (query, location) in self.cached

    plan = [
        {"query": f"query-{index}", "location": "Morocco", "lang": "fr"}
        for index in range(6)
    ]
    connectors = [
        ({}, Connector()),
        ({}, Connector()),
        ({}, Connector()),
    ]
    scheduled = [plan.copy(), plan.copy(), plan.copy()]

    selected, deferred = _cap_scheduled_queries(scheduled, 5, connectors)
    queries = [
        item["query"] for source_queries in selected for item in source_queries
    ]

    assert len(queries) == 5
    assert len(set(queries)) == 5
    assert sum(deferred) == 18 - 5


def test_api_allocator_spreads_bounded_calls_over_unique_plan_combinations(tmp_path):
    key = "shared-budget-key"
    budget = DailyBudget(tmp_path / "budget.json", limit=20)

    class Connector:
        def __init__(self):
            self.keys = [TavilyKey(key, budget, key_fingerprint(key))]

        @staticmethod
        def has_cached_result(_query, _location):
            return False

    connectors = [({}, Connector()), ({}, Connector()), ({}, Connector())]
    plan = [
        {"query": f"query-{index}", "location": "Morocco", "lang": "ar"}
        for index in range(6)
    ]

    scheduled, deferred, _stale = _allocate_source_queries(
        connectors, plan, 0, rotation_offset=0, max_api_attempts=4,
    )
    selected_queries = [
        combo["query"] for source_queries in scheduled for combo in source_queries
    ]

    assert len(selected_queries) == 4
    assert len(set(selected_queries)) == 4
    assert sum(deferred) == 14


def test_tavily_duplicate_keys_are_deduplicated_before_attempt(tmp_path, monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    calls = []
    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda *_args, **_kwargs: (calls.append(1) or Response()),
    )
    key = "same-key"
    shared_path = tmp_path / "shared.json"
    source = ResilientTavily(
        keys=[
            TavilyKey(key, DailyBudget(shared_path, limit=20), key_fingerprint(key)),
            TavilyKey(key, DailyBudget(shared_path, limit=20), key_fingerprint(key)),
        ],
        cache=TavilyCache(tmp_path / "cache.json"),
    )

    assert len(source.keys) == 1
    assert asyncio.run(source.search("civil", "Morocco")) == []
    assert len(calls) == 1
    assert DailyBudget(shared_path, limit=20).remaining() == 19


def test_tavily_budget_persistence_failure_blocks_provider_request(
    tmp_path, monkeypatch
):
    calls = []
    budget = DailyBudget(tmp_path / "unwritable-budget.json", limit=20)
    monkeypatch.setattr(budget, "_save", lambda: False)
    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda *_args, **_kwargs: calls.append(1),
    )
    run_budget = ApiAttemptBudget(2)
    source = ResilientTavily(
        keys=[TavilyKey("key", budget, key_fingerprint("key"))],
        cache=TavilyCache(tmp_path / "cache.json"),
        attempt_budget=run_budget,
    )

    assert asyncio.run(source.search("civil", "Morocco")) == []
    assert calls == []
    assert source.last_status == "degraded"
    assert source.last_error_code == "budget_ledger_unavailable"
    assert run_budget.as_dict()["attempts"] == 0
    assert budget.remaining() == 0


def test_tavily_budget_exhaustion_has_an_explicit_safe_reason(tmp_path):
    budget = DailyBudget(tmp_path / "budget.json", limit=20)
    budget.exhaust()
    source = ResilientTavily(
        keys=[TavilyKey(
            "test-key",
            budget,
            key_fingerprint("test-key"),
        )],
        cache=TavilyCache(tmp_path / "cache.json"),
    )

    assert asyncio.run(source.search("civil", "Morocco")) == []
    assert source.last_status == "degraded"
    assert source.last_origin == "none"
    assert source.last_error_code == "daily_request_budget_exhausted"


def test_tavily_query_budget_is_shared_fairly_and_respects_per_source_limit(tmp_path):
    budget = DailyBudget(tmp_path / "budget.json", limit=1)

    class CachedSource:
        def __init__(self, name, cached=()):
            self.name = name
            self.keys = [SimpleNamespace(budget=budget)]
            self.cached = set(cached)

        def has_cached_result(self, query, location):
            return (query, location) in self.cached

    sources = [
        ({"name": "first"}, CachedSource("first", {("q1", "MA")})),
        ({"name": "second"}, CachedSource("second")),
        ({"name": "third"}, CachedSource("third")),
    ]
    plan = [
        {"query": "q1", "location": "MA"},
        {"query": "q2", "location": "MA"},
    ]

    scheduled, deferred, stale = _allocate_source_queries(
        sources, plan, max_requests_per_source=2, rotation_offset=0,
    )

    assert [len(items) for items in scheduled] == [1, 1, 0]
    assert sum(deferred) == 4
    assert scheduled[0] == [plan[0]]
    assert scheduled[1] == [plan[0]]
    assert stale == [0, 0, 0]
    assert budget.remaining() == 1
    rotated, _, _ = _allocate_source_queries(
        sources, plan, max_requests_per_source=2, rotation_offset=2,
    )
    assert rotated[2] == [plan[0]]


def test_discovery_applies_shared_tavily_budget_and_reports_deferred_sources(
    db, prefs, profile, monkeypatch, tmp_path
):
    budget = DailyBudget(tmp_path / "budget.json", limit=2)
    calls = []

    class BudgetedSource:
        kind = "tavily"

        def __init__(self, name):
            self.name = name
            self.keys = [SimpleNamespace(budget=budget)]
            self.last_origin = "none"
            self.last_status = "ready"
            self.last_error_code = ""

        def has_cached_result(self, _query, _location):
            return False

        def has_stale_cached_result(self, _query, _location):
            return False

        async def search(self, query, location=""):
            calls.append(self.name)
            budget.record()
            self.last_origin = "api"
            self.last_status = "empty"
            return []

    connectors = [
        ({"name": name}, BudgetedSource(name))
        for name in ("broad", "ats", "job_board")
    ]
    monkeypatch.setattr(
        discovery_workflow, "_load_source_connectors", lambda _path=None: connectors,
    )
    monkeypatch.setattr(
        "app.workflows.adaptive_plan.build_adaptive_plan",
        lambda *_args, **_kwargs: [{
            "query": "fixture query",
            "location": "Morocco",
            "country": "Morocco",
            "lang": "fr",
            "intent": "core",
        }],
    )
    config = AgentConfig(discovery={
        "company_universe_discovery": False,
        "max_requests_per_source": 1,
    })

    report = asyncio.run(discovery_workflow.run_discovery(
        db, config, prefs, profile=profile,
    ))

    assert len(calls) == 2
    assert len(set(calls)) == 2
    assert budget.remaining() == 0
    by_name = {item["name"]: item for item in report.source_reports}
    assert sum(item["queries_budget_deferred"] for item in by_name.values()) == 1
    assert sum(item["queries_api"] for item in by_name.values()) == 2
    assert sum(item["queries"] for item in by_name.values()) == 2


def test_discovery_enforces_aggregate_attempt_cap_across_tavily_connectors(
    db, prefs, profile, monkeypatch, tmp_path
):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    calls = []
    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda *_args, **_kwargs: (calls.append(1) or Response()),
    )
    budget = DailyBudget(tmp_path / "shared-budget.json", limit=20)
    key = TavilyKey("fixture-key", budget, key_fingerprint("fixture-key"))
    connectors = [
        (
            {"name": name, "kind": "tavily"},
            ResilientTavily(
                keys=[key],
                cache=TavilyCache(tmp_path / f"{name}-cache.json"),
                source_name=name,
            ),
        )
        for name in ("broad", "ats")
    ]
    monkeypatch.setattr(
        discovery_workflow, "_load_source_connectors", lambda _path=None: connectors,
    )
    monkeypatch.setattr(
        "app.workflows.adaptive_plan.build_adaptive_plan",
        lambda *_args, **_kwargs: [
            {"query": f"fixture {index}", "location": "Morocco", "country": "Morocco"}
            for index in range(2)
        ],
    )
    config = AgentConfig(discovery={
        "company_universe_discovery": False,
        "max_requests_per_source": 5,
        "max_api_requests_per_run": 1,
    })

    report = asyncio.run(discovery_workflow.run_discovery(
        db, config, prefs, profile=profile,
    ))

    assert len(calls) == 1
    assert report.api_request_budget == {
        "limit": 1,
        "attempts": 1,
        "successes": 1,
        "failed_attempts": 0,
        "retry_attempts": 0,
        "remaining": 0,
    }
    assert sum(item["api_attempts"] for item in report.source_reports) == 1
    assert sum(item["queries_api"] for item in report.source_reports) == 1
    assert sum(
        item["queries_budget_deferred"] for item in report.source_reports
    ) == 3


def test_discovery_reports_tavily_timeouts_as_attempted_failures(
    db, prefs, profile, monkeypatch, tmp_path
):
    def fail(*_args, **_kwargs):
        raise TimeoutError("fixture timeout")

    monkeypatch.setattr("app.connectors.tavily_resilience.requests.post", fail)
    keys = [
        TavilyKey(
            f"failure-key-{index}",
            DailyBudget(tmp_path / f"failure-budget-{index}.json", limit=20),
            key_fingerprint(f"failure-key-{index}"),
        )
        for index in range(3)
    ]
    connector = ResilientTavily(
        keys=keys,
        cache=TavilyCache(tmp_path / "failure-cache.json"),
        source_name="fixture-tavily",
    )
    connectors = [({"name": "fixture-tavily", "kind": "tavily"}, connector)]
    monkeypatch.setattr(
        discovery_workflow, "_load_source_connectors", lambda _path=None: connectors,
    )
    monkeypatch.setattr(
        "app.workflows.adaptive_plan.build_adaptive_plan",
        lambda *_args, **_kwargs: [{
            "query": "fixture timeout query",
            "location": "Morocco",
            "country": "Morocco",
        }],
    )
    config = AgentConfig(discovery={
        "company_universe_discovery": False,
        "max_requests_per_source": 1,
        "max_api_requests_per_run": 2,
    })

    report = asyncio.run(discovery_workflow.run_discovery(
        db, config, prefs, profile=profile,
    ))

    source_report = report.source_reports[0]
    assert source_report["queries_api"] == 0
    assert source_report["queries_empty"] == 0
    assert source_report["queries_failed"] == 1
    assert source_report["api_attempts"] == 2
    assert source_report["api_failed_attempts"] == 2
    assert source_report["api_retry_attempts"] == 1
    assert source_report["api_attempts_budget_deferred"] == 1
    assert source_report["query_error_codes"] == {"provider_request_failed": 1}
    assert report.api_request_budget["attempts"] == 2
    assert report.api_request_budget["failed_attempts"] == 2


def test_expired_tavily_cache_with_no_run_budget_is_not_treated_as_empty(
    tmp_path, monkeypatch
):
    source_cache = TavilyCache(tmp_path / "cache.json")
    source_cache.put("unused", [])
    key = "stale-key"
    source = ResilientTavily(
        keys=[TavilyKey(
            key,
            DailyBudget(tmp_path / "budget.json", limit=20),
            key_fingerprint(key),
        )],
        cache=source_cache,
        attempt_budget=ApiAttemptBudget(0),
    )
    query_key = _cache_key(source._build_query("civil", "Morocco"), "Morocco")
    source_cache._data[query_key] = {
        "cached_at": datetime.now(timezone.utc).timestamp() - 31 * 24 * 60 * 60,
        "results": [],
    }
    calls = []
    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda *_args, **_kwargs: calls.append(1),
    )

    assert source.has_stale_cached_result("civil", "Morocco")
    assert asyncio.run(source.search("civil", "Morocco")) == []
    assert calls == []
    assert source.last_origin == "none"
    assert source.last_status == "degraded"
    assert source.last_error_code == "run_api_attempt_budget_exhausted"


def test_discovery_reports_new_empty_failed_and_deduplicated_results(
    db, prefs, profile, monkeypatch
):
    class FixtureSource:
        def __init__(self, name, results=None, error=None):
            self.name = name
            self.kind = "fixture"
            self.results = results or []
            self.error = error
            self.last_status = "ready"

        async def search(self, query, location=""):
            if self.error:
                self.last_status = "degraded"
                raise RuntimeError(self.error)
            self.last_status = "ok" if self.results else "empty"
            return self.results

    repeated = Opportunity(
        source="fixture_jobs",
        source_type="job_board",
        external_id="https://jobs.example.test/role/1",
        title="Civil Engineering Technician",
        company="Fixture Construction",
        location="Casablanca, Morocco",
        country="Morocco",
        url="https://jobs.example.test/role/1",
    )
    malformed = Opportunity(
        source="fixture_jobs",
        external_id="invalid",
        title="x",
        company="Fixture Construction",
        url="https://jobs.example.test/role/invalid",
    )
    connectors = [
        ({"name": "legitimate"}, FixtureSource("legitimate", [repeated, repeated])),
        ({"name": "empty"}, FixtureSource("empty")),
        ({"name": "failed"}, FixtureSource("failed", error="fixture transport failure")),
        ({"name": "invalid"}, FixtureSource("invalid", [malformed])),
    ]
    monkeypatch.setattr(
        discovery_workflow, "_load_source_connectors", lambda _path=None: connectors,
    )
    monkeypatch.setattr(
        "app.workflows.adaptive_plan.build_adaptive_plan",
        lambda *_args, **_kwargs: [{
            "query": "fixture query",
            "location": "Morocco",
            "country": "Morocco",
            "lang": "fr",
            "intent": "core",
        }],
    )
    config = AgentConfig(discovery={"company_universe_discovery": False})

    report = asyncio.run(discovery_workflow.run_discovery(
        db, config, prefs, profile=profile,
    ))

    assert report.opportunities_fetched == 2
    assert report.new_jobs == 1
    assert report.duplicates == 1
    assert report.source_errors == ["failed: fixture transport failure"]
    by_name = {item["name"]: item for item in report.source_reports}
    assert by_name["legitimate"]["new"] == 1
    assert by_name["legitimate"]["duplicate_reasons"]["same_run"] == 1
    assert by_name["empty"]["queries_empty"] == 1
    assert by_name["empty"]["queries_stale_cache"] == 0
    assert by_name["empty"]["health"] == "empty"
    assert by_name["failed"]["queries_failed"] == 1
    assert by_name["failed"]["health"] == "failed"
    assert by_name["invalid"]["fetched"] == 1
    assert by_name["invalid"]["normalization_rejected"] == 1
    assert any("no new jobs" in alert for alert in report.alerts) is False


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


def test_email_extraction_accepts_sentence_punctuation_after_public_address():
    from app.discovery.email_verification import EMAIL_RE

    assert EMAIL_RE.findall("Envoyez le CV à drh-contact@cimar.co.ma.") == [
        "drh-contact@cimar.co.ma",
    ]


def test_alternate_employer_email_requires_same_page_instruction_evidence():
    from app.discovery.website_researcher import EvidenceFact

    service = EmailVerificationService()
    address = "drh-contact@cimar.co.ma"
    source_url = "https://www.cimentsdumaroc.com/fr/contacteznous"
    publication = EvidenceFact(
        source_url=source_url,
        source_type="contact",
        page_title="Contact",
        field_name="recruitment_email",
        extracted_value=address,
        snippet=f"Envoyez votre CV pour le poste souhaité à {address}.",
        domain_relationship="official_alternate_recruitment_domain",
        confidence=95,
        reason_code="official_page_explicitly_publishes_alternate_recruitment_domain",
    )
    instruction = EvidenceFact(
        source_url=source_url,
        source_type="contact",
        page_title="Contact",
        field_name="application_instructions",
        extracted_value=f"Envoyez votre CV pour le poste souhaité à {address}.",
        snippet=f"Envoyez votre CV pour le poste souhaité à {address}.",
        confidence=95,
    )

    verified = service.verify(
        address,
        source_url=source_url,
        source_type="company_career",
        official=True,
        employer_domain="cimentsdumaroc.com",
        publication_evidence=publication,
        application_instruction=instruction,
    )
    assert verified.verified
    assert verified.verification_method == (
        "official_page_published_alternate_recruitment_domain"
    )

    assert not service.verify(
        address,
        source_url=source_url,
        source_type="company_career",
        official=True,
        employer_domain="cimentsdumaroc.com",
    ).verified
    mismatched_instruction = SimpleNamespace(
        source_url="https://other.example/apply",
        field_name="application_instructions",
        extracted_value=instruction.extracted_value,
        snippet=instruction.snippet,
    )
    assert not service.verify(
        address,
        source_url=source_url,
        source_type="company_career",
        official=True,
        employer_domain="cimentsdumaroc.com",
        publication_evidence=publication,
        application_instruction=mismatched_instruction,
    ).verified


def test_alternate_domain_evidence_does_not_authorize_free_mail_or_ats():
    from app.discovery.website_researcher import EvidenceFact

    service = EmailVerificationService()
    source_url = "https://acme.ma/careers"
    instruction_text = "Send your CV to "
    instruction = SimpleNamespace(
        source_url=source_url,
        field_name="application_instructions",
        extracted_value="Send your CV and application to contact@greenhouse.io.",
        snippet="Send your CV and application to contact@greenhouse.io.",
    )
    for address in ("person@gmail.com", "jobs@greenhouse.io"):
        publication = EvidenceFact(
            source_url=source_url,
            source_type="careers",
            page_title="Careers",
            field_name="recruitment_email",
            extracted_value=address,
            snippet=instruction_text + address,
            domain_relationship="official_alternate_recruitment_domain",
        )
        instruction.extracted_value = f"Send your CV to {address}."
        instruction.snippet = instruction.extracted_value
        assert not service.verify(
            address,
            source_url=source_url,
            source_type="company_career",
            official=True,
            employer_domain="acme.ma",
            publication_evidence=publication,
            application_instruction=instruction,
        ).verified


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


def test_company_url_classification_never_promotes_job_or_ats_urls():
    direct_careers = SimpleNamespace(
        url="https://build.example.ma/careers/job-123",
        raw={"page": "https://build.example.ma/careers"},
    )
    ats = SimpleNamespace(
        url="https://jobs.lever.co/build/123",
        raw={"ats": "lever"},
    )
    job_board = SimpleNamespace(
        url="https://www.linkedin.com/jobs/view/123",
        raw={},
    )
    unrelated_social = SimpleNamespace(
        url="https://www.instagram.com/company-profile",
        raw={},
    )
    directory = SimpleNamespace(
        url="https://www.kerix.ma/annuaire/entreprises",
        raw={"page": "https://www.kerix.ma/annuaire/entreprises"},
    )
    third_party = SimpleNamespace(
        url="https://www.scribd.com/document/123/build-company-profile",
        raw={"page": "https://www.scribd.com/document/123/build-company-profile"},
    )
    untrusted_career_host = SimpleNamespace(
        url="https://www.linkedin.com/jobs/view/123",
        raw={"page": "https://www.linkedin.com/jobs/view/123"},
    )
    explicit_domain_from_ats = SimpleNamespace(
        url="https://jobs.lever.co/build/123",
        raw={"ats": "lever", "company_website": "https://build.example.ma"},
    )

    career = _classify_company_urls(direct_careers, "company_career")
    assert career["kind"] == CompanyUrlKind.EMPLOYER_CAREERS_PAGE
    assert career["website"] == "https://build.example.ma/"
    assert career["official_domain"] == "build.example.ma"
    assert career["careers_url"] == "https://build.example.ma/careers"

    assert _classify_company_urls(ats, "ats")["kind"] == CompanyUrlKind.ATS
    assert not _classify_company_urls(ats, "ats")["website"]
    assert _classify_company_urls(job_board, "social_signal")["kind"] == (
        CompanyUrlKind.JOB_BOARD
    )
    assert _classify_company_urls(unrelated_social, "social_signal")["kind"] == (
        CompanyUrlKind.THIRD_PARTY_COMPANY_PROFILE
    )
    assert _classify_company_urls(directory, "company_directory")["kind"] == CompanyUrlKind.DIRECTORY
    assert _classify_company_urls(third_party, "document_host")["kind"] == (
        CompanyUrlKind.THIRD_PARTY_COMPANY_PROFILE
    )
    assert not _classify_company_urls(untrusted_career_host, "company_career")["website"]
    explicit = _classify_company_urls(explicit_domain_from_ats, "ats")
    assert explicit["website"] == "https://build.example.ma/"
    assert explicit["official_domain"] == "build.example.ma"


def test_url_roles_keep_directories_registries_profiles_and_documents_out_of_employer_sites():
    assert classify_url_role(
        "https://www.kerix.ma/annuaire/atlas", "Atlas BTP", "Construction company"
    ) == CompanyUrlKind.COMPANY_DIRECTORY
    assert classify_url_role(
        "https://www.maroc1000.net/Secteur-d'activite_BTP",
        "Entreprises BTP au Maroc",
    ) == CompanyUrlKind.COMPANY_DIRECTORY
    assert classify_url_role(
        "https://marocproguide.com/btp-agadir.html", "Professionnels du BTP"
    ) == CompanyUrlKind.COMPANY_DIRECTORY
    assert classify_url_role(
        "https://registre.gov.ma/company/atlas", "Company registry"
    ) == CompanyUrlKind.GOVERNMENT_COMPANY_REGISTRY
    assert classify_url_role(
        "https://www.rocketreach.co/atlas-profile", "Atlas profile"
    ) == CompanyUrlKind.THIRD_PARTY_COMPANY_PROFILE
    assert classify_url_role(
        "https://docs.google.com/document/d/123", "Company profile"
    ) == CompanyUrlKind.DOCUMENT_HOST
    assert classify_url_role(
        "https://careers.atlas.ma/jobs", source_type="company_career"
    ) == CompanyUrlKind.EMPLOYER_CAREERS_PAGE
    assert _trusted_employer_url("https://www.kerix.ma/annuaire/atlas") == ""
    assert _trusted_employer_url("https://registre.gov.ma/company/atlas") == ""
    assert _trusted_employer_url("https://atlas.ma/careers") == "https://atlas.ma/careers"


def test_explicit_directory_claim_does_not_promote_or_lose_source_provenance():
    opportunity = SimpleNamespace(
        url="https://www.kerix.ma/annuaire/atlas",
        title="Atlas BTP - Kerix",
        description="Construction services in Morocco",
        raw={"company_website": "https://www.kerix.ma/annuaire/atlas"},
    )

    urls = _classify_company_urls(opportunity, "company_directory")

    assert not urls["website"]
    assert not urls["official_domain"]
    assert urls["kind"] == CompanyUrlKind.DIRECTORY
    assert urls["discovery_source_url"] == "https://www.kerix.ma/annuaire/atlas"


def test_company_table_persists_direct_employer_site_but_not_job_or_ats_url(db):
    career_opportunity = Opportunity(
        source="company_careers",
        source_type="company_career",
        external_id="career-job-1",
        title="Technicien BTP",
        company="Build Morocco",
        location="Rabat, Morocco",
        country="Morocco",
        url="https://build.example.ma/careers/job-1",
        raw={"page": "https://build.example.ma/careers"},
    )
    company, urls = _upsert_opportunity_company(
        db, career_opportunity, "company_career"
    )

    assert urls["kind"] == CompanyUrlKind.EMPLOYER_CAREERS_PAGE
    assert company.website == "https://build.example.ma/"
    assert company.official_domain == "build.example.ma"
    assert company.careers_url == "https://build.example.ma/careers"

    ats_opportunity = Opportunity(
        source="lever:build-morocco",
        source_type="ats",
        external_id="lever-job-2",
        title="Civil Engineer",
        company="Build ATS Employer",
        country="Morocco",
        url="https://jobs.lever.co/build/2",
        raw={"ats": "lever"},
    )
    ats_company, ats_urls = _upsert_opportunity_company(db, ats_opportunity, "ats")
    assert ats_urls["kind"] == CompanyUrlKind.ATS
    assert ats_company.website == ""
    assert ats_company.official_domain == ""

    conflicting = Opportunity(
        source="company_careers",
        source_type="company_career",
        external_id="other-career",
        title="Technicien",
        company="Build Morocco",
        country="Morocco",
        url="https://other-build.example/careers/job-3",
        raw={"page": "https://other-build.example/careers"},
    )
    same_company, _ = _upsert_opportunity_company(db, conflicting, "company_career")
    assert same_company.official_domain == "build.example.ma"
    assert same_company.website == "https://build.example.ma/"
    assert same_company.careers_url == "https://build.example.ma/careers"
