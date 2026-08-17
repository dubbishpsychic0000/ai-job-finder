"""Gmail sending layer tests — mocked provider, NO real emails ever sent.

Covers the full ApplicationEngine path for the Gmail provider: safety gate runs
before send, Gmail message ID is persisted, dry-run blocks at the engine AND at
the transport, duplicates are rejected, and OAuth secrets never leak into logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

import pytest

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
        description="road construction VRD", url="https://example.com/j",
        posted_at=datetime.now(timezone.utc) - timedelta(days=posted_days_ago),
        contact_email=email, status="analyzed",
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
                                       "email_mode": "live", **kw})


def _engine(db, config, profile, settings, communicator):
    return ApplicationEngine(db, config, settings, profile, communicator)


def _real_communicator(profile):
    from app.agents.llm import NullLLM

    return CommunicationAgent(NullLLM(profile), profile)


def _draft_communicator(body):
    class _Fake:
        async def generate(self, job, action, target_language="en", recipient_name=""):
            return {"subject": "Application", "body": body}
    return _Fake()


# ---------------------------------------------------------------------------
# happy path: send via Gmail, message id + app status persisted
# ---------------------------------------------------------------------------

def test_gmail_send_persists_status_and_message_id(db, config, profile, settings, monkeypatch, caplog):
    job = _job(db)
    decision = _decision(db, job)
    settings = _settings(settings)
    calls = []

    def fake_gmail(settings_, to, subject, body, attachments):
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


# ---------------------------------------------------------------------------
# OAuth hygiene
# ---------------------------------------------------------------------------

def test_scope_is_send_only():
    assert gmail_oauth.GMAIL_SEND_SCOPE == "https://www.googleapis.com/auth/gmail.send"


def test_load_client_config_reads_secret_without_logging(settings, caplog, tmp_path):
    secret_value = None
    src = "secrets/client_secret.json"
    import os
    if os.path.exists(src):
        secret_value = json.load(open(src, encoding="utf-8"))["web"]["client_secret"]

    with caplog.at_level(logging.INFO):
        cfg = gmail_oauth.load_client_config(settings)

    assert "web" in cfg
    if secret_value:
        assert secret_value not in caplog.text


def test_secret_and_token_never_logged_on_failure(settings, caplog, monkeypatch):
    settings = _settings(settings)
    secret_value = None
    import os
    if os.path.exists("secrets/client_secret.json"):
        secret_value = json.load(open("secrets/client_secret.json", encoding="utf-8"))["web"]["client_secret"]

    def failing_service(s):
        raise RuntimeError("oauth handshake failed")

    monkeypatch.setattr(gmail_oauth, "authenticated_service", failing_service)
    with caplog.at_level(logging.INFO):
        ok, mid, err = provider_mod.send(settings, to="x@y.example", subject="s", body="b")

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

    def fake_draft(settings_, *, to, subject, body, attachments):
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

    def fake_draft(settings_, *, to, subject, body, attachments):
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
