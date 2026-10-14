from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app import models
from app.config import AgentConfig
from app.connectors.base import Opportunity
from app.connectors.emploi_public import (
    EmploiPublicSource,
    _portal_datetime,
    parse_competition_detail,
)
from app.discovery.opportunity_details import (
    classify_entity,
    classify_opportunity,
    is_verified_individual_vacancy,
)
from app.email.service import ApplicationEngine
from app.workflows import discovery as discovery_workflow
from app.workflows.action import pending_actions
from app.workflows.analysis import analyze_job
from app.workflows.discovery_only import _safe_public_competition

FIXTURES = Path(__file__).parent / "fixtures" / "emploi_public"
PORTAL_ID = "baf4c125-9b1e-4fc1-84e2-0d720e7caf80"
DETAIL_URL = f"https://www.emploi-public.ma/fr/concours/details/{PORTAL_ID}"
NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(html: str | None = None, *, observed_at: datetime = NOW):
    return parse_competition_detail(
        html if html is not None else _html("detail_fr.html"),
        DETAIL_URL,
        language="fr",
        observed_at=observed_at,
    )


def _opportunity(metadata: dict | None = None) -> Opportunity:
    value = metadata or _parse()
    url = str(value.get("evidence_url") or DETAIL_URL)
    deadline = (
        datetime.fromisoformat(value["deadline_at"])
        if value.get("deadline_at") else None
    )
    posted_at = (
        datetime.fromisoformat(value["posted_at"])
        if value.get("posted_at") else None
    )
    return Opportunity(
        source="emploi_public_ma",
        external_id=str(value.get("portal_id") or PORTAL_ID),
        source_type="government",
        title=str(value.get("title_original") or "Technicien en Génie Civil"),
        company=str(value.get("administration_original") or ""),
        location=str(value.get("location_original") or ""),
        country="Morocco",
        description=str(value.get("description_original") or ""),
        url=url,
        posted_at=posted_at,
        posted_at_source="emploi_public:date_publication" if posted_at else "",
        posted_at_evidence_url=url if posted_at else "",
        closing_at=deadline,
        employment_type="government_competition",
        verification_status=str(value.get("verification_status") or "unverified"),
        raw={"public_competition": value},
    )


def test_french_detail_has_auditable_open_evidence_and_does_not_fetch_dossier():
    metadata = _parse()

    assert metadata["status"] == "OPEN_ANNOUNCEMENT"
    assert metadata["stage"] == "OPEN_ANNOUNCEMENT"
    assert metadata["title_original"] == "Technicien en Génie Civil"
    assert metadata["administration_original"] == (
        "Direction Régionale de l'Équipement"
    )
    assert metadata["location_original"] == "Rabat-Salé-Kénitra"
    assert metadata["posted_at"]
    assert metadata["deadline_at"]
    assert metadata["reason_code"] == "official_public_competition_verified_open"
    assert metadata["evidence_url"] == DETAIL_URL
    assert metadata["notice_documents_not_fetched"] is True
    assert metadata["notice_documents"][0]["url"].endswith(
        f"/fr/concours/download/{PORTAL_ID}",
    )
    assert metadata["submission_route"]["kind"] == "ONLINE_PORTAL"


def test_arabic_detail_parses_arabic_digits_and_explicit_application_route():
    url = f"https://www.emploi-public.ma/ar/concours/details/{PORTAL_ID}"
    metadata = parse_competition_detail(
        _html("detail_ar.html"),
        url,
        language="ar",
        observed_at=NOW,
    )

    assert metadata["status"] == "OPEN_ANNOUNCEMENT"
    assert metadata["title_original"] == "تقني في الهندسة المدنية"
    assert metadata["post_count"] == 2
    assert metadata["deadline_at"]
    assert metadata["submission_route"]["kind"] == "ONLINE_PORTAL"


