""""Auto-apply guard" tests: the DecisionAgent must never APPLY below the policy
minimum score/confidence, even if the model says APPLY.
"""
from __future__ import annotations

import pytest

from app.agents.decision_agent import DecisionAgent
from app.config import CandidateProfile, Preferences
from app.models import utcnow


class _FakeLLM:
    """Always says APPLY — the guard must overrule it."""

    name = "fake"

    def __init__(self, decision="APPLY", confidence=0.99):
        self._resp = {"decision": decision, "confidence": confidence, "reason": "model wants to apply"}

    async def complete_json(self, system, payload):
        return self._resp


def _job(db, status="new", country="France"):
    from datetime import datetime, timedelta, timezone

    from app import models
    from app.memory.store import get_or_create_company

    company = get_or_create_company(db, "Colas")
    job = models.Job(
        source="static_files", external_id="dp1", dedup_key="hash:policy1",
        title="Ingénieur VRD", company_id=company.id, location="Lyon",
        country=country, description="road works",
        url="https://boards.greenhouse.io/example/jobs/1",
        posted_at=datetime.now(timezone.utc) - timedelta(days=2),
        contact_email="jobs@colas.example", status=status,
        source_type="ats",
        entity_type="INDIVIDUAL_VACANCY",
        entity_state="VACANCY_VERIFIED",
        entity_reason_code="matching_public_ats_posting_record",
        entity_evidence_url="https://boards.greenhouse.io/example/jobs/1",
        verification_status="verified",
        last_verified_at=utcnow(),
        application_method="COMPANY_PORTAL",
        application_url="https://boards.greenhouse.io/example/jobs/1",
    )
    db.add(job)
    db.flush()
    return job


def _attach_analysis(db, job):
    from app import models

    db.add(models.JobAnalysis(job_id=job.id, summary="road sector", model_used="fake",
                              raw_json={}))
    db.flush()


def _decide(db, job, config, llm, overall, preferences=None, profile=None):
    import asyncio


    # compute the band from the real config thresholds
    dims = {"match": 70.0, "mobility": 70.0, "compensation": 70.0, "stability": 70.0}
    band = config.threshold_for(overall)
    agent = DecisionAgent(llm, config, profile=profile, preferences=preferences)
    _attach_analysis(db, job)
    return asyncio.run(agent.decide(db, job, overall, dims, band))


def test_model_wants_apply_but_score_below_policy(db, config):
    job = _job(db)
    result = _decide(db, job, config, _FakeLLM(), overall=75)
    assert result.decision != "APPLY"
    assert result.decision in ("ASK_EMPLOYER", "INVESTIGATE")
    assert "auto-apply guard" in result.reason


def test_model_wants_apply_score_meets_policy_but_low_confidence_lowband(db, config):
    # score 75 is below policy anyway; confidence here is deterministic from score
    job = _job(db)
    result = _decide(db, job, config, _FakeLLM(confidence=0.2), overall=75)
    assert result.decision != "APPLY"


def test_apply_allowed_at_or_above_policy(db, config):
    job = _job(db, status="analyzed")
    result = _decide(db, job, config, _FakeLLM(), overall=88)
    assert result.decision == "APPLY"


def test_unverified_entity_cannot_be_recommended_for_application(db, config):
    job = _job(db, status="analyzed")
    job.entity_state = "VACANCY_UNVERIFIED"
    result = _decide(db, job, config, _FakeLLM(), overall=95)
    assert result.decision == "HOLD"
    assert "vacancy_not_verified" in result.rules_fired


def test_hard_low_score_rule_ignores_before_model(db, config):
    job = _job(db)
    result = _decide(db, job, config, _FakeLLM(), overall=40)
    assert result.decision == "IGNORE"


@pytest.mark.parametrize("country", ["Morocco", "Maroc"])
def test_domestic_morocco_job_does_not_require_visa_sponsorship(db, config, country):
    job = _job(db, status="analyzed", country=country)
    job.sponsorship_signal = "unknown"
    preferences = Preferences(
        target_countries=["Morocco", "France"],
        sponsorship_required=True,
    )

    result = _decide(
        db, job, config, _FakeLLM(), overall=88, preferences=preferences,
    )

    assert result.decision == "APPLY"


def test_international_job_without_explicit_sponsorship_evidence_is_held(db, config):
    job = _job(db, status="analyzed", country="France")
    job.sponsorship_signal = "unknown"
    preferences = Preferences(
        target_countries=["Morocco", "France"],
        sponsorship_required=True,
    )

    result = _decide(
        db, job, config, _FakeLLM(), overall=88, preferences=preferences,
    )

    assert result.decision == "HOLD"
    assert "international_sponsorship_not_verified" in result.rules_fired
    assert "model wants to apply" not in result.reason


