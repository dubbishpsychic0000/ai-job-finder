from __future__ import annotations

import asyncio

from app.config import AgentConfig, Preferences
from app.connectors.ats_detect import detect_ats
from app.connectors.tavily_resilience import (
    DailyBudget,
    ResilientTavily,
    TavilyCache,
    TavilyKey,
    key_fingerprint,
)
from app.discovery.country_ranking import CountryScore
from app.workflows import adaptive_plan
from app.workflows.discovery import (
    _allocate_source_queries,
    _query_outcome_state,
    _source_query_trace,
)
from app.workflows.search_plan import SearchPlan


def test_explicit_first_target_country_survives_adaptive_tight_cap(
    db, monkeypatch,
):
    preferences = Preferences(
        target_countries=["Morocco", "France", "Canada"],
        target_roles=["civil engineering technician"],
    )
    plan = [
        {
            "query": f"role-{country}",
            "location": country,
            "country": country,
            "lang": "fr",
            "intent": "role",
            "weight": 1.0,
        }
        for country in preferences.countries
    ]
    monkeypatch.setattr(
        adaptive_plan.SearchPlan,
        "build",
        lambda *_args, **_kwargs: plan,
    )
    monkeypatch.setattr(
        adaptive_plan,
        "rank_countries",
        lambda *_args, **_kwargs: [
            CountryScore("France", 100),
            CountryScore("Canada", 90),
            CountryScore("Morocco", 1),
        ],
    )
    monkeypatch.setattr(
        adaptive_plan, "aggregate_query_stat", lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(adaptive_plan, "budget_remaining", lambda *_args: 1)
    monkeypatch.setattr(adaptive_plan, "repeats_for", lambda _stat: 1)

    selected = adaptive_plan.build_adaptive_plan(
        db,
        preferences,
        AgentConfig(discovery={"max_daily_search_queries": 1}),
        max_queries_per_run=1,
        learning_sources={"fixture"},
    )

    assert len(selected) == 1
    assert selected[0]["country"] == "Morocco"


def test_source_allocator_routes_morocco_domains_and_keeps_broad_sources(
    tmp_path,
):
    key = "allocation-test-key"
    budget = DailyBudget(tmp_path / "budget.json", limit=8)

    class Source:
        def __init__(self):
            self.keys = [TavilyKey(key, budget, key_fingerprint(key))]

        @staticmethod
        def has_cached_result(_query, _location):
            return False

        @staticmethod
        def has_stale_cached_result(_query, _location):
            return False

    connectors = [
        (
            {
                "name": "web",
                "applies_to_countries": ["*"],
                "allocation_priority": 100,
            },
            Source(),
        ),
        (
            {
                "name": "ats",
                "official_only": True,
                "applies_to_countries": ["*"],
                "allocation_priority": 95,
            },
            Source(),
        ),
        (
            {
                "name": "morocco-board",
                "domains": ["rekrute.com"],
                "applies_to_countries": ["Morocco"],
                "allocation_priority": 90,
            },
            Source(),
        ),
    ]
    plan = [
        {
            "query": f"civil role {country} {language}",
            "location": country,
            "country": country,
            "lang": language,
        }
        for country, language in (
            ("Morocco", "fr"),
            ("Morocco", "ar"),
            ("France", "fr"),
            ("Canada", "en"),
        )
    ]

    scheduled, deferred, _stale = _allocate_source_queries(
        connectors,
        plan,
        max_requests_per_source=0,
        rotation_offset=0,
        max_api_attempts=5,
    )

    board_countries = {
        combo["country"] for combo in scheduled[2]
    }
    assert board_countries == {"Morocco"}
    assert any(combo["country"] == "Morocco" for combo in scheduled[0])
    assert any(combo["country"] == "Morocco" for combo in scheduled[1])
    assert any(combo["country"] == "France" for combo in scheduled[0] + scheduled[1])
    assert any(combo["country"] == "Canada" for combo in scheduled[0] + scheduled[1])
    assert sum(map(len, scheduled)) == 5
    assert sum(deferred) == 5


def test_twelve_combination_plan_keeps_morocco_and_its_local_languages(profile):
    preferences = Preferences(
        target_countries=["Morocco", "France", "Germany", "Canada", "Spain"],
        target_roles=["civil engineering technician"],
    )

    plan = SearchPlan(preferences, profile=profile).build(
        max_per_country=3,
        max_queries_per_run=12,
    )

    morocco = [item for item in plan if item["country"] == "Morocco"]
    assert morocco
    assert {"fr", "ar"} <= {item["lang"] for item in morocco}
    assert len(plan) <= 12


def test_production_sources_declare_country_applicability():
    import yaml

    from app.config import ROOT_DIR

    source_config = yaml.safe_load(
        (ROOT_DIR / "config" / "sources.yaml").read_text(encoding="utf-8"),
    )
    enabled_tavily = [
        source for source in source_config["connectors"]
        if source.get("enabled", True) and source["kind"] == "tavily"
    ]
    broad_sources = [
        source for source in enabled_tavily if not source.get("domains")
    ]
    domestic_sources = [
        source for source in enabled_tavily if source.get("domains")
    ]

    assert {source["name"] for source in broad_sources} == {
        "tavily_web_search",
        "tavily_official_ats_search",
    }
    assert all(source["applies_to_countries"] == ["*"] for source in broad_sources)
    assert domestic_sources
    assert all(
        source["applies_to_countries"] == ["Morocco"]
        for source in domestic_sources
    )


def test_request_cache_is_scoped_to_source_filters(tmp_path):
    cache = TavilyCache(tmp_path / "cache.json")
    key = "cache-scope-key"
    shared_keys = [
        TavilyKey(key, DailyBudget(tmp_path / "budget.json", limit=20),
                  key_fingerprint(key)),
    ]
    rekrute = ResilientTavily(
        keys=shared_keys,
        cache=cache,
        domains=["rekrute.com"],
        source_name="morocco-board",
        query_suffix="civil jobs",
    )
    emploi = ResilientTavily(
        keys=shared_keys,
        cache=cache,
        domains=["emploi.ma"],
        source_name="morocco-board",
        query_suffix="civil jobs",
    )
    query = "technicien génie civil"
    location = "Morocco"
    cache.put(
        _cache_entry_key(rekrute, query, location),
        [],
    )

    assert rekrute.has_cached_result(query, location)
    assert not emploi.has_cached_result(query, location)
    assert rekrute.request_scope_id != emploi.request_scope_id


def test_provider_results_filtered_by_domain_are_not_reported_as_empty(
    tmp_path, monkeypatch,
):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [{
                    "url": "https://jobs.example.com/civil-role",
                    "title": "Civil technician",
                    "content": "Construction role",
                }],
            }

    calls = []

    def post(*_args, **_kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        post,
    )
    key = "filtering-test-key"
    source = ResilientTavily(
        keys=[TavilyKey(
            key,
            DailyBudget(tmp_path / "filter-budget.json", limit=20),
            key_fingerprint(key),
        )],
        cache=TavilyCache(tmp_path / "filter-cache.json"),
        domains=["rekrute.com"],
        source_name="morocco-board",
    )

    import asyncio

    results = asyncio.run(source.search("civil technician", "Morocco"))

    assert results == []
    assert source.last_provider_result_count == 1
    assert source.last_accepted_result_count == 0
    assert source.last_status == "filtered"
    assert source.last_origin == "api"
    assert asyncio.run(source.search("civil technician", "Morocco")) == []
    assert source.last_status == "filtered"
    assert source.last_provider_result_count == 1
    assert source.last_origin == "cache"
    assert len(calls) == 1


