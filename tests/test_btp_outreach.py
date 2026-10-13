from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.cli as cli
import app.workflows.btp_outreach as btp_outreach
from app import models
from app.connectors.tavily_resilience import DailyBudget, ResilientTavily, TavilyCache, TavilyKey
from app.discovery.website_researcher import (
    EvidenceFact,
    ResearchResult,
    WebsiteResearcher,
    extract_application_instructions,
)
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
    _merge_candidates,
    _release_retryable_gmail_auth_failure,
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


def test_cmd_btp_outreach_allows_only_authorized_live_mode(monkeypatch):
    settings = SimpleNamespace(
        email_mode="live",
        enable_email=True,
        email_provider="gmail",
        authorize_live_spontaneous=True,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "get_config", lambda: SimpleNamespace(discovery={}))
    monkeypatch.setattr(cli, "get_profile", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "_print_json", lambda *_args, **_kwargs: None)

    class _SessionCtx:
        def __enter__(self):
            return SimpleNamespace()
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(cli, "session_scope", lambda: _SessionCtx())

    sent = {}

    def fake_run_btp(session, config, settings_, profile, **kwargs):
        sent["called"] = True
        return SimpleNamespace(as_run_report=lambda: {"status": "ok"})

    monkeypatch.setattr("app.workflows.btp_outreach.run_btp_outreach", fake_run_btp)

    cli.cmd_btp_outreach(SimpleNamespace(origin_city="Casablanca", max_companies=1))
    assert sent["called"] is True

    bad_settings = SimpleNamespace(
        email_mode="live",
        enable_email=True,
        email_provider="gmail",
        authorize_live_spontaneous=False,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: bad_settings)
    with pytest.raises(SystemExit, match="AUTHORIZE_LIVE_SPONTANEOUS"):
        cli.cmd_btp_outreach(SimpleNamespace(origin_city="Casablanca", max_companies=1))


def test_cmd_targeted_preflight_rolls_back_and_uses_no_state_cache(monkeypatch, tmp_path):
    restored_db = tmp_path / "agent.db"
    restored_db.touch()
    settings = SimpleNamespace(
        email_mode="dry_run",
        enable_email=False,
        email_provider="gmail",
        authorize_live_spontaneous=False,
        sqlite_path=restored_db,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli, "init_db",
        lambda: pytest.fail("targeted preflight must not initialize or migrate the DB"),
    )
    monkeypatch.setattr(cli, "get_config", lambda: SimpleNamespace(discovery={}))
    monkeypatch.setattr(cli, "get_profile", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "_print_json", lambda *_args, **_kwargs: None)

    session = SimpleNamespace(rollback_calls=0)
    session.rollback = lambda: setattr(session, "rollback_calls", session.rollback_calls + 1)

    class _SessionCtx:
        def __enter__(self):
            return session

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(cli, "session_scope", lambda: _SessionCtx())
    captured = {}

    def fake_run_btp(_session, _config, _settings, _profile, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(as_run_report=lambda: {"workflow": "btp_outreach"})

    monkeypatch.setattr("app.workflows.btp_outreach.run_btp_outreach", fake_run_btp)

    cli.cmd_btp_outreach(SimpleNamespace(
        origin_city="Casablanca",
        max_companies=1,
        target_employer="megec",
        preflight_only=True,
    ))

    assert captured["target_employer"] == "megec"
    assert captured["preflight_only"] is True
    assert captured["researcher"].state_path is None
    assert captured["researcher"].allow_safe_same_site_redirects is True
    assert captured["researcher"].allow_redirects is False
    assert session.rollback_calls == 1


def test_cmd_targeted_preflight_refuses_missing_database(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        email_mode="dry_run",
        enable_email=False,
        email_provider="gmail",
        authorize_live_spontaneous=False,
        sqlite_path=tmp_path / "missing-agent.db",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli, "init_db",
        lambda: pytest.fail("missing preflight DB must not be initialized"),
    )

    with pytest.raises(SystemExit, match="existing restored database"):
        cli.cmd_btp_outreach(SimpleNamespace(
            origin_city="Casablanca",
            max_companies=1,
            target_employer="megec",
            preflight_only=True,
        ))


def _evidence(domain, *, instruction=True, email="recrutement@build.ma", portal=""):
    url = f"https://{domain}/contact"
    result = []
    if instruction and email:
        text = f"Envoyez votre candidature spontanée par e-mail à {email}."
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
        recruitment_local_terms = (
            "recruit", "recrut", "career", "emploi", "talent", "hr", "rh",
            "hiring", "jobs",
        )
        is_recruitment = any(
            term in email.partition("@")[0].lower()
            for term in recruitment_local_terms
        )
        result.append(EvidenceFact(
            source_url=url,
            source_type="recruitment" if is_recruitment else "contact",
            page_title="Contact",
            field_name="recruitment_email" if is_recruitment else "general_email",
            extracted_value=email,
            snippet=f"Contact: {email}",
            domain_relationship=(
                "company_recruitment" if is_recruitment else "company_general"
            ),
            confidence=100,
            reason_code=(
                "employer_domain_recruitment" if is_recruitment else "employer_domain"
            ),
        ))
    if portal:
        text = "Soumettez votre CV au moyen de ce formulaire de candidature."
        result.append(EvidenceFact(
            source_url=url,
            source_type="careers",
            page_title="Contact",
            field_name="application_instructions",
            extracted_value=text,
            snippet=text,
            confidence=90,
            reason_code="explicit_application_submission_instruction",
        ))
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
        discovery=kwargs.pop("discovery", _Discovery(companies)),
        wikidata_discovery=_Discovery([]),
        company_search=kwargs.pop("company_search", _EmptyCompanySearch()),
        researcher=researcher,
        website_finder=kwargs.pop("website_finder", _NoWebsiteFinder()),
        screening_state_path=kwargs.pop("screening_state_path", None),
        max_companies=kwargs.pop("max_companies", 20),
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


def test_candidate_merge_uses_domain_then_unique_name_fallback():
    known_domain = _company("Atlas Travaux", website="https://atlas.ma", distance=10)
    known_domain.official_domain = "atlas.ma"
    known_domain.source = "wikidata"
    no_website = _company("Atlas Travaux", website="", distance=5)
    no_website.source = "openstreetmap"

    merged = _merge_candidates([no_website], [known_domain])

    assert len(merged) == 1
    assert merged[0].website == "https://atlas.ma/"
    assert merged[0].official_domain == "atlas.ma"
    assert merged[0].source == "wikidata"


def test_candidate_merge_does_not_guess_between_conflicting_domains():
    candidates = [
        _company("Atlas", website="https://atlas-a.ma", distance=1),
        _company("Atlas", website="https://atlas-b.ma", distance=2),
        _company("Atlas", website="", distance=3),
    ]

    merged = _merge_candidates(candidates)

    assert len(merged) == 3


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


def test_old_negative_screening_is_retried_after_eligibility_policy_change(tmp_path):
    candidate = _company("Updated policy", "https://updated.ma")
    path = tmp_path / "screening.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "companies": {
            BtpOutreachState.key(candidate): {
                "name": candidate.name,
                "city": candidate.city,
                "source": "company_pool",
                "status": "no_explicit_instructions",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    }), encoding="utf-8")

    state = BtpOutreachState(path)
    assert not state.is_suppressed(candidate)

    state.mark(candidate, "drafted_for_review")
    assert state.is_suppressed(candidate)


