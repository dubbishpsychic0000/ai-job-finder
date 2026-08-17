"""Golden-set tests — the exact decisions the demo fixtures SHOULD produce.

These are the guardrails for score drift: if a future prompt/model change flips
one of these, the test fails loudly before an email goes out.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app import models
from app.agents.llm import NullLLM
from app.workflows.action import run_actions
from app.workflows.analysis import run_analysis
from app.workflows.discovery import run_discovery
from tests.conftest import ROOT


def _discover(db, config, prefs):
    asyncio.run(run_discovery(db, config, prefs, sources_path=ROOT / "tests" / "fixtures" / "sources_demo.yaml"))


def _analyze(db, config, prefs, profile):
    llm = NullLLM(profile)
    report = asyncio.run(run_analysis(db, config, profile, llm, prefs.countries))
    return {db.get(models.Job, d["job_id"]): d for d in report.analyzed}


def _act(db, config, prefs, profile, settings):
    llm = NullLLM(profile)
    asyncio.run(run_actions(db, config, settings, profile, llm))


def test_golden_decisions(db, config, prefs, profile):
    _discover(db, config, prefs)
    by_job = _analyze(db, config, prefs, profile)

    def dec(title, company=""):
        for job, d in by_job.items():
            if job.title == title and (not company or (job.company and job.company.name == company)):
                return d["decision"]
        raise AssertionError(f"job not found: {title} / {company}")

    assert dec("Technicien Génie Civil", "Bouygues Construction") == "APPLY", "sponsored FR role should APPLY"
    assert dec("Civil Engineering Technician", "EllisDon") == "APPLY", "LMIA-sponsored CA role should APPLY"
    assert dec("Técnico de Obras Civiles") == "IGNORE", "es-only role should IGNORE"
    assert dec("Técnico de Engenharia Civil") == "IGNORE", "pt-only role should IGNORE"
    assert dec("Technicien VRD", "Colas") in ("ASK_EMPLOYER", "APPLY", "INVESTIGATE")
    # the senior (12y) VRD posting must be blocked by the experience hard rule
    assert dec("Technicien VRD", "Eiffage") == "IGNORE"


def test_dedup_idempotent(db, config, prefs):
    _discover(db, config, prefs)
    first = db.execute(select(models.Job)).scalars().all()
    _discover(db, config, prefs)
    second = db.execute(select(models.Job)).scalars().all()
    assert len(first) == len(second), "re-running discovery must not duplicate jobs"


def test_applications_recorded_but_blocked_without_email(db, config, prefs, profile, settings):
    _discover(db, config, prefs)
    _analyze(db, config, prefs, profile)
    _act(db, config, prefs, profile, settings)
    apps = db.execute(select(models.Application)).scalars().all()
    assert apps, "decided APPLY/ASK must create application records"
    assert all(a.status == "blocked" for a in apps), "no email -> no send, only blocked records"
