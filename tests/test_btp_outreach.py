from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import app.workflows.btp_outreach as btp_outreach
from app import models
from app.connectors.tavily_resilience import DailyBudget, ResilientTavily, TavilyCache, TavilyKey
from app.discovery.website_researcher import EvidenceFact, ResearchResult
from app.email import provider
from app.memory import store
from app.memory.store import applications_due_for_followup
from app.models import utcnow
from app.workflows.action import pending_actions
from app.workflows.analysis import analyze_job
from app.workflows.btp_outreach import (
    MOROCCO_CITY_CENTERS,
    BtpCompany,
    BtpOutreachSafetyError,
    BtpOutreachState,
    OverpassBtpDiscovery,
    PublicBtpCompanySearch,
    PublicCompanyWebsiteFinder,
    WikidataBtpDiscovery,
    extract_spontaneous_instruction,
    haversine_km,
    load_btp_seed_csv,
    parse_overpass_companies,
    rank_nearest,
    run_btp_outreach,
)


def _osm_element(name, lat, lon, *, craft="construction", website="https://build.ma",
                 city="Rabat", region="Rabat-Salé-Kénitra"):
    return {
        "type": "node",
        "lat": lat,
        "lon": lon,
        "tags": {
            "name": name,
            "craft": craft,
            "website": website,
            "addr:city": city,
            "addr:region": region,
            "addr:street": "Must never be retained",
        },
    }


def _company(name="Build SARL", website="https://build.ma", distance=5, city="Rabat"):
    return BtpCompany(
        name=name,
        website=website,
        city=city,
        region="",
        latitude=33.6,
        longitude=-7.5,
        distance_km=distance,
    )


class _Discovery:
    def __init__(self, companies):
        self.companies = companies
        self.called = False

    def discover(self):
        self.called = True
        return self.companies


class _Researcher:
    def __init__(self, evidence_by_domain):
        self.evidence_by_domain = evidence_by_domain
        self.visited = []

    def research(self, url, *, company_id=None, official_domain="", discover_sitemaps=True):
        self.visited.append(official_domain)
        return ResearchResult(
            official_domain=official_domain,
            evidence=list(self.evidence_by_domain.get(official_domain, [])),
        )


class _NoWebsiteFinder:
    def find(self, company_name, location=""):
        return ""


class _EmptyCompanySearch:
    def discover(self):
        return []


def _evidence(domain, *, instruction=True, email="recrutement@build.ma", portal=""):
    url = f"https://{domain}/contact"
    result = []
    if instruction:
        text = "Envoyez votre candidature spontanée par e-mail à recrutement@build.ma."
        result.append(EvidenceFact(
            source_url=url,
            source_type="spontaneous",
            page_title="Contact",
            field_name="spontaneous_application",
            extracted_value=text,
            snippet=text,
            confidence=90,
            reason_code="explicit_spontaneous_instruction",
        ))
    if email:
        result.append(EvidenceFact(
            source_url=url,
            source_type="contact",
            page_title="Contact",
            field_name="general_email",
            extracted_value=email,
            snippet=f"Contact: {email}",
            domain_relationship="company_general",
            confidence=100,
            reason_code="employer_domain",
        ))
    if portal:
        result.append(EvidenceFact(
            source_url=url,
            source_type="careers",
            page_title="Contact",
            field_name="application_url",
            extracted_value=portal,
            snippet="Apply online",
        ))
    return result


def _run(db, config, settings, profile, companies, researcher, **kwargs):
    return run_btp_outreach(
        db,
        config,
        settings,
        profile,
        discovery=_Discovery(companies),
        wikidata_discovery=_Discovery([]),
        company_search=kwargs.pop("company_search", _EmptyCompanySearch()),
        researcher=researcher,
        website_finder=kwargs.pop("website_finder", _NoWebsiteFinder()),
        screening_state_path=kwargs.pop("screening_state_path", None),
        **kwargs,
    )