def test_deadline_and_ambiguous_or_missing_evidence_stay_conservative():
    expired = _parse(observed_at=datetime(2026, 10, 16, tzinfo=timezone.utc))
    missing_deadline = _parse(
        _html("detail_fr.html").replace(
            "<h3>Délai de dépôt des candidatures : 15 Octobre 2026 - 15:00</h3>",
            "",
        ),
    )
    conflicting_route = _parse(
        _html("detail_fr.html").replace(
            '<a href="https://candidature.equipement.gov.ma/concours/42">Portail officiel</a>',
            "contact@example.gov.ma",
        ),
    )
    missing_stage = _parse(
        _html("detail_fr.html").replace(
            '<a class="active" aria-current="page">Annonce</a>',
            "<a>Annonce</a>",
        ),
    )

    assert expired["status"] == "CLOSED_ANNOUNCEMENT"
    assert expired["reason_code"] == "application_deadline_elapsed"
    assert missing_deadline["status"] == "UNKNOWN"
    assert missing_deadline["reason_code"] == "deadline_missing_or_ambiguous"
    assert conflicting_route["status"] == "UNKNOWN"
    assert conflicting_route["reason_code"] == "submission_route_conflict"
    assert missing_stage["status"] == "UNKNOWN"
    assert missing_stage["reason_code"] == "announcement_stage_not_explicit"
    assert _portal_datetime("32/13/2026") is None


def test_unknown_or_malformed_posting_date_does_not_change_open_status():
    html = _html("detail_fr.html")
    no_posting_date = _parse(
        html.replace(
            "<h3>Date de publication : 10 Octobre 2026</h3>",
            "<h3>Dernière consultation : aujourd'hui</h3>",
        ),
    )
    malformed_posting_date = _parse(
        html.replace(
            "Date de publication : 10 Octobre 2026",
            "Date de publication : 42/19/2026",
        ),
    )

    assert no_posting_date["status"] == "OPEN_ANNOUNCEMENT"
    assert no_posting_date["posted_at"] == ""
    assert no_posting_date["observed_at"]
    assert malformed_posting_date["status"] == "OPEN_ANNOUNCEMENT"
    assert malformed_posting_date["posted_at"] == ""


def test_procedural_notice_is_not_an_open_announcement():
    html = _html("detail_fr.html").replace(
        '<a class="active" aria-current="page">Annonce</a>',
        '<a class="active" aria-current="page">Résultats</a>',
    )
    metadata = _parse(html)

    assert metadata["stage"] == "RESULTS_PUBLICATION"
    assert metadata["status"] == "RESULTS_PUBLICATION"
    assert classify_entity(_opportunity(metadata)) == (
        "UNKNOWN",
        "RESEARCH_REQUIRED",
        "public_competition_results_publication",
    )


def test_portal_landing_pages_are_not_individual_competitions():
    metadata = parse_competition_detail(
        _html("listing_fr.html"),
        "https://www.emploi-public.ma/fr/concours-liste",
        language="fr",
        observed_at=NOW,
    )

    assert metadata["status"] == "UNKNOWN"
    assert metadata["reason_code"] == "invalid_official_detail_url"


def test_search_wording_does_not_classify_a_competition_as_pfe():
    opportunity = _opportunity()
    query = "PFE stage technicien génie civil"
    actual_content_type = classify_opportunity(
        opportunity.title,
        opportunity.description,
        opportunity.source_type,
    )

    assert "PFE" in query
    assert actual_content_type == "JOB"


def test_adapter_is_bounded_and_never_requests_robots_disallowed_documents():
    listing = _html("listing_fr.html").encode()
    detail = _html("detail_fr.html").encode()
    calls: list[tuple[str, bool]] = []

    class Response:
        def __init__(self, content: bytes):
            self.status_code = 200
            self.headers = {"content-type": "text/html; charset=utf-8"}
            self.content = content

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["allow_redirects"]))
        if url.endswith("robots.txt"):
            return Response(b"User-agent: *\nDisallow: /fr/concours/download/\n")
        return Response(listing if url.endswith("concours-liste") else detail)

    source = EmploiPublicSource(max_pages=1, max_detail_pages=1, get=fake_get)
    results = asyncio.run(source.search("technicien génie civil", "Morocco"))

    assert len(results) == 1
    assert len(calls) == 3
    assert all(not follow for _url, follow in calls)
    assert not any("/concours/download/" in url for url, _follow in calls)
    assert source.last_page_requests == 3
    assert results[0].closing_at is not None


def test_pagination_is_bounded_and_later_pages_are_deduplicated():
    calls = []
    listing = _html("listing_fr.html").encode()
    detail = _html("detail_fr.html").encode()

    class Response:
        def __init__(self, content: bytes, content_type: str = "text/html"):
            self.status_code = 200
            self.headers = {"content-type": content_type}
            self.content = content

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("robots.txt"):
            return Response(b"User-agent: *", "text/plain")
        if "page=2" in url:
            return Response(listing)
        if url.endswith("concours-liste"):
            return Response(listing)
        return Response(detail)

    source = EmploiPublicSource(max_pages=2, max_detail_pages=1, get=fake_get)
    results = asyncio.run(source.search("technicien génie civil", "Morocco"))

    assert len(results) == 1
    assert any("page=2" in url for url in calls)
    assert sum(url.endswith("concours-liste") or "page=2" in url for url in calls) == 2
    assert len(calls) == 4


