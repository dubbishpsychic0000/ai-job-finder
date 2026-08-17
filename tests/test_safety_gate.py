"""Email Safety Gate — the checks that stand between the LLM and the outside world."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import models
from app.email.safety_gate import validate
from app.memory.store import get_or_create_company, upsert_contact


def _job(db, title="Technicien VRD", email="hr@colas.example", posted_days_ago=2):
    company = get_or_create_company(db, "Colas")
    job = models.Job(
        source="static_files", external_id="t1", dedup_key="hash:test1", title=title,
        company_id=company.id, location="Lyon, France", country="France",
        description="road construction VRD", url="https://example.com/j",
        posted_at=datetime.now(timezone.utc) - timedelta(days=posted_days_ago),
        contact_email=email, status="analyzed",
    )
    db.add(job)
    db.flush()
    return job


@pytest.fixture()
def cv_file(tmp_path):
    p = tmp_path / "cv.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    return str(p)


def _run(db, profile, config, body, cv_file, to="hr@colas.example", daily=0,
         check_cooldown=True, stage_fn=None):
    job = stage_fn(db) if stage_fn else _job(db)
    return validate(to_addr=to, subject="Application", body=body, attachments=[cv_file],
                    job=job, profile=profile, config=config, session=db,
                    daily_sent=daily, check_cooldown=check_cooldown)


def test_honest_body_passes_claims(db, profile, config, cv_file):
    body = ("I am applying for the Technicien VRD position. I have 4 years of experience "
            "in civil engineering, road construction, VRD and AutoCAD.")
    report = _run(db, profile, config, body, cv_file)
    assert report.checks.get("claims") is not False


def test_inflated_experience_blocked(db, profile, config, cv_file):
    body = "I have 10 years of experience in civil engineering."
    report = _run(db, profile, config, body, cv_file)
    assert not report.allowed
    assert any("years experience" in r for r in report.reasons)


def test_invented_employer_blocked(db, profile, config, cv_file):
    body = "I previously worked at SpaceX as a launch director."
    report = _run(db, profile, config, body, cv_file)
    assert not report.allowed
    assert any("invented" in r for r in report.reasons)


def test_invented_flag_skill_blocked(db, profile, config, cv_file):
    body = "I am a certified nuclear engineer (PE) with 4 years of experience."
    report = _run(db, profile, config, body, cv_file)
    assert not report.allowed
    assert any(x for x in report.reasons if "nuclear engineer" in x)


def test_language_inflation_blocked(db, profile, config, cv_file):
    body = "I possess native French fluency, which is essential for your role."
    report = _run(db, profile, config, body, cv_file)
    assert not report.allowed
    assert any("french" in r for r in report.reasons)


def test_invalid_recipient_blocked(db, profile, config, cv_file):
    body = "I have 4 years of experience in civil engineering."
    report = _run(db, profile, config, body, cv_file, to="not-an-email")
    assert not report.allowed
    assert any("recipient" in r for r in report.reasons)


def test_cooldown_contact_blocked(db, profile, config, cv_file):
    body = "I have 4 years of experience in civil engineering."
    job = _job(db)
    upsert_contact(db, job.contact_email)  # records last_contacted_at = now
    report = validate(to_addr=job.contact_email, subject="Application", body=body,
                      attachments=[cv_file], job=job, profile=profile,
                      config=config, session=db, daily_sent=0, check_cooldown=True)
    assert not report.allowed
    assert any("already contacted" in r for r in report.reasons)


def test_followup_skips_cooldown_but_caps_calls(db, profile, config, cv_file):
    """Follow-ups intentionally reuse the same address; the gate must allow
    that when explicitly requested while still running every other check."""
    body = "I have 4 years of experience in civil engineering."
    job = _job(db)
    upsert_contact(db, job.contact_email)
    report = validate(to_addr=job.contact_email, subject="Re: Application", body=body,
                      attachments=[cv_file], job=job, profile=profile,
                      config=config, session=db, daily_sent=0, check_cooldown=False)
    assert report.allowed


def test_daily_rate_limit_blocked(db, profile, config, cv_file):
    body = "I have 4 years of experience in civil engineering."
    limit = int(config.email.get("max_daily_outbound", 10))
    report = _run(db, profile, config, body, cv_file, daily=limit)
    assert not report.allowed
    assert any("daily outbound" in r for r in report.reasons)


def test_stale_posting_blocked(db, profile, config, cv_file):
    body = "I have 4 years of experience in civil engineering."

    def stale(db):
        return _job(db, posted_days_ago=999)

    report = _run(db, profile, config, body, cv_file, stage_fn=stale)
    assert not report.allowed
    assert any("days old" in r for r in report.reasons)


def test_employer_cooldown_blocked(db, profile, config, cv_file):
    """Any contact with the same company within the cooldown window blocks."""
    body = "I have 4 years of experience in civil engineering."
    job = _job(db)
    contact = models.Contact(email="someone@colas.example", company_id=job.company_id)
    contact.last_contacted_at = datetime.now(timezone.utc)
    db.add(contact)
    db.flush()
    report = validate(to_addr=job.contact_email, subject="Application", body=body,
                      attachments=[cv_file], job=job, profile=profile, config=config,
                      session=db, daily_sent=0, employer_cooldown_days=7)
    assert not report.allowed
    assert any("employer" in r for r in report.reasons)


def test_daily_application_limit_blocked(db, profile, config, cv_file):
    body = "I have 4 years of experience in civil engineering."
    limit = int(config.rules.get("max_daily_applications", 5))
    report = validate(to_addr="hr@colas.example", subject="Application", body=body,
                      attachments=[cv_file], job=_job(db), profile=profile, config=config,
                      session=db, daily_total=0, daily_applications=limit, action="APPLY")
    assert not report.allowed
    assert any("application limit" in r for r in report.reasons)


def test_daily_inquiry_limit_blocked(db, profile, config, cv_file):
    body = "I have 4 years of experience in civil engineering."
    limit = int(config.rules.get("max_daily_inquiries", 5))
    report = validate(to_addr="hr@colas.example", subject="Question", body=body,
                      attachments=[cv_file], job=_job(db), profile=profile, config=config,
                      session=db, daily_total=0, daily_inquiries=limit, action="ASK_EMPLOYER")
    assert not report.allowed
    assert any("inquiry limit" in r for r in report.reasons)
