"""Worldwide Discovery V3: safe contacts, routes, and non-spam notifications."""
from __future__ import annotations

from app.discovery.email_verification import EmailVerificationService, is_safe_email
from app.discovery.opportunity_details import (
    classify_opportunity,
    detect_application_method,
)
from app.memory.store import (
    enqueue_notification,
    get_or_create_company,
    opportunity_id,
)
from app.models import Job
from app.notifications.service import NotificationService


def test_email_verification_rejects_placeholders_and_only_trusts_evidence(db):
    service = EmailVerificationService(db)
    assert not is_safe_email("hr@example.com")
    assert not service.verify("jobs@company.com", source_url="https://board.example/job",
                              source_type="job_board").verified
    verified = service.verify("recruitment@company.fr", source_url="https://company.fr/jobs/1",
                              source_type="ats")
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
    method, url = detect_application_method(text="Apply online today", url="https://example.test/app")
    assert (method, url) == ("ONLINE_FORM", "https://example.test/app")
    assert detect_application_method(text="A great job", has_verified_email=False)[0] == "UNKNOWN"


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