def test_overpass_parser_filters_and_extracts_only_city_region_and_coordinates():
    parsed = parse_overpass_companies({
        "elements": [
            _osm_element("Casablanca BTP", 33.5731, -7.5898, city="Casablanca"),
            _osm_element("Far BTP", 34.0, -6.8, website="https://far.ma"),
            _osm_element("Bakery", 34.0, -6.8, craft="bakery"),
            {
                "type": "node",
                "lat": 33.58,
                "lon": -7.6,
                "tags": {"name": "Casa BTP Travaux", "office": "company"},
            },
            {"type": "way", "center": {"lat": 34.1, "lon": -6.7},
             "tags": {"name": "Unknown company", "office": "accountant"}},
        ]
    })
    assert [item.name for item in parsed] == ["Casablanca BTP", "Casa BTP Travaux", "Far BTP"]
    assert parsed[0].city == "Casablanca"
    assert parsed[0].location == "Casablanca, Rabat-Salé-Kénitra, Morocco"
    assert "must never be retained" not in repr(parsed[0]).lower()
    assert parsed[0].distance_km == pytest.approx(0)
    assert parsed[2].distance_km == pytest.approx(
        haversine_km(34.0, -6.8)
    )


def test_overpass_parser_handles_way_centers_and_bounds_candidates():
    elements = [
        {"type": "way", "center": {"lat": 33.6, "lon": -7.5},
         "tags": {"name": f"Builder {index}", "industry": "construction"}}
        for index in range(4)
    ]
    assert len(parse_overpass_companies({"elements": elements}, limit=2)) == 2
    assert parse_overpass_companies({"elements": [
        {"type": "way", "tags": {"name": "No center", "craft": "construction"}}
    ]}) == []


def test_overpass_candidate_limit_keeps_nearest_after_sorting():
    elements = [
        _osm_element(f"Builder {index}", 34.0 + index / 1000, -6.8)
        for index in range(5)
    ]
    nearest = parse_overpass_companies({"elements": elements}, limit=2)
    assert len(nearest) == 2
    assert nearest[0].distance_km <= nearest[1].distance_km


def test_overpass_adapter_passes_public_query_to_injected_fetcher():
    calls = []

    def fetch(url, *, params):
        calls.append((url, params))
        return {"elements": [_osm_element("Build", 33.6, -7.5)]}

    candidates = OverpassBtpDiscovery(fetch_json=fetch).discover()
    assert candidates[0].name == "Build"
    assert calls[0][1]["data"].find('ISO3166-1"="MA"') >= 0
    assert "out center;" in calls[0][1]["data"]


def test_overpass_rejects_incomplete_timed_out_responses():
    discovery = OverpassBtpDiscovery(fetch_json=lambda *_args, **_kwargs: {
        "elements": [],
        "remark": 'runtime error: Query timed out in "query"',
    })

    with pytest.raises(RuntimeError, match="incomplete result"):
        discovery.discover()


def test_overpass_uses_query_versioned_stale_cache_on_refresh_failure(tmp_path):
    cache_path = tmp_path / "overpass.json"
    fresh_discovery = OverpassBtpDiscovery(
        fetch_json=lambda *_args, **_kwargs: {
            "elements": [_osm_element("Cached Build", 33.6, -7.5)],
        },
        cache_path=cache_path,
    )
    assert fresh_discovery.discover()[0].name == "Cached Build"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    cached["fetched_at"] = (
        datetime.now(timezone.utc) - timedelta(days=8)
    ).isoformat()
    cache_path.write_text(json.dumps(cached), encoding="utf-8")

    stale_discovery = OverpassBtpDiscovery(
        fetch_json=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("Overpass is unreachable")
        ),
        cache_path=cache_path,
    )
    assert [company.name for company in stale_discovery.discover()] == ["Cached Build"]
    assert any("using cached public listings" in error for error in stale_discovery.errors)


