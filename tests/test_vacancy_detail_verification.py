from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.connectors.base import Opportunity
from app.discovery.opportunity_details import (
    classify_entity,
    classify_opportunity,
    is_verified_individual_vacancy,
)
from app.discovery.vacancy_verification import (
    VacancyDetailVerifier,
    VacancyEvidence,
    supported_ats_detail,
)
from app.memory.store import get_or_create_company
from app.models import Job
from app.workflows import discovery as discovery_workflow

NOW = datetime(2026, 10, 14, 8, tzinfo=timezone.utc)
GREENHOUSE_URL = "https://boards.greenhouse.io/acme/jobs/12345"
SMARTRECRUITERS_URL = (
    "https://jobs.smartrecruiters.com/Acme/744000012345678-civil-engineer"
)


class Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _greenhouse_record(**updates):
    payload = {
        "id": 12345,
        "title": "Civil Engineer",
        "company_name": "Acme Construction",
        "location": {"name": "Casablanca, Morocco"},
        "content": (
            "<p>Design and supervise civil infrastructure projects. "
            "Coordinate site teams and review engineering drawings.</p>"
        ),
        "absolute_url": GREENHOUSE_URL,
    }
    payload.update(updates)
    return payload


def _jsonld(**updates):
    import json

    item = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "datePosted": "2026-10-01",
        "validThrough": "2026-12-01T00:00:00Z",
    }
    item.update(updates)
    return (
        '<script type="application/ld+json">'
        + json.dumps(item)
        + "</script>"
    )


def test_greenhouse_active_detail_requires_matching_structured_record():
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if "boards-api.greenhouse.io" in url:
            return Response(200, _greenhouse_record())
        return Response(
            200,
            text=_jsonld(
                hiringOrganization={"@type": "Organization", "name": "Acme Construction"},
            ),
        )

    result = VacancyDetailVerifier(get=get, now=lambda: NOW).verify(GREENHOUSE_URL)

    assert result.outcome == "OPEN"
    assert result.reason_code == "matching_public_ats_posting_record"
    assert result.title == "Civil Engineer"
    assert result.employer == "Acme Construction"
    assert result.location == "Casablanca, Morocco"
    assert result.country == "Morocco"
    assert result.description.startswith("Design and supervise")
    assert result.posted_at == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert result.posted_at_source == "greenhouse:datePosted"
    assert result.closing_at == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert result.application_url == GREENHOUSE_URL
    assert result.evidence_checks == {
        "employer": True,
        "role": True,
        "location": True,
        "description_sufficient": True,
        "original_detail_page_reference": True,
        "original_detail_page_fetched": True,
        "posting_identity_matched": True,
        "current_status_recognized": False,
        "publication_date": True,
        "deadline": True,
        "application_route_verified": True,
    }
    assert len(calls) == 2
    assert all(call[1]["allow_redirects"] is False for call in calls)


def test_smartrecruiters_active_record_can_have_unknown_posting_date():
    payload = {
        "id": "744000012345678",
        "name": "Civil Engineering Internship",
        "company": {"name": "Acme Construction"},
        "location": {"city": "Casablanca", "country": "Morocco"},
        "jobAd": {
            "sections": [
                {
                    "title": "Responsibilities",
                    "text": "<p>Assist site engineers with field inspections.</p>",
                },
                {
                    "title": "Requirements",
                    "text": "Civil engineering enrollment and field experience.",
                },
            ],
        },
        "status": "published",
        "postingUrl": SMARTRECRUITERS_URL,
    }
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(200, payload)

    result = VacancyDetailVerifier(get=get, now=lambda: NOW).verify(
        SMARTRECRUITERS_URL,
    )

    assert result.outcome == "OPEN"
    assert result.employer == "Acme Construction"
    assert result.location == "Casablanca, Morocco"
    assert "Responsibilities" in result.description
    assert result.evidence_checks["original_detail_page_fetched"] is False
    assert result.posted_at is None
    assert result.posted_at_source == ""
    assert result.application_url == SMARTRECRUITERS_URL
    assert len(calls) == 1


