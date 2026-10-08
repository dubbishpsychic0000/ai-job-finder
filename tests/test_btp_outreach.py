from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app import models
from app.discovery.website_researcher import EvidenceFact, ResearchResult
from app.email import provider
from app.memory import store
from app.memory.store import applications_due_for_followup
from app.models import utcnow
from app.workflows.action import pending_actions
from app.workflows.analysis import analyze_job
from app.workflows.btp_outreach import (
    BtpCompany,
    BtpOutreachSafetyError,
    OverpassBtpDiscovery,
    extract_spontaneous_instruction,
    haversine_km,
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
        researcher=researcher,
        **kwargs,
    )


def test_overpass_parser_filters_and_extracts_only_city_region_and_coordinates():
    parsed = parse_overpass_companies({
        "elements": [
            _osm_element("Casablanca BTP", 33.5731, -7.5898, city="Casablanca"),
            _osm_element("Far BTP", 34.0, -6.8, website="https://far.ma"),
            _osm_element("Not construction", 34.0, -6.8, craft="bakery"),
            {"type": "way", "center": {"lat": 34.1, "lon": -6.7},
             "tags": {"name": "No BTP tag", "office": "accountant"}},
        ]
    })
    assert [item.name for item in parsed] == ["Casablanca BTP", "Far BTP"]
    assert parsed[0].city == "Casablanca"
    assert parsed[0].location == "Casablanca, Rabat-Salé-Kénitra, Morocco"
    assert "street" not in repr(parsed[0]).lower()
    assert parsed[0].distance_km == pytest.approx(0)
    assert parsed[1].distance_km == pytest.approx(
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


def test_nearest_first_and_per_run_cap():
    companies = [_company("Furthest", distance=50), _company("Nearest", distance=1),
                 _company("Middle", distance=10)]
    assert [item.name for item in rank_nearest(companies, 2)] == ["Nearest", "Middle"]
    assert len(rank_nearest(companies, 500)) == 3


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