def test_wikidata_corrupt_cache_is_reported_and_refreshed(tmp_path):
    cache_path = tmp_path / "wikidata.json"
    cache_path.write_text("[]", encoding="utf-8")
    discovery = WikidataBtpDiscovery(
        fetch_json=lambda *_args, **_kwargs: {"results": {"bindings": []}},
        cache_path=cache_path,
    )

    assert discovery.discover() == []
    assert any("cache is unreadable" in error for error in discovery.errors)
    assert json.loads(cache_path.read_text(encoding="utf-8"))["companies"] == []


def test_btp_screening_state_retries_expired_negatives_but_never_redrafts(tmp_path):
    state = BtpOutreachState(tmp_path / "screening.json")
    candidate = _company()
    state.mark(candidate, "no_verified_website")
    assert state.is_suppressed(candidate)
    expired = datetime.now(timezone.utc) + timedelta(days=31)
    assert not state.is_suppressed(candidate, now=expired)
    assert state.is_expired_negative(candidate, now=expired)

    state.mark(candidate, "drafted_for_review")
    assert state.is_suppressed(candidate, now=expired)
    assert not state.is_expired_negative(candidate, now=expired)
    assert json.loads((tmp_path / "screening.json").read_text(encoding="utf-8"))[
        "schema_version"
    ] == 1


def test_btp_screening_state_rejects_corrupt_ledger(tmp_path):
    path = tmp_path / "screening.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(BtpOutreachSafetyError, match="unreadable"):
        BtpOutreachState(path)