def test_official_ats_filter_uses_host_not_untrusted_url_substrings(
    tmp_path, monkeypatch,
):
    from app.connectors.tavily_resilience import ApiAttemptBudget

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "url": "https://jobs.lever.co/acme/123",
                        "title": "Civil Engineering Technician",
                        "content": "Public ATS result.",
                    },
                    {
                        "url": "https://jobs.smartrecruiters.com/Acme/123-role",
                        "title": "Junior Civil Engineer",
                        "content": "Public ATS result.",
                    },
                    {
                        "url": (
                            "https://example.test/redirect/"
                            "https://jobs.lever.co/acme/123"
                        ),
                        "title": "Spoofed ATS URL",
                        "content": "Not an ATS host.",
                    },
                    {
                        "url": "https://example.test/careers",
                        "title": "Careers",
                        "content": "Not an ATS posting.",
                    },
                    {
                        "url": None,
                        "title": "Missing URL",
                        "content": "Malformed provider result.",
                    },
                    {
                        "url": "https://jobs.lever.co/acme/456",
                        "title": "Malformed content",
                        "content": {"unexpected": "type"},
                    },
                ],
            }

    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda *_args, **_kwargs: Response(),
    )
    source = ResilientTavily(
        keys=[TavilyKey(
            "official-filter-key",
            DailyBudget(tmp_path / "official-budget.json", limit=17),
            key_fingerprint("official-filter-key"),
        )],
        cache=TavilyCache(tmp_path / "official-cache.json"),
        official_only=True,
        source_name="official-ats",
        attempt_budget=ApiAttemptBudget(2),
    )

    results = asyncio.run(source.search("civil engineer", "Morocco"))

    assert {item.url for item in results} == {
        "https://jobs.lever.co/acme/123",
        "https://jobs.smartrecruiters.com/Acme/123-role",
        "https://jobs.lever.co/acme/456",
    }
    assert source.last_provider_result_count == 6
    assert source.last_accepted_result_count == 3
    assert source.last_filter_reason_counts == {
        "unrecognized_official_ats_host": 2,
        "missing_result_url": 1,
    }
    assert next(
        item for item in results if item.title == "Malformed content"
    ).description == ""
    path_pattern = source.last_filter_examples[0]["path_pattern"]
    assert all(
        part == "{item}" for part in path_pattern.strip("/").split("/")
    )
    assert "jobs.lever.co" not in path_pattern
    assert "acme" not in path_pattern
    assert detect_ats(None) == ""
    assert detect_ats(
        "https://example.test/jobs.lever.co/acme/123",
    ) == ""