def test_explicit_closed_status_and_elapsed_closing_date_are_closed():
    closed_status = _greenhouse_record(status="closed")
    result = VacancyDetailVerifier(
        get=lambda url, **kwargs: Response(200, closed_status),
        now=lambda: NOW,
    ).verify(GREENHOUSE_URL)
    assert result.outcome == "CLOSED"
    assert result.reason_code == "ats_reports_closed_status"

    expired = {
        "id": "744000012345678",
        "name": "Civil Engineer",
        "company": {"name": "Acme Construction"},
        "location": {"city": "Casablanca", "country": "Morocco"},
        "jobAd": {"sections": [{"title": "Description", "text": "x" * 60}]},
        "status": "published",
        "expirationDate": "2026-10-13",
        "postingUrl": SMARTRECRUITERS_URL,
    }
    result = VacancyDetailVerifier(
        get=lambda url, **kwargs: Response(200, expired),
        now=lambda: NOW,
    ).verify(SMARTRECRUITERS_URL)
    assert result.outcome == "CLOSED"
    assert result.reason_code == "source_reported_closing_date_elapsed"


def test_malformed_dates_remain_unknown_and_collection_time_is_not_posting_date():
    payload = _greenhouse_record(
        updated_at="2026-10-13T00:00:00Z",
        content=(
            "<p>Design roads, bridges, drainage, and other civil infrastructure. "
            "Coordinate construction teams, review engineering drawings, and "
            "support field inspections and project delivery.</p>"
        ),
    )

    def get(url, **kwargs):
        if "boards-api.greenhouse.io" in url:
            return Response(200, payload)
        return Response(200, text=_jsonld(datePosted="not-a-date"))

    result = VacancyDetailVerifier(get=get, now=lambda: NOW).verify(GREENHOUSE_URL)

    assert result.outcome == "OPEN"
    assert result.posted_at is None
    assert result.posted_at_source == ""


def test_http_success_alone_redirect_and_inaccessible_record_are_not_open():
    html_only = VacancyDetailVerifier(
        get=lambda url, **kwargs: Response(200, text="<html>Still available</html>"),
        now=lambda: NOW,
    ).verify(GREENHOUSE_URL)
    assert html_only.outcome == "UNKNOWN"
    assert html_only.reason_code == "ats_detail_invalid_structured_data"

    redirect = VacancyDetailVerifier(
        get=lambda url, **kwargs: Response(302),
        now=lambda: NOW,
    ).verify(SMARTRECRUITERS_URL)
    assert redirect.outcome == "UNKNOWN"
    assert redirect.reason_code == "ats_detail_redirect_unverified"

    unavailable = VacancyDetailVerifier(
        get=lambda url, **kwargs: Response(403),
        now=lambda: NOW,
    ).verify(GREENHOUSE_URL)
    assert unavailable.outcome == "UNKNOWN"
    assert unavailable.reason_code == "ats_detail_unavailable"


def test_mismatched_id_and_insufficient_job_evidence_do_not_verify():
    mismatch = VacancyDetailVerifier(
        get=lambda url, **kwargs: Response(200, _greenhouse_record(id=99)),
        now=lambda: NOW,
    ).verify(GREENHOUSE_URL)
    assert mismatch.outcome == "UNKNOWN"
    assert mismatch.reason_code == "ats_posting_id_mismatch"

    insufficient = _greenhouse_record(
        company_name="",
        location={"name": ""},
        content="<p>Job information.</p>",
    )
    missing = VacancyDetailVerifier(
        get=lambda url, **kwargs: Response(200, insufficient),
        now=lambda: NOW,
    ).verify(GREENHOUSE_URL)
    assert missing.outcome == "UNKNOWN"
    assert missing.reason_code == "ats_record_missing_required_job_evidence"
    assert missing.evidence_checks["employer"] is False
    assert missing.evidence_checks["location"] is False
    assert missing.evidence_checks["description_sufficient"] is False
    assert missing.evidence_checks["posting_identity_matched"] is True


