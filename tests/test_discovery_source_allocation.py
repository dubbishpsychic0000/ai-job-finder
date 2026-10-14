from __future__ import annotations

from app.config import AgentConfig, Preferences
from app.connectors.tavily_resilience import (
    DailyBudget,
    ResilientTavily,
    TavilyCache,
    TavilyKey,
    key_fingerprint,
)
from app.discovery.country_ranking import CountryScore
from app.workflows import adaptive_plan
from app.workflows.discovery import _allocate_source_queries
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


def _cache_entry_key(source, query: str, location: str) -> str:
    from app.connectors.tavily_resilience import _cache_key

    return _cache_key(
        source._build_query(query, location),
        location,
        source.request_scope_id,
    )
