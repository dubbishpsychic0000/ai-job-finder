from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import memory as mem
from app import models
from app.cli import cmd_send_approved
from app.config import Preferences, RunnerSettings
from app.email import gmail_oauth, provider
from app.email.application_queue import (
    BLOCKED,
    NEEDS_REVIEW,
    PORTAL_APPLICATION,
    READY_TO_SEND,
    classify_application,
)
from app.email.service import ApplicationEngine, resolve_attachment
from app.memory.store import add_application, get_or_create_company


def _job(db, *, email="jobs@colas.fr", method="EMAIL"):
    company = get_or_create_company(db, "Colas")
    next_id = (db.query(models.Job).count() or 0) + 1
    job = models.Job(
        source="greenhouse",
        source_type="ats",
        external_id=f"queue-{next_id}",
        dedup_key=f"queue:{next_id}",
        title="Technicien VRD",
        company_id=company.id,
        location="Lyon, France",
        country="France",
        description=(
            f"Road construction VRD. Send your application and CV to {email}."
        ),
        url="https://boards.greenhouse.io/colas/jobs/12345",
        application_url="https://boards.greenhouse.io/colas/jobs/12345",
        contact_email=email or "",
        posted_at=datetime.now(timezone.utc) - timedelta(days=2),
        status="analyzed",
        entity_type="INDIVIDUAL_VACANCY",
        entity_state="VACANCY_VERIFIED",
        entity_reason_code="matching_public_ats_posting_record",
        entity_evidence_url="https://boards.greenhouse.io/colas/jobs/12345",
        verification_status="verified",
        last_verified_at=datetime.now(timezone.utc),
        application_method=method,
    )
    db.add(job)
    db.flush()
    db.add(models.JobAnalysis(
        job_id=job.id,
        skills_required=["VRD"],
        raw_json={},
    ))
    decision = models.Decision(
        job_id=job.id,
        decision="APPLY",
        overall_score=90,
        scores={},
        reason="fit",
        rules_fired=[],
        ai_reason="",
    )
    db.add(decision)
    if email:
        mem.store.add_email_verification(
            db,
            job_id=job.id,
            email=email,
            source_url=job.url,
            source_domain="boards.greenhouse.io",
            verified=True,
            verification_method="official_employer_posting",
            confidence=100,
        )
    db.flush()
    return job


def _settings(settings, **updates):
    return settings.model_copy(update={
        "email_mode": "live",
        "enable_email": True,
        "email_provider": "gmail",
        "employer_cooldown_days": 7,
        **updates,
    })