def test_btp_run_persists_screening_and_retries_after_negative_ttl(
    db, config, settings, profile, tmp_path
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    candidate = _company("No Website", website="")
    state_path = tmp_path / "screening.json"
    first = _run(
        db, config, settings, profile, [candidate], _Researcher({}),
        screening_state_path=state_path,
    )
    second = _run(
        db, config, settings, profile, [candidate], _Researcher({}),
        screening_state_path=state_path,
    )
    assert first.no_website == 1
    assert second.existing == 1
    assert second.processed == 0

    state = json.loads(state_path.read_text(encoding="utf-8"))
    record = next(iter(state["companies"].values()))
    record["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(days=31)
    ).isoformat()
    state_path.write_text(json.dumps(state), encoding="utf-8")
    retried = _run(
        db, config, settings, profile, [candidate], _Researcher({}),
        screening_state_path=state_path,
    )
    assert retried.no_website == 1
    assert retried.processed == 1


def test_nearest_first_and_per_run_cap():
    companies = [_company("Furthest", distance=50), _company("Nearest", distance=1),
                 _company("Middle", distance=10)]
    assert [item.name for item in rank_nearest(companies, 2)] == ["Nearest", "Middle"]
    assert len(rank_nearest(companies, 500)) == 3


def test_public_website_search_requires_company_name_and_skips_directories():
    finder = PublicCompanyWebsiteFinder(search=lambda query: [
        {"url": "https://telecontact.ma/build", "title": "Build SARL BTP"},
        {"url": "https://build.ma", "title": "Build SARL - site officiel"},
    ])
    assert finder.find("Build SARL", "Casablanca") == "https://build.ma/"

    unrelated = PublicCompanyWebsiteFinder(search=lambda query: [
        {"url": "https://other.ma", "title": "Other Construction Maroc"},
    ])
    assert unrelated.find("Build SARL", "Casablanca") == ""


def test_public_btp_search_queries_cities_from_nearest_outward():
    def search(query):
        if query.endswith("Casablanca"):
            return [{
                "url": "https://casa-btp.ma",
                "title": "Casa BTP — travaux publics",
                "snippet": "Entreprise de construction au Maroc",
            }]
        if query.endswith("Rabat"):
            return [{
                "url": "https://rabat-btp.ma",
                "title": "Rabat BTP",
                "snippet": "Travaux publics et génie civil",
            }]
        return []

    results = PublicBtpCompanySearch(
        search=search,
        max_city_searches=len(MOROCCO_CITY_CENTERS),
    ).discover()
    assert [company.city for company in results] == ["Casablanca", "Rabat"]
    assert results[0].distance_km < results[1].distance_km


def test_public_btp_search_uses_directory_only_as_a_company_name_seed():
    def search(query):
        if query.endswith("Casablanca"):
            return [{
                "url": "https://telecontact.ma/annuaire/atlas",
                "title": "Société Atlas BTP Casablanca - Annuaire Maroc",
                "snippet": "Entreprise de construction et travaux publics",
            }]
        return []

    results = PublicBtpCompanySearch(search=search).discover()
    assert len(results) == 1
    assert results[0].name == "Atlas"
    assert results[0].website == ""
    assert results[0].city == "Casablanca"


def test_public_btp_search_rotates_city_batches_without_repeating_nearby_queries(tmp_path):
    calls = []

    def search(query):
        city = next(
            name for name in MOROCCO_CITY_CENTERS if query.endswith(name)
        )
        calls.append(city)
        return []

    state_path = tmp_path / "btp_search_state.json"
    first = PublicBtpCompanySearch(
        search=search, state_path=state_path, max_city_searches=2,
    )
    first.discover()
    second = PublicBtpCompanySearch(
        search=search, state_path=state_path, max_city_searches=2,
    )
    second.discover()

    assert calls == ["Casablanca", "Mohammedia", "Settat", "Rabat"]


def test_public_search_reports_duckduckgo_human_verification(monkeypatch):
    class Response:
        status_code = 202
        text = "Unfortunately, bots use DuckDuckGo too. Please complete the following challenge"

    monkeypatch.setattr(btp_outreach.requests, "post", lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match="human-verification challenge"):
        PublicCompanyWebsiteFinder._search("construction companies Casablanca")


def test_company_seed_csv_import_requires_public_provenance_and_city_or_coordinates(tmp_path):
    path = tmp_path / "btp_company_seeds.csv"
    path.write_text(
        "name,city,region,latitude,longitude,website,source_url\n"
        "Atlas BTP,Casablanca,Casablanca-Settat,,,,https://fnbtp.ma/companies/atlas\n"
        "Nord Routes,,Tanger-Tétouan-Al Hoceïma,35.76,-5.83,https://nordroutes.ma,"
        "https://public.example.ma/nord-routes\n",
        encoding="utf-8",
    )

    companies = load_btp_seed_csv(path)

    assert [company.name for company in companies] == ["Atlas BTP", "Nord Routes"]
    assert companies[0].source == "curated_seed_csv"
    assert companies[0].website == ""
    assert companies[0].city == "Casablanca"
    assert companies[1].city == "Tangier"
    assert companies[1].website == "https://nordroutes.ma/"


def test_company_seed_csv_rejects_email_data_and_malformed_locations(tmp_path):
    path = tmp_path / "btp_company_seeds.csv"
    path.write_text(
        "name,city,email,source_url\n"
        "Atlas BTP,Casablanca,jobs@atlas.ma,https://fnbtp.ma/companies/atlas\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="headers"):
        load_btp_seed_csv(path)

    path.write_text(
        "name,city,source_url\n"
        "Atlas BTP,Unknown City,https://fnbtp.ma/companies/atlas\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="needs a known city or coordinates"):
        load_btp_seed_csv(path)


def test_btp_workflow_includes_audited_csv_candidates(db, config, settings, profile, tmp_path):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    path = tmp_path / "btp_company_seeds.csv"
    path.write_text(
        "name,city,source_url\n"
        "Atlas BTP,Casablanca,https://fnbtp.ma/companies/atlas\n",
        encoding="utf-8",
    )

    report = _run(
        db,
        config,
        settings,
        profile,
        [],
        _Researcher({}),
        seed_csv_path=path,
    )

    assert report.seed_candidates == 1
    assert report.candidates == 1
    assert report.no_website == 1
    company = db.query(models.Company).one()
    assert company.name == "Atlas BTP"
    discovery = db.query(models.Discovery).one()
    assert discovery.source == "curated_seed_csv"
    assert discovery.evidence["discovery_source_url"] == "https://fnbtp.ma/companies/atlas"


def test_wikidata_btp_parser_filters_unrelated_industries_and_requires_location():
    def binding(value):
        return {"value": value}

    rows = [
        {
            "entity": binding("https://www.wikidata.org/entity/Q1"),
            "entityLabel": binding("Ciments du Maroc"),
            "industryLabel": binding("construction materials industry"),
            "website": binding("https://cimentsdumaroc.ma"),
            "cityLabel": binding("Casablanca"),
            "coordinates": binding("Point(-7.6 33.57)"),
        },
        {
            "entity": binding("https://www.wikidata.org/entity/Q2"),
            "entityLabel": binding("Vehicle Works"),
            "industryLabel": binding("vehicle construction"),
            "cityLabel": binding("Casablanca"),
            "coordinates": binding("Point(-7.6 33.57)"),
        },
        {
            "entity": binding("https://www.wikidata.org/entity/Q4"),
            "entityLabel": binding("X Chem Maroc"),
            "industryLabel": binding("construction materials industry"),
            "website": binding("https://akfix.co.ma"),
            "coordinates": binding("Point(-6.816667 34.05)"),
        },
        {
            "entity": binding("https://www.wikidata.org/entity/Q3"),
            "entityLabel": binding("Unlocated BTP"),
            "industryLabel": binding("construction"),
        },
    ]

    companies = WikidataBtpDiscovery._parse({"results": {"bindings": rows}})

    assert [company.name for company in companies] == ["Ciments du Maroc", "X Chem Maroc"]
    assert companies[0].source == "wikidata"
    assert companies[0].latitude == pytest.approx(33.57)
    assert companies[1].website == ""
    assert companies[1].city == "Rabat"


def test_wikidata_discovery_caches_monthly_results(tmp_path):
    calls = []
    payload = {"results": {"bindings": [{
        "entity": {"value": "https://www.wikidata.org/entity/Q1"},
        "entityLabel": {"value": "Ciments du Maroc"},
        "industryLabel": {"value": "construction materials industry"},
        "cityLabel": {"value": "Casablanca"},
        "coordinates": {"value": "Point(-7.6 33.57)"},
    }]}}

    def fetch_json(url, *, params):
        calls.append(url)
        assert params["format"] == "json"
        return payload

    discovery = WikidataBtpDiscovery(
        fetch_json=fetch_json,
        cache_path=tmp_path / "wikidata-cache.json",
    )
    assert len(discovery.discover()) == 1
    assert len(discovery.discover()) == 1
    assert len(calls) == 1


def test_wikidata_cache_invalidates_when_query_changes(tmp_path):
    path = tmp_path / "wikidata-cache.json"
    path.write_text(json.dumps({
        "fetched_at": utcnow().isoformat(),
        "query_hash": hashlib.sha256(b"older query").hexdigest(),
        "companies": [],
    }), encoding="utf-8")
    calls = []
    discovery = WikidataBtpDiscovery(
        fetch_json=lambda *_args, **_kwargs: (
            calls.append("fetch") or {"results": {"bindings": []}}
        ),
        cache_path=path,
    )

    assert discovery.discover() == []
    assert calls == ["fetch"]


def test_configured_public_search_uses_tavily_budgeted_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEYS", "key-one,key-two")
    monkeypatch.setattr(btp_outreach, "ROOT_DIR", tmp_path)

    class FakeTavily:
        def __init__(self, **kwargs):
            self.options = kwargs

    monkeypatch.setattr(btp_outreach, "ResilientTavily", FakeTavily)
    search = btp_outreach._configured_public_search()
    assert isinstance(search, btp_outreach._TavilyCompanySearch)
    assert len(search.connector.options["keys"]) == 2
    assert search.connector.options["query_suffix"] == ""
    assert search.max_api_calls == 3


def test_tavily_company_search_maps_public_results():
    class FakeBudget:
        def remaining(self):
            return 10

    class FakeConnector:
        last_status = "ok"

        def __init__(self):
            self.keys = [type("Key", (), {"budget": FakeBudget()})()]

        def has_cached_result(self, query):
            return False

        async def search(self, query):
            assert query == "construction companies Casablanca"
            return [type("Result", (), {
                "url": "https://builder.ma",
                "title": "Builder Morocco",
                "description": "Construction company",
            })()]

    result = btp_outreach._TavilyCompanySearch(FakeConnector())(
        "construction companies Casablanca"
    )
    assert result == [{
        "url": "https://builder.ma",
        "title": "Builder Morocco",
        "snippet": "Construction company",
    }]


def test_tavily_company_search_caps_uncached_requests_but_allows_cache_hits():
    class FakeBudget:
        def remaining(self):
            return 10

    class FakeConnector:
        last_status = "ok"

        def __init__(self):
            self.keys = [type("Key", (), {"budget": FakeBudget()})()]
            self.cached_queries = set()
            self.calls = []

        def has_cached_result(self, query):
            return query in self.cached_queries

        async def search(self, query):
            if query not in self.cached_queries:
                self.calls.append(query)
                self.cached_queries.add(query)
            return []

    connector = FakeConnector()
    search = btp_outreach._TavilyCompanySearch(connector, max_api_calls=1)
    search("cached query")
    with pytest.raises(btp_outreach.PublicSearchBudgetError, match="per-run"):
        search("new query")
    search("cached query")
    assert connector.calls == ["cached query"]


def test_resilient_tavily_can_search_without_job_suffix(monkeypatch, tmp_path):
    request = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{
                "url": "https://builder.ma",
                "title": "Builder Morocco",
                "content": "Construction company",
            }]}

    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda _url, *, json, timeout: (request.update(json) or Response()),
    )
    budget = DailyBudget(tmp_path / "budget.json", limit=5)
    connector = ResilientTavily(
        keys=[TavilyKey("test-key", budget, "test-key")],
        cache=TavilyCache(tmp_path / "cache.json"),
        query_suffix="",
    )

    results = asyncio.run(connector.search("construction companies Casablanca"))

    assert request["query"] == "construction companies Casablanca"
    assert len(results) == 1
    assert budget.remaining() == 4