def test_previously_screened_negative_can_be_requalified_by_official_recruitment_page(
    db, config, settings, profile, monkeypatch, tmp_path
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    candidate = _company("Previously screened", "https://previously-screened.ma")
    screening_path = tmp_path / "screening.json"
    screening_path.write_text(json.dumps({
        "schema_version": 1,
        "companies": {
            BtpOutreachState.key(candidate): {
                "name": candidate.name,
                "city": candidate.city,
                "source": "wikidata",
                "status": "no_explicit_instructions",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    }), encoding="utf-8")
    recruitment = EvidenceFact(
        source_url="https://previously-screened.ma/careers",
        source_type="recruitment",
        page_title="Careers",
        field_name="recruitment_email",
        extracted_value="recrutement@previously-screened.ma",
        snippet="Recruitment contact",
        confidence=100,
        reason_code="employer_domain",
    )
    address = recruitment.extracted_value
    instruction = EvidenceFact(
        source_url=recruitment.source_url,
        source_type="recruitment",
        page_title="Careers",
        field_name="application_instructions",
        extracted_value=f"Envoyez votre CV de candidature à {address}.",
        snippet=f"Envoyez votre CV de candidature à {address}.",
    )
    researcher = _Researcher({
        "previously-screened.ma": [instruction, recruitment],
    })
    monkeypatch.setattr(
        provider, "create_draft", lambda *_args, **_kwargs: (True, "real-draft", "")
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)

    report = _run(
        db, config, settings, profile, [candidate], researcher,
        screening_state_path=screening_path,
    )

    assert report.drafts == 1
    assert report.as_run_report()["action"]["btp"]["production_smoke_outcome"] == (
        "gmail_draft_confirmed"
    )


def test_btp_screening_state_rejects_corrupt_ledger(tmp_path):
    path = tmp_path / "screening.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(BtpOutreachSafetyError, match="unreadable"):
        BtpOutreachState(path)


def test_nonpersistent_btp_screening_state_keeps_suppression_without_writing(tmp_path):
    path = tmp_path / "screening.json"
    state = BtpOutreachState(path, persist=False)
    candidate = _company("MEGEC", "https://www.megec.com/candidature-spontanee/")

    state.mark(candidate, "in_progress")

    assert state.is_suppressed(candidate)
    assert path.exists() is False


@pytest.mark.parametrize(
    ("error", "expected_retry"),
    [
        ("invalid_grant: Bad Request", True),
        (provider.GMAIL_REAUTH_REQUIRED, True),
        ("Gmail draft creation timed out", False),
    ],
)
def test_btp_releases_only_known_pre_api_gmail_auth_failures(
    db, error, expected_retry,
):
    company = store.get_or_create_company(
        db, "Senan Holding", "https://senanholding.com", "Morocco",
        official_domain="senanholding.com",
    )
    job = models.Job(
        source="btp_spontaneous",
        external_id="senan-retry",
        dedup_key="senan-retry",
        title="Candidature spontanée",
        company_id=company.id,
        opportunity_type="SPONTANEOUS_APPLICATION",
    )
    db.add(job)
    db.flush()
    application = models.Application(
        job_id=job.id, status="deferred", action="APPLY",
        contact_email="careers@senanholding.com",
    )
    db.add(application)
    db.flush()
    db.add(models.Email(
        application_id=application.id,
        to_addr=application.contact_email,
        subject="Application",
        body="",
        status="failed",
        mode="draft",
        error=error,
    ))
    db.flush()

    released = _release_retryable_gmail_auth_failure(
        db, _company("Senan Holding", "https://senanholding.com"),
    )

    assert released is expected_retry
    assert application.status == ("failed" if expected_retry else "deferred")


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


def test_btp_website_gate_rejects_profiles_documents_and_registries_but_keeps_employer_careers():
    assert not btp_outreach._eligible_website(
        "https://www.kerix.ma/annuaire/atlas"
    )
    assert not btp_outreach._eligible_website(
        "https://registre.gov.ma/company/atlas"
    )
    assert not btp_outreach._eligible_website(
        "https://files.atlas.ma/reports/company.pdf"
    )
    assert not btp_outreach._eligible_website(
        "https://www.maroc1000.net/Secteur-d'activit%C3%A9_BATIMENT"
    )
    assert btp_outreach._eligible_website(
        "https://careers.atlas.ma/jobs"
    ) == "https://careers.atlas.ma/jobs"

    finder = PublicCompanyWebsiteFinder(search=lambda query: [
        {"url": "https://registre.gov.ma/company/atlas", "title": "Atlas Construction"},
        {"url": "https://docs.google.com/document/d/atlas", "title": "Atlas company"},
        {"url": "https://careers.atlas.ma/jobs", "title": "Atlas Careers"},
    ])
    assert finder.find("Atlas", "Casablanca") == "https://careers.atlas.ma/jobs"


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


def test_public_btp_search_extracts_entity_from_maroc1000_but_not_publisher():
    def search(query):
        if not query.endswith("Casablanca"):
            return []
        return [
            {
                "url": "https://www.maroc1000.net/Secteur-d'activit%C3%A9_BATIMENT",
                "title": "Atlas BTP - Maroc1000",
                "snippet": "Annuaire d'entreprises du secteur BTP au Maroc",
            },
            {
                "url": "https://www.maroc1000.net/Secteur-d'activit%C3%A9_BATIMENT",
                "title": "Maroc1000 - Secteur d'activité BTP",
                "snippet": "Répertoire des entreprises de construction",
            },
        ]

    results = PublicBtpCompanySearch(search=search).discover()

    assert len(results) == 1
    assert results[0].name == "Atlas"
    assert not results[0].website
    assert results[0].source_url == (
        "https://www.maroc1000.net/Secteur-d'activit%C3%A9_BATIMENT"
    )


def test_directory_result_names_discard_publishers_and_profile_department_labels():
    assert btp_outreach._directory_company_name(
        "1000 Premières par secteur - Maroc1000", "Casablanca",
    ) == ""
    assert btp_outreach._directory_company_name(
        "Professionnels du BTP - MarocProGuide", "Agadir",
    ) == ""
    assert btp_outreach._directory_company_name(
        "Bouygues Information - RocketReach", "Casablanca",
    ) == "Bouygues"
    assert btp_outreach._directory_company_name(
        "Guicopres Finance Department - RocketReach", "Casablanca",
    ) == "Guicopres"


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


def test_curated_senan_holding_seed_uses_official_careers_provenance():
    seed_path = btp_outreach.ROOT_DIR / "candidate" / "btp_company_seeds.csv"

    companies = load_btp_seed_csv(seed_path)

    assert len(companies) == 1
    assert companies[0].name == "Senan Holding"
    assert companies[0].city == "Rabat"
    assert companies[0].website == "https://senanholding.com/careers"
    assert companies[0].source_url == "https://senanholding.com/careers"
    assert companies[0].source == "curated_seed_csv"


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
    assert isinstance(search, btp_outreach._BoundedPublicSearchRouter)
    assert isinstance(search.primary, btp_outreach._TavilyCompanySearch)
    assert len(search.primary.connector.options["keys"]) == 2
    assert search.primary.connector.options["query_suffix"] == ""
    assert search.primary.max_api_calls == 3


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


def test_bounded_public_search_falls_back_when_tavily_budget_is_exhausted(monkeypatch):
    calls = []
    monkeypatch.setattr(btp_outreach.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(btp_outreach.time, "sleep", lambda _seconds: None)
    router = btp_outreach._BoundedPublicSearchRouter(
        primary=lambda _query: (_ for _ in ()).throw(
            btp_outreach.PublicSearchBudgetError("Tavily daily budget exhausted")
        ),
        fallback=lambda query: (
            calls.append(query) or [{"url": "https://example-company.ma"}]
        ),
        max_fallback_calls=2,
    )

    assert router("example company") == [{"url": "https://example-company.ma"}]
    assert router("another company") == [{"url": "https://example-company.ma"}]
    assert calls == ["example company", "another company"]
    with pytest.raises(btp_outreach.PublicSearchBudgetError, match="run budgets"):
        router("third company")


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

    def create_draft(settings_, *, to, subject, body, attachments, attempt_key=""):
        calls.append((to, subject, body, attachments))
        return True, "draft-review-only", ""

    monkeypatch.setattr(provider, "create_draft", create_draft)
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    report = _run(db, config, settings, profile, companies, researcher)

    assert report.drafts == 1
    assert report.no_spontaneous_instructions == 1
    assert report.no_qualifying_email == 2
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


def test_uncertain_btp_draft_is_reconciled_before_any_new_create(
    db, config, settings, profile, monkeypatch, tmp_path,
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    candidate = _company("Recovery BTP", "https://recovery.ma")
    researcher = _Researcher({
        "recovery.ma": _evidence("recovery.ma", email="jobs@recovery.ma"),
    })
    create_calls = []
    reconcile_calls = []
    monkeypatch.setattr(
        provider, "create_draft",
        lambda *_args, **_kwargs: (
            create_calls.append("create")
            or (False, "", provider.GMAIL_DRAFT_OUTCOME_UNCERTAIN)
        ),
    )
    monkeypatch.setattr(
        provider, "reconcile_draft",
        lambda *_args, **_kwargs: (
            reconcile_calls.append("reconcile")
            or provider.DraftReconciliation("not_found")
        ),
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    state_path = tmp_path / "recovery-state.json"

    first = _run(
        db, config, settings, profile, [candidate], researcher,
        screening_state_path=state_path,
    )
    assert first.draft_attempts == 1
    assert first.companies[0]["status"] == "draft_recovery_required"
    assert first.companies[0]["delivery_stage"] == "gmail_draft_recovery_required"
    assert db.query(models.Application).one().status == "deferred"
    assert db.query(models.Email).one().delivery_state == "uncertain"

    db.commit()
    db.expire_all()
    second = _run(
        db, config, settings, profile, [candidate], researcher,
        screening_state_path=state_path,
    )
    assert second.draft_attempts == 0
    assert second.existing == 1
    assert second.companies[0]["status"] == "draft_recovery_required"
    assert create_calls == ["create"]
    assert reconcile_calls == ["reconcile"]


@pytest.mark.parametrize(
    ("reconciliation", "expected_status", "expected_sent"),
    [
        (provider.LiveSendReconciliation("found", "gmail-message", 1),
         "existing_application", 1),
        (provider.LiveSendReconciliation("not_found"),
         "live_send_recovery_required", 0),
    ],
)
def test_uncertain_btp_live_send_is_reconciled_without_a_new_send(
    db, config, settings, profile, monkeypatch, reconciliation,
    expected_status, expected_sent,
):
    settings = settings.model_copy(update={
        "email_mode": "live", "enable_email": True, "email_provider": "gmail",
        "authorize_live_spontaneous": True,
    })
    candidate = _company("Live Recovery BTP", "https://live-recovery.ma")
    company = store.get_or_create_company(db, candidate.name)
    company.website = candidate.website
    company.official_domain = "live-recovery.ma"
    job = models.Job(
        source="btp_spontaneous", external_id="live-recovery",
        dedup_key="live-recovery-job", title=candidate.name,
        company_id=company.id, location="Casablanca, Morocco", country="Morocco",
        description="BTP company", url="https://live-recovery.ma",
        contact_email="jobs@live-recovery.ma", status="outreach_only",
        opportunity_type="SPONTANEOUS_APPLICATION",
    )
    db.add(job)
    db.flush()
    decision = store.add_decision(
        db, job.id, "APPLY", 0, {}, "verified instructions", [],
    )
    application = store.add_application(
        db, job.id, decision.id, "APPLY", 0, job.contact_email,
    )
    application.status = "deferred"
    email = store.add_email(
        db, application.id, job.contact_email, "Application", "Hello",
        status="uncertain", mode="live",
    )
    email.delivery_state = "uncertain"
    attempt = models.LiveSendAttempt(
        attempt_key="a" * 32, active_job_id=job.id, job_id=job.id,
        application_id=application.id, email_id=email.id,
        employer_name=candidate.name, recipient=job.contact_email,
        subject=email.subject, message_fingerprint="b" * 64,
        state="uncertain",
    )
    db.add(attempt)
    db.commit()
    monkeypatch.setattr(
        provider, "reconcile_live_send",
        lambda *_args, **_kwargs: reconciliation,
    )
    monkeypatch.setattr(
        provider, "send",
        lambda *_args, **_kwargs: pytest.fail("recovery must never dispatch a new send"),
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)

    report = _run(
        db, config, settings, profile, [candidate],
        _Researcher({"live-recovery.ma": []}),
    )

    assert report.sent == expected_sent
    assert report.companies[0]["status"] == expected_status
    assert db.query(models.LiveSendAttempt).one().state == (
        "confirmed_sent" if expected_sent else "uncertain"
    )


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


def test_search_budget_defers_only_unknown_site_and_known_sites_still_create_draft(
    db, config, settings, profile, monkeypatch, tmp_path
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    candidate_a = _company("Unknown BTP", website="", distance=1)
    candidate_b = _company("Atlas Company", website="https://atlas.ma", distance=5)
    candidate_c = _company("Rif Company", website="https://rif.ma", distance=8)
    researcher = _Researcher({
        "atlas.ma": _evidence("atlas.ma", email="recruitment@atlas.ma"),
        "rif.ma": _evidence("rif.ma", instruction=True, email=""),
    })
    finder_calls = []

    class ExhaustedFinder:
        def find(self, company_name, location=""):
            finder_calls.append(company_name)
            raise btp_outreach.PublicSearchBudgetError("Tavily daily budget exhausted")

    class ExhaustedCompanySearch:
        def __init__(self):
            self.errors = ["public company search failed: Tavily daily budget exhausted"]
            self.search_budget_limited = True

        def discover(self):
            return []

    draft_calls = []
    screening_state_path = tmp_path / "btp-screening.json"
    monkeypatch.setattr(
        provider,
        "create_draft",
        lambda *_args, **_kwargs: (
            draft_calls.append("draft") or (True, "draft-id", "")
        ),
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)

    report = _run(
        db, config, settings, profile,
        [candidate_a, candidate_b, candidate_c],
        researcher,
        company_search=ExhaustedCompanySearch(),
        website_finder=ExhaustedFinder(),
        screening_state_path=screening_state_path,
    )

    statuses = {item["name"]: item["status"] for item in report.companies}
    assert statuses == {
        "Atlas Company": "drafted_for_review",
        "Rif Company": "no_recruitment_contact",
        "Unknown BTP": "search_budget_deferred",
    }
    assert finder_calls == ["Unknown BTP"]
    assert researcher.visited == ["atlas.ma", "rif.ma"]
    assert draft_calls == ["draft"]
    assert report.candidates == 3
    assert report.known_website_candidates == 2
    assert report.website_search_required == 1
    assert report.search_budget_deferred == 1
    assert report.search_budget_limited
    assert report.researched == 2
    assert report.drafts == 1
    assert report.as_run_report()["action"]["btp"]["gmail_drafts_confirmed"] == 1
    drafted_company = next(
        company for company in report.companies if company["name"] == "Atlas Company"
    )
    assert drafted_company["gmail_draft_confirmed"]
    assert drafted_company["company"] == "Atlas Company"
    assert drafted_company["website"] == "https://atlas.ma/"
    assert drafted_company["official_domain"] == "atlas.ma"
    assert drafted_company["recruitment_evidence_source"] == "https://atlas.ma/contact"
    assert drafted_company["contact_email_source"] == "https://atlas.ma/contact"
    assert drafted_company["eligibility_reason"]
    assert drafted_company["previous_application"] is False
    assert drafted_company["previous_draft"] is False
    assert drafted_company["cooldown"] is False
    assert json.loads(screening_state_path.read_text(encoding="utf-8"))[
        "companies"
    ][BtpOutreachState.key(candidate_b)]["gmail_draft_id"] == "draft-id"
    assert report.no_qualifying_email == 1
    serialized = report.as_run_report()["action"]["btp"]
    assert serialized["known_website_candidates"] == 2
    assert serialized["website_search_required"] == 1
    assert serialized["search_budget_deferred"] == 1
    assert serialized["drafts"] == 1
    assert serialized["no_official_website"] == 0
    assert serialized["sent"] == 0
    persisted_email = db.query(models.Email).one()
    assert persisted_email.delivery_state == "confirmed"
    assert persisted_email.draft_attempt_key

    db.commit()
    db.expire_all()
    next_run = _run(
        db, config, settings, profile,
        [candidate_a, candidate_b, candidate_c],
        researcher,
        company_search=ExhaustedCompanySearch(),
        website_finder=ExhaustedFinder(),
        screening_state_path=screening_state_path,
    )
    assert next_run.drafts == 0
    assert next_run.existing == 1
    assert next_run.search_budget_deferred == 1
    duplicate_company = next(
        company for company in next_run.companies
        if company["name"] == "Atlas Company"
    )
    assert duplicate_company["previous_application"] is True
    assert duplicate_company["previous_draft"] is True
    assert draft_calls == ["draft"]


def test_draft_without_gmail_id_is_not_counted_as_confirmed(
    db, config, settings, profile, monkeypatch, tmp_path
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    company = _company("Unconfirmed", "https://unconfirmed.ma")
    researcher = _Researcher({
        "unconfirmed.ma": _evidence("unconfirmed.ma", email="jobs@unconfirmed.ma"),
    })
    monkeypatch.setattr(provider, "create_draft", lambda *_args, **_kwargs: (True, "", ""))
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)

    report = _run(
        db, config, settings, profile, [company], researcher,
        screening_state_path=tmp_path / "screening.json",
    )

    assert report.drafts == 0
    assert report.blocked == 1
    assert report.as_run_report()["action"]["btp"]["gmail_drafts_confirmed"] == 0
    assert report.companies[0]["status"] == "draft_unconfirmed"
    assert any("without a draft ID" in error for error in report.errors)
    assert db.query(models.Email).one().draft_id == ""


def test_company_pool_reuses_verified_morocco_employer_website_without_search(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    company = store.get_or_create_company(
        db,
        "Civil Pool Employer",
        "https://civil-pool.ma/careers",
        "Morocco",
        industry="civil engineering",
        source="company_careers_direct",
        official_domain="civil-pool.ma",
    )
    db.add(models.Job(
        source="company_careers_direct",
        external_id="pool-job",
        dedup_key="pool-job",
        title="Ingénieur génie civil",
        company_id=company.id,
        country="Morocco",
        location="Rabat",
        description="Infrastructure and public works engineering.",
    ))
    db.flush()
    researcher = _Researcher({
        "civil-pool.ma": _evidence(
            "civil-pool.ma", email="recrutement@civil-pool.ma"
        ),
    })

    class NoSearchFinder:
        def find(self, *_args, **_kwargs):
            raise AssertionError("A verified company-pool website must bypass search")

    monkeypatch.setattr(
        provider, "create_draft", lambda *_args, **_kwargs: (True, "draft-id", "")
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    report = _run(
        db, config, settings, profile, [], researcher, website_finder=NoSearchFinder()
    )

    assert report.company_pool_candidates == 1
    assert report.candidates == 1
    assert report.researched == 1
    assert report.drafts == 1
    assert researcher.visited == ["civil-pool.ma"]
    assert report.companies[0]["name"] == "Civil Pool Employer"


def test_company_pool_reuses_fresh_recruitment_evidence_without_search_or_recrawl(
    db, config, settings, profile, monkeypatch, tmp_path
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    company = store.get_or_create_company(
        db,
        "Morocco Civil Works",
        "https://civil-works.ma/",
        "Morocco",
        careers_url="https://civil-works.ma/careers",
        recruitment_url="https://civil-works.ma/careers",
        industry="BTP civil engineering and infrastructure",
        source="company_careers_direct",
        official_domain="civil-works.ma",
    )
    company.last_researched_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.add(models.Job(
        source="company_careers_direct",
        external_id="civil-works-job",
        dedup_key="civil-works-job",
        title="Technicien BTP",
        company_id=company.id,
        location="Rabat, Morocco",
        country="Morocco",
        description="Civil construction and infrastructure role.",
    ))
    db.add(models.Evidence(
        company_id=company.id,
        source_url="https://civil-works.ma/careers",
        source_type="recruitment",
        page_title="Careers",
        field_name="recruitment_email",
        extracted_value="recrutement@civil-works.ma",
        snippet="Recruitment contact: recrutement@civil-works.ma",
        domain_relationship="company_recruitment",
        confidence=100,
        reason_code="employer_domain",
    ))
    address = "recrutement@civil-works.ma"
    db.add(models.Evidence(
        company_id=company.id,
        source_url="https://civil-works.ma/careers",
        source_type="recruitment",
        page_title="Careers",
        field_name="application_instructions",
        extracted_value=f"Envoyez votre CV de candidature à {address}.",
        snippet=f"Envoyez votre CV de candidature à {address}.",
        confidence=95,
        reason_code="explicit_application_submission_instruction",
    ))
    db.flush()

    class SearchMustNotRun:
        called = False

        def discover(self):
            self.called = True
            raise AssertionError("Company-pool target must be consumed before web search")

    class ResearchMustNotRun:
        def research(self, *_args, **_kwargs):
            raise AssertionError("Fresh persisted employer recruitment evidence must be reused")

    search = SearchMustNotRun()
    draft_ids = iter(("pool-draft-id", "unexpected-second-draft"))
    draft_calls = []
    monkeypatch.setattr(
        provider,
        "create_draft",
        lambda *_args, **_kwargs: (
            draft_calls.append("draft") or (True, next(draft_ids), "")
        ),
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    state_path = tmp_path / "company-pool-screening.json"

    first = _run(
        db, config, settings, profile, [], ResearchMustNotRun(),
        company_search=search,
        screening_state_path=state_path,
        max_companies=1,
    )
    assert first.company_pool_candidates == 1
    assert first.external_company_search_skipped
    assert first.known_website_candidates == 1
    assert first.draft_attempts == 1
    assert first.drafts == 1
    assert first.as_run_report()["action"]["btp"]["gmail_drafts_confirmed"] == 1
    assert first.companies[0]["readiness"] == "OUTREACH_ELIGIBLE"
    assert first.companies[0]["location"] == "Rabat, Morocco"
    assert first.companies[0]["previous_application"] is False
    assert first.companies[0]["previous_draft"] is False
    assert first.companies[0]["cooldown"] is False
    assert db.query(models.Email).one().draft_id == "pool-draft-id"
    assert not search.called

    second = _run(
        db, config, settings, profile, [], ResearchMustNotRun(),
        company_search=search,
        screening_state_path=state_path,
        max_companies=1,
    )
    assert second.drafts == 0
    assert second.as_run_report()["action"]["btp"]["gmail_drafts_confirmed"] == 0
    assert second.existing == 1
    assert second.as_run_report()["action"]["sent"] == 0
    assert second.companies[0]["previous_draft"] is True
    assert draft_calls == ["draft"]


def test_official_careers_email_without_literal_submission_instruction_is_eligible(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={"email_mode": "dry_run", "enable_email": False})
    recruitment_email = EvidenceFact(
        source_url="https://recruiting.ma/careers",
        source_type="recruitment",
        page_title="Careers — Join our team",
        field_name="recruitment_email",
        extracted_value="jobs@recruiting.ma",
        snippet="Careers contact: jobs@recruiting.ma",
        domain_relationship="company_recruitment",
        confidence=100,
        reason_code="employer_domain_recruitment",
    )
    researcher = _Researcher({"recruiting.ma": [recruitment_email]})
    monkeypatch.setattr(
        provider, "send",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not call Gmail"),
    )
    monkeypatch.setattr(
        provider, "create_draft",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not create a Gmail draft"),
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)

    report = _run(
        db, config, settings, profile,
        [_company("Recruiting Company", "https://recruiting.ma")],
        researcher,
    )

    company = report.companies[0]
    assert report.validation_attempts == 1
    assert company["status"] == "dry_run_validated"
    assert company["contact_email"] == "jobs@recruiting.ma"
    assert company["contact_email_source"] == recruitment_email.source_url
    assert company["recruitment_evidence_source"] == recruitment_email.source_url
    assert company["application_instructions"] == ""
    assert company["application_route_type"] == "level_b_official_recruitment_email"
    assert company["application_evidence_excerpt"] == recruitment_email.snippet
    assert company["application_evidence_confidence"] == 100
    assert "spontaneous" in company["eligibility_reason"]
    assert company["previous_application"] is False
    assert company["previous_draft"] is False


def test_application_instruction_must_name_the_qualified_email_route():
    source_url = "https://route-check.ma/careers"
    instruction = EvidenceFact(
        source_url=source_url,
        source_type="recruitment",
        page_title="Careers",
        field_name="application_instructions",
        extracted_value="Envoyez votre CV de candidature à candidatures@route-check.ma.",
        snippet="Envoyez votre CV de candidature à candidatures@route-check.ma.",
    )
    email = EvidenceFact(
        source_url=source_url,
        source_type="recruitment",
        page_title="Careers",
        field_name="recruitment_email",
        extracted_value="recrutement@route-check.ma",
        snippet="Contact recrutement: recrutement@route-check.ma",
        domain_relationship="company_recruitment",
    )

    assert btp_outreach._qualifying_contact(
        [instruction, email], "route-check.ma",
    ) is None


def test_official_recruitment_email_with_explicit_instructions_is_level_a(
    db, config, settings, profile, monkeypatch,
):
    settings = settings.model_copy(update={"email_mode": "dry_run", "enable_email": False})
    source_url = "https://apply-check.ma/careers"
    address = "jobs@apply-check.ma"
    instruction = EvidenceFact(
        source_url=source_url,
        source_type="recruitment",
        page_title="Careers",
        field_name="application_instructions",
        extracted_value=f"Submit your CV and application to {address}.",
        snippet=f"Submit your CV and application to {address}.",
        confidence=96,
    )
    email = EvidenceFact(
        source_url=source_url,
        source_type="recruitment",
        page_title="Careers",
        field_name="recruitment_email",
        extracted_value=address,
        snippet=f"Careers contact: {address}",
        domain_relationship="company_recruitment",
        confidence=98,
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)

    report = _run(
        db, config, settings, profile,
        [_company("Apply Check", "https://apply-check.ma")],
        _Researcher({"apply-check.ma": [instruction, email]}),
    )

    company = report.companies[0]
    assert report.validation_attempts == 1
    assert company["application_route_type"] == "level_a_explicit_email_instruction"
    assert company["application_instructions"] == instruction.extracted_value
    assert company["application_evidence_confidence"] == instruction.confidence


def test_generic_info_contact_on_careers_page_remains_excluded_by_default(
    db, config, settings, profile, monkeypatch,
):
    settings = settings.model_copy(update={"email_mode": "dry_run", "enable_email": False})
    generic = EvidenceFact(
        source_url="https://generic-careers.ma/careers",
        source_type="careers",
        page_title="Careers",
        field_name="general_email",
        extracted_value="info@generic-careers.ma",
        snippet="Contact: info@generic-careers.ma",
        domain_relationship="company_general",
        confidence=100,
    )
    researcher = _Researcher({"generic-careers.ma": [generic]})
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)

    report = _run(
        db, config, settings, profile,
        [_company("Generic Careers", "https://generic-careers.ma")],
        researcher,
    )

    assert report.validation_attempts == 0
    assert report.companies[0]["status"] == "no_recruitment_contact"
    assert report.companies[0]["eligibility_reason_code"] == (
        "missing_application_instructions"
    )


def test_unpublished_or_fabricated_recruitment_address_is_not_eligible():
    page_context_only = EvidenceFact(
        source_url="https://fabricated-careers.ma/careers",
        source_type="careers",
        page_title="Careers",
        field_name="relevant_page",
        extracted_value="https://fabricated-careers.ma/careers",
        snippet="Careers",
    )

    assert btp_outreach._qualifying_contact(
        [page_context_only], "fabricated-careers.ma",
    ) is None
    fabricated = EvidenceFact(
        source_url=page_context_only.source_url,
        source_type="careers",
        page_title="Careers",
        field_name="recruitment_email",
        extracted_value="jobs@fabricated-careers.ma",
        snippet="Careers contact: jobs@fabricated-careers.ma",
        domain_relationship="company_recruitment",
        confidence=100,
    )
    assert btp_outreach._qualifying_contact(
        [fabricated], "fabricated-careers.ma",
    ) is not None
    unrelated = EvidenceFact(
        source_url=page_context_only.source_url,
        source_type="careers",
        page_title="Careers",
        field_name="recruitment_email",
        extracted_value="jobs@invented-mail.example",
        snippet="Careers contact: jobs@invented-mail.example",
        domain_relationship="company_recruitment",
        confidence=100,
    )
    assert btp_outreach._qualifying_contact(
        [unrelated], "fabricated-careers.ma",
    ) is None


def test_configured_level_c_contact_requires_same_page_explicit_instructions():
    source_url = "https://configured-contact.ma/careers"
    address = "info@configured-contact.ma"
    instruction = EvidenceFact(
        source_url=source_url,
        source_type="careers",
        page_title="Careers",
        field_name="application_instructions",
        extracted_value=f"Submit your CV for employment enquiries to {address}.",
        snippet=f"Submit your CV for employment enquiries to {address}.",
        confidence=92,
    )
    email = EvidenceFact(
        source_url=source_url,
        source_type="careers",
        page_title="Careers",
        field_name="general_email",
        extracted_value=address,
        snippet=f"General contact: {address}",
        domain_relationship="company_general",
        confidence=100,
    )

    assert btp_outreach._qualifying_contact(
        [instruction, email], "configured-contact.ma",
    ) is None
    route = btp_outreach._qualifying_contact(
        [instruction, email], "configured-contact.ma",
        allow_general_contact=True,
    )
    assert route is not None
    assert route[0] == address
    assert route[3] == "level_c_configured_general_contact"


def test_explicit_apply_label_qualifies_published_general_recipient():
    source_url = "https://www.megec.com/candidature-spontanee/"
    address = "Contact@megec.com"
    instruction = EvidenceFact(
        source_url=source_url,
        source_type="spontaneous",
        page_title="Candidature spontanée",
        field_name="application_instructions",
        extracted_value=f"Postuler : {address}",
        snippet=f"Postuler : {address}",
        confidence=95,
    )
    email = EvidenceFact(
        source_url=source_url,
        source_type="spontaneous",
        page_title="Candidature spontanée",
        field_name="general_email",
        extracted_value=address,
        snippet=f"Postuler : {address}",
        domain_relationship="company_general",
        confidence=95,
    )

    route = btp_outreach._qualifying_contact(
        [instruction, email], "megec.com",
    )

    assert route is not None
    assert route[0] == address.lower()
    assert route[3] == "level_a_explicit_email_instruction"
    assert extract_application_instructions(
        "Postuler : Contact@megec.com"
    ) == "Postuler : Contact@megec.com"


def test_targeted_btp_dry_run_only_processes_allowlisted_employer(
    db, config, settings, profile, monkeypatch,
):
    settings = settings.model_copy(update={"email_mode": "dry_run", "enable_email": False})
    unrelated = _Discovery([_company("Unrelated", "https://unrelated.ma")])
    source_url = "https://www.megec.com/candidature-spontanee/"
    address = "Contact@megec.com"
    evidence = [
        EvidenceFact(
            source_url=source_url,
            source_type="spontaneous",
            page_title="Candidature spontanée",
            field_name="application_instructions",
            extracted_value=f"Postuler : {address}",
            snippet=f"Postuler : {address}",
            confidence=95,
        ),
        EvidenceFact(
            source_url=source_url,
            source_type="spontaneous",
            page_title="Candidature spontanée",
            field_name="general_email",
            extracted_value=address,
            snippet=f"Postuler : {address}",
            domain_relationship="company_general",
            confidence=95,
        ),
    ]
    researcher = _Researcher({"megec.com": evidence})
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    monkeypatch.setattr(
        provider, "send",
        lambda *_args, **_kwargs: pytest.fail("targeted dry run must never send"),
    )
    monkeypatch.setattr(
        provider, "create_draft",
        lambda *_args, **_kwargs: pytest.fail("targeted dry run must not create drafts"),
    )

    report = _run(
        db, config, settings, profile, unrelated.companies, researcher,
        discovery=unrelated,
        target_employer="megec",
        max_companies=1,
    )

    assert unrelated.called is False
    assert report.target_employer == "MEGEC"
    assert report.processed == 1
    assert [company["name"] for company in report.companies] == ["MEGEC"]
    assert report.companies[0]["contact_email"] == "contact@megec.com"
    assert report.companies[0]["application_route_type"] == (
        "level_a_explicit_email_instruction"
    )
    assert report.send_attempts == report.draft_attempts == 0
    assert report.sent == report.drafts == 0


def test_cetemco_current_vacancy_requires_manual_title_and_cv_review(
    db, config, settings, profile, monkeypatch,
):
    settings = settings.model_copy(update={"email_mode": "dry_run", "enable_email": False})
    evidence = _evidence("cetemco.org", email="recrute@cetemco.ma")
    evidence[0] = EvidenceFact(
        source_url="https://cetemco.org/offres-demplois/",
        source_type="careers",
        page_title="Offres d'emplois — CETEMCO",
        field_name="application_instructions",
        extracted_value=(
            "Pour postuler à Ingénieur Technico-commercial, envoyez votre CV à "
            "recrute@cetemco.ma, en précisant l'intitulé du poste dans l'objet du mail."
        ),
        snippet=(
            "Pour postuler à Ingénieur Technico-commercial, envoyez votre CV à "
            "recrute@cetemco.ma, en précisant l'intitulé du poste dans l'objet du mail."
        ),
        confidence=95,
    )
    evidence[1] = EvidenceFact(
        source_url="https://cetemco.org/offres-demplois/",
        source_type="careers",
        page_title="Offres d'emplois — CETEMCO",
        field_name="recruitment_email",
        extracted_value="recrute@cetemco.ma",
        snippet="Pour postuler: recrute@cetemco.ma",
        domain_relationship="official_alternate_recruitment_domain",
        confidence=95,
    )
    researcher = _Researcher({"cetemco.org": evidence})
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    monkeypatch.setattr(
        provider, "send",
        lambda *_args, **_kwargs: pytest.fail("CETEMCO vacancy review must not send"),
    )
    monkeypatch.setattr(
        provider, "create_draft",
        lambda *_args, **_kwargs: pytest.fail("CETEMCO vacancy review must not draft"),
    )

    report = _run(
        db, config, settings, profile, [], researcher,
        target_employer="cetemco",
        max_companies=1,
    )

    company = report.companies[0]
    assert company["status"] == "blocked_by_safety_gate"
    assert company["eligibility_reason_code"] == (
        "vacancy_subject_and_cv_fit_not_supported"
    )
    assert company["application_instructions"].find(
        "l'intitulé du poste dans l'objet"
    ) >= 0
    assert report.validation_attempts == report.send_attempts == report.draft_attempts == 0


def test_btp_target_rejects_arbitrary_employer_strings(db, config, settings, profile):
    settings = settings.model_copy(update={"email_mode": "dry_run", "enable_email": False})
    with pytest.raises(BtpOutreachSafetyError, match="supported official-employer list"):
        run_btp_outreach(
            db,
            config,
            settings,
            profile,
            target_employer="https://attacker.example/",
        )


def test_official_application_portal_records_qualifying_evidence(
    db, config, settings, profile, monkeypatch,
):
    settings = settings.model_copy(update={"email_mode": "dry_run", "enable_email": False})
    source_url = "https://portal-qualified.ma/careers"
    portal_url = "https://portal-qualified.ma/apply"
    instruction = EvidenceFact(
        source_url=source_url,
        source_type="careers",
        page_title="Careers",
        field_name="application_instructions",
        extracted_value="Submit your application through our online form.",
        snippet="Submit your application through our online form.",
        confidence=94,
    )
    portal = EvidenceFact(
        source_url=source_url,
        source_type="careers",
        page_title="Careers",
        field_name="application_url",
        extracted_value=portal_url,
        snippet="Apply online",
        confidence=90,
    )
    monkeypatch.setattr(
        provider, "send",
        lambda *_args, **_kwargs: pytest.fail("portal research must not call Gmail"),
    )
    monkeypatch.setattr(
        provider, "create_draft",
        lambda *_args, **_kwargs: pytest.fail("portal research must not create drafts"),
    )

    report = _run(
        db, config, settings, profile,
        [_company("Portal Qualified", "https://portal-qualified.ma")],
        _Researcher({"portal-qualified.ma": [instruction, portal]}),
    )

    company = report.companies[0]
    assert report.portal_or_form == 1
    assert company["application_route_type"] == "level_a_official_portal"
    assert company["application_url"] == portal_url
    assert company["application_evidence_url"] == source_url
    assert company["application_evidence_excerpt"] == instruction.extracted_value
    assert company["application_evidence_confidence"] == instruction.confidence


def test_enabled_general_contact_fallback_still_requires_submission_instructions(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    config.email["btp_allow_official_general_contact"] = True
    evidence = _evidence(
        "general-contact.ma", instruction=False, email="info@general-contact.ma",
    )
    researcher = _Researcher({"general-contact.ma": evidence})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Configured general-contact fallback cannot bypass instructions")

    monkeypatch.setattr(provider, "create_draft", forbidden)
    report = _run(
        db, config, settings, profile,
        [_company("General Contact", "https://general-contact.ma")],
        researcher,
    )

    assert report.draft_attempts == 0
    assert report.companies[0]["eligibility_reason_code"] == (
        "missing_application_instructions"
    )


def test_partial_crawl_without_instructions_fails_closed_with_reason_code(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    address = "recrutement@partial-crawl.ma"
    recruitment_email = EvidenceFact(
        source_url="https://partial-crawl.ma/careers",
        source_type="recruitment",
        page_title="Careers",
        field_name="recruitment_email",
        extracted_value=address,
        snippet=f"Recruitment contact: {address}",
        domain_relationship="company_recruitment",
        confidence=100,
    )

    class PartiallyBlockedResearcher(_Researcher):
        def research(self, *args, **kwargs):
            result = super().research(*args, **kwargs)
            result.blocked = ["https://partial-crawl.ma/recruitment"]
            return result

    researcher = PartiallyBlockedResearcher({
        "partial-crawl.ma": [recruitment_email],
    })
    researcher.evidence_by_domain["partial-crawl.ma"].append(EvidenceFact(
        source_url="https://partial-crawl.ma/careers",
        source_type="recruitment",
        page_title="Careers",
        field_name="application_instructions",
        extracted_value="Submit your application to jobs@partial-crawl.ma.",
        snippet="Submit your application to jobs@partial-crawl.ma.",
        confidence=95,
    ))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A partial crawl cannot authorize a draft without instructions")

    monkeypatch.setattr(provider, "create_draft", forbidden)
    report = _run(
        db, config, settings, profile,
        [_company("Partial Crawl", "https://partial-crawl.ma")],
        researcher,
    )

    assert report.draft_attempts == 0
    assert report.companies[0]["status"] == "crawl_incomplete"
    assert report.companies[0]["eligibility_reason_code"] == "crawl_incomplete"


def test_official_portal_without_submission_instructions_is_not_eligible(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    portal = EvidenceFact(
        source_url="https://portal-incomplete.ma/careers",
        source_type="careers",
        page_title="Careers",
        field_name="application_url",
        extracted_value="https://portal-incomplete.ma/apply",
        snippet="Apply",
    )
    researcher = _Researcher({"portal-incomplete.ma": [portal]})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("An application link without instructions is not actionable")

    monkeypatch.setattr(provider, "create_draft", forbidden)
    report = _run(
        db, config, settings, profile,
        [_company("Portal Without Instructions", "https://portal-incomplete.ma")],
        researcher,
    )

    assert report.portal_or_form == 0
    assert report.draft_attempts == 0
    assert report.companies[0]["status"] == "no_recruitment_contact"
    assert report.companies[0]["eligibility_reason_code"] == (
        "missing_application_instructions"
    )


def test_recruitment_email_named_by_explicit_application_instruction_qualifies(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    address = "recrutement@recruiting.ma"
    source_url = "https://recruiting.ma/careers"
    instruction = EvidenceFact(
        source_url=source_url,
        source_type="recruitment",
        page_title="Careers",
        field_name="application_instructions",
        extracted_value=f"Envoyez votre CV de candidature à {address}.",
        snippet=f"Envoyez votre CV de candidature à {address}.",
    )
    recruitment_email = EvidenceFact(
        source_url=source_url,
        source_type="recruitment",
        page_title="Careers",
        field_name="recruitment_email",
        extracted_value=address,
        snippet=f"Contact recrutement: {address}",
        domain_relationship="company_recruitment",
        confidence=100,
        reason_code="employer_domain",
    )
    researcher = _Researcher({
        "recruiting.ma": [instruction, recruitment_email],
    })
    monkeypatch.setattr(
        provider, "create_draft", lambda *_args, **_kwargs: (True, "draft-id", "")
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)

    report = _run(
        db, config, settings, profile,
        [_company("Recruiting Company", "https://recruiting.ma")],
        researcher,
    )

    assert report.drafts == 1
    assert report.no_explicit_instructions == 0
    assert report.companies[0]["application_instructions"] == (
        f"Envoyez votre CV de candidature à {address}."
    )


@pytest.mark.parametrize(
    (
        "company_name",
        "website",
        "start_url",
        "pages",
        "email",
        "contact_source",
        "relationship",
        "verification_method",
    ),
    [
        (
            "Ciments du Maroc",
            "https://www.cimentsdumaroc.com/fr",
            "https://www.cimentsdumaroc.com/fr",
            {
                "https://www.cimentsdumaroc.com/sitemap.xml": (
                    "<urlset><url><loc>"
                    "https://www.cimentsdumaroc.com/fr/award"
                    "</loc></url></urlset>"
                ),
                "https://www.cimentsdumaroc.com/fr": (
                    '<title>Ciments du Maroc</title>'
                    '<nav><a href="/fr/contacteznous">Contact</a>'
                    '<a href="/fr/award">Quarry Life Award</a></nav>'
                    "<p>Construction materials and quarry projects.</p>"
                ),
                "https://www.cimentsdumaroc.com/fr/contacteznous": (
                    "<title>Contact</title><h1>Contactez-nous</h1>"
                    "Envoyez votre CV en précisant le poste souhaité à "
                    "drh-contact@cimar.co.ma."
                ),
                "https://www.cimentsdumaroc.com/fr/award": (
                    "<title>Quarry Life Award</title><h1>Quarry Life Award</h1>"
                    "The award supports biodiversity. Recruitment is mentioned "
                    "in a partner description."
                ),
            },
            "drh-contact@cimar.co.ma",
            "https://www.cimentsdumaroc.com/fr/contacteznous",
            "official_alternate_recruitment_domain",
            "official_page_published_alternate_recruitment_domain",
        ),
        (
            "Senan Holding",
            "https://senanholding.com/careers",
            "https://senanholding.com/careers",
            {
                "https://senanholding.com/sitemap.xml": None,
                "https://senanholding.com/careers": (
                    "<title>Senan Holding Careers</title><h1>Build with Senan</h1>"
                    "<h2>Civil Engineer</h2><p>Engineering &amp; Contracting</p>"
                    "<p>Don't see your role? Send a spontaneous application to "
                    "<a href='mailto:careers@senanholding.com'>"
                    "careers@senanholding.com</a>.</p>"
                ),
            },
            "careers@senanholding.com",
            "https://senanholding.com/careers",
            "company_recruitment",
            "official_employer_posting",
        ),
    ],
)
def test_named_employer_pages_extract_and_qualify_published_recruitment_route(
    db, config, settings, profile, monkeypatch, tmp_path,
    company_name, website, start_url, pages, email, contact_source,
    relationship, verification_method,
):
    class PageResponse:
        def __init__(self, text):
            self.headers = {"Content-Type": "text/html"}
            self.text = text

    responses = {
        url: PageResponse(body) if body is not None else None
        for url, body in pages.items()
    }

    fetched_urls = []

    def fetch(url):
        fetched_urls.append(url)
        return responses.get(url)

    crawler = WebsiteResearcher(
        max_pages=4,
        per_host_delay=0,
        state_path=tmp_path / f"{company_name.split()[0]}-research.json",
        session=db,
        fetcher=fetch,
        robots_fetcher=lambda _domain: None,
        allow_redirects=False,
    )
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    if company_name == "Ciments du Maroc":
        company = store.get_or_create_company(
            db,
            company_name,
            website,
            "Morocco",
            official_domain="cimentsdumaroc.com",
        )
        company.careers_url = (
            "https://www.cimentsdumaroc.com/fr/award"
        )
        company.recruitment_url = (
            "https://www.cimentsdumaroc.com/fr/award"
        )
        db.flush()
    monkeypatch.setattr(
        provider,
        "create_draft",
        lambda *_args, **_kwargs: (True, "fixture-draft-id", ""),
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)

    report = run_btp_outreach(
        db,
        config,
        settings,
        profile,
        discovery=_Discovery([
            BtpCompany(
                name=company_name,
                website=website,
                city="Casablanca",
                region="Casablanca-Settat",
                latitude=33.5731,
                longitude=-7.5898,
                distance_km=0,
                source="fixture",
            )
        ]),
        wikidata_discovery=_Discovery([]),
        company_search=_EmptyCompanySearch(),
        researcher=crawler,
        website_finder=_NoWebsiteFinder(),
        screening_state_path=tmp_path / f"{company_name.split()[0]}-screening.json",
        max_companies=1,
    )

    assert report.draft_attempts == 1, [
        (
            item["status"],
            item["eligibility_reason"],
            item["contact_email"],
            item["contact_email_relationship"],
            item["application_instructions"],
        )
        for item in report.companies
    ] + fetched_urls
    assert report.drafts == 1
    outcome = report.companies[0]
    assert outcome["company"] == company_name
    assert outcome["website"] == website
    assert outcome["contact_email"] == email
    assert outcome["contact_email_relationship"] == relationship
    assert outcome["contact_verification_method"] == verification_method
    assert outcome["contact_email_source"] == contact_source
    assert outcome["application_instructions"]
    assert outcome["gmail_draft_confirmed"]
    if company_name == "Ciments du Maroc":
        assert start_url in fetched_urls
        assert contact_source in fetched_urls
        company = db.query(models.Company).filter_by(name=company_name).one()
        assert company.careers_url == contact_source
        assert company.recruitment_url == contact_source
        assert outcome["careers_url"] == contact_source
        assert outcome["recruitment_url"] == contact_source
    assert db.query(models.EmailVerification).one().verified
    assert db.query(models.EmailVerification).one().verification_method == verification_method
    if company_name == "Ciments du Maroc":
        assert outcome["contact_email"].endswith("@cimar.co.ma")
        assert outcome["contact_email_source"].endswith("/fr/contacteznous")
        assert "Quarry Life Award" not in outcome["application_instructions"]
    else:
        assert outcome["readiness"] == "OUTREACH_ELIGIBLE"
        company = db.query(models.Company).filter_by(name=company_name).one()
        assert db.query(models.Evidence).filter_by(
            company_id=company.id,
            field_name="job_title",
            extracted_value="Civil Engineer",
        ).one()


def test_recruitment_evidence_from_third_party_domain_is_not_qualifying():
    evidence = EvidenceFact(
        source_url="https://jobs.third-party.example/careers",
        source_type="recruitment",
        page_title="Careers",
        field_name="recruitment_email",
        extracted_value="recrutement@recruiting.ma",
        snippet="Recruitment contact",
        confidence=100,
    )

    assert btp_outreach._qualifying_contact([evidence], "recruiting.ma") is None


@pytest.mark.parametrize(
    ("email", "portal", "expected"),
    [
        ("", "", "no_qualifying_email"),
        ("", "https://portal.vendor.example/apply", "portal_or_form"),
        ("", "https://nocontact.ma/apply", "portal_or_form"),
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
    from app.memory.store import count_dispatched_today

    assert count_dispatched_today(db, action="APPLY") == 1
    assert count_dispatched_today(db) == 1


def test_existing_job_application_consumes_shared_btp_application_quota(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    config.rules["max_daily_applications"] = 1
    job, _ = store.upsert_job(db, {
        "source": "company_careers",
        "external_id": "prior-job-application",
        "dedup_key": "prior-job-application",
        "title": "Technicien BTP",
        "country": "Morocco",
        "status": "acted",
    })
    prior = store.add_application(db, job.id, None, "APPLY", 90, "jobs@prior.ma")
    prior.status = "drafted"
    prior.sent_at = utcnow()
    db.flush()
    from app.memory.store import count_dispatched_today

    assert prior.action == "APPLY"
    assert prior.status == "drafted"
    assert prior.sent_at is not None
    assert count_dispatched_today(db, action="APPLY") == 1

    def forbidden(*_args, **_kwargs):
        raise AssertionError("BTP must respect the shared daily application cap")

    monkeypatch.setattr(provider, "create_draft", forbidden)
    researcher = _Researcher({
        "build.ma": _evidence("build.ma", email="jobs@build.ma"),
    })

    report = _run(
        db, config, settings, profile, [_company()], researcher,
    )

    assert report.daily_limit_reached
    assert report.processed == 0
    assert not researcher.visited
    assert db.query(models.Application).count() == 1


def test_authorized_live_btp_run_is_capped_to_one_and_requires_message_id(
    db, config, settings, profile, monkeypatch,
):
    settings = settings.model_copy(update={
        "email_mode": "live",
        "enable_email": True,
        "email_provider": "gmail",
        "authorize_live_spontaneous": True,
    })
    monkeypatch.setattr(
        "app.email.service.validate",
        lambda **_kwargs: SimpleNamespace(allowed=True, reasons=[], checks={}),
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    sends = []
    monkeypatch.setattr(
        provider,
        "send",
        lambda *_args, **_kwargs: (
            sends.append("send") or (True, "message-id", "")
        ),
    )
    companies = [
        _company("Atlas Company", "https://atlas.ma", distance=1),
        _company("Rif Company", "https://rif.ma", distance=2),
    ]
    researcher = _Researcher({
        "atlas.ma": _evidence("atlas.ma", email="jobs@atlas.ma"),
        "rif.ma": _evidence("rif.ma", email="jobs@rif.ma"),
    })

    report = _run(db, config, settings, profile, companies, researcher)

    assert report.processed == 1
    assert report.send_attempts == 1
    assert report.sent == 1
    assert report.drafts == 0
    assert sends == ["send"]
    assert report.companies[0]["delivery_stage"] == "gmail_send_confirmed"


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