def _evidence(job, **updates):
    values = {
        "outcome": "OPEN",
        "reason_code": "matching_public_ats_posting_record",
        "evidence_url": job.url,
        "observed_at": datetime.now(timezone.utc),
        "title": job.title,
        "employer": job.company.name,
        "location": job.location,
        "description": job.description,
        "application_url": job.application_url,
        "application_method": "EMAIL",
        "application_email": job.contact_email,
        "closing_at": None,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _configure_ready(monkeypatch, tmp_path):
    token = tmp_path / "gmail_token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail_oauth, "token_path", lambda _settings: token)
    monkeypatch.setattr(
        gmail_oauth,
        "granted_scopes",
        lambda _settings: {
            gmail_oauth.GMAIL_SEND_SCOPE,
            gmail_oauth.GMAIL_COMPOSE_SCOPE,
            gmail_oauth.GMAIL_READONLY_SCOPE,
        },
    )
    cv = tmp_path / "CV_FR.pdf"
    cv.write_bytes(b"%PDF-1.7\nfixture")
    monkeypatch.setattr(
        "app.email.service.resolve_attachment",
        lambda *_args: str(cv),
    )
    monkeypatch.setattr("app.email.application_queue.is_paused", lambda: False)
    return cv


def _ready_item(db, job, config, profile, settings, monkeypatch, tmp_path):
    _configure_ready(monkeypatch, tmp_path)
    return classify_application(
        db,
        job,
        config=config,
        settings=settings,
        profile=profile,
        preferences=Preferences(sponsorship_required=False),
        live_evidence=_evidence(job),
    )


def test_automatic_application_setting_defaults_off(monkeypatch):
    monkeypatch.delenv("AUTO_SEND_VERIFIED_APPLICATIONS", raising=False)
    assert RunnerSettings(_env_file=None).auto_send_verified_applications is False


def test_queue_requires_fresh_verified_vacancy_route_recipient_and_all_gates(
    db, config, profile, settings, monkeypatch, tmp_path,
):
    job = _job(db)
    settings = _settings(settings)
    row = _ready_item(
        db, job, config, profile, settings, monkeypatch, tmp_path,
    )

    assert row["category"] == READY_TO_SEND
    assert row["recipient"] == "jobs@colas.fr"
    assert row["recipient_evidence"]["source_url"] == job.url
    assert row["application_route"] == {
        "type": "EMAIL",
        "url": job.url,
        "verified": True,
    }


def test_queue_never_counts_company_portal_as_email_application(
    db, config, profile, settings, monkeypatch, tmp_path,
):
    job = _job(db, method="COMPANY_PORTAL")
    item = _ready_item(
        db, job, config, profile, _settings(settings), monkeypatch, tmp_path,
    )

    assert item["category"] == PORTAL_APPLICATION, item
    assert item["reason"] == "official_application_requires_portal"


def test_queue_requires_confirmed_nationality_and_qualification_facts(
    db, config, profile, settings, monkeypatch, tmp_path,
):
    job = _job(db)
    job.description += " Moroccan nationality and a Technicien Spécialisé degree in Génie Civil required."
    unknown_profile = profile.model_copy(update={"nationality": ""})
    item = _ready_item(
        db, job, config, unknown_profile, _settings(settings),
        monkeypatch, tmp_path,
    )
    assert item["category"] == NEEDS_REVIEW
    assert item["reason"] == "required_nationality_not_in_profile"


def test_queue_blocks_nationality_mismatch_and_unresolved_work_authorization(
    db, config, profile, settings, monkeypatch, tmp_path,
):
    job = _job(db)
    job.description += " Moroccan nationality required."
    mismatch = profile.model_copy(update={"nationality": "French"})
    item = _ready_item(
        db, job, config, mismatch, _settings(settings), monkeypatch, tmp_path,
    )
    assert item["category"] == BLOCKED
    assert item["reason"] == "required_nationality_mismatch"

    authorization_job = _job(db, email="right-to-work@colas.fr")
    authorization_job.description += " Applicants must have the right to work in France."
    analysis = mem.store.get_analysis(db, authorization_job.id)
    analysis.work_authorization = "Must have the right to work in France."
    unresolved = _ready_item(
        db, authorization_job, config,
        profile.model_copy(update={"work_authorization": {}}),
        _settings(settings), monkeypatch, tmp_path,
    )
    assert unresolved["category"] == NEEDS_REVIEW
    assert unresolved["reason"] == "work_authorization_requirement_unresolved"


def test_verified_public_competition_is_queued_as_portal_not_email(
    db, config, profile, settings,
):
    job = _job(db, method="COMPANY_PORTAL")
    observed = datetime.now(timezone.utc)
    official_url = "https://www.emploi-public.ma/fr/concours/details/example"
    job.source = "emploi_public_ma"
    job.source_type = "government"
    job.country = "Morocco"
    job.last_verified_at = observed
    job.closing_at = observed + timedelta(days=2)
    job.url = official_url
    job.entity_evidence_url = job.url
    job.entity_state = "VACANCY_VERIFIED"
    job.entity_reason_code = "official_public_competition_verified_open"
    job.application_url = "https://alomrane.gov.ma/Le-groupe/Offre-d-emplois"
    job.public_competition = {
        "source": "emploi_public_ma",
        "status": "OPEN_ANNOUNCEMENT",
        "stage": "OPEN_ANNOUNCEMENT",
        "observed_at": observed.isoformat(),
        "evidence_url": job.url,
        "qualification_requirements": "Technicien specialise en genie civil",
        "eligibility_requirements": "Nationalite marocaine obligatoire",
        "submission_route": {
            "kind": "ONLINE_PORTAL",
            "destination_url": job.application_url,
            "evidence_url": "https://www.emploi-public.ma/fr/concours/download/arrete/example",
            "conflict": False,
        },
    }
    profile = profile.model_copy(update={"nationality": "Moroccan"})

    item = classify_application(
        db, job, config=config, settings=settings, profile=profile,
        preferences=Preferences(sponsorship_required=False),
    )

    assert item["category"] == PORTAL_APPLICATION, item
    assert item["application_route"]["type"] == "COMPANY_PORTAL"


def test_queue_requires_job_specific_verified_recipient(
    db, config, profile, settings, monkeypatch, tmp_path,
):
    job = _job(db)
    verification = mem.store.get_verified_email(db, job.id)
    db.delete(verification)
    db.flush()

    item = _ready_item(
        db, job, config, profile, _settings(settings), monkeypatch, tmp_path,
    )

    assert item["category"] == NEEDS_REVIEW
    assert item["reason"] == "official_job_specific_recipient_evidence_missing"


def test_queue_blocks_stale_and_duplicate_applications(
    db, config, profile, settings, monkeypatch, tmp_path,
):
    settings = _settings(settings)
    stale = _job(db)
    stale.posted_at = datetime.now(timezone.utc) - timedelta(days=90)
    stale_row = _ready_item(
        db, stale, config, profile, settings, monkeypatch, tmp_path,
    )
    assert stale_row["category"] == BLOCKED
    assert stale_row["reason"].startswith("candidate_eligibility:")

    duplicate = _job(db, email="second@colas.fr")
    prior = add_application(
        db, duplicate.id, None, "APPLY", 90, duplicate.contact_email,
    )
    prior.status = "sent"
    prior.sent_at = datetime.now(timezone.utc)
    db.flush()
    duplicate_row = _ready_item(
        db, duplicate, config, profile, settings, monkeypatch, tmp_path,
    )
    assert duplicate_row["category"] == BLOCKED
    assert duplicate_row["reason"] == "duplicate_application_or_existing_outbound"


def test_queue_blocks_daily_outbound_cap(db, config, profile, settings, monkeypatch, tmp_path):
    settings = _settings(settings)
    job = _job(db)
    for index in range(10):
        other = _job(db, email=f"sent{index}@colas.fr")
        prior = add_application(db, other.id, None, "APPLY", 90, other.contact_email)
        prior.status = "sent"
        prior.sent_at = datetime.now(timezone.utc)
    db.flush()

    row = _ready_item(
        db, job, config, profile, settings, monkeypatch, tmp_path,
    )
    assert row["category"] == BLOCKED
    assert row["reason"] == "daily_combined_outbound_cap_reached"


def test_queue_reports_missing_readonly_recovery_scope(
    db, config, profile, settings, monkeypatch, tmp_path,
):
    _configure_ready(monkeypatch, tmp_path)
    monkeypatch.setattr(
        gmail_oauth, "granted_scopes",
        lambda _settings: {
            gmail_oauth.GMAIL_SEND_SCOPE,
            gmail_oauth.GMAIL_COMPOSE_SCOPE,
        },
    )
    job = _job(db)
    row = classify_application(
        db,
        job,
        config=config,
        settings=_settings(settings),
        profile=profile,
        preferences=Preferences(sponsorship_required=False),
        live_evidence=_evidence(job),
    )

    assert row["category"] == NEEDS_REVIEW
    assert row["reason"] == "gmail_scopes_missing:gmail.readonly"


def test_queue_reports_missing_cv_as_a_blocking_error(
    db, config, profile, settings, monkeypatch, tmp_path,
):
    _configure_ready(monkeypatch, tmp_path)
    empty_cv_dir = tmp_path / "empty-cv"
    empty_cv_dir.mkdir()
    monkeypatch.setattr(
        "app.email.service.resolve_attachment",
        lambda _directory, language: resolve_attachment(empty_cv_dir, language),
    )
    job = _job(db)

    row = classify_application(
        db,
        job,
        config=config,
        settings=_settings(settings),
        profile=profile,
        preferences=Preferences(sponsorship_required=False),
        live_evidence=_evidence(job),
    )

    assert row["category"] == BLOCKED
    assert row["reason"] == "verified_cv_missing"


def test_send_approved_sends_exact_preview_once_and_reports_gmail_message_id(
    db, config, profile, settings, monkeypatch, tmp_path, capsys,
):
    from app import cli
    _configure_ready(monkeypatch, tmp_path)
    job = _job(db)
    settings = _settings(settings)
    live_evidence = _evidence(job)

    class _Verifier:
        def verify(self, _url):
            return live_evidence

    class _Communicator:
        def __init__(self, *_args):
            pass

        async def generate(self, *_args, **_kwargs):
            return {
                "subject": "Application — Technicien VRD",
                "body": "I am applying for the Technicien VRD position. My skills include VRD.",
            }

    class _LLM:
        pass

    monkeypatch.setattr("app.discovery.vacancy_verification.VacancyDetailVerifier", _Verifier)
    monkeypatch.setattr("app.agents.communication_agent.CommunicationAgent", _Communicator)
    monkeypatch.setattr(cli, "_application_queue_context", lambda: (
        config, settings, profile, Preferences(sponsorship_required=False),
    ))
    monkeypatch.setattr(cli, "get_llm", lambda _profile: _LLM())
    monkeypatch.setattr(
        builtins, "input",
        lambda _prompt: f"SEND {mem.store.opportunity_id(job)}",
    )
    monkeypatch.setattr(provider, "send", lambda *_args, **_kwargs: (True, "gmail-msg-123", ""))

    @contextmanager
    def _session_scope():
        yield db

    monkeypatch.setattr(cli, "session_scope", _session_scope)
    args = SimpleNamespace(
        confirm_send=True,
        job_id=mem.store.opportunity_id(job),
    )
    cmd_send_approved(args)
    output = capsys.readouterr().out

    assert "Subject: Application — Technicien VRD" in output
    assert "Status: SENT" in output
    assert "Gmail message ID: gmail-msg-123" in output
    assert db.query(models.LiveSendAttempt).one().state == "confirmed_sent"


def test_send_approved_refuses_without_exact_confirmation(
    db, config, profile, settings, monkeypatch, tmp_path, capsys,
):
    from app import cli
    _configure_ready(monkeypatch, tmp_path)
    job = _job(db)
    monkeypatch.setattr("app.discovery.vacancy_verification.VacancyDetailVerifier", lambda: SimpleNamespace(
        verify=lambda _url: _evidence(job),
    ))
    monkeypatch.setattr(
        cli, "_application_queue_context",
        lambda: (config, _settings(settings), profile, Preferences(sponsorship_required=False)),
    )
    monkeypatch.setattr("app.agents.communication_agent.CommunicationAgent", lambda *_args: SimpleNamespace(
        generate=lambda *_args, **_kwargs: asyncio.sleep(0, result={
            "subject": "Application",
            "body": "I am applying for this role.",
        }),
    ))
    monkeypatch.setattr(cli, "get_llm", lambda _profile: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    monkeypatch.setattr(provider, "send", lambda *_args, **_kwargs: pytest.fail("must not send"))

    @contextmanager
    def _session_scope():
        yield db

    monkeypatch.setattr(cli, "session_scope", _session_scope)
    cmd_send_approved(SimpleNamespace(confirm_send=True, job_id=mem.store.opportunity_id(job)))

    assert "exact confirmation not received" in capsys.readouterr().out
    assert db.query(models.LiveSendAttempt).count() == 0


def test_send_approved_reports_uncertain_and_reconciles_before_any_second_send(
    db, config, profile, settings, monkeypatch, tmp_path, capsys,
):
    from app import cli

    _configure_ready(monkeypatch, tmp_path)
    job = _job(db)
    settings = _settings(settings)

    class _Verifier:
        def verify(self, _url):
            return _evidence(job)

    class _Communicator:
        def __init__(self, *_args):
            pass

        async def generate(self, *_args, **_kwargs):
            return {"subject": "Application", "body": "I am applying for this role."}

    monkeypatch.setattr("app.discovery.vacancy_verification.VacancyDetailVerifier", _Verifier)
    monkeypatch.setattr("app.agents.communication_agent.CommunicationAgent", _Communicator)
    monkeypatch.setattr(cli, "_application_queue_context", lambda: (
        config, settings, profile, Preferences(sponsorship_required=False),
    ))
    monkeypatch.setattr(cli, "get_llm", lambda _profile: None)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: f"SEND {mem.store.opportunity_id(job)}",
    )
    send_calls = []
    monkeypatch.setattr(
        provider,
        "send",
        lambda *_args, **_kwargs: (
            send_calls.append("sent"),
            False,
            "",
            provider.GMAIL_SEND_OUTCOME_UNCERTAIN,
        )[1:],
    )
    monkeypatch.setattr(
        provider,
        "reconcile_live_send",
        lambda *_args, **_kwargs: SimpleNamespace(status="unavailable"),
    )

    @contextmanager
    def _session_scope():
        yield db

    monkeypatch.setattr(cli, "session_scope", _session_scope)
    args = SimpleNamespace(confirm_send=True, job_id=mem.store.opportunity_id(job))
    with pytest.raises(SystemExit):
        cmd_send_approved(args)
    first_output = capsys.readouterr().out
    assert "Status: SEND_OUTCOME_UNKNOWN" in first_output
    assert send_calls == ["sent"]

    with pytest.raises(SystemExit):
        cmd_send_approved(args)
    second_output = capsys.readouterr().out
    assert "Status: SEND_OUTCOME_UNKNOWN" in second_output
    assert "Do not retry" in second_output
    assert send_calls == ["sent"]
    assert db.query(models.LiveSendAttempt).one().state == "uncertain"


def test_send_approved_reports_definitive_gmail_failure_without_drafting(
    db, config, profile, settings, monkeypatch, tmp_path, capsys,
):
    from app import cli

    _configure_ready(monkeypatch, tmp_path)
    job = _job(db)
    settings = _settings(settings)

    class _Verifier:
        def verify(self, _url):
            return _evidence(job)

    class _Communicator:
        def __init__(self, *_args):
            pass

        async def generate(self, *_args, **_kwargs):
            return {"subject": "Application", "body": "I am applying for this role."}

    monkeypatch.setattr("app.discovery.vacancy_verification.VacancyDetailVerifier", _Verifier)
    monkeypatch.setattr("app.agents.communication_agent.CommunicationAgent", _Communicator)
    monkeypatch.setattr(cli, "_application_queue_context", lambda: (
        config, settings, profile, Preferences(sponsorship_required=False),
    ))
    monkeypatch.setattr(cli, "get_llm", lambda _profile: None)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: f"SEND {mem.store.opportunity_id(job)}",
    )
    monkeypatch.setattr(
        provider,
        "send",
        lambda *_args, **_kwargs: (
            False, "", f"{provider.GMAIL_SEND_NOT_ATTEMPTED} OAuthError.",
        ),
    )
    monkeypatch.setattr(provider, "create_draft", lambda *_args, **_kwargs: pytest.fail(
        "a failed direct send must not create a draft",
    ))

    @contextmanager
    def _session_scope():
        yield db

    monkeypatch.setattr(cli, "session_scope", _session_scope)
    with pytest.raises(SystemExit) as exc:
        cmd_send_approved(SimpleNamespace(
            confirm_send=True,
            job_id=mem.store.opportunity_id(job),
        ))
    output = capsys.readouterr().out

    assert exc.value.code == 1
    assert "Status: BLOCKED" in output
    assert "Provider outcome:" in output
    assert db.query(models.LiveSendAttempt).one().state == "definitively_failed"
    assert db.query(models.Email).one().draft_id == ""