def test_ats_filter_keeps_job_boards_and_employer_careers_out_of_ats_allowlist(
    tmp_path,
):
    source = ResilientTavily(
        keys=[TavilyKey(
            "ats-host-test-key",
            DailyBudget(tmp_path / "ats-host-budget.json", limit=17),
            key_fingerprint("ats-host-test-key"),
        )],
        cache=TavilyCache(tmp_path / "ats-hosts-cache.json"),
        official_only=True,
        source_name="official-ats",
    )
    accepted = (
        "https://jobs.lever.co/acme/123",
        "https://boards.greenhouse.io/acme/jobs/123",
        "https://jobs.smartrecruiters.com/Acme/123-role",
        "https://jobs.ashbyhq.com/acme/123",
    )
    rejected = (
        "https://people.bayt.com/profile",
        "https://www.bayt.com/en/jobs/123",
        "https://www.indeed.com/viewjob",
        "https://www.stepstone.be/jobs/123",
        "https://englishjobs.be/jobs/123",
        "https://www.sweco.careers/be/job/123",
        "https://joobaz.com/job/123",
        "https://example.test/jobs.lever.co/acme/123",
    )

    assert all(not source._result_filter_reason(url, "Civil Engineer") for url in accepted)
    assert all(
        source._result_filter_reason(url, "Civil Engineer")
        == "unrecognized_official_ats_host"
        for url in rejected
    )
    assert source._result_filter_reason(
        "https://be.linkedin.com/jobs/123", "Civil Engineer",
    ) == "blocked_social_domain"


def test_anapec_domain_scope_accepts_official_subdomains_and_rejects_others(
    tmp_path, monkeypatch,
):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "url": "https://www.anapec.org/offres-emploi/123",
                        "title": "Technicien travaux publics",
                        "content": "ANAPEC public listing.",
                    },
                    {
                        "url": "https://jobs.example.test/offres-emploi/123",
                        "title": "Technicien travaux publics",
                        "content": "Outside the configured source scope.",
                    },
                ],
            }

    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda *_args, **_kwargs: Response(),
    )
    source = ResilientTavily(
        keys=[TavilyKey(
            "anapec-scope-key",
            DailyBudget(tmp_path / "anapec-budget.json", limit=17),
            key_fingerprint("anapec-scope-key"),
        )],
        cache=TavilyCache(tmp_path / "anapec-cache.json"),
        domains=["anapec.org"],
        source_name="tavily_anapec",
        result_source_type="government",
    )

    results = asyncio.run(source.search("technicien BTP", "Morocco"))

    assert [item.url for item in results] == [
        "https://www.anapec.org/offres-emploi/123",
    ]
    assert source.last_filter_reason_counts == {
        "outside_configured_domain_scope": 1,
    }
    assert source.last_filter_examples[0]["host"] == "jobs.example.test"