def test_application_route_must_match_the_public_ats_without_embedded_credentials():
    payload = _greenhouse_record(
        absolute_url=(
            "https://embedded-secret@boards.greenhouse.io/acme/jobs/12345"
        ),
    )
    result = VacancyDetailVerifier(
        get=lambda url, **kwargs: Response(200, payload),
        now=lambda: NOW,
    ).verify(GREENHOUSE_URL)

    assert result.outcome == "UNKNOWN"
    assert result.reason_code == "ats_application_route_unverified"
    assert result.application_url == ""


def test_generic_employer_name_is_not_identifiable_vacancy_evidence():
    payload = _greenhouse_record(company_name="Confidential")
    result = VacancyDetailVerifier(
        get=lambda url, **kwargs: Response(200, payload),
        now=lambda: NOW,
    ).verify(GREENHOUSE_URL)

    assert result.outcome == "UNKNOWN"
    assert result.reason_code == "ats_record_missing_required_job_evidence"


def test_direct_ats_leads_are_unverified_until_live_detail_evidence():
    for url, title in (
        (GREENHOUSE_URL, "Civil Engineer"),
        (SMARTRECRUITERS_URL, "Junior Civil Engineer"),
    ):
        opportunity = Opportunity(
            source="tavily_official_ats_search",
            source_type="ats",
            external_id=url,
            title=title,
            url=url,
            raw={"query": "internship PFE civil engineer"},
        )
        assert classify_entity(opportunity) == (
            "INDIVIDUAL_VACANCY",
            "VACANCY_UNVERIFIED",
            "ats_detail_requires_live_evidence",
        )
    assert classify_opportunity("Civil Engineer", "General search snippet") == "JOB"


def test_search_results_cannot_assert_salary_pages_are_vacancies():
    salary = Opportunity(
        source="tavily_web_search",
        external_id="https://www.salaryexpert.com/salary/job/civil-engineer/france",
        title="Technician Civil Engineering Salary in France (2026)",
        url="https://www.salaryexpert.com/salary/job/civil-engineer/france",
    )
    assert classify_entity(salary) == (
        "SALARY_INFORMATION",
        "REJECTED_NON_VACANCY",
        "salary_reference",
    )


def test_careers_category_and_occupational_pages_remain_non_vacancy_leads():
    cases = (
        (
            Opportunity(
                source="tavily_web_search",
                external_id="https://employer.example/careers",
                title="Careers",
                url="https://employer.example/careers",
                source_type="company_career",
            ),
            ("EMPLOYER_CAREERS_PAGE", "RESEARCH_REQUIRED", "employer_careers_page"),
        ),
        (
            Opportunity(
                source="tavily_web_search",
                external_id="https://jobs.example/jobs/search",
                title="Civil engineering vacancies",
                url="https://jobs.example/jobs/search",
            ),
            (
                "JOB_BOARD_CATEGORY",
                "RESEARCH_REQUIRED",
                "job_search_results_page",
            ),
        ),
        (
            Opportunity(
                source="tavily_web_search",
                external_id="https://reference.example/occupation/civil-engineer",
                title="Occupational Outlook for Civil Engineers",
                url="https://reference.example/occupation/civil-engineer",
            ),
            (
                "OCCUPATIONAL_REFERENCE",
                "REJECTED_NON_VACANCY",
                "occupational_reference",
            ),
        ),
    )
    for opportunity, expected in cases:
        assert classify_entity(opportunity) == expected


def test_supported_detail_url_recognition_is_exact():
    assert supported_ats_detail(GREENHOUSE_URL) == ("greenhouse", "acme", "12345")
    assert supported_ats_detail(SMARTRECRUITERS_URL) == (
        "smartrecruiters", "Acme", "744000012345678",
    )
    assert supported_ats_detail("https://boards.greenhouse.io/acme/jobs") is None
    assert supported_ats_detail("https://evil.example/boards.greenhouse.io/acme/jobs/1") is None
    assert supported_ats_detail("http://boards.greenhouse.io/acme/jobs/1") is None
    assert supported_ats_detail(
        "https://user:pass@boards.greenhouse.io/acme/jobs/1",
    ) is None
    assert supported_ats_detail("https://boards.greenhouse.io:444/acme/jobs/1") is None


