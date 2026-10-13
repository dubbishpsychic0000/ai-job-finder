from types import SimpleNamespace

from app.workflows import btp_outreach, pipeline
from app.workflows.pipeline import _run_company_outreach


def test_company_outreach_is_skipped_outside_enabled_gmail_draft_mode(
    db, config, settings, profile
):
    settings = settings.model_copy(update={
        "email_mode": "live", "enable_email": True, "email_provider": "gmail",
    })

    result = _run_company_outreach(db, config, settings, profile, paused=False)

    assert result == {
        "status": "skipped",
        "reason": "BTP outreach requires enabled Gmail draft mode",
    }


def test_company_outreach_uses_btp_pipeline_when_draft_mode_is_enabled(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    report = {"workflow": "btp_outreach", "action": {"drafts": 0}}
    calls = []
    monkeypatch.setattr(
        btp_outreach,
        "run_btp_outreach",
        lambda *args: (
            calls.append(args) or SimpleNamespace(as_run_report=lambda: report)
        ),
    )

    result = _run_company_outreach(db, config, settings, profile, paused=False)

    assert result == report
    assert calls == [(db, config, settings, profile)]


def test_company_outreach_is_skipped_while_paused(db, config, settings, profile):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })

    result = _run_company_outreach(db, config, settings, profile, paused=True)

    assert result["status"] == "skipped"
    assert "paused" in result["reason"]


def test_zero_job_discovery_does_not_skip_independent_company_research(
    db, config, settings, profile, monkeypatch
):
    settings = settings.model_copy(update={
        "email_mode": "draft", "enable_email": True, "email_provider": "gmail",
    })
    discovery = SimpleNamespace(
        new_jobs=0, duplicates=4, opportunities_fetched=4, source_errors=[],
        employers_discovered=0, companies_discovered=0, company_universe={},
        immigration_facts=0, opportunity_sources=0, social_signals=0,
        source_reports=[], alerts=["all fetched jobs were already known"],
    )
    company_report = {
        "status": "researched",
        "research": {"companies_processed": 1, "outreach_ready": 0},
    }
    calls = []

    async def run_discovery(*_args, **_kwargs):
        return discovery

    async def run_analysis(*_args, **_kwargs):
        return SimpleNamespace(analyzed=[], errors=[])

    async def run_actions(*_args, **_kwargs):
        return SimpleNamespace(
            applied=[], asked=[], investigated=[], blocked=0, errors=[],
        )

    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "get_config", lambda: config)
    monkeypatch.setattr(pipeline, "get_preferences", lambda: SimpleNamespace(countries=[]))
    monkeypatch.setattr(pipeline, "get_profile", lambda: profile)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "get_llm", lambda *_args: None)
    monkeypatch.setattr(pipeline, "run_discovery", run_discovery)
    monkeypatch.setattr(pipeline, "run_analysis", run_analysis)
    monkeypatch.setattr(pipeline, "run_actions", run_actions)
    monkeypatch.setattr(
        pipeline,
        "_run_company_outreach",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or company_report),
    )
    monkeypatch.setattr(pipeline, "_is_paused", lambda: False)
    monkeypatch.setattr(
        "app.notifications.whatsapp.MetaWhatsAppProvider",
        lambda: SimpleNamespace(configured=False),
    )

    result = pipeline.run_pipeline(
        db, do_followups=False, with_immigration=False,
    )

    assert result.discovery["new_jobs"] == 0
    assert result.analysis["analyzed"] == 0
    assert result.company_outreach == company_report
    assert len(calls) == 1
