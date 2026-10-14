"""Golden-set tests — the exact decisions the demo fixtures SHOULD produce.

These are the guardrails for score drift: if a future prompt/model change flips
one of these, the test fails loudly before an email goes out.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import models
from app.agents.llm import NullLLM
from app.workflows.action import run_actions
from app.workflows.analysis import run_analysis
from app.workflows.discovery import run_discovery
from tests.conftest import ROOT


def _discover(db, config, prefs):
    asyncio.run(run_discovery(db, config, prefs, sources_path=ROOT / "tests" / "fixtures" / "sources_demo.yaml"))


def _refresh_demo_posting_dates(db):
    """Keep decision golden tests independent of the calendar date they run."""
    posted_at = datetime.now(timezone.utc) - timedelta(days=1)
    for job in db.execute(select(models.Job)).scalars():
        job.posted_at = posted_at
    db.flush()


def _analyze(db, config, prefs, profile):
    llm = NullLLM(profile)
    report = asyncio.run(
        run_analysis(db, config, profile, llm, prefs.countries, preferences=prefs)
    )
    return {db.get(models.Job, d["job_id"]): d for d in report.analyzed}


def _act(db, config, prefs, profile, settings):
    llm = NullLLM(profile)
    asyncio.run(run_actions(db, config, settings, profile, llm))


def test_golden_decisions(db, config, prefs, profile):
    _discover(db, config, prefs)
    _refresh_demo_posting_dates(db)
    by_job = _analyze(db, config, prefs, profile)
    jobs = db.execute(select(models.Job)).scalars().all()

    assert by_job == {}, "unverified listings must not enter eligibility analysis"
    assert jobs
    assert all(job.status == "verification_required" for job in jobs)


def test_dedup_idempotent(db, config, prefs):
    _discover(db, config, prefs)
    first = db.execute(select(models.Job)).scalars().all()
    _discover(db, config, prefs)
    second = db.execute(select(models.Job)).scalars().all()
    assert len(first) == len(second), "re-running discovery must not duplicate jobs"


def test_unverified_listings_never_enter_the_application_action_path(
    db, config, prefs, profile, settings,
):
    _discover(db, config, prefs)
    _refresh_demo_posting_dates(db)
    _analyze(db, config, prefs, profile)
    _act(db, config, prefs, profile, settings)
    apps = db.execute(select(models.Application)).scalars().all()
    assert apps == []
