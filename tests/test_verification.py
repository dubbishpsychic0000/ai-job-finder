"""Opportunity freshness + verification tests — Phase 5 (spec §19, §20).

Hermetic: the verifier's network call is injectable; the §20 before-email
re-verification is tested with a fake checker (no live web access).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import models
from app.agents.communication_agent import CommunicationAgent
from app.discovery.verification import (
    OpportunityVerifier,
    VerificationResult,
    _LiveCheckFailed,
    freshness_label,
    freshness_score,
)
from app.email.service import ApplicationEngine
from app.memory.store import get_or_create_company

NOW = datetime(2026, 2, 10, tzinfo=timezone.utc)


# ---- freshness (§20) ----------------------------------------------------------


def test_freshness_bands():
    cases = [
        (0, "very_high"), (3, "very_high"), (4, "high"), (7, "high"),
        (8, "medium"), (14, "medium"), (15, "low"), (30, "low"), (31, "stale"),
        (45, "stale"), (None, "unknown"),
    ]
    for days, label in cases:
        posted = NOW - timedelta(days=days) if days is not None else None
        assert freshness_label(posted, NOW) == label, f"{days} -> {label}"


def test_freshness_score_ordering():
    assert freshness_score(NOW - timedelta(days=1), NOW) > freshness_score(NOW - timedelta(days=40), NOW)
    assert freshness_score(None) == 50


# ---- verifier (§19) ------------------------------------------------------------


def test_verifier_live_page():
    v = OpportunityVerifier(fetch_html=lambda url: "<html><body>job listing</body></html>")
    r = v.verify("https://employer.example/jobs/1")
    assert isinstance(r, VerificationResult)
    assert r.ok and r.live


def test_verifier_empty_page_is_not_verified():
    v = OpportunityVerifier(fetch_html=lambda url: "   ")
    assert v.verify("https://employer.example/jobs/1").ok is False


def test_verifier_transport_failure_is_not_verified():
    def _boom(url):
        raise _LiveCheckFailed("status 404")
    assert OpportunityVerifier(fetch_html=_boom).verify("https://x.example/j").ok is False


def test_verifier_generic_exception_is_not_verified():
    def _boom(url):
        raise TimeoutError("slow")
    assert OpportunityVerifier(fetch_html=_boom).verify("https://x.example/j").ok is False


# ---- verify-before-email (§20) --------------------------------------------------


def _job(db, status="analyzed"):
    company = get_or_create_company(db, "Colas")
    job = models.Job(
        source="static_files", external_id="v5", dedup_key="hash:verify5", title="Technicien VRD",
        company_id=company.id, location="Lyon, France", country="France",
        description="road construction", url="https://example.com/j", status=status,
        contact_email="jobs@colas.example",
        posted_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db.add(job)
    db.flush()
    return job


def _decision(db, job):
    d = models.Decision(job_id=job.id, decision="APPLY", overall_score=88.0,
                        scores={}, reason="test", rules_fired=[], ai_reason="")
    db.add(d)
    db.flush()
    return d


def _engine(db, config, profile, settings, verify_fn):
    from app.agents.llm import NullLLM

    communicator = CommunicationAgent(NullLLM(profile), profile)
    settings = settings.model_copy(update={"enable_email": False, "email_mode": "dry_run"})
    return ApplicationEngine(db, config, settings, profile, communicator, verify_url_fn=verify_fn)


def test_email_blocked_when_posting_not_live(db, config, profile, settings):
    job = _job(db)
    decision = _decision(db, job)
    result = asyncio.run(_engine(db, config, profile, settings, lambda url: False).run(
        job, decision, "APPLY", job.contact_email, "fr"))
    assert result["status"] == "blocked"
    assert result["reason"] == "posting not live"
    assert job.verification_status == "unverified"
    events = db.execute(select(models.Event)).scalars().all()
    assert any("posting no longer live" in e.message for e in events)


def test_email_proceeds_when_posting_live(db, config, profile, settings):
    job = _job(db)
    decision = _decision(db, job)
    result = asyncio.run(_engine(db, config, profile, settings, lambda url: True).run(
        job, decision, "APPLY", job.contact_email, "fr"))
    assert result["status"] == "dry_run"
    assert job.verification_status == "verified"
    assert job.last_verified_at is not None