def test_verified_action_gate_requires_recent_evidence_and_verified_route(db):
    company = get_or_create_company(db, "Acme Construction")
    verified = Job(
        source="tavily_official_ats_search",
        external_id=GREENHOUSE_URL,
        dedup_key="verified-ats-1",
        title="Civil Engineer",
        company_id=company.id,
        location="Casablanca, Morocco",
        description="Design and supervise civil infrastructure projects.",
        url=GREENHOUSE_URL,
        application_url=GREENHOUSE_URL,
        application_method="COMPANY_PORTAL",
        source_type="ats",
        verification_status="verified",
        entity_type="INDIVIDUAL_VACANCY",
        entity_state="VACANCY_VERIFIED",
        entity_reason_code="matching_public_ats_posting_record",
        entity_evidence_url=GREENHOUSE_URL,
        last_verified_at=datetime.now(timezone.utc),
    )
    db.add(verified)
    db.flush()
    assert is_verified_individual_vacancy(verified)
    verified.last_verified_at = NOW - timedelta(days=2)
    assert not is_verified_individual_vacancy(verified)
    verified.last_verified_at = NOW
    verified.application_url = "https://boards.greenhouse.io/acme"
    assert not is_verified_individual_vacancy(verified)
    verified.application_url = GREENHOUSE_URL
    verified.last_verified_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert not is_verified_individual_vacancy(verified)
    verified.last_verified_at = datetime.now(timezone.utc)
    verified.closing_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not is_verified_individual_vacancy(verified)


def test_discovery_persists_detail_evidence_and_source_verification_counts(
    db, config, prefs, profile, monkeypatch,
):
    from app.discovery.verification import freshness_label

    monkeypatch.setattr(
        discovery_workflow,
        "freshness_label",
        lambda posted_at: freshness_label(posted_at, NOW),
    )

    class Connector:
        name = "tavily_official_ats_search"
        kind = "api_search"
        source_type = "search_engine"
        last_origin = "api"
        last_status = "ok"

        async def search(self, query, location):
            return [Opportunity(
                source=self.name,
                source_type="ats",
                external_id=GREENHOUSE_URL,
                title="Civil Engineer search result",
                location="Belgium",
                country="Belgium",
                description="Search snippet only.",
                url=GREENHOUSE_URL,
                raw={"query": query},
            )]

    class Verifier:
        def verify(self, url):
            assert url == GREENHOUSE_URL
            return VacancyEvidence(
                outcome="OPEN",
                reason_code="matching_public_ats_posting_record",
                evidence_url=url,
                observed_at=NOW,
                title="Civil Engineer",
                employer="Acme Construction",
                location="Casablanca, Morocco",
                country="Morocco",
                description=(
                    "Design and supervise civil infrastructure projects and "
                    "coordinate site inspections."
                ),
                posted_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                posted_at_source="greenhouse:datePosted",
                application_url=url,
            )

    monkeypatch.setattr(
        discovery_workflow,
        "_load_source_connectors",
        lambda *args, **kwargs: [(
            {"name": "tavily_official_ats_search", "kind": "tavily"},
            Connector(),
        )],
    )
    monkeypatch.setattr(
        discovery_workflow, "VacancyDetailVerifier", Verifier,
    )
    config.discovery = {
        **(config.discovery or {}),
        "verify_vacancy_details": True,
        "company_universe_discovery": False,
        "employer_discovery": False,
        "immigration_discovery": False,
        "opportunity_source_discovery": False,
        "social_signal_discovery": False,
        "website_research": False,
        "parallel_fetch": False,
        "vocab_llm": False,
        "max_requests_per_source": 1,
    }

    report = asyncio.run(discovery_workflow.run_discovery(
        db,
        config,
        prefs,
        profile=profile,
        max_queries_override=1,
        max_search_combinations=1,
    ))
    job = db.execute(select(Job)).scalar_one()

    assert job.entity_state == "VACANCY_VERIFIED"
    assert job.entity_reason_code == "matching_public_ats_posting_record"
    assert job.entity_evidence_url == GREENHOUSE_URL
    assert job.last_verified_at.replace(tzinfo=None) == NOW.replace(tzinfo=None)
    assert job.company.name == "Acme Construction"
    assert job.location == "Casablanca, Morocco"
    assert job.country == "Morocco"
    assert job.posted_at_source == "greenhouse:datePosted"
    assert job.posted_at_evidence_url == GREENHOUSE_URL
    assert job.application_method == "COMPANY_PORTAL"
    assert job.application_url == GREENHOUSE_URL
    assert job.opportunity_type == "JOB"
    assert job.freshness == "medium"
    assert report.source_reports[0]["verified_individual_vacancies"] == 1
    assert report.source_reports[0]["detail_verification_attempts"] == 1