def test_robots_disallow_blocks_listing_before_page_fetch():
    calls = []

    class Response:
        status_code = 200
        content = (
            b"User-agent: *\n"
            b"Disallow: /fr/concours-liste\n"
        )

        def __init__(self):
            self.headers = {"content-type": "text/plain"}

    def fake_get(url, **kwargs):
        calls.append(url)
        return Response()

    source = EmploiPublicSource(max_pages=1, max_detail_pages=1, get=fake_get)
    results = asyncio.run(source.search("technicien génie civil", "Morocco"))

    assert results == []
    assert calls == ["https://www.emploi-public.ma/robots.txt"]
    assert source.last_page_requests == 1


def test_redirect_is_not_followed_and_unavailable_detail_remains_unknown():
    calls = []

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = {"content-type": "text/html"}
            self.content = b""

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["allow_redirects"]))
        if url.endswith("robots.txt"):
            return type("RobotsResponse", (), {
                "status_code": 200,
                "headers": {"content-type": "text/plain"},
                "content": b"User-agent: *\nDisallow: /fr/concours/download/\n",
            })()
        if url.endswith("concours-liste"):
            return type("ListingResponse", (), {
                "status_code": 200,
                "headers": {"content-type": "text/html"},
                "content": _html("listing_fr.html").encode(),
            })()
        return Response(302)

    source = EmploiPublicSource(max_pages=1, max_detail_pages=1, get=fake_get)
    results = asyncio.run(source.search("technicien génie civil", "Morocco"))

    assert len(results) == 1
    assert results[0].raw["public_competition"]["reason_code"] == (
        "detail_page_unavailable"
    )
    assert all(not follow for _url, follow in calls)


def test_competition_evidence_persists_and_is_held_for_owner_review(
    db, config, prefs, profile, monkeypatch,
):
    observed_at = datetime.now(timezone.utc)
    metadata = _parse(observed_at=observed_at)
    deadline = observed_at.replace(microsecond=0) + timedelta(days=2)
    metadata["deadline_at"] = deadline.isoformat()
    metadata["deadline_original"] = deadline.strftime("%d/%m/%Y - %H:%M")
    opportunity = _opportunity(metadata)
    opportunity.closing_at = deadline

    class Connector:
        name = "emploi_public_ma"
        kind = "html"
        source_type = "government"
        last_page_requests = 2

        async def search(self, _query, _location):
            return [opportunity]

    monkeypatch.setattr(
        discovery_workflow,
        "_load_source_connectors",
        lambda *args, **kwargs: [(
            {
                "name": "emploi_public_ma",
                "kind": "emploi_public",
                "applies_to_countries": ["Morocco"],
            },
            Connector(),
        )],
    )
    monkeypatch.setattr(
        "app.workflows.adaptive_plan.build_adaptive_plan",
        lambda *_args, **_kwargs: [{
            "query": "PFE stage génie civil",
            "location": "Morocco",
            "country": "Morocco",
            "lang": "fr",
            "intent": "internship",
            "weight": 1.0,
        }],
    )
    config.discovery = {
        **(config.discovery or {}),
        "verify_vacancy_details": True,
        "max_requests_per_source": 1,
        "company_universe_discovery": False,
        "employer_discovery": False,
        "immigration_discovery": False,
        "opportunity_source_discovery": False,
        "social_signal_discovery": False,
        "website_research": False,
        "parallel_fetch": False,
        "vocab_llm": False,
    }

    report = asyncio.run(discovery_workflow.run_discovery(
        db,
        config,
        prefs,
        profile=profile,
        max_queries_override=1,
        max_search_combinations=1,
    ))
    job = db.execute(select(models.Job)).scalar_one()

    assert job.entity_state == "VACANCY_VERIFIED"
    assert job.entity_reason_code == "official_public_competition_verified_open"
    assert job.entity_evidence_url == DETAIL_URL
    assert job.public_competition["portal_id"] == PORTAL_ID
    assert job.posted_at_source == "emploi_public:date_publication"
    assert job.closing_at is not None
    assert is_verified_individual_vacancy(job)
    assert report.source_reports[0]["public_page_requests"] == 2

    summary = asyncio.run(analyze_job(
        db, job, None, None, None, None, profile, AgentConfig(), [],
    ))

    assert summary["decision"] == "HOLD"
    assert job.public_competition["eligibility_checklist"]["candidate_eligibility"] == (
        "NOT_ASSESSED"
    )
    assert profile.student_status == "unknown"
    assert job.application_method == "UNKNOWN"
    assert _safe_public_competition(job.public_competition)[
        "submission_owner_approval"
    ] == "REQUIRED_NOT_GRANTED"
    assert job.entity_reason_code == "official_public_competition_verified_open"

    db.add(models.Decision(
        job_id=job.id,
        decision="APPLY",
        overall_score=90,
        scores={},
        reason="test gate",
        rules_fired=[],
    ))
    db.flush()
    assert pending_actions(db) == []
    result = asyncio.run(ApplicationEngine(
        db,
        AgentConfig(),
        SimpleNamespace(email_mode="live"),
        profile,
        None,
        verify_url_fn=lambda _job: True,
    ).run(job, db.execute(
        select(models.Decision).where(
            models.Decision.job_id == job.id,
        ).order_by(models.Decision.id.desc()).limit(1),
    ).scalar_one(), "APPLY", "nobody@example.gov.ma"))
    assert result["status"] == "blocked"
    assert result["reason"] == "competition_owner_approval_required"


