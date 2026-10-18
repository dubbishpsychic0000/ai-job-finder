"""Worldwide Discovery V3: safe contacts, routes, and non-spam notifications."""
from __future__ import annotations

from app.connectors.base import Opportunity
from app.discovery.email_verification import EmailVerificationService, is_safe_email
from app.discovery.opportunity_details import (
    classify_entity,
    classify_opportunity,
    detect_application_method,
)
from app.memory.store import (
    enqueue_notification,
    get_or_create_company,
    opportunity_id,
)
from app.models import Decision, Job, utcnow
from app.notifications.service import NotificationService
from app.workflows.action import pending_actions


def test_email_verification_rejects_placeholders_and_only_trusts_evidence(db):
    service = EmailVerificationService(db)
    assert not is_safe_email("hr@example.com")
    assert not service.verify("jobs@company.com", source_url="https://board.example/job",
                              source_type="job_board").verified
    verified = service.verify("recruitment@company.fr", source_url="https://company.fr/jobs/1",
                              source_type="ats", employer_domain="company.fr")
    assert verified.verified
    assert verified.verification_method == "official_employer_posting"


def test_details_and_digest_are_queue_backed(db):
    company = get_or_create_company(db, "Acme Construction", "https://acme.example", "Morocco")
    job = Job(source="company_careers", external_id="1", dedup_key="v3-1", title="Technicien VRD",
              company_id=company.id, country="Morocco", location="Casablanca",
              description="Apply online for this civil works role.", url="https://acme.example/jobs/1",
              application_method="ONLINE_FORM", application_url="https://acme.example/jobs/1")
    db.add(job)
    db.flush()
    enqueue_notification(db, "JOB_FOUND", job_id=job.id, payload={"score": 89})
    details = NotificationService(db).details(opportunity_id(job))
    assert details and details["application_method"] == "ONLINE_FORM"
    digest = NotificationService(db).digest(force=True)
    assert digest and "Technicien VRD" in digest


def test_type_and_route_detection_do_not_make_up_email_routes():
    assert classify_opportunity("Stage Technicien VRD") == "INTERNSHIP"
    assert classify_opportunity("Civil engineering internship") == "INTERNSHIP"
    assert classify_opportunity("PFE génie civil") == "INTERNSHIP"
    assert classify_opportunity("Graduate program for recent graduates") == "GRADUATE_PROGRAM"
    method, url = detect_application_method(text="Apply online today", url="https://example.test/app")
    assert (method, url) == ("ONLINE_FORM", "https://example.test/app")
    assert detect_application_method(text="A great job", has_verified_email=False)[0] == "UNKNOWN"
    address = "recruitment@company.fr"
    assert detect_application_method(
        text=f"Send your CV and application to {address}.",
        source_type="ats",
        has_verified_email=True,
        contact_email=address,
    ) == ("EMAIL", "")
    assert detect_application_method(
        text=f"Contact {address} with questions. Apply online.",
        url="https://example.test/app",
        source_type="ats",
        has_verified_email=True,
        contact_email=address,
    ) == ("COMPANY_PORTAL", "https://example.test/app")
    assert detect_application_method(
        text=f"Apply by email to {address}.",
        source_type="ats",
        has_verified_email=True,
        contact_email="other@company.fr",
    )[0] == "COMPANY_PORTAL"


def test_entity_classification_distinguishes_vacancies_from_research_pages():
    detail = Opportunity(
        source="tavily_web_search",
        source_type="search_engine",
        external_id="https://jobs.example.test/jobs/technician-123",
        title="Civil Engineering Technician",
        company="",
        location="Casablanca, Morocco",
        url="https://jobs.example.test/jobs/technician-123",
        raw={"query": "civil engineering technician"},
    )
    assert classify_entity(detail) == (
        "INDIVIDUAL_VACANCY",
        "VACANCY_UNVERIFIED",
        "specific_listing_url_unverified",
    )

    salary_page = Opportunity(
        source="tavily_web_search",
        source_type="search_engine",
        external_id="https://reference.example.test/civil-engineer-salary",
        title="Civil Engineer Salary Guide",
        url="https://reference.example.test/civil-engineer-salary",
    )
    assert classify_entity(salary_page) == (
        "SALARY_INFORMATION",
        "REJECTED_NON_VACANCY",
        "salary_reference",
    )

    forum = Opportunity(
        source="tavily_web_search",
        source_type="search_engine",
        external_id="https://forum.example.test/civil-engineering",
        title="Civil engineering technician discussion forum",
        url="https://forum.example.test/civil-engineering",
    )
    assert classify_entity(forum) == (
        "FORUM_DISCUSSION",
        "REJECTED_NON_VACANCY",
        "forum_or_discussion",
    )

    category = Opportunity(
        source="job_board",
        source_type="job_board",
        external_id="https://jobs.example.test/jobs",
        title="Civil engineering jobs in Morocco",
        url="https://jobs.example.test/jobs",
    )
    assert classify_entity(category) == (
        "JOB_BOARD_CATEGORY",
        "RESEARCH_REQUIRED",
        "job_category_page",
    )

    official_record = Opportunity(
        source="greenhouse:example",
        source_type="ats",
        external_id="greenhouse:example:123",
        title="Civil Engineering Technician",
        company="Example Construction",
        location="Casablanca, Morocco",
        url="https://boards.greenhouse.io/example/jobs/123",
        raw={"ats": "greenhouse"},
    )
    assert classify_entity(official_record) == (
        "INDIVIDUAL_VACANCY",
        "VACANCY_UNVERIFIED",
        "ats_detail_requires_live_evidence",
    )

    official_record.company = "example"
    assert classify_entity(official_record)[1] == "VACANCY_UNVERIFIED"