def test_eight_attempt_allocator_uses_multiple_morocco_source_scopes(tmp_path):
    key = "phase-2-6-shared-key"
    budget = DailyBudget(tmp_path / "shared-budget.json", limit=17)

    class Source:
        def __init__(self):
            self.keys = [TavilyKey(key, budget, key_fingerprint(key))]

        @staticmethod
        def has_cached_result(_query, _location):
            return False

        @staticmethod
        def has_stale_cached_result(_query, _location):
            return False

    connectors = [
        (
            {
                "name": "web",
                "applies_to_countries": ["*"],
                "allocation_priority": 100,
            },
            Source(),
        ),
        (
            {
                "name": "official-ats",
                "official_only": True,
                "applies_to_countries": ["*"],
                "allocation_priority": 95,
            },
            Source(),
        ),
    ]
    connectors.extend(
        (
            {
                "name": f"morocco-{domain}",
                "domains": [domain],
                "applies_to_countries": ["Morocco"],
                "allocation_priority": priority,
            },
            Source(),
        )
        for domain, priority in (
            ("anapec.org", 90),
            ("emploi-public.ma", 90),
            ("rekrute.com", 80),
            ("emploi.ma", 80),
            ("amaljob.com", 80),
        )
    )
    plan = [
        {
            "query": f"role-{country}-{language}",
            "location": country,
            "country": country,
            "lang": language,
        }
        for country, language in (
            ("Morocco", "fr"),
            ("Morocco", "ar"),
            ("Morocco", "fr"),
            ("France", "fr"),
            ("Belgium", "fr"),
            ("Canada", "en"),
            ("Germany", "de"),
        )
    ]
    deferrals = [{} for _ in connectors]

    scheduled, deferred, _stale = _allocate_source_queries(
        connectors,
        plan,
        max_requests_per_source=0,
        rotation_offset=0,
        max_api_attempts=8,
        deferral_reasons=deferrals,
    )

    assert sum(map(len, scheduled)) == 8
    selected_market = [
        (connectors[index][0]["name"], combo)
        for index, combos in enumerate(scheduled[2:], start=2)
        for combo in combos
    ]
    assert len(selected_market) == 5
    assert {combo["country"] for _, combo in selected_market} == {"Morocco"}
    assert {
        "anapec.org",
        "emploi-public.ma",
        "rekrute.com",
        "emploi.ma",
        "amaljob.com",
    } == {
        connectors[index][0]["domains"][0]
        for index, combos in enumerate(scheduled)
        if combos and connectors[index][0].get("domains")
    }
    assert sum(deferred) == 21
    assert sum(
        reasons.get("run_attempt_cap", 0) for reasons in deferrals
    ) == sum(deferred)
    assert not any(
        reasons.get("aggregate_daily_budget", 0) for reasons in deferrals
    )


def test_query_trace_distinguishes_allocation_and_combination_deferrals():
    source = {"name": "broad", "applies_to_countries": ["*"]}
    plan = [
        {
            "query": f"civil engineering {index}",
            "country": "Morocco",
            "location": "Morocco",
            "lang": "fr",
            "intent": "role",
            "_plan_index": index,
        }
        for index in range(3)
    ]

    trace = _source_query_trace(
        source,
        plan,
        plan[:2],
        plan[:1],
        max_requests_per_source=3,
        allocation_reason="run_attempt_cap",
    )

    assert [entry["request_status"] for entry in trace] == [
        "pending",
        "deferred",
        "deferred",
    ]
    assert [entry["reason_code"] for entry in trace] == [
        "",
        "search_combination_cap",
        "run_attempt_cap",
    ]


def test_successful_provider_fallback_is_not_counted_as_query_deferral():
    assert _query_outcome_state(
        origin="api",
        status="ok",
        error_code="",
        deferred_reason="daily_request_budget_exhausted",
        provider_attempts=1,
    ) == ("attempted", "")
    assert _query_outcome_state(
        origin="none",
        status="degraded",
        error_code="run_api_attempt_budget_exhausted",
        deferred_reason="run_api_attempt_budget_exhausted",
        provider_attempts=0,
    ) == ("deferred", "run_api_attempt_budget_exhausted")
    assert _query_outcome_state(
        origin="api",
        status="degraded",
        error_code="provider_request_failed",
        deferred_reason="daily_request_budget_exhausted",
        provider_attempts=1,
    ) == ("failed", "provider_request_failed")


def test_normalization_rejection_has_a_stable_reason_code():
    from app.connectors.base import Opportunity
    from app.normalization import normalization_rejection_reason, normalize

    invalid = Opportunity(
        source="fixture",
        external_id="bad",
        title="Civil technician",
        url="javascript:alert(1)",
    )
    too_short = Opportunity(
        source="fixture",
        external_id="short",
        title="x",
        url="https://example.test/job/123",
    )

    assert normalization_rejection_reason(invalid) == "invalid_listing_url"
    assert normalization_rejection_reason(too_short) == "title_too_short"
    assert normalize(invalid) is None
    assert normalize(too_short) is None


def _cache_entry_key(source, query: str, location: str) -> str:
    from app.connectors.tavily_resilience import _cache_key

    return _cache_key(
        source._build_query(query, location),
        location,
        source.request_scope_id,
    )