def test_international_job_with_explicit_sponsorship_evidence_is_not_blocked(
    db, config,
):
    job = _job(db, status="analyzed", country="France")
    job.sponsorship_signal = "high"
    preferences = Preferences(
        target_countries=["Morocco", "France"],
        sponsorship_required=True,
    )

    result = _decide(
        db, job, config, _FakeLLM(), overall=88, preferences=preferences,
    )

    assert result.decision == "APPLY"


def test_explicitly_unavailable_sponsorship_overrides_positive_signal(db, config):
    job = _job(db, status="analyzed", country="France")
    job.sponsorship_signal = "high"
    job.description = "Visa sponsorship is not available."
    preferences = Preferences(
        target_countries=["Morocco", "France"],
        sponsorship_required=True,
    )

    result = _decide(
        db, job, config, _FakeLLM(), overall=88, preferences=preferences,
    )

    assert result.decision == "HOLD"
    assert "international_sponsorship_not_verified" in result.rules_fired


def test_student_only_internship_with_unknown_status_is_held(db, config):
    job = _job(db, status="analyzed")
    job.opportunity_type = "INTERNSHIP"
    job.description = "Applicants must be currently enrolled at a university."

    result = _decide(db, job, config, _FakeLLM(), overall=88)

    assert result.decision == "HOLD"
    assert "student_enrollment_unverified" in result.rules_fired


def test_enrollment_language_does_not_gate_noninternship_jobs(db, config):
    job = _job(db, status="analyzed")
    job.opportunity_type = "JOB"
    job.description = "Applicants must be currently enrolled at a university."

    result = _decide(db, job, config, _FakeLLM(), overall=88)

    assert result.decision == "APPLY"
    assert "student_enrollment_unverified" not in result.rules_fired


@pytest.mark.parametrize("description", [
    "Stage réservé aux étudiants.",
    "Bewerber müssen aktuell eingeschrieben sein.",
    "Debe estar matriculado en una universidad.",
    "Applicants must be currently enrolled at a university.",
])
def test_explicit_multilingual_enrollment_requirements_are_held(
    db, config, description,
):
    job = _job(db, status="analyzed")
    job.opportunity_type = "INTERNSHIP"
    job.description = description

    result = _decide(db, job, config, _FakeLLM(), overall=88)

    assert result.decision == "HOLD"
    assert "student_enrollment_unverified" in result.rules_fired


def test_student_only_internship_accepts_confirmed_current_student(db, config):
    job = _job(db, status="analyzed")
    job.opportunity_type = "INTERNSHIP"
    job.description = "Applicants must be currently enrolled at a university."
    profile = CandidateProfile(name="Test Candidate", student_status="current_student")

    result = _decide(
        db, job, config, _FakeLLM(), overall=88, profile=profile,
    )

    assert result.decision == "APPLY"
    assert not any(rule.startswith("student_enrollment_") for rule in result.rules_fired)


def test_student_only_internship_excludes_known_nonstudent(db, config):
    job = _job(db, status="analyzed")
    job.opportunity_type = "INTERNSHIP"
    job.description = "Students only; applicants must be currently enrolled."
    profile = CandidateProfile(name="Test Candidate", student_status="not_student")

    result = _decide(
        db, job, config, _FakeLLM(), overall=88, profile=profile,
    )

    assert result.decision == "IGNORE"
    assert "student_enrollment_required" in result.rules_fired


def test_internship_open_to_recent_graduates_needs_no_student_status(db, config):
    job = _job(db, status="analyzed")
    job.opportunity_type = "INTERNSHIP"
    job.description = "Internship open to current students and recent graduates."

    result = _decide(db, job, config, _FakeLLM(), overall=88)

    assert result.decision == "APPLY"
    assert not any(rule.startswith("student_enrollment_") for rule in result.rules_fired)


@pytest.mark.parametrize("score,min_score,min_conf,expected", [
    (79, 80, 0.80, "not apply"),
    (80, 80, 0.80, "apply"),
])
def test_policy_threshold_boundary(db, config, score, min_score, min_conf, expected):
    config.rules["min_application_score"] = min_score
    config.rules["min_application_confidence"] = min_conf
    job = _job(db, status="analyzed")
    result = _decide(db, job, config, _FakeLLM(), overall=float(score))
    if expected == "apply":
        assert result.decision == "APPLY"
    else:
        assert result.decision != "APPLY"