def test_classification_handles_run_38664200891_false_positive_shapes():
    from urllib.parse import urlsplit

    examples = [
        (
            "Free Job Post Civil and Environmental Engineering in Morocco",
            "https://www.academicjobs.com/en-ma/free-job-post",
            ("JOB_BOARD_CATEGORY", "RESEARCH_REQUIRED", "job_board_landing_page"),
        ),
        (
            "Civil Engineering Technician Job Description",
            "https://skillpointe.com/careers/construction/civil-engineering-technician",
            ("OCCUPATIONAL_REFERENCE", "REJECTED_NON_VACANCY", "occupational_reference"),
        ),
        (
            "Civil/Family Case Look-up",
            "https://civilinquiry.jud.ct.gov/",
            ("UNKNOWN", "REJECTED_NON_VACANCY", "non_employment_information"),
        ),
        (
            "Real Civil Engineer",
            "https://www.youtube.com/@RealCivilEngineerGaming",
            ("FORUM_DISCUSSION", "REJECTED_NON_VACANCY", "discussion_or_social_page"),
        ),
        (
            "Civil Infrastructure Platform - Industrial Grade Linux",
            "https://cip-project.org/",
            ("UNKNOWN", "REJECTED_NON_VACANCY", "non_employment_information"),
        ),
        (
            "Job Vacancy: Apprentice Civil Engineering Technician",
            "https://teacakeclub.org/2026/08/19/job-vacancy-apprentice-civil-engineering-technician",
            ("UNKNOWN", "RESEARCH_REQUIRED", "unsupported_vacancy_source"),
        ),
        (
            "Civil Engineer Job Description - Apply Now",
            "https://jobs.example.test/jobs/civil-engineer-123",
            ("INDIVIDUAL_VACANCY", "VACANCY_UNVERIFIED", "specific_listing_url_unverified"),
        ),
    ]
    for title, url, expected in examples:
        opportunity = Opportunity(
            source="tavily_web_search",
            source_type="search_engine",
            external_id=urlsplit(url).path or url,
            title=title,
            url=url,
        )
        assert classify_entity(opportunity) == expected
        assert opportunity.posted_at is None


def test_unverified_entities_are_excluded_from_pending_actions(db):
    unverified = Job(
        source="tavily",
        external_id="listing",
        dedup_key="unverified-listing",
        title="Civil Engineer",
        url="https://jobs.example.test/jobs/123",
        status="analyzed",
    )
    verified = Job(
        source="greenhouse",
        external_id="official",
        dedup_key="verified-listing",
        title="Civil Engineer",
        url="https://boards.greenhouse.io/example/jobs/123",
        status="analyzed",
        source_type="ats",
        company_id=get_or_create_company(db, "Example Construction").id,
        location="Casablanca, Morocco",
        description="Design and supervise civil engineering projects.",
        verification_status="verified",
        application_method="COMPANY_PORTAL",
        application_url="https://boards.greenhouse.io/example/jobs/123",
        entity_type="INDIVIDUAL_VACANCY",
        entity_state="VACANCY_VERIFIED",
        entity_reason_code="matching_public_ats_posting_record",
        entity_evidence_url="https://boards.greenhouse.io/example/jobs/123",
        last_verified_at=utcnow(),
    )
    db.add_all([unverified, verified])
    db.flush()
    db.add_all([
        Decision(
            job_id=job.id, decision="APPLY", overall_score=90,
            scores={}, reason="test", rules_fired=[], ai_reason="",
        )
        for job in (unverified, verified)
    ])
    db.flush()

    assert pending_actions(db) == [verified]


def test_verified_email_application_route_is_eligible_for_pending_actions(db):
    company = get_or_create_company(db, "Example Construction").id
    email_job = Job(
        source="greenhouse", external_id="email", dedup_key="email-route",
        title="Civil Engineer", url="https://boards.greenhouse.io/example/jobs/456",
        status="analyzed", source_type="ats", company_id=company,
        location="Casablanca, Morocco",
        description="Send your application and CV to jobs@example.org.",
        contact_email="jobs@example.org", verification_status="verified",
        application_method="EMAIL",
        application_url="https://boards.greenhouse.io/example/jobs/456",
        entity_type="INDIVIDUAL_VACANCY", entity_state="VACANCY_VERIFIED",
        entity_reason_code="matching_public_ats_posting_record",
        entity_evidence_url="https://boards.greenhouse.io/example/jobs/456",
        last_verified_at=utcnow(),
    )
    db.add(email_job)
    db.flush()
    db.add(Decision(
        job_id=email_job.id, decision="APPLY", overall_score=90,
        scores={}, reason="test", rules_fired=[], ai_reason="",
    ))
    db.flush()

    assert pending_actions(db) == [email_job]


def test_daily_limit_draft_is_an_immediate_notification(db):
    company = get_or_create_company(db, "Daily Cap Co", "https://daily.example", "Morocco")
    job = Job(source="company_careers", external_id="cap", dedup_key="v3-cap", title="Technicien BTP",
              company_id=company.id, country="Morocco", url="https://daily.example/jobs/1")
    db.add(job)
    db.flush()
    row = enqueue_notification(db, "EMAIL_DRAFT_CREATED", job_id=job.id, priority="high",
                               payload={"daily_limit_reached": True})
    messages = NotificationService(db).immediate()
    assert row.status == "delivered"
    assert messages and "DAILY EMAIL LIMIT REACHED" in messages[0]
