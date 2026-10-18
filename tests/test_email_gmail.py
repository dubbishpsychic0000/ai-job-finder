"""Gmail sending layer tests — mocked provider, NO real emails ever sent.

Covers the full ApplicationEngine path for the Gmail provider: safety gate runs
before send, Gmail message ID is persisted, dry-run blocks at the engine AND at
the transport, duplicates are rejected, and OAuth secrets never leak into logs.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest
from rich.console import Console
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.cli as cli
from app import models
from app.agents.communication_agent import CommunicationAgent
from app.email import gmail_oauth
from app.email import provider as provider_mod
from app.email.service import ApplicationEngine
from app.memory.store import add_application, get_or_create_company


def _job(db, title="Technicien VRD", email="jobs@colas.example", posted_days_ago=2):
    company = get_or_create_company(db, "Colas")
    job = models.Job(
        source="static_files", external_id="g1", dedup_key="hash:gmail1", title=title,
        company_id=company.id, location="Lyon, France", country="France",
        description=f"Road construction VRD. Send your application and CV to {email}.",
        url="https://boards.greenhouse.io/example/jobs/1",
        posted_at=datetime.now(timezone.utc) - timedelta(days=posted_days_ago),
        contact_email=email, status="analyzed",
        source_type="ats",
        entity_type="INDIVIDUAL_VACANCY",
        entity_state="VACANCY_VERIFIED",
        entity_reason_code="matching_public_ats_posting_record",
        entity_evidence_url="https://boards.greenhouse.io/example/jobs/1",
        verification_status="verified",
        last_verified_at=datetime.now(timezone.utc),
        application_method="EMAIL",
        application_url="https://boards.greenhouse.io/example/jobs/1",
    )
    db.add(job)
    db.flush()
    return job


def _decision(db, job, decision="APPLY", score=88.0):
    d = models.Decision(job_id=job.id, decision=decision, overall_score=score,
                        scores={}, reason="test", rules_fired=[], ai_reason="")
    db.add(d)
    db.flush()
    return d


def _settings(settings, **kw):
    return settings.model_copy(update={"enable_email": True, "email_provider": "gmail",
                                       "email_mode": "live",
                                       "authorize_live_job_email": True,
                                       "approved_live_job_id": "1",
                                       "live_email_manual_dispatch": True, **kw})


def _engine(db, config, profile, settings, communicator):
    def verify(job):
        return SimpleNamespace(
            outcome="OPEN",
            reason_code="matching_public_ats_posting_record",
            evidence_url=job.url,
            observed_at=datetime.now(timezone.utc),
            title=job.title,
            employer=job.company.name,
            location=job.location,
            description=job.description,
            application_url=job.application_url,
            application_method=job.application_method,
            application_email=job.contact_email,
        )

    return ApplicationEngine(
        db, config, settings, profile, communicator, verify_url_fn=verify,
    )


def _real_communicator(profile):
    from app.agents.llm import NullLLM

    return CommunicationAgent(NullLLM(profile), profile)


def _draft_communicator(body):
    class _Fake:
        async def generate(self, job, action, target_language="en", recipient_name=""):
            return {"subject": "Application", "body": body}
    return _Fake()


def test_application_engine_blocks_unverified_non_spontaneous_vacancy(db, config, profile, settings):
    job = models.Job(
        source="search_engine",
        external_id="research-page",
        dedup_key="research-page",
        title="Civil Engineering Technician",
        url="https://example.test/jobs/technician",
        status="analyzed",
    )
    db.add(job)
    db.flush()
    decision = _decision(db, job)
    result = asyncio.run(ApplicationEngine(
        db, config, settings, profile, _draft_communicator("test"),
    ).run(job, decision, "APPLY", "jobs@example.test", "en"))

    assert result == {
        "sent": False,
        "status": "blocked",
        "reason": "vacancy_not_verified",
    }
    assert db.query(models.Application).all() == []


def test_application_engine_requires_live_reverification_for_verified_vacancy(
    db, config, profile, settings,
):
    job = _job(db)
    decision = _decision(db, job)
    result = asyncio.run(ApplicationEngine(
        db, config, settings, profile, _draft_communicator("test"),
    ).run(job, decision, "APPLY", job.contact_email, "en"))

    assert result == {
        "sent": False,
        "status": "blocked",
        "reason": "vacancy_reverification_unavailable",
    }
    assert db.query(models.Application).all() == []


# ---------------------------------------------------------------------------
# happy path: send via Gmail, message id + app status persisted
# ---------------------------------------------------------------------------

def test_gmail_send_persists_status_and_message_id(db, config, profile, settings, monkeypatch, caplog):
    job = _job(db)
    decision = _decision(db, job)
    settings = _settings(settings)
    calls = []

    def fake_gmail(settings_, to, subject, body, attachments, **_kwargs):
        calls.append(to)
        return True, "msg-gmail-12345", ""

    monkeypatch.setattr(provider_mod, "_send_gmail", fake_gmail)
    with caplog.at_level(logging.INFO):
        result = asyncio.run(_engine(db, config, profile, settings, _real_communicator(profile)).run(
            job, decision, "APPLY", job.contact_email, "fr"))

    assert calls == [job.contact_email]
    assert result["sent"] is True
    assert result["status"] == "sent"
    assert result["message_id"] == "msg-gmail-12345"

    app = db.query(models.Application).one()
    assert app.status == "sent"
    assert app.sent_at is not None
    assert app.follow_up_at is not None
    email = db.query(models.Email).one()
    assert email.status == "sent"
    assert email.message_id == "msg-gmail-12345"
    assert job.status == "acted"

    # the message id is persisted in the action event for the dashboard
    event = db.query(models.Event).filter(models.Event.type == "action").one()
    assert event.data.get("message_id") == "msg-gmail-12345"


def test_gmail_blocked_in_dry_run_never_calls_provider(db, config, profile, settings, monkeypatch):
    job = _job(db)
    decision = _decision(db, job)
    settings = _settings(settings, enable_email=False)

    def boom(*a, **k):
        raise AssertionError("provider must not be called when email is disabled")

    monkeypatch.setattr(provider_mod, "_send_gmail", boom)
    result = asyncio.run(_engine(db, config, profile, settings, _real_communicator(profile)).run(
        job, decision, "APPLY", job.contact_email, "fr"))

    assert result["sent"] is False
    assert result["status"] == "blocked"
    assert db.query(models.Application).one().status == "blocked"


def test_safety_gate_blocks_invented_claims_before_gmail(db, config, profile, settings, monkeypatch):
    job = _job(db)
    decision = _decision(db, job)
    settings = _settings(settings)
    invented = _draft_communicator("I have 25 years of experience in civil engineering.")

    def boom(*a, **k):
        raise AssertionError("provider must not be called when safety gate blocks")

    monkeypatch.setattr(provider_mod, "_send_gmail", boom)
    result = asyncio.run(_engine(db, config, profile, settings, invented).run(
        job, decision, "APPLY", job.contact_email, "fr"))

    assert result["status"] == "blocked"
    assert "invented claim" in " ".join(result["report"])


def test_duplicate_application_blocked(db, config, profile, settings, monkeypatch):
    job = _job(db)
    decision = _decision(db, job)
    settings = _settings(settings)
    already = add_application(db, job.id, decision.id, "APPLY", 88.0, job.contact_email)
    already.status = "sent"
    db.commit()

    def boom(*a, **k):
        raise AssertionError("provider must not be called for a duplicate")

    monkeypatch.setattr(provider_mod, "_send_gmail", boom)
    result = asyncio.run(_engine(db, config, profile, settings, _real_communicator(profile)).run(
        job, decision, "APPLY", job.contact_email, "fr"))

    assert result["status"] == "blocked"
    assert any("already applied" in r for r in result["report"])


def test_spontaneous_live_send_requires_explicit_authorization(
    db, config, profile, settings, monkeypatch,
):
    job = _job(db)
    job.opportunity_type = "SPONTANEOUS_APPLICATION"
    decision = _decision(db, job)
    settings = _settings(settings, authorize_live_spontaneous=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unauthorized spontaneous live mode must not reach Gmail")

    monkeypatch.setattr(provider_mod, "send", forbidden)
    result = asyncio.run(_engine(
        db, config, profile, settings, _real_communicator(profile),
    ).run(job, decision, "APPLY", job.contact_email, "fr"))

    assert result["status"] == "blocked"
    assert "not explicitly authorized" in result["reason"]


def test_authorized_spontaneous_live_send_requires_provider_message_id(
    db, config, profile, settings, monkeypatch,
):
    job = _job(db)
    job.opportunity_type = "SPONTANEOUS_APPLICATION"
    decision = _decision(db, job)
    settings = _settings(settings, authorize_live_spontaneous=True)
    monkeypatch.setattr(
        "app.email.service.validate",
        lambda **_kwargs: SimpleNamespace(allowed=True, reasons=[], checks={}),
    )
    calls = []
    monkeypatch.setattr(
        provider_mod,
        "send",
        lambda *_args, **_kwargs: (calls.append("send") or (True, "", "")),
    )

    result = asyncio.run(_engine(
        db, config, profile, settings, _real_communicator(profile),
    ).run(job, decision, "APPLY", job.contact_email, "fr"))

    assert calls == ["send"]
    assert result["status"] == "recovery_required"
    assert result["sent"] is False
    assert provider_mod.GMAIL_SEND_OUTCOME_UNCERTAIN in result["error"]
    assert db.query(models.Email).one().delivery_state == "uncertain"


def test_ordinary_live_email_requires_matching_manual_single_job_authorization(
    db, config, profile, settings, monkeypatch,
):
    job = _job(db)
    decision = _decision(db, job)
    settings = _settings(
        settings,
        approved_live_job_id=str(job.id + 1),
    )
    monkeypatch.setattr(
        provider_mod, "send",
        lambda *_args, **_kwargs: pytest.fail("unauthorized job must not reach Gmail"),
    )

    result = asyncio.run(_engine(
        db, config, profile, settings, _real_communicator(profile),
    ).run(job, decision, "APPLY", job.contact_email, "fr"))

    assert result == {
        "sent": False,
        "status": "blocked",
        "reason": "single_job_live_email_authorization_required",
    }


def test_live_action_workflow_only_processes_the_exact_approved_job_id(
    db, config, profile, settings, monkeypatch,
):
    from app.agents.llm import NullLLM
    from app.workflows.action import run_actions

    first_job = _job(db)
    first_decision = _decision(db, first_job)
    approved_job = _job(db, email="approved@colas.example")
    approved_job.external_id = "g2"
    approved_job.dedup_key = "hash:gmail2"
    approved_decision = _decision(db, approved_job)
    settings = _settings(
        settings,
        approved_live_job_id=str(approved_job.id),
    )
    processed = []

    class _RecordingEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, job, decision, action, contact_email, target_language):
            processed.append((job.id, decision.id, action))
            return {"sent": True, "status": "sent", "application_id": None}

    monkeypatch.setattr("app.workflows.action.ApplicationEngine", _RecordingEngine)

    report = asyncio.run(run_actions(
        db, config, settings, profile, NullLLM(profile),
    ))

    assert processed == [(approved_job.id, approved_decision.id, "APPLY")]
    assert first_job.id != approved_job.id
    assert first_decision.id != approved_decision.id
    assert [item["job_id"] for item in report.applied] == [approved_job.id]


def test_portal_vacancy_never_sends_cv_by_email(db, config, profile, settings, monkeypatch):
    job = _job(db)
    job.application_method = "COMPANY_PORTAL"
    decision = _decision(db, job)
    monkeypatch.setattr(
        provider_mod, "send",
        lambda *_args, **_kwargs: pytest.fail("portal route must not send email"),
    )

    result = asyncio.run(_engine(
        db, config, profile, _settings(settings), _real_communicator(profile),
    ).run(job, decision, "APPLY", job.contact_email, "fr"))

    assert result["status"] == "blocked"
    assert result["reason"] == "portal_application_requires_portal_submission"


def test_employer_inquiry_is_not_an_application_and_has_no_cv_attachment(
    db, config, profile, settings, monkeypatch,
):
    job = _job(db)
    job.application_method = "COMPANY_PORTAL"
    decision = _decision(db, job, decision="ASK_EMPLOYER")
    monkeypatch.setattr(
        "app.email.service.validate",
        lambda **_kwargs: SimpleNamespace(allowed=True, reasons=[], checks={}),
    )
    sent = []

    def send(_settings, **kwargs):
        sent.append(kwargs)
        return True, "gmail-inquiry-message-id", ""

    monkeypatch.setattr(provider_mod, "send", send)
    result = asyncio.run(_engine(
        db, config, profile, _settings(settings), _real_communicator(profile),
    ).run(job, decision, "ASK_EMPLOYER", job.contact_email, "fr"))

    assert result["status"] == "inquiry_sent"
    assert result["sent"] is True
    assert sent[0]["attachments"] == []
    application = db.query(models.Application).one()
    assert application.action == "ASK_EMPLOYER"
    assert application.status == "inquiry_sent"


# ---------------------------------------------------------------------------
# transport-level guards
# ---------------------------------------------------------------------------

def test_gmail_transport_refuses_when_disabled(settings, monkeypatch):
    settings = _settings(settings, enable_email=False)

    def boom(*a, **k):
        raise AssertionError("authenticated_service must not be built when disabled")

    monkeypatch.setattr(gmail_oauth, "authenticated_service", boom)
    ok, _mid, err = provider_mod.send(settings, to="x@y.example", subject="s", body="b")
    assert ok is False
    assert "ENABLE_EMAIL" in err


def test_gmail_transport_returns_message_id_on_success(settings, monkeypatch):
    settings = _settings(settings)

    class _FakeSend:
        def execute(self):
            return {"id": "msg-api-999"}

    class _FakeMessages:
        def send(self, **kwargs):
            return _FakeSend()

    class _FakeUsers:
        def messages(self):
            return _FakeMessages()

    class _FakeService:
        def users(self):
            return _FakeUsers()

    monkeypatch.setattr(gmail_oauth, "authenticated_service", lambda s: _FakeService())
    ok, mid, err = provider_mod.send(settings, to="x@y.example", subject="s", body="b",
                                     attachments=[])
    assert ok is True
    assert mid == "msg-api-999"
    assert err == ""


def test_gmail_timeout_after_send_dispatch_is_uncertain(settings, monkeypatch):
    settings = _settings(settings)

    class _FakeSend:
        def execute(self):
            raise TimeoutError("simulated network timeout")

    class _FakeMessages:
        def send(self, **_kwargs):
            return _FakeSend()

    class _FakeUsers:
        def messages(self):
            return _FakeMessages()

    monkeypatch.setattr(
        gmail_oauth, "authenticated_service",
        lambda _settings: SimpleNamespace(users=lambda: _FakeUsers()),
    )

    ok, message_id, error = provider_mod.send(
        settings, to="jobs@employer.example", subject="Application", body="Hello",
        attempt_key="send-attempt",
    )

    assert not ok
    assert not message_id
    assert error == provider_mod.GMAIL_SEND_OUTCOME_UNCERTAIN


def test_live_send_mime_contains_stable_attempt_marker():
    encoded = provider_mod._build_mime(
        "jobs@employer.example", "Application", "Hello", [],
        live_attempt_key="stable-live-attempt",
    )
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(encoded + "===")
    )

    assert message["X-WCA-Live-Attempt"] == "stable-live-attempt"


@pytest.mark.parametrize(
    ("draft_response", "expected_ok", "expected_id"),
    [
        ({"id": "draft-api-123"}, True, "draft-api-123"),
        ({}, False, ""),
        (None, False, ""),
        ([], False, ""),
    ],
)
def test_gmail_draft_transport_requires_api_draft_id(
    settings, monkeypatch, draft_response, expected_ok, expected_id
):
    settings = _settings(settings, email_mode="draft")

    class _FakeDraftCreate:
        def execute(self):
            return draft_response

    class _FakeDrafts:
        def create(self, **_kwargs):
            return _FakeDraftCreate()

    class _FakeUsers:
        def drafts(self):
            return _FakeDrafts()

    class _FakeService:
        def users(self):
            return _FakeUsers()

    monkeypatch.setattr(gmail_oauth, "authenticated_service", lambda _settings: _FakeService())
    monkeypatch.setattr(
        provider_mod, "_build_mime",
        lambda *_args, **_kwargs: "encoded-message",
    )

    ok, draft_id, error = provider_mod.create_draft(
        settings, to="jobs@employer.example", subject="Application", body="Hello",
    )

    assert ok is expected_ok
    assert draft_id == expected_id
    if not expected_ok:
        assert error == provider_mod.GMAIL_DRAFT_OUTCOME_UNCERTAIN


def test_draft_mime_contains_reconciliation_marker():
    encoded = provider_mod._build_mime(
        "jobs@employer.example", "Application", "Hello", [],
        draft_attempt_key="stable-attempt-key",
    )
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(encoded + "===")
    )

    assert message["X-WCA-Draft-Attempt"] == "stable-attempt-key"


def test_gmail_draft_reconciliation_matches_only_exact_attempt_marker(
    settings, monkeypatch,
):
    settings = _settings(settings, email_mode="draft")

    class _Drafts:
        def list(self, **_kwargs):
            return SimpleNamespace(execute=lambda: {"drafts": [{"id": "gmail-id"}]})

        def get(self, **_kwargs):
            return SimpleNamespace(execute=lambda: {
                "id": "gmail-id",
                "message": {
                    "payload": {
                        "headers": [
                            {"name": "X-WCA-Draft-Attempt", "value": "expected-key"},
                        ],
                    },
                },
            })

    class _Users:
        def drafts(self):
            return _Drafts()

    monkeypatch.setattr(
        gmail_oauth, "authenticated_service",
        lambda _settings: SimpleNamespace(users=lambda: _Users()),
    )

    result = provider_mod.reconcile_draft(settings, "expected-key")

    assert result == provider_mod.DraftReconciliation(
        "found", draft_id="gmail-id", matches=1,
    )


def _mock_reconciliation_service(monkeypatch, settings, pages, messages):
    class _Request:
        def __init__(self, response):
            self.response = response

        def execute(self):
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

    class _Drafts:
        def list(self, *, pageToken=None, **_kwargs):
            response = pages.get(pageToken, {})
            return _Request(response)

        def get(self, *, id, **_kwargs):
            return _Request(messages.get(id, {}))

    class _Users:
        def drafts(self):
            return _Drafts()

    monkeypatch.setattr(
        gmail_oauth, "authenticated_service",
        lambda _settings: SimpleNamespace(users=lambda: _Users()),
    )


def _mock_live_reconciliation_service(monkeypatch, settings, pages, messages):
    class _Request:
        def __init__(self, response):
            self.response = response

        def execute(self):
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

    class _Messages:
        def list(self, *, pageToken=None, **_kwargs):
            return _Request(pages.get(pageToken, {}))

        def get(self, *, id, **_kwargs):
            return _Request(messages.get(id, {}))

    class _Users:
        def messages(self):
            return _Messages()

    monkeypatch.setattr(
        gmail_oauth, "authenticated_service",
        lambda _settings: SimpleNamespace(users=lambda: _Users()),
    )
    monkeypatch.setattr(
        gmail_oauth, "granted_scopes",
        lambda _settings: {gmail_oauth.GMAIL_READONLY_SCOPE},
    )


def _live_message(marker, *, message_id="gmail-id", recipient="jobs@employer.example",
                  subject="Application"):
    headers = [
        {"name": "To", "value": recipient},
        {"name": "Subject", "value": subject},
    ]
    if marker is not None:
        headers.append({"name": "X-WCA-Live-Attempt", "value": marker})
    return {
        "id": message_id,
        "payload": {"headers": headers},
    }


def test_live_sent_reconciliation_requires_exact_marker_recipient_and_subject(
    settings, monkeypatch,
):
    settings = _settings(settings)
    _mock_live_reconciliation_service(
        monkeypatch, settings,
        {None: {"messages": [{"id": "gmail-id"}]}},
        {"gmail-id": _live_message("expected-key")},
    )

    result = provider_mod.reconcile_live_send(
        settings, attempt_key="expected-key", recipient="jobs@employer.example",
        subject="Application", started_at=datetime.now(timezone.utc),
    )

    assert result == provider_mod.LiveSendReconciliation(
        "found", message_id="gmail-id", matches=1,
    )


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ({"one": _live_message("expected-key", message_id="one"),
          "two": _live_message("expected-key", message_id="two")}, "multiple"),
        ({"one": _live_message("other-key", message_id="one")}, "ambiguous"),
        ({"one": _live_message("expected-key", message_id="one",
                               recipient="other@employer.example")}, "not_found"),
    ],
)
def test_live_sent_reconciliation_rejects_ambiguous_or_duplicate_matches(
    settings, monkeypatch, messages, expected,
):
    settings = _settings(settings)
    ids = list(messages)
    _mock_live_reconciliation_service(
        monkeypatch, settings,
        {None: {"messages": [{"id": key} for key in ids]}},
        messages,
    )

    result = provider_mod.reconcile_live_send(
        settings, attempt_key="expected-key", recipient="jobs@employer.example",
        subject="Application", started_at=datetime.now(timezone.utc),
    )

    assert result.status == expected


def test_live_sent_reconciliation_incomplete_scan_is_not_a_no_match(
    settings, monkeypatch,
):
    settings = _settings(settings)
    _mock_live_reconciliation_service(
        monkeypatch, settings,
        {None: {"messages": [{"id": "one"}], "nextPageToken": "more"}},
        {"one": _live_message("other-key", message_id="one")},
    )

    result = provider_mod.reconcile_live_send(
        settings, attempt_key="expected-key", recipient="jobs@employer.example",
        subject="Application", started_at=datetime.now(timezone.utc),
        max_messages=1,
    )

    assert result.status == "incomplete"


def _draft_message(marker=None, *, draft_id="gmail-id"):
    headers = [] if marker is None else [
        {"name": "X-WCA-Draft-Attempt", "value": marker},
    ]
    return {
        "id": draft_id,
        "message": {"payload": {"headers": headers}},
    }


def test_reconciliation_paginates_before_confirming_a_marker(settings, monkeypatch):
    settings = _settings(settings, email_mode="draft")
    pages = {
        None: {"drafts": [{"id": "first"}], "nextPageToken": "page-2"},
        "page-2": {"drafts": [{"id": "matching"}]},
    }
    messages = {
        "first": _draft_message("other-attempt", draft_id="first"),
        "matching": _draft_message("expected-key", draft_id="matching"),
    }
    _mock_reconciliation_service(monkeypatch, settings, pages, messages)

    result = provider_mod.reconcile_draft(settings, "expected-key")

    assert result == provider_mod.DraftReconciliation(
        "found", draft_id="matching", matches=1,
    )


def test_reconciliation_multiple_matches_remains_ambiguous(settings, monkeypatch):
    settings = _settings(settings, email_mode="draft")
    pages = {None: {"drafts": [{"id": "one"}, {"id": "two"}]}}
    messages = {
        "one": _draft_message("expected-key", draft_id="one"),
        "two": _draft_message("expected-key", draft_id="two"),
    }
    _mock_reconciliation_service(monkeypatch, settings, pages, messages)

    result = provider_mod.reconcile_draft(settings, "expected-key")

    assert result == provider_mod.DraftReconciliation("multiple", matches=2)


def test_reconciliation_limit_with_more_pages_is_incomplete(settings, monkeypatch):
    settings = _settings(settings, email_mode="draft")
    pages = {
        None: {"drafts": [{"id": "one"}], "nextPageToken": "page-2"},
    }
    messages = {"one": _draft_message("other-attempt", draft_id="one")}
    _mock_reconciliation_service(monkeypatch, settings, pages, messages)

    result = provider_mod.reconcile_draft(
        settings, "expected-key", max_drafts=1,
    )

    assert result == provider_mod.DraftReconciliation("incomplete")


@pytest.mark.parametrize(
    ("pages", "messages"),
    [
        ({None: RuntimeError("timeout")}, {}),
        ({None: []}, {}),
        ({None: {"nextPageToken": "page-2"}}, {}),
        ({None: {"drafts": [], "nextPageToken": 123}}, {}),
        ({None: {"drafts": [{"id": ""}]}}, {}),
        ({None: {"drafts": [{"id": "one"}]}}, {"one": {"id": "one"}}),
        (
            {None: {"drafts": [{"id": "one"}]}},
            {"one": {"id": "one", "message": {"payload": {}}}},
        ),
    ],
)
def test_reconciliation_partial_or_unavailable_response_is_inconclusive(
    settings, monkeypatch, pages, messages,
):
    settings = _settings(settings, email_mode="draft")
    _mock_reconciliation_service(monkeypatch, settings, pages, messages)

    result = provider_mod.reconcile_draft(settings, "expected-key")

    assert result.status == "unavailable"


def test_missing_custom_marker_is_not_treated_as_proof_of_no_draft(
    settings, monkeypatch,
):
    settings = _settings(settings, email_mode="draft")
    pages = {None: {"drafts": [{"id": "draft-without-marker"}]}}
    messages = {
        "draft-without-marker": _draft_message(
            draft_id="draft-without-marker",
        ),
    }
    _mock_reconciliation_service(monkeypatch, settings, pages, messages)

    result = provider_mod.reconcile_draft(settings, "expected-key")

    # Gmail's retention of app-defined headers is not established here.
    # A complete scan without one is only "no marker match"; service recovery
    # must still fail closed instead of interpreting it as permission to retry.
    assert result == provider_mod.DraftReconciliation("not_found")


def test_reconciliation_repeated_page_token_is_incomplete(settings, monkeypatch):
    settings = _settings(settings, email_mode="draft")
    pages = {
        None: {"drafts": [], "nextPageToken": "repeat"},
        "repeat": {"drafts": [], "nextPageToken": "repeat"},
    }
    _mock_reconciliation_service(monkeypatch, settings, pages, {})

    result = provider_mod.reconcile_draft(settings, "expected-key")

    assert result == provider_mod.DraftReconciliation("incomplete")


def test_gmail_draft_authentication_failure_is_safe_to_retry(settings, monkeypatch, caplog):
    settings = _settings(settings, email_mode="draft")
    monkeypatch.setattr(
        gmail_oauth, "authenticated_service",
        lambda _settings: (_ for _ in ()).throw(
            gmail_oauth.OAuthError("refresh token rejected")
        ),
    )

    with caplog.at_level(logging.WARNING):
        ok, draft_id, error = provider_mod.create_draft(
            settings, to="jobs@employer.example", subject="Application", body="Hello",
        )

    assert (ok, draft_id, error) == (
        False, "", provider_mod.GMAIL_REAUTH_REQUIRED,
    )
    assert "refresh token rejected" not in caplog.text


def test_invalid_grant_refresh_is_reported_without_provider_details(settings, monkeypatch, caplog):
    settings = _settings(settings, email_mode="draft")

    class _RejectedCredentials:
        scopes = (gmail_oauth.GMAIL_COMPOSE_SCOPE,)
        valid = False
        expired = True
        refresh_token = "must-not-be-logged"

        def refresh(self, _request):
            raise RuntimeError("invalid_grant: must-not-be-logged")

    monkeypatch.setattr(
        gmail_oauth, "_credentials_from_file",
        lambda _path: _RejectedCredentials(),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(
        gmail_oauth.OAuthError, match="re-authorize",
    ) as error:
        gmail_oauth.get_credentials(settings)

    assert "must-not-be-logged" not in caplog.text
    assert "must-not-be-logged" not in str(error.value)


def test_rejected_gmail_auth_failure_does_not_block_retry(
    db, config, profile, settings, monkeypatch,
):
    job = _job(db)
    job.opportunity_type = "SPONTANEOUS_APPLICATION"
    decision = _decision(db, job)
    settings = _settings(settings, email_mode="draft")
    monkeypatch.setattr(
        "app.email.service.validate",
        lambda **_kwargs: SimpleNamespace(allowed=True, reasons=[], checks={}),
    )
    responses = iter([
        (False, "", provider_mod.GMAIL_REAUTH_REQUIRED),
        (True, "real-draft-id", ""),
    ])
    monkeypatch.setattr(
        provider_mod, "create_draft",
        lambda *_args, **_kwargs: next(responses),
    )
    engine = _engine(db, config, profile, settings, _draft_communicator(
        "Je vous adresse ma candidature pour un poste en ingénierie civile."
    ))

    first = asyncio.run(engine.run(job, decision, "APPLY", job.contact_email, "fr"))
    assert first["status"] == "failed"
    assert db.query(models.Application).one().status == "failed"
    assert db.query(models.Email).one().status == "failed"
    assert not db.query(models.Email).one().draft_id

    second = asyncio.run(engine.run(job, decision, "APPLY", job.contact_email, "fr"))
    assert second["status"] == "drafted"
    assert second["draft_id"] == "real-draft-id"
    assert [app.status for app in db.query(models.Application).order_by(
        models.Application.id
    )] == ["failed", "drafted"]


def test_confirmed_draft_blocks_second_process_after_persisted_reload(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings, email_mode="draft")
    created = []

    def create_draft(_settings, *, attempt_key, **_kwargs):
        created.append(attempt_key)
        return True, "gmail-draft-opaque-123", ""

    monkeypatch.setattr(provider_mod, "create_draft", create_draft)
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    job = _job(db)
    decision = _decision(db, job)
    first = asyncio.run(_engine(
        db, config, profile, settings,
        _draft_communicator("Bonjour, je souhaite soumettre ma candidature."),
    ).run(job, decision, "APPLY", job.contact_email, "fr"))
    assert first["status"] == "drafted"
    db.commit()

    reloaded = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        persisted_job = reloaded.get(models.Job, job.id)
        persisted_decision = reloaded.get(models.Decision, decision.id)
        second = asyncio.run(_engine(
            reloaded, config, profile, settings,
            _draft_communicator("Bonjour, je souhaite soumettre ma candidature."),
        ).run(
            persisted_job, persisted_decision, "APPLY",
            persisted_job.contact_email, "fr",
        ))
        assert second["status"] == "blocked"
        assert any("already applied" in reason for reason in second["report"])
        assert len(created) == 1
        assert created[0]
        assert reloaded.query(models.Email).filter(
            models.Email.draft_id == "gmail-draft-opaque-123"
        ).count() == 1
    finally:
        reloaded.close()


def test_distinct_companies_can_each_receive_a_draft(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings, email_mode="draft")
    monkeypatch.setattr(
        "app.email.service.validate",
        lambda **_kwargs: SimpleNamespace(allowed=True, reasons=[], checks={}),
    )
    create_calls = []
    monkeypatch.setattr(
        provider_mod, "create_draft",
        lambda _settings, *, to, **_kwargs: (
            create_calls.append(to) or (True, f"draft-{len(create_calls)}", "")
        ),
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    first_job = _job(db, email="jobs@colas.example")
    first_decision = _decision(db, first_job)
    other_company = get_or_create_company(db, "Other Employer")
    second_job = models.Job(
        source="static_files",
        external_id="other-job",
        dedup_key="hash:other-job",
        title="Project Engineer",
        company_id=other_company.id,
        location="Lyon, France",
        country="France",
        description="Civil engineering. Send your application and CV to jobs@other.example.",
        url="https://boards.greenhouse.io/example/jobs/2",
        posted_at=datetime.now(timezone.utc) - timedelta(days=2),
        contact_email="jobs@other.example",
        status="analyzed",
        source_type="ats",
        entity_type="INDIVIDUAL_VACANCY",
        entity_state="VACANCY_VERIFIED",
        entity_reason_code="matching_public_ats_posting_record",
        entity_evidence_url="https://boards.greenhouse.io/example/jobs/2",
        verification_status="verified",
        last_verified_at=datetime.now(timezone.utc),
        application_method="EMAIL",
        application_url="https://boards.greenhouse.io/example/jobs/2",
    )
    db.add(second_job)
    db.flush()
    second_decision = _decision(db, second_job)
    communicator = _draft_communicator("Je vous adresse ma candidature.")

    first = asyncio.run(_engine(
        db, config, profile, settings, communicator,
    ).run(first_job, first_decision, "APPLY", first_job.contact_email, "fr"))
    second = asyncio.run(_engine(
        db, config, profile, settings, communicator,
    ).run(second_job, second_decision, "APPLY", second_job.contact_email, "fr"))

    assert first["status"] == second["status"] == "drafted"
    assert create_calls == ["jobs@colas.example", "jobs@other.example"]


@pytest.mark.parametrize(
    "reconciliation_status",
    ["not_found", "multiple", "incomplete", "unavailable"],
)
def test_uncertain_gmail_outcome_is_reconciled_not_recreated(
    db, config, profile, settings, monkeypatch, reconciliation_status,
):
    settings = _settings(settings, email_mode="draft")
    monkeypatch.setattr(
        "app.email.service.validate",
        lambda **_kwargs: SimpleNamespace(allowed=True, reasons=[], checks={}),
    )
    create_calls = []
    monkeypatch.setattr(
        provider_mod, "create_draft",
        lambda *_args, **_kwargs: (
            create_calls.append("attempt")
            or (False, "", provider_mod.GMAIL_DRAFT_OUTCOME_UNCERTAIN)
        ),
    )
    job = _job(db)
    decision = _decision(db, job)
    first = asyncio.run(_engine(
        db, config, profile, settings,
        _draft_communicator("Je vous adresse ma candidature."),
    ).run(job, decision, "APPLY", job.contact_email, "fr"))
    assert first["status"] == "deferred"
    assert db.query(models.Email).one().delivery_state == "uncertain"
    db.commit()

    reloaded = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        monkeypatch.setattr(
            provider_mod, "reconcile_draft",
            lambda *_args, **_kwargs: provider_mod.DraftReconciliation(
                reconciliation_status,
            ),
        )
        persisted_job = reloaded.get(models.Job, job.id)
        persisted_decision = reloaded.get(models.Decision, decision.id)
        retry = asyncio.run(_engine(
            reloaded, config, profile, settings,
            _draft_communicator("Je vous adresse ma candidature."),
        ).run(
            persisted_job, persisted_decision, "APPLY",
            persisted_job.contact_email, "fr",
        ))
        assert retry["status"] == "recovery_required"
        assert create_calls == ["attempt"]
        assert reloaded.query(models.Application).filter_by(
            status="deferred"
        ).count() == 1
    finally:
        reloaded.close()


def test_initial_intent_commit_failure_never_calls_gmail(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings, email_mode="draft")
    monkeypatch.setattr(
        "app.email.service.validate",
        lambda **_kwargs: SimpleNamespace(allowed=True, reasons=[], checks={}),
    )
    job = _job(db)
    decision = _decision(db, job)
    create_calls = []
    monkeypatch.setattr(
        provider_mod, "create_draft",
        lambda *_args, **_kwargs: create_calls.append("called"),
    )
    monkeypatch.setattr(
        db, "commit", lambda: (_ for _ in ()).throw(
            RuntimeError("intent persistence failed"),
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="intent could not be persisted; no draft was attempted",
    ):
        asyncio.run(_engine(
            db, config, profile, settings,
            _draft_communicator("Je vous adresse ma candidature."),
        ).run(job, decision, "APPLY", job.contact_email, "fr"))

    assert create_calls == []
    db.rollback()
    assert db.query(models.Email).count() == 0


def test_job_scoped_unique_intent_blocks_competing_worker_before_create(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings, email_mode="draft")
    monkeypatch.setattr(
        "app.email.service.validate",
        lambda **_kwargs: SimpleNamespace(allowed=True, reasons=[], checks={}),
    )
    monkeypatch.setattr("app.scheduler.control.is_paused", lambda: False)
    job = _job(db)
    decision = _decision(db, job)
    create_calls = []
    monkeypatch.setattr(
        provider_mod, "create_draft",
        lambda *_args, **_kwargs: (
            create_calls.append("create") or (True, "draft-one", "")
        ),
    )
    result = asyncio.run(_engine(
        db, config, profile, settings,
        _draft_communicator("Je vous adresse ma candidature."),
    ).run(job, decision, "APPLY", job.contact_email, "fr"))
    assert result["status"] == "drafted"
    db.commit()

    competing = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        competing_job = competing.get(models.Job, job.id)
        competing_decision = competing.get(models.Decision, decision.id)
        with pytest.raises(IntegrityError):
            asyncio.run(_engine(
                competing, config, profile, settings,
                _draft_communicator("Je vous adresse ma candidature."),
            ).run(
                competing_job, competing_decision, "APPLY",
                competing_job.contact_email, "fr",
            ))
        competing.rollback()

        expected_key = hashlib.sha256(
            f"wca-gmail-draft-job:{job.id}".encode()
        ).hexdigest()
        assert db.query(models.Email).filter_by(
            draft_attempt_key=expected_key,
        ).count() == 1
        assert create_calls == ["create"]
    finally:
        competing.close()


def test_gmail_created_draft_is_recovered_if_final_database_commit_fails(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings, email_mode="draft")
    monkeypatch.setattr(
        "app.email.service.validate",
        lambda **_kwargs: SimpleNamespace(allowed=True, reasons=[], checks={}),
    )
    create_calls = []
    monkeypatch.setattr(
        provider_mod, "create_draft",
        lambda *_args, **_kwargs: (
            create_calls.append("created")
            or (True, "gmail-draft-after-db-error", "")
        ),
    )
    job = _job(db)
    decision = _decision(db, job)
    original_commit = db.commit
    commit_calls = 0

    def fail_after_intent_commit():
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            original_commit()
            return
        raise RuntimeError("simulated final persistence failure")

    monkeypatch.setattr(db, "commit", fail_after_intent_commit)
    first = asyncio.run(_engine(
        db, config, profile, settings,
        _draft_communicator("Je vous adresse ma candidature."),
    ).run(job, decision, "APPLY", job.contact_email, "fr"))
    assert first["draft_id"] == "gmail-draft-after-db-error"
    with pytest.raises(RuntimeError, match="simulated final persistence failure"):
        db.commit()
    db.rollback()

    reloaded = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        monkeypatch.setattr(
            provider_mod, "create_draft",
            lambda *_args, **_kwargs: pytest.fail("must reconcile, not create again"),
        )
        monkeypatch.setattr(
            provider_mod, "reconcile_draft",
            lambda _settings, key: (
                provider_mod.DraftReconciliation("found", "gmail-draft-after-db-error")
                if key else provider_mod.DraftReconciliation("not_found")
            ),
        )
        persisted_job = reloaded.get(models.Job, job.id)
        persisted_decision = reloaded.get(models.Decision, decision.id)
        recovered = asyncio.run(_engine(
            reloaded, config, profile, settings,
            _draft_communicator("Je vous adresse ma candidature."),
        ).run(
            persisted_job, persisted_decision, "APPLY",
            persisted_job.contact_email, "fr",
        ))
        assert recovered["status"] == "drafted"
        assert recovered["recovered"] is True
        assert recovered["draft_id"] == "gmail-draft-after-db-error"
        assert create_calls == ["created"]
        assert reloaded.query(models.Email).filter_by(
            delivery_state="confirmed",
            draft_id="gmail-draft-after-db-error",
        ).count() == 1
    finally:
        reloaded.close()


# ---------------------------------------------------------------------------
# OAuth hygiene
# ---------------------------------------------------------------------------

def test_live_send_requires_send_and_compose_with_optional_reconciliation_scope(settings):
    live = _settings(settings)

    assert gmail_oauth.required_scopes(live) == [
        gmail_oauth.GMAIL_COMPOSE_SCOPE,
        gmail_oauth.GMAIL_SEND_SCOPE,
    ]
    assert gmail_oauth.optional_scopes(live) == [
        gmail_oauth.GMAIL_READONLY_SCOPE,
    ]
    assert gmail_oauth.authorization_scopes(live) == [
        gmail_oauth.GMAIL_COMPOSE_SCOPE,
        gmail_oauth.GMAIL_SEND_SCOPE,
        gmail_oauth.GMAIL_READONLY_SCOPE,
    ]


def test_live_credentials_accept_send_and_compose_without_readonly(
    settings, monkeypatch,
):
    live = _settings(settings)

    class _Credentials:
        scopes = (
            gmail_oauth.GMAIL_SEND_SCOPE,
            gmail_oauth.GMAIL_COMPOSE_SCOPE,
        )
        valid = True
        expired = False
        refresh_token = "not-used"

    credentials = _Credentials()
    monkeypatch.setattr(
        gmail_oauth, "_credentials_from_file", lambda _path: credentials,
    )

    assert gmail_oauth.get_credentials(live) is credentials


def test_live_status_distinguishes_required_and_optional_scope(
    settings, monkeypatch, tmp_path,
):
    token_path = tmp_path / "gmail_token.json"
    token_path.write_text(json.dumps({
        "scopes": [
            gmail_oauth.GMAIL_SEND_SCOPE,
            gmail_oauth.GMAIL_COMPOSE_SCOPE,
        ],
    }), encoding="utf-8")
    live = _settings(settings, gmail_token_path=str(token_path))
    monkeypatch.setattr("app.config.get_settings", lambda: live)
    monkeypatch.setattr(
        gmail_oauth, "client_secret_path", lambda _settings: tmp_path / "client.json",
    )
    monkeypatch.setattr(gmail_oauth, "token_path", lambda _settings: token_path)
    cli.console = Console(record=True, width=200)

    cli.cmd_gmail_status(None)
    output = cli.console.export_text()

    assert f"required send scope (granted): {gmail_oauth.GMAIL_SEND_SCOPE}" in output
    assert f"required send scope (granted): {gmail_oauth.GMAIL_COMPOSE_SCOPE}" in output
    assert gmail_oauth.GMAIL_SEND_SCOPE in output
    assert gmail_oauth.GMAIL_COMPOSE_SCOPE in output
    assert f"optional automatic-reconciliation scope (missing): {gmail_oauth.GMAIL_READONLY_SCOPE}" in output
    assert "token validity is not tested with Gmail" in output


def test_live_send_without_read_scope_keeps_uncertain_attempt_blocked_and_allows_other_job(
    db, config, profile, settings, monkeypatch, tmp_path,
):
    live = _settings(
        settings,
        gmail_token_path=str(tmp_path / "gmail_token.json"),
    )
    token_path = tmp_path / "gmail_token.json"
    token_path.write_text(json.dumps({
        "scopes": [
            gmail_oauth.GMAIL_SEND_SCOPE,
            gmail_oauth.GMAIL_COMPOSE_SCOPE,
        ],
    }), encoding="utf-8")
    _allowed_live(monkeypatch)

    uncertain_job = _job(db, title="Uncertain First Application",
                         email="first@uncertain.example")
    uncertain_decision = _decision(db, uncertain_job)
    send_calls = []
    monkeypatch.setattr(
        provider_mod, "send",
        lambda *_args, **_kwargs: (
            send_calls.append("uncertain")
            or (False, "", provider_mod.GMAIL_SEND_OUTCOME_UNCERTAIN)
        ),
    )

    first = _run_live(
        db, config, profile, live, uncertain_job, uncertain_decision,
    )
    assert first["status"] == "recovery_required"
    assert db.query(models.LiveSendAttempt).one().state == "uncertain"

    reloaded = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        def forbidden_reconciliation_service(_settings):
            raise AssertionError("missing readonly scope must skip Sent API access")

        monkeypatch.setattr(
            gmail_oauth, "authenticated_service", forbidden_reconciliation_service,
        )
        persisted_job = reloaded.get(models.Job, uncertain_job.id)
        persisted_decision = reloaded.get(models.Decision, uncertain_decision.id)
        blocked_retry = _run_live(
            reloaded, config, profile, live, persisted_job, persisted_decision,
        )
        assert blocked_retry["status"] == "recovery_required"
        assert reloaded.query(models.LiveSendAttempt).one().state == "uncertain"
        assert send_calls == ["uncertain"]

        separate_job = _job(
            reloaded, title="Independent Eligible Application",
            email="second@independent.example",
        )
        separate_decision = _decision(reloaded, separate_job)

        def send_independent(*_args, **_kwargs):
            send_calls.append("independent")
            return True, "independent-message-id", ""

        monkeypatch.setattr(provider_mod, "send", send_independent)
        independent_result = _run_live(
                reloaded, config, profile,
                live.model_copy(update={"approved_live_job_id": str(separate_job.id)}),
                separate_job, separate_decision,
            )

        assert independent_result["status"] == "sent"
        assert independent_result["message_id"] == "independent-message-id"
        assert send_calls == ["uncertain", "independent"]
        attempts = reloaded.query(models.LiveSendAttempt).order_by(
            models.LiveSendAttempt.id,
        ).all()
        assert [attempt.state for attempt in attempts] == [
            "uncertain", "confirmed_sent",
        ]
    finally:
        reloaded.close()


def test_load_client_config_reads_secret_without_logging(settings, caplog, tmp_path):
    secret_path = tmp_path / "client_secret.json"
    secret_value = "GOCSPX-hermetic-secret-value-1234"
    secret_path.write_text(json.dumps({
        "web": {
            "client_id": "c-hermetic.apps.googleusercontent.com",
            "project_id": "hermetic",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": secret_value,
            "redirect_uris": ["http://localhost"],
        }
    }))
    s = settings.model_copy(update={"gmail_client_secret_path": str(secret_path)})

    with caplog.at_level(logging.INFO):
        cfg = gmail_oauth.load_client_config(s)

    assert "web" in cfg
    assert secret_value not in caplog.text


def _allowed_live(monkeypatch):
    monkeypatch.setattr(
        "app.email.service.validate",
        lambda **_kwargs: SimpleNamespace(allowed=True, reasons=[], checks={}),
    )


def _run_live(db, config, profile, settings, job, decision):
    return asyncio.run(_engine(
        db, config, profile, settings,
        _draft_communicator("Je vous adresse ma candidature."),
    ).run(job, decision, "APPLY", job.contact_email, "fr"))


def test_live_prepared_intent_after_crash_before_dispatch_is_safe_to_retry(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings)
    _allowed_live(monkeypatch)
    job = _job(db)
    decision = _decision(db, job)
    send_calls = []
    monkeypatch.setattr(
        provider_mod, "send",
        lambda *_args, **_kwargs: (
            send_calls.append("sent") or (True, "gmail-message", "")
        ),
    )
    original_commit = db.commit
    commits = 0

    def fail_dispatch_commit():
        nonlocal commits
        commits += 1
        if commits == 2:
            raise RuntimeError("simulated crash before dispatch")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_dispatch_commit)
    with pytest.raises(RuntimeError, match="dispatch state could not be persisted"):
        _run_live(db, config, profile, settings, job, decision)
    assert send_calls == []
    assert db.query(models.LiveSendAttempt).one().state == "prepared"

    monkeypatch.setattr(db, "commit", original_commit)
    result = _run_live(db, config, profile, settings, job, decision)

    assert result["status"] == "sent"
    assert send_calls == ["sent"]
    assert sorted(
        attempt.state for attempt in db.query(models.LiveSendAttempt).all()
    ) == ["confirmed_sent", "definitively_failed"]


def test_gmail_success_with_final_persistence_failure_recovers_from_sent(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings)
    _allowed_live(monkeypatch)
    job = _job(db)
    decision = _decision(db, job)
    send_calls = []
    monkeypatch.setattr(
        provider_mod, "send",
        lambda *_args, **_kwargs: (
            send_calls.append("sent") or (True, "gmail-message", "")
        ),
    )
    original_commit = db.commit
    commits = 0

    def fail_final_commit():
        nonlocal commits
        commits += 1
        if commits == 3:
            raise RuntimeError("simulated final persistence failure")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_final_commit)
    with pytest.raises(RuntimeError, match="reconciliation is required"):
        _run_live(db, config, profile, settings, job, decision)
    monkeypatch.setattr(db, "commit", original_commit)

    reloaded = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        monkeypatch.setattr(
            provider_mod, "reconcile_live_send",
            lambda *_args, **_kwargs: provider_mod.LiveSendReconciliation(
                "found", "gmail-message", 1,
            ),
        )
        persisted_job = reloaded.get(models.Job, job.id)
        persisted_decision = reloaded.get(models.Decision, decision.id)
        result = _run_live(
            reloaded, config, profile, settings, persisted_job, persisted_decision,
        )
        assert result["status"] == "sent"
        assert result["recovered"] is True
        assert result["message_id"] == "gmail-message"
        assert send_calls == ["sent"]
        assert reloaded.query(models.LiveSendAttempt).one().state == "confirmed_sent"
    finally:
        reloaded.close()


def test_timeout_with_one_exact_sent_match_confirms_without_resending(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings)
    _allowed_live(monkeypatch)
    job = _job(db)
    decision = _decision(db, job)
    send_calls = []
    monkeypatch.setattr(
        provider_mod, "send",
        lambda *_args, **_kwargs: (
            send_calls.append("timeout")
            or (False, "", provider_mod.GMAIL_SEND_OUTCOME_UNCERTAIN)
        ),
    )
    first = _run_live(db, config, profile, settings, job, decision)
    assert first["status"] == "recovery_required"
    db.commit()

    reloaded = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        monkeypatch.setattr(
            provider_mod, "reconcile_live_send",
            lambda *_args, **_kwargs: provider_mod.LiveSendReconciliation(
                "found", "gmail-message", 1,
            ),
        )
        result = _run_live(
            reloaded, config, profile, settings,
            reloaded.get(models.Job, job.id),
            reloaded.get(models.Decision, decision.id),
        )
        assert result["status"] == "sent"
        assert result["recovered"] is True
        assert send_calls == ["timeout"]
    finally:
        reloaded.close()


@pytest.mark.parametrize("reconciliation_status", [
    "not_found", "multiple", "incomplete", "unavailable", "ambiguous",
])
def test_uncertain_live_send_never_retries_without_exact_sent_match(
    db, config, profile, settings, monkeypatch, reconciliation_status,
):
    settings = _settings(settings)
    _allowed_live(monkeypatch)
    job = _job(db)
    decision = _decision(db, job)
    send_calls = []
    monkeypatch.setattr(
        provider_mod, "send",
        lambda *_args, **_kwargs: (
            send_calls.append("send")
            or (False, "", provider_mod.GMAIL_SEND_OUTCOME_UNCERTAIN)
        ),
    )
    first = _run_live(db, config, profile, settings, job, decision)
    assert first["status"] == "recovery_required"
    db.commit()

    reloaded = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        monkeypatch.setattr(
            provider_mod, "reconcile_live_send",
            lambda *_args, **_kwargs: provider_mod.LiveSendReconciliation(
                reconciliation_status,
            ),
        )
        result = _run_live(
            reloaded, config, profile, settings,
            reloaded.get(models.Job, job.id),
            reloaded.get(models.Decision, decision.id),
        )
        assert result["status"] == "recovery_required"
        assert send_calls == ["send"]
        assert reloaded.query(models.LiveSendAttempt).one().state == "uncertain"
    finally:
        reloaded.close()


def test_pre_dispatch_gmail_failure_is_recorded_as_definitive_and_retryable(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings)
    _allowed_live(monkeypatch)
    job = _job(db)
    decision = _decision(db, job)
    calls = []

    def no_dispatch(*_args, **_kwargs):
        calls.append("not-attempted")
        return False, "", f"{provider_mod.GMAIL_SEND_NOT_ATTEMPTED} OAuth setup failed."

    monkeypatch.setattr(provider_mod, "send", no_dispatch)
    result = _run_live(db, config, profile, settings, job, decision)

    attempt = db.query(models.LiveSendAttempt).one()
    assert result["status"] == "failed"
    assert attempt.state == "definitively_failed"
    assert attempt.active_job_id is None
    assert calls == ["not-attempted"]


def test_live_gate_failure_creates_no_attempt_and_never_calls_provider(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings)
    job = _job(db)
    decision = _decision(db, job)
    monkeypatch.setattr(
        provider_mod, "send",
        lambda *_args, **_kwargs: pytest.fail("blocked application reached Gmail"),
    )
    result = asyncio.run(_engine(
        db, config, profile, settings, _draft_communicator(
            "I have 25 years of experience in civil engineering.",
        ),
    ).run(job, decision, "APPLY", job.contact_email, "fr"))

    assert result["status"] == "blocked"
    assert db.query(models.LiveSendAttempt).count() == 0


def test_active_live_attempt_database_lock_blocks_competing_worker(
    db, config, profile, settings, monkeypatch,
):
    settings = _settings(settings)
    _allowed_live(monkeypatch)
    job = _job(db)
    decision = _decision(db, job)
    monkeypatch.setattr(
        provider_mod, "send",
        lambda *_args, **_kwargs: (
            False, "", provider_mod.GMAIL_SEND_OUTCOME_UNCERTAIN,
        ),
    )
    first = _run_live(db, config, profile, settings, job, decision)
    assert first["status"] == "recovery_required"
    db.commit()

    competing = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        attempt = db.query(models.LiveSendAttempt).one()
        duplicate = models.LiveSendAttempt(
            attempt_key="competing-worker-attempt",
            active_job_id=job.id,
            job_id=job.id,
            application_id=attempt.application_id,
            email_id=attempt.email_id,
            employer_name=attempt.employer_name,
            recipient=attempt.recipient,
            subject=attempt.subject,
            message_fingerprint=attempt.message_fingerprint,
            state="dispatching",
        )
        competing.add(duplicate)
        with pytest.raises(IntegrityError):
            competing.commit()
        competing.rollback()
        assert db.query(models.LiveSendAttempt).count() == 1
    finally:
        competing.close()


def test_secret_and_token_never_logged_on_failure(settings, caplog, monkeypatch):
    settings = _settings(settings)
    secret_value = None
    import os
    if os.path.exists("secrets/client_secret.json"):
        with open("secrets/client_secret.json", encoding="utf-8") as secret_file:
            secret_value = json.load(secret_file)["web"]["client_secret"]

    def failing_service(s):
        raise RuntimeError("oauth handshake failed")

    monkeypatch.setattr(gmail_oauth, "authenticated_service", failing_service)
    with caplog.at_level(logging.INFO):
        ok, _mid, err = provider_mod.send(settings, to="x@y.example", subject="s", body="b")

    assert ok is False
    for secretish in (secret_value, "GOCSPX", "gmail_token", "refresh_token"):
        if secretish:
            assert secretish not in caplog.text
            assert secretish not in err


def test_save_credentials_never_logs_token_contents(settings, caplog, tmp_path):
    settings = _settings(settings, gmail_token_path=str(tmp_path / "tok.json"))
    token_value = {"token": "FACEBOOK-TOKEN-TOKEN", "refresh_token": "RT-SECRET", "scope": "https://www.googleapis.com/auth/gmail.send"}

    class _Creds:
        def to_json(self):
            return json.dumps(token_value)

    with caplog.at_level(logging.INFO):
        gmail_oauth.save_credentials(settings, _Creds())

    assert (tmp_path / "tok.json").exists()
    for secret in token_value.values():
        if isinstance(secret, str) and secret != token_value["scope"]:
            assert secret not in caplog.text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_save_credentials_restricts_permissions(settings, tmp_path):
    settings = _settings(settings, gmail_token_path=str(tmp_path / "tok.json"))
    gmail_oauth.save_credentials(settings, _CredsStub())
    mode = (tmp_path / "tok.json").stat().st_mode & 0o777
    assert mode == 0o600


class _CredsStub:
    def to_json(self):
        return json.dumps({"token": "t", "refresh_token": "r", "scopes": ["https://www.googleapis.com/auth/gmail.send"]})


# ---------------------------------------------------------------------------
# email modes: dry_run / draft / live + emergency pause + daily accounting
# ---------------------------------------------------------------------------

def test_default_email_mode_is_draft(settings):
    assert settings.email_mode == "draft"


def test_dry_run_never_touches_gmail(db, config, profile, settings, monkeypatch):
    job = _job(db)
    decision = _decision(db, job)
    settings = settings.model_copy(update={"enable_email": False, "email_mode": "dry_run"})

    def boom(*a, **k):
        raise AssertionError("dry_run must never call Gmail providers")

    monkeypatch.setattr(provider_mod, "create_draft", boom)
    monkeypatch.setattr(provider_mod, "_send_gmail", boom)
    result = asyncio.run(_engine(db, config, profile, settings, _real_communicator(profile)).run(
        job, decision, "APPLY", job.contact_email, "fr"))

    assert result["status"] == "dry_run"
    assert job.status == "analyzed"  # no outbound artifact -> can be re-tried
    from app.memory.store import count_dispatched_today
    assert count_dispatched_today(db) == 0  # dry_run never consumes the daily budget


def test_draft_mode_creates_gmail_draft_and_records_id(db, config, profile, settings, monkeypatch):
    job = _job(db)
    decision = _decision(db, job)
    settings = settings.model_copy(update={"enable_email": True, "email_mode": "draft"})

    def fake_draft(settings_, *, to, subject, body, attachments, attempt_key=""):
        return True, "draft-xyz-99", ""

    monkeypatch.setattr(provider_mod, "create_draft", fake_draft)
    result = asyncio.run(_engine(db, config, profile, settings, _real_communicator(profile)).run(
        job, decision, "APPLY", job.contact_email, "fr"))

    assert result["status"] == "drafted"
    assert result["draft_id"] == "draft-xyz-99"
    email = db.query(models.Email).one()
    assert email.status == "drafted"
    assert email.mode == "draft"
    assert email.draft_id == "draft-xyz-99"
    assert email.delivery_state == "confirmed"
    assert email.draft_attempt_key
    app = db.query(models.Application).one()
    assert app.status == "drafted"
    assert job.status == "acted"
    from app.memory.store import count_dispatched_today
    assert count_dispatched_today(db) == 1


def test_draft_mode_requires_enable_email(db, config, profile, settings, monkeypatch):
    job = _job(db)
    decision = _decision(db, job)
    settings = settings.model_copy(update={"enable_email": False, "email_mode": "draft"})

    def boom(*a, **k):
        raise AssertionError("Gmail must not be touched while ENABLE_EMAIL=false")

    monkeypatch.setattr(provider_mod, "create_draft", boom)
    result = asyncio.run(_engine(db, config, profile, settings, _real_communicator(profile)).run(
        job, decision, "APPLY", job.contact_email, "fr"))

    assert result["status"] == "blocked"
    assert "ENABLE_EMAIL" in result["reason"]


def test_emergency_pause_blocks_outbound_even_in_dry_run(db, config, profile, settings, monkeypatch):
    from app.scheduler.control import set_paused

    job = _job(db)
    decision = _decision(db, job)
    settings = settings.model_copy(update={"enable_email": True, "email_mode": "live"})

    def boom(*a, **k):
        raise AssertionError("paused agent must not emit outbound")

    monkeypatch.setattr(provider_mod, "_send_gmail", boom)
    set_paused(True)
    try:
        result = asyncio.run(_engine(db, config, profile, settings, _real_communicator(profile)).run(
            job, decision, "APPLY", job.contact_email, "fr"))
    finally:
        set_paused(False)

    assert result["status"] == "blocked"
    assert "pause" in result["reason"].lower()


def test_live_mode_cap_overflow_keeps_gmail_draft(db, config, profile, settings, monkeypatch):
    """At the 10/day outbound cap, live mode keeps further messages as drafts
    instead of blocking them (the ask: send up to 10, hold the rest in draft)."""
    from app.memory.store import add_application, count_dispatched_today

    job = _job(db)
    decision = _decision(db, job)
    settings = _settings(settings)  # live + enable_email

    for i in range(10):
        seed = _job(db, title=f"Seeded Job {i}", email=f"seed{i}@colas.example")
        a = add_application(db, seed.id, None, "APPLY", 88.0, seed.contact_email)
        a.status = "sent"
        a.sent_at = datetime.now(timezone.utc)
    db.commit()
    assert count_dispatched_today(db) == 10

    drafts = []

    def fake_draft(settings_, *, to, subject, body, attachments, attempt_key=""):
        drafts.append(to)
        return True, "draft-cap-1", ""

    monkeypatch.setattr(provider_mod, "create_draft", fake_draft)
    result = asyncio.run(_engine(db, config, profile, settings, _real_communicator(profile)).run(
        job, decision, "APPLY", job.contact_email, "fr"))

    assert result["status"] == "drafted"
    assert result["draft_id"] == "draft-cap-1"
    assert drafts == [job.contact_email]  # drafted, not blocked, not sent
    assert count_dispatched_today(db) == 11  # drafts consume the daily budget too


def test_live_mode_content_block_stays_blocked_not_drafted(db, config, profile, settings, monkeypatch):
    """Only daily-cap failures soften into drafts; content violations stay blocked."""
    job = _job(db)
    decision = _decision(db, job)
    settings = _settings(settings)

    def boom(*a, **k):
        raise AssertionError("a hard block must never create a draft")

    monkeypatch.setattr(provider_mod, "create_draft", boom)
    communicator = _draft_communicator("I worked at Meta for 10 years as a rocket scientist.")
    result = asyncio.run(_engine(db, config, profile, settings, communicator).run(
        job, decision, "APPLY", job.contact_email, "fr"))

    assert result["status"] == "blocked"
