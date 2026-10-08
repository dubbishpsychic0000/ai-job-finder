from types import SimpleNamespace

from app.workflows import btp_outreach
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