def test_resilient_tavily_caps_api_key_attempts(monkeypatch, tmp_path):
    calls = []

    class Response:
        status_code = 429

    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda *_args, **_kwargs: (calls.append("request") or Response()),
    )
    first_budget = DailyBudget(tmp_path / "first.json", limit=5)
    second_budget = DailyBudget(tmp_path / "second.json", limit=5)
    connector = ResilientTavily(
        keys=[
            TavilyKey("first", first_budget, "first"),
            TavilyKey("second", second_budget, "second"),
        ],
        cache=TavilyCache(tmp_path / "cache.json"),
        query_suffix="",
        max_key_attempts=1,
    )

    assert asyncio.run(connector.search("construction company Casablanca")) == []
    assert asyncio.run(connector.search("construction company Rabat")) == []
    assert calls == ["request", "request"]
    assert first_budget.remaining() == 0
    assert second_budget.remaining() == 0


def test_explicit_instruction_requires_submission_direction():
    assert extract_spontaneous_instruction(
        "Envoyez votre candidature spontanée à recrutement@entreprise.ma."
    )
    assert not extract_spontaneous_instruction(
        "Nous acceptons les candidatures spontanées."
    )
    assert not extract_spontaneous_instruction("Candidature spontanée.")


def test_only_explicit_instructions_and_official_domain_contact_create_french_draft(
    db, config, settings, profile, monkeypatch
):
    companies = [
        _company("Mention Only", "https://mention.ma", distance=1),
        _company("Free Email", "https://free.ma", distance=2),
        _company("Eligible", "https://eligible.ma", distance=3),
    ]
    evidence = {
        "mention.ma": _evidence("mention.ma", instruction=False, email="jobs@mention.ma"),
        "free.ma": _evidence("free.ma", email="jobs@gmail.com"),
        "eligible.ma": _evidence("eligible.ma", email="jobs@eligible.ma"),
    }
    researcher = _Researcher(evidence)
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    calls = []

    def create_draft(settings_, *, to, subject, body, attachments):
        calls.append((to, subject, body, attachments))
        return True, "draft-review-only", ""

    monkeypatch.setattr(provider, "create_draft", create_draft)
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    report = _run(db, config, settings, profile, companies, researcher)

    assert report.drafts == 1
    assert report.no_spontaneous_instructions == 1
    assert report.no_qualifying_email == 1
    assert calls[0][0] == "jobs@eligible.ma"
    assert "Candidature spontanée" in calls[0][1]
    assert "candidature spontanée" in calls[0][2].lower()
    assert calls[0][3]
    synthetic = db.query(models.Job).one()
    assert synthetic.opportunity_type == "SPONTANEOUS_APPLICATION"
    assert synthetic.source_type == "SPONTANEOUS_APPLICATION"
    assert synthetic.status == "outreach_only"
    application = db.query(models.Application).one()
    assert application.follow_up_at is None