def test_procedural_notice_updates_matching_competition_without_new_job(db):
    company = models.Company(
        name="Direction Régionale de l'Équipement",
        normalized_name="direction regionale de l'equipement",
    )
    db.add(company)
    db.flush()
    metadata = _parse()
    job = models.Job(
        source="emploi_public_ma",
        external_id=PORTAL_ID,
        dedup_key=f"emploi_public_ma:{PORTAL_ID}",
        title=metadata["title_original"],
        company_id=company.id,
        location=metadata["location_original"],
        country="Morocco",
        description=metadata["description_original"],
        url=DETAIL_URL,
        status="new",
        source_type="government",
        verification_status="verified",
        entity_type="INDIVIDUAL_VACANCY",
        entity_state="VACANCY_VERIFIED",
        entity_reason_code="official_public_competition_verified_open",
        entity_evidence_url=DETAIL_URL,
        public_competition=metadata,
    )
    db.add(job)
    db.flush()
    notice = dict(metadata)
    notice.update({
        "stage": "CANCELLATION",
        "status": "CANCELLATION",
        "verification_status": "unverified",
        "reason_code": "official_cancellation_notice",
    })
    opportunity = _opportunity(notice)

    matched = discovery_workflow._persist_public_competition_notice(
        db, opportunity, notice, "emploi_public_ma",
    )

    assert matched is True
    assert job.status == "closed"
    assert job.entity_state == "VACANCY_CLOSED"
    assert job.entity_reason_code == "official_cancellation_notice"
    assert job.public_competition["latest_procedural_notice"]["stage"] == (
        "CANCELLATION"
    )
    assert db.execute(select(models.Job)).scalars().all() == [job]


def test_inconsistent_submission_route_never_passes_verification():
    metadata = _parse(
        _html("detail_fr.html").replace(
            '<a href="https://candidature.equipement.gov.ma/concours/42">Portail officiel</a>',
            "contact@example.gov.ma",
        ),
    )
    opportunity = _opportunity(metadata)
    entity_type, state, reason = classify_entity(opportunity)

    assert metadata["status"] == "UNKNOWN"
    assert (entity_type, state) == (
        "INDIVIDUAL_VACANCY", "VACANCY_STATUS_UNKNOWN",
    )
    assert reason == "submission_route_conflict"
    assert not is_verified_individual_vacancy(SimpleNamespace(
        source="emploi_public_ma",
        entity_evidence_url=DETAIL_URL,
        entity_type=entity_type,
        entity_state=state,
        entity_reason_code=reason,
        source_type="government",
        verification_status="unverified",
        last_verified_at=NOW,
        closing_at=opportunity.closing_at,
        public_competition=metadata,
        url=DETAIL_URL,
        title=opportunity.title,
        description=opportunity.description,
        location=opportunity.location,
        company=SimpleNamespace(name=opportunity.company),
        application_method="UNKNOWN",
        application_url="",
    ))