def test_discovery_rechecks_existing_unverified_ats_duplicate(
    db, config, prefs, profile, monkeypatch,
):
    opportunity = Opportunity(
        source="tavily_official_ats_search",
        source_type="ats",
        external_id=GREENHOUSE_URL,
        title="Civil Engineer search result",
        company="acme",
        location="Belgium",
        description="Search snippet only.",
        url=GREENHOUSE_URL,
        raw={"query": "civil engineer"},
    )
    normalized = discovery_workflow.normalize(opportunity)
    company = get_or_create_company(db, "Acme Construction")
    existing = Job(
        source=opportunity.source,
        external_id=normalized.external_id,
        dedup_key=normalized.dedup_key(),
        canonical_job_id=normalized.canonical_job_id(),
        title="Civil Engineer search result",
        company_id=company.id,
        location="Belgium",
        description="Search snippet only.",
        url=GREENHOUSE_URL,
        status="verification_required",
        source_type="ats",
        verification_status="unverified",
        entity_type="INDIVIDUAL_VACANCY",
        entity_state="VACANCY_UNVERIFIED",
        entity_reason_code="ats_detail_requires_live_evidence",
        entity_evidence_url=GREENHOUSE_URL,
    )
    db.add(existing)
    db.flush()

    class Connector:
        name = "tavily_official_ats_search"
        kind = "api_search"
        source_type = "search_engine"
        last_origin = "cache"
        last_status = "ok"

        async def search(self, query, location):
            return [opportunity]

    class Verifier:
        def verify(self, url):
            assert url == GREENHOUSE_URL
            return VacancyEvidence(
                outcome="OPEN",
                reason_code="matching_public_ats_posting_record",
                evidence_url=url,
                observed_at=datetime.now(timezone.utc),
                title="Civil Engineer",
                employer="Acme Construction",
                location="Casablanca, Morocco",
                country="Morocco",
                description=(
                    "Design and supervise civil infrastructure projects and "
                    "coordinate site inspections."
                ),
                application_url=url,
            )

    monkeypatch.setattr(
        discovery_workflow,
        "_load_source_connectors",
        lambda *args, **kwargs: [(
            {"name": "tavily_official_ats_search", "kind": "tavily"},
            Connector(),
        )],
    )
    monkeypatch.setattr(discovery_workflow, "VacancyDetailVerifier", Verifier)
    config.discovery = {
        **(config.discovery or {}),
        "verify_vacancy_details": True,
        "company_universe_discovery": False,
        "employer_discovery": False,
        "immigration_discovery": False,
        "opportunity_source_discovery": False,
        "social_signal_discovery": False,
        "website_research": False,
        "parallel_fetch": False,
        "vocab_llm": False,
        "max_requests_per_source": 1,
    }

    report = asyncio.run(discovery_workflow.run_discovery(
        db,
        config,
        prefs,
        profile=profile,
        max_queries_override=1,
        max_search_combinations=1,
    ))

    assert existing.entity_state == "VACANCY_VERIFIED"
    assert existing.title == "Civil Engineer"
    assert existing.company.name == "Acme Construction"
    assert existing.application_url == GREENHOUSE_URL
    assert existing.status == "new"
    assert report.new_jobs == 0
    assert report.source_reports[0]["existing_candidates_reverified"] == 1
    assert report.source_reports[0]["existing_candidates_verified"] == 1