def test_direct_live_send_cap_does_not_fall_back_to_a_draft(
    db, config, profile, settings, monkeypatch,
):
    from app import models
    from app.email import provider as provider_mod

    job = _job(db)
    decision = mem.store.get_last_decision(db, job.id)
    settings = settings.model_copy(update={
        "enable_email": True,
        "email_provider": "gmail",
        "email_mode": "live",
        "authorize_live_job_email": True,
        "approved_live_job_id": str(job.id),
        "live_email_manual_dispatch": True,
    })
    for index in range(10):
        other = _job(db, email=f"cap{index}@colas.fr")
        application = add_application(db, other.id, None, "APPLY", 90, other.contact_email)
        application.status = "sent"
        application.sent_at = datetime.now(timezone.utc)
    db.flush()
    assert mem.store.count_dispatched_today(db) == 10
    monkeypatch.setattr(provider_mod, "create_draft", lambda *_args, **_kwargs: pytest.fail(
        "direct send must never silently fall back to draft",
    ))
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    result = asyncio.run(ApplicationEngine(
        db, config, settings, profile,
        SimpleNamespace(generate=lambda *_args, **_kwargs: asyncio.sleep(0, result={
            "subject": "Application",
            "body": "I am applying for this role.",
        })),
        verify_url_fn=lambda selected: _evidence(selected),
    ).run(
        job, decision, "APPLY", job.contact_email, "fr",
        allow_live_cap_draft_fallback=False,
    ))

    assert result["status"] == "blocked"
    email = db.query(models.Email).one()
    assert email.status == "blocked"
    assert email.draft_id == ""
