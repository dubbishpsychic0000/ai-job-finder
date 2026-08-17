""""Auto-apply guard" tests: the DecisionAgent must never APPLY below the policy
minimum score/confidence, even if the model says APPLY.
"""
from __future__ import annotations

import pytest

from app.agents.decision_agent import DecisionAgent


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
        posted_at=datetime.now(timezone.utc) - timedelta(days=2),
        contact_email="jobs@colas.example", status=status,
    )
    db.add(job)
    db.flush()
    return job


def _attach_analysis(db, job):
    from app import models

    db.add(models.JobAnalysis(job_id=job.id, summary="road sector", model_used="fake",
                              raw_json={}))
    db.flush()


def _decide(db, job, config, llm, overall):
    import asyncio


    # compute the band from the real config thresholds
    dims = {"match": 70.0, "mobility": 70.0, "compensation": 70.0, "stability": 70.0}
    band = config.threshold_for(overall)
    agent = DecisionAgent(llm, config)
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


def test_hard_low_score_rule_ignores_before_model(db, config):
    job = _job(db)
    result = _decide(db, job, config, _FakeLLM(), overall=40)
    assert result.decision == "IGNORE"


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