def test_osm_company_without_website_uses_public_site_lookup_before_research(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    researcher = _Researcher({
        "build.ma": _evidence("build.ma", email="jobs@build.ma"),
    })
    finder_calls = []

    class Finder:
        def find(self, company_name, location=""):
            finder_calls.append((company_name, location))
            return "https://build.ma"

    monkeypatch.setattr(provider, "create_draft", lambda *_a, **_kw: (True, "draft", ""))
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    report = _run(
        db, config, settings, profile, [_company(website="")], researcher,
        website_finder=Finder(),
    )
    assert finder_calls == [("Build SARL", "Rabat, Morocco")]
    assert report.drafts == 1
    assert researcher.visited == ["build.ma"]


@pytest.mark.parametrize(
    ("email", "portal", "expected"),
    [
        ("", "", "no_qualifying_email"),
        ("", "https://portal.vendor.example/apply", "portal_or_form"),
        ("jobs@gmail.com", "", "no_qualifying_email"),
        ("jobs@other.ma", "", "no_qualifying_email"),
    ],
)
def test_no_email_is_created_for_portal_or_unsafe_contact(
    db, config, settings, profile, monkeypatch, email, portal, expected
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    company = _company("No Contact", "https://nocontact.ma")
    researcher = _Researcher({
        "nocontact.ma": _evidence("nocontact.ma", email=email, portal=portal),
    })

    def forbidden(*args, **kwargs):
        raise AssertionError("Gmail must not be touched without an eligible recipient")

    monkeypatch.setattr(provider, "create_draft", forbidden)
    report = _run(db, config, settings, profile, [company], researcher)
    assert getattr(report, expected) == 1
    assert db.query(models.Email).count() == 0
    assert db.query(models.Application).count() == 0


@pytest.mark.parametrize(
    ("mode", "enabled", "provider_name"),
    [("live", True, "gmail"), ("draft", False, "gmail"), ("draft", True, "smtp")],
)
def test_refuses_non_draft_disabled_or_non_gmail_before_discovery(
    db, config, settings, profile, mode, enabled, provider_name
):
    settings = settings.model_copy(update={
        "email_mode": mode, "enable_email": enabled, "email_provider": provider_name,
    })
    discovery = _Discovery([])
    with pytest.raises(BtpOutreachSafetyError):
        run_btp_outreach(db, config, settings, profile, discovery=discovery)
    assert not discovery.called


def test_per_run_processing_uses_nearest_candidates_and_caps_count(
    db, config, settings, profile
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    companies = [
        _company("Outside", website="", distance=20),
        _company("Near", website="", distance=2),
        _company("Middle", website="", distance=10),
    ]
    report = _run(db, config, settings, profile, companies, _Researcher({}), max_companies=2)
    assert [item["name"] for item in report.companies] == ["Near", "Middle"]
    assert report.processed == 2


def test_next_run_advances_past_companies_already_screened(
    db, config, settings, profile
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    companies = [
        _company("Nearest", website="", distance=1),
        _company("Next", website="", distance=5),
        _company("Farther", website="", distance=10),
    ]
    researcher = _Researcher({})
    first = _run(db, config, settings, profile, companies, researcher, max_companies=1)
    second = _run(db, config, settings, profile, companies, researcher, max_companies=1)
    assert first.companies[0]["name"] == "Nearest"
    assert second.companies[0]["name"] == "Next"


def test_repeat_run_keeps_unique_synthetic_company_tracking(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    researcher = _Researcher({
        "build.ma": _evidence("build.ma", email="jobs@build.ma"),
    })
    calls = []
    monkeypatch.setattr(
        provider, "create_draft",
        lambda *_args, **_kwargs: (calls.append("draft") or True, "draft-id", ""),
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    companies = [_company()]
    first = _run(db, config, settings, profile, companies, researcher)
    second = _run(db, config, settings, profile, companies, researcher)
    assert first.drafts == 1
    assert second.existing == 1
    assert calls == ["draft"]
    assert db.query(models.Job).filter(
        models.Job.opportunity_type == "SPONTANEOUS_APPLICATION"
    ).count() == 1
    assert db.query(models.Application).count() == 1


@pytest.mark.parametrize("safety_failure", ["missing_cv", "daily_limit"])
def test_existing_safety_gate_blocks_missing_cv_and_daily_cap(
    db, config, settings, profile, monkeypatch, safety_failure
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    if safety_failure == "missing_cv":
        monkeypatch.setattr("app.email.service.resolve_attachment", lambda *_args: "")
    else:
        config.rules["max_daily_applications"] = 0
    researcher = _Researcher({"build.ma": _evidence("build.ma", email="jobs@build.ma")})

    def forbidden(*args, **kwargs):
        raise AssertionError("Gmail must not be called when an existing safety check blocks")

    monkeypatch.setattr(provider, "create_draft", forbidden)
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    report = _run(db, config, settings, profile, [_company()], researcher)
    if safety_failure == "daily_limit":
        assert report.daily_limit_reached
        assert report.candidates == 1
        assert report.processed == 0
        assert db.query(models.Application).count() == 0
        assert db.query(models.Email).count() == 0
        return
    assert report.blocked == 1
    assert db.query(models.Application).one().status == "blocked"
    assert db.query(models.Email).count() <= 1


def test_spontaneous_records_are_excluded_from_generic_job_actions(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    researcher = _Researcher({"build.ma": _evidence("build.ma", email="jobs@build.ma")})
    monkeypatch.setattr(provider, "create_draft", lambda *_a, **_kw: (True, "draft", ""))
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    _run(db, config, settings, profile, [_company()], researcher)
    job = db.query(models.Job).one()
    job.status = "new"
    assert asyncio.run(analyze_job(
        db, job, None, None, None, None, profile, config, ["Morocco"],
    )) is None
    assert job.status == "outreach_only"
    job.status = "analyzed"
    store.add_decision(db, job.id, "APPLY", 99, {}, "test", [])
    assert pending_actions(db) == []
    application = db.query(models.Application).one()
    application.status = "sent"
    application.follow_up_at = utcnow() - timedelta(days=1)
    assert applications_due_for_followup(db) == []
